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

NOTIFY-3. The Omodachi plugin posts one notification per pairing request and
per SSH-key request (`omarchy-notification-send --app-name Omodachi -u critical
... --exec omarchy-shell shell summon com.omodachi.host {"view": "devices",
...}`). The shell never expires a critical toast, and nothing took these down
once the request was decided, so they stayed on the desktop - and in every
client's list - for good. The mirror now recognises its own (the app name *and*
the plugin's summon command, see `request_marker`), asks core which requests are
still waiting, and withdraws the rest: they leave every client's list at once
(`notification.updated` with `withdrawn: true`), the toast is taken off the
desktop through the shell's own `dismiss`, and the file the shell keeps of it
is removed. Nothing that is not ours is ever matched, dismissed or deleted.
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

# NOTIFY-3: what makes a notification ours. The plugin posts with this app name
# and a click command that summons this plugin's Devices view; a pairing
# request names its request id there. An SSH-key request names no request, so
# it is known by its title and the device id its body starts with.
OUR_APP = "Omodachi"
PLUGIN_ID = "com.omodachi.host"
SUMMON = ["omarchy-shell", "shell", "summon", PLUGIN_ID]
REQUEST_ID = re.compile(r"pair_[0-9a-f]{32}\Z")
DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SSH_TITLE_END = "\u201d wants an SSH login"
KINDS = ("pairing_request", "ssh_request")
# How long a shell `dismiss` that answered "ok" may take to move the file
# before the file is removed by hand instead.
WITHDRAW_GRACE_SECONDS = 4.0
WITHDRAWN_LIMIT = 2000


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


def request_marker(document: dict):
    """NOTIFY-3. Which Omodachi request this notification announces, or None.

    Both halves must hold: the app name is ours *and* the stored click command
    is exactly the plugin's summon of its Devices view. A notification any other
    sender posts - even one calling itself "Omodachi" - is never a marker, so
    nothing here can withdraw it.
    """
    if not isinstance(document, dict) or document.get("app") != OUR_APP:
        return None
    raw = document.get("execArgv")
    if not isinstance(raw, str) or len(raw) > 4096:
        return None
    try:
        argv = json.loads(raw)
    except ValueError:
        return None
    if not (isinstance(argv, list) and len(argv) == 5 and all(isinstance(item, str) for item in argv)
            and argv[:4] == SUMMON):
        return None
    try:
        payload = json.loads(argv[4])
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("view") != "devices":
        return None
    request = payload.get("request_id")
    if set(payload) == {"view", "request_id"} and isinstance(request, str) and REQUEST_ID.fullmatch(request):
        return {"kind": "pairing_request", "request_id": request}
    if set(payload) == {"view"}:
        summary, body = document.get("summary"), document.get("body")
        if isinstance(summary, str) and summary.endswith(SSH_TITLE_END) and isinstance(body, str):
            device = body.split(" \u00b7 ", 1)[0]
            if DEVICE_ID.fullmatch(device):
                return {"kind": "ssh_request", "device_id": device}
    return None


def still_waiting(marker: dict, pending: dict) -> bool:
    if marker["kind"] == "pairing_request":
        return marker["request_id"] in pending.get("pair", ())
    return marker["device_id"] in pending.get("ssh", ())


def project(identifier: str, document: dict, *, active: bool) -> dict:
    """The wire row. `execArgv` is read only to answer "is there an action"
    and, for Omodachi's own requests, which kind of request it is (NOTIFY-3)."""
    urgency = document.get("urgency")
    timestamp = document.get("timestamp")
    row = {"id": identifier,
           "app": _text(document.get("app"), 200) or "unknown",
           "summary": _text(document.get("summary")),
           "body": _text(document.get("body")),
           "glyph": _text(document.get("glyph"), 64),
           "urgency": URGENCIES.get(urgency if type(urgency) is int else 1, "normal"),
           "timestamp": timestamp if type(timestamp) is int else 0,
           "has_action": bool(document.get("execArgv")),
           "active": active}
    marker = request_marker(document)
    if marker is not None:
        row["kind"] = marker["kind"]
    return row


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


def journal_line(entry):
    """One JSON line on stdout, which systemd keeps (the `_ssh_journal` shape)."""
    try:
        print(json.dumps({"omodachi": "notifications", **entry}, separators=(",", ":"), default=str), flush=True)
    except (OSError, ValueError):
        pass


