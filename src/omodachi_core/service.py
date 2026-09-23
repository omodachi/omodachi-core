"""Device-scoped application service shared by Unix IPC and HTTPS/WSS.

Only server-owned adapters can execute commands. Request bodies contain entry
IDs, finite parameters, and revisions, never executables or configuration paths.
"""
from __future__ import annotations

from collections import OrderedDict
import asyncio
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import time
import threading
import uuid
from typing import Any, Callable, Mapping

from .catalog import Catalog, compile_catalog
from .catalog_runtime import CatalogRuntime
from .protocol import CONTRACT_REVISION, PAIRING_MODES, PANEL_VIEWS, PLUGIN_DEVICE_ID
from .hub import Hub
from .routes import RoutePolicy
from .workspace_actions import (WORKSPACE_LAYOUT_ENTRY, WorkspaceActionContext, WorkspaceActionError,
                                validate_workspace_request, validate_workspace_effects, validate_workspace_binding)


REMOTE_OPERATIONS = ("remote.capabilities", "remote.status", "remote.start", "remote.get", "remote.resize",
                     "remote.backend", "remote.heartbeat", "remote.presented", "remote.stop", "remote.recover")

#: PERF-5. The three ways `RoutePolicy.resolve` says "this row is not the row
#: the adapter was reviewed against any more" - the source action, the source
#: fields a reviewed registration pinned, or the declared surface. A client
#: that asked for the id is asking for what the id *meant*, so when one of
#: these is the answer the invocation is refused as stale rather than run.
REDEFINED_ROUTE_REASONS = frozenset({"menu_action_changed", "menu_source_changed", "menu_surface_changed"})


def _ssh_journal(entry):
    """systemd captures the daemon's stdout, so this *is* the journal line.

    UX-4 §2 requires a device's key replacement to be recorded. The shape is
    `biometric.py`'s: one JSON object a line, tagged with the subsystem, so
    `journalctl -u omodachid | grep \'"omodachi":"ssh"\'` is the whole query.
    """
    import json as _json
    try:
        print(_json.dumps({"omodachi": "ssh", **entry}, separators=(",", ":"), default=str), flush=True)
    except (OSError, ValueError):
        pass


class ServiceError(ValueError):
    def __init__(self, code: str, message: str | None = None, status: int = 400, **detail):
        # `detail` is the bounded, code-specific context the boundary serializes
        # as `error.detail`; most codes have none and carry an empty mapping.
        self.code, self.status, self.detail = code, status, detail
        super().__init__(message or code)


def fields(data: Mapping[str, Any], required=(), optional=()) -> None:
    if not isinstance(data, dict) or set(data) - (set(required) | set(optional)) or set(required) - set(data):
        raise ServiceError("invalid_request", "missing or unsupported request fields")


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ServiceError("invalid_request", "invalid identifier")
    return value


