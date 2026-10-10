"""TAIL-2: the tailnet readings and what the wire does with them.

Every reading is driven through a fake `runner` that answers the exact argv
the module asks for, so nothing here needs Tailscale, `ip`, or a network. The
values are synthetic (100.64.0.0/10 test addresses, `tail0000.ts.net`).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import aiohttp
from aiohttp import WSMsgType

from omodachi_core import network, tailnet
from omodachi_core.auth import DeviceAuthenticator
from omodachi_core.bootstrap import create_service
from omodachi_core.host_identity import HostIdentity, typed_endpoints
from omodachi_core.hub import Hub
from omodachi_core.lan_discovery import (DiscoveryConfig, DiscoveryError, ReadyListener,
                                         descriptor_for)
from omodachi_core.network import NetworkServer, apply_road, on_this_road
from omodachi_core.pairing import PairingStore, clean_via

STATUS_RUNNING = {
    "BackendState": "Running",
    "Self": {"DNSName": "omodachi-lab.tail0000.ts.net.",
             "TailscaleIPs": ["100.64.0.11", "fd7a:115c:a1e0::b"]},
    "Peer": {
        "nodekey:direct": {"TailscaleIPs": ["100.64.0.20"], "CurAddr": "192.0.2.4:41641", "Relay": "syd"},
        "nodekey:relayed": {"TailscaleIPs": ["100.64.0.21"], "CurAddr": "", "Relay": "syd"},
    },
}
PREFS = {"RunSSH": False, "RouteAll": True, "NetfilterMode": 2}


class Runner:
    """Answers argv by prefix; records every call."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        for prefix, (code, value) in self.answers.items():
            if tuple(argv[:len(prefix)]) == prefix:
                text = value if isinstance(value, str) else json.dumps(value)
                return type("R", (), {"returncode": code, "stdout": text})()
        return type("R", (), {"returncode": 1, "stdout": ""})()


def which(name):
    return "/usr/bin/" + name if name == "tailscale" else None


def runner(status=STATUS_RUNNING, prefs=PREFS, **extra):
    answers = {("/usr/bin/tailscale", "status", "--json"): (0, status),
               ("/usr/bin/tailscale", "debug", "prefs"): (0, prefs)}
    answers.update(extra)
    return Runner(answers)


class AddressTests(unittest.TestCase):
    def test_tailnet_ranges(self):
        for value in ("100.64.0.1", "100.64.12.11", "100.127.255.254", "fd7a:115c:a1e0::a42b:7c0c",
                      "::ffff:100.100.1.1"):
            self.assertTrue(tailnet.is_tailnet_address(value), value)
        for value in ("100.63.255.255", "100.128.0.1", "10.201.201.20", "192.168.1.10",
                      "fd42::1", "fe80::1", "", None, "omarchy.local"):
            self.assertFalse(tailnet.is_tailnet_address(value), value)

    def test_kinds(self):
        self.assertEqual(tailnet.address_kind("100.64.0.5"), "tailnet")
        self.assertEqual(tailnet.address_kind("192.168.1.20"), "lan")
        self.assertEqual(tailnet.address_kind("127.0.0.1"), "loopback")
        self.assertEqual(tailnet.address_kind("8.8.8.8"), "other")
        self.assertEqual(tailnet.address_kind(None), "other")


class ReadingTests(unittest.TestCase):
    def test_running_node(self):
        value = tailnet.reading(runner=runner(), which=which)
        self.assertEqual(value["dns_name"], "omodachi-lab.tail0000.ts.net")
        self.assertEqual(value["ips"], ["100.64.0.11", "fd7a:115c:a1e0::b"])
        self.assertEqual((value["running"], value["ssh"], value["accept_routes"]), (True, False, True))
        self.assertEqual(tailnet.public(value),
                         {"dns_name": "omodachi-lab.tail0000.ts.net", "ips": ["100.64.0.11", "fd7a:115c:a1e0::b"]})
        # Other nodes are never collected into the reading.
        self.assertNotIn("100.64.0.20", json.dumps(value))

    def test_stopped_logged_out_and_absent(self):
        for state in ("Stopped", "NeedsLogin", "Starting"):
            value = tailnet.reading(runner=runner(status={"BackendState": state}), which=which)
            self.assertFalse(value["running"])
            self.assertIsNone(tailnet.public(value))
        value = tailnet.reading(runner=runner(), which=lambda name: None)
        self.assertEqual((value["installed"], value["running"]), (False, False))
        broken = tailnet.reading(runner=Runner({}), which=which)
        self.assertEqual((broken["installed"], broken["running"], broken["backend_state"]), (True, False, None))

    def test_tailscale_ssh_is_read_from_prefs(self):
        value = tailnet.reading(runner=runner(prefs={"RunSSH": True}), which=which)
        self.assertTrue(value["ssh"])
        # Prefs that cannot be read say "unknown", not "off".
        value = tailnet.reading(runner=runner(prefs="not json"), which=which)
        self.assertIsNone(value["ssh"])

    def test_a_name_outside_ts_net_is_not_published(self):
        status = {**STATUS_RUNNING, "Self": {"DNSName": "evil.example.com.", "TailscaleIPs": ["100.64.0.11"]}}
        self.assertIsNone(tailnet.reading(runner=runner(status=status), which=which)["dns_name"])


