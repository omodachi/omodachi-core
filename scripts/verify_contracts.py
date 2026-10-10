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
from omodachi_core.network import apply_road

ENCODER = EncoderLimits(4096, 4096, 16_777_216, 60, 40_000, 2, 2)
OWNED_OUTPUT = "OMODACHI-0123456789abcdef"


class _FixtureWayVNC:
    """Synthetic stand-in so the connection example comes from the real adapter."""
    def available(self): return True
    def start(self, output, pixels, logical_size=None):
        self.pixels = dict(pixels)
        return {"socket": "/run/user/1000/omodachi-fixture/rfb.sock"}

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
    # REMOTE-INPUT-1: `text_input` is added by RemoteService from the live
    # keymap and `wtype`; the example shows a US host that has it.
    # REMOTE-INPUT-2 adds the route's limits and `compose`.
    from omodachi_core.remote.text import LIMITS
    capabilities = {**manager.capabilities(),
                    "text_input": {"available": True, "layout": "us", "variant": None, **LIMITS,
                                   "compose": True}}
    from omodachi_core.remote.text import HostText
    typed = HostText(runner=lambda argv, env, timeout: 0, environment=lambda: {"WAYLAND_DISPLAY": "wayland-1"},
                     which=lambda name: "/usr/bin/wtype")
    typed.executable = lambda: "/usr/bin/wtype"
    return {"remote-session.json": resource(session.to_dict()),
            "remote-connection.json": session.connection,
            "remote-capabilities.json": resource(capabilities),
            "remote-text.json": resource(typed.type("你好")),
            # TAIL-2 P2-2: the HTTPS answer for a peer Tailscale is relaying.
            "remote-capabilities-relayed.json": resource(apply_road(
                {**capabilities, "backends": {
                    "sunshine": {"available": True, "reason": None},
                    "vnc": {"available": True, "reason": None}}}, "tailnet", True))}


FIXTURE_IDENTITY = HostIdentity(host_id="0" * 32, host_name="omarchy-fixture",
                                certificate=None, port=8099,
                                addresses=["192.168.1.10"])
# A fixed synthetic fingerprint: the fixture must not depend on a certificate
# that exists only on one developer machine.
FIXTURE_IDENTITY.fingerprint = lambda: "ab" * 32
# TAIL-2: a synthetic tailnet reading (TEST-NET-style names, no real tailnet).
FIXTURE_IDENTITY.tailnet = lambda: {
    "installed": True, "backend_state": "Running", "running": True,
    "dns_name": "omarchy-fixture.tail0000.ts.net", "ips": ["100.64.0.35", "fd7a:115c:a1e0::35"],
    "ssh": False, "accept_routes": False, "netfilter_mode": 2}


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
        # RELEASE-9 (B2): a device rotates only with the SSH grant its Approve
        # recorded; the fixture device is one that has it.
        service._ssh_grants = lambda device: {"request_id": "pair_fixture", "ssh": True}
        try:
            return {"ssh-key.json": service.ssh_key("device-fixture-redacted",
                                                    {"public_key": FIXTURE_SSH_KEY_REPLACEMENT}),
                    "ssh-key-state.json": redact_ssh_user(service.ssh_key_state("device-fixture-redacted"))}
        finally:
            service.ssh_keys = original
            del service._ssh_grants


def redact_ssh_user(document):
    """The daemon's own Unix account exists only on one machine."""
    if isinstance(document.get("target"), dict):
        document["target"] = {**document["target"], "user": "fixture-user"}
    return document


def pairing_fixtures(service: CoreService) -> dict[str, Any]:
    """Drive the real pairing store end to end, then redact the credential."""
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        authority = DeviceAuthenticator(secret=b"\x00" * 32, state_path=root / "credentials.json")
        service.pairing = PairingStore(authority, root / "pairing.json")
        invitation = service.pairing.begin()["invitation"]
        row = service.pairing.request(invitation, "device-fixture-redacted", "Fixture iPad",
                                      ssh_public_key=FIXTURE_SSH_KEY,
                                      via={"kind": "tailnet", "peer_name": "fixture-ipad",
                                           "login": "fixture@example.com"})
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
    return {"pairing-claim.json": claim, "pairing-claim-web.json": web_claim_fixture(service),
            **credential_fixtures()}


def web_claim_fixture(service: CoreService) -> dict[str, Any]:
    """LITE-1a: a browser's pairing through `tailscale serve`, claimed.

    The request carried `Origin` (so it is `web` whatever it said), came from a
    loopback peer that forwarded for a tailnet address - so its `via` is the
    same tailnet road and whois answer the App's would be (NEXT-1) - and was
    approved: the claim holds the companion credential and nothing else.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        authority = DeviceAuthenticator(secret=b"\x00" * 32, state_path=root / "credentials.json")
        service.pairing = PairingStore(authority, root / "pairing.json")
        row = service.pairing.request(None, "odomo-fixture-redacted", "Safari on iPhone",
                                      remote_addr="100.64.0.7", client={"kind": "web", "label": "Safari on iPhone"},
                                      browser=True, via={"kind": "tailnet", "peer_name": "fixture-iphone",
                                                         "login": "fixture@example.com"})
        service.pairing.decide(row["request_id"], approve=True)
        # LITE-1b C7: a host whose installer made the `tailscale serve`
        # mapping tells the browser that paired through it where Odomo lives.
        identity = service.host_identity
        service.install_host_identity(with_odomo(identity))
        claim = service.pairing_claim(row["request_id"], {"request_secret": row["request_secret"]})
        service.install_host_identity(identity)
    claim["request_id"] = "pair_" + "1" * 32
    claim["ssh"] = {**claim["ssh"], "user": "fixture-user"}
    claim["credential"] = "fixture-redacted-web-credential"
    claim["issued_at"] = int(NOW.timestamp())
    claim["credential_expires_at"] = int(NOW.timestamp()) + authority.ttl_seconds
    claim["expires_at"] = int(NOW.timestamp()) + PairingStore.TTL
    return claim


FIXTURE_ODOMO_URL = "https://omarchy-fixture.tail0000.ts.net/odomo/"


def with_odomo(identity: HostIdentity) -> HostIdentity:
    """The same fixture identity, with the installer's serve record behind it."""
    value = replace(identity, odomo=lambda: FIXTURE_ODOMO_URL, odomo_push=lambda: 1)
    value.fingerprint = identity.fingerprint
    return value


def without_odomo(identity: HostIdentity) -> HostIdentity:
    """LITE-1d: a host with push but no front door (no Tailscale there)."""
    value = replace(identity, odomo=lambda: None, odomo_push=lambda: 0)
    value.fingerprint = identity.fingerprint
    return value


