"""CLIP-1. The host's clipboard, for a paired device, when both sides say so.

Three rules shape everything here.

**Off is the default and the refusal is explicit.** `clipboard_sync` ships as
`off`; `host_to_device` lets a device read this host's clipboard and be told
when it changes; `both` additionally lets a device write it. A request the
preference does not allow is refused by code, never answered with an empty
string, because "nothing is copied" and "you may not read this" must not look
the same on a screen.

**The content is not ours to keep.** Text passes through this module on its way
to a socket or to `wl-copy` and is not written anywhere: not to the journal, not
to a log line, not into the event payload. `trace` records the direction, the
length and the time, which is what a person debugging this needs and all of what
they may have. The one identifier kept between calls is a digest of what this
host last wrote or announced, and it exists only to keep a change that is not
news off the wire: our own write arriving back through the watcher, or the same
text copied twice in a row.

**The watcher is the host's own tool.** `wl-paste --type text/plain --watch`
already exists to answer "has the clipboard changed", and Omarchy's clipboard
plugin runs two of them for its history. Ours runs `/bin/echo`, so the watcher
pipe carries one newline per change and never the content; the content is read
back deliberately, on our terms, by the same bounded reader every other path
uses. See `docs/specs/CLIP-1-report.md` §3 for the measured comparison with the
other candidate (tailing the plugin's own history file).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import selectors
import subprocess
import threading
import time

#: The preference's three values. `host_to_device` is one-way on purpose: a
#: device that only wants to paste what it copied on the desktop should not have
#: to grant the reverse.
CLIPBOARD_MODES = ("off", "host_to_device", "both")
#: 64 KiB of UTF-8, both ways. Past this the answer is a refusal with a reason
#: rather than a truncation: half a copied file is worse than none.
LIMIT = 65536
#: The advertised type. Images are the second phase (spec §2); until then a
#: clipboard holding only an image is `clipboard_not_text`, not empty.
TEXT_TYPE = "text/plain"
WL_PASTE = "/usr/bin/wl-paste"
WL_COPY = "/usr/bin/wl-copy"
#: One newline per change, from a program that reads nothing and says nothing.
#:
#: `setpriv --pdeathsig TERM` is Omarchy's own answer for the two watchers its
#: clipboard plugin runs, and it is here for the reason this spec's host walk
#: found: a daemon that is killed rather than shut down does not get to run
#: `stop()`, and the watcher it spawned then outlives it — a `wl-paste` holding
#: a Wayland connection with nothing at the other end of its pipe. The kernel
#: is the only thing that can be relied on to end it, so the kernel is asked.
#: Without `setpriv` the plain form still works; it just leaks that one process.
SETPRIV = "/usr/bin/setpriv"
WATCH_COMMAND = (WL_PASTE, "--type", "text/plain", "--watch", "/bin/echo")
WATCH_ARGV = ((SETPRIV, "--pdeathsig", "TERM") + WATCH_COMMAND
              if os.access(SETPRIV, os.X_OK) else WATCH_COMMAND)
#: How long `wl-copy`/`wl-paste` are given. They talk to a compositor on the
#: same machine; a second is already a fault.
COMMAND_TIMEOUT = 2.0
#: At most one `clipboard.changed` per this many seconds. A person holding a
#: key down in a clipboard manager can move the selection dozens of times a
#: second, and every one of those is the same answer to the same question.
THROTTLE = 0.5
#: How long a dead watcher waits before it is started again, so a session that
#: is gone is not respawned at full speed.
RESTART_DELAY = 2.0
TRACE_LIMIT = 32


class ClipboardError(ValueError):
    """The wire shape of a refusal: a code, a status, no content."""

    def __init__(self, code: str, status: int = 409):
        self.code, self.status, self.detail = code, status, {}
        super().__init__(code)


def _bounded(argv, environment, *, stdin=None, cap=LIMIT + 1, timeout=COMMAND_TIMEOUT):
    """Run one clipboard tool with a deadline and a ceiling on what it may say.

    Returns `(exit code, stdout)`. Unlike `graphical.bounded_hyprctl` a non-zero
    exit is returned rather than raised: `wl-paste` exits 1 for an empty
    clipboard, which is an ordinary answer and not a fault.
    """
    process = subprocess.Popen(argv, env=environment,
                               stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    result = bytearray()
    deadline = time.monotonic() + timeout
    try:
        if stdin is not None:
            try:
                process.stdin.write(stdin)
            except OSError:
                pass
            finally:
                process.stdin.close()
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClipboardError("clipboard_unavailable", 503)
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, max(1, min(65536, cap - len(result))))
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    result.extend(chunk)
                    if len(result) >= cap:
                        selector.unregister(key.fileobj)
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
        return process.returncode, bytes(result)
    except (OSError, subprocess.TimeoutExpired):
        raise ClipboardError("clipboard_unavailable", 503) from None
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        for stream in (process.stdout, process.stdin):
            if stream is not None and not stream.closed:
                stream.close()


class ClipboardService:
    """Read, write and watch one host clipboard, under one preference.

    `mode` is a callable rather than a value because the preference can change
    under a running daemon and the answer must change with it; `apply()` is how
    the daemon tells this object that it has.
    """

    def __init__(self, *, mode=None, environment=None, runner=_bounded, publish=None,
                 clock=time.monotonic, limit=LIMIT, throttle=THROTTLE,
                 restart_delay=RESTART_DELAY, watch_argv=WATCH_ARGV):
        from .graphical import graphical_environment
        self.mode = mode or (lambda: "off")
        self.environment = environment or graphical_environment
        self.runner = runner
        self.publish = publish
        self.clock = clock
        self.limit = limit
        self.throttle = throttle
        self.restart_delay = restart_delay
        self.watch_argv = tuple(watch_argv)
        self.sequence = 0
        #: Direction, length and time. Never content — that is the whole point
        #: of it being a separate structure rather than a log line.
        self.trace: list[dict] = []
        self._lock = threading.RLock()
        self._echo = None
        self._last_event = -float("inf")
        self._pending = False
        self._watcher = None
        self._thread = None
        self._stop = threading.Event()
        # The hub is single-threaded by contract, and the watcher is not on its
        # thread. Whatever loop was running when the watcher started is the one
        # the event is handed back to; without one (a test, a CLI) the publish
        # happens inline, which is the same thread it was asked on.
        self._loop = None

    # -- the preference ----------------------------------------------------

    def _mode(self) -> str:
        try:
            value = self.mode()
        except Exception:
            return "off"
        return value if value in CLIPBOARD_MODES else "off"

    def require(self, direction: str) -> str:
        """The mode, or the refusal that says which switch is in the way.

        `host_to_device` is a read by the device, `device_to_host` a write.
        """
        mode = self._mode()
        if mode == "off":
            raise ClipboardError("clipboard_sync_disabled", 403)
        if direction == "device_to_host" and mode != "both":
            raise ClipboardError("clipboard_write_disabled", 403)
        return mode

    # -- reading and writing ----------------------------------------------

    def _environment(self):
        try:
            environment = self.environment()
        except Exception:
            raise ClipboardError("clipboard_unavailable", 503) from None
        if not environment:
            raise ClipboardError("clipboard_unavailable", 503)
        return dict(environment)

    def _read_bytes(self, environment):
        code, raw = self.runner((WL_PASTE, "--no-newline", "--type", TEXT_TYPE), environment,
                                cap=self.limit + 1)
        if code != 0:
            # `wl-paste` fails the same way for an empty clipboard and for one
            # holding only an image, so ask what is on it before deciding which.
            types, listing = self.runner((WL_PASTE, "--list-types"), environment, cap=4096)
            if types != 0 or not listing.strip():
                return b""
            raise ClipboardError("clipboard_not_text", 409)
        if len(raw) > self.limit:
            raise ClipboardError("clipboard_too_large", 413)
        return raw

    def read(self) -> str:
        """The host's clipboard as text, or `""` when nothing is copied."""
        self.require("host_to_device")
        with self._lock:
            raw = self._read_bytes(self._environment())
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ClipboardError("clipboard_not_text", 409) from None
            self._record("host_to_device", len(raw))
            return text

    def write(self, text) -> dict:
        """Put `text` on the host's clipboard. Answers with what was accepted."""
        self.require("device_to_host")
        if not isinstance(text, str):
            raise ClipboardError("clipboard_invalid", 400)
        raw = text.encode("utf-8")
        if len(raw) > self.limit:
            raise ClipboardError("clipboard_too_large", 413)
        with self._lock:
            environment = self._environment()
            # An empty write would leave `wl-copy` holding an empty selection,
            # which is not the same as the clipboard being cleared and is not
            # something a device should be able to ask for by accident.
            if not raw:
                raise ClipboardError("clipboard_invalid", 400)
            code, _ = self.runner((WL_COPY, "--type", "text/plain;charset=utf-8"),
                                  environment, stdin=raw, cap=4096)
            if code != 0:
                raise ClipboardError("clipboard_unavailable", 503)
            # Our own write is about to arrive back through the watcher. It is
            # not news, so it is remembered by digest — the shortest thing that
            # can recognise it without keeping it.
            self._echo = hashlib.sha256(raw).hexdigest()
            self._record("device_to_host", len(raw))
            return {"bytes": len(raw), "mime": TEXT_TYPE}

    def _record(self, direction, length):
        self.trace.append({"direction": direction, "bytes": length, "ts": time.time()})
        del self.trace[:-TRACE_LIMIT]

    # -- watching ----------------------------------------------------------

    def apply(self, mode=None) -> bool:
        """Start or stop the watcher to match the preference. Returns running."""
        mode = self._mode() if mode is None else mode
        if mode in {"host_to_device", "both"}:
            self.start()
        else:
            self.stop()
        return self._thread is not None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None
            self._thread = threading.Thread(target=self._watch_loop, name="omodachi-clipboard",
                                            daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            watcher, self._watcher = self._watcher, None
            thread, self._thread = self._thread, None
        if watcher is not None and watcher.poll() is None:
            watcher.kill()
            try:
                watcher.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _watch_loop(self):
        while not self._stop.is_set():
            try:
                environment = self._environment()
            except ClipboardError:
                if self._stop.wait(self.restart_delay):
                    return
                continue
            try:
                watcher = subprocess.Popen(self.watch_argv, env=environment,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL)
            except OSError:
                if self._stop.wait(self.restart_delay):
                    return
                continue
            with self._lock:
                if self._stop.is_set():
                    watcher.kill()
                    watcher.wait(timeout=1)
                    if watcher.stdout is not None:
                        watcher.stdout.close()
                    return
                self._watcher = watcher
            try:
                while not self._stop.is_set():
                    # `readline`, not iteration: a file iterator reads ahead,
                    # and a clipboard change that arrives when the read-ahead
                    # buffer is not full would sit there until the next one.
                    # That is a change announced minutes late, or never.
                    line = watcher.stdout.readline()
                    if not line:
                        break
                    if line.strip():
                        continue  # `/bin/echo` says one empty line; anything else is not ours.
                    if self.changed() is None and self._pending:
                        # The throttle swallowed it. Wait out the window and
                        # publish once, so a burst ends with the truth on the
                        # wire rather than with the last change unannounced.
                        if self._stop.wait(self.throttle):
                            break
                        self.flush()
            except (OSError, ValueError):
                pass
            finally:
                if watcher.poll() is None:
                    watcher.kill()
                try:
                    watcher.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                if watcher.stdout is not None and not watcher.stdout.closed:
                    watcher.stdout.close()
            # A watcher that exits is a session that went away (or a compositor
            # that restarted). Wait, then look for the session again.
            if self._stop.wait(self.restart_delay):
                return

    def changed(self) -> dict | None:
        """One clipboard change, throttled and de-echoed. Returns the payload.

        `None` means nothing was published: the preference is off, this is our
        own write coming back, the clipboard is not text, or the last event was
        less than `throttle` ago.
        """
        if self._mode() not in {"host_to_device", "both"}:
            return None
        now = self.clock()
        with self._lock:
            if now - self._last_event < self.throttle:
                self._pending = True
                return None
            self._last_event = now
            self._pending = False
            try:
                raw = self._read_bytes(self._environment())
            except ClipboardError:
                return None
            if not raw:
                return None
            digest = hashlib.sha256(raw).hexdigest()
            # `_echo` is the last content this daemon either wrote or announced.
            # Skipping a match covers both of the cases where a change is not
            # news: our own `wl-copy` arriving back through the watcher, and the
            # desktop putting the same text on the clipboard twice in a row.
            if digest == self._echo:
                return None
            self._echo = digest
            self.sequence += 1
            payload = {"sequence": self.sequence, "bytes": len(raw), "mime": TEXT_TYPE}
            self._record("host_to_device", len(raw))
        self._emit("clipboard.changed", dict(payload))
        return payload

    def _emit(self, event, payload):
        """Hand one event to the hub on the hub's own thread."""
        if not callable(self.publish):
            return
        loop = self._loop
        if loop is not None:
            try:
                if loop.is_running() and asyncio.get_running_loop() is not loop:
                    raise RuntimeError
            except RuntimeError:
                try:
                    loop.call_soon_threadsafe(self.publish, event, payload)
                    return
                except RuntimeError:
                    pass
        self.publish(event, payload)

    def flush(self) -> dict | None:
        """Publish a change the throttle swallowed. The daemon's tick calls it."""
        with self._lock:
            if not self._pending or self.clock() - self._last_event < self.throttle:
                return None
            self._pending = False
            self._last_event = -float("inf")
        return self.changed()