class WhoisAndPathTests(unittest.TestCase):
    def test_whois_names_the_node_and_its_login(self):
        answer = {"Node": {"ComputedName": "iphone-a", "Name": "iphone-a.tail0000.ts.net."},
                  "UserProfile": {"LoginName": "someone@example.com", "DisplayName": "Someone"}}
        run = Runner({("/usr/bin/tailscale", "whois", "--json"): (0, answer)})
        self.assertEqual(tailnet.whois("100.64.0.20", runner=run, which=which),
                         {"kind": "tailnet", "peer_name": "iphone-a", "login": "someone@example.com"})
        self.assertEqual(run.calls[0][-1], "100.64.0.20")

    def test_whois_of_a_tagged_node_has_no_login(self):
        answer = {"Node": {"Name": "ci-box.tail0000.ts.net."}, "UserProfile": {"LoginName": "tagged-devices"}}
        run = Runner({("/usr/bin/tailscale", "whois", "--json"): (0, answer)})
        self.assertEqual(tailnet.whois("100.64.0.20", runner=run, which=which),
                         {"kind": "tailnet", "peer_name": "ci-box", "login": None})

    def test_whois_is_never_asked_about_a_lan_peer(self):
        run = Runner({})
        self.assertEqual(tailnet.whois("192.168.1.20", runner=run, which=which),
                         {"kind": "lan", "peer_name": None, "login": None})
        self.assertEqual(run.calls, [])

    def test_peer_path(self):
        self.assertEqual(tailnet.peer_path(STATUS_RUNNING, "100.64.0.20"), {"kind": "tailnet", "relayed": False})
        self.assertEqual(tailnet.peer_path(STATUS_RUNNING, "100.64.0.21"), {"kind": "tailnet", "relayed": True})
        self.assertEqual(tailnet.peer_path(STATUS_RUNNING, "100.64.0.99"), {"kind": "tailnet", "relayed": None})
        self.assertEqual(tailnet.peer_path(None, "100.64.0.21"), {"kind": "tailnet", "relayed": None})
        self.assertEqual(tailnet.peer_path(STATUS_RUNNING, "192.168.1.20"), {"kind": "lan", "relayed": None})