def push_fixtures(service: CoreService) -> dict[str, Any]:
    """LITE-1b C6: the push routes' documents, from the real store and builders."""
    import tempfile
    from omodachi_core import webpush
    from omodachi_core.push import AGENT_TAG, PushStore, Signal, payload_for, retract_payload
    from omodachi_core.notifications import NotificationMirror
    vapid = webpush.Vapid.from_document({"private": webpush.b64u(bytes(range(1, 33)))})
    browser_key = webpush.b64u(b"\x04" + bytes(range(64)))
    with tempfile.TemporaryDirectory() as directory:
        store = PushStore(Path(directory) / "push-subscriptions.json", clock=lambda: NOW.timestamp())
        record = store.subscribe("odomo-fixture-redacted", {
            "endpoint": "https://web.push.apple.com/fixture-not-a-real-endpoint",
            "keys": {"p256dh": browser_key, "auth": webpush.b64u(bytes(16))}, "expirationTime": None})
        prefs = store.set_prefs("odomo-fixture-redacted", 0, {"kinds": {"agent_done": False}, "lang": "en"})
    origin = FIXTURE_ODOMO_URL.removesuffix("/odomo/")
    approval = Signal("agent_approval", "agent", AGENT_TAG, panel="agent", ids={"ref": "approval"})
    note = Signal("host_notification", "note:Omodachi", "note-1789711988810-1", panel="notifications",
                  ids={"note": "1789711988810-1"}, name="Omodachi", title="\u201cQA companion\u201d wants to connect",
                  body="Open Omodachi to approve or reject it.")
    blocked = Signal("herdr_blocked", "herdr:omodachi:w1:p2", "herdr-w1-p2", panel="herdr",
                     ids={"session": "omodachi", "pane": "w1:p2", "agent": "codex"}, name="codex")
    hub = Hub()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "notifications"
        (root / "history").mkdir(parents=True)
        (root / "1789711988810-1.json").write_text(json.dumps({
            "id": 1, "app": "Omodachi", "summary": "\u201cQA companion\u201d wants to connect",
            "body": "Open Omodachi to approve or reject it.", "glyph": "", "urgency": 2,
            "timestamp": 1789711988810}))
        mirror = NotificationMirror(hub, state_dir=root)
        mirror.scan()
        (root / "1789711988810-1.json").rename(root / "history/1789711988810-1.json")
        mirror.scan()
    updated = next(event for event in hub.events_since(0) if event.type == "notification.updated")
    updated = replace(updated, ts=NOW.timestamp())
    return {
        "push-vapid-key.json": resource({"vapid_public_key": vapid.public_b64u}),
        "push-subscription.json": resource({"subscription_id": "sub_" + "0" * 24,
                                            "device_id": "odomo-fixture-redacted",
                                            "created_at": record["created_at"],
                                            "endpoint_host": "web.push.apple.com",
                                            "vapid_public_key": vapid.public_b64u}),
        "push-unsubscribe.json": resource({"device_id": "odomo-fixture-redacted", "removed": True}),
        "notifications-prefs.json": resource(prefs),
        # NOTIFY-2: `navigate` carries the host id, so Odomo can link the App there.
        "push-payload-agent-approval.json": json.loads(payload_for(approval, origin=origin, lang="en",
                                                                    host="omarchy-fixture",
                                                                    host_id=FIXTURE_IDENTITY.host_id)),
        "push-payload-herdr-blocked-zh.json": json.loads(payload_for(blocked, origin=origin, lang="zh-Hans",
                                                                      host="omarchy-fixture",
                                                                      host_id=FIXTURE_IDENTITY.host_id)),
        "push-payload-host-notification.json": json.loads(payload_for(note, origin=origin, lang="en",
                                                                       host="omarchy-fixture")),
        "push-payload-retract.json": json.loads(retract_payload(AGENT_TAG)),
        "event-notification-updated.json": resource(asdict(updated)),
        **presence_fixtures(),
    }


def notify1_state(state: dict) -> dict:
    """NOTIFY-1: what the App's alerts read off the state - the oldest pending
    approval's ids, whether the person is at the computer, and what finished
    with nobody looking - from the real provider helper, desk and ledger."""
    from omodachi_core.agent_chat_provider import approval_fingerprint
    from omodachi_core.desk import DeskPresence
    from omodachi_core.seen import SeenLedger
    value = json.loads(json.dumps(state))
    row = {"request_id": "approval-fixture-01", "kind": "commandExecution", "summary": "make test",
           "details": {"cwd": "/home/fixture/project"}, "decisions": ["accept", "acceptForSession", "decline", "cancel"]}
    value["agent"]["chat"] = {"status": {"type": "active", "activeFlags": ["waitingOnApproval"], "waiting": "approval"},
                              "pending_approvals": 1,
                              "approval": {"request_id": row["request_id"], "kind": row["kind"],
                                           "fingerprint": approval_fingerprint(row), "decidable": True}}
    value["agent"]["status"] = "waiting_on_approval"
    desk = DeskPresence(locked_probe=lambda: False)
    desk._set(False, "wayland")
    value["desk"] = desk.evaluate()
    ledger = SeenLedger(None, clock=lambda: 1_790_000_000.0)
    pane = lambda status: {"workspaces": [{"tabs": [{"panes": [
        {"id": "w1:p2", "agent_status": status, "agent": {"name": "default", "kind": "codex", "status": status}}]}]}]}
    ledger.observe_herdr("omodachi", pane("working"))
    ledger.observe_herdr("omodachi", pane("idle"))
    value["unseen"] = ledger.value()
    return value


def overview1_state(state: dict) -> dict:
    """OVERVIEW-1: the "Needs you" list from the real projector - the Agent
    waiting on a command approval, and two sessions: a codex pane blocked on
    an approval dialog (read off a synthetic screen), a claude pane working, a
    blocked pane whose screen reads as no dialog, and a finished pane whose
    agent's last reply ends in a question."""
    from omodachi_core.overview import OverviewProjector
    value = notify1_state(state)
    projector = OverviewProjector(clock=lambda: 100.0)
    pane = lambda ident, status, kind, title, cwd: {
        "id": ident, "title": title, "cwd": cwd, "agent_status": status, "revision": 4,
        "agent": {"name": "default" if kind else None, "kind": kind, "status": status} if kind else None}
    projector.set_session("work", {"workspaces": [
        {"id": "w1", "label": "api", "number": 1, "tabs": [{"panes": [
            pane("w1:p1", "blocked", "codex", None, "/home/fixture/api"),
            pane("w1:p2", "working", "claude", "claude", "/home/fixture/api")]}]},
        {"id": "w2", "label": None, "number": 2, "tabs": [{"panes": [
            pane("w2:p1", "blocked", "codex", None, "/home/fixture/site")]}]}]})
    projector.set_session("omodachi", {"workspaces": [{"id": "w1", "label": "~", "number": 1, "tabs": [{"panes": [
        pane("w1:p1", "working", "codex", "codex", "/home/fixture"),
        pane("w1:p3", "idle", "codex", None, "/home/fixture/notes")]}]}]})
    projector.note_screen("work", "w1:p1", "\n".join([
        "  Would you like to run the following command?", "", "  $ make test", "",
        "› 1. Yes, proceed (y)", "  2. No, and tell Codex what to do differently (esc)", "",
        "  Press enter to confirm or esc to cancel"]))
    projector.note_screen("work", "w2:p1", "Thinking about it…")
    projector.note_reply("omodachi", "w1:p3", "Done. The notes are sorted.\n\nShould I also archive the old ones?")
    value["unseen"] = {"agent": False, "herdr": [{"session": "omodachi", "pane_id": "w1:p3", "name": "codex"}]}
    value["overview"] = projector.value(agent=value["agent"], unseen=value["unseen"], default_pane="w1:p1")
    return value


def presence_fixtures() -> dict[str, Any]:
    """NOTIFY-2: the App in front, alerting the kinds it shows by itself; and
    the last word of a Herdr session that stopped, from the real service."""
    from omodachi_core.herdr_bridge import HerdrBridge
    hub = Hub()
    hub.register_device("ipad-fixture")
    service = create_service(hub, demo=True)
    presence = service.set_presence("ipad-fixture", {"foreground": True, "alerts": [
        "agent_approval", "agent_needs_input", "herdr_blocked", "host_notification"]})
    service.herdr_bridge = HerdrBridge("omodachi", runner=lambda argv: {"result": {}})
    gone = HerdrBridge("probe", runner=lambda argv: {"result": {}})
    gone._revision = 7
    service._herdr_bridges["probe"] = gone
    service._watch_running_sessions = lambda now=None: [service._herdr_bridges.pop("probe")]
    service.notify_herdr_layout()
    stopped = next(event for event in hub.events_since(0) if event.payload.get("stopped"))
    stopped = DeviceEvent(1, "evt_00000001", stopped.type, stopped.device_id, stopped.payload, NOW.timestamp())
    return {"presence.json": presence, "event-herdr-layout-stopped.json": resource(asdict(stopped))}


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
            **herdr_write_fixtures(),
            **herdr_session_start_fixtures(),
            **herdr_workspace_fixtures(),
            **herdr11_fixtures(),
            **operator_rows_fixtures(),
            "herdr-layout.json": resource(bridge.layout()),
            "herdr-sessions.json": resource({"selected": "omodachi", "owned": "omodachi",
                                             "sessions": sessions})}


