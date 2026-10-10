"""The official Herdr bridge: the host's sessions, read through official calls.

Herdr's own answer to "where is the mobile app" is `terminal session
observe/control` — a documented third-party bridge that speaks NDJSON on stdout
and, for `control`, reads four commands on stdin. Omodachi draws its own grid
from `api snapshot` and puts exactly those streams on the WebSockets the device
already authenticated, adding no framing of its own.

`omodachi` is the session core owns (`omodachi-herdr.service`) and the one every
device starts on. HERDR-2 adds the rest of the user's own sessions: `herdr
session list --json` enumerates them, a device picks one, and from then on that
device's layout, observe and control are that session's. A name is only ever
accepted because it came back from that listing, so a request can never name a
socket Herdr did not publish. The Herdr socket is still never exposed to the
LAN: it has no authentication of its own, so the authenticated WSS and the
same-user socket are the only ways in.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import stat
import tempfile
import threading
import time
from typing import Any
import unicodedata

SESSION = "omodachi"
BINARY = "herdr"
# Herdr's own home. Every session socket the listing reports lives under it, and
# a path that does not is refused rather than connected to.
HERDR_HOME = Path.home() / ".config/herdr"
SOCKET_PATH = HERDR_HOME / "sessions" / SESSION / "herdr.sock"
# `herdr session list --json` prints names the user chose with `--session`.
# Anything outside this alphabet never becomes an argv element here.
SESSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
PANE_ID = re.compile(r"w[0-9]{1,9}:p[0-9]{1,9}")
WORKSPACE_ID = re.compile(r"w[0-9]{1,9}")
TAB_ID = re.compile(r"w[0-9]{1,9}:t[0-9]{1,9}")
SPLIT_DIRECTIONS = ("right", "down")
FOCUS_DIRECTIONS = ("left", "right", "up", "down")
ZOOM_MODES = ("on", "off", "toggle")
# A tab label is a display string Herdr prints in its own tab bar. It is passed
# as one argv element, never through a shell, and control characters are out.
TAB_LABEL = re.compile(r"[^\x00-\x1f\x7f]{1,64}\Z")
# The four commands `control` reads on stdin. Verified against herdr 0.8.2:
# `terminal.input` takes text or bytes but not both, resize takes positive
# cols/rows, scroll takes positive lines. `observe` reads no stdin at all, so a
# resize there is a restart of the stream.
CONTROL_COMMANDS = ("terminal.input", "terminal.resize", "terminal.scroll", "terminal.release")
MAX_COLS, MAX_ROWS = 1000, 1000
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024

# HERDR-3. The socket methods this bridge may call, and nothing else. The three
# write/read methods are one-shot RPCs on the session socket: they take no
# controller and never resize the PTY, which is why the phone types through
# them by default and keeps `control` for the rare full-screen program.
SOCKET_METHODS = ("pane.focus", "pane.send_keys", "pane.send_text", "pane.read",
                  # HERDR-11: which processes run in a pane, to find the
                  # agent's own log (`herdr_reply.py`). Read-only.
                  "pane.process_info")
# `pane.read` is called with `source: "visible"` only. `recent` asks Herdr for
# more than the screen holds, and for an idle agent Herdr gets those rows by
# scrolling the desktop pane up and back down (Collie HERDR_API.md); `visible`
# is the rendered viewport and has nothing above it to collect.
READ_SOURCE = "visible"

# `pane.send_keys` grammar, probed on herdr 0.8.2 / protocol 20 (2026-10-03,
# HERDR-3 report §1): special names are case-insensitive, a one-character
# string is typed as that character, and `+` joins modifiers to a base. The
# allowlist is ours and narrower than Herdr's: `cmd+`/`super+` are accepted by
# Herdr and silently dropped to the bare key, and 0.8.2 also takes tmux's `C-c`
# - neither is a key a person meant, so neither is passed.
SPECIAL_KEYS = {name.lower(): name for name in (
    "Escape", "Tab", "Enter", "Space", "Backspace", "Up", "Down", "Left", "Right",
    *(f"F{number}" for number in range(1, 13)))}
SPECIAL_KEYS["esc"] = "Escape"
# Herdr answers these with `invalid_key` on every version probed. They are
# refused here with a code of their own, so a client can say which key and why
# instead of "Herdr refused".
UNSUPPORTED_KEYS = ("PageUp", "PageDown", "Home", "End", "Insert", "Delete")
KEY_MODIFIERS = ("ctrl", "alt", "shift")
MAX_KEYS = 32
# One `send_text`, never chunked: the RPC carries no bracketed paste, so a chunk
# boundary on a `\n` would submit half a message (Collie ADR 0010). Herdr's own
# request line tops out at 1 MiB; 16 KiB of UTF-8 is a long message and well
# inside it, and inside this server's 64 KiB body limit after JSON escaping.
MAX_TEXT_BYTES = 16 * 1024
# The submit proof: read the visible screen up to eight times, 350 ms apart,
# and press Enter only once the end of what was sent is on it (Collie
# `reply-action.ts`, POLL_ATTEMPTS / POLL_DELAY_MS).
PROOF_ATTEMPTS = 8
PROOF_DELAY_SECONDS = 0.35
PROOF_TAIL = 32
PROOF_FLOOR = 8
# HERDR-4: a short reply's line proof holds only on a quiet screen.
SHORT_PROOF_MAX_FRESH = 2
# Claude Code collapses a long paste into this token instead of echoing it, so
# the token appearing is the evidence for a long send.
PASTE_PLACEHOLDER = re.compile(r"\[Pasted text #(\d+)(?: \+(\d+) lines?)?\]")
_BOX_DRAWING = re.compile(r"[\u2500-\u257f]")


class HerdrUnavailable(ValueError):
    """The owned session did not answer; the boundary reports 503 or 409."""


def _run_document(argv: tuple[str, ...], *, timeout_seconds: float = 5.0) -> dict:
    """One official CLI call, parsed. The document is Herdr's, not ours."""
    from .agent import ReadOnlyAgentProbe
    value = ReadOnlyAgentProbe._run_process(argv, timeout_seconds=timeout_seconds,
                                            max_bytes=MAX_SNAPSHOT_BYTES)
    if value.returncode or value.error:
        raise HerdrUnavailable("herdr_unavailable")
    try:
        document = json.loads(value.stdout)
    except ValueError:
        raise HerdrUnavailable("herdr_unavailable") from None
    if not isinstance(document, dict) or document.get("error"):
        raise HerdrUnavailable("herdr_request_failed")
    return document


def _run(argv: tuple[str, ...], *, timeout_seconds: float = 5.0) -> dict:
    """The `{"id": …, "result": …}` envelope the socket-API commands print.

    `herdr session list --json` is the one official call that does *not* use it,
    so it reads the document itself.
    """
    result = _run_document(argv, timeout_seconds=timeout_seconds).get("result")
    return result if isinstance(result, dict) else {}


def session_name(value: Any) -> str:
    if not isinstance(value, str) or not SESSION_NAME.fullmatch(value):
        raise HerdrUnavailable("invalid_session")
    return value