class RoutesAndReportTests(unittest.TestCase):
    ADDR = [{"ifname": "lo", "addr_info": [{"local": "127.0.0.1", "prefixlen": 8}]},
            {"ifname": "enp5s0", "addr_info": [{"local": "10.201.201.20", "prefixlen": 24},
                                               {"local": "fe80::1", "prefixlen": 64}]},
            {"ifname": "tailscale0", "addr_info": [{"local": "100.64.0.11", "prefixlen": 32}]}]

    def routes(self, table52):
        return Runner({("ip", "-j", "addr"): (0, self.ADDR),
                       ("ip", "-j", "-4", "route", "show", "table", "52"): (0, table52),
                       ("ip", "-j", "-6", "route", "show", "table", "52"): (0, []),
                       ("/usr/bin/tailscale", "status", "--json"): (0, STATUS_RUNNING),
                       ("/usr/bin/tailscale", "debug", "prefs"): (0, PREFS)})

    def test_lan_prefixes_skip_loopback_link_local_and_the_tailnet(self):
        self.assertEqual(tailnet.lan_prefixes(runner=self.routes([])), ["10.201.201.0/24"])

    def test_a_route_covering_this_lan_is_reported(self):
        # TAIL-1 §2.2: a subnet router advertising the host's own /24.
        table = [{"dst": "100.64.0.20", "dev": "tailscale0"}, {"dst": "10.201.0.0/16", "dev": "tailscale0"},
                 {"dst": "192.168.50.0/24", "dev": "tailscale0"}, {"dst": "default", "dev": "tailscale0"}]
        self.assertEqual(tailnet.captured_lan_routes(runner=self.routes(table)), ["10.201.0.0/16"])

    def test_host_report_warnings(self):
        table = [{"dst": "10.201.201.0/24", "dev": "tailscale0"}]
        report = tailnet.host_report(runner=self.routes(table), which=which)
        ids = [row["id"] for row in report["warnings"]]
        self.assertEqual(ids, ["tailnet_firewall", "accept_routes_lan"])
        fix = report["warnings"][1]
        self.assertEqual(fix["command"], "tailscale set --accept-routes=false")
        self.assertIn("10.201.201.0/24", fix["text"])
        self.assertIn("ts-input", report["warnings"][0]["text"])

    def test_host_report_names_tailscale_ssh(self):
        run = self.routes([])
        run.answers[("/usr/bin/tailscale", "debug", "prefs")] = (0, {"RunSSH": True, "RouteAll": False})
        report = tailnet.host_report(runner=run, which=which)
        self.assertEqual([row["id"] for row in report["warnings"]], ["tailnet_firewall", "tailscale_ssh"])
        self.assertIn("revoking a device here do not apply", report["warnings"][1]["text"])
        # With accept-routes off, the route table is not even read.
        self.assertFalse([call for call in run.calls if "route" in call])

    def test_no_tailscale_no_warnings(self):
        report = tailnet.host_report(runner=Runner({}), which=lambda name: None)
        self.assertEqual((report["running"], report["warnings"]), (False, []))


class EndpointTests(unittest.TestCase):
    def test_order_and_kinds(self):
        reading = {"running": True, "dns_name": "omarchy.tail0000.ts.net", "ips": ["100.64.0.9", "fd7a:115c:a1e0::9"]}
        rows = typed_endpoints(["fd42::5", "100.64.0.9", "192.168.1.10"], "omarchy", 8099, reading)
        self.assertEqual([(row["host"], row["kind"]) for row in rows], [
            ("192.168.1.10", "lan"), ("fd42::5", "lan"), ("omarchy.local", "mdns"),
            ("omarchy.tail0000.ts.net", "tailnet"), ("100.64.0.9", "tailnet"), ("fd7a:115c:a1e0::9", "tailnet")])

    def test_a_stopped_tailnet_contributes_no_name(self):
        rows = typed_endpoints(["192.168.1.10"], "omarchy", 8099, {"running": False, "dns_name": "x.ts.net"})
        self.assertEqual([row["kind"] for row in rows], ["lan", "mdns"])

    def test_bounded(self):
        rows = typed_endpoints([f"10.0.0.{n}" for n in range(1, 30)], "omarchy", 8099)
        self.assertEqual(len(rows), 16)

    def test_monitor_survives_a_failing_reader(self):
        monitor = tailnet.TailnetMonitor(reader=lambda: 1 / 0)
        self.assertIsNone(monitor.refresh())
        self.assertIsNone(monitor.current())
        identity = HostIdentity("9" * 32, "omarchy", addresses=["192.168.1.10"], tailnet=lambda: 1 / 0)
        self.assertNotIn("tailnet", identity.roads())
        self.assertEqual(set(identity.health()), {"host_id", "tls_fingerprint_sha256"})


class BonjourTests(unittest.TestCase):
    """TAIL-2 P1-3: the LAN record never carries a tailnet address."""

    def test_descriptor_drops_tailnet_addresses(self):
        config = DiscoveryConfig("omarchy", "omarchy.local.", ("10.201.201.20",))
        with self.assertRaises(DiscoveryError) as refused:
            DiscoveryConfig("omarchy", "omarchy.local.", ("10.201.201.20", "100.64.12.11"))
        self.assertEqual(refused.exception.code, "discovery_address_tailnet")
        listener = ReadyListener(True, (("0.0.0.0", 8099),))
        self.assertEqual(descriptor_for(config, listener).addresses, ("10.201.201.20",))

    def test_a_tailnet_only_listener_advertises_nothing(self):
        # TAIL-1 §7.1: listening on 100.x only still advertised `['100.64.12.11']`.
        config = DiscoveryConfig("omarchy", "omarchy.local.", ("10.201.201.20",))
        with self.assertRaises(DiscoveryError) as refused:
            descriptor_for(config, ReadyListener(True, (("100.64.12.11", 8099),)))
        self.assertEqual(refused.exception.code, "discovery_no_lan_listener")

    def test_daemon_auto_addresses_exclude_the_tailnet(self):
        from omodachi_core import cli
        source = Path(cli.__file__).read_text()
        self.assertIn("not is_tailnet_address(value)", source)


