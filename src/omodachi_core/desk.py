"""NOTIFY-1 part 2: is Leo at the computer right now?

"At the desk" means three things at once, all read without privileges and
without any hook on the input devices:

* **recently used** - the compositor's own idle timer, through the Wayland
  protocol `ext-idle-notify-v1`, the same one hypridle and swayidle use. This
  process registers one notification with a timeout and Hyprland tells it
  `idled` after that long without input and `resumed` at the next input. No
  input event, key or pointer position ever reaches this process: only those
  two words. Version 2's `get_input_idle_notification` is preferred, because it
  ignores idle inhibitors (a playing video would otherwise keep the desk
  "busy" forever).
* **unlocked** - no screen locker of this user is running (`hyprlock` is
  Omarchy's; the other common Wayland lockers are listed too). Read from
  `/proc`, never by asking the locker anything.
* **not driven from the phone** - while a Remote session is live, the input the
  compositor sees is this person's phone, so the desk does not count as
  occupied.

When the idle watch cannot start (no Wayland socket, a compositor without the
protocol), presence is `unknown` and nothing is ever held because of it: the
alerts go out the way they always did. That is the degraded mode, and `/v1/state`
says so (`desk.state == "unknown"`, `desk.source == "unavailable"`).

The Wayland client below speaks the wire protocol directly (it is four
requests and four events), so core needs no new dependency.
"""
from __future__ import annotations

import os
from pathlib import Path
import select
import socket
import struct
import threading
import time

# How long without input before the desk counts as empty. Spec: "e.g. 2 min".
IDLE_SECONDS = 120
LOCKERS = ("hyprlock", "swaylock", "gtklock", "waylock")
IDLE_NOTIFIER = "ext_idle_notifier_v1"


class WaylandError(OSError):
    pass


def _string(value: str) -> bytes:
    raw = value.encode() + b"\0"
    return struct.pack("<I", len(raw)) + raw + b"\0" * (-len(raw) % 4)


def _message(obj: int, opcode: int, body: bytes = b"") -> bytes:
    return struct.pack("<II", obj, ((8 + len(body)) << 16) | opcode) + body


def _read_string(body: bytes, offset: int) -> tuple[str, int]:
    (length,) = struct.unpack_from("<I", body, offset)
    offset += 4
    text = body[offset:offset + max(0, length - 1)].decode(errors="replace")
    return text, offset + length + (-length % 4)


def wayland_socket_path(env=None) -> Path | None:
    env = os.environ if env is None else env
    display = env.get("WAYLAND_DISPLAY") or "wayland-0"
    if os.path.isabs(display):
        return Path(display)
    runtime = env.get("XDG_RUNTIME_DIR")
    return Path(runtime) / display if runtime else None


class WaylandIdleClient:
    """One connection, one idle notification, two possible events.

    `connect()` does the registry round trip and raises `WaylandError` when the
    compositor has no `ext_idle_notifier_v1` or no seat. `read_events()` then
    returns `"idled"` / `"resumed"` words as they arrive.
    """

    DISPLAY, REGISTRY, CALLBACK, SEAT, NOTIFIER, NOTIFICATION = 1, 2, 3, 4, 5, 6

    def __init__(self, path: Path, timeout_ms: int, *, sock=None):
        self.path, self.timeout_ms = path, int(timeout_ms)
        self.sock = sock
        self._buffer = b""
        self.globals: dict[str, tuple[int, int]] = {}
        self.version = 0

    def fileno(self) -> int:
        return self.sock.fileno()

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _send(self, data: bytes):
        self.sock.sendall(data)

    def _messages(self, block=True, timeout=5.0):
        """Whatever whole messages are buffered, reading the socket once."""
        if block:
            ready, _, _ = select.select([self.sock], [], [], timeout)
            if not ready:
                raise WaylandError("wayland compositor did not answer")
        chunk = self.sock.recv(65536)
        if not chunk:
            raise WaylandError("wayland connection closed")
        self._buffer += chunk
        out = []
        while len(self._buffer) >= 8:
            obj, word = struct.unpack_from("<II", self._buffer)
            size, opcode = word >> 16, word & 0xFFFF
            if size < 8 or len(self._buffer) < size:
                break
            out.append((obj, opcode, self._buffer[8:size]))
            self._buffer = self._buffer[size:]
        return out

    def connect(self):
        if self.sock is None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
            self.sock.connect(str(self.path))
        # wl_display.get_registry(new_id) and wl_display.sync(new_id): the
        # callback's `done` marks the end of the global list.
        self._send(_message(self.DISPLAY, 1, struct.pack("<I", self.REGISTRY))
                   + _message(self.DISPLAY, 0, struct.pack("<I", self.CALLBACK)))
        deadline = time.monotonic() + 5
        done = False
        while not done:
            for obj, opcode, body in self._messages(timeout=max(0.1, deadline - time.monotonic())):
                if obj == self.DISPLAY and opcode == 0:
                    raise WaylandError("wayland protocol error")
                if obj == self.REGISTRY and opcode == 0:
                    (name,) = struct.unpack_from("<I", body)
                    interface, offset = _read_string(body, 4)
                    (version,) = struct.unpack_from("<I", body, offset)
                    if interface not in self.globals:
                        self.globals[interface] = (name, version)
                elif obj == self.CALLBACK and opcode == 0:
                    done = True
        if IDLE_NOTIFIER not in self.globals:
            raise WaylandError("compositor has no ext_idle_notifier_v1")
        if "wl_seat" not in self.globals:
            raise WaylandError("compositor has no seat")
        seat_name, _ = self.globals["wl_seat"]
        notifier_name, advertised = self.globals[IDLE_NOTIFIER]
        self.version = min(advertised, 2)
        # wl_registry.bind(name, interface, version, new_id), twice; then the
        # notification itself. v2's opcode 2 ignores idle inhibitors.
        self._send(
            _message(self.REGISTRY, 0, struct.pack("<I", seat_name) + _string("wl_seat")
                     + struct.pack("<II", 1, self.SEAT))
            + _message(self.REGISTRY, 0, struct.pack("<I", notifier_name) + _string(IDLE_NOTIFIER)
                       + struct.pack("<II", self.version, self.NOTIFIER))
            + _message(self.NOTIFIER, 2 if self.version >= 2 else 1,
                       struct.pack("<III", self.NOTIFICATION, self.timeout_ms, self.SEAT)))
        return self

    def read_events(self) -> list[str]:
        words = []
        for obj, opcode, body in self._messages(block=False):
            if obj == self.DISPLAY and opcode == 0:
                raise WaylandError("wayland protocol error")
            if obj == self.NOTIFICATION:
                words.append("idled" if opcode == 0 else "resumed" if opcode == 1 else "?")
        return words