def herdr_write_fixtures() -> dict[str, Any]:
    """HERDR-3. The real bridge, against a pretend pane with one input line.

    `echo` shows what was typed, so the proof holds and Enter follows;
    `dialog` swallows it, so nothing is submitted and the reply says why.
    """
    from omodachi_core.herdr_bridge import HerdrBridge

    def pane(echo: bool, blocked: bool = False):
        screen = {"text": "\u2500\u2500\u2500\n> "}

        def request(method, params):
            if method == "pane.read":
                return {"type": "pane_read", "read": {"text": screen["text"]}}
            if method == "pane.send_text" and echo:
                screen["text"] += params["text"]
            return {"type": "ok"}
        # VERIFY-1: what Herdr says about the pane's agent. `{}` is a snapshot
        # that cannot be read, which says nothing and leaves the proof to decide.
        state = {"snapshot": {"panes": [{"pane_id": "w1:p2", "agent_status": "blocked"}]}} if blocked else {}
        bridge = HerdrBridge(runner=lambda argv: state)
        bridge.request = request
        return bridge

    quiet = lambda seconds: None
    return {"herdr-keys.json": resource(pane(True).send_keys("w1:p2", ["Down", "2", "Enter"])),
            "herdr-text.json": resource(pane(True).send_text(
                "w1:p2", "please run the tests again", submit=True, sleep=quiet)),
            "herdr-text-not-seen.json": resource(pane(False).send_text(
                "w1:p2", "please run the tests again", submit=True, sleep=quiet)),
            # HERDR-4: a reply this short is proved by its line only.
            "herdr-text-short.json": resource(pane(True).send_text("w1:p2", "yes", submit=True, sleep=quiet)),
            # VERIFY-1: a dialog has the keyboard; nothing is typed.
            "herdr-text-agent-blocked.json": resource(pane(True, blocked=True).send_text(
                "w1:p2", "continue", submit=True, sleep=quiet))}


def upload_fixtures() -> dict[str, Any]:
    """ATTACH-1. The real store over synthetic bytes in a throwaway directory,
    with a fixed clock and ids; the path is then written as a home's own."""
    import tempfile
    from omodachi_core import uploads
    from omodachi_core.service import ServiceError
    home_dir = "/home/user/.local/state/omodachi/uploads"
    ids = iter(f"{n:032x}" for n in range(0xa1, 0xa1 + 16))
    real_uuid4 = uploads.uuid.uuid4
    uploads.uuid.uuid4 = lambda: type("U", (), {"hex": next(ids)})()
    try:
        with tempfile.TemporaryDirectory() as scratch:
            store = uploads.UploadStore(Path(scratch) / "uploads", clock=lambda: NOW.timestamp())

            def put(name, data, target):
                receiving = store.receive()
                receiving.write(data)
                upload = receiving.finish(name, device="ipad-fixture", target=target)
                document = upload.document()
                document["path"] = document["path"].replace(str((Path(scratch) / "uploads").resolve()), home_dir)
                return resource(document)

            def refusal(name, data):
                try:
                    put(name, data, {})
                except ServiceError as error:
                    return resource({"error": {"code": error.code, "message": str(error)}})
                raise AssertionError(name)
            png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
            fixtures = {
                "upload-image.json": put("Screenshot 2026-10-04.png", png, {"kind": "agent"}),
                "upload-text.json": put("main.py", b"print('hello')\n", {"kind": "herdr", "session": "work",
                                                                          "pane": "w1:p2"}),
                "upload-pdf.json": put("notes.pdf", b"%PDF-1.7\n%fixture\n", {"kind": "agent"}),
                "http-error-upload-svg-refused.json": refusal("drawing.svg", b"<svg xmlns='x'/>"),
                "http-error-upload-type-refused.json": refusal("bundle.zip", b"PK\x03\x04" + b"\0" * 32),
                "http-error-upload-not-text.json": refusal("data.py", b"\x00\x01\x02binary"),
            }
            small = uploads.UploadStore(Path(scratch) / "small", max_bytes=8)
            receiving = small.receive()
            try:
                receiving.write(b"0123456789")
            except ServiceError as error:
                fixtures["http-error-upload-too-large.json"] = resource(
                    {"error": {"code": error.code, "message": str(error)}})
            fixtures["upload-removed.json"] = resource(store.delete(fixtures["upload-pdf.json"]["id"]))
            return fixtures
    finally:
        uploads.uuid.uuid4 = real_uuid4


def herdr_session_start_fixtures() -> dict[str, Any]:
    """HERDR-5. The real service and starter against the recorded listing:
    `default` is stopped and comes back with its own workspaces; `h5test` is
    new and gets one in $HOME. The runner stands in for `systemd-run`."""
    from omodachi_core.agent import ProbeOutput
    from omodachi_core.herdr_bridge import (HerdrBridge, HerdrSessionChoices, HerdrSessions,
                                            HerdrSessionStarter, HerdrWriteAudit)
    listing = json.loads((FIXTURES / "herdr/session-list.json").read_text())
    for row in listing["sessions"]:
        row["running"] = row["name"] == "omodachi"

    def runner(argv):
        if argv[0] == "systemd-run":
            name = argv[-2]
            rows = [row for row in listing["sessions"] if row["name"] == name]
            if rows:
                rows[0]["running"] = True
            else:
                listing["sessions"].append({"default": False, "name": name, "running": True})
        return ProbeOutput(0)
    service = create_service(Hub(), demo=True)
    service.herdr_bridge = HerdrBridge(runner=lambda argv: {})
    service.herdr_choices = HerdrSessionChoices()
    service.herdr_audit = HerdrWriteAudit()
    service._herdr_sessions = HerdrSessions(runner=lambda argv: listing, home=Path("/home/alex/.config/herdr"))
    service.herdr_starter = HerdrSessionStarter(runner=runner, home=Path("/home/user"), sleep=lambda _: None)
    service._herdr_fresh_snapshot = lambda name, socket_path=None: {"workspaces": [] if name == "h5test" else [{}]}
    return {"herdr-session-start.json": service.herdr_start_session("phone-a", "default"),
            "herdr-session-create.json": service.herdr_create_session("phone-a", {"name": "h5test"})}


