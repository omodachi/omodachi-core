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
from typing import Any

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


def pane_identifier(value: Any) -> str:
    if not isinstance(value, str) or not PANE_ID.fullmatch(value):
        raise HerdrUnavailable("invalid_pane")
    return value


def workspace_identifier(value: Any) -> str:
    if not isinstance(value, str) or not WORKSPACE_ID.fullmatch(value):
        raise HerdrUnavailable("invalid_workspace")
    return value


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
        self._controllers: dict[str, str] = {}

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
                agents[agent["pane_id"]] = {"name": agent.get("name"), "kind": agent.get("kind"),
                                            "status": agent.get("status")}
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
        with self._lock:
            if signature != self._signature:
                self._signature = signature
                self._revision += 1
            revision = self._revision
        return {**payload, "revision": revision}

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

    def workspace_select(self, workspace_id: str) -> dict:
        workspace_id = workspace_identifier(workspace_id)
        return {"action": "select", "workspace": workspace_id,
                "result": self.runner(self.argv("workspace", "focus", workspace_id))}

    def request(self, method: str, params: dict) -> dict:
        """One allowlisted socket call, newline-delimited JSON, same user only."""
        if method not in {"pane.focus"}:
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
        if not isinstance(document, dict) or document.get("error"):
            raise HerdrUnavailable("herdr_request_failed")
        result = document.get("result")
        return result if isinstance(result, dict) else {}

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

    def claim_control(self, pane: str, channel: str) -> None:
        """Herdr allows one controller per pane; so does this bridge, first."""
        pane = pane_identifier(pane)
        with self._lock:
            if pane in self._controllers:
                raise HerdrUnavailable("herdr_control_in_use")
            self._controllers[pane] = channel

    def release_control(self, pane: str, channel: str) -> None:
        with self._lock:
            if self._controllers.get(pane) == channel:
                del self._controllers[pane]