class NotificationMirror:
    """Watches the two directories and publishes what appears in them."""

    def __init__(self, hub=None, *, state_dir=None, runner=None, poll_seconds=POLL_SECONDS,
                 clock=time.monotonic, pending_requests=None, journal=None):
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
        # LITE-1b. How the watch thread hands an event to the hub: None calls
        # it right here (tests, the IPC-only daemon); the HTTPS daemon sets it
        # to its loop's `call_soon_threadsafe`, so the hub - whose subscriber
        # queues belong to that loop - is only ever touched on it.
        self.dispatch = None
        # NOTIFY-3. `pending_requests()` answers which of Omodachi's own
        # requests are still waiting: `{"pair": {request_id}, "ssh":
        # {device_id}}`, or None when core cannot tell - and then nothing is
        # withdrawn, because a request nobody can see is not a decided one.
        self.pending_requests = pending_requests
        # Where a withdrawal is recorded: the daemon passes `journal_line`.
        self.journal = journal or (lambda entry: None)
        # Ours, decided, and not yet gone from the shell's files: identifier ->
        # {"summary", "dismissed_at"}. Rows here never reach a client again.
        self._withdrawn: dict[str, dict] = {}

    def _emit(self, function, *arguments):
        if self.hub is None:
            return
        dispatch = self.dispatch
        if dispatch is None:
            function(*arguments)
            return
        try:
            dispatch(function, *arguments)
        except RuntimeError:
            pass  # the loop is closing; the next scan sees the same files

    def _publish(self, event_type, payload):
        self.hub.publish(event_type, payload)

    # --- reading -------------------------------------------------------------
    def _read_file(self, path: Path):
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return None
            document = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    def _pending(self):
        source = self.pending_requests
        if source is None:
            return None
        try:
            value = source()
        except Exception:
            return None
        return value if isinstance(value, dict) else None

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
                found.append((project(identifier, document, active=active), request_marker(document)))
        # NOTIFY-3: which of our own requests have been decided (approved,
        # rejected, expired, replaced by a retry, the device revoked - or the
        # daemon restarted and the request is simply not there any more). Only
        # asked when one of ours is on disk at all.
        stale = set()
        if any(marker is not None for _, marker in found):
            pending = self._pending()
            if pending is not None:
                stale = {row["id"] for row, marker in found
                         if marker is not None and not still_waiting(marker, pending)}
        fresh, ended, withdrawn = [], [], []
        with self._lock:
            on_disk = {row["id"] for row, _ in found}
            for identifier in [key for key in self._withdrawn if key not in on_disk]:
                del self._withdrawn[identifier]  # the shell's files no longer hold it
            for row, _ in found:
                identifier = row["id"]
                if identifier in stale or identifier in self._withdrawn:
                    if identifier not in self._withdrawn:
                        self._withdrawn[identifier] = {"summary": row["summary"], "dismissed_at": None}
                        while len(self._withdrawn) > WITHDRAWN_LIMIT:
                            self._withdrawn.pop(next(iter(self._withdrawn)))
                    previous = self._rows.pop(identifier, None)
                    if previous is not None:
                        self._order.remove(identifier)
                        withdrawn.append({**previous, "active": False, "withdrawn": True})
                    continue
                previous = self._rows.get(identifier)
                if previous is None:
                    self._rows[identifier] = row
                    self._order.append(identifier)
                    fresh.append(row)
                elif previous["active"] and not row["active"]:
                    previous["active"] = False
                    ended.append(dict(previous))
            # An active file that vanished between passes has been dismissed.
            live = {row["id"] for row, _ in found if row["active"]}
            for identifier in self._order:
                stored = self._rows[identifier]
                if stored["active"] and identifier not in live:
                    stored["active"] = False
                    ended.append(dict(stored))
            while len(self._order) > HISTORY_LIMIT:
                self._rows.pop(self._order.pop(0), None)
        for row in fresh:
            self._emit(self._publish, "notification.posted", dict(row))
        # LITE-1b (NEXT-1 §6.1): a toast that left the screen - dismissed on
        # the desktop, by a client, or timed out into history - says so, so
        # every client's list moves with it. `notification.updated`, not a
        # second `notification.posted`: the App toasts on `posted`.
        for row in ended:
            self._emit(self._publish, "notification.updated", row)
        # NOTIFY-3: a decided request leaves every list - not "history", gone.
        for row in withdrawn:
            self.journal({"event": "notification.withdrawn", "id": row["id"], "kind": row.get("kind")})
            self._emit(self._publish, "notification.updated", row)
        self.refresh_dnd()
        return fresh

    # --- NOTIFY-3: withdrawing what was decided ------------------------------
    def withdraw_stale(self) -> list[tuple[str, str]]:
        """Take our decided requests off the desktop and out of the shell's files.

        Runs on the watch thread after each pass, never on a request path. For
        every withdrawn identifier still on disk, the file is read again and
        must still be ours (`request_marker`) before anything is done to it:

        - **On screen:** the shell's own `dismiss <title>` - the only way to take
          a toast off the desktop from outside. It matches titles by substring,
          so it is only sent when no other toast on screen contains the title.
          When the other one is *ours and still waiting* (the same device asked
          again: same title), this one waits and goes with it. When it is
          anybody else's, the shell is not asked at all.
        - **Fallback - remove our own file:** when the shell is not answering,
          says it has no such toast, has not moved the file within
          `WITHDRAW_GRACE_SECONDS`, or a stranger shares the title. Safe for
          the shell: the file is only its restart backstop, a later archive of
          it is `mv ... || exit 0`, and nothing else reads it. Not complete: a
          toast that is still drawn stays drawn until it is clicked or the
          shell restarts (it is gone from every client and is not restored).
        - **In history:** our file is removed. A replayed history would
          otherwise show the request again - as a critical toast that never
          expires.
        """
        with self._lock:
            targets = {key: dict(value) for key, value in self._withdrawn.items()}
        if not targets:
            return []
        documents, others = {}, []
        for directory, active in ((self.state_dir, True), (self.history_dir, False)):
            try:
                names = sorted(os.listdir(directory))
            except OSError:
                continue
            for name in names:
                if FILE_NAME.fullmatch(name) is None:
                    continue
                identifier = name[:-5]
                if identifier not in targets and not active:
                    continue
                document = self._read_file(directory / name)
                if document is None:
                    continue
                marker = request_marker(document)
                if identifier in targets:
                    if marker is not None:
                        documents[identifier] = (directory / name, document, active)
                else:
                    others.append((_text(document.get("summary")), marker))
        actions, dismissed = [], []
        titles: dict[str, list[str]] = {}
        for identifier, (path, document, active) in documents.items():
            if active:
                titles.setdefault(_text(document.get("summary")), []).append(identifier)
            elif self._remove(path):
                actions.append((identifier, "history_removed"))
        now = self.clock()
        for title, identifiers in titles.items():
            sharing = [marker for text, marker in others if title and title in text]
            if sharing and all(marker is not None for marker in sharing):
                actions.extend((identifier, "deferred") for identifier in identifiers)
                continue
            if title and not sharing:
                asked = [targets[identifier]["dismissed_at"] for identifier in identifiers]
                if any(at is None for at in asked):
                    try:
                        result = _shell(("dismiss", title), runner=self.runner)
                    except NotificationsUnavailable:
                        result = "unavailable"
                    if result == "ok":
                        dismissed.extend(identifiers)
                        actions.extend((identifier, "dismissed") for identifier in identifiers)
                        continue
                elif now - max(at for at in asked if at is not None) < WITHDRAW_GRACE_SECONDS:
                    continue
            for identifier in identifiers:
                if self._remove(documents[identifier][0]):
                    actions.append((identifier, "file_removed"))
        if dismissed:
            with self._lock:
                for identifier in dismissed:
                    if identifier in self._withdrawn:
                        self._withdrawn[identifier]["dismissed_at"] = now
        for identifier, how in actions:
            if how != "deferred":
                self.journal({"event": "notification.withdraw", "id": identifier, "how": how})
        return actions

    @staticmethod
    def _remove(path: Path) -> bool:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

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
        if self.dispatch is not None:
            self._emit(self._publish_dnd, value)
            return
        self._publish_dnd(value)

    def _publish_dnd(self, value) -> None:
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
            ended = None
            with self._lock:
                stored = self._rows.get(identifier)
                if stored is not None and stored["active"]:
                    stored["active"] = False
                    ended = dict(stored)
            if ended is not None:
                self._emit(self._publish, "notification.updated", ended)
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
                try:
                    self.withdraw_stale()
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