def herdr_workspace_fixtures() -> dict[str, Any]:
    """HERDR-6. The real service over a scratch home folder, written back as
    `/home/user`: a new workspace in `~/Work/omodachi`, the folders offered
    (two running sessions' panes), one level of `~/Work`, and two refusals.
    Herdr's side is the recorded `workspace create` reply."""
    import os
    import tempfile
    from omodachi_core.herdr_bridge import (HerdrBridge, HerdrFolders, HerdrSessionChoices, HerdrSessions,
                                            HerdrWriteAudit)
    from omodachi_core.service import ServiceError
    listing = json.loads((FIXTURES / "herdr/session-list.json").read_text())
    work = "work"
    listing["sessions"].append({"default": False, "name": work, "running": True,
                                "session_dir": "/home/alex/.config/herdr/sessions/work",
                                "socket_path": "/home/alex/.config/herdr/sessions/work/herdr.sock"})
    for row in listing["sessions"]:
        row["running"] = row["name"] in ("omodachi", work)
    with tempfile.TemporaryDirectory() as scratch:
        home = Path(os.path.realpath(scratch)) / "home"
        for name in ("Work/omodachi", "Work/notes", "Work/.cache", "code", "Projects", ".ssh"):
            (home / name).mkdir(parents=True)
        panes = {"omodachi": [str(home)], work: [str(home / "code"), str(home / "Work/omodachi")]}

        def runner(session):
            def run(argv):
                if argv[3:5] == ("workspace", "create"):
                    cwd = argv[6]
                    return {"type": "workspace_created",
                            "workspace": {"workspace_id": "w3",
                                          "label": argv[8] if "--label" in argv else Path(cwd).name,
                                          "number": 3, "focused": False,
                                          "active_tab_id": "w3:t1", "agent_status": "unknown",
                                          "pane_count": 1, "tab_count": 1},
                            "root_pane": {"pane_id": "w3:p1", "cwd": cwd, "foreground_cwd": cwd}}
                return {"snapshot": {"panes": [{"pane_id": f"w{n + 1}:p1", "cwd": cwd}
                                               for n, cwd in enumerate(panes[session])]}}
            return run
        service = create_service(Hub(), demo=True)
        service.herdr_bridge = HerdrBridge("omodachi", runner=runner("omodachi"))
        service._herdr_bridges[work] = HerdrBridge(work, runner=runner(work))
        service.herdr_choices = HerdrSessionChoices()
        service.herdr_audit = HerdrWriteAudit()
        service._herdr_sessions = HerdrSessions(runner=lambda argv: listing, home=Path("/home/alex/.config/herdr"))
        service.herdr_folders_memory = HerdrFolders(home, clock=lambda: 0.0)
        # HERDR-11: the launchers a host with both agents installed offers,
        # and the root pane a launcher is typed into (its prompt drawn).
        from omodachi_core.operator_rows import Launchers
        nowhere = Path("/nonexistent/omodachi-fixture")
        service.herdr_launchers = Launchers(nowhere, nowhere, which=lambda name, path=None: "/usr/bin/" + name)
        for bridge in (service.herdr_bridge, *service._herdr_bridges.values()):
            bridge.request = lambda method, params: {"read": {"text": "~/Work/omodachi $ "}} \
                if method == "pane.read" else {}

        def refusal(cwd, launch=None):
            try:
                body = {"cwd": cwd} if launch is None else {"cwd": cwd, "launch": launch}
                service.herdr_create_workspace("phone-a", work, body)
            except ServiceError as error:
                return resource({"error": {"code": error.code, "message": str(error)}})
            raise AssertionError(cwd)
        service.herdr_folder_favourite("phone-a", {"path": "~/Work/notes", "favourite": True})
        documents = {
            "herdr-folders.json": service.herdr_folders("phone-a", {}),
            "herdr-folders-browse.json": service.herdr_folders("phone-a", {"under": "~/Work"}),
            "herdr-workspace-create.json": service.herdr_create_workspace(
                "phone-a", work, {"cwd": "~/Work/omodachi", "name": "omodachi"}),
            "herdr-workspace-create-launch.json": service.herdr_create_workspace(
                "phone-a", work, {"cwd": "", "launch": "codex"}),
            "http-error-herdr-launch-not-allowed.json": refusal("~", "rm -rf ~"),
            "http-error-herdr-folder-private.json": refusal("~/.ssh"),
            "http-error-herdr-folder-outside-home.json": refusal("/etc"),
        }
        text = json.dumps(documents).replace(str(home), "/home/user")
    return json.loads(text)


def herdr11_state(light: dict, full: dict) -> dict:
    """The light state, with the per-process identity normalised the way
    `state.json`'s is, so the two documents differ only in the catalog."""
    light = json.loads(json.dumps(light))
    light["instance_id"] = full["instance_id"]
    light["event_cursor"] = full["event_cursor"]
    light["revision"] = full["revision"]
    if light.get("focus", {}).get("target_token") is not None:
        light["focus"]["target_token"] = full["focus"]["target_token"]
    return light


def herdr11_fixtures() -> dict[str, Any]:
    """HERDR-11. A pane's latest reply from a synthetic codex rollout and a
    synthetic Claude Code log (shapes read off the host, words made up), one
    pane with nothing to show, a workspace started with a launcher, and the
    refusal of a command that is not one."""
    import tempfile
    from omodachi_core.herdr_reply import AgentReplies, ProcessTable
    from omodachi_core.service import ServiceError
    documents = {}
    with tempfile.TemporaryDirectory() as scratch:
        home = Path(scratch)
        sessions = home / ".codex/sessions/2026/10/07"
        sessions.mkdir(parents=True)
        thread = "01a110e1-ee88-7e23-aae2-3c5cdbb8a356"
        rollout = sessions / f"rollout-2026-10-07T10-00-00-{thread}.jsonl"
        lines = [
            {"timestamp": "2026-10-07T10:00:00.000Z", "type": "session_meta", "payload": {"id": thread}},
            {"timestamp": "2026-10-07T10:00:01.000Z", "type": "event_msg",
             "payload": {"type": "task_started", "turn_id": "turn-fixture-01"}},
            {"timestamp": "2026-10-07T10:00:04.000Z", "type": "response_item",
             "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [
                 {"type": "output_text", "text": "Done. The tests pass now:\n\n| suite | result |\n|---|---|\n"
                                                  "| unit | 12 passed |\n\n中文也一样：全部通过。"}]}},
            {"timestamp": "2026-10-07T10:00:05.000Z", "type": "event_msg",
             "payload": {"type": "task_complete", "turn_id": "turn-fixture-01"}}]
        rollout.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n")
        project = home / ".claude/projects/-home-user-h11test"
        project.mkdir(parents=True)
        session_id = "6f1c2d3e-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
        claude = [{"type": "assistant", "isSidechain": False, "timestamp": "2026-10-07T10:02:00.000Z",
                   "message": {"id": "msg_fixture", "role": "assistant", "stop_reason": None,
                               "content": [{"type": "text", "text": "I read the two files."}]}},
                  {"type": "assistant", "isSidechain": False, "timestamp": "2026-10-07T10:02:00.000Z",
                   "message": {"id": "msg_fixture", "role": "assistant", "stop_reason": "end_turn",
                               "content": [{"type": "text", "text": "Both look right; nothing to change."}]}}]
        (project / f"{session_id}.jsonl").write_text("\n".join(json.dumps(line) for line in claude) + "\n")

        class Table(ProcessTable):
            def tree(self, roots): return list(roots)
            def open_files(self, pid): return [str(rollout)] if pid == 41 else []
            def name(self, pid): return {41: "codex", 42: "claude"}.get(pid, "zsh")
            def cmdline(self, pid): return [self.name(pid)]
            def cwd(self, pid): return "/home/user/h11test"
            def pids(self): return [41, 42]
            def parent(self, pid): return 1
            def started(self, pid): return 0.0
        replies = AgentReplies(home=home, table=Table())
        codex = replies.reply(session="h11-test", pane="w1:p1", agent="codex",
                              process_info={"foreground_processes": [{"pid": 41}]})
        claude_reply = replies.reply(session="h11-test", pane="w2:p1", agent="claude",
                                     process_info={"foreground_processes": [{"pid": 42}]})
        documents["herdr-reply.json"] = resource({"session": "h11-test", "pane": "w1:p1", **codex})
        documents["herdr-reply-claude.json"] = resource({"session": "h11-test", "pane": "w2:p1", **claude_reply})
        documents["herdr-reply-unavailable.json"] = resource(
            {"session": "h11-test", "pane": "w1:p2", **replies.unavailable("not_an_agent")})
    return documents