class HerdrSessions:
    """`herdr session list --json` — 0.8.2's only enumeration of sessions.

    Verbatim, on herdr 0.8.2::

        $ herdr session list --json
        {"sessions":[{"default":true,"name":"default","running":true,
          "session_dir":"/home/alex/.config/herdr",
          "socket_path":"/home/alex/.config/herdr/herdr.sock"}, …]}

    Note that the default session's socket is *not* under `sessions/<name>/`, so
    a socket path is taken from this listing rather than constructed. There is
    no runtime directory involved: `ls $XDG_RUNTIME_DIR | grep -i herdr` is
    empty on this host.
    """

    def __init__(self, *, runner=None, owned: str = SESSION, home: Path | None = None) -> None:
        self.runner = runner or _run_document
        self.owned = owned
        # The directory a reported socket must live inside. It is a parameter so
        # a recorded listing from the real host can be replayed off it.
        self.home = Path(home) if home is not None else HERDR_HOME

    def rows(self) -> list[dict]:
        document = self.runner((BINARY, "session", "list", "--json"))
        rows = document.get("sessions")
        if not isinstance(rows, list):
            raise HerdrUnavailable("herdr_unavailable")
        listing = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                continue
            if not SESSION_NAME.fullmatch(row["name"]):
                continue
            listing.append({"name": row["name"], "running": bool(row.get("running")),
                            "herdr_default": bool(row.get("default")),
                            "owned": row["name"] == self.owned,
                            "socket_path": self._socket(row.get("socket_path"))})
        # The owned session first, then the rest by name: a list a person reads
        # top down should start where every device starts.
        listing.sort(key=lambda row: (not row["owned"], row["name"]))
        return listing

    def _socket(self, value: Any) -> str | None:
        """Herdr's own path for this session's socket, refused if it is not one.

        The listing is trusted output from the same-user binary, but a socket
        path is what this bridge later connects to, so it must stay inside
        Herdr's own home rather than be followed anywhere the string says.
        """
        if not isinstance(value, str) or not value:
            return None
        path = Path(value)
        try:
            path.relative_to(self.home)
        except ValueError:
            return None
        return str(path)

    def names(self) -> set[str]:
        return {row["name"] for row in self.rows()}


