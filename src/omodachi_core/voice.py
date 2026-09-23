"""Voice uplink into the host's own Voxtype, over its documented contract.

Voxtype has no way to be handed audio: it captures with `cpal` from one named
PulseAudio/PipeWire source and exposes no "type this text" entry point
(`docs/INTEGRATIONS.md` is its contract; everything under
`$XDG_RUNTIME_DIR/voxtype/` except the audio socket is an implementation
detail). So the shape of this module follows from that: core owns a virtual
source named `omodachi_mic`, points Voxtype's `[audio] device` at it only while
the user has voice uplink switched on, drives `voxtype record start|stop`, and
puts the transcript back on the wire.

Two rules the upstream contract states outright and this module keeps:

- "Voxtype does not modify your config; please do not modify Voxtype's."  The
  one line core changes is `[audio] device`, it is changed only inside a
  dictation session, the original bytes are saved first, and stopping restores
  those exact bytes. Nothing else in the file is parsed, reformatted or moved.
- `execArgv`-style host execution is not invented here. Typing a transcript into
  the focused window uses `wtype`, or `wl-copy` plus a synthetic paste chord,
  which is what Omarchy itself installs for Voxtype.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time

SOURCE_NAME = "omodachi_mic"
SERVICE = "voxtype.service"
CONFIG_PATH = Path.home() / ".config/voxtype/config.toml"
BACKUP_SUFFIX = ".omodachi-dictation-bak"
INSTALL_COMMAND = ("omarchy-voxtype-install",)
PRESENT_COMMAND = ("/usr/share/omarchy/bin/omarchy-cmd-present", "voxtype")
STATES = ("idle", "recording", "transcribing")
# `AudioFrame { seq: u32, min: f32, max: f32, peak_dbfs: f32 }`, written at
# 100 Hz to a write-only fan-out socket while a recording is in flight.
LEVEL_FRAME = struct.Struct("<Ifff")
LEVEL_INTERVAL = 0.05  # 20 Hz on the wire; the source is 100 Hz.
MAX_TRANSCRIPT_BYTES = 64 * 1024

_AUDIO_SECTION = re.compile(rb"^[ \t]*\[audio\][ \t]*\r?$", re.M)
_NEXT_SECTION = re.compile(rb"^[ \t]*\[", re.M)
_DEVICE_LINE = re.compile(rb"^([ \t]*device[ \t]*=[ \t]*)\"(?P<value>[^\"\n]*)\"", re.M)
_DEVICE_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_TERMINAL_TAG = re.compile(r"\bterminal\b")
_VOXTYPE_STREAM = re.compile(r"voxtype", re.I)


class VoiceError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def _run(argv, *, timeout=10.0, env=None, check=True):
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=timeout, env=env, text=True, shell=False)
    except (OSError, subprocess.SubprocessError):
        raise VoiceError("voxtype_command_failed", 503) from None
    if check and result.returncode:
        raise VoiceError("voxtype_command_failed", 503)
    return result


def audio_device(document: bytes) -> str | None:
    """Read `[audio] device` without parsing, reformatting or rewriting TOML."""
    section = _AUDIO_SECTION.search(document)
    if section is None:
        return None
    tail = document[section.end():]
    following = _NEXT_SECTION.search(tail)
    body = tail[:following.start()] if following else tail
    match = _DEVICE_LINE.search(body)
    return match.group("value").decode("utf-8", "replace") if match else None


def set_audio_device(document: bytes, device: str) -> bytes:
    """Replace exactly one line's quoted value and leave every other byte alone."""
    if not _DEVICE_NAME.fullmatch(device):
        raise VoiceError("voice_device_invalid", 400)
    section = _AUDIO_SECTION.search(document)
    if section is None:
        raise VoiceError("voxtype_config_unsupported", 409)
    tail = document[section.end():]
    following = _NEXT_SECTION.search(tail)
    limit = following.start() if following else len(tail)
    match = _DEVICE_LINE.search(tail[:limit])
    if match is None:
        raise VoiceError("voxtype_config_unsupported", 409)
    start = section.end() + match.start()
    end = section.end() + match.end()
    return document[:start] + match.group(1) + b'"' + device.encode() + b'"' + document[end:]