def operator_rows_fixtures() -> dict[str, Any]:
    """HERDR-4. What ships for a claude pane and a shell; and an operator's
    Collie-format files (in a scratch directory standing in for
    `~/.config/collie/`) replacing it, with one dropped row and one file that
    no longer parses holding its last good rows."""
    import tempfile
    from omodachi_core.operator_rows import OperatorRows

    with tempfile.TemporaryDirectory() as scratch:
        ours, collie = Path(scratch) / "omodachi", Path(scratch) / "collie"
        ours.mkdir(); collie.mkdir()
        shipped = OperatorRows(ours, collie)
        claude, shell = shipped.resolve("claude"), shipped.resolve(None)
        (collie / "quick-replies.toml").write_text(
            '[[replies]]\nscope = "claude"\ntitle = "bestätigen"\nitems = ["ja", "nein"]\n\n'
            '[[replies]]\ntitle = "häufig"\nitems = ["weiter", "nochmal"]\n')
        (ours / "keys.toml").write_text(
            '[[keys]]\nscope = "claude"\nlabel = "Back"\nkeys = ["shift+tab"]\n\n'
            '[[keys]]\nlabel = "Quit"\nkeys = ["ctrl+d"]\ndanger = true\n\n'
            '[[keys]]\nlabel = "Page"\nkeys = ["PageUp"]\n')
        (ours / "commands.toml").write_text(
            '[[commands]]\nscope = "claude"\ncommand = "/statusline"\ndescription = "Set the status line"\n'
            'bar = true\nbar_label = "Status"\n\n'
            '[[commands]]\ncommand = "/standup"\ndescription = "Post today\'s standup"\n\n'
            '[[commands]]\nscope = "claude"\ncommand = "/clear"\nbar = true\n')
        rows = OperatorRows(ours, collie)
        rows.resolve("claude")
        # Mid-edit: the commands file stops parsing; its last good rows stay.
        (ours / "commands.toml").write_text('[[commands]]\ncommand = "/statusline\n')
        operator = rows.resolve("claude-code")
    return {"herdr-operator-rows.json": resource(claude),
            "herdr-operator-rows-shell.json": resource(shell),
            "herdr-operator-rows-operator.json": resource(operator)}


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
    fixtures["host-roads.json"] = service.host_roads()
    service.install_host_identity(with_odomo(FIXTURE_IDENTITY))
    fixtures["host-roads-odomo.json"] = service.host_roads()
    service.install_host_identity(without_odomo(FIXTURE_IDENTITY))
    fixtures["host-roads-odomo-none.json"] = service.host_roads()
    service.install_host_identity(FIXTURE_IDENTITY)
    fixtures.update(push_fixtures(service))
    fixtures.update(upload_fixtures())
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
    # MOTION-1: the chat summary `agent.changed` carries while a tool runs. A
    # copy of the same state, so no later fixture's revision or cursor moves.
    activity_state = json.loads(json.dumps(state))
    activity_state["agent"]["chat"] = {"status": {"type": "active", "activeFlags": [], "waiting": None},
                                       "pending_approvals": 0,
                                       "activity": {"phase": "tool", "turn_id": "turn-fixture-01"}}
    fixtures["state-agent-activity.json"] = activity_state
    fixtures["state-notify1.json"] = notify1_state(state)
    fixtures["state-overview1.json"] = overview1_state(state)
    # HERDR-11 §4: the same reading for a client that already holds this
    # catalog revision (`X-Omodachi-Catalog`): the rows are not sent again.
    fixtures["state-herdr11-catalog-unchanged.json"] = herdr11_state(
        service.state("device-fixture-redacted", known_catalog=state["catalog"]["revision"]), state)
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
    fixtures.update(agent_new_conversation_fixtures())
    fixtures.update(links_fixtures())
    fixtures.update(agent_fix2_fixtures())
    fixtures.update(agent_collie1_fixtures())
    fixtures.update(agent_command_fixtures())
    fixtures.update(agent_plan1_fixtures())
    fixtures.update(agent_queue1_fixtures())
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
    # LITE-1a L4 / C5: the opt-in heartbeat and a WebSocket ticket.
    fixtures["wss-heartbeat.json"] = {"type": "heartbeat", "instance_id": "daemon-instance-fixture"}
    fixtures["ws-ticket.json"] = resource({"ticket": "fixture-ticket-not-a-real-one-0123456789abc",
                                           "path": "/v1/events", "expires_in": 30})
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


def links_fixtures() -> dict[str, Any]:
    """LINKS-1. `POST /v1/host/open` from the real rules over a throwaway home
    (the opener records instead of launching anything), and an agent snapshot
    whose rows carry what codex emits for files: a markdown reply, a
    fileChange and an imageView, read through the real adapter."""
    import asyncio
    import os
    import tempfile
    from omodachi_core.agent_chat_provider import CodexDefaultAgentChat, DefaultAgentBinding
    from omodachi_core.host_open import DesktopOpener, HostOpen, Request
    from omodachi_core.service import ServiceError

    fixtures: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as scratch:
        home = Path(scratch).resolve()
        (home / "l1test").mkdir()
        (home / "l1test" / "report.md").write_text("# Report\n" + "line\n" * 20)
        (home / "l1test" / "run.sh").write_text("#!/bin/sh\necho hi\n")
        os.chmod(home / "l1test" / "run.sh", 0o755)
        (home / ".ssh").mkdir()
        (home / ".ssh" / "config").write_text("Host *\n")
        launched = []
        opener = DesktopOpener(environment=lambda: {"PATH": "/usr/bin"}, home=home,
                               runner=lambda argv, env: (
                                   "text/markdown" if argv[1:3] == ("query", "filetype") else
                                   "nvim.desktop" if argv[1:3] == ("query", "default") else
                                   json.dumps([{"name": "OMODACHI-0123456789abcdef", "focused": True}])
                                   if argv[1:] == ("-j", "monitors") else "ok"),
                               spawner=lambda argv, env, cwd: launched.append(argv))
        opener.default_editor = lambda: "nvim"
        opener.has_editor_launcher = lambda: True
        rules = HostOpen(opener=opener, home=home, log=lambda entry: None)
        said = (f"Wrote [report.md]({home}/l1test/report.md) - the totals are at `{home}/l1test/report.md:12`. "
                f"Serving at http://localhost:8000/ ; the script is {home}/l1test/run.sh and your SSH config is {home}/.ssh/config.")
        agent = {"kind": "agent", "item": "item-fixture-reply"}

        def run(text, *, url=False, check=False, output="OMODACHI-0123456789abcdef"):
            try:
                return resource(rules.run(Request(agent, text, url, check), device="ipad-fixture", role="companion",
                                          haystack=said, cwd=home / "l1test", output=output))
            except ServiceError as error:
                return resource({"error": {"code": error.code, "message": str(error)}})

        fixtures["host-open.json"] = run(f"{home}/l1test/report.md:12")
        fixtures["host-open-check.json"] = run(f"{home}/l1test/report.md", check=True)
        fixtures["host-open-url.json"] = run("http://localhost:8000/", url=True, output=None)
        fixtures["http-error-open-not-mentioned.json"] = run(f"{home}/.bashrc")
        fixtures["http-error-open-denied.json"] = run(f"{home}/.ssh/config")
        fixtures["http-error-open-executable.json"] = run(f"{home}/l1test/run.sh")
        assert launched == [("/usr/bin/omarchy-launch-editor", "+12", f"{home}/l1test/report.md"),
                            ("/usr/bin/xdg-open", "http://localhost:8000/")], launched

    turns = [{"id": "turn-fixture-01", "status": "completed", "items": [
        {"type": "userMessage", "id": "item-fixture-ask", "content": [
            {"type": "text", "text": "Make a chart of the totals and show it to me."}]},
        {"type": "fileChange", "id": "item-fixture-change", "status": "completed", "changes": [
            {"path": "/home/user/l1test/chart.py", "kind": {"type": "add"}, "diff": "print('chart')\n"}]},
        {"type": "imageView", "id": "item-fixture-view", "path": "/home/user/l1test/chart.png"},
        {"type": "agentMessage", "id": "item-fixture-reply", "text":
            "## Done\n\n- Chart: ![Bar chart](/home/user/l1test/chart.png)\n- Script: [chart.py](/home/user/l1test/chart.py)\n\n"
            "Preview at http://localhost:8000/ (served from `~/l1test`).\n\n```sh\npython3 chart.py\n```"},
        {"type": "agentMessage", "id": "item-fixture-blank", "text": " "}]}]

    class Peer:
        def __init__(self):
            self.ordinal = 0
            self.on_notification = lambda value, ordinal: None
            self.on_server_request = lambda value: None
            self.on_disconnect = lambda: None
        async def request(self, method, params):
            result, _ = await self.request_fenced(method, params)
            return result
        async def request_fenced(self, method, params):
            self.ordinal += 1
            if method == "thread/loaded/list":
                return {"data": ["thread-fixture-01"]}, self.ordinal
            return {"thread": {"id": "thread-fixture-01", "turns": turns}}, self.ordinal
        async def close(self):
            pass

    async def build():
        binding = DefaultAgentBinding("omarchy-fixture", "thread-fixture-01", "ws://127.0.0.1:47311", "f" * 64)
        chat = CodexDefaultAgentChat(binding, Peer())
        await chat.attach()
        return chat.snapshot_value()

    fixtures["agent-chat-snapshot-links.json"] = asyncio.run(build())
    return fixtures