class HerdrSessionChoices:
    """Which Herdr session each device is looking at.

    A choice is per device, because two people holding two iPads are looking at
    two different things; `default` is the host-wide fallback an operator can
    set from the CLI, and the owned session is the fallback for that. Nothing
    here is a credential, so the file is small, 0600 and rewritten atomically.
    """

    def __init__(self, path=None, *, owned: str = SESSION) -> None:
        self.path = Path(path) if path is not None else None
        self.owned = owned
        self._mutex = threading.RLock()
        self._memory = {"version": 1, "default": None, "devices": {}}
        if self.path is not None and not self.path.is_absolute():
            raise HerdrUnavailable("invalid_request")

    @contextmanager
    def _locked(self):
        with self._mutex:
            if self.path is None:
                yield
                return
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock = self.path.with_name(self.path.name + ".lock")
            handle = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX)
                yield
            finally:
                os.close(handle)

    def _read(self) -> dict:
        if self.path is None or not self.path.exists():
            return self._memory
        try:
            document = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return self._memory
        if not isinstance(document, dict):
            return self._memory
        devices = document.get("devices")
        chosen = document.get("default")
        self._memory = {"version": 1,
                        "default": chosen if isinstance(chosen, str) and SESSION_NAME.fullmatch(chosen) else None,
                        "devices": {key: value for key, value in (devices or {}).items()
                                    if isinstance(key, str) and isinstance(value, str)
                                    and SESSION_NAME.fullmatch(value)} if isinstance(devices, dict) else {}}
        return self._memory

    def _write(self, state: dict) -> None:
        self._memory = state
        if self.path is None:
            return
        handle, temporary = tempfile.mkstemp(dir=str(self.path.parent), prefix=".herdr-sessions")
        try:
            os.fchmod(handle, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(state, stream)
            os.replace(temporary, self.path)
        except OSError:
            with open(os.devnull):  # pragma: no cover - the replace is what matters
                pass
            Path(temporary).unlink(missing_ok=True)

    def selected(self, device: str | None = None) -> str:
        """The session this device is on: its own choice, the host's, or ours."""
        with self._locked():
            state = self._read()
            if device and isinstance(state["devices"].get(device), str):
                return state["devices"][device]
            return state["default"] or self.owned

    def choose(self, name: str, device: str | None = None) -> dict:
        name = session_name(name)
        with self._locked():
            state = dict(self._read())
            state["devices"] = dict(state["devices"])
            if device:
                state["devices"][device] = name
            else:
                state["default"] = name
            self._write(state)
        return {"selected": name, "device": device, "scope": "device" if device else "host"}

    def forget(self, device: str) -> None:
        with self._locked():
            state = dict(self._read())
            state["devices"] = {key: value for key, value in state["devices"].items() if key != device}
            self._write(state)

    def chosen(self) -> set[str]:
        """HERDR-11: every session some device (or the host default) is on."""
        with self._locked():
            state = self._read()
            return set(state["devices"].values()) | ({state["default"]} if state["default"] else set())


# HERDR-5. A name a person types for a new session. Herdr's own alphabet,
# narrowed: at most 32 characters and a letter or digit first, so `.`, `..`
# (which would make `~/.config/herdr/sessions/..` - the default session's own
# directory) and a leading `-` (a flag to Herdr's parser) can never be one.
NEW_SESSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")


def new_session_name(value: Any) -> str:
    if not isinstance(value, str) or not NEW_SESSION_NAME.fullmatch(value):
        raise HerdrUnavailable("invalid_session_name")
    return value


def _run_unit(argv: tuple[str, ...], *, timeout_seconds: float = 10.0):
    from .agent import ReadOnlyAgentProbe
    return ReadOnlyAgentProbe._run_process(argv, timeout_seconds=timeout_seconds, max_bytes=65536)


class HerdrSessionStarter:
    """HERDR-5: bring a session's Herdr server up with no window and no client.

    `herdr --session <name> server` is herdr 0.8.2's own headless server - the
    very command `omodachi-herdr.service` runs for the owned session. It is
    started as a transient **user unit** (`systemd-run --user`), not as a child
    of core and not through a short-lived client in a PTY: a server a client
    spawns stays in that client's cgroup (on the host, Leo's `new` server lives
    in the `foot` terminal's scope), so one spawned from core would sit in
    `omodachid.service`'s cgroup and be killed with it on every core restart or
    reinstall. A unit of its own outlives core, needs no client to stay up, and
    ends when the session is stopped (`herdr session stop`); `--collect` lets
    systemd forget it then.

    Herdr restores a stopped session's workspaces from its own `session.json`
    when the server starts; a session with none (a new one) gets one workspace
    in `$HOME`, which is what a desktop client would have made on first attach.

    The owned session is started the way it always has been, through its own
    service. Every argv here is fixed but for the session name, which has
    passed `SESSION_NAME` (start) or `NEW_SESSION_NAME` (create) first.
    """

    UNIT_PREFIX = "omodachi-herdr-session-"
    OWNED_UNIT = "omodachi-herdr.service"

    def __init__(self, *, runner=None, owned: str = SESSION, home: Path | None = None,
                 sleep=time.sleep, clock=time.monotonic, wait_seconds: float = 10.0) -> None:
        self.runner = runner or _run_unit
        self.owned = owned
        self.home = Path(home) if home is not None else Path.home()
        self.sleep, self.clock, self.wait_seconds = sleep, clock, wait_seconds
        self._mutex = threading.Lock()

    def unit(self, name: str) -> str:
        return self.UNIT_PREFIX + session_name(name)

    def server_argv(self, name: str) -> tuple[str, ...]:
        name = session_name(name)
        if name == self.owned:
            return ("systemctl", "--user", "start", self.OWNED_UNIT)
        return ("systemd-run", "--user", "--unit=" + self.unit(name), "--collect",
                "--property=Type=exec", "--working-directory=" + str(self.home),
                "--description=Herdr session " + name + " (started from Omodachi)",
                BINARY, "--session", name, "server")

    def workspace_argv(self, name: str) -> tuple[str, ...]:
        return (BINARY, "--session", session_name(name), "workspace", "create",
                "--cwd", str(self.home), "--no-focus")

    def start(self, name: str, *, running, snapshot) -> bool:
        """Start `name` and wait until it answers. False when it already did.

        `running()` is whether `herdr session list` says the session runs,
        `snapshot()` is that session's `api snapshot` (raising
        `HerdrUnavailable` until it answers). One start at a time: a second
        phone pressing Start on the same session waits here and then finds it
        running.
        """
        with self._mutex:
            if running():
                return False
            result = self.runner(self.server_argv(name))
            # A unit that failed to start may still be a race lost to another
            # starter (herdr: "server is already running"); the listing decides.
            deadline = self.clock() + self.wait_seconds
            state = None
            while True:
                up = running()
                if up:
                    try:
                        state = snapshot()
                        break
                    except HerdrUnavailable:
                        pass
                failed = bool(result.returncode or result.error) and not up
                if failed or self.clock() >= deadline:
                    raise HerdrUnavailable("herdr_session_start_failed")
                self.sleep(0.25)
            workspaces = state.get("workspaces") if isinstance(state, dict) else None
            if not workspaces:
                made = self.runner(self.workspace_argv(name))
                if made.returncode or made.error:
                    raise HerdrUnavailable("herdr_session_start_failed")
            return True


# HERDR-6. A workspace's name is a label Herdr prints in its own sidebar, the
# same alphabet as a tab's. None (or blank) lets Herdr name it after its
# folder, as it does on the desktop.
def workspace_label(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HerdrUnavailable("invalid_workspace_name")
    value = value.strip()
    if not value:
        return None
    if not TAB_LABEL.fullmatch(value):
        raise HerdrUnavailable("invalid_workspace_name")
    return value


class HerdrFolders:
    """HERDR-6: the folders a new workspace is offered in.

    - **recent**: the folders panes are in now, across the running sessions,
      newest first: a folder counts from when this daemon first saw a pane in
      it (the two-second poll and every read here both look), so one a pane
      just moved to comes first. Herdr 0.8.2's snapshot has no times of its
      own; where two were first seen together, the later pane in Herdr's own
      order (its ids grow) comes first. At most eight, never the home folder
      itself (it is the first common one), and only what `host_paths` allows.
      POLISH-U: merged with core's **own** short list of the folders new
      workspaces were made in (`herdr-folders.json`, beside
      `herdr-sessions.json`: at most `MAX_USED`, newest first, 0600, written
      atomically), so a folder used yesterday is still offered after its pane
      closed, the session stopped or core restarted. A remembered folder is
      offered only while `host_paths` still allows it (it exists, it is a
      folder in the home folder, it is not private); one that does not is
      dropped from the file the next time it is written.
    - **common**: `~`, `~/Work`, `~/Projects`, `~/code`, `~/src`, `~/dev`,
      the ones this host has.
    - **browse**: the folders directly inside one folder (one level), names
      that start with `.` only when asked, never a private place, at most 100.
    """

    COMMON = ("", "Work", "Projects", "code", "src", "dev")
    MAX_RECENT = 8
    # HERDR-11: the folders a person starred, kept in the same file.
    MAX_FAVOURITES = 12
    MAX_ROWS = 100
    # How many folders the "first seen" memory keeps.
    MEMORY = 256
    # POLISH-U: how many folders new workspaces were made in are remembered.
    MAX_USED = 16
    VERSION = 1

    def __init__(self, home=None, *, clock=time.time, path=None) -> None:
        from .host_paths import home_folder
        self.home = home_folder(home)
        # Wall-clock seconds: the remembered folders' times outlive this
        # process, and the live ones are ordered against them.
        self.clock = clock
        self.path = Path(path) if path is not None else None
        if self.path is not None and not self.path.is_absolute():
            raise ValueError("herdr folders path must be absolute")
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}
        self._present: dict[str, set[str]] = {}
        self._used: list[dict] | None = None
        self._favourites: list[dict] | None = None

    # --- POLISH-U: the folders new workspaces were made in, kept -------------
    def _load_used(self) -> list[dict]:
        """The remembered folders, newest first (read once, then kept)."""
        if self._used is not None:
            return self._used
        rows: list[dict] = []
        document = None
        if self.path is not None:
            try:
                document = json.loads(self.path.read_text("utf-8"))
            except (OSError, ValueError):
                document = None
            entries = document.get("folders") if isinstance(document, dict) else None
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict):
                    continue
                folder, at = entry.get("path"), entry.get("at")
                if (isinstance(folder, str) and folder.startswith("/") and len(folder) <= 4096
                        and "\x00" not in folder and type(at) in (int, float)
                        and all(row["path"] != folder for row in rows)):
                    rows.append({"path": folder, "at": float(at)})
        rows.sort(key=lambda row: -row["at"])
        self._used = rows[:self.MAX_USED]
        # HERDR-11: the starred folders, in the order they were starred.
        starred: list[dict] = []
        for entry in (document.get("favourites") if isinstance(document, dict) else None) or []:
            if not isinstance(entry, dict):
                continue
            folder, at = entry.get("path"), entry.get("at")
            if (isinstance(folder, str) and folder.startswith("/") and len(folder) <= 4096
                    and "\x00" not in folder and type(at) in (int, float)
                    and all(row["path"] != folder for row in starred)):
                starred.append({"path": folder, "at": float(at)})
        self._favourites = starred[:self.MAX_FAVOURITES]
        return self._used

    def _write_used(self) -> None:
        if self.path is None:
            return
        body = {"version": self.VERSION, "folders": self._used or []}
        if self._favourites:
            # HERDR-11; left out when there are none, so the file a host had
            # before stays byte for byte what it was.
            body["favourites"] = self._favourites
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=str(self.path.parent), prefix=".herdr-folders")
        except OSError:
            return
        try:
            os.fchmod(handle, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(body, stream)
            os.replace(temporary, self.path)
        except OSError:
            # A folder list that cannot be saved costs a suggestion, never the
            # workspace that was just made.
            Path(temporary).unlink(missing_ok=True)

    def _used_rows(self) -> list[tuple[Path, float]]:
        """The remembered folders `host_paths` still allows, newest first."""
        from .host_paths import usable
        rows = []
        with self._lock:
            used = list(self._load_used())
        for entry in used:
            path = usable(entry["path"], self.home)
            if path is not None and path != self.home:
                rows.append((path, entry["at"]))
        return rows

    def row(self, path: Path) -> dict:
        from .host_paths import display
        return {"path": str(path), "display": display(path, self.home),
                "name": "~" if path == self.home else path.name}

    def _resolve(self, cwds) -> list[Path]:
        from .host_paths import usable
        seen, rows = set(), []
        for cwd in cwds:
            if not isinstance(cwd, str) or not cwd:
                continue
            path = usable(cwd, self.home)
            if path is None or str(path) in seen:
                continue
            seen.add(str(path))
            rows.append(path)
        return rows

    def note(self, session: str, cwds) -> list[Path]:
        """One session's pane folders as they are now; a folder no running
        session had a moment ago counts as new from now."""
        paths = self._resolve(cwds)
        now = self.clock()
        with self._lock:
            before = set().union(*self._present.values()) if self._present else set()
            self._present[session] = {str(path) for path in paths}
            for path in paths:
                if str(path) not in before or str(path) not in self._seen:
                    self._seen[str(path)] = now
            if len(self._seen) > self.MEMORY:
                for key, _ in sorted(self._seen.items(), key=lambda item: item[1])[:len(self._seen) - self.MEMORY]:
                    del self._seen[key]
        return paths

    def touch(self, path: Path) -> None:
        """A folder a workspace was just made in is the newest there is, and
        (POLISH-U) it is remembered: the file keeps the newest `MAX_USED`,
        and anything `host_paths` no longer allows is left out of it."""
        from .host_paths import usable
        now = self.clock()
        with self._lock:
            self._seen[str(path)] = now
            # The home folder is always offered (the first common one), so it
            # is not what this list is for.
            kept = [entry for entry in self._load_used() if entry["path"] != str(path)
                    and usable(entry["path"], self.home) not in (None, self.home)]
            fresh = [] if Path(path) == self.home else [{"path": str(path), "at": now}]
            self._used = (fresh + kept)[:self.MAX_USED]
            self._write_used()

    def forget(self, session: str) -> None:
        with self._lock:
            self._present.pop(session, None)

    # --- HERDR-11: favourites -------------------------------------------------
    def favourites(self) -> list[dict]:
        """The starred folders `host_paths` still allows, as rows. The home
        folder may be one: it is a folder a person can star like any other."""
        from .host_paths import usable
        with self._lock:
            self._load_used()
            starred = list(self._favourites or [])
        rows = []
        for entry in starred:
            path = usable(entry["path"], self.home)
            if path is not None and all(row["path"] != str(path) for row in rows):
                rows.append(self.row(path))
        return rows

    def favourite(self, path: Path, on: bool) -> None:
        """Star or un-star one folder (already resolved by `host_paths`). A
        folder starred again moves to the end; past `MAX_FAVOURITES` the
        oldest star goes."""
        from .host_paths import usable
        now = self.clock()
        with self._lock:
            self._load_used()
            kept = [entry for entry in (self._favourites or []) if entry["path"] != str(path)
                    and usable(entry["path"], self.home) is not None]
            if on:
                kept.append({"path": str(path), "at": now})
            self._favourites = kept[-self.MAX_FAVOURITES:]
            self._write_used()

    def recent(self, paths) -> list[dict]:
        """The live folders (`paths`, as `note` returned them) and the
        remembered ones, newest first by whichever time is later."""
        unique = []
        for path in paths:
            if path != self.home and path not in unique:
                unique.append(path)
        with self._lock:
            seen = dict(self._seen)
        for path, at in self._used_rows():
            seen[str(path)] = max(seen.get(str(path), 0.0), at)
            if path not in unique:
                unique.append(path)
        ordered = sorted(enumerate(unique), key=lambda item: (-seen.get(str(item[1]), 0.0), item[0]))
        return [self.row(path) for _, path in ordered[:self.MAX_RECENT]]

    def common(self) -> list[dict]:
        from .host_paths import usable
        rows = []
        for name in self.COMMON:
            path = usable(str(self.home / name) if name else "~", self.home)
            if path is not None and all(row["path"] != str(path) for row in rows):
                rows.append(self.row(path))
        return rows

    def browse(self, under: Any, *, hidden: bool = False) -> dict:
        """One level of folders inside `under`; `FolderRefused` says why not."""
        from .host_paths import FolderRefused, folder, usable
        base = folder(under, home=self.home)
        names = []
        try:
            with os.scandir(base) as entries:
                for entry in entries:
                    if entry.name.startswith(".") and not hidden:
                        continue
                    try:
                        if not entry.is_dir():
                            continue
                    except OSError:
                        continue
                    names.append(entry.name)
        except OSError:
            raise FolderRefused("folder_unreadable") from None
        rows = []
        truncated = False
        for name in sorted(names, key=lambda value: (value.casefold(), value)):
            # A link that leads out of the home folder, or into a private
            # place, is not offered: picking it would only be refused.
            path = usable(str(base / name), self.home)
            if path is None:
                continue
            if len(rows) == self.MAX_ROWS:
                truncated = True
                break
            row = self.row(base / name)
            row["path"] = str(path)
            rows.append(row)
        parent = base.parent if base != self.home else None
        return {**self.row(base), "parent": self.row(parent) if parent is not None else None,
                "hidden": hidden, "dirs": rows, "truncated": truncated}


def pane_identifier(value: Any) -> str:
    if not isinstance(value, str) or not PANE_ID.fullmatch(value):
        raise HerdrUnavailable("invalid_pane")
    return value


def workspace_identifier(value: Any) -> str:
    if not isinstance(value, str) or not WORKSPACE_ID.fullmatch(value):
        raise HerdrUnavailable("invalid_workspace")
    return value


def key_name(value: Any) -> str:
    """One key in Herdr's own spelling, or a refusal that names the reason.

    Returns the canonical spelling (`Escape`, `ctrl+c`, `shift+Tab`), so what
    reaches Herdr and what the audit counts are the same thing.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 24:
        raise HerdrUnavailable("invalid_key")
    if len(value) == 1:
        return _printable(value)
    *modifiers, base = value.split("+")
    seen = []
    for modifier in modifiers:
        modifier = modifier.lower()
        if modifier not in KEY_MODIFIERS or modifier in seen:
            raise HerdrUnavailable("invalid_key")
        seen.append(modifier)
    if len(base) == 1:
        base = _printable(base)
    elif base.lower() in {name.lower() for name in UNSUPPORTED_KEYS}:
        raise HerdrUnavailable("herdr_key_unsupported")
    elif base.lower() in SPECIAL_KEYS:
        base = SPECIAL_KEYS[base.lower()]
    else:
        raise HerdrUnavailable("invalid_key")
    ordered = [modifier for modifier in KEY_MODIFIERS if modifier in seen]
    return "+".join([*ordered, base])


def _printable(character: str) -> str:
    # A letter, a digit, punctuation or a symbol. Control characters and
    # whitespace have names of their own (`Enter`, `Tab`, `Space`).
    if unicodedata.category(character)[0] not in "LNPS":
        raise HerdrUnavailable("invalid_key")
    return character


def key_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise HerdrUnavailable("invalid_key")
    if len(value) > MAX_KEYS:
        raise HerdrUnavailable("herdr_too_many_keys")
    return [key_name(item) for item in value]


def pane_text(value: Any) -> str:
    """What `send_text` may type: words, `\n` and `\t`, and no escape codes.

    `\r` is refused because it is the submit key in most TUIs, and the submit
    is this bridge's decision after the proof, not something a client smuggles
    inside the text. Any other control character could be an escape sequence,
    which would be a key the keys allowlist never saw.
    """
    if not isinstance(value, str) or not value.strip():
        raise HerdrUnavailable("invalid_text")
    if len(value.encode("utf-8", "surrogatepass")) > MAX_TEXT_BYTES:
        raise HerdrUnavailable("herdr_text_too_long")
    for character in value:
        code = ord(character)
        if (code < 0x20 and character not in "\n\t") or code == 0x7f or 0xd800 <= code <= 0xdfff:
            raise HerdrUnavailable("invalid_text")
    return value


def _squash(text: str) -> str:
    """The screen without the parts a terminal adds: wrapping, indentation
    and the lines a TUI draws round its input box."""
    return "".join(_BOX_DRAWING.sub("", text).split())


def _line_end(line: str) -> str:
    """One screen line as its input box would read: no frame, no padding."""
    return _BOX_DRAWING.sub("", line).strip()


def text_proof(sent: str, before: str, after: str) -> str | None:
    """How the visible screen shows that `sent` arrived, or None when it does not.

    - `tail`: the end of what was sent (its last 32 non-space characters) is on
      the screen more often than it was before the send, so a sentence that
      was already on screen proves nothing.
    - `paste`: a long paste Claude Code collapsed into `[Pasted text #N +M
      lines]` - a new token whose line count fits this text.
    - `line` (HERDR-4): a short reply - fewer than eight characters, `yes`,
      `y`, `/model` - is a word that may well turn up in new output anywhere
      on the screen, so counting it is no proof at all. It counts only when a
      line that is new on the screen (not one that only moved) now *ends*
      with it: the input line, `> ` becoming `> yes` (or a placeholder hint
      becoming `> yes`) - and only when at most two lines are new, since a
      busy screen proves nothing about a short word. That is still a weaker reading than the tail, and the
      reply says which one it was, so the app can say "Sent" without
      claiming more.
    """
    squashed = _squash(sent)
    if not squashed:
        return None
    if len(squashed) >= PROOF_FLOOR:
        tail = squashed[-PROOF_TAIL:]
        if _squash(after).count(tail) > _squash(before).count(tail):
            return "tail"
    else:
        last = _line_end([line for line in sent.split("\n") if line.strip()][-1]) or squashed
        old = before.split("\n")
        seen = {_line_end(line) for line in old}
        fresh = []
        for index, line in enumerate(after.split("\n")):
            now = _line_end(line)
            was = _line_end(old[index]) if index < len(old) else ""
            # New on the screen, not a line that only moved: a log that
            # scrolls by one carries its old `SUBMITTED: yes` to a new row.
            if now and now != was and now not in seen:
                fresh.append(now)
        # Typing a short reply changes the input line and little else; a
        # screen where more than a couple of lines are new is busy, and a
        # short word on it proves nothing.
        if len(fresh) <= SHORT_PROOF_MAX_FRESH and any(now.endswith(last) for now in fresh):
            return "line"
    newlines = sent.count("\n")
    earlier = PASTE_PLACEHOLDER.findall(before)
    for token in PASTE_PLACEHOLDER.findall(after):
        if token in earlier:
            earlier.remove(token)
            continue
        lines = token[1]
        if not lines or abs(int(lines) - newlines) <= 1:
            return "paste"
    return None


def text_landed(sent: str, before: str, after: str) -> bool:
    """Whether the visible screen now shows `sent` and did not a moment ago."""
    return text_proof(sent, before, after) is not None


def geometry(cols: Any, rows: Any) -> tuple[int, int]:
    if type(cols) is not int or type(rows) is not int:
        raise HerdrUnavailable("invalid_geometry")
    if not 1 <= cols <= MAX_COLS or not 1 <= rows <= MAX_ROWS:
        raise HerdrUnavailable("invalid_geometry")
    return cols, rows


def control_command(value: Any) -> dict:
    """Validate the envelope, then pass the client's command through as written.

    The four commands are Herdr's, not ours: their fields belong to Herdr and
    are forwarded unchanged. What is checked here is that the message is one of
    the four and is a flat JSON object, so this bridge can never be used to
    speak a different protocol at the session socket.
    """
    if not isinstance(value, dict) or value.get("type") not in CONTROL_COMMANDS:
        raise HerdrUnavailable("invalid_control_command")
    if len(value) > 8:
        raise HerdrUnavailable("invalid_control_command")
    for key, item in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z_.]{0,31}", key):
            raise HerdrUnavailable("invalid_control_command")
        if not isinstance(item, (str, int, float, bool)):
            raise HerdrUnavailable("invalid_control_command")
    if value["type"] == "terminal.resize":
        geometry(value.get("cols"), value.get("rows"))
    return value


class HerdrBridge:
    def __init__(self, session: str = SESSION, *, runner=None, socket_path: Path | None = None) -> None:
        # The session name becomes an argv element, so it is validated here and
        # not only where a client happened to supply it.
        self.session = session_name(session)
        self.runner = runner or _run
        # Herdr's default session keeps its socket at the root of its home
        # rather than under `sessions/<name>/`, so the real path comes from
        # `herdr session list`; the constructed one is only the fallback.
        self.socket_path = (Path(socket_path) if socket_path is not None
                            else SOCKET_PATH if session == SESSION
                            else HERDR_HOME / "sessions" / session / "herdr.sock")
        self._revision = 0
        self._signature: str | None = None
        self._lock = threading.Lock()
        # pane -> (channel, evict). `evict` ends that stream when another
        # device takes the pane over with `?takeover=1`.
        self._controllers: dict[str, tuple[str, Any]] = {}
        # One write at a time per pane, so two sends cannot interleave their
        # text and their proof reads.
        self._pane_locks: dict[str, threading.Lock] = {}
        self.proof_attempts = PROOF_ATTEMPTS
        self.proof_delay = PROOF_DELAY_SECONDS

    def argv(self, *arguments: str) -> tuple[str, ...]:
        return (BINARY, "--session", self.session, *arguments)

    # --- reading ------------------------------------------------------------
    def snapshot(self) -> dict:
        result = self.runner(self.argv("api", "snapshot"))
        value = result.get("snapshot")
        if not isinstance(value, dict):
            raise HerdrUnavailable("herdr_unavailable")
        return value

    def layout(self) -> dict:
        """One session snapshot projected as workspaces → tabs → panes."""
        snapshot = self.snapshot()
        agents = {}
        for agent in snapshot.get("agents") or []:
            if isinstance(agent, dict) and isinstance(agent.get("pane_id"), str):
                # NOTIFY-2, measured on the lab VM: 0.8.2's live snapshot names
                # an agent `agent` and its state `agent_status` (the recorded
                # fixture says name/kind/status); both shapes are read.
                agents[agent["pane_id"]] = {"name": agent.get("name") or agent.get("agent"),
                                            "kind": agent.get("kind") or agent.get("agent"),
                                            "status": agent.get("status") or agent.get("agent_status")}
        rectangles, zoomed_tabs = {}, {}
        for entry in snapshot.get("layouts") or []:
            if not isinstance(entry, dict):
                continue
            zoomed_tabs[entry.get("tab_id")] = bool(entry.get("zoomed"))
            for row in entry.get("panes") or []:
                if isinstance(row, dict) and isinstance(row.get("pane_id"), str):
                    rectangles[row["pane_id"]] = row.get("rect") or {}
        panes: dict[str, list[dict]] = {}
        for pane in snapshot.get("panes") or []:
            if not isinstance(pane, dict) or not isinstance(pane.get("pane_id"), str):
                continue
            rect = rectangles.get(pane["pane_id"], {})
            tab_id = pane.get("tab_id")
            # Herdr zooms a tab, and the zoomed pane is that tab's focused one;
            # a pane is only "zoomed" when both are true.
            zoomed = bool(zoomed_tabs.get(tab_id)) and bool(pane.get("focused"))
            panes.setdefault(tab_id, []).append({
                "id": pane["pane_id"], "tab_id": tab_id, "workspace_id": pane.get("workspace_id"),
                "title": pane.get("terminal_title_stripped") or pane.get("terminal_title"),
                "cwd": pane.get("cwd"), "focused": bool(pane.get("focused")), "zoomed": zoomed,
                "agent_status": pane.get("agent_status"), "agent": agents.get(pane["pane_id"]),
                "size": {"cols": rect.get("width"), "rows": rect.get("height")},
                "revision": pane.get("revision")})
        tabs: dict[str, list[dict]] = {}
        for tab in snapshot.get("tabs") or []:
            if not isinstance(tab, dict) or not isinstance(tab.get("tab_id"), str):
                continue
            tabs.setdefault(tab.get("workspace_id"), []).append({
                "id": tab["tab_id"], "label": tab.get("label"), "number": tab.get("number"),
                "focused": bool(tab.get("focused")), "zoomed": bool(zoomed_tabs.get(tab["tab_id"])),
                "agent_status": tab.get("agent_status"),
                "panes": sorted(panes.get(tab["tab_id"], []), key=lambda row: row["id"])})
        workspaces = []
        for workspace in snapshot.get("workspaces") or []:
            if not isinstance(workspace, dict) or not isinstance(workspace.get("workspace_id"), str):
                continue
            workspaces.append({
                "id": workspace["workspace_id"], "label": workspace.get("label"),
                "number": workspace.get("number"), "focused": bool(workspace.get("focused")),
                "active_tab_id": workspace.get("active_tab_id"),
                "agent_status": workspace.get("agent_status"),
                "tabs": sorted(tabs.get(workspace["workspace_id"], []), key=lambda row: row["id"])})
        payload = {"session": self.session, "protocol": snapshot.get("protocol"),
                   "version": snapshot.get("version"),
                   "focused": {"workspace_id": snapshot.get("focused_workspace_id"),
                               "tab_id": snapshot.get("focused_tab_id"),
                               "pane_id": snapshot.get("focused_pane_id")},
                   "workspaces": sorted(workspaces, key=lambda row: row["id"])}
        signature = json.dumps(payload, sort_keys=True, default=str)
        sessions = {}
        for row in snapshot.get("panes") or []:
            # HERDR-11: a Herdr that knows the agent's own session (protocol
            # 21's `agent_session`, `{source, agent, kind: id|path, value}`)
            # says so per pane; kept beside the projection, not in it.
            if isinstance(row, dict) and isinstance(row.get("pane_id"), str) \
                    and isinstance(row.get("agent_session"), dict):
                sessions[row["pane_id"]] = row["agent_session"]
        with self._lock:
            if signature != self._signature:
                self._signature = signature
                self._revision += 1
            revision = self._revision
            result = {**payload, "revision": revision}
            self._cached = (time.monotonic(), result)
            self._agent_sessions = sessions
        return result

    def cached_layout(self, max_age: float = 2.0) -> dict:
        """HERDR-11 §4: the last projection while it is at most `max_age`
        seconds old and still the newest revision; a fresh read otherwise.
        The poll publishes `herdr.layout.changed` after every read that moved,
        so a device asking for the layout because of that event gets exactly
        what was just read, without a second `api snapshot`."""
        with self._lock:
            cached = getattr(self, "_cached", None)
            if cached is not None and time.monotonic() - cached[0] <= max_age \
                    and cached[1].get("revision") == self._revision:
                return cached[1]
        return self.layout()

    def agent_session(self, pane: str):
        with self._lock:
            return (getattr(self, "_agent_sessions", None) or {}).get(pane)

    @property
    def revision(self) -> int:
        return self._revision

    # --- acting -------------------------------------------------------------
    def pane_action(self, pane: str, action: str, payload: dict) -> dict:
        pane = pane_identifier(pane)
        if not isinstance(payload, dict):
            raise HerdrUnavailable("invalid_request")
        if action == "split":
            direction = payload.get("direction", "right")
            if direction not in SPLIT_DIRECTIONS or set(payload) - {"direction", "ratio"}:
                raise HerdrUnavailable("invalid_request")
            argv = self.argv("pane", "split", "--pane", pane, "--direction", direction)
            ratio = payload.get("ratio")
            if ratio is not None:
                if type(ratio) not in (int, float) or not 0.05 <= float(ratio) <= 0.95:
                    raise HerdrUnavailable("invalid_request")
                argv += ("--ratio", format(float(ratio), ".4f"))
            return {"action": "split", "pane": pane, "result": self.runner(argv)}
        if action == "zoom":
            mode = payload.get("mode", "toggle")
            if mode not in ZOOM_MODES or set(payload) - {"mode"}:
                raise HerdrUnavailable("invalid_request")
            return {"action": "zoom", "pane": pane,
                    "result": self.runner(self.argv("pane", "zoom", "--pane", pane, "--" + mode))}
        if action == "close":
            if payload:
                raise HerdrUnavailable("invalid_request")
            return {"action": "close", "pane": pane,
                    "result": self.runner(self.argv("pane", "close", pane))}
        if action == "focus":
            direction = payload.get("direction")
            if set(payload) - {"direction"}:
                raise HerdrUnavailable("invalid_request")
            if direction is None:
                # herdr 0.8.2's CLI only focuses a *neighbour*; the absolute
                # focus a tap needs is the protocol's own `pane.focus`, which
                # the CLI does not surface. Same socket, same session, one
                # fixed method name.
                return {"action": "focus", "pane": pane, "result": self.request("pane.focus", {"pane_id": pane})}
            if direction not in FOCUS_DIRECTIONS:
                raise HerdrUnavailable("invalid_request")
            return {"action": "focus", "pane": pane, "result": self.runner(
                self.argv("pane", "focus", "--pane", pane, "--direction", direction))}
        raise HerdrUnavailable("herdr_action_unsupported")

    def tab_create(self, workspace_id: str, payload: dict) -> dict:
        """`herdr tab create` in one owned workspace.

        No `--cwd`: a client-chosen working directory is a path this bridge
        would be handing to a spawned shell, and the owned session already
        starts where core put it.
        """
        workspace_id = workspace_identifier(workspace_id)
        if not isinstance(payload, dict) or set(payload) - {"label", "focus"}:
            raise HerdrUnavailable("invalid_request")
        argv = self.argv("tab", "create", "--workspace", workspace_id)
        label = payload.get("label")
        if label is not None:
            if not isinstance(label, str) or not TAB_LABEL.fullmatch(label):
                raise HerdrUnavailable("invalid_request")
            argv += ("--label", label)
        focus = payload.get("focus", False)
        if type(focus) is not bool:
            raise HerdrUnavailable("invalid_request")
        argv += ("--focus",) if focus else ("--no-focus",)
        return {"action": "create", "workspace": workspace_id, "result": self.runner(argv)}

    def tab_close(self, workspace_id: str, tab_id: str) -> dict:
        workspace_id = workspace_identifier(workspace_id)
        if (not isinstance(tab_id, str) or not TAB_ID.fullmatch(tab_id)
                or not tab_id.startswith(workspace_id + ":")):
            raise HerdrUnavailable("invalid_tab")
        return {"action": "close", "workspace": workspace_id, "tab": tab_id,
                "result": self.runner(self.argv("tab", "close", tab_id))}

    def workspace_create(self, cwd, label: str | None = None) -> dict:
        """HERDR-6: `herdr workspace create --cwd <folder> [--label] --no-focus`.

        `cwd` has already passed `host_paths.folder()` - Herdr would take a
        missing folder silently and open the shell in `$HOME`. `--no-focus`
        keeps the computer where it is: the session's focused workspace (what
        a desktop client shows) does not move, probed on herdr 0.8.2. The one
        exception is Herdr's own: a session with no workspace at all focuses
        its first one, since there was nothing else to look at.

        Verbatim, on herdr 0.8.2 (ids from the probe)::

            {"id":"cli:workspace:create","result":{"type":"workspace_created",
              "workspace":{"workspace_id":"w2","label":"other","number":2,
                           "focused":false,"active_tab_id":"w2:t1",…},
              "tab":{…},"root_pane":{"pane_id":"w2:p1",
                           "cwd":"/home/…/h6test/other",…}}}
        """
        argv = self.argv("workspace", "create", "--cwd", str(cwd))
        if label is not None:
            argv += ("--label", workspace_label(label))
        result = self.runner(argv + ("--no-focus",))
        workspace = result.get("workspace") if isinstance(result.get("workspace"), dict) else {}
        pane = result.get("root_pane") if isinstance(result.get("root_pane"), dict) else {}
        identifier = workspace.get("workspace_id")
        if not isinstance(identifier, str) or not WORKSPACE_ID.fullmatch(identifier):
            raise HerdrUnavailable("herdr_request_failed")
        pane_id = pane.get("pane_id")
        number = workspace.get("number")
        return {"id": identifier,
                "label": workspace.get("label") if isinstance(workspace.get("label"), str) else None,
                "number": number if type(number) is int else None,
                "focused": bool(workspace.get("focused")),
                "pane": pane_id if isinstance(pane_id, str) and PANE_ID.fullmatch(pane_id) else None,
                "cwd": pane.get("cwd") if isinstance(pane.get("cwd"), str) else str(cwd)}

    def workspace_select(self, workspace_id: str) -> dict:
        workspace_id = workspace_identifier(workspace_id)
        return {"action": "select", "workspace": workspace_id,
                "result": self.runner(self.argv("workspace", "focus", workspace_id))}

    def request(self, method: str, params: dict) -> dict:
        """One allowlisted socket call, newline-delimited JSON, same user only."""
        if method not in SOCKET_METHODS:
            raise HerdrUnavailable("herdr_action_unsupported")
        if method == "pane.read" and params.get("source") != READ_SOURCE:
            # Never `recent`: it scrolls the desktop pane to collect its rows.
            raise HerdrUnavailable("herdr_action_unsupported")
        frame = json.dumps({"id": "omodachi-" + os.urandom(8).hex(), "method": method,
                            "params": params}, allow_nan=False) + "\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5.0)
                client.connect(str(self.socket_path))
                client.sendall(frame.encode())
                buffer = bytearray()
                while b"\n" not in buffer:
                    chunk = client.recv(65536)
                    if not chunk:
                        raise HerdrUnavailable("herdr_unavailable")
                    buffer.extend(chunk)
                    if len(buffer) > MAX_SNAPSHOT_BYTES:
                        raise HerdrUnavailable("herdr_unavailable")
            document = json.loads(bytes(buffer).split(b"\n", 1)[0])
        except (OSError, ValueError) as error:
            raise HerdrUnavailable("herdr_unavailable") from error
        if not isinstance(document, dict):
            raise HerdrUnavailable("herdr_request_failed")
        error = document.get("error")
        if error:
            code = error.get("code") if isinstance(error, dict) else None
            # Two of Herdr's own codes mean something a person can act on;
            # every other refusal stays "Herdr refused".
            raise HerdrUnavailable({"pane_not_found": "herdr_pane_gone",
                                    "invalid_key": "invalid_key"}.get(code, "herdr_request_failed"))
        result = document.get("result")
        return result if isinstance(result, dict) else {}

    # --- typing without control (HERDR-3) -------------------------------------
    def _pane_lock(self, pane: str) -> threading.Lock:
        with self._lock:
            return self._pane_locks.setdefault(pane, threading.Lock())

    def send_keys(self, pane: str, keys: Any) -> dict:
        """`pane.send_keys`: one RPC, the keys applied in order."""
        pane = pane_identifier(pane)
        names = key_list(keys)
        with self._pane_lock(pane):
            self.request("pane.send_keys", {"pane_id": pane, "keys": names})
        return {"action": "keys", "pane": pane, "sent": len(names)}

    def read_visible(self, pane: str) -> str:
        result = self.request("pane.read", {"pane_id": pane_identifier(pane), "source": READ_SOURCE,
                                            "format": "text"})
        read = result.get("read") if isinstance(result.get("read"), dict) else result
        text = read.get("text")
        if not isinstance(text, str):
            raise HerdrUnavailable("herdr_unavailable")
        return text

    def agent_blocked(self, pane: str) -> bool:
        """Whether Herdr reports this pane's agent as blocked (a permission or
        approval dialog has the keyboard). A snapshot that cannot be read says
        nothing, and the send goes ahead with the screen proof as before."""
        try:
            snapshot = self.snapshot()
        except (HerdrUnavailable, OSError, ValueError, AttributeError, TypeError):
            return False
        for row in snapshot.get("panes") or []:
            if isinstance(row, dict) and row.get("pane_id") == pane:
                return row.get("agent_status") == "blocked"
        return False

    def send_text(self, pane: str, text: Any, *, submit: bool = False, sleep=time.sleep) -> dict:
        """`pane.send_text`, and Enter only once the screen shows it arrived.

        An ack from Herdr means it took the bytes, never that the program in
        the pane put them in its input box: a dialog with focus swallows the
        text, and a blind Enter would then answer the dialog. So the screen is
        read before the send and again after it, and Enter is pressed only
        when the text is there and was not before. Without that proof nothing
        is submitted and the reply says so; the text itself is not taken back.
        """
        pane = pane_identifier(pane)
        text = pane_text(text)
        if type(submit) is not bool:
            raise HerdrUnavailable("invalid_request")
        with self._pane_lock(pane):
            if submit and self.agent_blocked(pane):
                # VERIFY-1, real codex on the host: an approval dialog reads
                # letters as its own shortcuts, so "continue" sent into one
                # cancelled the request ('n' / 'c') before any proof could
                # hold. While Herdr says the pane's agent is blocked on a
                # question, a reply is not typed at all; the keys answer it.
                return {"action": "text", "pane": pane, "delivered": False, "submitted": False,
                        "reason": "herdr_agent_blocked", "proof": None}
            before = self.read_visible(pane) if submit else ""
            self.request("pane.send_text", {"pane_id": pane, "text": text})
            if not submit:
                return {"action": "text", "pane": pane, "delivered": True, "submitted": False,
                        "reason": "not_requested", "proof": None}
            for attempt in range(self.proof_attempts):
                # The first look is immediate: `pane.read` is a live read, and
                # the echo is often on screen by the time the send returns.
                if attempt:
                    sleep(self.proof_delay)
                try:
                    after = self.read_visible(pane)
                except HerdrUnavailable:
                    continue
                proof = text_proof(text, before, after)
                if proof is not None:
                    try:
                        self.request("pane.send_keys", {"pane_id": pane, "keys": ["Enter"]})
                    except HerdrUnavailable:
                        return {"action": "text", "pane": pane, "delivered": True, "submitted": False,
                                "reason": "submit_failed", "proof": proof}
                    return {"action": "text", "pane": pane, "delivered": True, "submitted": True,
                            "reason": None, "proof": proof}
            return {"action": "text", "pane": pane, "delivered": True, "submitted": False,
                    "reason": "not_seen", "proof": None}

    # --- HERDR-11: start a command in a pane core just made -------------------
    LAUNCH_ATTEMPTS = 16
    LAUNCH_DELAY_SECONDS = 0.25

    def launch(self, pane: str, command: str, *, sleep=time.sleep) -> dict:
        """Type `command` and Enter into a pane this bridge has just created.

        Never anything else: the caller has checked `command` against the
        operator's allowlist, and the pane is the root pane `workspace create`
        returned a moment ago, so nobody has typed in it. The shell is given
        up to four seconds to draw its prompt first (Collie waits for that
        too); a shell that has not drawn by then still reads what was typed
        once it does, from the terminal's own input queue.
        """
        pane = pane_identifier(pane)
        text = pane_text(command)
        drawn = False
        with self._pane_lock(pane):
            for attempt in range(self.LAUNCH_ATTEMPTS):
                if attempt:
                    sleep(self.LAUNCH_DELAY_SECONDS)
                try:
                    if self.read_visible(pane).strip():
                        drawn = True
                        break
                except HerdrUnavailable:
                    continue
            self.request("pane.send_text", {"pane_id": pane, "text": text})
            self.request("pane.send_keys", {"pane_id": pane, "keys": ["Enter"]})
        return {"command": text, "pane": pane, "sent": True, "drawn": drawn, "reason": None}

    def process_info(self, pane: str) -> dict:
        result = self.request("pane.process_info", {"pane_id": pane_identifier(pane)})
        info = result.get("process_info") if isinstance(result.get("process_info"), dict) else result
        return info if isinstance(info, dict) else {}

    # --- streams ------------------------------------------------------------
    def stream_argv(self, pane: str, mode: str, cols: int, rows: int, *, takeover: bool = False):
        pane = pane_identifier(pane)
        cols, rows = geometry(cols, rows)
        if mode not in {"observe", "control"}:
            raise HerdrUnavailable("invalid_request")
        argv = self.argv("terminal", "session", mode, pane, "--cols", str(cols), "--rows", str(rows))
        if mode == "control" and takeover:
            argv += ("--takeover",)
        return argv

    def claim_control(self, pane: str, channel: str, *, takeover: bool = False, evict=None) -> None:
        """Herdr allows one controller per pane; so does this bridge, first.

        `takeover` is the explicit "take it anyway": the stream holding the
        pane is told to end (its `evict`), and this channel holds it from now
        on. Without it a held pane is `409 herdr_control_in_use`, before the
        WebSocket upgrade.
        """
        pane = pane_identifier(pane)
        with self._lock:
            held = self._controllers.get(pane)
            if held is not None and not takeover:
                raise HerdrUnavailable("herdr_control_in_use")
            self._controllers[pane] = (channel, evict)
        if held is not None and callable(held[1]):
            held[1]()

    def release_control(self, pane: str, channel: str) -> None:
        with self._lock:
            held = self._controllers.get(pane)
            if held is not None and held[0] == channel:
                del self._controllers[pane]


class HerdrWriteAudit:
    """One JSONL line per write a device made into a pane (HERDR-3 §5).

    It records who, where, what kind and how much - never the keys or the text
    themselves, because Type mode sends every keystroke through here and some
    of those are passwords. The file is 0600 and rolls once at 1 MiB into a
    single `.1`, so it cannot grow without bound on a host that is used daily.
    """

    MAX_BYTES = 1024 * 1024

    def __init__(self, path=None, *, clock=None) -> None:
        self.path = Path(path) if path is not None else None
        self.clock = clock or (lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self._mutex = threading.Lock()
        self.lines: list[dict] = []  # the last few, for a host without a path

    def record(self, *, device: str | None, session: str, pane: str, kind: str, length: int,
               outcome: str, submitted: bool | None = None, attachments: int = 0) -> dict:
        line = {"at": self.clock(), "device": device, "session": session, "pane": pane,
                "kind": kind, "length": length, "outcome": outcome}
        if submitted is not None:
            line["submitted"] = submitted
        if attachments:
            # ATTACH-1: how many files' paths the text carried, never which.
            line["attachments"] = attachments
        encoded = (json.dumps(line, sort_keys=True, ensure_ascii=True) + "\n").encode()
        with self._mutex:
            self.lines = [*self.lines[-49:], line]
            if self.path is None:
                return line
            try:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(encoded) > self.MAX_BYTES:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                handle = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
                                 | os.O_CLOEXEC, 0o600)
                try:
                    os.fchmod(handle, 0o600)
                    os.write(handle, encoded)
                finally:
                    os.close(handle)
            except OSError:
                # An audit that cannot be written does not block the write it
                # describes; the line is still kept in memory above.
                pass
        return line