class CoreService:
    def __init__(self, hub: Hub, *, catalog: Catalog | None = None,
                 runtime: CatalogRuntime | None = None, policy: RoutePolicy | None = None,
                 remote_manager=None, remote_manager_factory=None, preferences_store=None,
                 audio_session_factory=None,
                 herdr_choices=None, unavailable_reason="remote_runtime_unavailable"):
        self.hub = hub
        self.host_identity = None
        # The three host surfaces SPEC-F1 adds. Each is installed by bootstrap
        # on a real host and stays None in demo, where there is no host theme,
        # no host font and no owned Herdr session to read.
        self.theme = None
        self.fonts = None
        # ICON-1: the host's icon theme. Only the host can turn `org.gnome.Nautilus`
        # into a picture, so only the host answers for one.
        self.icons = None
        self.herdr_bridge = None
        # HERDR-2: one bridge per session a device is actually looking at, plus
        # the per-device record of which that is. The owned session's bridge
        # stays `herdr_bridge` so everything that only knows about it still works.
        self._herdr_bridges: dict[str, Any] = {}
        self._herdr_lock = threading.RLock()
        self._herdr_sessions = None
        from .herdr_bridge import HerdrSessionChoices
        self.herdr_choices = herdr_choices or HerdrSessionChoices()
        self.media_pairing = None
        # AUTH-1's approval broker. None everywhere except a real daemon, and
        # every auth surface answers `biometric_unavailable` while it is None
        # rather than inventing a permissive default.
        self.biometric = None
        # CLIP-1's clipboard bridge. None in demo and in tests that do not ask
        # for one; every clipboard route answers `clipboard_unavailable` while
        # it is None rather than inventing an empty clipboard.
        self.clipboard = None
        # Neither host mirror exists in demo: there is no Voxtype to drive and
        # no Omarchy shell writing notification files.
        self.voice = None
        self.notifications = None
        self.notification_mirror = None
        self._media_jobs = set()
        self._media_maintenance = None
        self._media_closed = False
        self._local_lock = threading.RLock()
        self._media_candidates = {}
        self._candidate_lock = threading.Lock()
        self._preferences_policy_lock = asyncio.Lock()
        from .preferences import HostPreferencesStore
        self.preferences_store = preferences_store or HostPreferencesStore()
        # `/health` answers before a host identity exists (the loopback
        # development path), and PAIR-2's `pairing.mode` is true there too.
        if hasattr(self.hub, "health_extra"):
            self.hub.health_extra = self._health_extra
        self.instance_id = getattr(hub, "instance_id", uuid.uuid4().hex)
        self.policy = policy or RoutePolicy()
        self.runtime = runtime or CatalogRuntime(catalog or compile_catalog({}))
        self._executors: dict[str, tuple[Callable[[tuple[str, ...]], Any], bool]] = {}
        self._device_executors: set[str] = set()
        self._workspace_preflights = {}
        self._workspace_layout_signature = None
        self._workspace_layout_revision = 0
        self._requests: OrderedDict[tuple[str, str], tuple[str, dict]] = OrderedDict()
        self._catalog: dict[str, Any] | None = None
        self._focused_host_id: str | None = None
        self._focused_token: str | None = None
        self._focused_record: dict | None = None
        from .ssh_keys import AuthorizedKeys
        self.ssh_keys = AuthorizedKeys()
        self._agent_probe = None
        self._agent_probe_task = None
        self._herdr_layout_task = None
        self._workspace_adapter = None
        self.wake_adapter = None
        self.bar_modules = None
        self.bar_geometry = None
        self._wake_lock = asyncio.Lock()
        self._workspace_probe_task = None
        self._catalog_refresh_task = None
        self._condition_task = None
        self._catalog_signature = None
        from .remote.service import RemoteService
        self.remote = RemoteService(hub, manager=remote_manager,
                                    manager_factory=remote_manager_factory,
                                    audio_session_factory=audio_session_factory,
                                    unavailable_reason=unavailable_reason)
        # MENU-2: the compositor's `openlayer`/`closelayer omarchy-bar` arrive
        # on the Remote service's event stream, and what they mean is "re-read
        # the bar geometry", which this object owns.
        self.remote.core = self
        self.hub.update_state({"host": {"connected": True}, "capabilities": {
            "contract_revision": CONTRACT_REVISION, "sunshine": False, "desktop": False, "terminal": True, "native": [],
        }})
        # Volatile (PERF-4 §0): this is a field on this object, and it is what
        # `omodachi.workspace.move.N` is visible on. Reading it costs nothing
        # and it moves every time the user changes windows.
        self.runtime.register_condition("omodachi-focus-available",
                                        lambda: self._focused_token is not None, volatile=True)
        self.refresh_catalog()
        if hasattr(hub, "register_handler"):
            for operation in ("actions.invoke", "panel.summon", "workspace.select"):
                hub.register_handler(operation, lambda device, params, op=operation: self.dispatch(op, params, device))
            # HERDR-2's two session operations, so `omodachi-host herdr
            # sessions|select` asks the daemon rather than shelling out to herdr
            # behind its back.
            hub.register_handler("herdr.sessions", lambda device, params: self.herdr_sessions(
                params.get("device") or None))
            hub.register_handler("herdr.select", lambda device, params: self.herdr_select_session(
                params.get("device") or None, params.get("name")))
            hub.register_handler("control.wake", lambda device, params: self.wake_control(device, params))
            for operation in REMOTE_OPERATIONS:
                hub.register_handler(operation, lambda device, params, op=operation: self.dispatch_remote_async(op, params, device))

    async def _media_worker(self, callback, *args, **kwargs):
        if self._media_closed:
            raise ServiceError("media_pairing_unavailable", status=503)
        task = asyncio.create_task(asyncio.to_thread(callback, *args, **kwargs))
        self._media_jobs.add(task)
        def finished(job):
            self._media_jobs.discard(job)
            if not job.cancelled():
                job.exception()  # Retrieve a timed-out waiter's eventual safe failure.
        task.add_done_callback(finished)
        try:
            return await asyncio.wait_for(asyncio.shield(task), 10.0)
        except asyncio.TimeoutError:
            raise ServiceError("media_pairing_timeout", status=504) from None
        except Exception as exc:
            from .media_pairing import MediaPairingError
            from .preferences import PreferencesError
            if isinstance(exc, (MediaPairingError, PreferencesError)):
                raise ServiceError(exc.code, status=exc.status) from None
            raise

    async def dispatch_media_async(self, operation, payload, device, *, authorize):
        if self.media_pairing is None:
            raise ServiceError("media_pairing_unavailable", status=503)
        identifier(device)
        bridge = self.media_pairing
        if operation == "discover":
            fields(payload, ("client_cert_sha256", "pairing_intent"))
            if payload["pairing_intent"] is not True:
                raise ServiceError("invalid_request")
            result = await self._media_worker(bridge.discover, device, payload["client_cert_sha256"],
                pairing_intent=True, authorize=authorize)
        elif operation == "submit":
            fields(payload, ("request_id", "client_cert_sha256", "pin"))
            result = await self._media_worker(bridge.submit, device, payload, authorize=authorize)
        elif operation in {"status", "cancel"}:
            fields(payload, ("attempt_id",))
            result = await self._media_worker(getattr(bridge, operation), device, payload["attempt_id"], authorize=authorize)
        else:
            raise ServiceError("route_unavailable", status=404)
        return self.resource(result)

    # --- theme and fonts ---------------------------------------------------
    # Both are pure host reads: the template Omarchy rendered, the generated
    # shell.toml, the wallpaper symlink, fontconfig's answer. The daemon never
    # switches a theme, never sets a font and never writes into a theme.
    def theme_snapshot(self) -> dict:
        from .theme import ThemeUnavailable
        if self.theme is None:
            raise ServiceError("theme_unavailable", status=503)
        try:
            return self.resource(self.theme.snapshot())
        except ThemeUnavailable as error:
            raise ServiceError(error.args[0] if error.args else "theme_unavailable", status=503) from None

    def theme_background(self) -> tuple[Path, dict]:
        from .theme import ThemeUnavailable
        if self.theme is None:
            raise ServiceError("theme_unavailable", status=503)
        try:
            return self.theme.background_path(), self.theme.background()
        except ThemeUnavailable as error:
            raise ServiceError(error.args[0] if error.args else "theme_unavailable", status=503) from None

    def fonts_snapshot(self) -> dict:
        from .fonts import FontsUnavailable
        if self.fonts is None:
            raise ServiceError("fonts_unavailable", status=503)
        try:
            return self.resource(self.fonts.snapshot())
        except FontsUnavailable as error:
            raise ServiceError(error.args[0] if error.args else "fonts_unavailable", status=503) from None

    def font_file(self, font_id: str) -> dict:
        from .fonts import FontsUnavailable
        identifier(font_id)
        if self.fonts is None:
            raise ServiceError("fonts_unavailable", status=503)
        try:
            return self.fonts.resolve(font_id)
        except FontsUnavailable as error:
            code = error.args[0] if error.args else "fonts_unavailable"
            raise ServiceError(code, status=404 if code == "font_not_found" else 503) from None

    def icon_file(self, name: str, size: Any = None) -> dict:
        """One icon, by the name the host itself published on a catalog row.

        A miss is a 404 carrying `fallback: "application"` — the client then
        draws the generic application glyph rather than an empty box, which is
        the same answer Omarchy's own menu gives itself
        (`AppLibrary.qml:57-69` ends at `application-x-executable`).
        """
        from .icons import HostIcons, IconsUnavailable, MAX_SIZE, MIN_SIZE
        if self.icons is None:
            raise ServiceError("icons_unavailable", status=503)
        if size is None:
            requested = 64
        else:
            try:
                requested = int(str(size), 10)
            except ValueError:
                raise ServiceError("invalid_request", "icon size must be an integer") from None
        if not MIN_SIZE <= requested <= MAX_SIZE:
            raise ServiceError("invalid_request", "icon size out of range")
        if not isinstance(name, str) or not name or len(name) > 1024:
            raise ServiceError("icon_not_found", status=404, fallback=HostIcons.FALLBACK)
        try:
            return self.icons.render(name, size=requested)
        except IconsUnavailable as error:
            code = error.args[0] if error.args else "icon_not_found"
            if code in {"icon_not_found", "icon_too_large"}:
                raise ServiceError(code, status=404, fallback=HostIcons.FALLBACK) from None
            raise ServiceError(code, status=400) from None

    def notify_theme_changed(self) -> dict:
        """The `theme-set` hook ran. Re-read, and publish only a real change."""
        from .theme import ThemeUnavailable
        if self.theme is None:
            return {"published": False, "reason": "theme_unavailable"}
        before = self.theme.revision
        try:
            snapshot = self.theme.snapshot()
        except ThemeUnavailable as error:
            return {"published": False, "reason": error.args[0] if error.args else "theme_unavailable"}
        if snapshot["revision"] != before:
            self.hub.publish("theme.changed", {"revision": snapshot["revision"], "name": snapshot["name"]})
        return {"published": snapshot["revision"] != before, "revision": snapshot["revision"],
                "name": snapshot["name"]}

    def notify_fonts_changed(self) -> dict:
        from .fonts import FontsUnavailable
        if self.fonts is None:
            return {"published": False, "reason": "fonts_unavailable"}
        before = self.fonts.revision
        # TERM-1. The fallback chain is remembered between requests because it
        # costs two `fc-*` processes per probe; this hook is the one event that
        # says fontconfig's answer may have moved, so it is also the one place
        # allowed to throw that memory away.
        self.fonts.refresh()
        try:
            snapshot = self.fonts.snapshot()
        except FontsUnavailable as error:
            return {"published": False, "reason": error.args[0] if error.args else "fonts_unavailable"}
        if snapshot["revision"] != before:
            self.hub.publish("fonts.changed", {"revision": snapshot["revision"]})
        return {"published": snapshot["revision"] != before, "revision": snapshot["revision"]}

    # --- the host's Herdr sessions ------------------------------------------
    def _herdr(self, device: str | None = None):
        """The bridge for the session this device is looking at.

        Every device starts on the owned `omodachi` session; HERDR-2 lets it
        choose another of the host's own sessions, and from then on layout,
        observe and control all mean that one. A bridge is kept per session so
        the one-controller rule and the layout revision stay that session's.
        """
        if self.herdr_bridge is None:
            raise ServiceError("herdr_unavailable", status=503)
        name = self.herdr_selected(device)
        if name == self.herdr_bridge.session:
            return self.herdr_bridge
        with self._herdr_lock:
            bridge = self._herdr_bridges.get(name)
            if bridge is None:
                from .herdr_bridge import HerdrBridge
                bridge = HerdrBridge(name, socket_path=self._herdr_socket(name))
                self._herdr_bridges[name] = bridge
            return bridge

    def _herdr_socket(self, name: str):
        """Herdr's own socket path for this session, from its own listing.

        The default session's socket is not under `sessions/<name>/`, so it is
        read rather than constructed; `None` lets the bridge fall back to the
        constructed path for a session the listing no longer has.
        """
        from .herdr_bridge import HerdrUnavailable
        try:
            for row in self.herdr_sessions_list():
                if row["name"] == name and row.get("socket_path"):
                    return row["socket_path"]
        except HerdrUnavailable:
            return None
        return None

    def herdr_selected(self, device: str | None = None) -> str:
        if self.herdr_bridge is None:
            raise ServiceError("herdr_unavailable", status=503)
        return self.herdr_choices.selected(device)

    def herdr_sessions_list(self) -> list[dict]:
        from .herdr_bridge import HerdrSessions
        if self._herdr_sessions is None:
            self._herdr_sessions = HerdrSessions(owned=self.herdr_bridge.session
                                                 if self.herdr_bridge else "omodachi")
        return self._herdr_sessions.rows()

    def herdr_sessions(self, device: str | None = None) -> dict:
        """`GET /v1/herdr/sessions`: every session on the host, and its shape.

        The counts come from each session's own `api snapshot`, so a session
        that has stopped answering is listed as unreadable rather than left out
        — a name the user knows is on the machine must not silently vanish from
        the list.
        """
        from .herdr_bridge import HerdrUnavailable
        if self.herdr_bridge is None:
            raise ServiceError("herdr_unavailable", status=503)
        try:
            rows = self.herdr_sessions_list()
        except HerdrUnavailable as error:
            raise self._herdr_error(error) from None
        sessions = []
        for row in rows:
            entry = {"name": row["name"], "running": row["running"], "owned": row["owned"],
                     "herdr_default": row["herdr_default"], "readable": False,
                     "workspaces": None, "tabs": None, "panes": None, "agents": None,
                     "protocol": None, "version": None}
            if row["running"]:
                try:
                    entry.update(self._herdr_shape(row["name"], row.get("socket_path")))
                except HerdrUnavailable:
                    pass
            sessions.append(entry)
        return self.resource({"selected": self.herdr_selected(device),
                              "owned": self.herdr_bridge.session, "sessions": sessions})

    def _herdr_shape(self, name: str, socket_path=None) -> dict:
        from .herdr_bridge import HerdrBridge
        if self.herdr_bridge is not None and name == self.herdr_bridge.session:
            bridge = self.herdr_bridge
        else:
            with self._herdr_lock:
                bridge = self._herdr_bridges.get(name)
                if bridge is None:
                    bridge = HerdrBridge(name, socket_path=socket_path)
                    self._herdr_bridges[name] = bridge
        snapshot = bridge.snapshot()
        def count(key):
            value = snapshot.get(key)
            return len(value) if isinstance(value, list) else 0
        return {"readable": True, "workspaces": count("workspaces"), "tabs": count("tabs"),
                "panes": count("panes"), "agents": count("agents"),
                "protocol": snapshot.get("protocol"), "version": snapshot.get("version")}

    def herdr_select_session(self, device: str | None, name: str) -> dict:
        """`POST /v1/herdr/sessions/{name}/select`, remembered for this device.

        A name is accepted only because `herdr session list` just returned it:
        the client never gets to invent a session, and a stopped one is refused
        with the same `invalid_session` as a name that does not exist.
        """
        from .herdr_bridge import HerdrUnavailable, session_name
        if self.herdr_bridge is None:
            raise ServiceError("herdr_unavailable", status=503)
        try:
            name = session_name(name)
            rows = {row["name"]: row for row in self.herdr_sessions_list()}
        except HerdrUnavailable as error:
            raise self._herdr_error(error) from None
        row = rows.get(name)
        if row is None or not row["running"]:
            raise ServiceError("invalid_session", status=400)
        self.herdr_choices.choose(name, device)
        # No event is published here: this call runs on a worker thread and the
        # two-second poll is what owns `herdr.layout.changed`. The client gets
        # the new session from the reply and re-reads the layout itself.
        return self.resource({"selected": name, "owned": self.herdr_bridge.session,
                              "scope": "device" if device else "host"})

    @staticmethod
    def _herdr_error(error) -> ServiceError:
        code = error.args[0] if error.args else "herdr_unavailable"
        status = {"invalid_request": 400, "invalid_pane": 400, "invalid_workspace": 400,
                  "invalid_geometry": 400, "invalid_control_command": 400,
                  "herdr_control_in_use": 409, "herdr_action_unsupported": 400,
                  "invalid_tab": 400, "herdr_request_failed": 409}.get(code, 503)
        return ServiceError(code, status=status)

    def herdr_layout(self, device: str | None = None) -> dict:
        from .herdr_bridge import HerdrUnavailable
        try:
            return self.resource(self._herdr(device).layout())
        except HerdrUnavailable as error:
            raise self._herdr_error(error) from None

    def herdr_pane_action(self, pane: str, action: str, payload: dict, device: str | None = None) -> dict:
        from .herdr_bridge import HerdrUnavailable
        try:
            return self.resource(self._herdr(device).pane_action(pane, action, payload))
        except HerdrUnavailable as error:
            raise self._herdr_error(error) from None

    def herdr_workspace_select(self, workspace_id: str, device: str | None = None) -> dict:
        from .herdr_bridge import HerdrUnavailable
        try:
            return self.resource(self._herdr(device).workspace_select(workspace_id))
        except HerdrUnavailable as error:
            raise self._herdr_error(error) from None

    def herdr_tab_create(self, workspace_id: str, payload: dict, device: str | None = None) -> dict:
        from .herdr_bridge import HerdrUnavailable
        try:
            return self.resource(self._herdr(device).tab_create(workspace_id, payload))
        except HerdrUnavailable as error:
            raise self._herdr_error(error) from None

    def herdr_tab_close(self, workspace_id: str, tab_id: str, device: str | None = None) -> dict:
        from .herdr_bridge import HerdrUnavailable
        try:
            return self.resource(self._herdr(device).tab_close(workspace_id, tab_id))
        except HerdrUnavailable as error:
            raise self._herdr_error(error) from None

    def notify_herdr_layout(self) -> dict:
        """Poll-driven, because 0.8.2 event replay on subscribe is unverified.

        `events.subscribe` streams on the connection that asked for it and
        0.9.0 changed whether history is replayed, so the safe reading for a
        0.8.2 host is the snapshot the bridge already projects. It publishes
        only when the projection actually changed.
        """
        from .herdr_bridge import HerdrUnavailable
        if self.herdr_bridge is None:
            return {"published": False, "reason": "herdr_unavailable"}
        # The owned session plus whichever others a device has chosen. A bridge
        # is only created by a selection, so this stays the sessions somebody is
        # actually looking at rather than every session on the machine.
        with self._herdr_lock:
            bridges = [self.herdr_bridge, *self._herdr_bridges.values()]
        published, revisions, reason = [], {}, None
        for bridge in bridges:
            before = bridge.revision
            try:
                layout = bridge.layout()
            except HerdrUnavailable as error:
                reason = error.args[0] if error.args else "herdr_unavailable"
                continue
            revisions[bridge.session] = layout["revision"]
            if layout["revision"] != before:
                # `session` is what tells a device whether this is its own
                # session moving or somebody else's; the revision alone cannot.
                self.hub.publish("herdr.layout.changed",
                                 {"revision": layout["revision"], "session": bridge.session})
                published.append(bridge.session)
        owned = self.herdr_bridge.session
        if owned not in revisions:
            return {"published": bool(published), "sessions": published,
                    "reason": reason or "herdr_unavailable"}
        return {"published": bool(published), "sessions": published,
                "revision": revisions[owned], "revisions": revisions}

    def schedule_herdr_layout_refresh(self) -> bool:
        if self.herdr_bridge is None or self._herdr_layout_task is not None and not self._herdr_layout_task.done():
            return False
        async def run():
            try:
                await asyncio.to_thread(self.notify_herdr_layout)
            finally:
                self._herdr_layout_task = None
        self._herdr_layout_task = asyncio.ensure_future(run())
        return True

    def shortcuts_snapshot(self):
        catalog=self.refresh_catalog(invalidate=True)
        parent=next((r for r in catalog['entries'] if r['id']=='learn.keybindings'),{})
        available=parent.get('provider_state',{}).get('status')=='available'
        rows=[r['shortcut'] for r in catalog['entries'] if r.get('providerMenu')=='learn.keybindings' and isinstance(r.get('shortcut'),dict)]
        return self.resource({'revision':catalog['revision'],'source':'hyprland','available':available,
            'reason':None if available else 'keybindings_unavailable','items':sorted(rows,key=lambda r:r['order'])})

    async def wake_control(self,device,payload):
        if not isinstance(device,str) or not device:raise ServiceError('pairing_required',status=401)
        fields(payload)
        if self.wake_adapter is None:raise ServiceError('wake_unavailable',status=503)
        from .graphical import GraphicalUnavailable
        async with self._wake_lock:
            try:result=await asyncio.to_thread(self.wake_adapter.wake)
            except (OSError,ValueError,GraphicalUnavailable):raise ServiceError('wake_unavailable',status=503) from None
            if self.hub.state_view('wake')['wake']!=result['wake']:
                self.hub.update_state({'wake':result['wake']},event_type='wake.changed')
            return self.resource(result)

    async def dispatch_local_async(self, operation, payload, *, local_authorize):
        # This entry point is installed on the verified Unix transport only.
        # No Hub/public handler registers local operations.
        if operation == "local.auth.approve":
            # AUTH-1. Deliberately outside `_dispatch_local`: it waits for a
            # human for up to two minutes, and `_local_lock` is held by every
            # theme change, pairing decision and device revoke on this host.
            # It also never raises for an ordinary refusal - a PAM helper needs
            # an answer, and "no" is an answer.
            if not callable(local_authorize) or local_authorize() is not True:
                raise ServiceError("permission_denied", status=403)
            if self.biometric is None:
                return {"approved": False, "outcome": "unavailable", "approval_id": None,
                        "device_id": None, "device_name": None}
            try:
                return await self.biometric.request(payload)
            except ServiceError:
                raise
            except Exception:
                return {"approved": False, "outcome": "error", "approval_id": None,
                        "device_id": None, "device_name": None}
        if operation == "local.preferences.set":
            async with self._preferences_policy_lock:
                return await self._media_worker(self._dispatch_local, operation, payload, local_authorize)
        result = await self._media_worker(self._dispatch_local, operation, payload, local_authorize)
        if operation in {"local.devices.revoke", "local.media-pairing.revoke"}:
            session = self.remote.manager.current() if self.remote.manager is not None else None
            if session is not None and session.device_id == payload["device_id"]:
                try:
                    result["remote_cleanup"] = await self.remote.release(None, session.id)
                except Exception:
                    result["remote_cleanup"] = {"released": False, "errors": [{"step": "release", "code": "release_failed"}]}
        return result

    def _dispatch_local(self, operation, payload, local_authorize):
        if not callable(local_authorize) or local_authorize() is not True:
            raise ServiceError("permission_denied", status=403)
        with self._local_lock:
            # The Omarchy `theme-set` / `font-set` hooks run as the same user
            # and only say "it changed"; the daemon re-reads the host itself
            # and decides whether that is news.
            if operation == "local.theme.changed":
                fields(payload)
                return self.notify_theme_changed()
            if operation == "local.fonts.changed":
                fields(payload)
                return self.notify_fonts_changed()
            if operation == "local.preferences.get":
                fields(payload)
                return self.preferences_snapshot()
            if operation == "local.preferences.set":
                fields(payload, ("expected_revision", "changes"))
                result = self._preferences_runtime(self.preferences_store.set(**payload))
                # CLIP-1: the watcher exists only while the preference allows
                # it, so the switch takes effect where it is thrown rather than
                # at the next restart.
                self.apply_clipboard_preference()
                return result
            if operation.startswith("local.pair."):
                pairing = getattr(self, "pairing", None)
                if pairing is None:
                    raise ServiceError("pairing_unavailable", status=503)
                action = operation.removeprefix("local.pair.")
                if action in {"begin", "pending"}:
                    fields(payload)
                    return getattr(pairing, action)()
                if action in {"approve", "reject"}:
                    fields(payload, ("request_id",), ("remote",) if action == "approve" else ())
                    # PAIR-3 §2.1: Remote is what one approval means, so it is
                    # the default rather than a flag the caller has to remember.
                    # Making it opt-in is how a device ended up approved for the
                    # terminal and not for the screen, with nothing saying so.
                    remote = payload.get("remote", True) if action == "approve" else False
                    if type(remote) is not bool:
                        raise ServiceError("invalid_request")
                    try:
                        result = pairing.decide(payload["request_id"], approve=action == "approve")
                    except ServiceError as error:
                        # Retrying an explicit Approve + Remote after a lost
                        # reply/claim race resumes the same approved binding.
                        if not remote or error.code != "pairing_request_already_decided":
                            raise
                        with pairing.transaction() as state:
                            row = state["requests"].get(payload["request_id"])
                            if not row or row["status"] not in {"approved", "claimed"}:
                                raise error
                            result = pairing.public(row)
                    if action == "reject":
                        return result
                    # One approval, three grants. The credential is the claim's
                    # own; these two land here, and each records what really
                    # happened so the claim reports it instead of promising it.
                    granted = {}
                    if remote:
                        # A streaming grant that cannot be made does not fail
                        # the approval and does not pass silently either: the
                        # companion still lands, and `grants.media` plus a
                        # reason say the streaming half is missing so the panel
                        # can offer `media-pairing grant-remote`.
                        result["remote"] = self._grant_remote(result["device_id"], result["request_id"], local_authorize)
                        granted["media"] = bool(result["remote"].get("media_authorized"))
                    if result.get("ssh_public_key"):
                        result["ssh"] = self._authorize_ssh(result["ssh_public_key"], result["device_id"])
                        granted["ssh"] = bool(result["ssh"].get("authorized"))
                    if granted:
                        result["grants"] = pairing.grant(result["request_id"], **granted)["grants"]
                    return result
            if operation.startswith("local.auth."):
                action = operation.removeprefix("local.auth.")
                if self.biometric is None:
                    raise ServiceError("biometric_unavailable", status=503)
                if action == "status":
                    fields(payload)
                    return self.biometric.status()
                if action == "revoke":
                    fields(payload, ("device_id",))
                    return self.biometric.revoke(identifier(payload["device_id"]))
                raise ServiceError("route_unavailable", status=404)
            if operation == "local.devices.list":
                fields(payload, (), ("all", "prune_ssh_keys"))
                everything = payload.get("all", False)
                prune = payload.get("prune_ssh_keys", False)
                if type(everything) is not bool or type(prune) is not bool:
                    raise ServiceError("invalid_request")
                # UX-4 §3. Prune first, so the rows this answers with are the
                # rows the file now has rather than the ones it had a moment ago.
                pruned = self.ssh_key_prune() if prune else None
                result = self._devices_result(everything, local_authorize)
                if pruned is not None:
                    result["ssh_keys_pruned"] = pruned
                return result
            if operation == "local.devices.purge":
                # PLUG-4 §2.2: take the corpses out of the registry. Only a
                # revoked device, only when its media revocation is finished,
                # and never this host's own panel credential.
                fields(payload, (), ("older_than_days",))
                older = payload.get("older_than_days")
                if older is not None and (type(older) not in (int, float) or not 0 <= older <= 3650):
                    raise ServiceError("invalid_request")
                return self._purge_devices(None if older is None else float(older), local_authorize)
            if operation == "local.devices.revoke":
                fields(payload, ("device_id",), ("force",))
                force = payload.get("force", False)
                if type(force) is not bool:
                    raise ServiceError("invalid_request")
                device = identifier(payload["device_id"])
                # The plugin's own credential is what draws this page. Revoking
                # it blanks the panel that the user would need in order to undo
                # the revoke, so it takes an explicit --force.
                if device == PLUGIN_DEVICE_ID and not force:
                    raise ServiceError("plugin_credential", status=409)
                if self.media_pairing is None:
                    raise ServiceError("media_pairing_unavailable", status=503)
                media = self.media_pairing.revoke_device(device, local_authorize=local_authorize)
                revoked = self.hub.auth.revoke_device(device)
                # PAIR-3: the durable approval is part of what a revoke takes
                # back. Leaving it behind would keep a grant source alive for a
                # device that no longer has a credential.
                pairing = getattr(self, "pairing", None)
                if pairing is not None:
                    try:
                        pairing.forget_device(device)
                    except (ServiceError, OSError, ValueError):
                        pass
                with self._candidate_lock:
                    self._media_candidates = {k: v for k, v in self._media_candidates.items() if k[2] != device}
                # A revoked device keeps nothing: its credential, its Sunshine
                # certificate and the authorized_keys line SPEC-F3's terminal
                # opens with all go in the same call. A file we cannot write is
                # reported, never silently treated as one fewer way in.
                # AUTH-1: an enrolled approval key is one more thing a revoked
                # device keeps nothing of. A key left behind would let a device
                # with no credential still be named in an approval's audience.
                if self.biometric is not None:
                    try:
                        self.biometric.revoke(device)
                    except (ServiceError, OSError, ValueError):
                        pass
                from .ssh_keys import SshKeyError
                try:
                    ssh = self.ssh_keys.revoke(device)
                except SshKeyError as error:
                    ssh = {"revoked": False, "removed": 0, "device": device, "error": error.code}
                except OSError:
                    ssh = {"revoked": False, "removed": 0, "device": device, "error": "authorized_keys_unreadable"}
                result = {"device_id": device, "revoked": revoked, "media": media, "ssh": ssh}
                # PLUG-4 §2.3: when the fork really took the certificate back
                # there is nothing left to chase, so the device leaves the
                # registry instead of becoming another "revoked" row nobody can
                # act on. A fork that cannot revoke still leaves the row, and
                # `certificate_revocation_supported: false` says why.
                if media.get("certificate_revocation_supported") and media.get("pending_media_revocations") == 0:
                    result["purged"] = self._purge_device(device, local_authorize)
                return result
            if not operation.startswith("local.media-pairing.") or self.media_pairing is None:
                raise ServiceError("route_unavailable", status=404)
            action = operation.removeprefix("local.media-pairing.")
            if action == "pending":
                fields(payload)
                return self.media_pairing.pending_local(local_authorize=local_authorize)
            if action in {"approve", "cancel"}:
                fields(payload, ("attempt_id", "request_id", "client_cert_sha256"))
                return getattr(self.media_pairing, action + "_local")(**payload, local_authorize=local_authorize)
            if action == "revoke":
                fields(payload, ("device_id",))
                return self.media_pairing.revoke_device(payload["device_id"], local_authorize=local_authorize)
            if action == "certificates":
                # PLUG-4 follow-up: the certificates the fork authorizes that
                # no binding of this host claims. Reading them used to mean
                # opening Sunshine's private state file by hand.
                fields(payload, (), ("purge_unknown",))
                purge = payload.get("purge_unknown", False)
                if type(purge) is not bool:
                    raise ServiceError("invalid_request")
                return self.media_pairing.certificates(local_authorize=local_authorize, purge_unknown=purge)
            if action == "grant-remote":
                fields(payload, ("device_id", "source_request_id"))
                pairing = getattr(self, "pairing", None)
                if pairing is None:
                    raise ServiceError("pairing_unavailable", status=503)
                # A real approval on record is still the only thing that
                # authorizes this, and a made-up request id still gets 403.
                # What changed in PAIR-3 is that the record survives: a claimed
                # row is no longer pruned at the 300 s TTL, so the repair for a
                # half-paired device does not disappear five minutes after
                # pairing (`pairing.PairingStore.transaction`).
                with pairing.transaction() as state:
                    row = state["requests"].get(payload["source_request_id"])
                    if not row or row["device_id"] != payload["device_id"] or row["status"] not in {"approved", "claimed"}:
                        raise ServiceError("pairing_approval_required", status=403)
                return self.media_pairing.grant_remote(payload["device_id"], remote_allowed=True,
                    source_request_id=payload["source_request_id"], local_authorize=local_authorize)
            raise ServiceError("route_unavailable", status=404)

    def _grant_remote(self, device, request_id, local_authorize):
        """The streaming half of one approval, reported rather than assumed."""
        from .media_pairing import MediaPairingError
        if self.media_pairing is None:
            return {"device_id": device, "media_authorized": False, "reason": "media_pairing_unavailable"}
        try:
            return self.media_pairing.grant_remote(device, remote_allowed=True,
                source_request_id=request_id, local_authorize=local_authorize)
        except (ServiceError, MediaPairingError) as error:
            return {"device_id": device, "media_authorized": False, "reason": error.code}
        except OSError:
            return {"device_id": device, "media_authorized": False, "reason": "media_pairing_state_unavailable"}

    def _devices_result(self, everything, local_authorize):
        """What the Devices page should actually show.

        PLUG-4 §2.2: every spec's throwaway test device stayed on this page
        forever as a `revoked` row with no action on it - 32 of them against
        one real device. The default answer is now what a user can act on:
        authorized devices (including this host's own panel credential) and
        anything with a request still waiting. `--all` is the full registry,
        and `revoked_hidden` is how many rows the default answer left out, so
        a panel can offer to clear them instead of listing them.
        """
        devices = self._devices_with_media()
        if everything:
            return {"devices": devices, "source": "local_registry", "revoked_hidden": 0, "filtered": False}
        waiting = set()
        if self.media_pairing is not None:
            try:
                pending = self.media_pairing.pending_local(local_authorize=local_authorize)
                waiting = {row["device_id"] for row in pending["requests"]}
            except (ServiceError, OSError, ValueError):
                # Unreadable is not "nothing is waiting": keep every row rather
                # than hide a device that is mid-approval.
                return {"devices": devices, "source": "local_registry", "revoked_hidden": 0, "filtered": False}
        # CORE-2: an expired device is still something to act on - it is one
        # approval from working again, or one Remove from gone - so it stays.
        shown = [row for row in devices
                 if row["status"] in ("authorized", "expired") or row["device_id"] in waiting]
        return {"devices": shown, "source": "local_registry",
                "revoked_hidden": len(devices) - len(shown), "filtered": True}

    def _purge_device(self, device, local_authorize):
        """Remove one already-revoked device from every local registry."""
        from .media_pairing import MediaPairingError
        media = {"purged": True}
        if self.media_pairing is not None:
            try:
                media = self.media_pairing.purge_device(device, local_authorize=local_authorize)
            except (ServiceError, MediaPairingError, OSError, ValueError) as error:
                return {"device_id": device, "purged": False, "reason": getattr(error, "code", "media_pairing_unavailable")}
        if not media.get("purged"):
            return {"device_id": device, "purged": False, "reason": "media_revocation_pending",
                    "pending_media_revocations": media.get("pending_media_revocations", 0)}
        try:
            credentials = self.hub.auth.purge_device(device)
        except (ValueError, OSError):
            return {"device_id": device, "purged": False, "reason": "credential_registry_unavailable"}
        pairing = getattr(self, "pairing", None)
        if pairing is not None:
            try:
                pairing.forget_device(device)
            except (ServiceError, OSError, ValueError):
                pass
        with self._candidate_lock:
            self._media_candidates = {k: v for k, v in self._media_candidates.items() if k[2] != device}
        return {"device_id": device, "purged": True,
                "removed_credentials": credentials["removed_credentials"],
                "removed_bindings": media.get("removed_bindings", 0),
                "removed_attempts": media.get("removed_attempts", 0)}

    def _purge_devices(self, older_than_days, local_authorize):
        purged, kept = [], []
        cutoff = None if older_than_days is None else time.time() - older_than_days * 86400
        for row in self.hub.auth.list_devices():
            device = row["device_id"]
            if row["status"] != "revoked" or device == PLUGIN_DEVICE_ID:
                continue
            if cutoff is not None:
                # The media permission row is the only per-device timestamp the
                # host keeps. An unknown age is never assumed to be old.
                changed = None
                if self.media_pairing is not None:
                    try:
                        changed = self.media_pairing.device_last_change(device)
                    except (ServiceError, OSError, ValueError):
                        changed = None
                if changed is None or changed > cutoff:
                    kept.append({"device_id": device, "reason": "newer_than_cutoff"})
                    continue
            result = self._purge_device(device, local_authorize)
            (purged if result["purged"] else kept).append(result if result["purged"] else
                {"device_id": device, "reason": result.get("reason", "purge_failed")})
        return {"purged": [row["device_id"] for row in purged], "kept": kept,
                "removed_credentials": sum(row.get("removed_credentials", 0) for row in purged),
                "older_than_days": older_than_days}

    def _devices_with_media(self):
        """The credential registry, with the streaming grant beside it.

        PAIR-3 §2.3: "companion yes, streaming no" was invisible here, so a
        half-paired device looked fully paired on the computer and unpaired on
        the iPad. `media_authorized` says which it is and `source_request_id`
        says what `media-pairing grant-remote` can repair it from.
        """
        devices = self.hub.auth.list_devices()
        allowed, pairing = {}, getattr(self, "pairing", None)
        if self.media_pairing is not None:
            try:
                allowed = self.media_pairing.authorized_devices()
            except (ServiceError, OSError, ValueError):
                # Unreadable is not the same as unauthorized: leave the column
                # off entirely rather than tell the panel every device lost its
                # streaming grant.
                return devices
        for row in devices:
            device = row["device_id"]
            row["media_authorized"] = device in allowed
            source = allowed.get(device) or ""
            if not source and pairing is not None:
                try:
                    source = pairing.grant_source(device) or ""
                except (ServiceError, OSError, ValueError):
                    source = ""
            row["source_request_id"] = source
        self._decorate_ssh_keys(devices)
        return devices

    def _decorate_ssh_keys(self, devices):
        """UX-4 §3: what each device can open the terminal with, on its row.

        `devices list` named every grant a device holds except this one, so a
        device carrying two `authorized_keys` lines - the shape a key drift
        leaves behind - could only be found by reading `ssh list` separately
        and noticing a repeated name. An unreadable file leaves the column off
        rather than telling every row it has no key.
        """
        from .ssh_keys import SshKeyError
        try:
            rows = self.ssh_keys.listing()
        except (SshKeyError, OSError):
            return
        owned = {}
        for row in rows:
            owned.setdefault(row["device"], []).append(row["fingerprint"])
        for row in devices:
            keys = owned.get(row["device_id"], [])
            row["ssh_keys"] = keys
            # The one fact a panel acts on: more than one line for one device.
            row["ssh_key_duplicates"] = len(keys) > 1

    def ssh_key(self, device: str, payload: dict) -> dict:
        """UX-4 §2. The device replaces the key this host holds for it.

        A device that reinstalls keeps its credential (iOS Keychain outlives
        the app container) and loses the profile the SSH key was keyed by, so
        it comes back authenticated, trusted, and holding a private key whose
        public half this host has never seen. Until now the only way back was
        to pair again, which is the one thing an already-paired device will not
        think to do - it is paired.

        So an *already authenticated* device may state the key it is offering,
        and this host writes it in place of whatever that device had. The
        authority is the credential on this request: a device can only ever
        replace its own line, and `device_id` is never read from the body.
        """
        from .ssh_keys import SshKeyError
        fields(payload, ("public_key",))
        public_key = payload["public_key"]
        if not isinstance(public_key, str):
            raise ServiceError("invalid_request", "public_key must be one OpenSSH line")
        try:
            result = self.ssh_keys.replace(public_key, device)
        except SshKeyError as error:
            raise ServiceError(error.code, error.code, 409 if error.code.startswith("public_key_") else 400) from None
        except OSError:
            raise ServiceError("authorized_keys_unreadable", "authorized_keys could not be written", 503) from None
        _ssh_journal({"event": "ssh.key.replaced", "device_id": device,
                      "fingerprint": result["fingerprint"], "reason": result["reason"],
                      "removed": result["removed"], "replaced": result["replaced"]})
        # `device` and `device_id` are the same string; the wire says it once,
        # under the name every other resource on this boundary uses.
        return self.resource({"device_id": result.pop("device"), **result})

    def ssh_key_state(self, device: str) -> dict:
        """What key this host holds for the device asking, if any.

        UX-4 §2. A device cannot otherwise find out: `omodachi-host ssh list`
        is a local administrator command and the claim only echoes the key the
        *request* carried. Without this a companion has no way to notice that
        the host's line and its own private half have come apart - which is the
        state the whole of UX-4 is about, and it is silent until the terminal
        fails.

        It answers only about the device on the credential, and only with
        fingerprints. A public key is not returned: this is an inventory, the
        same as `GET /v1/auth/keys`, not a key distribution point.
        """
        from .ssh_keys import SshKeyError
        try:
            rows = self.ssh_keys.listing()
        except SshKeyError as error:
            raise ServiceError(error.code, error.code, 503) from None
        except OSError:
            raise ServiceError("authorized_keys_unreadable", "authorized_keys could not be read", 503) from None
        # In file order, so the last one is the newest (see `AuthorizedKeys.prune`).
        mine = [row["fingerprint"] for row in rows if row["device"] == device]
        return self.resource({"device_id": device, "authorized": bool(mine),
                              "fingerprint": mine[-1] if mine else None, "fingerprints": mine})

    def ssh_key_prune(self, device=None) -> dict:
        """UX-4 §3. Keep the newest owned line per device, drop the older ones."""
        from .ssh_keys import SshKeyError
        try:
            result = self.ssh_keys.prune(device)
        except SshKeyError as error:
            raise ServiceError(error.code, error.code, 400) from None
        except OSError:
            raise ServiceError("authorized_keys_unreadable", "authorized_keys could not be written", 503) from None
        if result["removed"]:
            _ssh_journal({"event": "ssh.key.pruned", "removed": result["removed"],
                          "devices": sorted(result["devices"])})
        return result

    def _authorize_ssh(self, public_key, device):
        """Land one companion's public key, reporting failure instead of hiding it."""
        from .ssh_keys import SshKeyError
        try:
            return self.ssh_keys.authorize(public_key, device)
        except SshKeyError as error:
            return {"authorized": False, "changed": False, "device": device, "error": error.code}
        except OSError:
            return {"authorized": False, "changed": False, "device": device,
                    "error": "authorized_keys_unreadable"}

    def _preferences_runtime(self, snapshot):
        available = bool(self.hub.capabilities_snapshot().get("desktop"))
        return {**snapshot, "runtime": {"profile_defaults": True, "host_audio_default": True,
            "remote_available": available,
            # CLIP-1. A host with no clipboard bridge (demo, or a daemon
            # started outside a graphical session) offers the switch as
            # unsupported rather than as off: they are different answers.
            "clipboard_supported": self.clipboard is not None}}

    def apply_clipboard_preference(self):
        """Match the watcher to `clipboard_sync`. Safe to call when there is none."""
        clipboard = getattr(self, "clipboard", None)
        if clipboard is None:
            return False
        try:
            return clipboard.apply()
        except Exception:
            # A watcher that cannot start is not a reason to fail the write that
            # asked for it; the next change or restart tries again.
            return False

    def clipboard_read(self):
        """CLIP-1 `GET /v1/clipboard`. Text only, and only when both sides agree."""
        clipboard = getattr(self, "clipboard", None)
        if clipboard is None:
            from .clipboard import ClipboardError
            raise ClipboardError("clipboard_unavailable", 503)
        return clipboard.read()

    def clipboard_write(self, text):
        """CLIP-1 `PUT /v1/clipboard`. `both` only; the answer carries no content."""
        clipboard = getattr(self, "clipboard", None)
        if clipboard is None:
            from .clipboard import ClipboardError
            raise ClipboardError("clipboard_unavailable", 503)
        return clipboard.write(text)

    def preferences_snapshot(self):
        return self._preferences_runtime(self.preferences_store.get())

    def schedule_media_maintenance(self):
        if self.media_pairing is None or self._media_closed:
            return
        if self._media_maintenance is None or self._media_maintenance.done():
            async def maintain():
                try:
                    await self._media_worker(self.media_pairing.maintenance)
                except (ServiceError, OSError, ValueError):
                    pass  # Next tick retries; never log request data or remote errors.
            self._media_maintenance = asyncio.create_task(maintain())

    async def close_media(self):
        if getattr(self,"agent_chat",None) is not None:await self.agent_chat.close()
        self._media_closed = True
        if self._media_maintenance is not None:
            await asyncio.gather(self._media_maintenance, return_exceptions=True)
        if self._media_jobs:
            await asyncio.gather(*tuple(self._media_jobs), return_exceptions=True)
        if self.media_pairing is not None:
            await asyncio.to_thread(self.media_pairing.close)
        with self._candidate_lock:
            self._media_candidates.clear()

    def _health_extra(self) -> dict:
        """`/health` is an anchor, not authorization: an identity and one word.

        `pairing.mode` is that one word. A companion reads it to decide whether
        its host list row says "unpaired" or "needs an invitation"; it proves
        nothing on its own, exactly like the fingerprint beside it.
        """
        identity = self.host_identity.health() if self.host_identity is not None else {}
        return {**identity, "pairing": {"mode": self.pairing_mode()}}

    def install_host_identity(self, identity):
        """Publish the pinned certificate identity on health and on claim."""
        self.host_identity = identity
        if hasattr(self.hub, "health_extra"):
            self.hub.health_extra = self._health_extra
        return identity

    def host_descriptor(self) -> dict:
        """What a claiming companion pins and reconnects to. Never a credential."""
        if self.host_identity is None:
            return {"host_id": None, "host_name": None,
                    "tls_fingerprint_sha256": None, "endpoints": []}
        return self.host_identity.descriptor()

    def ssh_target(self) -> dict:
        """Where this host's terminal is, so no one has to type it in."""
        import getpass
        try:
            user = getpass.getuser()
        except (KeyError, OSError):
            user = None
        descriptor = self.host_descriptor()
        endpoints = descriptor.get("endpoints") or []
        host = endpoints[0]["host"] if endpoints else descriptor.get("host_name")
        return {"user": user, "host": host, "port": 22}

    def pairing_mode(self) -> str:
        """`open` unless this host has been locked to invitations (PAIR-2 §1)."""
        store = getattr(self, "preferences_store", None)
        if store is None:
            return "open"
        try:
            value = store.get()["values"].get("pairing_mode")
        except Exception:
            # A preferences store that cannot be read is not a reason to change
            # how this host pairs; it answers with the documented default.
            return "open"
        return value if value in PAIRING_MODES else "open"

    def pairing_request(self, payload: dict, *, remote_addr=None) -> dict:
        pairing = getattr(self, "pairing", None)
        if pairing is None:
            raise ServiceError("pairing_unavailable", status=503)
        # PAIR-2: the invitation is optional. In `open` mode a request without
        # one becomes pending and waits for the same local Approve; in `invite`
        # mode its absence is `pairing_invitation_required`.
        fields(payload, ("device_id", "device_name"), ("invitation", "ssh_public_key"))
        return self.resource(pairing.request(**payload, remote_addr=remote_addr,
                                             mode=self.pairing_mode()))

    def pairing_claim(self, request_id: str, payload: dict) -> dict:
        """A successful claim is the moment the client pins this certificate."""
        pairing = getattr(self, "pairing", None)
        if pairing is None:
            raise ServiceError("pairing_unavailable", status=503)
        fields(payload, ("request_secret",))
        result = pairing.claim(request_id, payload["request_secret"])
        # The claim is the only place a client learns where its terminal lives.
        # SPEC-I deletes the field that used to ask the user for it.
        return self.resource({**result, **self.host_descriptor(), "ssh": self.ssh_target()})

    def remote_certificate(self, device_id):
        """The Sunshine backend's client identity comes only from a real pairing."""
        if self.media_pairing is None:
            return None
        if self.media_pairing.remote_denied(device_id):
            return None
        return self.media_pairing.paired_certificate(device_id)

    @staticmethod
    def resource(value: dict) -> dict:
        return {"contract_revision": CONTRACT_REVISION, **value}

    def _resolve_target(self, entry_id: str) -> tuple[dict, dict | None]:
        """Re-read one row's own sources and answer with the catalog it is in.

        PERF-5. This is the whole of what `invoke`'s pre-dispatch refresh is
        for, narrowed to the row being invoked: the menu sources (an edit to
        the JSONC has to be seen before anything is authorised), that row's
        `when`/`checked`, and the listing it came out of - which is what
        re-registers the executor and the route adapter, so a desktop entry or
        a keybinding record that changed under the client is caught here and
        not after the action has already run.

        The fallback to the whole table is for an id the snapshot does not
        have, where there is no row to narrow to. That is the path that ends
        in a refusal, so paying the old price on it costs a user nothing.
        """
        snapshot = (self.refresh_catalog(allow_expired=True) if self.runtime.invalidate_row(entry_id)
                    else self.refresh_catalog(invalidate=True))
        return snapshot, next((row for row in snapshot["entries"] if row["id"] == entry_id), None)

    def refresh_catalog(self, *, invalidate=False, copy=True, allow_expired=False) -> dict:
        """The published catalog. `copy=False` is for a caller that ignores it.

        PERF-5: `allow_expired` is "answer from the shelf unless something
        changed", for the invoke path. See `CatalogRuntime.current_revision`.
        """
        if invalidate:
            self.runtime.invalidate()
        capabilities = self.hub.capabilities_snapshot()
        current_state = self.hub.state_view("host", "agent", "herdr", "workspace")
        state_agent = (current_state.get("agent") or {}).get("default_agent", {})
        source_stale = bool((current_state.get("host") or {}).get("catalog_stale"))
        # PERF-4. The published catalog is a pure function of the runtime's own
        # snapshot (whose revision is a digest of every row and every condition
        # reading in it) and the handful of state readings below: nothing else
        # decides a row's route or its readiness. When none of them has moved
        # the answer is the one already on the shelf, and what is skipped is a
        # route resolution per row, a SHA-256 over all 594 of them, a deep
        # compare against the previous catalog and two deep copies of half a
        # megabyte - about 280 ms, paid every time the 0.5 s workspace probe or
        # the 2 s source poll asked the same question over again.
        workspace = current_state.get("workspace") or {}
        def signature_for(revision):
            return (revision, source_stale,
                    json.dumps([capabilities, state_agent.get("ready_to_attach"),
                                (current_state.get("host") or {}).get("graphical_state"),
                                (current_state.get("herdr") or {}).get("available"),
                                workspace.get("active"),
                                sorted(row["id"] for row in workspace.get("items") or []),
                                sorted(self._executors), sorted(self.policy._adapters)],
                               sort_keys=True, default=str))
        # The cheap question first: the runtime can say which catalog is
        # current without building one, so a refresh that changes nothing costs
        # neither the rebuild nor a deep copy of it.
        known = self.runtime.current_revision(allow_expired=allow_expired)
        if (known is not None and self._catalog is not None
                and signature_for(known) == self._catalog_signature):
            return deepcopy(self._catalog) if copy else self._catalog
        snapshot = self.runtime.refresh()
        signature = signature_for(snapshot["revision"])
        if self._catalog is not None and signature == self._catalog_signature:
            self._catalog_signature = signature
            return deepcopy(self._catalog) if copy else self._catalog
        for entry in snapshot["entries"]:
            route = self.policy.resolve(entry).as_dict()
            ready = bool(route["supported"]) and not source_stale
            if route["route"] == "desktop":
                ready = ready and bool(capabilities.get("desktop"))
            elif entry["id"] == "omodachi.agent":
                ready = ready and bool(state_agent.get("ready_to_attach"))
            elif route["route"] == "terminal":
                ready = ready and bool(capabilities.get("terminal")) and bool(route.get("argv"))
                if route.get("argv", [None])[0] == "herdr":
                    ready = ready and bool((current_state.get("herdr") or {}).get("available"))
            elif route["route"] == "native":
                ready = ready and route.get("native_view") in capabilities.get("native", [])
            elif route["route"] == "host":
                ready = ready and entry["id"] in self._executors
                if entry["id"].startswith("omodachi.workspace."):
                    ready = ready and (current_state.get("host") or {}).get("graphical_state") in {"available", "fixture"}
                    suffix=entry["id"].rsplit(".",1)[-1]
                    if suffix.isdigit():ready = ready and int(suffix) in {row['id'] for row in workspace.get('items') or []}
            # MENU-3: a row whose `disabled` expression is true is drawn grey
            # with this reason; one whose `disabled` nobody could answer is not.
            disabled = ((entry.get("conditions") or {}).get("disabled") or {}).get("value") is True
            route["ready"] = ready and not disabled
            if not ready:
                route["readiness_reason"] = "catalog_source_unavailable" if source_stale else route.get("reason", "capability_unavailable")
            elif disabled:
                route["readiness_reason"] = "condition_disabled"
            entry["route"] = route
        snapshot["revision"] = hashlib.sha256(json.dumps(snapshot["entries"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:16]
        snapshot = self.resource(snapshot)
        if snapshot != self._catalog:
            self._catalog = deepcopy(snapshot)
            # PERF-4: the event says which catalog is current, not what is in
            # it. The rows live in the state, which is read back over HTTP;
            # carrying them here filled the retained history and every
            # subscriber queue with copies of half a megabyte.
            self.hub.update_state({"catalog": snapshot}, event_type="catalog.changed",
                                  event_patch={"catalog": {"contract_revision": snapshot["contract_revision"],
                                                           "revision": snapshot["revision"],
                                                           "source_revision": snapshot.get("source_revision")}})
        workspace = self.hub.state_view("workspace")["workspace"] or {}
        existing = {entry["id"] for entry in snapshot["entries"]}
        before = deepcopy(workspace)
        for item in workspace.get("items", []):
            for field, operation in (("select_entry_id", "select"), ("move_entry_id", "move")):
                candidate = f"omodachi.workspace.{operation}.{item['id']}"
                item[field] = candidate if candidate in existing else None
        if workspace != before:
            self.hub.update_state({"workspace": workspace}, event_type="workspace.changed")
        self._publish_workspace_layout_binding(snapshot)
        self._catalog_signature = signature
        return deepcopy(snapshot) if copy else snapshot

    def _publish_workspace_layout_binding(self, catalog, *, unavailable=False):
        state=self.hub.state_view("workspace")
        active=state.get("workspace",{}).get("active")
        row=next((item for item in catalog.get("entries",[]) if item.get("id")==WORKSPACE_LAYOUT_ENTRY),None)
        checked=(row or {}).get("conditions",{}).get("checked",{})
        layout=({"workspace_layout_dwindle":"dwindle","workspace_layout_scrolling":"scrolling"}.get(checked.get("reason"))
                if checked.get("status")=="available" and checked.get("value") is None else None)
        route=(row or {}).get("route",{})
        available=(not unavailable and type(active) is int and 1<=active<=10 and layout is not None
                   and (row or {}).get("visible") is True and route.get("route")=="host"
                   and route.get("supported") is True and route.get("ready") is True)
        source={key:(row or {}).get(key) for key in ("action","when","checked","target","provider","surface")}
        signature=(active,layout,available,hashlib.sha256(json.dumps(source,sort_keys=True).encode()).hexdigest())
        if signature!=self._workspace_layout_signature:
            self._workspace_layout_signature=signature
            self._workspace_layout_revision+=1
        binding=({"workspace_id":active,"layout":layout,"revision":self._workspace_layout_revision,
                  "instance_id":self.instance_id} if available else None)
        existing=state.get("workspace",{})
        if "layout_binding" not in existing or existing["layout_binding"]!=binding:
            self.hub.update_state({"workspace":{"layout_binding":binding}},event_type="workspace.layout_binding_changed")
        return binding

    def state(self, device_id: str) -> dict:
        # PERF-4 §0: this is read straight onto the wire and never mutated, and
        # deep-copying the catalog inside it for every poll was most of what
        # the daemon's CPU went on.
        snapshot = self.hub.state_snapshot(device_id, copy_state=False)
        return self.resource({**snapshot, "event_cursor": self.hub.event_cursor, "instance_id": self.instance_id})

    def register_executor(self, entry_id: str, executor: Callable[[tuple[str, ...]], Any], *, requires_target=False,
                          requires_workspace=False, workspace_preflight=None, with_device=False):
        """Host-owned adapters; window and workspace authority stay distinct.

        MENU-4: `with_device` hands the executor the invoking device as a
        keyword, so an adapter that journals each invocation can say who asked.
        It never receives anything else the client sent.
        """
        identifier(entry_id)
        if with_device and (requires_target or requires_workspace):
            raise ValueError("invalid_device_executor_registration")
        if with_device: self._device_executors.add(entry_id)
        else: self._device_executors.discard(entry_id)
        if entry_id==WORKSPACE_LAYOUT_ENTRY and not requires_workspace:
            raise ValueError("workspace_executor_requires_workspace_binding")
        if requires_workspace:
            if entry_id != WORKSPACE_LAYOUT_ENTRY or requires_target or not callable(workspace_preflight):
                raise ValueError("invalid_workspace_executor_registration")
            self._workspace_preflights[entry_id] = workspace_preflight
        else:
            if workspace_preflight is not None:raise ValueError("invalid_workspace_executor_registration")
            self._workspace_preflights.pop(entry_id,None)
        self._executors[entry_id] = (executor, requires_target)

    def _capture_workspace_action(self, params, device):
        try:
            context=validate_workspace_request(params.get("params"),params.get("state_revision"),params.get("target_token"))
        except WorkspaceActionError as error:
            raise ServiceError(error.code,status=error.status) from None
        if "target_token" in params:raise ServiceError("invalid_workspace_request")
        proof=None
        if "workspace_binding" in params:
            try:proof=validate_workspace_binding(params["workspace_binding"])
            except WorkspaceActionError as error:raise ServiceError(error.code,status=error.status) from None
        reader=self._workspace_preflights.get(WORKSPACE_LAYOUT_ENTRY)
        if reader is None:raise ServiceError("workspace_preflight_unavailable",status=503)
        if self._workspace_adapter is not None:self._workspace_adapter.refresh()
        try:projection=reader()
        except Exception as error:
            self._publish_workspace_layout_binding(self._catalog or {},unavailable=True)
            code=getattr(error,"code","")
            if not isinstance(code,str) or not re.fullmatch(r"workspace_[a-z0-9_]{1,85}",code):code="workspace_preflight_unavailable"
            raise ServiceError(code,status=409) from None
        if (not isinstance(projection,dict) or set(projection)!={"workspace_id","layout"}
                or type(projection["workspace_id"]) is not int or not 1<=projection["workspace_id"]<=10
                or not isinstance(projection["layout"],str) or projection["layout"] not in {"dwindle","scrolling"}):
            self._publish_workspace_layout_binding(self._catalog or {},unavailable=True)
            raise ServiceError("workspace_preflight_unavailable",status=503)
        state=self.hub.state_snapshot(device)
        if proof is None:
            if context.state_revision!=state["revision"]:raise ServiceError("stale_workspace_revision",status=409)
        else:
            binding=state.get("workspace",{}).get("layout_binding")
            if (not isinstance(binding,dict) or proof["instance_id"]!=self.instance_id
                    or proof["revision"]!=binding["revision"] or context.state_revision>state["revision"]
                    or binding["workspace_id"]!=context.workspace_id or binding["layout"]!=context.from_layout):
                raise ServiceError("stale_workspace_binding",status=409)
        active_workspace=state.get("workspace",{}).get("active")
        if type(active_workspace) is not int or active_workspace!=context.workspace_id or projection["workspace_id"]!=context.workspace_id:
            self._publish_workspace_layout_binding(self._catalog or {},unavailable=True)
            raise ServiceError("stale_workspace",status=409)
        if projection["layout"]!=context.from_layout:
            self._publish_workspace_layout_binding(self._catalog or {},unavailable=True)
            raise ServiceError("workspace_layout_changed",status=409)
        return context

    def relative_workspace(self, value) -> int:
        """`e+1` / `e-1` resolved against the collection this host publishes.

        ARCH-1 / Study 04 A-64 and review item 24: a client that computes its own
        neighbour computes it from a snapshot that may already be stale, and then
        switches to the wrong workspace. The only reading that cannot be wrong is
        the one the host just took, so the host takes it. The order is the same
        one the bar draws (`state.workspace.items`, which is
        `Workspaces.qml`'s "the fixed five plus whatever exists"), and it wraps,
        because a bar of squares has no end.
        """
        if value not in ("e+1", "e-1"):
            raise ServiceError("invalid_request")
        snapshot = self.hub.state_view("workspace")["workspace"] or {}
        numbers = sorted({row["id"] for row in (snapshot.get("items") or [])
                          if type(row.get("id")) is int})
        active = snapshot.get("active")
        if not numbers or active not in numbers:
            raise ServiceError("workspace_unavailable", status=409)
        step = 1 if value == "e+1" else -1
        return numbers[(numbers.index(active) + step) % len(numbers)]

    def set_workspace_adapter(self, adapter) -> None:
        self._workspace_adapter = adapter

    def schedule_workspace_refresh(self) -> bool:
        if self._workspace_adapter is None or (self._workspace_probe_task is not None and not self._workspace_probe_task.done()):
            return False
        adapter = self._workspace_adapter
        generation = adapter.generation
        async def run():
            snapshot = await asyncio.to_thread(adapter.inspect)
            # A probe begun before an action must not overwrite its readback.
            if generation == adapter.generation:
                adapter.publish(snapshot)
        self._workspace_probe_task = asyncio.create_task(run())
        return True

    def set_agent_probe(self, probe) -> None:
        """Install a read-only probe owned by the daemon bootstrap."""
        self._agent_probe = probe

    def warm_readings(self) -> None:
        """Take the cold readings the next refresh will want, off the loop.

        PERF-4 §0. Everything here is a subprocess or a sysfs walk that writes
        nothing but its own cache: the menu's shell conditions, and the three
        bar modules (`wpctl`, `omarchy-network-status`, the battery). The
        daemon's maintenance tick calls this through `asyncio.to_thread`, and
        the refresh that follows on the loop finds them already read.
        """
        try:
            self.runtime.warm()
        except Exception:
            pass
        modules = self.bar_modules
        if modules is not None:
            try:
                modules.snapshot()
            except Exception:
                pass
        # MENU-2: `hyprctl -j layers` is a subprocess and `refresh_bar` runs on
        # the loop. Same contract as the bar modules: read here, hit the cache
        # there.
        geometry = self.bar_geometry
        if geometry is not None:
            try:
                geometry.layers()
            except Exception:
                pass

    def schedule_catalog_refresh(self) -> bool:
        """Rebuild the catalog *after* the answer has gone out.

        PERF-4. This used to be a blocking `refresh_catalog(invalidate=True)`
        at the end of `invoke`, so every action a user tapped paid for a full
        rebuild before its own receipt was written - and the client, which
        re-reads the state anyway the moment the `catalog.changed` event lands,
        learned nothing from the wait. Off an event loop (the unit tests) it
        stays synchronous, because there is nothing to schedule onto.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.refresh_catalog(invalidate=True, copy=False)
            return False
        if self._catalog_refresh_task is not None and not self._catalog_refresh_task.done():
            return False
        async def run():
            # One tick of the loop is what lets the response be written first.
            await asyncio.sleep(0)
            try:self.refresh_catalog(invalidate=True, copy=False)
            except Exception:self.hub.update_state({"host":{"catalog_stale":True}},event_type="catalog.source_unavailable")
        self._catalog_refresh_task = loop.create_task(run())
        return True

    def schedule_condition_refresh(self, *, demand: bool = False) -> bool:
        """MENU-3: take the owed condition readings on a worker, then republish.

        `demand=True` is a menu being opened (`panel.summon`, `GET
        /v1/catalog`): the demand-class conditions older than 10 s become owed
        first. Nothing owed, nothing spawned. Off an event loop (the unit
        tests) it runs inline.
        """
        runtime = self.runtime
        if getattr(runtime, "engine", None) is None:
            return False
        if demand and not runtime.demand():
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            runtime.warm()
            self.refresh_catalog(copy=False)
            return True
        if self._condition_task is not None and not self._condition_task.done():
            return False
        async def run():
            try:
                await asyncio.to_thread(runtime.warm)
                self.refresh_catalog(copy=False)
            except Exception:
                pass
        self._condition_task = loop.create_task(run())
        return True

    def schedule_agent_refresh(self) -> bool:
        """Start at most one bounded probe in the background; never blocks the hub ticker."""
        if self._agent_probe is None or (self._agent_probe_task is not None and not self._agent_probe_task.done()):
            return False
        async def run():
            try:
                capabilities, herdr = await asyncio.to_thread(self._agent_probe.inspect)
                self.update_agent(capabilities, herdr)
                return True
            except Exception:
                # Preserve a separately labelled last-confirmed identity while
                # making the current result non-attachable and non-ready. A
                # failed probe is state, not an informational event only.
                current = (self.hub.state_view("agent")["agent"] or {}).get("default_agent", {})
                configured = current.get("configured_kind") or current.get("omarchy_default_agent")
                supported = frozenset(current.get("herdr_supported_kinds", ()))
                from .agent import DefaultAgentCapabilities, HerdrStatusSnapshot, AgentStatus, ProbeStatus
                degraded = DefaultAgentCapabilities(
                    omarchy_default_agent=configured, omarchy_probe=ProbeStatus.UNREADABLE,
                    herdr_supported_kinds=supported, herdr_probe=ProbeStatus.UNREADABLE,
                    default_agent_exists=False, default_agent_probe=ProbeStatus.UNREADABLE,
                    pane_probe=ProbeStatus.UNREADABLE, pane_id=current.get("pane_id"),
                    pane_available=False, agent_status=AgentStatus.UNKNOWN,
                    actual_kind=current.get("actual_kind"), diagnostics=("read_only_probe_failed",),
                )
                degraded_herdr = HerdrStatusSnapshot(
                    server_installed=None, server_running=None, socket_available=None,
                    supported_kinds=supported, agents=(), diagnostics=("read_only_probe_failed",),
                    schema_probe=ProbeStatus.UNREADABLE,
                )
                self.update_agent(degraded, degraded_herdr)
                return False
        self._agent_probe_task = asyncio.create_task(run())
        return True

    async def refresh_agent_now(self) -> bool:
        """Test/integration helper that awaits one scheduled probe."""
        self.schedule_agent_refresh()
        if self._agent_probe_task is None:
            return False
        return bool(await asyncio.shield(self._agent_probe_task))

    def update_agent(self, capabilities, herdr) -> None:
        agent = capabilities.to_dict()
        previous = (self.hub.state_view("agent")["agent"] or {}).get("default_agent", {})
        last_confirmed = previous if previous.get("default_agent_probe") == "available" else (self.hub.state_view("agent")["agent"] or {}).get("last_confirmed")
        herdr_state = herdr.to_dict()
        # Herdr reads a pane; codex reports its own thread. When the provider
        # says it is waiting on a human, that is the truth the badge needs.
        chat = getattr(self, "agent_chat", None)
        waiting = chat.status_override() if chat is not None else None
        previous_chat = (self.hub.state_view("agent")["agent"] or {}).get("chat")
        self.hub.update_state({
            "agent": {"kind": agent["actual_kind"] or agent["configured_kind"],
                      "exists": agent["default_agent_exists"], "pane_available": agent["pane_available"],
                      "status": waiting or agent["agent_status"], "default_agent": agent,
                      **({"chat": previous_chat} if previous_chat else {}),
                      **({"last_confirmed": last_confirmed} if last_confirmed and agent["default_agent_probe"] not in {"available", "missing"} else {"last_confirmed": None})},
            "herdr": {"available": agent["herdr_available"], "agent_count": len(herdr_state["agents"]),
                      "agent_count_scope": "default_agent_only", "snapshot": herdr_state},
        }, event_type="agent.changed")
        self.refresh_catalog()

    async def dispatch_remote_async(self, op, params, device):
        """One entry point for every Remote operation, on IPC and on HTTPS."""
        from .remote.errors import RemoteError
        if not isinstance(device, str) or not device:
            raise ServiceError("pairing_required", status=401)
        if not isinstance(params, dict):
            raise ServiceError("invalid_request")
        payload = dict(params)
        session_id = payload.pop("session_id", None)
        needs_id = op not in {"remote.capabilities", "remote.status", "remote.start", "remote.recover"}
        if needs_id and not isinstance(session_id, str):
            raise ServiceError("invalid_request")
        if op == "remote.start":
            wake = getattr(self, "wake_adapter", None)
            if wake is not None:
                try:
                    state = await asyncio.to_thread(wake.inspect)
                except (OSError, ValueError):
                    raise ServiceError("wake_state_unavailable", status=503) from None
                if any(state.get(key) for key in ("screensaver_active", "display_asleep", "wake_pending")):
                    raise ServiceError("host_waking", status=409)
        try:
            if op == "remote.capabilities":
                return self.resource(await self.remote.capabilities())
            if op == "remote.status":
                return self.resource(await self.remote.status())
            if op == "remote.recover":
                # CORE-2 §2: removing outputs no journal here accounts for is
                # an explicit operator step, never the default.
                orphans = payload.pop("orphans", False)
                if payload or type(orphans) is not bool:
                    raise ServiceError("invalid_request")
                return self.resource(await self.remote.recover(orphans=orphans))
            if op == "remote.start":
                return self.resource({"session": await self.remote.create(device, payload)})
            if op == "remote.get":
                if payload:
                    raise ServiceError("invalid_request")
                return self.resource({"session": await self.remote.get(device, session_id)})
            if op == "remote.resize":
                return self.resource({"session": await self.remote.resize(device, session_id, payload)})
            if op == "remote.backend":
                return self.resource({"session": await self.remote.switch_backend(device, session_id, payload)})
            if op == "remote.heartbeat":
                if payload:
                    raise ServiceError("invalid_request")
                return self.resource(await self.remote.heartbeat(device, session_id))
            if op == "remote.presented":
                return self.resource(await self.remote.presented(device, session_id, payload))
            if op == "remote.stop":
                if payload:
                    raise ServiceError("invalid_request")
                return self.resource(await self.remote.release(device, session_id))
        except RemoteError as error:
            # The Remote subsystem's detail is documented in remote-api.md; it
            # only reaches a client if it survives this translation.
            raise ServiceError(error.code, error.code, error.status, **error.detail) from None
        raise ServiceError("route_unavailable", status=404)

    def dispatch(self, op: str, params: dict, device: str) -> dict:
        if not isinstance(device, str) or not device:
            raise ServiceError("pairing_required", status=401)
        if op == "actions.invoke":
            return self.invoke(params, device)
        if op == "workspace.select":
            fields(params,(),("workspace_id","relative"))
            if ("workspace_id" in params)==("relative" in params):raise ServiceError("invalid_request")
            if "relative" in params:
                number=self.relative_workspace(params["relative"])
            else:
                number=params["workspace_id"]
                if type(number) is not int or not 1<=number<=2147483647:raise ServiceError("invalid_request")
            if self._workspace_adapter is None:raise ServiceError("workspace_unavailable",status=503)
            # Study 04 A-64 / ARCH-1: a workspace square acts on the screen the
            # user is looking at. With a session open that screen is the one we
            # made for it, so the workspace is pulled to the session's own
            # output the same way SHORTCUT-1 rewrites `SUPER+N`; with no session
            # this is the physical screen, unchanged. It used to refuse with
            # `remote_session_required`, which left the Panel's squares dead for
            # the whole session — the one place they are most wanted.
            manager=self.remote.manager
            session=manager.current() if manager is not None else None
            owned=getattr(session,"output_name",None) if session is not None else None
            if session is not None and not (isinstance(owned,str) and owned):
                raise ServiceError("workspace_unavailable",status=409)
            from .graphical import GraphicalUnavailable
            try:self._workspace_adapter.select_existing(number,on_output=owned)
            except GraphicalUnavailable as exc:raise ServiceError(str(exc),status=409) from None
            return self.resource({"workspace":self.hub.state_view("workspace")["workspace"] or {}})
        if op == "panel.summon":
            fields(params, (), ("view",))
            view = params.get("view", "overview")
            if view not in PANEL_VIEWS:
                raise ServiceError("invalid_request")
            self.schedule_condition_refresh(demand=True)
            manager = self.remote.manager
            session = manager.current() if manager is not None else None
            if session is None:
                return {"route": "local", "view": view, "opened": False}
            # The recall names the destination. Without it SUPER+K on the host
            # could only ever reopen the iPad's root panel.
            self.hub.publish("panel.summon", {"session_id": session.id, "revision": session.revision,
                                              "expires_in_seconds": session.ttl_seconds, "view": view},
                             device_id=session.device_id)
            return {"route": "remote", "owner_device_id": session.device_id, "view": view,
                    "session_id": session.id, "revision": session.revision}
        raise ServiceError("route_unavailable", status=404)

    def invoke(self, params: dict, device: str) -> dict:
        fields(params, ("entry_id", "request_id", "catalog_revision"), ("params", "state_revision", "target_token", "workspace_binding", "execution_context"))
        entry_id, request_id = identifier(params["entry_id"]), identifier(params["request_id"])
        if entry_id.startswith('omodachi.shortcut.'):
            provider=getattr(self,'shortcut_provider',None)
            if provider is None:raise ServiceError('route_unavailable',status=404)
            provider.validate_context(params.get('execution_context'),device)
        elif 'execution_context' in params:raise ServiceError('invalid_request')
        if "workspace_binding" in params and entry_id!=WORKSPACE_LAYOUT_ENTRY:raise ServiceError("invalid_request")
        if not isinstance(params.get("params", {}), dict):
            raise ServiceError("invalid_request")
        fingerprint = hashlib.sha256(json.dumps(params, sort_keys=True, allow_nan=False).encode()).hexdigest()
        key = (device, request_id)
        if key in self._requests:
            previous, result = self._requests[key]
            if previous != fingerprint:
                raise ServiceError("request_conflict", status=409)
            return self.resource(deepcopy(result))
        # PERF-5. The optimistic path. This used to be
        # `refresh_sources()` + `refresh_catalog(invalidate=True)`: a bar
        # module snapshot (three subprocesses, throttled to 2 s, so every
        # other tap paid for it), every shell `when`/`checked` in the menu
        # read again, and every provider listing on the machine walked again -
        # the Gio desktop scan, the keybinding records, the font list - all on
        # the event loop, inside the user's tap, before the tap was allowed to
        # do anything. On a healthy host that is ~175 ms of subprocess and on
        # a loaded one it was the better part of two seconds, and it is the
        # only reason the same tap is sometimes instant and sometimes not.
        #
        # What that read was *for* is one row. So read the shelf - the
        # maintenance tick rebuilds it every two seconds - and take the
        # expensive reading again only for the row being invoked, and only
        # when the client's revision says it may have moved. `_resolve_target`
        # below is that rule; the safety it has to keep is in its docstring.
        if hasattr(self, "refresh_sources"):
            # Cheap: a stamp on each source file, and a recompile only when one
            # of them actually moved. A source that stopped parsing marks the
            # catalog stale here, which is what the refusal below reads.
            self.refresh_sources(bar=False)
        snapshot = self.refresh_catalog(allow_expired=True)
        entry = next((row for row in snapshot["entries"] if row["id"] == entry_id), None)
        moved = params["catalog_revision"] != snapshot["revision"]
        if entry is None or moved:
            snapshot, entry = self._resolve_target(entry_id)
            moved = params["catalog_revision"] != snapshot["revision"]
        if (self.hub.state_view("host")["host"] or {}).get("catalog_stale"):
            raise ServiceError("catalog_source_unavailable", "catalog source must be repaired before invoking an action", 409)
        if entry is None:
            # An id the current catalog does not have and never had is a client
            # asking for something that is not there; an id that is gone after
            # the revision moved is the row having been taken away underneath a
            # client that could still see it. Only the second is `stale`.
            if not moved:
                raise ServiceError("route_unavailable", status=404)
            raise ServiceError("stale_catalog_revision", status=409)
        # MENU-3: a `when` nobody could answer (a timeout, a shell that did
        # not start) leaves the row drawn and tappable, as Omarchy's own menu
        # does; the host decides what the tap does. False, or a condition with
        # no evaluator at all, still refuses.
        when_state = (entry.get("conditions") or {}).get("when") or {}
        if entry.get("visible") is not True and not (entry.get("visible") is None
                                                     and when_state.get("status") == "unknown"):
            raise ServiceError("route_unavailable", "condition is false or unavailable", 409)
        # The revision is no longer a gate: the row was re-resolved by id
        # above, so the only thing left to check is that the row still means
        # what it meant. `resolve` answers that (`menu_action_changed`,
        # `menu_source_changed`, `menu_surface_changed`), and it now answers it
        # against readings taken for this row rather than for the whole table.
        resolved = self.policy.resolve(entry)
        if resolved.reason in REDEFINED_ROUTE_REASONS:
            raise ServiceError("stale_target", status=409)
        if not resolved.supported:
            # Without the revision gate in front of it this is the refusal a
            # client sees when the row it named stopped being runnable, so it
            # says so at 409 rather than arriving as a bare `invalid_request`.
            raise ServiceError("route_unavailable", resolved.reason or "route_unavailable", 409)
        descriptor = self.policy.prepare_invocation(entry, params=params.get("params"))
        if entry["route"].get("ready") is not True and descriptor.route != "desktop":
            raise ServiceError("route_unavailable", "route capability is unavailable", 409)
        if descriptor.route == "desktop" and not self.hub.capabilities_snapshot().get("desktop"):
            raise ServiceError("profile_unsupported", "Desktop adapter is not installed", 409)
        if descriptor.route == "host":
            registered = self._executors.get(entry_id)
            if not registered:
                raise ServiceError("route_unavailable", "host executor is not installed", 409)
            executor, needs_target = registered
            workspace_context = self._capture_workspace_action(params,device) if entry_id in self._workspace_preflights else None
            if needs_target:
                # Revalidate live focus before accepting a stale-window-sensitive action.
                if self._workspace_adapter is not None:
                    self._workspace_adapter.refresh()
                state = self.hub.state_snapshot(device)
                if params.get("state_revision") != state["revision"] or not params.get("target_token") or params["target_token"] != state["focus"].get("target_token"):
                    raise ServiceError("stale_target", status=409)
            # Store acceptance before execution: a timed-out side effect must
            # not run twice when a caller retries the same request ID.
            # PERF-5. The revision this was actually resolved against. A
            # client that invoked optimistically with an older one adopts it
            # here, so the next tap is not optimistic about the same gap.
            result = {"request_id": request_id, "entry_id": entry_id, "status": "accepted",
                      "catalog_revision": snapshot["revision"], "route": descriptor.as_dict()}
            self._requests[key] = (fingerprint, deepcopy(result))
            try:
                if workspace_context is not None:
                    effects=validate_workspace_effects(executor(descriptor.argv,workspace_context),workspace_context)
                    result["workspace_effects"]=effects
                    if effects["status"]!="applied":result.update(status="failed",code=effects["code"])
                elif needs_target:
                    executor(descriptor.argv, params["target_token"])
                else:
                    # SHORTCUT-1 item 3. An adapter that can say what the host
                    # did returns it; "accepted" alone made a shortcut that ran
                    # and a shortcut that silently did nothing look identical.
                    outcome = (executor(descriptor.argv, device=device) if entry_id in self._device_executors
                               else executor(descriptor.argv))
                    if outcome is not None:
                        from .shortcut_provider import validate_observation
                        observed = validate_observation(outcome)
                        if observed is not None:
                            result["observed"] = observed
            except Exception as error:
                # SHORTCUT-1 item 3. The acceptance is already stored, so this
                # cannot become an HTTP error without making a retry of the same
                # request ID claim success. It can still say which failure it
                # was: `action_failed` used to cover a shortcut that found no
                # focused window and one the compositor refused alike, and the
                # client had nothing to put on screen but "已发送".
                code = getattr(error, "code", None)
                if workspace_context is not None:
                    code = "workspace_layout_outcome_unknown"
                elif not (isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code)):
                    code = "action_failed"
                result = {**result, "status": "failed", "code": code}
            if workspace_context is not None:
                # Preserve the final partial/full outcome before a secondary
                # catalog refresh. Losing that readback must not turn a retry
                # into another workspace mutation or an accepted-only receipt.
                self._requests[key] = (fingerprint,deepcopy(result))
                self.runtime.after_invoke(entry_id)
                try:self.refresh_catalog(invalidate=True)
                except Exception:self.hub.update_state({"host":{"catalog_stale":True}},event_type="catalog.source_unavailable")
            else:
                self.runtime.after_invoke(entry_id)
                self.schedule_catalog_refresh()
            self.schedule_condition_refresh()
        else:
            # A launch descriptor prepares an SSH/native surface. It does not
            # pretend the client attached, or the command already completed.
            result = {"request_id": request_id, "entry_id": entry_id, "status": "prepared",
                      "catalog_revision": snapshot["revision"], "route": descriptor.as_dict()}
        self._requests[key] = (fingerprint, deepcopy(result))
        while len(self._requests) > 512:
            self._requests.popitem(last=False)
        self.hub.publish("action.result", result, device_id=device)
        return self.resource(result)


    # The official bar always draws 1-5 and adds any other live workspace up to
    # 10 (shell/plugins/bar/widgets/Workspaces.qml:21-31). A self-drawn bar can
    # only show the same set if core publishes which rows are the fixed five.
    PERSISTENT_WORKSPACES = tuple(range(1, 6))

    def set_workspace_snapshot(self, *, active: int | None,
                               window_counts: Mapping[int, int | None],
                               focused_window: Mapping[str, Any] | None = None,
                               snapshot_available: bool = True) -> None:
        """Host-adapter-only update; private window IDs never leave this service."""
        if active is not None and (type(active) is not int or not 1 <= active <= 2147483647):
            raise ValueError("workspace must be a positive compositor ID or unknown")
        if any(type(key) is not int or not 1 <= key <= 2147483647 for key in window_counts):
            raise ValueError("unknown workspace")
        items = []
        observed_ids=set(window_counts)
        if active is not None:observed_ids.add(active)
        observed_ids.update(self.PERSISTENT_WORKSPACES)
        for number in sorted(observed_ids):
            count = window_counts.get(number)
            # A persistent row the compositor did not list exists as far as the
            # bar is concerned and holds nothing - but only when the compositor
            # answered at all. Without a reading, occupancy stays unknown.
            if count is None and number not in window_counts and snapshot_available:
                count = 0
            if count is not None and (type(count) is not int or not 0 <= count <= 10000):
                raise ValueError("window count must be bounded or unknown")
            select = self.runtime.catalog.by_id(f"omodachi.workspace.select.{number}")
            move = self.runtime.catalog.by_id(f"omodachi.workspace.move.{number}")
            items.append({"id": number, "label": f"Workspace {number}",
                          "active": active == number, "window_count": count,
                          "occupied": None if count is None else count > 0,
                          "persistent": number in self.PERSISTENT_WORKSPACES,
                          "select_entry_id": select.id if select else None,
                          "move_entry_id": move.id if move else None})
        focus = {"window": None, "app_id": None, "app_name": None, "target_token": None,
                 "icon": "", "icon_kind": "none"}
        if focused_window is not None:
            host_id = identifier(focused_window.get("id"))
            app_id = focused_window.get("app_id")
            app_name = focused_window.get("app_name")
            for value in (app_id, app_name):
                if value is not None and (not isinstance(value, str) or len(value) > 128 or any(ord(c) < 32 for c in value)):
                    raise ValueError("application summary must be bounded plain text")
            if host_id != self._focused_host_id:
                self._focused_token = uuid.uuid4().hex
            self._focused_host_id = host_id
            self._focused_record = {"id": host_id, "app_id": app_id, "app_name": app_name}
            focus.update(app_id=app_id, app_name=app_name, target_token=self._focused_token)
            # ICON-1: the focused window's own icon, from the same desktop
            # database the Apps submenu is compiled from. A window whose
            # `app_id` matches no installed entry keeps `icon_kind: "none"`
            # and the client draws the generic application glyph.
            found = self.runtime.app_icon(app_id)
            if found is not None:
                focus.update(icon=found[0], icon_kind=found[1])
        else:
            self._focused_host_id = self._focused_token = self._focused_record = None
        patch = {"workspace": {"active": active, "items": items}, "focus": focus}
        current = self.hub.state_view("workspace", "focus")
        # PERF-4 §0: what a `when` expression can possibly depend on is *which*
        # workspace is in front of the user and which ones exist - never how
        # many windows are on them. Window counts move constantly on a desktop
        # somebody is working at, and treating that as "the workspace changed"
        # re-ran `pacman -T`, `omarchy-default-browser` and every other menu
        # condition with it, several times a minute, for ever.
        before_workspace = current.get("workspace", {})
        workspace_moved = (before_workspace.get("active") != patch["workspace"]["active"]
                           or {row["id"] for row in before_workspace.get("items", [])}
                           != {row["id"] for row in patch["workspace"]["items"]})
        workspace_changed = any(before_workspace.get(key) != value for key, value in patch["workspace"].items())
        if workspace_changed or current.get("focus") != patch["focus"]:
            self.hub.update_state(patch, event_type="workspace.changed")
            # The provider listings are never dropped here: a workspace change
            # says nothing about which desktop entries exist, and throwing them
            # away meant a Gio scan of every application on the machine
            # (0.63 s) per workspace the user switched to.
            if workspace_moved:
                self.runtime.invalidate_workspace()
            self.refresh_catalog(copy=False)

    def resolve_window_target(self, token: str) -> dict:
        """An executor resolves the already validated token, never focused pane."""
        if not token or token != self._focused_token or self._focused_record is None:
            raise ServiceError("stale_target", status=409)
        return deepcopy(self._focused_record)

    def search_catalog(self, query: str) -> dict:
        if not isinstance(query, str) or len(query) > 128:
            raise ServiceError("invalid_request", "search query exceeds limit")
        snapshot = self.refresh_catalog()
        # MENU-3: a client reading the menu is a menu being opened.
        self.schedule_condition_refresh(demand=True)
        query = query.strip().casefold()
        if not query:
            return snapshot
        def score(row):
            aliases = [str(value).casefold() for value in row.get("aliases", [])]
            name = str(row.get("label", "")).casefold()
            if query == row["id"].casefold() or query in aliases:
                return 0
            if query == name:
                return 1
            if query in name or any(query in alias for alias in aliases):
                return 2
            return 3
        indexed = [(score(row), index, row) for index, row in enumerate(snapshot["entries"])]
        snapshot["entries"] = [row for rank, index, row in sorted(indexed, key=lambda item: (item[0], item[1])) if rank < 3]
        snapshot["query"] = query
        return snapshot
