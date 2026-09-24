"""Server bootstrap: explicit demo mode or read-only local host sources."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import socket
import time
from .bar import parse_bar_layout
from .bar_geometry import BarGeometry
from .bar_modules import BarModules
from .agent import AgentState, AgentStatus, DefaultAgentCapabilities, HerdrStatusSnapshot, ProbeStatus, ReadOnlyAgentProbe
from .catalog import compile_catalog, load_jsonc
from .catalog_runtime import CatalogRuntime
from .hub import Hub
from .routes import RoutePolicy, RouteDescriptor
from .service import CoreService

DATA = Path(__file__).parent / "data"

OMARCHY_DEFAULT_MENU = Path("/usr/share/omarchy/default/omarchy/omarchy-menu.jsonc")


def create_service(hub: Hub, *, demo=False, default_menu: Path | None = None,
                   user_menu: Path | None = None, omodachi_menu: Path | None = None, shell_config: Path | None = None, agent_probe=None,
                   enable_live_menu=True, live_menu_readers=None, remote_manager=None,
                   remote_manager_factory=None,
                   media_pairing=None, preferences_store=None, audio_session_factory=None,
                   enable_catalog_providers=True, catalog_provider_options=None,
                   condition_engine=None, menu_actions=None):
    from .remote import RemoteManager
    if remote_manager is not None and not isinstance(remote_manager, RemoteManager):
        raise ValueError("remote manager invalid")
    shell_path = shell_config or (DATA / "demo-shell.json" if demo else Path.home() / ".config/omarchy/shell.json")
    installed_default = OMARCHY_DEFAULT_MENU
    # Non-demo startup reads only the installed host menu. A copied source fixture
    # is never selected implicitly; pass --default-menu explicitly for an offline
    # source fixture. This keeps provenance visible and avoids stale source drift.
    default_source = default_menu or (DATA / "demo-menu.jsonc" if demo else installed_default)
    paths = [default_source,
             user_menu or Path.home() / ".config/omarchy/extensions/omarchy-menu.jsonc",
             omodachi_menu or ((Path.home() / ".config/omodachi/omodachi-menu.jsonc")
                 if (Path.home() / ".config/omodachi/omodachi-menu.jsonc").exists()
                 else DATA / "omodachi-menu.jsonc")]
    # Demo is independent of the developer's Omarchy extensions.
    if demo and user_menu is None:
        paths[1] = None
    if demo and omodachi_menu is None:
        paths[2] = DATA / "omodachi-menu.jsonc"
    # PERF-4 §0: the three JSONC sources are re-read every two seconds for ever.
    # Parsing them and compiling the catalog is about 150 ms, and the files
    # change when somebody edits a menu - not twice a minute. This is the
    # filesystem's own answer to "has it changed", and a `None` stamp (a file
    # that cannot be stat'd) always reads, so an unreadable source still raises
    # and still marks the catalog stale.
    source_cache: dict[str, Any] = {"stamp": None, "value": None}
    def source_stamps():
        stamps = []
        for path in paths + [DATA / "omodachi-menu.jsonc"]:
            if path is None:
                stamps.append(None)
                continue
            try:
                info = path.stat()
                stamps.append((str(path), info.st_mtime_ns, info.st_size, info.st_ino))
            except OSError:
                stamps.append((str(path), None, None, None))
        return tuple(stamps)
    def read_sources():
        stamp = source_stamps()
        if source_cache["value"] is not None and source_cache["stamp"] == stamp:
            return source_cache["value"]
        values = []
        for index,path in enumerate(paths):
            # Installed user extensions override packaged Omodachi defaults,
            # but their presence must not hide newly shipped provider entries.
            if index==2 and path!=DATA/'omodachi-menu.jsonc':
                values.append(load_jsonc(DATA/'omodachi-menu.jsonc'))
            values.append(load_jsonc(path) if path and path.exists() else {})
        catalog = compile_catalog(*values)
        source_cache.update(stamp=stamp, value=catalog)
        return catalog
    default_source_status = "fixture" if demo else ("available" if default_source.exists() else "unavailable")
    # PERF-4. Every `when`/`checked` expression in the Omarchy menu is a shell
    # command - `omarchy-hw-webcam`, `omarchy-pkg-present google-chrome`,
    # `[[ "$(omarchy-default-browser)" == "chromium" ]]` - and there are enough
    # of them that one cold pass costs about 0.8 s. The daemon re-reads its
    # sources every two seconds, so a two-second window meant it paid that on
    # every pass, for ever: the daemon this replaced had burned 4 h 59 m of CPU
    # in 5 h 24 m of wall clock. What these expressions answer changes with the
    # hardware and the installed packages, not by the second, and every path
    # that does change one - a source edit, an action the user invoked -
    # invalidates explicitly. The demo host keeps the short default so its
    # fixtures stay immediate.
    runtime = CatalogRuntime(read_sources(), **({} if demo else {"cache_seconds": 15.0}))
    policy = RoutePolicy()
    service = CoreService(hub, runtime=runtime, policy=policy, remote_manager=remote_manager,
                          preferences_store=preferences_store, audio_session_factory=audio_session_factory,
                          unavailable_reason="remote_runtime_unavailable")
    service.media_pairing = media_pairing

    def wire_remote(manager):
        """Give a freshly built manager the host answers only this file has.

        INSTALL-1 §1.3: this used to run inline, exactly once, against the
        manager the daemon happened to have at startup. A manager built later -
        when the graphical session finally exists - has to arrive wired the
        same way or it would plan every session with the defaults instead of
        the user's preferences, so it is a function and the retry path calls
        it too.
        """
        if manager is None:
            return None
        if manager.allow_resize is None:
            manager.allow_resize = lambda: service.preferences_store.get()["values"]["allow_dynamic_resolution"]
        if manager.host_quality is None:
            # The same `profile_defaults.quality` the client reads from
            # GET /v1/preferences, applied where the profile is actually planned.
            manager.host_quality = lambda: service.preferences_store.get()["profile_defaults"]["quality"]
        if manager.host_backend is None:
            # Study 03 open question 5: one host answer for "which backend by
            # default", read from the same store the plugin's Settings page writes.
            manager.host_backend = lambda: service.preferences_store.get()["values"]["remote_backend"]
        if manager.device_names is None:
            # A test or demo authenticator need not keep names; then a second
            # device is told the device_id, which is still true, just less kind.
            manager.device_names = getattr(hub.auth, "device_name", None)
        sunshine = manager.backends.get("sunshine")
        if sunshine is not None and sunshine.certificate_resolver is None:
            # The fork's client identity is the fingerprint an actual pairing produced.
            sunshine.certificate_resolver = service.remote_certificate
        return manager

    wire_remote(remote_manager)
    if remote_manager_factory is not None:
        service.remote._manager_factory = lambda: wire_remote(remote_manager_factory())
    # Demo performs no host reads at all, so it publishes the layout with every
    # module status null - which is also what a host with none of them does.
    bar_modules = None if demo else BarModules()
    # MENU-2. Where that bar is on the session's own output, so the App can put
    # its own mark over the host's Omarchy logo. Demo has no compositor and no
    # session, so it publishes `geometry: null` - which is also what a real host
    # without a session publishes.
    bar_geometry = None if demo else BarGeometry()
    def refresh_bar():
        statuses = bar_modules.snapshot() if bar_modules is not None else None
        try:
            with shell_path.open("rb") as source:
                raw = source.read(65537)
            value = json.loads(raw) if len(raw) <= 65536 else None
            status = "fixture" if demo else "available"
        except (OSError, ValueError):
            value, status = None, "unavailable"
        layout = parse_bar_layout(value, source_status=status, statuses=statuses)
        # REMOTE-SAFE-1 §5 finding: the manager the service holds *now*. The
        # one this function was built with is None whenever the daemon started
        # before the desktop, and a manager built later never had its bar
        # measured - no logo mark on any session after a reboot.
        manager = service.remote.manager if service.remote is not None else remote_manager
        if bar_geometry is not None and manager is not None:
            try:
                geometry = bar_geometry.snapshot(manager.current(), layout["position"],
                                                 {"left": layout["left"]})
            except (OSError, ValueError, TypeError, AttributeError):
                geometry = None
            if geometry is not None:
                layout = parse_bar_layout(value, source_status=status, statuses=statuses,
                                          geometry=geometry)
        if hub.state_view("bar")["bar"] != layout:
            hub.update_state({"bar": layout}, event_type="bar.changed")
    # PERF-4 §0: so the maintenance tick can take these readings on a worker
    # instead of on the event loop. `BarModules` throttles itself to the same
    # two seconds, so the call that follows on the loop is a cache hit.
    service.bar_modules = bar_modules
    service.bar_geometry = bar_geometry
    service.refresh_bar = refresh_bar
    if agent_probe is not None:
        service.set_agent_probe(agent_probe)
    refresh_bar()
    host_patch = {"name": "omarchy-fixture" if demo else socket.gethostname(),
                  "connected": True, "source": "fixture" if demo else "local",
                  "graphical_state": "fixture" if demo else "unavailable"}
    # Existing state schema has a stable stale marker; do not add provenance
    # fields to device state. The source path remains an internal bootstrap value.
    if default_source_status == "unavailable":
        host_patch["catalog_stale"] = True
    hub.update_state({"host": host_patch,
                      "capabilities": {"native": ["shortcuts"] if demo else [], "demo": demo}})
    if demo:
        policy.register("omodachi.herdr", RouteDescriptor("terminal", True, argv=("herdr", "session", "attach", "session-fixture-01")), source_action="")
        toggles = {"notifications": False}
        runtime.register_condition("fixture.notifications", lambda: toggles["notifications"])
        runtime.register_provider("fixture.apps", lambda: [{"id":"apps.demo", "label":"Demo application", "action":"unregistered-demo"}])
        policy.register("trigger.toggle.notifications", RouteDescriptor("host", True, argv=("fixture-toggle-notifications",)), source_action="fixture-toggle-notifications")
        def toggle(argv):
            toggles["notifications"] = not toggles["notifications"]
        service.register_executor("trigger.toggle.notifications", toggle)
        policy.register_terminal("learn.demo", "omarchy-launch-terminal 'printf demo'")
        # Same native shortcuts surface a real host gets from
        # install_shortcut_provider; demo exposes the route without host rows.
        policy.register_native("learn.keybindings", "shortcuts", source_action="omarchy-menu-keybindings")
        for group in ("install", "update", "about"):
            policy.register_terminal(group + ".demo", "omarchy-launch-terminal 'printf " + group + "-demo'")
        # The only directory source remains third-layer JSONC. This registry
        # supplies reviewed behavior; it never synthesizes catalog rows.
        windows = {
            "fixture-terminal": {"id": "fixture-terminal", "app_id": "fixture.terminal", "app_name": "Terminal", "workspace": 1},
            "fixture-editor": {"id": "fixture-editor", "app_id": "fixture.editor", "app_name": "Editor", "workspace": 2},
            "fixture-browser": {"id": "fixture-browser", "app_id": "fixture.browser", "app_name": "Browser", "workspace": 3},
        }
        desktop = {"active": 2, "focus": "fixture-editor"}
        def publish_workspaces():
            counts = {n: sum(window["workspace"] == n for window in windows.values()) for n in range(1, 11)}
            service.set_workspace_snapshot(active=desktop["active"], window_counts=counts,
                focused_window=windows.get(desktop["focus"]))
        def select_workspace(argv):
            number = int(argv[1])
            desktop["active"] = number
            desktop["focus"] = next((ident for ident, window in windows.items() if window["workspace"] == number), None)
            publish_workspaces()
        def move_workspace(argv, token):
            number = int(argv[1])
            target = service.resolve_window_target(token)
            windows[target["id"]]["workspace"] = number
            desktop["focus"] = next((ident for ident, window in windows.items() if window["workspace"] == desktop["active"]), None)
            publish_workspaces()
        for number in range(1, 11):
            for op, command, callback in (("select", "select", select_workspace), ("move", "move-focused", move_workspace)):
                entry_id = f"omodachi.workspace.{op}.{number}"
                # Names describe this in-memory host adapter's arguments, not
                # a subprocess sent to an implicit focused Hyprland target.
                policy.register(entry_id, RouteDescriptor("host", True, argv=("workspace." + op, str(number))),
                                source_action=f"omodachi-host workspace {command} {number}")
                service.register_executor(entry_id, callback, requires_target=op == "move")
        publish_workspaces()
        cap = DefaultAgentCapabilities(omarchy_default_agent="codex", omarchy_probe=ProbeStatus.AVAILABLE,
                                      herdr_supported_kinds=frozenset({"codex", "claude"}), herdr_probe=ProbeStatus.AVAILABLE,
                                      default_agent_exists=True, default_agent_probe=ProbeStatus.AVAILABLE,
                                      pane_id="pane-fixture-01", pane_available=True, pane_probe=ProbeStatus.AVAILABLE, agent_status=AgentStatus.WORKING,
                                      actual_kind="codex")
        herdr = HerdrStatusSnapshot(True, True, True, frozenset({"codex", "claude"}),
                                    (AgentState("default", "codex", AgentStatus.WORKING, "pane-fixture-01", True),), schema_probe=ProbeStatus.AVAILABLE)
        service.update_agent(cap, herdr)
        publish_workspaces()
    else:
        from .graphical import HyprlandWorkspaceAdapter
        from .host_wake import HostWake
        service.wake_adapter=HostWake()
        from .herdr_bridge import HerdrBridge
        service.herdr_bridge=HerdrBridge()
        from .theme import HostTheme
        from .fonts import HostFonts
        from .icons import HostIcons
        service.theme=HostTheme()
        service.fonts=HostFonts()
        service.icons=HostIcons()
        from .audio_uplink import MicrophoneArbiter
        from .voice_service import VoiceService
        # One virtual microphone exists, so the Remote uplink and the voice
        # uplink share the arbiter that decides who holds it.
        arbiter = MicrophoneArbiter()
        service.remote.audio.arbiter = arbiter
        service.voice = VoiceService(hub, factory=audio_session_factory, arbiter=arbiter,
                                     preferences=service.preferences_store)
        from .notifications import NotificationMirror, NotificationService
        service.notification_mirror = NotificationMirror(hub)
        service.notifications = NotificationService(service.notification_mirror)
        from .shortcut_provider import install_shortcut_provider
        install_shortcut_provider(service)
        # CLIP-1. The clipboard bridge reads the same preference the plugin's
        # Settings page writes, and publishes `clipboard.changed` to paired
        # devices. It is constructed here and starts nothing: `apply()` starts
        # the watcher only if the preference already allows it.
        from .clipboard import ClipboardService
        service.clipboard = ClipboardService(
            mode=lambda: service.preferences_store.get()["values"].get("clipboard_sync", "off"),
            publish=lambda event, payload: hub.publish(event, payload))
        service.apply_clipboard_preference()
        adapter = HyprlandWorkspaceAdapter(service)
        service.set_workspace_adapter(adapter)
        adapter.refresh()
        # Fixed argv, never a server start/prompt or implicit focused target.
        service.set_agent_probe(agent_probe or ReadOnlyAgentProbe(session="omodachi"))
        from .agent_lifecycle import DefaultAgentManager
        service.agent_manager = DefaultAgentManager(probe=service._agent_probe)
        from .agent_chat_service import DefaultAgentChatService
        service.agent_chat=DefaultAgentChatService(service.agent_manager,socket.gethostname(),hub=hub)
        policy.register("omodachi.agent", RouteDescriptor("terminal", True,
                        argv=("herdr", "--session", "omodachi", "agent", "attach", "default")), source_action="")
        policy.register("omodachi.herdr", RouteDescriptor("terminal", True,
                        argv=("herdr", "session", "attach", "omodachi")), source_action="")
        if enable_live_menu:
            from .live_menu_adapter import install_live_menu_adapters
            install_live_menu_adapters(service, readers=live_menu_readers)
        if enable_catalog_providers:
            from .catalog_providers import install_catalog_providers, ReviewedProviderActionRunner
            options = dict(catalog_provider_options or {})
            options.setdefault("actions", True)
            if options["actions"]:
                options.setdefault("action_runner", ReviewedProviderActionRunner())
            service.catalog_providers = install_catalog_providers(service, **options)
        # MENU-4. Every menu source row nothing above claimed runs the way
        # Omarchy's own menu runs it. Last, so a reviewed adapter keeps the row
        # it owns. `False` opts out; a mapping is the adapter's options.
        if menu_actions is not False:
            from .menu_actions import install_menu_action_adapter
            install_menu_action_adapter(service, **(menu_actions or {}))
        # MENU-3. Every `when`/`checked`/`disabled` no reviewed adapter answers
        # goes to bash, and the engine decides when each is read again. It is
        # attached after every adapter above is registered, so the first build
        # spawns a shell only for what none of them knows. `False` opts out.
        if condition_engine is not False:
            if condition_engine is None:
                from .conditions import ConditionEngine, PathWatcher
                condition_engine = ConditionEngine(watcher=PathWatcher())
            runtime.attach_conditions(condition_engine)
            hub.condition_engine = condition_engine
            service.condition_engine = condition_engine
    service.refresh_catalog(invalidate=True)
    last_sources = runtime.catalog.revision
    def refresh_sources(*, bar: bool = True):
        """Re-read the menu sources; `bar=False` leaves the bar modules alone.

        PERF-5. The three bar readings are `wpctl`, `omarchy-network-status`
        and the battery, throttled to two seconds - so an invoke that asked
        for them paid for three subprocesses on the event loop about half the
        time it was tapped, and nothing about them decides whether an action
        may run. The source stamp below is four `stat` calls and it does, so
        it stays on the invoke path.
        """
        nonlocal last_sources
        if bar:
            refresh_bar()
        try:
            catalog = read_sources()
            if catalog.revision != last_sources:
                last_sources = catalog.revision
                runtime.catalog = catalog
                runtime.invalidate()
            # MENU-4: a menu edit adds, changes or removes runnable rows.
            if getattr(service, "menu_actions", None) is not None:
                service.menu_actions.sync()
            service.refresh_catalog(copy=False)
        except (OSError, ValueError):
            # Keep last known snapshot but visibly mark it stale until repaired.
            if not (hub.state_view("host")["host"] or {}).get("catalog_stale"):
                hub.update_state({"host": {"catalog_stale": True}}, event_type="catalog.source_unavailable")
                service.refresh_catalog(copy=False)
        else:
            if (hub.state_view("host")["host"] or {}).get("catalog_stale"):
                hub.update_state({"host": {"catalog_stale": False}})
    service.refresh_sources = refresh_sources
    return service