def agent_fix2_fixtures():
    """AGENT-FIX-2: a command's output as increments (`tool.output`) and a
    snapshot whose tool row was clipped to its newest output
    (`detail_truncated`), from the real adapter and a controlled peer."""
    import asyncio
    from omodachi_core.agent_chat_provider import CodexDefaultAgentChat, DefaultAgentBinding, TOOL_DETAIL_LIMIT

    class Peer:
        def __init__(self):
            self.ordinal = 0
            self.on_notification = lambda value, ordinal: None
            self.on_server_request = lambda value: None
            self.on_disconnect = lambda: None
        async def request(self, method, params):
            result, _ = await self.request_fenced(method, params)
            return result
        async def request_fenced(self, method, params):
            self.ordinal += 1
            if method == "thread/loaded/list":
                return {"data": ["thread-fixture-01"]}, self.ordinal
            return {"thread": {"id": "thread-fixture-01", "turns": []}}, self.ordinal
        async def close(self):
            pass

    async def build():
        peer = Peer()
        chat = CodexDefaultAgentChat(DefaultAgentBinding("omarchy-fixture", "thread-fixture-01",
                                                         "ws://127.0.0.1:47311", "f" * 64), peer)
        await chat.attach()
        def notify(method, **params):
            peer.ordinal += 1
            peer.on_notification({"method": method, "params": {"threadId": "thread-fixture-01", **params}},
                                 peer.ordinal)
        notify("turn/started", turn={"id": "turn-fixture-01", "status": "inProgress"})
        notify("item/started", turnId="turn-fixture-01", item={
            "type": "commandExecution", "id": "item-fixture-build", "command": "make", "status": "inProgress"})
        notify("item/commandExecution/outputDelta", turnId="turn-fixture-01", itemId="item-fixture-build",
               delta="cc -c main.c\n")
        notify("item/commandExecution/outputDelta", turnId="turn-fixture-01", itemId="item-fixture-build",
               delta="cc -o app main.o\n")
        chat._flush_output()
        events = []
        while not chat.events.empty():
            events.append(chat.events.get_nowait())
        output = next(row for row in events if row["event"]["type"] == "tool.output")
        notify("item/completed", turnId="turn-fixture-01", item={
            "type": "commandExecution", "id": "item-fixture-build", "command": "make", "status": "completed",
            "aggregatedOutput": "".join("compile step %05d\n" % index for index in range(4000))})
        snapshot = chat.snapshot_value()
        assert snapshot["rows"][0]["detail_truncated"] > 0 and len(snapshot["rows"][0]["detail"]) == TOOL_DETAIL_LIMIT
        # The fixture keeps the row's shape, not 64 KB of it.
        snapshot["rows"][0]["detail"] = snapshot["rows"][0]["detail"][-38:]
        return {"agent-chat-event-tool-output.json": output, "agent-chat-snapshot-tool-output.json": snapshot}

    return asyncio.run(build())


def agent_collie1_fixtures():
    """AGENT-COLLIE-1: what real codex 0.160 frames become. The frames are the
    ones `tests/test_agent_collie1.py` replays (a private app-server with a
    scripted model, paths redacted): a fileChange waiting for approval with its
    change cards, six commands read into steps, and a question with
    descriptions, Other and a secret answer."""
    import asyncio
    sys.path.insert(0, str(ROOT / "tests"))
    from test_agent_collie1 import replay

    async def build():
        changes, _, _ = await replay("edit", until="serverRequest/resolved")
        steps, _, _ = await replay("steps")
        _, _, asked = await replay("ask", until="serverRequest/resolved")
        snapshot = steps.snapshot_value()
        for row in snapshot["rows"]:
            # The rows' shape, not every byte a command printed.
            if row["kind"] == "tool" and len(row["detail"]) > 80:
                row["detail"] = row["detail"][:80]
        events = [event for event in asked if event["type"] == "agent.approval.requested"]
        question = {"identity": steps.binding.identity() | {"providerSessionID": "thread-fixture-01"},
                    "sequence": 1, "event": events[0]}
        return {"agent-chat-snapshot-changes.json": changes.snapshot_value(),
                "agent-chat-snapshot-steps.json": snapshot,
                "agent-chat-event-question.json": question}

    return asyncio.run(build())