class WireHelperTests(unittest.TestCase):
    class Request:
        def __init__(self, sockname):
            self.transport = type("T", (), {"get_extra_info": lambda self, key: sockname})()

    SESSION = {"contract_revision": "omodachi.v1", "session": {"id": "rs_x", "connection": {
        "backend": "sunshine", "host": "10.201.201.20", "https_port": 47984}}}

    def test_sunshine_connection_names_the_road_in_use(self):
        moved = on_this_road(self.SESSION, self.Request(("100.64.0.11", 8099)))
        self.assertEqual(moved["session"]["connection"]["host"], "100.64.0.11")
        self.assertEqual(self.SESSION["session"]["connection"]["host"], "10.201.201.20")
        lan = on_this_road(self.SESSION, self.Request(("10.201.201.20", 8099)))
        self.assertEqual(lan["session"]["connection"]["host"], "10.201.201.20")
        # Loopback (tests, the simulator backend) keeps the backend's answer.
        self.assertIs(on_this_road(self.SESSION, self.Request(("127.0.0.1", 8099))), self.SESSION)
        vnc = {"session": {"connection": {"backend": "vnc", "path": "/v1/remote/sessions/rs_x/vnc"}}}
        self.assertIs(on_this_road(vnc, self.Request(("100.64.0.11", 8099))), vnc)
        self.assertEqual(on_this_road({"session": None}, self.Request(None)), {"session": None})

    RUNNING = {"running": True, "dns_name": "omarchy.tail0000.ts.net", "ips": ["100.64.0.11"]}

    def test_on_the_tailnet_the_connection_names_the_magicdns_name(self):
        """REMOTE-FIX-2: iOS ATS refuses the fork's certificate on a 100.x
        literal (-1200 / -9802); a Tailscale host is its MagicDNS name."""
        moved = on_this_road(self.SESSION, self.Request(("100.64.0.11", 8099)), self.RUNNING)
        self.assertEqual(moved["session"]["connection"]["host"], "omarchy.tail0000.ts.net")
        v6 = on_this_road(self.SESSION, self.Request(("fd7a:115c:a1e0::5a2b:390c", 8099, 0, 0)), self.RUNNING)
        self.assertEqual(v6["session"]["connection"]["host"], "omarchy.tail0000.ts.net")
        # Everything else in the connection is the backend's own.
        self.assertEqual(moved["session"]["connection"]["https_port"], 47984)
        self.assertEqual(self.SESSION["session"]["connection"]["host"], "10.201.201.20")

    def test_the_lan_road_keeps_its_address_whatever_tailscale_says(self):
        lan = on_this_road(self.SESSION, self.Request(("10.201.201.20", 8099)), self.RUNNING)
        self.assertEqual(lan["session"]["connection"]["host"], "10.201.201.20")
        self.assertIs(on_this_road(self.SESSION, self.Request(("127.0.0.1", 8099)), self.RUNNING), self.SESSION)

    def test_without_a_magicdns_name_the_literal_is_the_fallback(self):
        for reading in (None, {"running": False, "dns_name": "omarchy.tail0000.ts.net"},
                        {"running": True, "dns_name": None}, {"running": True, "dns_name": "omarchy.example.com"}):
            moved = on_this_road(self.SESSION, self.Request(("100.64.0.11", 8099)), reading)
            self.assertEqual(moved["session"]["connection"]["host"], "100.64.0.11", reading)

    def test_the_session_route_hands_the_cached_reading_over(self):
        from omodachi_core import network
        source = Path(network.__file__).read_text()
        self.assertIn("on_this_road(result, request, reading)", source)
        self.assertIn("service.host_identity.tailnet_reading()", source)

    def test_a_relayed_peer_defaults_to_vnc_and_says_why(self):
        capabilities = {"default_backend": "sunshine",
                        "backends": {"sunshine": {"available": True}, "vnc": {"available": True}}}
        relayed = apply_road(capabilities, "tailnet", True)
        self.assertEqual(relayed["default_backend"], "vnc")
        self.assertEqual(relayed["road"], {"kind": "tailnet", "relayed": True, "reason": "tailnet_relayed"})
        direct = apply_road(capabilities, "tailnet", False)
        self.assertEqual((direct["default_backend"], direct["road"]["reason"]), ("sunshine", None))
        no_vnc = apply_road({**capabilities, "backends": {"vnc": {"available": False}}}, "tailnet", True)
        self.assertEqual((no_vnc["default_backend"], no_vnc["road"]["reason"]), ("sunshine", None))

    def test_via_is_cleaned(self):
        self.assertIsNone(clean_via({"kind": "satellite"}))
        self.assertIsNone(clean_via("tailnet"))
        self.assertEqual(clean_via({"kind": "tailnet", "peer_name": "a‮b\n", "login": "x" * 400}),
                         {"kind": "tailnet", "peer_name": "ab", "login": "x" * 128})


