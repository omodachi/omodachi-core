"""Explicit-session, owned null-sink -> virtual-source PCM consumer.

Import and construction perform no audio operation. begin() is called only by
an authorized microphone session. No defaults, physical sources or loopback
are accessed. PCM is never logged/persisted. Cleanup failures retain ownership.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import threading
import time
from typing import Callable

FRAME_BYTES = 1920
FRAME_SAMPLES = 960
RATE = 48000
FORMAT = "s16le"
CHANNELS = 1
MAX_FRAMES = 3
MAX_FLUSH_SECONDS = 0.06
MAX_METADATA = 2 * 1024 * 1024
_LIST_KINDS = {"modules", "sinks", "sources"}
_PREFIX = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,31}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")


class VirtualInputError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _index(value):
    if type(value) is int and 0 <= value < 2**32: return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value) and int(value) < 2**32: return int(value)
    raise VirtualInputError("virtual_input_metadata_invalid")


@dataclass(frozen=True)
class VirtualInputMetadata:
    generation: int
    session_nonce: str
    sink_name: str
    source_name: str
    sink_module_id: int | None = None
    remap_module_id: int | None = None

    def as_dict(self):
        return {"generation": self.generation, "session_nonce": self.session_nonce,
                "sink_name": self.sink_name, "source_name": self.source_name,
                "sink_module_id": self.sink_module_id, "remap_module_id": self.remap_module_id,
                "format": FORMAT, "rate": RATE, "channels": CHANNELS,
                "frame_samples": FRAME_SAMPLES, "frame_bytes": FRAME_BYTES}


class _Writer:
    """Only the writer thread may change current/offset or discard its queue."""
    def __init__(self, process, *, max_frames=MAX_FRAMES, write=os.write, clock=time.monotonic):
        self.process, self.write, self.clock = process, write, clock
        self.max_frames = max_frames
        self.condition = threading.Condition()
        self.queue = deque()
        self.current = None
        self.offset = 0
        self.stopping = False
        self.flush_until = 0.0
        self.failure = None
        self.written_frames = self.dropped_frames = 0
        self.stdin_closed = False
        self.close_errors = []
        try:
            self.fd = process.stdin.fileno()
            os.set_blocking(self.fd, False)
            if process.poll() is not None: raise VirtualInputError("virtual_input_writer_exited")
        except VirtualInputError: raise
        except (AttributeError, OSError, ValueError): raise VirtualInputError("virtual_input_writer_invalid") from None
        self.thread = threading.Thread(target=self._run, name="omodachi-virtual-mic", daemon=True)
        self.thread.start()

    def accept(self, frame):
        if not isinstance(frame, bytes) or len(frame) != FRAME_BYTES:
            raise VirtualInputError("virtual_input_frame_invalid")
        with self.condition:
            if self.stopping or self.failure or not self.thread.is_alive():
                raise VirtualInputError(self.failure or "virtual_input_not_active")
            if self.process.poll() is not None:
                self.failure = "virtual_input_writer_exited";self.condition.notify_all()
                raise VirtualInputError(self.failure)
            if len(self.queue) + (self.current is not None) >= self.max_frames:
                self.dropped_frames += 1
                return False
            self.queue.append(frame);self.condition.notify_all()
            return True

    def _run(self):
        try:
            while True:
                with self.condition:
                    if self.failure: return
                    if self.process.poll() is not None:
                        self.failure = "virtual_input_writer_exited";return
                    if self.stopping and (self.clock() >= self.flush_until or self.current is None and not self.queue): return
                    if self.current is None:
                        if not self.queue:
                            self.condition.wait(0.01);continue
                        self.current = self.queue.popleft();self.offset = 0
                    frame, offset = self.current, self.offset
                try:
                    count = self.write(self.fd, frame[offset:])
                    if type(count) is not int or not 0 < count <= len(frame) - offset:
                        raise OSError("invalid write")
                except BlockingIOError:
                    with self.condition: self.condition.wait(0.002)
                    continue
                except OSError:
                    with self.condition: self.failure = "virtual_input_writer_pipe_failed"
                    return
                with self.condition:
                    self.offset += count
                    if self.offset == FRAME_BYTES:
                        self.current = None;self.offset = 0;self.written_frames += 1
        except Exception:
            with self.condition: self.failure = "virtual_input_writer_failed"
        finally:
            # close() never races this mutation or reuses a live thread's fd.
            with self.condition:
                self.dropped_frames += len(self.queue) + (self.current is not None)
                self.queue.clear();self.current = None;self.offset = 0
                self.condition.notify_all()

    def _stop_process(self):
        errors = []
        try:
            if self.process.poll() is None: self.process.terminate()
            self.process.wait(timeout=0.5)
        except (OSError, subprocess.SubprocessError):
            try:
                if self.process.poll() is None: self.process.kill()
                self.process.wait(timeout=0.5)
            except (OSError, subprocess.SubprocessError): errors.append("virtual_input_process_stop_failed")
        return errors

    def close(self, *, flush=False):
        with self.condition:
            self.stopping = True
            # A normal disconnect discards unsent audio; an explicit bounded
            # drain is available for synthetic tests only.
            self.flush_until = self.clock() + (MAX_FLUSH_SECONDS if flush else 0)
            self.condition.notify_all()
        self.thread.join(timeout=MAX_FLUSH_SECONDS + 0.1)
        errors = self._stop_process()
        if self.thread.is_alive(): self.thread.join(timeout=0.2)
        if self.thread.is_alive():
            errors.append("virtual_input_writer_join_pending")
        else:
            try: self.process.stdin.close();self.stdin_closed = True
            except ValueError: self.stdin_closed = True
            except OSError: errors.append("virtual_input_stdin_close_failed")
        try: exited = self.process.poll() is not None
        except Exception: exited = False
        if not exited and "virtual_input_process_stop_failed" not in errors: errors.append("virtual_input_process_stop_failed")
        self.close_errors = errors
        return {"input_closed": not self.thread.is_alive() and self.stdin_closed and exited,
                "errors": list(errors), "writer_failure": self.failure}

    def stats(self):
        with self.condition:
            pending = len(self.queue) + (self.current is not None)
            return {"writer_failed": self.failure is not None, "writer_failure": self.failure,
                    "writer_thread_alive": self.thread.is_alive(), "written_frames": self.written_frames,
                    "dropped_frames": self.dropped_frames, "queued_frames": pending,
                    "pending_bytes": sum(map(len, self.queue)) + (FRAME_BYTES - self.offset if self.current is not None else 0)}


class VirtualMicrophoneSession:
    """Control methods may wait boundedly; accept() never waits for pactl/cleanup."""
    def __init__(self, *, runner: Callable | None = None, process_factory: Callable | None = None,
                 clock=time.monotonic, name_prefix="omodachi-mic", nonce_factory=None, write=os.write,
                 stable_name: str | None = None):
        if not isinstance(name_prefix, str) or not _PREFIX.fullmatch(name_prefix):
            raise VirtualInputError("virtual_input_name_invalid")
        # Voxtype selects its capture device by name from config.toml, so the
        # source this session publishes has to be the same name every time. A
        # per-session name is still used when nobody has to find it by name.
        if stable_name is not None and (not isinstance(stable_name, str) or not _PREFIX.fullmatch(stable_name)):
            raise VirtualInputError("virtual_input_name_invalid")
        self.stable_name = stable_name
        self.runner = runner or self._run
        self.process_factory = process_factory or self._spawn
        self.clock, self.name_prefix, self.write = clock, name_prefix, write
        self.nonce_factory = nonce_factory or (lambda: secrets.token_hex(16))
        self.metadata = None
        self.writer = None
        self._process = None
        self.closed = True
        self.input_closed = True
        self._accepting = False
        self._owned = {}
        self._expected = {}
        self._last_stats = {}
        self._last_result = None
        self._control_lock = threading.RLock()
        self._highest_generation = 0

    @staticmethod
    def _run(argv):
        if (len(argv) == 4 and argv[:3] == ("pactl", "--format=json", "list") and argv[3] in _LIST_KINDS):
            operation = "list"
        elif argv == ("pactl", "list", "short", "modules"):
            operation = "list_short_modules"
        elif len(argv) >= 3 and argv[:2] == ("pactl", "load-module") and argv[2] in {"module-null-sink", "module-remap-source"}:
            operation = "load"
        elif len(argv) == 3 and argv[:2] == ("pactl", "unload-module") and re.fullmatch(r"[0-9]+", argv[2]):
            operation = "unload"
        else: raise VirtualInputError("virtual_input_command_not_allowed")
        try:
            # A regular temporary metadata file bounds memory while stdout is
            # produced; it never contains audio, credentials or command stderr.
            import tempfile
            with tempfile.TemporaryFile() as output:
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL, shell=False)
                deadline = time.monotonic() + 2.0
                try:
                    while process.poll() is None:
                        if output.tell() > MAX_METADATA: raise VirtualInputError("virtual_input_metadata_limit")
                        if time.monotonic() >= deadline: raise VirtualInputError("virtual_input_command_timeout")
                        time.sleep(0.005)
                    if process.returncode: raise VirtualInputError("virtual_input_command_failed")
                    output.seek(0);raw = output.read(MAX_METADATA + 1)
                    if len(raw) > MAX_METADATA: raise VirtualInputError("virtual_input_metadata_limit")
                finally:
                    if process.poll() is None: process.kill();process.wait(timeout=0.5)
            if operation == "unload": return None  # successful empty stdout is normal
            if operation == "load":
                text = raw.decode().strip()
                if not re.fullmatch(r"[0-9]{1,10}", text): raise VirtualInputError("virtual_input_module_id_invalid")
                return _index(text)
            if operation == "list_short_modules":
                # Some pactl versions omit module indexes from JSON. Short
                # output carries them, but unrelated rows can have a blank ID.
                # Keep IDs as text; ownership lookup selects only our exact ID.
                rows = []
                for line in raw.decode().splitlines():
                    fields = line.split("\t")
                    if len(fields) >= 3:
                        rows.append({"index": fields[0], "name": fields[1], "argument": fields[2]})
                if len(rows) > 4096: raise VirtualInputError("virtual_input_metadata_invalid")
                return rows
            value = json.loads(raw)
            if not isinstance(value, list) or len(value) > 4096: raise VirtualInputError("virtual_input_metadata_invalid")
            return value
        except VirtualInputError: raise
        except (OSError, ValueError, subprocess.SubprocessError): raise VirtualInputError("virtual_input_backend_failed") from None

    @staticmethod
    def _spawn(argv):
        return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, shell=False, start_new_session=True, bufsize=0)

    def _list(self, kind):
        rows = self.runner(("pactl", "--format=json", "list", kind))
        if not isinstance(rows, list) or len(rows) > 4096 or any(not isinstance(row, dict) for row in rows):
            raise VirtualInputError("virtual_input_metadata_invalid")
        return rows

    def _module_row(self, rows, ident):
        def is_target(row):
            value = row.get("index")
            return type(value) is int and value == ident or isinstance(value, str) and value == str(ident)
        matches = [row for row in rows if is_target(row)]
        if len(matches) > 1: raise VirtualInputError("virtual_input_metadata_invalid")
        if matches: return matches[0]
        kinds = [kind for kind, owned_id in self._owned.items() if owned_id == ident]
        if all("index" in row for row in rows):
            if any(self._matches_module(row, kind) for row in rows for kind in kinds):
                raise VirtualInputError("virtual_input_module_metadata_unconfirmed")
            return None
        # Join our returned load ID to a short-list row, then to its exact JSON
        # name/arguments. A name or nonce alone never authorizes unloading.
        short_rows = self.runner(("pactl", "list", "short", "modules"))
        if (not isinstance(short_rows, list) or len(short_rows) > 4096
                or any(not isinstance(row, dict) for row in short_rows)):
            raise VirtualInputError("virtual_input_metadata_invalid")
        short_matches = [row for row in short_rows if is_target(row)]
        if len(short_matches) > 1: raise VirtualInputError("virtual_input_metadata_invalid")
        if not short_matches:
            if any(self._matches_module(row, kind) for row in rows for kind in kinds):
                raise VirtualInputError("virtual_input_module_metadata_unconfirmed")
            return None
        target = short_matches[0]
        linked = [row for row in rows if row.get("name") == target.get("name")
                  and row.get("argument") == target.get("argument")]
        if len(linked) != 1:
            raise VirtualInputError("virtual_input_module_metadata_unconfirmed")
        return {**linked[0], "index": ident}

    def _matches_module(self, row, kind):
        if row is None or row.get("name") != self._expected[kind][0]: return False
        try: args = dict(item.split("=", 1) for item in shlex.split(row.get("argument", "")) if "=" in item)
        except (ValueError, TypeError): return False
        return all(args.get(key) == value for key, value in self._expected[kind][1].items())

    def _verify_resources(self):
        meta = self.metadata
        modules = self._list("modules")
        for kind, ident in self._owned.items():
            if not self._matches_module(self._module_row(modules, ident), kind):
                raise VirtualInputError("virtual_input_module_ownership_mismatch")
        sinks = [row for row in self._list("sinks") if row.get("name") == meta.sink_name]
        sources = self._list("sources")
        monitor = [row for row in sources if row.get("name") == meta.sink_name + ".monitor"]
        remap = [row for row in sources if row.get("name") == meta.source_name]
        if (len(sinks) != 1 or len(monitor) != 1 or len(remap) != 1
                or _index(sinks[0].get("owner_module")) != meta.sink_module_id
                or _index(monitor[0].get("owner_module")) != meta.sink_module_id
                or _index(remap[0].get("owner_module")) != meta.remap_module_id):
            raise VirtualInputError("virtual_input_node_ownership_mismatch")
        return {"sink_index": _index(sinks[0].get("index")), "source_index": _index(remap[0].get("index")), "ownership_verified": True}

    def verify_owned(self):
        with self._control_lock:
            if not self.metadata or self.closed: raise VirtualInputError("virtual_input_not_active")
            return {**self.metadata.as_dict(), **self._verify_resources()}

    def begin(self, *, generation):
        with self._control_lock:
            if not self.closed: raise VirtualInputError("virtual_input_cleanup_or_session_active")
            if type(generation) is not int or not 0 < generation < 2**53 or generation <= self._highest_generation:
                raise VirtualInputError("virtual_input_generation_invalid")
            nonce = self.nonce_factory()
            if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce): raise VirtualInputError("virtual_input_nonce_invalid")
            name = (self.stable_name + "_sink") if self.stable_name else f"{self.name_prefix}-g{generation:x}-{nonce}"
            source = self.stable_name or (name + "-source")
            wanted = {name, name + ".monitor", source}
            if any(row.get("name") in wanted for kind in ("sinks", "sources") for row in self._list(kind)):
                if self.stable_name is None:
                    raise VirtualInputError("virtual_input_name_collision")
                # A fixed name means a leaked module from a crashed session
                # would block this one forever. Reclaim only modules whose name
                # AND full argument list are byte-for-byte the ones we create.
                self._reclaim(name, source)
                if any(row.get("name") in wanted for kind in ("sinks", "sources") for row in self._list(kind)):
                    raise VirtualInputError("virtual_input_name_collision")
            self._highest_generation = generation
            self.metadata = VirtualInputMetadata(generation, nonce, name, source)
            self.closed = False;self.input_closed = True;self._accepting = False;self._last_result = None
            self._owned = {};self._last_stats = {};self.writer = None;self._process = None
            self._expected = {
                "sink": ("module-null-sink", {"sink_name": name, "format": FORMAT, "rate": str(RATE), "channels": "1", "channel_map": "mono"}),
                "remap": ("module-remap-source", {"source_name": source, "master": name + ".monitor", "format": FORMAT, "rate": str(RATE), "channels": "1", "channel_map": "mono"})}
            process = None
            try:
                for kind, properties in (("sink", f"sink_properties=device.description={name}"), ("remap", f"source_properties=device.description={source}")):
                    module_name, arguments = self._expected[kind]
                    ident = _index(self.runner(("pactl", "load-module", module_name, *[key + "=" + value for key, value in arguments.items()], properties)))
                    self._owned[kind] = ident
                    self.metadata = replace(self.metadata, **{("sink_module_id" if kind == "sink" else "remap_module_id"): ident})
                verified = self._verify_resources()
                argv = ("pacat", "--playback", "--raw", f"--device={name}", f"--format={FORMAT}", f"--rate={RATE}",
                        "--channels=1", "--channel-map=mono", "--latency-msec=20", "--process-time-msec=20",
                        "--property=node.dont-fallback=true", "--property=node.dont-reconnect=true")
                process = self.process_factory(argv)
                self._process = process;self.input_closed = False
                self.writer = _Writer(process, clock=self.clock, write=self.write)
                self.input_closed = False;self._accepting = True
                return {**self.metadata.as_dict(), **verified, "state": "active", "input_closed": False, "cleanup_complete": False}
            except Exception as error:
                self._accepting = False
                self.end(generation=generation, reason="begin_failed")
                raise VirtualInputError(error.code if isinstance(error, VirtualInputError) else "virtual_input_begin_failed") from None

    def _reclaim(self, sink_name, source_name):
        """Unload leaked modules that are exactly ours, and nothing else."""
        expected = {
            "sink": ("module-null-sink", {"sink_name": sink_name, "format": FORMAT, "rate": str(RATE),
                                          "channels": "1", "channel_map": "mono"}),
            "remap": ("module-remap-source", {"source_name": source_name, "master": sink_name + ".monitor",
                                              "format": FORMAT, "rate": str(RATE), "channels": "1",
                                              "channel_map": "mono"})}
        previous, self._expected = self._expected, expected
        try:
            rows = self._list("modules")
            targets = []
            for kind in ("remap", "sink"):
                for row in rows:
                    if self._matches_module(row, kind):
                        try: targets.append((kind, _index(row.get("index"))))
                        except VirtualInputError: pass
            for _, ident in targets:
                try: self.runner(("pactl", "unload-module", str(ident)))
                except Exception: raise VirtualInputError("virtual_input_reclaim_failed") from None
        finally:
            self._expected = previous

    def accept(self, frame):
        writer = self.writer
        if not self._accepting or writer is None: raise VirtualInputError("virtual_input_not_active")
        return writer.accept(frame)

    def _cleanup_modules(self):
        errors = []
        for kind in ("remap", "sink"):
            ident = self._owned.get(kind)
            if ident is None: continue
            # Preserve the master while a remap is unresolved.
            if kind == "sink" and "remap" in self._owned: break
            try:
                row = self._module_row(self._list("modules"), ident)
                if row is None:
                    self._owned.pop(kind);continue
                if not self._matches_module(row, kind):
                    errors.append("virtual_input_cleanup_ownership_mismatch");continue
                try: self.runner(("pactl", "unload-module", str(ident)))
                except Exception: errors.append("virtual_input_unload_failed")
                after = self._module_row(self._list("modules"), ident)
                if after is None:
                    self._owned.pop(kind)
                else: errors.append("virtual_input_module_still_present")
            except Exception as error:
                errors.append(error.code if isinstance(error, VirtualInputError) else "virtual_input_cleanup_probe_failed")
        names_gone = False
        if not self._owned:
            try:
                wanted = {self.metadata.sink_name, self.metadata.sink_name + ".monitor", self.metadata.source_name}
                names_gone = not any(row.get("name") in wanted for kind in ("sinks", "sources") for row in self._list(kind))
                if not names_gone: errors.append("virtual_input_owned_name_still_present")
            except Exception: errors.append("virtual_input_cleanup_probe_failed")
        return names_gone, errors

    def _close_unstarted_process(self):
        errors = []
        process = self._process
        if process is None:return True, errors
        try:process.stdin.close()
        except ValueError:pass
        except Exception:errors.append("virtual_input_stdin_close_failed")
        try:
            if process.poll() is None:process.terminate()
            process.wait(timeout=0.5)
        except Exception:
            try:
                if process.poll() is None:process.kill()
                process.wait(timeout=0.5)
            except Exception:errors.append("virtual_input_process_stop_failed")
        try:closed = process.poll() is not None and process.stdin.closed
        except Exception:closed = False
        if not closed and not errors:errors.append("virtual_input_process_stop_failed")
        return closed, errors

    def end(self, *, generation, reason="session_end"):
        with self._control_lock:
            if type(generation) is not int or self.metadata is None or generation != self.metadata.generation:
                raise VirtualInputError("virtual_input_generation_mismatch")
            if self.closed: return dict(self._last_result)
            self._accepting = False
            errors = []
            if self.writer is not None:
                stopped = self.writer.close(flush=False)
                self._last_stats = self.writer.stats()
                self.input_closed = stopped["input_closed"]
                errors.extend(stopped["errors"])
                if self.input_closed: self.writer = None;self._process = None
            elif self._process is not None:
                self.input_closed, process_errors = self._close_unstarted_process()
                errors.extend(process_errors)
                if self.input_closed:self._process = None
            complete = False
            if self.input_closed:
                complete, cleanup_errors = self._cleanup_modules();errors.extend(cleanup_errors)
            self.closed = self.input_closed and complete
            result = {**self.metadata.as_dict(), "state": "closed" if self.closed else "cleanup_pending",
                      "input_closed": self.input_closed, "cleanup_complete": self.closed,
                      "remaining_module_ids": list(self._owned.values()), "errors": sorted(set(errors)),
                      "writer_failure": self._last_stats.get("writer_failure"),
                      "reason": reason if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_:-]{1,64}", reason) else "session_end"}
            self._last_result = result
            return dict(result)

    def stats(self):
        stats = self.writer.stats() if self.writer is not None else dict(self._last_stats)
        return {**stats, "active": self._accepting and not stats.get("writer_failed", False),
                "input_closed": self.input_closed, "cleanup_pending": not self.closed and (not self._accepting or stats.get("writer_failed", False)),
                "remaining_module_ids": list(self._owned.values())}