class VoxtypeHost:
    """The whole host surface this module touches, so tests can replace it."""

    def __init__(self, *, runner=_run, config_path=None, runtime_dir=None,
                 service=SERVICE, which=shutil.which, cache_dir=None):
        self.runner = runner
        self.config_path = Path(config_path) if config_path is not None else CONFIG_PATH
        self.runtime_dir = Path(runtime_dir) if runtime_dir is not None else Path(
            os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "voxtype"
        self.service = service
        self.which = which
        self.cache_dir = Path(cache_dir) if cache_dir is not None else Path.home() / ".cache/omodachi/voice"

    # --- probing -------------------------------------------------------------
    def installed(self) -> bool:
        if self.which("voxtype") is None:
            return False
        present = Path(PRESENT_COMMAND[0])
        if not present.exists():
            return True
        return self.runner(PRESENT_COMMAND, timeout=5, check=False).returncode == 0

    def supports_wait(self) -> bool:
        result = self.runner(("voxtype", "record", "stop", "--help"), timeout=5, check=False)
        return result.returncode == 0 and "--wait" in (result.stdout or "")

    def devices(self) -> list[str]:
        """`voxtype info devices`, the official answer to "what can it capture".

        This matters more than it looks. Voxtype selects its device through
        `cpal`, and on a PipeWire host cpal's ALSA backend advertises `default`
        and `pipewire` and nothing else - no PulseAudio source names at all. On
        such a host `[audio] device = "omodachi_mic"` cannot work, and writing
        it would only break capture, so this list decides which route dictation
        takes.
        """
        result = self.runner(("voxtype", "info", "devices"), timeout=10, check=False)
        rows = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line or line.startswith(("Audio input", "Select one")):
                continue
            name = line.split(" (", 1)[0].strip()
            if _DEVICE_NAME.fullmatch(name):
                rows.append(name)
        return rows

    def source_outputs(self) -> list[dict]:
        result = self.runner(("pactl", "-f", "json", "list", "source-outputs"), timeout=10, check=False)
        try:
            rows = json.loads(result.stdout or "[]")
        except ValueError:
            return []
        return [row for row in rows if isinstance(row, dict)]

    def default_source(self) -> str | None:
        result = self.runner(("pactl", "get-default-source"), timeout=10, check=False)
        name = (result.stdout or "").strip()
        return name if result.returncode == 0 and _DEVICE_NAME.fullmatch(name) else None

    def set_default_source(self, source_name: str) -> None:
        if not _DEVICE_NAME.fullmatch(source_name):
            raise VoiceError("voice_device_invalid", 400)
        self.runner(("pactl", "set-default-source", source_name), timeout=10)

    def route_capture(self, source_name: str, *, attempts=60, delay=0.05) -> dict:
        """Move Voxtype's own capture stream onto our source, and nothing else.

        This is per-stream and dies with the recording, so it leaves no global
        default device changed behind and never touches Voxtype's config.
        """
        if not _DEVICE_NAME.fullmatch(source_name):
            raise VoiceError("voice_device_invalid", 400)
        for _ in range(attempts):
            for row in self.source_outputs():
                properties = row.get("properties") or {}
                signature = " ".join(str(properties.get(key, "")) for key in
                                     ("application.name", "application.process.binary",
                                      "application.process.id", "media.name"))
                index = row.get("index")
                if _VOXTYPE_STREAM.search(signature) and type(index) is int:
                    self.runner(("pactl", "move-source-output", str(index), source_name), timeout=10)
                    return {"moved": True, "index": index}
            time.sleep(delay)
        return {"moved": False, "index": None}

    def state(self) -> str | None:
        try:
            value = (self.runtime_dir / "state").read_text().strip()
        except OSError:
            return None
        return value if value in STATES else None

    def service_active(self) -> bool:
        return self.runner(("systemctl", "--user", "is-active", self.service),
                           timeout=5, check=False).returncode == 0

    def capabilities(self) -> dict:
        installed = self.installed()
        if not installed:
            return {"installed": False, "supported": False, "wait_supported": False,
                    "service_active": False, "state": None, "device": None,
                    "source_name": SOURCE_NAME, "devices": [], "names_sources": False,
                    "levels": False, "install_command": list(INSTALL_COMMAND),
                    "reason": "voxtype_not_installed"}
        wait = self.supports_wait()
        device = None
        try:
            device = audio_device(self.config_path.read_bytes())
        except OSError:
            device = None
        devices = self.devices()
        return {"installed": True, "supported": wait, "wait_supported": wait,
                "service_active": self.service_active(), "state": self.state(),
                "device": device, "source_name": SOURCE_NAME,
                "devices": devices, "names_sources": SOURCE_NAME in devices,
                "levels": (self.runtime_dir / "audio.sock").exists(),
                "install_command": None,
                "reason": None if wait else "voxtype_wait_unsupported"}

    # --- the one configuration line ------------------------------------------
    @property
    def backup_path(self) -> Path:
        return self.config_path.with_name(self.config_path.name + BACKUP_SUFFIX)

    def point_at(self, device: str) -> dict:
        """Save the original bytes, then change one value in place."""
        try:
            original = self.config_path.read_bytes()
        except OSError:
            raise VoiceError("voxtype_config_unreadable", 503) from None
        previous = audio_device(original)
        updated = set_audio_device(original, device)
        if self.backup_path.exists():
            raise VoiceError("voice_dictation_already_active", 409)
        self._atomic(self.backup_path, original)
        if updated != original:
            self._atomic(self.config_path, updated)
        return {"previous_device": previous, "device": device, "changed": updated != original}

    def restore(self) -> dict:
        """Put the saved bytes back, exactly. A failure keeps the backup."""
        try:
            original = self.backup_path.read_bytes()
        except OSError:
            return {"restored": False, "reason": "no_backup"}
        current = self.config_path.read_bytes() if self.config_path.exists() else None
        if current != original:
            self._atomic(self.config_path, original)
        if self.config_path.read_bytes() != original:
            raise VoiceError("voxtype_config_restore_failed", 503)
        self.backup_path.unlink()
        return {"restored": True, "changed": current != original}

    def _atomic(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = 0o600
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            pass
        handle, name = tempfile.mkstemp(prefix=path.name + "-", dir=path.parent)
        try:
            with os.fdopen(handle, "wb") as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            if Path(name).exists():
                os.unlink(name)

    def restart(self) -> None:
        self.runner(("systemctl", "--user", "restart", self.service), timeout=30)

    # --- recording -----------------------------------------------------------
    def transcript_path(self) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.cache_dir, 0o700)
        return self.cache_dir / ("transcript-" + os.urandom(8).hex() + ".txt")

    def record_start(self, transcript: Path) -> None:
        self.runner(("voxtype", "record", "start", "--file=" + str(transcript), "--no-osd",
                     "--no-auto-submit", "--no-smart-auto-submit"), timeout=15)

    def record_stop(self, transcript: Path, *, timeout_seconds=120) -> dict:
        result = self.runner(("voxtype", "record", "stop", "--wait", "--json",
                              "--wait-file", str(transcript), "--timeout", str(int(timeout_seconds))),
                             timeout=timeout_seconds + 15, check=False)
        # Documented exits: 0 transcribed, 3 nothing to transcribe, 4 timed out.
        if result.returncode not in (0, 3, 4):
            raise VoiceError("voxtype_transcription_failed", 503)
        try:
            value = json.loads((result.stdout or "").strip() or "{}")
        except ValueError:
            raise VoiceError("voxtype_transcription_failed", 503) from None
        text = value.get("text")
        text = text if isinstance(text, str) else ""
        if len(text.encode()) > MAX_TRANSCRIPT_BYTES:
            text = text.encode()[:MAX_TRANSCRIPT_BYTES].decode("utf-8", "ignore")
        status = value.get("status")
        return {"status": status if isinstance(status, str) else "unknown",
                "text": text, "chars": len(text), "exit_code": result.returncode}

    def record_cancel(self) -> None:
        self.runner(("voxtype", "record", "cancel"), timeout=10, check=False)


class HostTyping:
    """Put a transcript into the focused host window, the way Omarchy does.

    A terminal takes SHIFT+Insert, everything else CTRL+V; which one applies is
    read from Hyprland's own `activewindow` tags. Nothing here executes text.
    """

    def __init__(self, *, runner=None, environment=None, hyprctl=None):
        self.runner = runner or self._run
        if environment is None:
            from .graphical import graphical_environment
            environment = graphical_environment
        self.environment = environment
        self._hyprctl = hyprctl

    @staticmethod
    def _run(argv, *, env=None, stdin=None, timeout=10.0):
        try:
            return subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout,
                                  env=env, text=True, shell=False)
        except (OSError, subprocess.SubprocessError):
            raise VoiceError("voice_typing_failed", 503) from None

    def _active_is_terminal(self, env) -> bool:
        if self._hyprctl is not None:
            document = self._hyprctl()
        else:
            result = self.runner(("hyprctl", "activewindow", "-j"), env=env, timeout=5)
            if result.returncode:
                return False
            document = result.stdout
        try:
            value = json.loads(document or "{}")
        except ValueError:
            return False
        tags = value.get("tags") or []
        joined = " ".join(item for item in tags if isinstance(item, str))
        return bool(_TERMINAL_TAG.search(joined))

    def type(self, text: str) -> dict:
        if not isinstance(text, str) or not text:
            raise VoiceError("voice_transcript_empty", 409)
        env = dict(self.environment() or {})
        if not env:
            raise VoiceError("voice_host_session_unavailable", 503)
        if shutil.which("wtype"):
            result = self.runner(("wtype", "--", text), env=env, timeout=20)
            if not result.returncode:
                return {"delivered": "wtype", "chars": len(text)}
        if not shutil.which("wl-copy"):
            raise VoiceError("voice_typing_unavailable", 503)
        # Clipboard plus a synthetic paste is what survives an IME; the chord
        # depends on whether the focused window is a terminal.
        self.runner(("wl-copy", "--", text), env=env, timeout=10)
        terminal = self._active_is_terminal(env)
        mods, key = ("SHIFT", "Insert") if terminal else ("CTRL", "V")
        for state in ("down", "up"):
            self.runner(("hyprctl", "dispatch",
                         "hl.dsp.send_key_state({ mods=\"%s\", key=\"%s\", state=\"%s\" })" % (mods, key, state)),
                        env=env, timeout=5)
            if state == "down":
                time.sleep(0.05)
        return {"delivered": "clipboard", "chars": len(text), "chord": mods + "+" + key}


