#!/usr/bin/env python3
"""Rebuild redacted examples and strictly validate the local contract registry.

Run from the checkout: .venv/bin/python scripts/verify_contracts.py [--write]
Only --write changes files, and only known fixtures under contracts/fixtures.
All sources are local implementation models and synthetic, fixed input data.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
FIXTURES = CONTRACTS / "fixtures"
REVISION = "omodachi.v1"
NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)
CHECKED_AT = "2026-09-15T00:00:00Z"
sys.path.insert(0, str(ROOT / "src"))

from omodachi_core.agent import AgentState, AgentStatus, AgentTarget, DefaultAgentCapabilities, HerdrStatusSnapshot, ProbeStatus
from omodachi_core.catalog import compile_catalog_from_jsonc
from omodachi_core.catalog_runtime import CatalogRuntime
from omodachi_core.hub import DeviceEvent, Hub
from omodachi_core.routes import RoutePolicy
from omodachi_core.bootstrap import create_service
from omodachi_core.bar import parse_bar_layout
from omodachi_core.bar_geometry import BarStyle, compose
from omodachi_core.service import CoreService
from omodachi_core.remote.backends import VncBackend
from omodachi_core.remote.profile import EncoderLimits, ProfileRequest, ViewportProfilePlanner
from omodachi_core.remote.session import RemoteManager, RemoteSession
from omodachi_core.auth import DeviceAuthenticator
from omodachi_core.host_identity import HostIdentity
from omodachi_core.pairing import PairingStore

ENCODER = EncoderLimits(4096, 4096, 16_777_216, 60, 40_000, 2, 2)
OWNED_OUTPUT = "OMODACHI-0123456789abcdef"


class _FixtureWayVNC:
    """Synthetic stand-in so the connection example comes from the real adapter."""
    def available(self): return True
    def start(self, output, pixels, logical_size=None):
        self.pixels = dict(pixels)
        return {"port": 5901}

    def settle(self, timeout=4.0):
        # The real instance takes WayVNC's one mid-stream resize here, so the
        # fixture shows a document whose ServerInit is already settled.
        return dict(self.pixels)


def remote_fixtures():
    request = ProfileRequest.from_dict({
        "viewport_points": {"width": 1194, "height": 834}, "orientation": "landscape_left",
        "logical_long_edge": 1280.0, "quality": {"max_pixels": 4000000, "fps": 60, "bitrate_kbps": 20000},
        "decoder": {"max_width": 4096, "max_height": 4096, "max_pixels": 16777216, "max_fps": 60,
                    "max_bitrate_kbps": 40000, "codecs": ["h264"]}})
    # A vnc session, planned the way RemoteManager plans one: the host's own
    # render density, so the owned output carries the device's pixels and
    # WayVNC's opening logical size and its served buffer pixels are the two
    # different numbers a real client has to follow (REMOTE-6).
    profile = ViewportProfilePlanner(OWNED_OUTPUT, encoder=ENCODER, render_density=2.0).plan(request)
    profile = replace(profile, stream_pixels=profile.output_mode_pixels)
    session = RemoteSession(id="rs_" + "0" * 32, device_id="device-fixture-redacted", backend="vnc",
                            mode="extend", state="ready", revision=2, output_name=OWNED_OUTPUT,
                            journal_path="/home/user/.local/state/omodachi/remote/" + OWNED_OUTPUT + ".json",
                            created_at=1789516800.0, ttl_seconds=30.0, placement="right",
                            profile=profile, request=request.to_dict(), position=(1536, 0),
                            last_heartbeat=1030.0, reason="created")
    session.connection = VncBackend(lambda _: _FixtureWayVNC()).prepare(session, profile)
    manager = RemoteManager(hyprland=None, journal_dir=Path("/home/user/.local/state/omodachi/remote"),
                            encoder=ENCODER)
    return {"remote-session.json": resource(session.to_dict()),
            "remote-connection.json": session.connection,
            "remote-capabilities.json": resource(manager.capabilities())}


FIXTURE_IDENTITY = HostIdentity(host_id="0" * 32, host_name="omarchy-fixture",
                                certificate=None, port=8099,
                                addresses=["192.168.1.10"])
# A fixed synthetic fingerprint: the fixture must not depend on a certificate
# that exists only on one developer machine.
FIXTURE_IDENTITY.fingerprint = lambda: "ab" * 32


# A syntactically real ed25519 public key with an all-known body, so the example
# shows the field's shape without publishing anybody's key.
FIXTURE_SSH_KEY = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f"
                   " omodachi-fixture")


# A second synthetic key: UX-4's whole subject is a device that comes back
# holding a different one from the one the host wrote down.
FIXTURE_SSH_KEY_REPLACEMENT = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB8eHRwbGhkYFxYVFBMSERAPDg0MCwoJCAcGBQQDAgEA"
                               " omodachi-fixture-replacement")


def ssh_key_fixture(service: CoreService) -> dict[str, Any]:
    """UX-4 §2. What `PUT /v1/ssh/key` answers, from the real file writer.

    Driven through the same `CoreService.ssh_key` the boundary calls, against a
    temporary home, so the example is the endpoint's own output rather than a
    hand-written copy of it.
    """
    import tempfile
    from omodachi_core.ssh_keys import AuthorizedKeys
    with tempfile.TemporaryDirectory() as directory:
        keys = AuthorizedKeys(Path(directory))
        keys.authorize(FIXTURE_SSH_KEY, "device-fixture-redacted")
        original, service.ssh_keys = service.ssh_keys, keys
        try:
            return {"ssh-key.json": service.ssh_key("device-fixture-redacted",
                                                    {"public_key": FIXTURE_SSH_KEY_REPLACEMENT}),
                    "ssh-key-state.json": service.ssh_key_state("device-fixture-redacted")}
        finally:
            service.ssh_keys = original


def pairing_fixtures(service: CoreService) -> dict[str, Any]:
    """Drive the real pairing store end to end, then redact the credential."""
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        authority = DeviceAuthenticator(secret=b"\x00" * 32, state_path=root / "credentials.json")
        service.pairing = PairingStore(authority, root / "pairing.json")
        invitation = service.pairing.begin()["invitation"]
        row = service.pairing.request(invitation, "device-fixture-redacted", "Fixture iPad",
                                      ssh_public_key=FIXTURE_SSH_KEY)
        service.pairing.decide(row["request_id"], approve=True)
        # What a local Approve records once the streaming grant and the
        # authorized_keys line have landed.
        service.pairing.grant(row["request_id"], media=True, ssh=True)
        claim = service.pairing_claim(row["request_id"], {"request_secret": row["request_secret"]})
    claim["request_id"] = "pair_" + "0" * 32
    # The daemon's own Unix account exists only on one machine.
    claim["ssh"] = {**claim["ssh"], "user": "fixture-user"}
    claim["credential"] = "fixture-redacted-device-credential"
    claim["issued_at"] = int(NOW.timestamp())
    claim["credential_expires_at"] = int(NOW.timestamp()) + authority.ttl_seconds
    claim["expires_at"] = int(NOW.timestamp()) + PairingStore.TTL
    return {"pairing-claim.json": claim, **credential_fixtures()}


def credential_fixtures() -> dict[str, Any]:
    """CORE-2 §1: the credential read, the renewal and a refusal, from the real authority."""
    from omodachi_core.auth import CredentialError
    from omodachi_core.network import error_response, renewal_document
    issued = int(NOW.timestamp())
    authority = DeviceAuthenticator(secret=b"\x00" * 32)
    token = authority.issue("device-fixture-redacted", now=issued).token
    due = issued + authority.ttl_seconds - authority.renew_window_seconds + 3600
    info = resource(authority.credential_info(token, due))
    renewed = authority.renew(token, due)
    renewal = resource(renewal_document(authority, renewed))
    renewal["credential"] = "fixture-redacted-device-credential"
    try:
        authority.verify(token, issued + authority.ttl_seconds)
    except CredentialError as refused:
        response = error_response("permission_denied", "invalid or revoked device credential", 401,
                                  reason=refused.reason)
        expired = json.loads(response.body)
    return {"pairing-credential.json": info, "pairing-renew.json": renewal,
            "http-error-credential-expired.json": expired}


def resource(value: dict[str, Any]) -> dict[str, Any]:
    return {**value, "contract_revision": REVISION}


def theme_font_herdr_fixtures() -> dict[str, Any]:
    """Drive the real readers over fixture inputs, never over this machine.

    The theme tree is a synthetic palette plus Omarchy's own shell template, so
    the example shows the backfill working without pinning any host's colours.
    The font rows hash three synthetic files, and the Herdr layout is projected
    from one recorded `api snapshot` envelope.
    """
    from omodachi_core.theme import HostTheme
    from omodachi_core.fonts import HostFonts
    from omodachi_core.herdr_bridge import HerdrBridge
    theme_root = FIXTURES / "theme"
    theme = HostTheme(theme_root, shell_template=theme_root / "shell.toml.tpl")
    fonts_root = FIXTURES / "fonts"
    listing = (fonts_root / "fc-list.txt").read_text().replace(
        "contracts/fixtures/fonts/", str(fonts_root) + "/")
    # TERM-1. A fake fontconfig, so the example carries a fallback chain without
    # pinning this machine's: `fc-match` answers for every probe (that is what
    # matching means) and `fc-list :charset=` confirms only some of them.
    probes = json.loads((fonts_root / "fc-fallback.json").read_text())["probes"]

    def font_runner(argv):
        if argv[0].endswith("omarchy-font-current"):
            return "FixtureMono Nerd Font\n"
        if argv[0].endswith("fc-match"):
            answer = probes[argv[1].removeprefix("monospace:charset=")]
            return f"{answer['family']}\t{fonts_root / answer['file']}\n"
        if len(argv) == 3 and argv[1].startswith(":charset="):
            answer = probes[argv[1].removeprefix(":charset=")]
            return f"{fonts_root / answer['file']}: \n" if answer["covers"] else ""
        return listing

    rows = HostFonts(icon_font=fonts_root / "omarchy.ttf", runner=font_runner).snapshot()
    for row in rows["fonts"]:
        # The example must not carry this checkout's absolute path.
        row["path"] = "/usr/share/fonts/fixture/" + Path(row["path"]).name
    snapshot = json.loads((FIXTURES / "herdr/snapshot.json").read_text())
    bridge = HerdrBridge(runner=lambda argv: snapshot)
    # HERDR-2. The listing is the recorded `herdr session list --json`
    # document; the counts come from the same recorded snapshot, so the example
    # shows the two shapes a client sees without reading this machine.
    from omodachi_core.herdr_bridge import HerdrSessions
    listing = json.loads((FIXTURES / "herdr/session-list.json").read_text())
    rows_by_name = {row["name"]: row for row in HerdrSessions(runner=lambda argv: listing).rows()}
    sessions = []
    for name, row in rows_by_name.items():
        entry = {"name": name, "running": row["running"], "owned": row["owned"],
                 "herdr_default": row["herdr_default"], "readable": False,
                 "workspaces": None, "tabs": None, "panes": None, "agents": None,
                 "protocol": None, "version": None}
        if row["running"]:
            shape = snapshot["snapshot"]
            entry |= {"readable": True, "workspaces": len(shape["workspaces"]),
                      "tabs": len(shape["tabs"]), "panes": len(shape["panes"]),
                      "agents": len(shape["agents"]), "protocol": shape["protocol"],
                      "version": shape["version"]}
        sessions.append(entry)
    return {"theme.json": theme.snapshot() | {"contract_revision": REVISION},
            "fonts.json": resource(rows),
            "herdr-layout.json": resource(bridge.layout()),
            "herdr-sessions.json": resource({"selected": "omodachi", "owned": "omodachi",
                                             "sessions": sessions})}


#: SHORTCUT-1. Synthetic records in the exact three-field shape the host's
#: `output_binding_records` prints, one per execution kind plus the one row
#: Omarchy publishes with no binding at all.
FIXTURE_BINDING_RECORDS = "\n".join((
    "SUPER + SPACE                       \u2192 Omarchy menu\texec\tomarchy-menu toggle",
    "SUPER + RETURN                      \u2192 Terminal\texec\tomarchy-launch-terminal",
    "SUPER + 3                           \u2192 Switch to workspace 3\tlua\thl.dsp.focus({ workspace = \"3\" })",
    "SUPER + W                           \u2192 Close window\tlua\thl.dsp.window.close()",
    "SHIFT ALT + D                       \u2192 Download Video from Web App\tsendshortcut\tSHIFT ALT,D,",
    "SUPER + C                           \u2192 Universal copy\t\t",
)) + "\n"

FIXTURE_WORKSPACE = {"id": 3, "name": "3", "monitor": "OMODACHI-0123456789abcdef"}
FIXTURE_WINDOW = {"address": "0x0000000000000001", "class": "kitty", "title": "redacted",
                  "fullscreen": 0, "floating": False, "workspace": {"id": 3, "name": "3"}}


def shortcut_fixtures(service):
    """The keybinding listing and one accepted receipt, from the real adapter."""
    from omodachi_core.shortcut_provider import install_shortcut_provider

    def runner(argv, env):
        if argv[:3] == ("/usr/bin/hyprctl", "-j", "activeworkspace"): return json.dumps(FIXTURE_WORKSPACE)
        if argv[:3] == ("/usr/bin/hyprctl", "-j", "activewindow"): return json.dumps(FIXTURE_WINDOW)
        return "0"

    install_shortcut_provider(service, reader=lambda: FIXTURE_BINDING_RECORDS, runner=runner,
                              environment=lambda: {}, sleeper=lambda seconds: None)
    listing = service.shortcuts_snapshot()
    row = next(item for item in listing["items"] if item["shortcut_display"] == "SUPER + 3")
    receipt = service.dispatch("actions.invoke", {"entry_id": row["action_ref"],
        "request_id": "request-fixture-shortcut", "catalog_revision": service.refresh_catalog()["revision"],
        "params": {}, "execution_context": {"surface": "omarchy"}}, "device-fixture-redacted")
    return {"shortcuts.json": listing, "action-accepted-shortcut.json": receipt}


def menu_action_fixtures():
    """MENU-4: every route the menu-action adapter gives Omarchy 4.0.3's own
    default menu, and one row run through it, from the real adapter with a
    fake session. `about` is the receipt; `system.*` carries `confirm`."""
    from omodachi_core.menu_actions import install_menu_action_adapter

    catalog = compile_catalog_from_jsonc(FIXTURES / "catalog" / "omarchy-default-v4.0.3.jsonc")
    service = CoreService(Hub(), runtime=CatalogRuntime(catalog))
    window = {"value": None}

    def runner(argv, env):
        if argv == ("/usr/bin/hyprctl", "-j", "activeworkspace"): return json.dumps(FIXTURE_WORKSPACE)
        if argv == ("/usr/bin/hyprctl", "-j", "activewindow"): return json.dumps(window["value"] or {})
        raise ValueError(argv)

    def spawner(action, env, entry_id):
        window["value"] = FIXTURE_WINDOW
        return {"pid": 4242, "exited": True, "exit_code": 0}

    install_menu_action_adapter(service, environment=lambda: {"HOME": "/home/fixture"}, spawner=spawner,
                                runner=runner, journal=lambda entry: None, clock=lambda: NOW.timestamp())
    snapshot = service.refresh_catalog()
    routes = [row["route"] for row in snapshot["entries"] if row.get("action")]
    receipt = service.dispatch("actions.invoke", {"entry_id": "about", "request_id": "request-fixture-menu-action",
                                                  "catalog_revision": snapshot["revision"], "params": {}},
                               "device-fixture-redacted")
    return {"route-descriptors-menu-actions.json": resource({"catalog_revision": snapshot["revision"],
                                                             "source_revision": catalog.revision,
                                                             "routes": routes}),
            "action-accepted-menu-action.json": receipt}


def generated_fixtures() -> dict[str, Any]:
    """Generate wire-shape examples using the actual serializers/compilers."""
    menu = FIXTURES / "catalog"
    catalog = compile_catalog_from_jsonc(menu / "default-omarchy-menu.jsonc", menu / "user-omarchy-menu.jsonc", menu / "omodachi-menu.jsonc")
    policy = RoutePolicy()
    catalog_value = CatalogRuntime(catalog).refresh()
    for entry in catalog_value["entries"]:
        entry["route"] = policy.resolve(entry).as_dict()
    catalog_value = resource(catalog_value)
    fixtures: dict[str, Any] = {"catalog-layered.json": catalog_value,
        "catalog/catalog.expected.json": {
            "notes": "Generated from the local JSONC compiler; all input rows are synthetic fixture data.",
            "required_ids": [entry.id for entry in catalog.entries],
            "route_expectations": {"omodachi.desktop": "desktop", "omodachi.agent": "terminal",
                "omodachi.herdr": "terminal", "setup.monitors": "native", "trigger.fixture-host": "host"},
        },
    }
    route_rows = [policy.resolve(entry.as_dict()).as_dict() for entry in catalog.entries if entry.action or entry.surface]
    # Include an explicit host descriptor from the same catalog rather than
    # inventing a client command, so all four surface values are represented.
    for entry in catalog.entries:
        if not any(row["route"] == "host" for row in route_rows):
            route_rows.append(policy.resolve(entry.as_dict()).as_dict())
    fixtures["route-descriptors.json"] = resource({"catalog_revision": catalog.revision, "routes": route_rows})

    supported = frozenset({"claude", "codex", "grok", "hermes", "opencode"})
    agent = DefaultAgentCapabilities(
        omarchy_default_agent="codex", omarchy_probe=ProbeStatus.AVAILABLE,
        herdr_supported_kinds=supported, herdr_probe=ProbeStatus.AVAILABLE,
        default_agent_exists=True, default_agent_probe=ProbeStatus.AVAILABLE,
        pane_id="pane-fixture-01", pane_available=True, pane_probe=ProbeStatus.AVAILABLE,
        agent_status=AgentStatus.WORKING, actual_kind="codex", checked_at=CHECKED_AT,
    )
    fixtures["default-agent.json"] = resource(agent.to_dict())
    for name, status in (("blocked", AgentStatus.BLOCKED), ("done", AgentStatus.DONE)):
        values = agent.to_dict()
        values["agent_status"] = status.value
        fixtures[f"default-agent-{name}.json"] = resource(DefaultAgentCapabilities.from_snapshot(values).to_dict())
    mismatch = agent.to_dict()
    mismatch.update(actual_kind="claude", agent_status="blocked")
    fixtures["default-agent-kind-mismatch.json"] = resource(DefaultAgentCapabilities.from_snapshot(mismatch).to_dict())
    missing = agent.to_dict()
    missing.update(default_agent_exists=False, default_agent_probe="missing", actual_kind=None,
                   pane_id=None, pane_available=False, pane_probe="unreadable", agent_status="unknown")
    fixtures["default-agent-missing.json"] = resource(DefaultAgentCapabilities.from_snapshot(missing).to_dict())
    herdr = HerdrStatusSnapshot(
        server_installed=True, server_running=True, socket_available=True,
        supported_kinds=supported,
        agents=(AgentState("default", "codex", AgentStatus.WORKING, "pane-fixture-01", True,
                           AgentTarget("default", "pane-fixture-01")),),
        pane_count=2, checked_at=CHECKED_AT, schema_probe=ProbeStatus.AVAILABLE,
    )
    fixtures["herdr.json"] = resource(herdr.to_dict())
    shell = json.loads((ROOT / "src/omodachi_core/data/demo-shell.json").read_text())
    fixtures["bar.json"] = parse_bar_layout(shell, source_status="fixture")
    fixtures["bar-unavailable.json"] = parse_bar_layout(None, source_status="unavailable")
    # MENU-2 / UX-3 §3. The same projection with the geometry a live session
    # publishes: the `omarchy-bar` layer on the owned output and the logo slot
    # inside its leading end. The plugin's own slot is deliberately not located.
    #
    # The style is the one that goes with a 30-thick bar, because that is what
    # this fixture's layer says: `size-horizontal` 26 at `base-size` 14 rounds
    # to 30, and the same font scale gives a 9 px leading margin and a 32 px
    # icon slot. A fixture whose thickness and whose slot came from different
    # font scales would describe a host that does not exist.
    fixtures["bar-geometry.json"] = parse_bar_layout(
        shell, source_status="fixture",
        geometry=compose(output=OWNED_OUTPUT, logical_size={"width": 1280, "height": 894},
                         position="top",
                         layer={"x": 0.0, "y": 0.0, "width": 1280.0, "height": 30.0},
                         sections={"left": parse_bar_layout(shell, source_status="fixture")["left"]},
                         style=BarStyle.from_tokens({"font": {"base-size": 14}})))

    # Exercise the actual packaged demo bootstrap, including its three-layer
    # menu, registered routes and owned shell layout source. Demo never reads
    # user configuration or runs real host operations; overwrite only variable
    # probe timestamps with the same fixed synthetic model data used above.
    hub = Hub()
    service = create_service(hub, demo=True)
    service.install_host_identity(FIXTURE_IDENTITY)
    fixtures["health.json"] = service.resource(hub.dispatch("health", {}))
    fixtures.update(pairing_fixtures(service))
    fixtures.update(ssh_key_fixture(service))
    service.update_agent(agent, herdr)
    hub.update_state({"host": {"name": "omarchy-fixture", "connected": True,
                              "source": "fixture", "graphical_state": "fixture"},
                      "workspace": {"active": 2}, "focus": {"window": None}})
    state = service.state("device-fixture-redacted")
    # Normalize the intentionally per-process identity for repeatability.
    state["instance_id"] = "daemon-instance-fixture"
    raw_focus_token = state.get("focus", {}).get("target_token")
    if raw_focus_token is not None:
        state["focus"]["target_token"] = "window-token-fixture-01"
    ipc_state = hub.dispatch("state", {}, "device-fixture-redacted")
    ipc_state["instance_id"] = "daemon-instance-fixture"
    if ipc_state.get("focus", {}).get("target_token") is not None:
        ipc_state["focus"]["target_token"] = "window-token-fixture-01"
    fixtures["state.json"] = state
    fixtures["capabilities.json"] = service.resource(hub.capabilities_snapshot())
    fixtures["herdr-resource.json"] = service.resource(hub.herdr_snapshot())
    # Every handed-off component belongs to this same effective host snapshot:
    # readiness is part of the service catalog revision used by invocation.
    catalog_value = service.refresh_catalog()
    fixtures["catalog.json"] = catalog_value
    fixtures["catalog-search-3.json"] = service.search_catalog("3")
    fixtures["route-descriptors.json"] = resource({
        "catalog_revision": catalog_value["revision"], "source_revision": service.runtime.catalog.revision,
        "routes": [row["route"] for row in catalog_value["entries"] if row.get("action") or row.get("surface")],
    })

    event = next(event for event in reversed(hub.events_since(0, device_id="device-fixture-redacted"))
                 if event.type == "catalog.changed")
    event_data = asdict(event)
    event_data["ts"] = NOW.timestamp()
    fixtures["event-catalog-changed.json"] = resource(event_data)
    fixtures["event-resync-required.json"] = resource(asdict(DeviceEvent(
        2, "resync_00000002", "resync.required", "device-fixture-redacted",
        {"since": 0, "cursor": 2, "reason": "history_gap", "snapshot_required": True,
         "instance_id": "daemon-instance-fixture"}, NOW.timestamp())))
    # ARCH-1 / A-59: the third destination. The recall is published by the same
    # dispatch a host bar click takes, so the example cannot drift from the
    # constant `PANEL_VIEWS` or from the payload the service actually sends.
    from types import SimpleNamespace as _Namespace
    from omodachi_core.protocol import PANEL_VIEWS as _PANEL_VIEWS
    _previous_manager = service.remote.manager
    service.remote.manager = _Namespace(current=lambda: _Namespace(
        id="rs_" + "0123456789abcdef" * 2, device_id="device-fixture-redacted", revision=4,
        ttl_seconds=60.0))
    assert "settings" in _PANEL_VIEWS
    service.dispatch("panel.summon", {"view": "settings"}, "device-fixture-redacted")
    service.remote.manager = _previous_manager
    summon = next(event for event in reversed(hub.events_since(0, device_id="device-fixture-redacted"))
                  if event.type == "panel.summon")
    summon_data = asdict(summon)
    summon_data["ts"] = NOW.timestamp()
    fixtures["event-panel-summon.json"] = resource(summon_data)
    # CLIP-1: the change announcement, taken from the real watcher path so the
    # example cannot drift from what `ClipboardService.changed` publishes - and
    # so that the fixture demonstrates, in the registry itself, that the text
    # is not in it.
    from omodachi_core.clipboard import ClipboardService as _ClipboardService
    _ClipboardService(mode=lambda: "both", environment=lambda: {"PATH": "/usr/bin"},
                      runner=lambda argv, environment, **options: (0, b"hello world"),
                      publish=hub.publish).changed()
    change = next(event for event in reversed(hub.events_since(0)) if event.type == "clipboard.changed")
    change_data = asdict(change)
    change_data["ts"] = NOW.timestamp()
    fixtures["event-clipboard-changed.json"] = resource(change_data)
    fixtures.update(remote_fixtures())
    fixtures.update(agent_chat_fixtures())
    fixtures.update(voice_fixtures())
    fixtures.update(notification_fixtures())
    fixtures.update(auth_fixtures())
    fixtures.update(theme_font_herdr_fixtures())
    fixtures["wss-snapshot.json"] = {"type": "snapshot", "state": state,
        "instance_id": "daemon-instance-fixture", "cursor": state["event_cursor"]}
    fixtures["wss-ready.json"] = {"type": "ready", "instance_id": "daemon-instance-fixture",
        "cursor": state["event_cursor"]}
    fixtures["wss-event.json"] = {"event": event_data,
        "instance_id": "daemon-instance-fixture", "after_cursor": event.seq - 1}
    action = service.dispatch("actions.invoke", {"entry_id": "omodachi.agent",
        "request_id": "request-fixture-prepared", "catalog_revision": service.refresh_catalog()["revision"]},
        "device-fixture-redacted")
    fixtures["action-prepared.json"] = action
    fixtures.update(shortcut_fixtures(create_service(Hub(), demo=True)))
    fixtures.update(menu_action_fixtures())
    token = "fixture-invalid-token-not-a-credential"
    fixtures["ipc-request-state.json"] = {"op": "state", "token": token}
    third = next(row for row in state["workspace"]["items"] if row["id"] == 3)
    fixtures["ipc-request-workspace-select.json"] = {"op": "actions.invoke", "token": token,
        "entry_id": third["select_entry_id"], "request_id": "request-fixture-workspace-select",
        "catalog_revision": catalog_value["revision"], "params": {}}
    fixtures["ipc-request-workspace-relative.json"] = {"op": "workspace.select", "token": token,
        "relative": "e+1"}
    fixtures["ipc-request-workspace-move.json"] = {"op": "actions.invoke", "token": token,
        "entry_id": third["move_entry_id"], "request_id": "request-fixture-workspace-move",
        "catalog_revision": catalog_value["revision"], "params": {},
        "state_revision": state["revision"], "target_token": state["focus"]["target_token"]}
    fixtures["ipc-request-actions.json"] = {"op": "actions.invoke", "token": token,
        "entry_id": "omodachi.agent", "request_id": "request-fixture-01",
        "catalog_revision": catalog_value["revision"], "params": {}}
    fixtures["ipc-response-state.json"] = {"ok": True, "result": ipc_state}
    fixtures["ipc-response-error.json"] = {"ok": False, "error": "ValueError", "message": "invalid device credential"}
    fixtures["ipc-event.json"] = {"event": event_data, "instance_id": "daemon-instance-fixture", "after_cursor": event.seq - 1}
    fixtures["ipc-request-subscribe.json"] = {"op": "events.subscribe", "token": token,
        "since": state["event_cursor"], "instance_id": "daemon-instance-fixture"}
    fixtures["http-error-stale-target.json"] = resource({"error": {"code": "stale_target", "message": "Refresh the catalog and target before invoking."}})
    # ICON-1. The one error a client is expected to act on rather than report:
    # `detail.fallback` names the glyph to draw where the host has no picture.
    from omodachi_core.icons import HostIcons
    fixtures["http-error-icon-not-found.json"] = resource(
        {"error": {"code": "icon_not_found", "message": "icon_not_found",
                   "detail": {"fallback": HostIcons.FALLBACK}}})
    return fixtures


def agent_chat_fixtures():
    """Drive the real adapter with a controlled peer; no daemon, no thread.

    Every document below comes out of the shipped serializers: the approval
    rows are produced by the same `_server_request` path a real codex prompt
    takes, and the usage numbers by the same notification handlers.
    """
    import asyncio
    from omodachi_core.agent_chat_provider import CodexDefaultAgentChat, DefaultAgentBinding

    class Peer:
        def __init__(self):
            self.ordinal = 0
            self.on_notification = lambda value, ordinal: None
            self.on_server_request = lambda value: None
            self.on_disconnect = lambda: None
            self.answers = []
        async def request(self, method, params):
            result, _ = await self.request_fenced(method, params)
            return result
        async def request_fenced(self, method, params):
            self.ordinal += 1
            if method == "thread/loaded/list":
                return {"data": ["thread-fixture-01"]}, self.ordinal
            if method == "model/list":
                return {"data": [
                    {"id": "gpt-6-astra", "model": "gpt-6-astra", "displayName": "GPT-6-Astra",
                     "description": "Our most capable model for complex, demanding work.",
                     "hidden": False, "isDefault": True, "defaultReasoningEffort": "medium",
                     "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "medium"},
                                                   {"reasoningEffort": "high"}]}]}, self.ordinal
            return {"thread": {"id": "thread-fixture-01", "turns": []}}, self.ordinal
        async def respond(self, request_id, result):
            self.answers.append((request_id, result))
        async def respond_error(self, request_id, code, message):
            self.answers.append((request_id, code))
        async def close(self):
            pass

    async def build():
        peer = Peer()
        binding = DefaultAgentBinding("omarchy-fixture", "thread-fixture-01",
                                      "ws://127.0.0.1:47311", "f" * 64)
        chat = CodexDefaultAgentChat(binding, peer)
        await chat.attach()
        peer.on_server_request({"id": 7, "method": "item/commandExecution/requestApproval",
                                "params": {"threadId": "thread-fixture-01", "itemId": "item-fixture-01",
                                           "turnId": "turn-fixture-01", "command": "ls -la",
                                           "cwd": "/home/fixture/workspace", "kind": "command"}})
        peer.on_notification({"method": "thread/status/changed", "params": {
            "threadId": "thread-fixture-01",
            "status": {"type": "active", "activeFlags": ["waitingOnApproval"]}}}, 10)
        peer.on_notification({"method": "thread/tokenUsage/updated", "params": {
            "threadId": "thread-fixture-01", "turnId": "turn-fixture-01",
            "tokenUsage": {"last": {"inputTokens": 1200, "cachedInputTokens": 900, "cacheWriteInputTokens": 0,
                                    "outputTokens": 240, "reasoningOutputTokens": 120, "totalTokens": 1440},
                           "total": {"inputTokens": 24000, "cachedInputTokens": 18000, "cacheWriteInputTokens": 0,
                                     "outputTokens": 3100, "reasoningOutputTokens": 1400, "totalTokens": 27100},
                           "modelContextWindow": 400000}}}, 11)
        peer.on_notification({"method": "account/rateLimits/updated", "params": {"rateLimits": {
            "planType": "pro", "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1789700000},
            "secondary": {"usedPercent": 41, "windowDurationMins": 10080, "resetsAt": 1790200000}}}}, 12)
        peer.on_notification({"method": "thread/settings/updated", "params": {
            "threadId": "thread-fixture-01", "settings": {"model": "gpt-6-astra", "effort": "high"}}}, 13)
        events = []
        while not chat.events.empty():
            events.append(chat.events.get_nowait())
        requested = next(row for row in events if row["event"]["type"] == "agent.approval.requested")
        models = await chat.models()
        resolved_event = None
        await chat.resolve_approval("7", decision="accept")
        while not chat.events.empty():
            row = chat.events.get_nowait()
            if row["event"]["type"] == "agent.approval.resolved":
                resolved_event = row
        snapshot = chat.snapshot_value()
        peer.on_server_request({"id": 8, "method": "item/commandExecution/requestApproval",
                                "params": {"threadId": "thread-fixture-01", "itemId": "item-fixture-02",
                                           "turnId": "turn-fixture-01", "command": "rm -rf build",
                                           "cwd": "/home/fixture/workspace", "kind": "command"}})
        approvals = resource({"agent_id": "default", "requests": chat.approvals_value()})
        usage = resource({"agent_id": "default", "usage": chat.usage_value(), "status": chat.status_value()})
        return {"agent-chat-snapshot.json": snapshot,
                "agent-chat-event-requested.json": requested,
                "agent-chat-event-resolved.json": resolved_event,
                "agent-chat-approvals.json": approvals,
                "agent-chat-usage.json": usage,
                "agent-chat-models.json": resource(models)}

    return asyncio.run(build())


def voice_fixtures():
    """Synthetic host, real serializers: no Voxtype and no audio device."""
    import asyncio
    import tempfile
    from omodachi_core.voice import VoxtypeHost
    from omodachi_core.voice_service import VoiceService

    class Preferences:
        def get(self):
            return {"values": {"voice_uplink": True}}

    class Host(VoxtypeHost):
        def capabilities(self):
            return {"installed": True, "supported": True, "wait_supported": True,
                    "service_active": True, "state": "idle", "device": "default",
                    "source_name": "omodachi_mic", "devices": ["default", "pipewire"],
                    "names_sources": False, "levels": True, "install_command": None,
                    "reason": None}

    def factory():
        raise AssertionError("fixtures never create an audio device")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        host = Host(config_path=root / "config.toml", runtime_dir=root / "voxtype",
                    cache_dir=root / "cache")
        voice = VoiceService(None, factory=factory, host=host, preferences=Preferences())
        return {"voice-capabilities.json": resource(asyncio.run(voice.capabilities("device-fixture-redacted"))),
                "voice-dictation.json": resource({"target": "client", "route": "default_source",
                                                  "text": "make the bar taller", "chars": 19,
                                                  "status": "ok", "delivered_to_host": None}),
                "event-voice-transcript.json": resource(asdict(DeviceEvent(
                    4, "evt_00000004", "voice.transcript", "device-fixture-redacted",
                    {"text": "make the bar taller", "chars": 19, "status": "ok"}, NOW.timestamp())))}


def notification_fixtures():
    """Real files in a temporary directory, read by the shipped mirror."""
    import json as json_module
    import tempfile
    from omodachi_core.notifications import NotificationMirror

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "notifications"
        (root / "history").mkdir(parents=True)
        (root / "1789711988810-1.json").write_text(json_module.dumps({
            "id": 1, "originalId": 1, "app": "Omodachi", "appIcon": "",
            "summary": "\u201cQA companion\u201d wants to connect",
            "body": "Open Omodachi to approve or reject it.", "image": "", "glyph": "",
            "execArgv": "[\"omarchy-shell\",\"shell\",\"summon\",\"com.omodachi.host\",\"{}\"]",
            "urgency": 2, "expireTimeout": 30000, "timestamp": 1789711988810}))
        (root / "history/1789608484220-52.json").write_text(json_module.dumps({
            "id": 52, "app": "omarchy-action", "summary": "Theme set", "body": "gruvbox",
            "glyph": "", "urgency": 1, "expireTimeout": 5000, "timestamp": 1789608484220}))
        mirror = NotificationMirror(None, state_dir=root)
        mirror.scan()
        listing = resource(mirror.history())
        posted = next(row for row in listing["notifications"] if row["id"] == "1789711988810-1")
        return {"notifications.json": listing,
                "notification-action.json": resource({"id": "1789711988810-1", "action": "dismiss",
                                                      "result": "ok"}),
                "notifications-dnd.json": resource({"dnd": False}),
                "event-notification-posted.json": resource(asdict(DeviceEvent(
                    3, "evt_00000003", "notification.posted", None, posted, NOW.timestamp())))}


def auth_fixtures():
    """AUTH-1, from the real broker: one approval, its resolution, the key list.

    The device half is a throwaway P-256 key generated here, so the fixture is
    an actual signed approval and not a hand-written picture of one.
    """
    import asyncio
    import base64
    import hashlib
    import itertools
    import secrets
    import tempfile
    import types
    from omodachi_core import biometric
    from omodachi_core.biometric import (ApprovalBroker, BiometricKeyStore, _G, _N, _multiply,
                                         approval_message, enrollment_message)

    def sign(private, message):
        digest = int.from_bytes(hashlib.sha256(message).digest(), "big")
        while True:
            k = secrets.randbelow(_N - 1) + 1
            r = _multiply(k, _G)[0] % _N
            if not r:
                continue
            s = pow(k, _N - 2, _N) * (digest + r * private) % _N
            if not s:
                continue
            def integer(value):
                raw = value.to_bytes((value.bit_length() + 8) // 8 or 1, "big")
                return bytes([0x02, len(raw)]) + raw
            body = integer(r) + integer(s)
            return bytes([0x30, len(body)]) + body

    class Preferences:
        def get(self):
            return {"values": {"biometric_auth": True}}

    class Identity:
        host_id = "0123456789abcdef0123456789abcdef"
        host_name = "omarchy"

    async def build():
        directory = Path(tempfile.mkdtemp())
        # Fixed identifiers and a fixed clock, like every other fixture here:
        # the approval is real and so is the signature over it, but the random
        # parts are pinned so `--write` produces the same bytes twice.
        counter = itertools.count(1)
        biometric.secrets = types.SimpleNamespace(
            token_hex=lambda size: ("%02x" % next(counter)) * size,
            token_urlsafe=lambda size: ("fixture-nonce-" + "%d" % next(counter)).ljust(43, "x")[:43],
            randbelow=secrets.randbelow, token_bytes=secrets.token_bytes)
        biometric.time = types.SimpleNamespace(time=lambda: NOW.timestamp())
        authority = DeviceAuthenticator(secret=b"\x22" * 32, state_path=directory / "credentials.json")
        authority.issue("ipad-fixture", device_name="Leo's iPad")
        hub = Hub(authenticator=authority)
        hub.connected_devices = lambda: {"ipad-fixture"}
        broker = ApprovalBroker(hub, BiometricKeyStore(directory / "keys.json"),
                                preferences=Preferences(), host_identity=Identity(),
                                journal=lambda entry: None, clock=lambda: NOW.timestamp())
        private = secrets.randbelow(_N - 1) + 1
        point = _multiply(private, _G)
        public = base64.b64encode(b"\x04" + point[0].to_bytes(32, "big")
                                  + point[1].to_bytes(32, "big")).decode()
        challenge = broker.challenge("ipad-fixture")
        broker.enroll("ipad-fixture", {
            "public_key": public, "label": "Leo's iPad", "challenge": challenge, "enabled": True,
            "secure_enclave": True, "signature": base64.b64encode(sign(private, enrollment_message(
                host_id=Identity.host_id, device_id="ipad-fixture", challenge=challenge,
                public_key_b64=public))).decode()})
        task = asyncio.create_task(broker.request({"service": "sudo", "user": "alex",
                                                   "requester": "alex", "tty": "pts/3",
                                                   "timeout": 45}))
        await asyncio.sleep(0)
        approval_id = next(iter(broker._pending))
        record = broker._pending[approval_id]
        requested = next(event for event in hub.events_since(0, limit=None, device_id="ipad-fixture")
                         if event.type == "auth.approval.requested")
        requested = DeviceEvent(1, "evt_00000001", requested.type, requested.device_id,
                                requested.payload, NOW.timestamp())
        broker.resolve(approval_id, "ipad-fixture", {"decision": "approve", "signature":
            base64.b64encode(sign(private, approval_message(
                host_id=Identity.host_id, approval_id=approval_id, nonce=record["nonce"],
                service="sudo", user="alex", device_id="ipad-fixture"))).decode()})
        await task
        resolved = [event for event in hub.events_since(0, limit=None, device_id="ipad-fixture")
                    if event.type == "auth.approval.resolved"][-1]
        resolved = DeviceEvent(2, "evt_00000002", resolved.type, resolved.device_id,
                               resolved.payload, NOW.timestamp())
        keys = resource({"enabled": True, "host_id": Identity.host_id, "host_name": Identity.host_name,
                         "device_id": "ipad-fixture", "challenge": broker.challenge("ipad-fixture"),
                         "enrolled": True, "keys": broker.keys.list()})
        return {"event-auth-approval-requested.json": resource(asdict(requested)),
                "event-auth-approval-resolved.json": resource(asdict(resolved)),
                "auth-keys.json": keys}

    try:
        return asyncio.run(build())
    finally:
        biometric.secrets, biometric.time = secrets, __import__("time")


def schema_for_fixture(name: str) -> str:
    if name == "auth-keys.json": return "auth-keys.schema.json"
    if name.startswith(("voice-", "notifications", "notification-")): return name.removesuffix(".json") + ".schema.json"
    if name.startswith("agent-chat-event-"): return "agent-chat-event.schema.json"
    if name.startswith("agent-chat-"): return name.removesuffix(".json") + ".schema.json"
    if name == "health.json": return "health.schema.json"
    if name == "theme.json": return "theme.schema.json"
    if name == "fonts.json": return "fonts.schema.json"
    if name == "herdr-layout.json": return "herdr-layout.schema.json"
    if name == "herdr-sessions.json": return "herdr-sessions.schema.json"
    if name == "pairing-claim.json": return "pairing-claim.schema.json"
    if name == "pairing-credential.json": return "pairing-credential.schema.json"
    if name == "pairing-renew.json": return "pairing-renew.schema.json"
    if name == "ssh-key.json": return "ssh-key.schema.json"
    if name == "ssh-key-state.json": return "ssh-key-state.schema.json"
    if name == "capabilities.json": return "capabilities.schema.json"
    if name == "state.json": return "state.schema.json"
    if name in {"bar.json", "bar-unavailable.json", "bar-geometry.json"}: return "bar.schema.json"
    if name in {"catalog.json", "catalog-layered.json", "catalog-search-3.json"}: return "catalog.schema.json"
    if name in {"route-descriptors.json", "route-descriptors-menu-actions.json"}: return "route-descriptors.schema.json"
    if name.startswith("default-agent"): return "schemas/default-agent-capabilities.schema.json"
    if name == "herdr.json": return "schemas/herdr-status.schema.json"
    if name == "herdr-resource.json": return "herdr-resource.schema.json"
    if name.startswith("event-"): return "events.schema.json"
    if name.startswith("remote-"): return name.removesuffix(".json")+".schema.json"
    if name.startswith("wss-"): return "wss-envelope.schema.json"
    if name == "shortcuts.json": return "shortcuts.schema.json"
    if name.startswith("action-"): return "action-result.schema.json"
    if name.startswith("ipc-request-"): return "ipc-request.schema.json"
    if name.startswith(("ipc-response-", "ipc-event")): return "ipc-envelope.schema.json"
    if name.startswith("http-error-"): return "http-error.schema.json"
    if name == "catalog/catalog.expected.json": return "catalog-input-expectation.schema.json"
    raise ValueError(f"fixture has no schema mapping: {name}")


def validators():
    from jsonschema import Draft202012Validator, FormatChecker
    from referencing import Registry, Resource
    registry = Registry()
    checker = FormatChecker()
    # jsonschema's optional RFC3339 dependency is not needed for this minimal
    # validator: our wire timestamps use seconds plus an explicit UTC offset.
    @checker.checks("date-time")
    def valid_datetime(value):
        import re
        if not isinstance(value, str):
            return True
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})", value):
            return False
        try:
            return datetime.fromisoformat(value.upper().replace("Z", "+00:00")).tzinfo is not None
        except ValueError:
            return False
    documents = {}
    for path in sorted(CONTRACTS.rglob("*.schema.json")):
        doc = json.loads(path.read_text())
        Draft202012Validator.check_schema(doc)
        documents[path.relative_to(CONTRACTS).as_posix()] = doc
        registry = registry.with_resource(doc["$id"], Resource.from_contents(doc))
    return {name: Draft202012Validator(document, registry=registry, format_checker=checker)
            for name, document in documents.items()}


def verify(*, write: bool = False) -> dict[str, int]:
    generated = generated_fixtures()
    checks = validators()
    # Refuse to write an invalid generated document; implementation changes
    # must update the declared contract before replacing saved examples.
    for name, document in generated.items():
        checks[schema_for_fixture(name)].validate(document)
        if name == "ipc-response-state.json":
            # Raw Hub IPC state has transport-known revision metadata. Validate
            # its body against the HTTP resource shape with that header added.
            checks["state.schema.json"].validate(resource(document["result"]))
    if write:
        for name, document in generated.items():
            (FIXTURES / name).write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    checked = 0
    inputs = ("theme/", "fonts/", "herdr/")
    for path in sorted(FIXTURES.rglob("*.json")):
        name = path.relative_to(FIXTURES).as_posix()
        # Recorded host inputs the readers consume; the generated wire examples
        # beside them are what the registry validates.
        if name.endswith(".meta.json") or name.startswith(inputs):
            continue
        document = json.loads(path.read_text())
        checks[schema_for_fixture(name)].validate(document)
        if name in generated and document != generated[name]:
            raise ValueError(f"fixture drift: {name}; run --write and review the changes")
        checked += 1
    for name in generated:
        if not (FIXTURES / name).exists():
            raise ValueError(f"missing generated fixture: {name}; run --write")
    routes = {row["route"] for row in generated["route-descriptors.json"]["routes"]}
    if routes != {"host", "terminal", "desktop", "native"}:
        raise ValueError("route fixtures do not cover all four surfaces")
    return {"schemas": len(checks), "fixtures": checked, "generated": len(generated)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate known fixtures before validation")
    args = parser.parse_args(argv)
    result = verify(write=args.write)
    print(f"Contract {REVISION}: {result['schemas']} Draft 2020-12 schemas, {result['fixtures']} fixtures validated; {result['generated']} generated examples match implementation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