def agent_plan1_fixtures():
    """AGENT-PLAN-1: Plan mode, from the real codex 0.160 frames
    `tests/test_agent_plan1.py` replays (redacted): the conversation after a
    Plan turn (a question answered, the plan as a marked message), the plan's
    streamed message event, the chip's `POST /v1/agent/default/chat/mode` from
    the real service and adapter, and `/plan` from the shipped commands."""
    import asyncio
    import tempfile
    from types import SimpleNamespace
    sys.path.insert(0, str(ROOT / "tests"))
    from test_agent_plan1 import Peer, attached, replay, binding, CAPTURE
    from omodachi_core.agent_chat_provider import CodexDefaultAgentChat
    from omodachi_core.agent_chat_service import DefaultAgentChatService
    from omodachi_core.agent_commands import AgentCommands

    async def build():
        chat, peer = await attached()
        # Up to the Plan turn's end: one question answered, one plan.
        ends = [index for index, frame in enumerate(CAPTURE["plan"]) if frame["method"] == "turn/completed"]
        frames = iter(range(len(CAPTURE["plan"])))
        events = await replay("plan", chat, peer, until=lambda frame: next(frames) > ends[1])
        snapshot = chat.snapshot_value()
        plan = [event for event in events if event["type"] == "message" and event["value"].get("plan")][-1]
        event = {"identity": chat.binding.identity(), "sequence": 1, "event": plan}

        with tempfile.TemporaryDirectory() as temporary:
            class Owner:
                def owned_metadata(self): return None
                async def ensure_new_or_owned(self, existing_agent=False): return binding()
                def tui_argv(self, b): return ["codex"]
                async def close_proxy(self): pass

            async def connect(b, **options):
                adapter = CodexDefaultAgentChat(b, Peer(), **options)
                await adapter.attach()
                return adapter
            manager = SimpleNamespace(root=Path(temporary), cwd=Path(temporary) / "agent-workspace", probe=None)
            service = DefaultAgentChatService(manager, "omarchy-fixture", owner=Owner(), connector=connect)
            await service._attach(binding())
            mode = await service.set_mode({"mode": "plan"}, lambda: None)
            await service.close()

        commands_chat, _ = await attached()
        async def require(authorize): return commands_chat
        commands = AgentCommands(SimpleNamespace(require=require, manager=SimpleNamespace(cwd=Path("/home/fixture"))))
        command = await commands.execute("plan", {"revision": commands.revision, "arguments": "",
                                                  "request_id": "request-fixture-04"}, lambda: None)
        return {"agent-chat-snapshot-plan.json": snapshot, "agent-chat-event-plan.json": event,
                "agent-chat-mode.json": resource(mode), "agent-chat-command-plan.json": resource(command)}

    return asyncio.run(build())


def agent_queue1_fixtures():
    """AGENT-QUEUE-1, from the real codex 0.160 frames `tests/test_agent_queue1.py`
    replays (redacted): a thread opened on its newest page (`earlier`), two
    messages waiting in codex's queue, codex's plan checklist; the queue and
    checklist events; the queue route's answer from the real service; a page
    of earlier messages."""
    import asyncio
    import tempfile
    from types import SimpleNamespace
    sys.path.insert(0, str(ROOT / "tests"))
    from test_agent_queue1 import (CAPTURE, CHECKLIST_THREAD, Codex, PAGED_RESUME, PAGING, PAGING_THREAD,
                                   attached, binding, drain)
    from omodachi_core.agent_chat_provider import CodexDefaultAgentChat
    from omodachi_core.agent_chat_service import DefaultAgentChatService

    lists = [call for call in PAGING if call["method"] == "thread/turns/list" and call["conn"] == "cold"
             and call["params"].get("limit") == 5]
    pages = {lists[0]["result"]["nextCursor"]: dict(lists[1]["result"], nextCursor=None)}
    # The resume's first page as the capture's thread had it: hello 11 back to 7.
    resume = dict(PAGED_RESUME["result"], initialTurnsPage=lists[0]["result"])
    checklist = json.loads(json.dumps([frame for frame in CAPTURE["checklist"]
                                       if frame["method"] == "turn/plan/updated"]).replace(CHECKLIST_THREAD, PAGING_THREAD))

    async def build():
        peer = Codex(PAGING_THREAD, resume=resume, pages=pages)
        chat = await attached(peer)
        drain(chat)
        chat.active_turn = "turn-fixture-running"
        await chat.enqueue("And then run the tests on SQLite.", "request-fixture-q1")
        await chat.enqueue("Keep the old port as a fallback.", "request-fixture-q2")
        queued = [event for event in drain(chat) if event["type"] == "agent.queue.changed"][-1]
        for frame in checklist:
            peer.feed(frame)
        ticked = [event for event in drain(chat) if event["type"] == "agent.checklist.updated"][1]
        chat.active_turn = None
        snapshot = chat.snapshot_value()
        history = await chat.history(snapshot["rows"][0]["id"], 4)

        with tempfile.TemporaryDirectory() as temporary:
            class Owner:
                def owned_metadata(self): return None
                async def ensure_new_or_owned(self, existing_agent=False): return binding(PAGING_THREAD)
                def tui_argv(self, b): return ["codex"]
                async def close_proxy(self): pass

            async def connect(b, **options):
                adapter = CodexDefaultAgentChat(b, Codex(PAGING_THREAD, resume=resume, pages=pages),
                                                **options)
                await adapter.attach()
                return adapter
            manager = SimpleNamespace(root=Path(temporary), cwd=Path(temporary) / "agent-workspace", probe=None)
            service = DefaultAgentChatService(manager, "omarchy-fixture", owner=Owner(), connector=connect)
            await service._attach(binding(PAGING_THREAD))
            service.chat.active_turn = "turn-fixture-running"
            reply = await service.queue_add({"request_id": "request-fixture-q1",
                                             "text": "And then run the tests on SQLite."}, lambda: None)
            await service.close()
        identity = chat.binding.identity()
        return {"agent-chat-snapshot-queue.json": snapshot,
                "agent-chat-event-queue.json": {"identity": identity, "sequence": 1, "event": queued},
                "agent-chat-event-checklist.json": {"identity": identity, "sequence": 2, "event": ticked},
                "agent-chat-queue.json": resource(reply),
                "agent-chat-history.json": resource({"agent_id": "default", **history})}

    return asyncio.run(build())


def agent_new_conversation_fixtures():
    """AGENT-NEW-1: `POST /v1/agent/default/chat:new`, from the real owner,
    adapter and service, against a peer that keeps threads as codex does (a
    fresh thread is loaded and has no rollout until its first message)."""
    import asyncio
    import tempfile
    from types import SimpleNamespace
    from omodachi_core.agent_chat_owner import OwnedCodexAgent
    from omodachi_core.agent_chat_provider import AgentChatError
    from omodachi_core.agent_chat_service import DefaultAgentChatService

    class Peer:
        def __init__(self):
            self.ordinal = 0
            self.loaded = ["thread-fixture-01"]
            self.on_notification = lambda value, ordinal: None
            self.on_server_request = lambda value: None
            self.on_disconnect = lambda: None
        async def initialize(self): return {}
        async def request(self, method, params):
            result, _ = await self.request_fenced(method, params)
            return result
        async def request_fenced(self, method, params):
            self.ordinal += 1
            thread = params.get("threadId")
            if method == "thread/loaded/list":
                return {"data": list(self.loaded), "nextCursor": None}, self.ordinal
            if method == "thread/start":
                self.loaded.append("thread-fixture-02")
                return {"thread": {"id": "thread-fixture-02"}, "model": "gpt-6-astra",
                        "reasoningEffort": "medium"}, self.ordinal
            if thread == "thread-fixture-02" and method == "thread/resume":
                raise AgentChatError("provider_thread_rollout_missing")
            if thread == "thread-fixture-02" and method == "thread/read" and params.get("includeTurns"):
                raise AgentChatError("provider_thread_not_materialized")
            if method == "thread/read":
                return {"thread": {"id": thread, "turns": [], "status": {"type": "idle"}}}, self.ordinal
            return {"thread": {"id": thread, "turns": [], "status": {"type": "idle"}},
                    "model": "gpt-6-astra", "reasoningEffort": "high"}, self.ordinal
        async def respond(self, request_id, result): pass
        async def respond_error(self, request_id, code, message): pass
        async def close(self): pass

    async def build():
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            peer = Peer()
            async def proxy(): return peer
            # The workspace path is not part of the answer; a temporary one
            # keeps the generator from creating anything outside it.
            owner = OwnedCodexAgent(home / "structured-default", home / "agent-workspace",
                                    "omarchy-fixture", rpc_factory=proxy, agent_dir=home / "agent")
            owner.token = "f" * 64
            owner._write_endpoint(47311)
            owner._save(owner._record("thread-fixture-01", creation_pending=False))
            manager = SimpleNamespace(root=home, cwd=owner.cwd, probe=None)
            service = DefaultAgentChatService(manager, "omarchy-fixture", owner=owner)
            binding = await owner.ensure_new_or_owned()
            await service._attach(binding)
            result = await service.new_conversation({"confirmed": True,
                                                     "expected_provider_session_id": "thread-fixture-01"},
                                                    lambda: None)
            await service.close()
            return {"agent-chat-new.json": resource(result)}

    return asyncio.run(build())


