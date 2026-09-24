"""Async, device-authenticated binding for the single RemoteManager.

Blocking compositor and backend work runs in serialized worker jobs; a
cancelled HTTP response never releases the lock while a host mutation is still
running. The event loop alone publishes hub state and events.
"""
from __future__ import annotations

import asyncio

from ..audio_uplink import AudioUplinkError, AudioUplinkManager
from .errors import RemoteError
from ..protocol import IDLE_REMOTE_BAR as IDLE_BAR


# The compositor's own names for "the display configuration is not what this
# session left it as". `configreloaded` is the one that matters most: the host's
# monitor watcher issues `hyprctl reload`, which re-applies the user's catch-all
# monitor rule to every output, including the session's.
DISPLAY_EVENTS = frozenset({"monitoradded", "monitoraddedv2", "monitorremoved", "monitorremovedv2",
                            "configreloaded", "monitorlayoutchanged"})
# MENU-2. The Omarchy bar is a layer surface, so the compositor announces it
# appearing and disappearing rather than moving: `openlayer`/`closelayer` with
# the namespace as the value. A shell restart, a bar the user hid, a bar that
# followed a new output - all three arrive here, and all three mean the
# published bar geometry is stale.
LAYER_EVENTS = frozenset({"openlayer", "closelayer"})
BAR_LAYER = "omarchy-bar"
# A reconcile still runs on this period when the event stream is unavailable, so
# a session converges even on a host whose `.socket2.sock` cannot be reached.
RECONCILE_INTERVAL = 2.0
# How often a daemon with no Remote manager tries to build one again. Building
# it reads the compositor, so this is not free; the app asks for capabilities
# far more often than this, and a user who has just logged in waits at most
# this long for Remote to come back by itself.
MANAGER_RETRY_INTERVAL = 15.0