class FakeTools:
    def __init__(self, relayed=True):
        self.relayed, self.asked = relayed, []

    def whois(self, address):
        self.asked.append(address)
        return {"kind": "tailnet", "peer_name": "iphone-a", "login": "someone@example.com"}

    def status_json(self):
        return {"Peer": {"n": {"TailscaleIPs": ["127.0.0.1"], "CurAddr": "" if self.relayed else "192.0.2.4:1",
                               "Relay": "syd"}}}

    @staticmethod
    def peer_path(status, address):
        return tailnet.peer_path(status, address)


class WireTests(unittest.IsolatedAsyncioTestCase):
    """Over real loopback HTTP, with loopback standing in for a tailnet peer."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.authority = DeviceAuthenticator.from_file(root / "device.secret")
        self.hub = Hub(authenticator=self.authority, auth_check_interval=0.05)
        self.service = create_service(self.hub, demo=True)
        self.service.pairing = PairingStore(self.authority, root / "pairing.json")
        self.service.tailnet_tools = self.tools = FakeTools()
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.close()
        await self.service.close_media()
        self.temp.cleanup()

    async def test_a_tailnet_request_carries_whois_not_an_address_to_show(self):
        with mock.patch.object(network.tailnet_module, "address_kind", lambda value: "tailnet"):
            async with self.client.post(self.url + "/v1/pairing/requests",
                                        json={"device_id": "ipad", "device_name": "iPad"}) as response:
                row = await response.json()
        self.assertEqual(response.status, 200, row)
        self.assertEqual(row["via"], {"kind": "tailnet", "peer_name": "iphone-a", "login": "someone@example.com"})
        self.assertEqual(self.tools.asked, ["127.0.0.1"])
        self.assertEqual(self.service.pairing.pending()["requests"][0]["via"]["peer_name"], "iphone-a")

    async def test_a_lan_request_never_asks_tailscale(self):
        async with self.client.post(self.url + "/v1/pairing/requests",
                                    json={"device_id": "ipad", "device_name": "iPad"}) as response:
            row = await response.json()
        self.assertEqual(row["via"]["kind"], "loopback")
        self.assertEqual(self.tools.asked, [])

    async def test_remote_capabilities_carry_the_road(self):
        token = self.hub.register_device("ipad")
        headers = {"Authorization": "Bearer " + token}
        async with self.client.get(self.url + "/v1/remote/capabilities", headers=headers) as response:
            body = await response.json()
        self.assertEqual(body["road"], {"kind": "loopback", "relayed": None, "reason": None})
        with mock.patch.object(network.tailnet_module, "address_kind", lambda value: "tailnet"):
            async with self.client.get(self.url + "/v1/remote/capabilities", headers=headers) as response:
                body = await response.json()
        self.assertEqual(body["road"]["kind"], "tailnet")
        self.assertTrue(body["road"]["relayed"])

    async def test_the_event_stream_answers_a_client_ping(self):
        # TAIL-2 P0-1: the client's liveness check is a WebSocket ping; core
        # must answer it (and pings the client itself every 20 s).
        token = self.hub.register_device("ipad")
        ws = await self.client.ws_connect(self.url + "/v1/events", headers={"Authorization": "Bearer " + token},
                                          autoping=False)
        first = await ws.receive(timeout=5)
        self.assertEqual(first.type, WSMsgType.TEXT)
        await ws.ping(b"tail-2")
        reply = await ws.receive(timeout=5)
        self.assertEqual((reply.type, reply.data), (WSMsgType.PONG, b"tail-2"))
        await ws.close()


if __name__ == "__main__":
    unittest.main()