def agent_command_fixtures():
    """AGENT-NEW-1: the slash catalog and three answers - `/model` opening the
    picker, `/model <id> <effort>` choosing in it, `/status` running - from
    the shipped `AgentCommands`, so `display` and `summary` are what ships."""
    import asyncio
    from types import SimpleNamespace
    from omodachi_core.agent_commands import AgentCommands

    async def rpc(method, params):
        if method == "model/list":
            return {"data": [{"id": "gpt-6-astra", "model": "gpt-6-astra", "displayName": "GPT-6-Astra",
                              "supportedReasoningEfforts": [{"reasoningEffort": "medium"}, {"reasoningEffort": "high"}]}],
                    "nextCursor": None}
        return {"thread": {"id": "thread-fixture-02", "cwd": "/home/fixture/.local/share/omodachi/agent-workspace",
                           "name": None, "status": {"type": "idle"}}}

    adapter = SimpleNamespace(binding=SimpleNamespace(thread_id="thread-fixture-02",
                                                      cwd="/home/fixture/.local/share/omodachi/agent-workspace"),
                              active_turn=None, rpc=SimpleNamespace(request=rpc),
                              settings={"model": "gpt-6-astra", "effort": "medium"})
    async def require(authorize): return adapter
    commands = AgentCommands(SimpleNamespace(require=require, manager=SimpleNamespace(cwd=Path("/home/fixture"))))

    async def build():
        rev = commands.revision
        run = lambda name, args, request: commands.execute(name, {"revision": rev, "arguments": args,
                                                                  "request_id": request}, lambda: None)
        return {"agent-chat-commands.json": resource(commands.listing()),
                "agent-chat-command-model.json": resource(await run("model", "", "request-fixture-01")),
                "agent-chat-command-model-selected.json": resource(await run("model", "gpt-6-astra high",
                                                                             "request-fixture-02")),
                "agent-chat-command-status.json": resource(await run("status", "", "request-fixture-03"))}

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
        # NOTIFY-3: the plugin's real click command, which names the request.
        request_id = "pair_" + "0" * 32
        summon = json_module.dumps({"view": "devices", "request_id": request_id}, separators=(",", ":"))
        (root / "1789711988810-1.json").write_text(json_module.dumps({
            "id": 1, "originalId": 1, "app": "Omodachi", "appIcon": "",
            "summary": "\u201cQA companion\u201d wants to connect",
            "body": "Open Omodachi to approve or reject it.", "image": "", "glyph": "",
            "execArgv": json_module.dumps(["omarchy-shell", "shell", "summon", "com.omodachi.host", summon],
                                          separators=(",", ":")),
            "urgency": 2, "expireTimeout": 30000, "timestamp": 1789711988810}))
        (root / "history/1789608484220-52.json").write_text(json_module.dumps({
            "id": 52, "app": "omarchy-action", "summary": "Theme set", "body": "gruvbox",
            "glyph": "", "urgency": 1, "expireTimeout": 5000, "timestamp": 1789608484220}))
        waiting = {"pair": {request_id}, "ssh": set()}
        hub = Hub()
        mirror = NotificationMirror(hub, state_dir=root, pending_requests=lambda: waiting)
        mirror.scan()
        listing = resource(mirror.history())
        posted = next(row for row in listing["notifications"] if row["id"] == "1789711988810-1")
        # Approved on the computer: the next pass withdraws it.
        waiting["pair"] = set()
        mirror.scan()
        withdrawn = next(event for event in hub.events_since(0)
                         if event.type == "notification.updated" and event.payload.get("withdrawn"))
        withdrawn = replace(withdrawn, seq=4, event_id="evt_00000004", ts=NOW.timestamp())
        return {"notifications.json": listing,
                "event-notification-withdrawn.json": resource(asdict(withdrawn)),
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
        # RELEASE-9: the root helper mints these; drawn from the same pinned
        # sequence, in the same order, so the fixture bytes do not move.
        approval_id = "appr_" + biometric.secrets.token_hex(16)
        nonce = biometric.secrets.token_urlsafe(32)
        task = asyncio.create_task(broker.request({"service": "sudo", "user": "alex",
                                                   "requester": "alex", "tty": "pts/3",
                                                   "timeout": 45, "protocol": 2,
                                                   "approval_id": approval_id, "nonce": nonce,
                                                   "host_id": Identity.host_id}))
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
    if name.startswith("agent-chat-command-"): return "agent-chat-command.schema.json"
    if name in ("agent-chat-snapshot-links.json", "agent-chat-snapshot-tool-output.json",
                "agent-chat-snapshot-changes.json", "agent-chat-snapshot-steps.json",
                "agent-chat-snapshot-plan.json", "agent-chat-snapshot-queue.json"):
        return "agent-chat-snapshot.schema.json"
    if name.startswith("host-open"): return "host-open.schema.json"
    if name == "links-detect.json": return "links-detect.schema.json"
    if name.startswith("agent-chat-"): return name.removesuffix(".json") + ".schema.json"
    if name == "health.json": return "health.schema.json"
    if name in {"host-roads.json", "host-roads-odomo.json", "host-roads-odomo-none.json"}: return "host-roads.schema.json"
    if name.startswith("push-payload-"): return "push-payload.schema.json"
    if name.startswith("push-"): return name.removesuffix(".json") + ".schema.json"
    if name == "presence.json": return "presence.schema.json"
    if name == "remote-capabilities-relayed.json": return "remote-capabilities.schema.json"
    if name == "theme.json": return "theme.schema.json"
    if name == "fonts.json": return "fonts.schema.json"
    if name == "herdr-layout.json": return "herdr-layout.schema.json"
    if name == "herdr-sessions.json": return "herdr-sessions.schema.json"
    if name in {"herdr-session-start.json", "herdr-session-create.json"}: return "herdr-session-start.schema.json"
    if name.startswith("herdr-workspace-create"): return "herdr-workspace-create.schema.json"
    if name.startswith("herdr-reply"): return "herdr-reply.schema.json"
    if name.startswith("herdr-folders"): return "herdr-folders.schema.json"
    if name == "herdr-keys.json": return "herdr-keys.schema.json"
    if name.startswith("herdr-text"): return "herdr-text.schema.json"
    if name == "upload-removed.json": return "upload-removed.schema.json"
    if name.startswith("upload-"): return "upload.schema.json"
    if name.startswith("herdr-operator-rows"): return "herdr-operator-rows.schema.json"
    if name in {"pairing-claim.json", "pairing-claim-web.json"}: return "pairing-claim.schema.json"
    if name == "ws-ticket.json": return "ws-ticket.schema.json"
    if name == "pairing-credential.json": return "pairing-credential.schema.json"
    if name == "pairing-renew.json": return "pairing-renew.schema.json"
    if name == "ssh-key.json": return "ssh-key.schema.json"
    if name == "ssh-key-state.json": return "ssh-key-state.schema.json"
    if name == "capabilities.json": return "capabilities.schema.json"
    if name in {"state.json", "state-agent-activity.json", "state-notify1.json", "state-overview1.json",
                "state-herdr11-catalog-unchanged.json"}: return "state.schema.json"
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