def locked(proc: Path = Path("/proc"), uid: int | None = None) -> bool:
    """A screen locker of this user is running."""
    uid = os.getuid() if uid is None else uid
    try:
        entries = list(proc.iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            if (entry / "comm").read_text().strip() in LOCKERS:
                return True
        except OSError:
            continue
    return False


class DeskPresence:
    """The three readings, combined into one answer and kept current.

    A background thread owns the Wayland connection and records `idle`; the
    daemon's loop calls `evaluate()` (cheap) and publishes when the answer
    changes. `present` is only ever true when the idle watch is live.
    """

    def __init__(self, *, idle_seconds=IDLE_SECONDS, env=None, locked_probe=locked,
                 client_factory=None, clock=time.monotonic, retry_seconds=60.0):
        self.idle_seconds = idle_seconds
        self._env = env
        self._locked_probe = locked_probe
        self._client_factory = client_factory or (lambda path, ms: WaylandIdleClient(path, ms))
        self._clock = clock
        self._retry = retry_seconds
        self._lock = threading.Lock()
        self.idle: bool | None = None  # None: the watch is not live
        self.source = "unavailable"
        self._stop = threading.Event()
        self._thread = None
        self._wake_r = self._wake_w = None

    # --- the idle watch -------------------------------------------------------
    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._wake_r, self._wake_w = os.pipe()
        self._thread = threading.Thread(target=self._run, name="omodachi-desk-idle", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._wake_w is not None:
            try:
                os.write(self._wake_w, b"x")
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None
        for fd in (self._wake_r, self._wake_w):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._wake_r = self._wake_w = None

    def _set(self, idle, source):
        with self._lock:
            self.idle, self.source = idle, source

    def _run(self):
        while not self._stop.is_set():
            path = wayland_socket_path(self._env)
            client = None
            try:
                if path is None or not path.exists():
                    raise WaylandError("no wayland socket")
                client = self._client_factory(path, int(self.idle_seconds * 1000)).connect()
                # The timer starts now: until it first fires, the desk counts
                # as used (core just started, most likely because someone is there).
                self._set(False, "wayland")
                while not self._stop.is_set():
                    ready, _, _ = select.select([client, self._wake_r], [], [], 30)
                    if self._wake_r in ready:
                        break
                    if client in ready:
                        for word in client.read_events():
                            if word in ("idled", "resumed"):
                                self._set(word == "idled", "wayland")
            except (OSError, ValueError, struct.error):
                pass
            finally:
                if client is not None:
                    client.close()
            self._set(None, "unavailable")
            if self._stop.wait(self._retry):
                return

    # --- the answer -----------------------------------------------------------
    def evaluate(self, *, remote_live: bool = False) -> dict:
        """`{present, state, source}`, `state` one of `using` (present),
        `idle`, `locked`, `remote`, `unknown`."""
        with self._lock:
            idle, source = self.idle, self.source
        try:
            is_locked = bool(self._locked_probe())
        except Exception:
            is_locked = False
        if is_locked:
            state = "locked"
        elif remote_live:
            state = "remote"
        elif idle is None:
            state = "unknown"
        elif idle:
            state = "idle"
        else:
            state = "using"
        return {"present": state == "using", "state": state, "source": source}


def probe(seconds: float, idle_ms: int = 5000) -> int:
    """`python -m omodachi_core.desk` on a host: what this module would read.

    Read-only: it registers one idle notification, prints the two words as
    they come, and the locker check; it sends no input and changes nothing.
    """
    path = wayland_socket_path()
    print({"socket": str(path), "locked": locked()})
    client = WaylandIdleClient(path, idle_ms).connect()
    print({"notifier_version": client.version,
           "globals": sorted(name for name in client.globals if "idle" in name or name == "wl_seat")})
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _, _ = select.select([client], [], [], max(0.0, end - time.monotonic()))
        if ready:
            for word in client.read_events():
                print({"t": round(seconds - (end - time.monotonic()), 1), "event": word})
    client.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - a host probe
    import sys
    raise SystemExit(probe(float(sys.argv[1]) if len(sys.argv) > 1 else 12.0))