def read_levels(socket_path, *, deadline=None, interval=LEVEL_INTERVAL, connect_timeout=2.0,
                reconnect=1.0):
    """Yield throttled `{peak, rms, vad}` rows from Voxtype's fan-out socket.

    The socket is write-only on Voxtype's side: this reads 16-byte frames and
    never writes. `rms` is not in the frame, so it is the symmetric amplitude
    the frame does carry; `vad` is the daemon's own speech threshold (-20 dBFS,
    `external_trigger_speech_threshold_dbfs`).

    Restarting Voxtype recreates the socket, and starting a dictation may do
    exactly that, so a dropped connection is retried rather than ending the
    stream - the same thing `voxtype-audio-bridge --reconnect-secs` does.
    """
    first = True
    while deadline is None or time.monotonic() < deadline:
        try:
            yield from _read_level_frames(socket_path, deadline=deadline, interval=interval,
                                          connect_timeout=connect_timeout)
        except VoiceError:
            if first:
                raise
        first = False
        if reconnect <= 0:
            return
        time.sleep(reconnect)


def _read_level_frames(socket_path, *, deadline, interval, connect_timeout):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(connect_timeout)
    try:
        client.connect(str(socket_path))
    except OSError:
        client.close()
        raise VoiceError("voice_levels_unavailable", 503) from None
    buffer = bytearray()
    emitted = 0.0
    try:
        while deadline is None or time.monotonic() < deadline:
            try:
                chunk = client.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            buffer.extend(chunk)
            frames = len(buffer) // LEVEL_FRAME.size
            if not frames:
                continue
            # Only the newest frame matters at 20 Hz; the rest is backlog.
            offset = (frames - 1) * LEVEL_FRAME.size
            seq, low, high, peak_dbfs = LEVEL_FRAME.unpack_from(buffer, offset)
            del buffer[:frames * LEVEL_FRAME.size]
            now = time.monotonic()
            if now - emitted < interval:
                continue
            emitted = now
            amplitude = max(abs(low), abs(high))
            yield {"seq": seq, "peak": round(float(peak_dbfs), 2),
                   "rms": round(float(amplitude), 4), "vad": bool(peak_dbfs > -20.0)}
    finally:
        client.close()
