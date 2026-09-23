"""Mirror of the toasts the Omarchy shell has already shown.

`org.freedesktop.Notifications` has exactly one owner and the Omarchy shell is
it, so there is no second server to start and no bus to eavesdrop on. What the
shell does do is write every toast to disk: one JSON file per notification under
`~/.local/state/omarchy/notifications/` while it is on screen, moved into
`history/` when it goes away. That directory is the collection point.

Two things follow from how the shell keeps it:

- **Only ten survive.** `historyLimit` is 10, and evicting a file deletes the
  icons it referenced. Anything watching has to see a notification while it is
  still there, so this watcher keeps its own bounded copy in memory.
- **`execArgv` is never kept.** The snapshot carries a command line the shell
  would run on click. It is not stored, not published and not executed from a
  remote request. A client sees `has_action` and nothing else; invoking maps
  onto the shell's own `invokeLast`, which is the same thing `SUPER+ALT+,`
  already does on the host.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import threading
import time

STATE_DIR = Path.home() / ".local/state/omarchy/notifications"
SHELL = "/usr/share/omarchy/bin/omarchy-shell"
OMARCHY_PATH = "/usr/share/omarchy"
FILE_NAME = re.compile(r"(?P<ms>[0-9]{1,16})-(?P<id>[0-9]{1,12})\.json\Z")
IDENTIFIER = re.compile(r"[0-9]{1,16}-[0-9]{1,12}\Z")
URGENCIES = {0: "low", 1: "normal", 2: "critical"}
MAX_FILE_BYTES = 256 * 1024
MAX_TEXT = 2000
HISTORY_LIMIT = 500
POLL_SECONDS = 2.0
DND_FILE = "notifications.json"
# How long to leave a shell alone after it refused to answer, and - CORE-2 §3 -
# the least time between two `qs` spawns just to *read* DND when the state
# file cannot answer.
DND_RETRY_SECONDS = 30.0
DND_FILE_LIMIT = 64 * 1024
# CORE-2 §3: `qs` is a Qt program; under LANG=C it logs four lines of "Detected
# locale C ... switched to C.UTF-8" into the user journal on every spawn.
UTF8_LOCALE = "C.UTF-8"


def read_dnd_file(path: Path):
    """DND as the Omarchy shell last saved it, or None when the file cannot say.

    The notifications plugin (`plugins/notifications/Service.qml`) hydrates
    `doNotDisturb` from `~/.local/state/omarchy/notifications.json` at start and
    writes `{"version": 3, "dnd": <bool>}` back 200 ms after every change. So the
    file *is* the state - reading it costs no process, where asking the shell
    over IPC spawns `qs` every time (CORE-2 §3, G17). None means: missing (the
    user never toggled DND on this install), unreadable, too large, not JSON, or
    no boolean `dnd` in it; the caller then falls back to asking the shell.
    """
    try:
        with open(path, "rb") as stream:
            raw = stream.read(DND_FILE_LIMIT + 1)
    except OSError:
        return None
    if len(raw) > DND_FILE_LIMIT:
        return None
    try:
        value = json.loads(raw).get("dnd")
    except (ValueError, AttributeError):
        return None
    return value if type(value) is bool else None


class NotificationsUnavailable(ValueError):
    def __init__(self, code="notifications_unavailable", status=503):
        self.code, self.status = code, status
        super().__init__(code)


def _text(value, limit=MAX_TEXT):
    return value[:limit] if isinstance(value, str) else ""


def project(identifier: str, document: dict, *, active: bool) -> dict:
    """The wire row. `execArgv` is read only to answer "is there an action"."""
    urgency = document.get("urgency")
    timestamp = document.get("timestamp")
    return {"id": identifier,
            "app": _text(document.get("app"), 200) or "unknown",
            "summary": _text(document.get("summary")),
            "body": _text(document.get("body")),
            "glyph": _text(document.get("glyph"), 64),
            "urgency": URGENCIES.get(urgency if type(urgency) is int else 1, "normal"),
            "timestamp": timestamp if type(timestamp) is int else 0,
            "has_action": bool(document.get("execArgv")),
            "active": active}


def _shell(arguments, *, runner=None, timeout=5.0):
    argv = (SHELL, "notifications", *arguments)
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()),
                   "OMARCHY_PATH": OMARCHY_PATH, "LANG": UTF8_LOCALE, "LC_ALL": UTF8_LOCALE}
    run = runner or subprocess.run
    try:
        result = run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                     timeout=timeout, env=environment, shell=False)
    except (OSError, subprocess.SubprocessError):
        raise NotificationsUnavailable() from None
    if result.returncode:
        raise NotificationsUnavailable()
    return (result.stdout or "").strip()


class NotificationMirror:
    """Watches the two directories and publishes what appears in them."""

    def __init__(self, hub=None, *, state_dir=None, runner=None, poll_seconds=POLL_SECONDS,
                 clock=time.monotonic):
        self.hub = hub
        self.state_dir = Path(state_dir) if state_dir is not None else STATE_DIR
        self.history_dir = self.state_dir / "history"
        # `docs/notifications.md`: "DND itself is
        # `~/.local/state/omarchy/notifications.json`". The file is only used as
        # a *wakeup* - its shape is the shell's business - and the reading comes
        # from `omarchy-shell notifications dndState`, which is the same command
        # the bar indicator uses.
        self.dnd_path = self.state_dir.parent / DND_FILE
        self._dnd = None
        self._dnd_stamp = None
        # A shell that is not answering must not be asked again two seconds
        # later. Without this the first failed read retried on every pass —
        # a subprocess against a busy IPC every 2 s, forever.
        self._dnd_retry_at = 0.0
        # When the shell last answered a set; the file lags it by its 200 ms
        # save timer, and a read in between must not report the old value.
        self._dnd_set_at = 0.0
        # CORE-2 §3: the last time `dndState` had to be asked because the file
        # could not answer, and what it said.
        self._dnd_asked_at = None
        self._dnd_asked = None
        self.runner = runner
        self.poll_seconds = poll_seconds
        self.clock = clock
        self._rows: dict[str, dict] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._started = False

    # --- reading -------------------------------------------------------------
    def _read_file(self, path: Path):
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return None
            document = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    def scan(self) -> list[dict]:
        """One pass over both directories; returns the rows that are new here."""
        found = []
        for directory, active in ((self.state_dir, True), (self.history_dir, False)):
            try:
                names = sorted(os.listdir(directory))
            except OSError:
                continue
            for name in names:
                match = FILE_NAME.fullmatch(name)
                if match is None:
                    continue
                identifier = name[:-5]
                document = self._read_file(directory / name)
                if document is None:
                    continue
                found.append(project(identifier, document, active=active))
        fresh = []
        with self._lock:
            for row in found:
                previous = self._rows.get(row["id"])
                if previous is None:
                    self._rows[row["id"]] = row
                    self._order.append(row["id"])
                    fresh.append(row)
                elif previous["active"] and not row["active"]:
                    previous["active"] = False
            # An active file that vanished between passes has been dismissed.
            live = {row["id"] for row in found if row["active"]}
            for identifier in self._order:
                stored = self._rows[identifier]
                if stored["active"] and identifier not in live:
                    stored["active"] = False
            while len(self._order) > HISTORY_LIMIT:
                self._rows.pop(self._order.pop(0), None)
        for row in fresh:
            if self.hub is not None:
                self.hub.publish("notification.posted", dict(row))
        self.refresh_dnd()
        return fresh

    # --- do not disturb ------------------------------------------------------
    def refresh_dnd(self, *, force=False):
        """Read DND when its file moved, and publish a change into hub state.

        Study 04 §7 open question 3 / review item 17: a user who switches DND on
        the desktop must not leave the iPad's bell lying. The client reads
        `state.notifications.dnd` and keeps no boolean of its own, so this is the
        only writer. It reads when the file's mtime moved rather than on every
        2 s pass; since CORE-2 the read is the file itself, not a subprocess.
        """
        now = self.clock()
        try:
            stamp = self.dnd_path.stat().st_mtime_ns
        except OSError:
            stamp = None
        unchanged = stamp == self._dnd_stamp
        if not force and unchanged and (self._dnd is not None or now < self._dnd_retry_at):
            return self._dnd
        self._dnd_stamp = stamp
        try:
            value = self.dnd()["dnd"]
        except NotificationsUnavailable:
            # The shell is not answering — it is busy, or this host has no
            # Omarchy shell at all. The last reading stands, nothing is
            # invented, and the next attempt is a long way off rather than on
            # the next 2 s pass.
            self._dnd_retry_at = now + DND_RETRY_SECONDS
            return self._dnd
        self._dnd_retry_at = 0.0
        self.publish_dnd(value)
        return value

    def publish_dnd(self, value) -> None:
        self._dnd = value
        if self.hub is None:
            return
        current = (self.hub.state_view("notifications")["notifications"] or {}).get("dnd")
        if current == value:
            return
        self.hub.update_state({"notifications": {"dnd": value}}, event_type="notifications.changed")

    def history(self, *, since=None, limit=None) -> dict:
        if since is not None and not IDENTIFIER.fullmatch(str(since)):
            raise NotificationsUnavailable("invalid_request", 400)
        count = 100
        if limit is not None:
            if not str(limit).isdigit() or not 1 <= int(limit) <= HISTORY_LIMIT:
                raise NotificationsUnavailable("invalid_request", 400)
            count = int(limit)
        with self._lock:
            rows = [dict(self._rows[identifier]) for identifier in self._order]
        if since is not None:
            rows = [row for row in rows if row["id"] > str(since)]
        rows.sort(key=lambda row: (row["timestamp"], row["id"]))
        rows = rows[-count:]
        return {"notifications": rows, "cursor": rows[-1]["id"] if rows else (str(since) if since else None),
                "limit": count, "history_limit": HISTORY_LIMIT}

    def newest_active(self):
        with self._lock:
            active = [row for row in self._rows.values() if row["active"]]
        if not active:
            return None
        return max(active, key=lambda row: (row["timestamp"], row["id"]))

    # --- acting --------------------------------------------------------------
    def act(self, identifier: str, action: str) -> dict:
        if not IDENTIFIER.fullmatch(identifier or ""):
            raise NotificationsUnavailable("invalid_request", 400)
        with self._lock:
            row = self._rows.get(identifier)
        if row is None:
            raise NotificationsUnavailable("notification_unknown", 404)
        newest = self.newest_active()
        if action == "invoke":
            # The shell only exposes "fire the default action on the newest
            # popup". Inventing an id-addressed invoke would mean running the
            # stored execArgv here, which this module refuses to keep at all.
            if not row["has_action"]:
                raise NotificationsUnavailable("notification_not_actionable", 409)
            if newest is None or newest["id"] != identifier:
                raise NotificationsUnavailable("notification_not_actionable", 409)
            result = _shell(("invokeLast",), runner=self.runner)
        elif action == "dismiss":
            if newest is not None and newest["id"] == identifier:
                result = _shell(("dismissOne",), runner=self.runner)
            elif row["active"] and row["summary"]:
                result = _shell(("dismiss", row["summary"]), runner=self.runner)
            else:
                result = "none"
        else:
            raise NotificationsUnavailable("invalid_request", 400)
        if result == "ok":
            with self._lock:
                stored = self._rows.get(identifier)
                if stored is not None:
                    stored["active"] = False
        return {"id": identifier, "action": action, "result": result}

    def dnd(self) -> dict:
        """The shell's DND, from its state file; the shell itself at most every 30 s.

        CORE-2 §3 (G17): this used to be one `qs` process per read.
        """
        try:
            written = self.dnd_path.stat().st_mtime
        except OSError:
            written = None
        if self._dnd is not None and written is not None and written < self._dnd_set_at:
            return {"dnd": self._dnd}
        value = read_dnd_file(self.dnd_path)
        if value is not None:
            return {"dnd": value}
        now = self.clock()
        if self._dnd_asked_at is not None and now - self._dnd_asked_at < DND_RETRY_SECONDS:
            if self._dnd_asked is None:
                raise NotificationsUnavailable()
            return {"dnd": self._dnd_asked}
        self._dnd_asked_at, self._dnd_asked = now, None
        self._dnd_asked = _shell(("dndState",), runner=self.runner) == "on"
        return {"dnd": self._dnd_asked}

    def set_dnd(self, enabled) -> dict:
        if enabled is None:
            result = {"dnd": _shell(("toggleDnd",), runner=self.runner) == "on"}
        else:
            if type(enabled) is not bool:
                raise NotificationsUnavailable("invalid_request", 400)
            result = {"dnd": _shell(("setDnd", "true" if enabled else "false"), runner=self.runner) == "on"}
        # The shell has answered with what it actually is, so the state a client
        # reads moves now rather than one watch pass later. The client's switch
        # follows the state, never its own tap (D-15).
        self._dnd_set_at = time.time()
        self._dnd_asked_at = None
        self.publish_dnd(result["dnd"])
        return result

    # --- the watch loop ------------------------------------------------------
    def _inotify(self):
        """A wakeup source, never the source of truth: every pass rescans.

        inotify is reached through ctypes because core has no watcher
        dependency. When it cannot be set up the loop is a plain 2 s poll,
        which is what the module promises anyway.
        """
        try:
            import ctypes
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            fd = libc.inotify_init1(0o4000)  # IN_NONBLOCK
            if fd < 0:
                return None, None
            mask = 0x00000008 | 0x00000080 | 0x00000200  # CLOSE_WRITE | MOVED_TO | DELETE
            for directory in (self.state_dir, self.history_dir, self.dnd_path.parent):
                libc.inotify_add_watch(fd, str(directory).encode(), mask)
            return fd, libc
        except Exception:
            return None, None

    def _loop(self):
        fd, libc = self._inotify()
        selector = selectors.DefaultSelector()
        if fd is not None:
            selector.register(fd, selectors.EVENT_READ)
        try:
            while not self._stop.is_set():
                try:
                    self.scan()
                except Exception:
                    pass
                if fd is None:
                    self._stop.wait(self.poll_seconds)
                    continue
                if selector.select(self.poll_seconds):
                    try:
                        os.read(fd, 65536)
                    except OSError:
                        pass
        finally:
            selector.close()
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def start(self):
        if self._started:
            return
        self._started = True
        self.refresh_dnd(force=True)
        self.scan()
        self._thread = threading.Thread(target=self._loop, name="omodachi-notifications", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_seconds + 1)
            self._thread = None
        self._started = False


class NotificationService:
    """The async face the HTTP boundary talks to."""

    def __init__(self, mirror: NotificationMirror):
        self.mirror = mirror

    async def history(self, *, since=None, limit=None):
        return await asyncio.to_thread(self.mirror.history, since=since, limit=limit)

    async def act(self, identifier, action):
        return await asyncio.to_thread(self.mirror.act, identifier, action)

    async def dnd(self):
        return await asyncio.to_thread(self.mirror.dnd)

    async def set_dnd(self, enabled):
        return await asyncio.to_thread(self.mirror.set_dnd, enabled)
