"""Existing-lease microphone channels; no pairing or new network identity.

The authenticated WebSocket adapter alone creates a server-owned channel handle.
An explicit begin creates the virtual input; capability probes and idle channels
never create sinks. Audio lifecycle is independent of adaptive display resources.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
import re
import os
from pathlib import Path
import stat
import shutil
import subprocess
import sys
import threading
import time

from .audio_virtual_input import VirtualMicrophoneSession, VirtualInputError, FRAME_BYTES, MAX_FRAMES

FORMAT = {"format": "s16le", "rate": 48000, "channels": 1, "frame_samples": 960,
          "frame_bytes": FRAME_BYTES, "max_queued_frames": MAX_FRAMES}
# Voxtype finds its capture device by name, so the virtual source has one.
SOURCE_NAME = "omodachi_mic"
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


class MicrophoneArbiter:
    """One virtual microphone exists on the host, so one holder may own it.

    The Remote session uplink and the standalone voice uplink publish the same
    fixed source name; whichever asks second is told the microphone is busy
    rather than fighting over the same PulseAudio modules.
    """
    def __init__(self):
        self._holder = None
        self._lock = threading.Lock()

    @property
    def holder(self):
        return self._holder

    def claim(self, owner):
        with self._lock:
            if self._holder is not None and self._holder != owner:
                raise AudioUplinkError("audio_input_busy", 409)
            self._holder = owner

    def release(self, owner):
        with self._lock:
            if self._holder == owner:
                self._holder = None

class AudioUplinkError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)

def generation_value(value):
    if type(value) is not int or not 0 < value < 2**53:
        raise AudioUplinkError("audio_input_generation_invalid", 400)
    return value

@dataclass(frozen=True)
class AuthorizedAudioUplink:
    channel_id: str
    generation: int
    authorized: bool = True
    def __post_init__(self):
        if not isinstance(self.channel_id, str) or not _ID.fullmatch(self.channel_id):
            raise AudioUplinkError("audio_uplink_channel_invalid", 400)
        generation_value(self.generation)
        if type(self.authorized) is not bool:
            raise AudioUplinkError("audio_uplink_authorization_invalid", 400)

class PulseVirtualInputFactory:
    """Read-only server reachability; never parse routes or create a device."""
    def available(self):
        if not sys.platform.startswith("linux") or not shutil.which("pactl") or not shutil.which("pacat"):
            return False
        try:
            endpoint = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "pulse/native"
            info = endpoint.stat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid(): return False
            # Connect to an already existing user server explicitly; capability
            # reads neither spawn an audio server nor enumerate device routes.
            # Discard info output and retain only its exit status.
            return subprocess.run(("pactl", "--server=unix:" + str(endpoint), "info"), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    def __call__(self):
        return VirtualMicrophoneSession(stable_name=SOURCE_NAME)

@dataclass(frozen=True)
class _SessionBinding:
    lease_id: str
    owner_device_id: str
    epoch: int = 1

@dataclass
class _AudioSession:
    device: str
    lease_id: str
    epoch: int
    uplink: AuthorizedAudioUplink
    authorize: object
    backend: object
    ending: bool = False

class AudioUplinkManager:
    def __init__(self, hub, factory=None, *, arbiter=None):
        self.hub, self.factory = hub, factory
        self.arbiter = arbiter or MicrophoneArbiter()
        self.active = None
        self._attached = {}  # Only one current lease may reserve a channel.
        self._last_generation = {}
        self._lock = asyncio.Lock()
        self._jobs = set()
        self._available = False
        self._probe_time = 0.0
        self._closed = False

    def _lease(self, device, lease_id, authorize=None):
        if authorize is not None:
            try:
                if authorize() is False: raise ValueError()
            except Exception:
                raise AudioUplinkError("permission_denied", 401) from None
        return self._binding_for(lease_id, device)

    def _binding_for(self, lease_id, device):
        """The uplink rides the one live Remote session, never its own identity."""
        remote = getattr(self.hub, "remote", None)
        binding = remote.session_binding() if remote is not None else None
        if binding is None or binding[0] != lease_id:
            raise AudioUplinkError("stale_session", 409)
        if binding[1] != device:
            raise AudioUplinkError("permission_denied", 403)
        return _SessionBinding(binding[0], binding[1])

    async def _job(self, callback):
        async def run():
            async with self._lock: return await callback()
        task = asyncio.create_task(run()); self._jobs.add(task)
        def done(job):
            self._jobs.discard(job)
            if not job.cancelled(): job.exception()
        task.add_done_callback(done)
        return await asyncio.shield(task)

    async def refresh_availability(self):
        if self.factory is None or self._closed:
            self._available = False
        elif time.monotonic() - self._probe_time >= 1:
            probe = getattr(self.factory, "available", None)
            try: self._available = callable(probe) and await asyncio.to_thread(probe) is True
            except Exception: self._available = False
            self._probe_time = time.monotonic()
        return self._available

    def capabilities(self, device=None):
        available = (self.factory is not None and self._available and not self._closed
                     and self.arbiter.holder in (None, "remote"))
        active = self.active is not None and self.active.device == device and not self.active.ending
        pending = self.active is not None and self.active.ending
        return {"supported": available, "available": available and not pending, "active": active,
            "transport": "websocket", "endpoint": "/v1/remote/sessions/{session_id}/audio", **FORMAT,
            "reason": "audio_input_cleanup_pending" if pending else "ready" if available
                      else "audio_backend_not_installed" if self.factory is None else "audio_backend_unavailable"}

    def attach(self, device, lease_id, uplink, authorize=None):
        lease = self._lease(device, lease_id, authorize)
        if not isinstance(uplink, AuthorizedAudioUplink) or uplink.authorized is not True:
            raise AudioUplinkError("audio_input_permission_required", 403)
        key = (lease_id, lease.epoch)
        if uplink.generation <= self._last_generation.get(key, 0):
            raise AudioUplinkError("audio_input_generation_stale", 409)
        if self.active is not None or self._attached:
            raise AudioUplinkError("audio_input_busy", 409)
        self._attached[uplink.channel_id] = (device, lease_id, lease.epoch, uplink)
        return {"channel_id": uplink.channel_id, "generation": uplink.generation}

    def _binding(self, device, lease_id, generation, channel_id=None):
        generation_value(generation)
        matches = [item for cid, item in self._attached.items() if channel_id is None or cid == channel_id]
        for item in matches:
            owner, ident, epoch, uplink = item
            if owner == device and ident == lease_id and uplink.generation == generation:
                return item
        raise AudioUplinkError("audio_input_generation_mismatch", 409)

    async def begin(self, device, lease_id, generation, authorize, *, channel_id=None):
        async def work():
            if self._closed or not await self.refresh_availability():
                raise AudioUplinkError("audio_input_transport_unavailable", 503)
            lease = self._lease(device, lease_id, authorize)
            owner, ident, epoch, uplink = self._binding(device, lease_id, generation, channel_id)
            if epoch != lease.epoch: raise AudioUplinkError("stale_lease", 409)
            if self.active is not None: raise AudioUplinkError("audio_input_already_active", 409)
            self.arbiter.claim("remote")
            self._last_generation = {(lease_id, epoch): generation}
            try:
                backend = self.factory()
            except Exception:
                self._attached.pop(uplink.channel_id, None)
                raise AudioUplinkError("audio_input_begin_failed", 503) from None
            session = _AudioSession(device, lease_id, epoch, uplink, authorize, backend)
            self.active = session
            try:
                await asyncio.to_thread(backend.begin, generation=generation)
                self._lease(device, lease_id, authorize)
            except Exception:
                session.ending = True
                await self._cleanup(session, "begin_failed")
                raise AudioUplinkError("audio_input_begin_failed", 503) from None
            return {"generation": generation, **FORMAT, "state": "active"}
        return await self._job(work)

    async def accept(self, device, lease_id, generation, frame, authorize, *, channel_id=None):
        async def work():
            lease = self._lease(device, lease_id, authorize)
            self._binding(device, lease_id, generation, channel_id)
            session = self.active
            if session is None or session.ending: raise AudioUplinkError("audio_input_not_active", 409)
            if (session.device, session.lease_id, session.epoch, session.uplink.generation) != (device, lease_id, lease.epoch, generation):
                raise AudioUplinkError("audio_input_generation_mismatch", 409)
            self._lease(device, lease_id, session.authorize)
            if not isinstance(frame, bytes) or len(frame) != FRAME_BYTES:
                raise AudioUplinkError("audio_input_frame_invalid", 400)
            try:
                # accept only enqueues <=3 fixed frames; it never waits for PCM
                # playback or runs subprocess/file I/O on this event loop.
                accepted = session.backend.accept(frame)
            except VirtualInputError:
                # A failed PCM writer must release this owned source/channel,
                # not remain active until a user toggles the microphone twice.
                await self._cleanup(session, "writer_failed")
                raise AudioUplinkError("audio_input_not_active", 409) from None
            return {"accepted": accepted is True, "generation": generation}
        return await self._job(work)

    async def _cleanup(self, session, reason):
        session.ending = True
        if getattr(session.backend, "closed", False) and getattr(session.backend, "metadata", None) is None:
            self._attached.pop(session.uplink.channel_id, None)
            if self.active is session: self.active = None
            self.arbiter.release("remote")
            return {"state": "closed", "cleanup_complete": True}
        try:
            result = await asyncio.to_thread(session.backend.end, generation=session.uplink.generation, reason=reason)
        except Exception:
            # A failed backend cleanup retains its owned IDs/session for retry.
            raise AudioUplinkError("audio_input_cleanup_pending", 503) from None
        if result.get("cleanup_complete") is False or result.get("state") not in {"closed", "ended"}:
            raise AudioUplinkError("audio_input_cleanup_pending", 503)
        self._attached.pop(session.uplink.channel_id, None)
        if self.active is session: self.active = None
        self.arbiter.release("remote")
        return result

    async def end(self, device, lease_id, generation, reason, authorize, *, channel_id=None, cleanup=False):
        async def work():
            # Explicit requests check owner and generation before changing any
            # reference. Cleanup may run after lease expiry, but still matches
            # the server-held channel handle so stale sockets cannot close new ones.
            if not cleanup: self._lease(device, lease_id, authorize)
            self._binding(device, lease_id, generation, channel_id)
            session = self.active
            if session is None:
                for cid, item in tuple(self._attached.items()):
                    if item[0] == device and item[1] == lease_id and item[3].generation == generation:
                        self._attached.pop(cid)
                return {"generation": generation, "state": "closed", "cleanup_complete": True}
            if (session.device, session.lease_id, session.uplink.generation) != (device, lease_id, generation):
                raise AudioUplinkError("audio_input_generation_mismatch", 409)
            return await self._cleanup(session, reason)
        return await self._job(work)

    async def close_channel(self, channel_id, reason="disconnected"):
        async def work():
            attached = self._attached.get(channel_id)
            if attached is None: return
            if self.active is not None and self.active.uplink.channel_id == channel_id:
                await self._cleanup(self.active, reason)
            else: self._attached.pop(channel_id, None)
        return await self._job(work)

    async def close_lease(self, device, lease_id, reason="session_end"):
        async def work():
            session = self.active
            if session is not None and (session.device, session.lease_id) == (device, lease_id):
                await self._cleanup(session, reason)
            for cid, item in tuple(self._attached.items()):
                if item[0] == device and item[1] == lease_id: self._attached.pop(cid)
        return await self._job(work)

    async def maintain(self):
        async def work():
            session = self.active
            if session is not None:
                try:
                    self._lease(session.device, session.lease_id, session.authorize)
                    if session.ending: raise ValueError()
                except Exception:
                    await self._cleanup(session, "session_unavailable")
                else:
                    # Writer failure can occur between frames (or while the
                    # client capture is silent). Poll only the in-memory writer
                    # stats here; no device enumeration or audio capture.
                    stats_reader = getattr(session.backend, "stats", None)
                    stats = stats_reader() if callable(stats_reader) else None
                    if isinstance(stats, dict) and (stats.get("writer_failed") is True
                            or stats.get("active") is False):
                        await self._cleanup(session, "writer_failed")
            remote = getattr(self.hub, "remote", None)
            active = remote.session_binding() if remote is not None else None
            for cid, item in tuple(self._attached.items()):
                if active is None or (item[0], item[1]) != (active[1], active[0]):
                    if self.active is None or self.active.uplink.channel_id != cid: self._attached.pop(cid)
        return await self._job(work)

    async def close(self):
        self._closed = True
        async def work():
            if self.active is not None: await self._cleanup(self.active, "transport_closed")
            self._attached.clear()
        return await self._job(work)
