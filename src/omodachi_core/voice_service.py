"""The voice surface: one uplink, one dictation session, one level stream.

The microphone uplink here is deliberately independent of Remote. Scenario 2 is
"I am away from the machine and talking to the agent"; there is no stream in
that picture, so requiring a Remote session to speak would be the wrong shape.
The PCM format, the frame size and the backpressure rules are the ones
`docs/audio-uplink-contract.md` already describes, because it is the same
virtual microphone underneath.

A dictation session is the only thing that touches `~/.config/voxtype/
config.toml`, it does so only when the user has voice uplink switched on in
settings, and it puts the original bytes back when it stops - including when it
stops because the daemon is shutting down.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import time

from .audio_uplink import AudioUplinkError, FORMAT, MicrophoneArbiter, SOURCE_NAME
from .audio_virtual_input import VirtualInputError, FRAME_BYTES
from .voice import VoiceError, VoxtypeHost, HostTyping, read_levels, LEVEL_INTERVAL

TARGETS = ("client", "host", "both")
MAX_DICTATION_SECONDS = 300


@dataclass
class _Uplink:
    device: str
    channel_id: str
    generation: int
    backend: object
    ending: bool = False


class VoiceService:
    def __init__(self, hub, *, factory=None, host=None, typing=None, arbiter=None,
                 preferences=None, levels_reader=read_levels, clock=time.monotonic):
        self.hub = hub
        self.factory = factory
        self.host = host if host is not None else VoxtypeHost()
        self._typing = typing
        self.arbiter = arbiter or MicrophoneArbiter()
        self.preferences = preferences
        self.levels_reader = levels_reader
        self.clock = clock
        self.active: _Uplink | None = None
        self.dictation: dict | None = None
        self._lock = asyncio.Lock()
        self._generations: dict[str, int] = {}

    # --- settings ------------------------------------------------------------
    def enabled(self) -> bool:
        if self.preferences is None:
            return False
        try:
            return bool(self.preferences.get()["values"].get("voice_uplink"))
        except Exception:
            return False

    @property
    def typing(self):
        if self._typing is None:
            self._typing = HostTyping()
        return self._typing

    # --- capabilities --------------------------------------------------------
    async def capabilities(self, device=None):
        voxtype = await asyncio.to_thread(self.host.capabilities)
        holder = self.arbiter.holder
        uplink_available = self.factory is not None and holder in (None, "voice")
        active = self.active is not None and self.active.device == device and not self.active.ending
        return {"voice_uplink_enabled": self.enabled(),
                "uplink": {"supported": self.factory is not None, "available": uplink_available,
                           "active": active, "transport": "websocket",
                           "endpoint": "/v1/voice/uplink", "source_name": SOURCE_NAME, **FORMAT,
                           "reason": None if uplink_available else
                                     ("audio_backend_not_installed" if self.factory is None else "audio_input_busy")},
                "dictation": {"available": bool(voxtype["supported"]) and self.enabled(),
                              "active": self.dictation is not None,
                              "targets": list(TARGETS),
                              "route": "config" if voxtype.get("names_sources") else "default_source",
                              "reason": None if self.enabled() else "voice_uplink_disabled"},
                "levels": {"available": bool(voxtype["levels"]), "hz": round(1 / LEVEL_INTERVAL)},
                "voxtype": voxtype}

    # --- uplink --------------------------------------------------------------
    async def begin(self, device, channel_id, generation):
        async with self._lock:
            if self.factory is None:
                raise AudioUplinkError("audio_input_transport_unavailable", 503)
            if type(generation) is not int or not 0 < generation < 2 ** 53:
                raise AudioUplinkError("audio_input_generation_invalid", 400)
            if generation <= self._generations.get(device, 0):
                raise AudioUplinkError("audio_input_generation_stale", 409)
            if self.active is not None:
                raise AudioUplinkError("audio_input_already_active", 409)
            self.arbiter.claim("voice")
            try:
                backend = self.factory()
                await asyncio.to_thread(backend.begin, generation=generation)
            except Exception:
                self.arbiter.release("voice")
                raise AudioUplinkError("audio_input_begin_failed", 503) from None
            self._generations[device] = generation
            self.active = _Uplink(device, channel_id, generation, backend)
            return {"generation": generation, "source_name": SOURCE_NAME, **FORMAT, "state": "active"}

    def accept(self, device, channel_id, generation, frame):
        session = self.active
        if session is None or session.ending or session.channel_id != channel_id:
            raise AudioUplinkError("audio_input_not_active", 409)
        if (session.device, session.generation) != (device, generation):
            raise AudioUplinkError("audio_input_generation_mismatch", 409)
        if not isinstance(frame, bytes) or len(frame) != FRAME_BYTES:
            raise AudioUplinkError("audio_input_frame_invalid", 400)
        try:
            accepted = session.backend.accept(frame)
        except VirtualInputError:
            raise AudioUplinkError("audio_input_not_active", 409) from None
        return {"accepted": accepted is True, "generation": generation}

    async def end(self, channel_id, reason="session_end"):
        async with self._lock:
            session = self.active
            if session is None or session.channel_id != channel_id:
                return {"state": "closed", "cleanup_complete": True}
            session.ending = True
            try:
                result = await asyncio.to_thread(session.backend.end,
                                                 generation=session.generation, reason=reason)
            except Exception:
                raise AudioUplinkError("audio_input_cleanup_pending", 503) from None
            if result.get("cleanup_complete") is False or result.get("state") not in {"closed", "ended"}:
                raise AudioUplinkError("audio_input_cleanup_pending", 503)
            self.active = None
            self.arbiter.release("voice")
            return result

    # --- dictation -----------------------------------------------------------
    async def dictation_start(self, device, payload):
        target = payload.get("target", "client")
        if target not in TARGETS:
            raise VoiceError("invalid_request", 400)
        async with self._lock:
            if not self.enabled():
                raise VoiceError("voice_uplink_disabled", 409)
            if self.dictation is not None:
                raise VoiceError("voice_dictation_already_active", 409)
            capabilities = await asyncio.to_thread(self.host.capabilities)
            if not capabilities["installed"]:
                raise VoiceError("voxtype_not_installed", 503)
            if not capabilities["wait_supported"]:
                raise VoiceError("voxtype_wait_unsupported", 503)
            if self.active is None or self.active.ending:
                # Pointing Voxtype at a source that does not exist would only
                # restart it into a broken capture; the uplink comes first.
                raise VoiceError("voice_uplink_required", 409)
            # Two routes, and the host decides which one is honest. Voxtype
            # picks its device through cpal, whose ALSA backend on a PipeWire
            # machine advertises `default` and `pipewire` and no source names
            # at all - there, naming our source in config.toml only breaks
            # capture, so the capture stream is moved instead and the file is
            # never touched.
            selectable = SOURCE_NAME in await asyncio.to_thread(self.host.devices)
            transcript = self.host.transcript_path()
            pointed = await asyncio.to_thread(self.host.point_at, SOURCE_NAME) if selectable else None
            previous_default = None if selectable else await asyncio.to_thread(self.host.default_source)
            self.dictation = {"device": device, "target": target, "transcript": transcript,
                              "started_at": self.clock(),
                              "route": "config" if selectable else "default_source",
                              "previous_device": pointed["previous_device"] if pointed else previous_default,
                              "previous_default_source": previous_default,
                              "config_changed": bool(pointed and pointed["changed"])}
            try:
                if pointed is not None:
                    await asyncio.to_thread(self.host.restart)
                    await self._await_state({"idle", "recording"})
                else:
                    # Voxtype resolves `default` when it opens the stream, so
                    # the default source has to be ours before `record start`.
                    await asyncio.to_thread(self.host.set_default_source, SOURCE_NAME)
                await asyncio.to_thread(self.host.record_start, transcript)
                if pointed is None:
                    # Belt and braces for a stream that opened a moment early.
                    await asyncio.to_thread(self.host.route_capture, SOURCE_NAME)
            except BaseException:
                await self._restore()
                raise
            return {"state": "recording", "target": target, "source_name": SOURCE_NAME,
                    "route": self.dictation["route"],
                    "previous_device": self.dictation["previous_device"],
                    "config_changed": self.dictation["config_changed"]}

    async def dictation_stop(self, device, payload):
        async with self._lock:
            session = self.dictation
            if session is None:
                raise VoiceError("voice_dictation_not_active", 409)
            if session["device"] != device:
                raise VoiceError("permission_denied", 403)
            target = payload.get("target", session["target"])
            if target not in TARGETS:
                raise VoiceError("invalid_request", 400)
            try:
                result = await asyncio.to_thread(self.host.record_stop, session["transcript"])
            finally:
                await self._restore()
            text = result["text"]
            delivery = None
            if text and target in ("host", "both"):
                delivery = await asyncio.to_thread(self.typing.type, text)
            payload_out = {"text": text if target != "host" else "", "chars": result["chars"],
                           "status": result["status"], "target": target,
                           "delivered_to_host": delivery}
            if self.hub is not None:
                # The transcript is a device event, not a broadcast: it is what
                # this device just said.
                self.hub.publish("voice.transcript", {"text": payload_out["text"],
                                                      "chars": payload_out["chars"],
                                                      "status": payload_out["status"]},
                                 device_id=device)
            return payload_out

    async def _await_state(self, wanted, timeout=10.0):
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            if await asyncio.to_thread(self.host.state) in wanted:
                return True
            await asyncio.sleep(0.1)
        return False

    async def _restore(self):
        session = self.dictation
        if session is None:
            return
        self.dictation = None
        try:
            await asyncio.to_thread(self.host.record_cancel)
            if session.get("route") == "config":
                restored = await asyncio.to_thread(self.host.restore)
                if restored.get("changed"):
                    await asyncio.to_thread(self.host.restart)
            elif session.get("previous_default_source"):
                await asyncio.to_thread(self.host.set_default_source, session["previous_default_source"])
        finally:
            for name in (session["transcript"], session["transcript"].with_name(session["transcript"].name + ".done")):
                try:
                    os.unlink(name)
                except OSError:
                    pass

    # --- levels --------------------------------------------------------------
    def level_frames(self, *, deadline=None):
        return self.levels_reader(self.host.runtime_dir / "audio.sock", deadline=deadline)

    async def close(self):
        await self._restore()
        session = self.active
        if session is not None:
            try:
                await self.end(session.channel_id, "transport_closed")
            except AudioUplinkError:
                pass