class RemoteService:
    def __init__(self, hub, *, manager=None, manager_factory=None, audio_session_factory=None,
                 unavailable_reason="remote_runtime_unavailable"):
        self.hub, self.manager = hub, manager
        self.unavailable_reason = unavailable_reason
        # INSTALL-1 §1.3. The manager is built once at startup and used to stay
        # None for the life of the daemon when that failed. It fails for one
        # ordinary reason - the daemon started before the graphical session,
        # which is exactly what `WantedBy=default.target` guarantees on a fresh
        # install - and on 2026-09-20 that turned a host that had come up
        # perfectly well into one that answered `remote_runtime_unavailable`
        # to every request until somebody restarted it by hand. The factory is
        # retried, on a cooldown, from the same call the app already makes to
        # read capabilities, so "Remote is unavailable" is now a statement
        # about now rather than about boot.
        self._manager_factory = manager_factory
        self.manager_retry_interval = MANAGER_RETRY_INTERVAL
        self._manager_attempt = None
        self.last_manager_error = None
        self.audio = AudioUplinkManager(hub, audio_session_factory)
        self._lock = asyncio.Lock()
        self._jobs: set[asyncio.Task] = set()
        self._watchdog = None
        self._display_watch = None
        # Set by the compositor's event stream; drained by the watchdog so the
        # reconcile runs under the same serialized job lock as everything else.
        self._display_dirty = False
        self._bar_layer_dirty = False
        self._reconciled_at = 0.0
        # Set by CoreService, which owns `state.bar`.
        self.core = None
        self._loop = None
        self._transport_count = 0
        self._closed = False
        self._recovered = False
        self._capabilities = None
        # At most one VNC bridge per session: a second RFB client on the same
        # WayVNC instance would fight the first one for the framebuffer.
        self._vnc_bridges: dict[str, str] = {}
        hub.remote = self

    # --- plumbing ----------------------------------------------------------

    def _require(self):
        if self.manager is None:
            raise RemoteError(self.unavailable_reason, 503)
        if self._closed:
            raise RemoteError("remote_transport_unavailable", 503)
        return self.manager

    async def _job(self, callback):
        async def work():
            async with self._lock:
                return await asyncio.to_thread(callback)
        task = asyncio.create_task(work())
        self._jobs.add(task)
        def finished(job):
            self._jobs.discard(job)
            if not job.cancelled():
                job.exception()  # consume errors whose response was cancelled
        task.add_done_callback(finished)
        return await asyncio.shield(task)

    def _emit(self, session, reason):
        """Called from a worker thread by the manager."""
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._publish, session, reason)

    def _publish(self, session, reason):
        payload = {"id": session["id"], "revision": session["revision"], "state": session["state"]}
        if reason:
            payload["reason"] = reason
        self.hub.publish("remote.session.changed", payload)
        self._sync_state()

    def _sync_state(self):
        projection = self.manager.state_projection() if self.manager is not None else {
            "session_id": None, "state": "offline", "mode": None, "backend": None, "revision": 0}
        # PERF-4 §0: `state_snapshot()` deep-copies the whole state, catalog
        # and all — 588 KB — to compare one key.
        if self.hub.state_view("remote")["remote"] != projection:
            self.hub.update_state({"remote": projection}, event_type="remote.changed")

    def session_binding(self):
        """(session_id, device_id) for the audio uplink, or None."""
        session = self.manager.current() if self.manager is not None else None
        return (session.id, session.device_id) if session is not None else None

    def _owned(self, device, session_id):
        session = self._require().get(session_id)
        if device is not None and session.device_id != device:
            raise RemoteError("permission_denied", 403)
        return session

    # --- transport lifecycle ----------------------------------------------

    async def attach_transport(self):
        self._transport_count += 1
        if self._transport_count != 1:
            return
        self._loop = asyncio.get_running_loop()
        self._closed = False
        self.audio._closed = False
        if self.manager is not None:
            self.manager.events = self._emit
            if not self._recovered:
                self._recovered = True
                try:
                    await self._job(self.manager.recover)
                except RemoteError:
                    pass  # a stuck resource is reported by the next recover call
            await self.refresh_capabilities()
        self._sync_state()
        async def watch():
            while True:
                await asyncio.sleep(0.25)
                if self.manager is not None:
                    try:
                        if await self._job(self.manager.maintain):
                            self._sync_state()
                        # HOST-1: a takeover can kill the Omarchy shell, and the
                        # user is not looking at the screen where that shows.
                        await self._job(self.manager.watch_shell)
                        await self.reconcile_display()
                        await self.refresh_bar()
                        await self.reread_bar_geometry()
                    except RemoteError:
                        pass  # retried on the next tick
                try:
                    await self.audio.maintain()
                except AudioUplinkError:
                    pass
        self._watchdog = asyncio.create_task(watch())
        if self.manager is not None:
            self._display_watch = asyncio.create_task(self._watch_display())

    async def reconcile_display(self):
        """Let the manager adopt whatever the host did to the displays."""
        if self.manager is None or self.manager.current() is None:
            self._display_dirty = False
            return None
        loop = asyncio.get_running_loop()
        if not self._display_dirty and loop.time() - self._reconciled_at < RECONCILE_INTERVAL:
            return None
        self._display_dirty = False
        self._reconciled_at = loop.time()
        reason = await self._job(self.manager.reconcile)
        if reason is not None:
            self._sync_state()
        return reason

    async def reread_bar_geometry(self):
        """Re-read the bar layer when the compositor said it changed.

        The read itself is a subprocess, so it happens on a worker; publishing
        `state.bar` stays on the loop, where every other state write is.
        """
        if not self._bar_layer_dirty:
            return False
        self._bar_layer_dirty = False
        core = self.core
        geometry = getattr(core, "bar_geometry", None)
        refresh = getattr(core, "refresh_bar", None)
        if geometry is None or refresh is None:
            return False
        geometry.mark_dirty()
        await asyncio.to_thread(geometry.layers)
        refresh()
        return True

    async def _watch_display(self):
        """Follow the compositor's event stream for the whole session's life."""
        path = self.manager.hyprland.event_socket()
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(path)
            except (OSError, ValueError):
                await asyncio.sleep(RECONCILE_INTERVAL)
                continue
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break  # the compositor went away; reconnect below
                    name, _, value = line.partition(b">>")
                    event = name.decode("utf-8", "replace")
                    if event in DISPLAY_EVENTS:
                        self._display_dirty = True
                    elif event in LAYER_EVENTS and value.strip().decode("utf-8", "replace") == BAR_LAYER:
                        self._bar_layer_dirty = True
            except OSError:
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
            await asyncio.sleep(0.25)

    async def detach_transport(self):
        if self._transport_count == 0:
            return
        self._transport_count -= 1
        if self._transport_count:
            return
        self._closed = True
        if self._display_watch:
            self._display_watch.cancel()
            await asyncio.gather(self._display_watch, return_exceptions=True)
            self._display_watch = None
        if self._watchdog:
            self._watchdog.cancel()
            await asyncio.gather(self._watchdog, return_exceptions=True)
            self._watchdog = None
        try:
            await self.audio.close()
        finally:
            if self.manager is not None:
                session = self.manager.current()
                if session is not None:
                    await self._job(lambda: self.manager.release(session.id, "daemon_stopping"))

    # --- operations --------------------------------------------------------

    async def ensure_manager(self):
        """Build the Remote manager if there is not one yet. Never raises.

        Rate-limited by `manager_retry_interval`, so a host that genuinely has
        no graphical session pays one compositor probe every 15 s rather than
        one per request. The first success publishes capabilities, which is
        what makes the app's Remote entry come alive without a reconnect.
        """
        if self.manager is not None or self._manager_factory is None or self._closed:
            return self.manager
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._manager_attempt is not None and now - self._manager_attempt < self.manager_retry_interval:
            return None
        self._manager_attempt = now
        try:
            manager = await asyncio.to_thread(self._manager_factory)
        except Exception as error:
            self.last_manager_error = getattr(error, "code", None) or type(error).__name__
            return None
        self.last_manager_error = None
        self.manager = manager
        # REMOTE-SAFE-1 §5 finding. A manager built here - the daemon started
        # before the desktop, which is every boot - was never given the event
        # callback `attach_transport` gives a startup manager, nor its one
        # recovery pass. Its sessions then never reached `state.remote`
        # (it stayed `offline` through a live session) and nothing that reads
        # that field - the plugin's icon, its takeover lock, the corner insets -
        # knew a session was running.
        if self._loop is not None:
            manager.events = self._emit
            # And the compositor event stream (display changes, the bar layer
            # opening or closing), which is also only started for a manager
            # that existed when the transport attached.
            if self._watchdog is not None and self._display_watch is None:
                self._display_watch = asyncio.create_task(self._watch_display())
            if not self._recovered:
                self._recovered = True
                try:
                    await self._job(manager.recover)
                except RemoteError:
                    pass
        self._sync_state()
        return manager

    async def refresh_capabilities(self):
        await self.ensure_manager()
        if self.manager is None:
            self._capabilities = {"backends": {"sunshine": {"available": False, "reason": self.unavailable_reason},
                                               "vnc": {"available": False, "reason": self.unavailable_reason}},
                                  "modes": [], "placement_options": [], "lock_local_input_supported": False,
                                  "bar_occlusion": False,
                                  "encoder_limits": None}
        else:
            self._capabilities = await self._job(self.manager.capabilities)
        available = any(row.get("available") for row in self._capabilities["backends"].values())
        current = self.hub.capabilities_snapshot()
        patch = {"desktop": available, "remote_backends": self._capabilities["backends"]}
        if any(current.get(key) != value for key, value in patch.items()):
            self.hub.update_state({"capabilities": patch}, event_type="remote.capabilities")
        return self._capabilities

    async def capabilities(self):
        return await self.refresh_capabilities()

    async def refresh_bar(self):
        value = dict(IDLE_BAR)
        if self.manager is not None and self.manager.current() is not None:
            try:
                value = await self._job(self.manager.bar_projection)
            except RemoteError:
                value = dict(IDLE_BAR)
        # PERF-4 §0. `watch()` calls this four times a second, and this line
        # used to deep-copy the whole state — the catalog inside it is 594
        # rows — to compare one key against it. A stack sampler on the live
        # daemon found the event loop here in half of every sample where it
        # was doing anything at all.
        if self.hub.state_view("remote_bar")["remote_bar"] != value:
            self.hub.update_state({"remote_bar": value}, event_type="remote_bar.changed")
        return value

    async def create(self, device, payload):
        # The one request a user makes by pressing a button. If the daemon
        # outlived a session it could not see at startup, this is where it
        # stops being true, rather than one capabilities poll later.
        await self.ensure_manager()
        manager = self._require()
        session = await self._job(lambda: manager.create(device, payload))
        await self.refresh_bar()
        return session.to_dict()

    async def get(self, device, session_id):
        return self._owned(device, session_id).to_dict()

    async def resize(self, device, session_id, payload):
        manager = self._require()
        self._owned(device, session_id)
        session = await self._job(lambda: manager.resize(session_id, payload))
        await self.refresh_bar()
        return session.to_dict()

    async def switch_backend(self, device, session_id, payload):
        manager = self._require()
        self._owned(device, session_id)
        session = await self._job(lambda: manager.switch_backend(session_id, payload))
        return session.to_dict()

    async def heartbeat(self, device, session_id):
        manager = self._require()
        self._owned(device, session_id)
        return manager.heartbeat(session_id)

    async def presented(self, device, session_id, payload):
        manager = self._require()
        self._owned(device, session_id)
        return manager.presented(session_id, payload)

    async def release(self, device, session_id):
        manager = self._require()
        session = manager.session
        if session is not None and session.id == session_id and device is not None and session.device_id != device:
            raise RemoteError("permission_denied", 403)
        result = await self._job(lambda: manager.release(session_id))
        await self.refresh_bar()
        self._sync_state()
        return result

    async def recover(self, *, orphans=False):
        """`orphans` is the operator's explicit say-so (CORE-2 §2); startup never passes it."""
        manager = self._require()
        result = await self._job(lambda: manager.recover(orphans=orphans))
        await self.refresh_bar()
        self._sync_state()
        return result

    async def status(self):
        capabilities = await self.refresh_capabilities()
        session = self.manager.current() if self.manager is not None else None
        return {"session": session.to_dict() if session is not None else None, "capabilities": capabilities}

    # --- the VNC bridge ----------------------------------------------------

    def vnc_endpoint(self, device, session_id):
        """The owned WayVNC loopback port, for this session's owner only.

        Everything a client is allowed to reach is decided here: the session
        must exist, be this device's, be ready, and actually be running the VNC
        backend. The port is never part of any response.
        """
        manager = self._require()
        session = self._owned(device, session_id)
        if session.backend != "vnc":
            raise RemoteError("vnc_bridge_unavailable", 409)
        if session.state != "ready":
            raise RemoteError("session_not_ready", 409)
        backend = manager.backends.get("vnc")
        port = backend.loopback_port(session) if backend is not None else None
        if type(port) is not int or not 1 <= port <= 65535:
            raise RemoteError("vnc_bridge_unavailable", 503)
        return port

    def claim_vnc_bridge(self, session_id, channel):
        if self._vnc_bridges.get(session_id) is not None:
            raise RemoteError("vnc_bridge_exists", 409)
        self._vnc_bridges[session_id] = channel

    def release_vnc_bridge(self, session_id, channel):
        if self._vnc_bridges.get(session_id) == channel:
            del self._vnc_bridges[session_id]

    # --- audio -------------------------------------------------------------

    async def audio_capabilities(self, device=None):
        await self.audio.refresh_availability()
        return self.audio.capabilities(device)
