"""LITE-1a C1-C5: Odomo served by core, and the gates a browser needs.

Every test here talks to a real loopback listener. The browser is aiohttp with
the headers a browser would send (`Origin`, `Host`), and `tailscale serve` is
a loopback peer that adds `X-Forwarded-For` / `Tailscale-User-Login`, which is
exactly what LITE-0 §4.5 measured it doing.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import aiohttp

from omodachi_core import browser_gate, network
from omodachi_core.auth import DeviceAuthenticator
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.odomo import CSP, OdomoBundle, build_id, load
from omodachi_core.network import NetworkServer
from omodachi_core.pairing import PairingStore, client_label
from omodachi_core.service import ServiceError

ROOT = Path(__file__).resolve().parents[1]
TS_NAME = "odomo-a.example-tailnet.ts.net"
HASHED = "assets/index-AbCd_123.js"


def write_bundle(root: Path):
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text('<!doctype html><script type="module" src="./assets/index-AbCd_123.js"></script>')
    (root / HASHED).write_text("console.log('odomo')")
    (root / "sw.js").write_text("self.addEventListener('fetch', () => {})")
    (root / "manifest.webmanifest").write_text('{"name": "Odomo"}')
    (root / ".hidden.js").write_text("secret")
    (root / "notes.md").write_text("not a type Odomo serves")
    outside = root.parent / "outside.txt"
    outside.write_text("outside the bundle")
    os.symlink(outside, root / "link.txt")


class _FakeTailnet:
    def __init__(self):
        self.asked, self.logins = [], {}

    def whois(self, address):
        self.asked.append(address)
        return {"kind": "tailnet", "peer_name": "phone-" + address.replace(".", "-"),
                "login": self.logins.get(address)}


class _Base(unittest.IsolatedAsyncioTestCase):
    """A daemon with a real credential registry and pairing store."""

    async def asyncSetUp(self):
        # The host's own addresses are `ip -j addr` on the host; on a test
        # machine without `ip` the fallback is a name lookup that can stall.
        patcher = patch.object(browser_gate, "_addresses", return_value=["192.0.2.10"])
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(browser_gate, "tailscale_dns_name", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.auth = DeviceAuthenticator.from_file(base / "device.secret")
        self.hub = Hub(authenticator=self.auth, auth_check_interval=0.05)
        self.service = create_service(self.hub, demo=True)
        self.store = PairingStore(self.auth, base / "pairing.json")
        self.service.pairing = self.store
        write_bundle(base / "odomo")
        self.service.odomo_bundle = OdomoBundle(base / "odomo")
        self.service.odomo_extra_host_names = {TS_NAME}
        # Never this machine's own tailscaled: whois on a tailnet peer is a
        # fake that knows a node name and, for one address, a login.
        self.service.tailnet_tools = self.tailnet_tools = _FakeTailnet()
        self.native = self.auth.issue("ipad-native", device_name="iPad").token
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.port = self.server.bound_port
        self.host = f"127.0.0.1:{self.port}"
        self.url = f"http://{self.host}"
        self.origin = f"http://{self.host}"
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))

    async def asyncTearDown(self):
        await self.client.close()
        await asyncio.wait_for(self.server.close(), 3)
        self.temp.cleanup()

    async def call(self, method, path, *, token=None, origin=None, host=None, body=None, headers=None):
        sent = dict(headers or {})
        if token:
            sent["Authorization"] = "Bearer " + token
        if origin:
            sent["Origin"] = origin
        if host:
            sent["Host"] = host
        async with self.client.request(method, self.url + path, headers=sent, json=body,
                                       allow_redirects=False) as response:
            raw = await response.read()
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw
            return response.status, value, response.headers

    async def pair_web(self, device_id="odomo-a", *, headers=None, label="Safari on iPhone"):
        """What Odomo does: a browser POST (so it carries Origin), approve, claim."""
        status, request, _ = await self.call("POST", "/v1/pairing/requests", origin=self.origin, headers=headers,
                                             body={"device_id": device_id, "device_name": label,
                                                   "client": {"kind": "web", "label": label}})
        self.assertEqual(status, 200, request)
        await self.service.dispatch_local_async("local.pair.approve", {"request_id": request["request_id"]},
                                                local_authorize=lambda: True)
        status, claim, _ = await self.call("POST", f"/v1/pairing/requests/{request['request_id']}/claim",
                                           origin=self.origin, body={"request_secret": request["request_secret"]})
        self.assertEqual(status, 200, claim)
        return request, claim


class StaticServingTests(_Base):
    """C1."""

    async def test_index_has_exact_csp_headers_and_revalidates(self):
        status, body, headers = await self.call("GET", "/odomo/")
        self.assertEqual(status, 200)
        self.assertIn(b"assets/index-AbCd_123.js", body)
        self.assertEqual(headers["Content-Security-Policy"], CSP)
        self.assertEqual(CSP, "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
                              "connect-src 'self'; worker-src 'self'; manifest-src 'self'; frame-ancestors 'none'; "
                              "base-uri 'none'; form-action 'none'")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(headers["X-Omodachi-Odomo-Build"], self.service.odomo_bundle.build)
        etag = headers["ETag"]
        status, _, again = await self.call("GET", "/odomo/", headers={"If-None-Match": etag})
        self.assertEqual(status, 304)
        self.assertEqual(again["ETag"], etag)
        self.assertEqual(again["Content-Security-Policy"], CSP)

    async def test_hashed_assets_are_immutable_and_the_rest_no_cache(self):
        status, _, headers = await self.call("GET", "/odomo/" + HASHED)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "public, max-age=31536000, immutable")
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        for path, kind in (("sw.js", "text/javascript"), ("manifest.webmanifest", "application/manifest+json")):
            status, _, headers = await self.call("GET", "/odomo/" + path)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-cache")
            self.assertTrue(headers["Content-Type"].startswith(kind))
        async with self.client.head(self.url + "/odomo/sw.js") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), b"")

    async def test_only_listed_paths_no_traversal_no_symlink(self):
        for path in ("/odomo/../pyproject.toml", "/odomo/%2e%2e/pyproject.toml", "/odomo/assets/../../device.secret",
                     "/odomo/link.txt", "/odomo/.hidden.js", "/odomo/notes.md", "/odomo/assets/",
                     "/odomo/..%2fdevice.secret", "/odomo//etc/passwd", "/odomo/nothing.js"):
            status, body, _ = await self.call("GET", path)
            # The client may normalise `..` away before sending, which lands
            # outside /odomo and so on the authenticated API (401): either way
            # nothing outside the bundle is ever read.
            self.assertIn(status, (401, 404), path)
            self.assertNotIn(b"outside the bundle", body if isinstance(body, bytes) else json.dumps(body).encode())
        self.assertNotIn("link.txt", self.service.odomo_bundle.assets)
        self.assertNotIn(".hidden.js", self.service.odomo_bundle.assets)

    async def test_redirects_and_auth_exemption_is_get_only(self):
        status, _, headers = await self.call("GET", "/odomo")
        self.assertEqual((status, headers["Location"]), (301, "/odomo/"))
        status, _, headers = await self.call("GET", "/", headers={"Accept": "text/html,application/xhtml+xml"})
        self.assertEqual((status, headers["Location"]), (302, "/odomo/"))
        status, body, _ = await self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body["web_ui"], "/odomo/")
        status, body, _ = await self.call("POST", "/odomo/index.html", body={})
        self.assertEqual(status, 401)
        # the API keeps no-store
        status, _, headers = await self.call("GET", "/v1/state", token=self.native)
        self.assertEqual((status, headers["Cache-Control"]), (200, "no-store"))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    async def test_the_old_lite_path_redirects_to_odomo_for_get_only(self):
        # LITE-1c: the web app's old address. Same tail, same query, still encoded.
        for path, location in (("/lite", "/odomo/"), ("/lite/", "/odomo/"),
                               ("/lite/sw.js", "/odomo/sw.js"),
                               ("/lite/?open=agent&ref=approval", "/odomo/?open=agent&ref=approval"),
                               ("/lite/a%20b?x=%0D%0A", "/odomo/a%20b?x=%0D%0A")):
            status, _, headers = await self.call("GET", path)
            self.assertEqual((status, headers.get("Location")), (301, location), path)
        # A line break in the path never reaches a header: no route matches it.
        status, _, headers = await self.call("GET", "/lite/a%0d%0ab")
        self.assertEqual((status, headers.get("Location")), (404, None))
        async with self.client.head(self.url + "/lite/") as response:
            self.assertEqual((response.status, response.headers["Location"]), (301, "/odomo/"))
        status, _, _ = await self.call("POST", "/lite/", body={})
        self.assertEqual(status, 401)
        # The same Host check as /odomo/ itself.
        status, _, _ = await self.call("GET", "/lite/", host="evil.example")
        self.assertEqual(status, 403)
        # `/literal` is not the old path.
        status, _, _ = await self.call("GET", "/literal")
        self.assertEqual(status, 401)

    async def test_unbuilt_core_says_so(self):
        self.service.odomo_bundle = OdomoBundle(Path(self.temp.name) / "missing")
        app = network.create_app(self.service)
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/odomo/")
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())["error"]["code"], "odomo_unavailable")
            response = await client.get("/", headers={"Accept": "text/html"}, allow_redirects=False)
            self.assertEqual(response.status, 200)

    def test_build_id_is_a_function_of_paths_and_bytes(self):
        assets = load(Path(self.temp.name) / "odomo")
        self.assertEqual(build_id(assets), build_id(dict(reversed(list(assets.items())))))
        self.assertRegex(build_id(assets), r"^[0-9a-f]{16}$")

    def test_wheel_carries_the_built_app(self):
        text = (ROOT / "pyproject.toml").read_text()
        self.assertIn('"data/odomo/**/*"', text)
        real = OdomoBundle()
        self.assertTrue(real, "src/omodachi_core/data/odomo has no index.html - run scripts/verify_odomo.py --write")
        self.assertIn("sw.js", real.assets)
        self.assertIn("manifest.webmanifest", real.assets)
        manifest = json.loads(real.assets["manifest.webmanifest"].body)
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["id"], "/odomo/")
        for asset in real.assets.values():
            if asset.content_type.startswith("text/html"):
                # no inline script: every <script> has a src
                html = asset.body.decode()
                self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)[^>]*>")
                self.assertNotIn("<style", html)


class HostOriginTests(_Base):
    """C2."""

    async def test_matrix(self):
        rows = [
            # (path, token, Host, Origin, expected status)
            ("/odomo/", None, None, None, 200),
            ("/odomo/", None, "evil.example", None, 403),
            ("/odomo/", None, f"evil.example:{0}", None, 403),
            ("/odomo/", None, TS_NAME, None, 200),
            ("/odomo/", None, TS_NAME + ":8443", None, 200),
            ("/odomo/", None, TS_NAME + ":9999", None, 403),
            ("/odomo/", None, "localhost:{port}", None, 200),
            ("/v1/state", "native", None, None, 200),
            ("/v1/state", "native", "evil.example", None, 200),     # native: no Origin, not asked
            ("/v1/state", "native", None, "{origin}", 200),
            ("/v1/state", "native", None, "https://evil.example", 403),
            ("/v1/state", "native", None, "null", 403),
            ("/v1/state", "native", "evil.example", "http://evil.example", 403),
            ("/v1/state", "native", None, "https://{host}", 403),   # scheme must be the transport's
            ("/health", None, "evil.example", "http://evil.example", 403),
        ]
        for path, token, host, origin, expected in rows:
            host = host.format(port=self.port) if host else None
            origin = origin.format(origin=self.origin, host=self.host) if origin else None
            status, body, _ = await self.call("GET", path, token=self.native if token else None,
                                              host=host, origin=origin)
            self.assertEqual(status, expected, (path, host, origin, body))
            if expected == 403:
                self.assertIn(body["error"]["code"], {"host_not_allowed", "origin_not_allowed"})

    async def test_pairing_from_a_foreign_origin_is_refused_before_anything_is_stored(self):
        status, body, _ = await self.call("POST", "/v1/pairing/requests", origin="https://evil.example",
                                          body={"device_id": "x", "device_name": "x"})
        self.assertEqual((status, body["error"]["code"]), (403, "origin_not_allowed"))
        status, body, _ = await self.call("POST", "/v1/pairing/requests", host="evil.example",
                                          origin="http://evil.example", body={"device_id": "x", "device_name": "x"})
        self.assertEqual((status, body["error"]["code"]), (403, "host_not_allowed"))
        self.assertEqual(self.store.pending()["requests"], [])

    def test_split_host(self):
        self.assertEqual(browser_gate.split_host("Name.ts.net."), ("name.ts.net", None))
        self.assertEqual(browser_gate.split_host("[::1]:8099"), ("::1", 8099))
        self.assertEqual(browser_gate.split_host("a:b:c"), None)
        self.assertEqual(browser_gate.split_host("a:0"), None)
        self.assertEqual(browser_gate.split_host("a b"), None)
        self.assertEqual(browser_gate.split_host(""), None)

    async def test_names_come_from_identity_tailscale_and_loopback(self):
        class Identity:
            host_name = "omodachi-vm"
            addresses = ["10.201.201.20", "100.89.1.2"]
        service = type("S", (), {"host_identity": Identity()})()
        names = browser_gate.HostNames(service)
        with patch.object(browser_gate, "tailscale_dns_name", return_value="omodachi-vm.tail.ts.net"):
            self.assertTrue(await names.allows("omodachi-vm.tail.ts.net", 8099))
            self.assertTrue(await names.allows("omodachi-vm.local:8099", 8099))
            self.assertTrue(await names.allows("100.89.1.2:8099", 8099))
            self.assertTrue(await names.allows("localhost:8099", 8099))
            self.assertFalse(await names.allows("omodachi-vm.tail.ts.net:8100", 8099))
            self.assertFalse(await names.allows("evil.example", 8099))

    async def test_the_daemon_s_cached_tailnet_reading_names_the_host_without_a_subprocess(self):
        # NEXT-1: with TAIL-2's TailnetMonitor on the identity, the MagicDNS
        # name is its cached reading; `tailscale status` is not run again.
        class Identity:
            host_name = "omodachi-vm"
            addresses = ["10.201.201.20"]
            reading = {"running": True, "dns_name": "omodachi-vm.tail.ts.net"}
            def tailnet(self):
                return self.reading
            def tailnet_reading(self):
                return self.tailnet()
        identity = Identity()
        service = type("S", (), {"host_identity": identity})()
        names = browser_gate.HostNames(service)
        with patch.object(browser_gate, "tailscale_dns_name", side_effect=AssertionError("no subprocess")):
            self.assertTrue(await names.allows("omodachi-vm.tail.ts.net", 8099))
            identity.reading = {"running": False, "dns_name": None}
            names._read_at = None
            self.assertFalse(await names.allows("omodachi-vm.tail.ts.net", 8099))

    async def test_a_refused_name_rereads_but_not_more_than_once_per_gap(self):
        clock = [1000.0]
        names = browser_gate.HostNames(type("S", (), {})(), clock=lambda: clock[0])
        with patch.object(browser_gate, "tailscale_dns_name", return_value=None) as reader:
            self.assertFalse(await names.allows("late.example.ts.net", None))
            calls = reader.call_count
            for _ in range(20):
                self.assertFalse(await names.allows("late.example.ts.net", None))
            self.assertEqual(reader.call_count, calls)
        clock[0] += browser_gate.HostNames.MISS_GAP + 1
        with patch.object(browser_gate, "tailscale_dns_name", return_value="late.example.ts.net"):
            self.assertTrue(await names.allows("late.example.ts.net", None))


class LoopbackForwardingTests(_Base):
    """C3."""

    def fake(self, remote, headers):
        from multidict import CIMultiDict, CIMultiDictProxy
        return type("R", (), {"remote": remote, "headers": CIMultiDictProxy(CIMultiDict(headers))})()

    def test_only_a_loopback_peer_is_believed(self):
        forged = [("X-Forwarded-For", "100.64.0.9"), ("Tailscale-User-Login", "alex@example.com")]
        self.assertEqual(browser_gate.forwarded_peer(self.fake("192.168.1.20", forged)), ("192.168.1.20", None))
        self.assertEqual(browser_gate.forwarded_peer(self.fake("100.64.0.3", forged)), ("100.64.0.3", None))
        address, via = browser_gate.forwarded_peer(self.fake("127.0.0.1", forged))
        self.assertEqual(address, "100.64.0.9")
        self.assertEqual(via, {"login": "alex@example.com"})
        # the last hop, which is the one serve wrote
        address, _ = browser_gate.forwarded_peer(self.fake("::1", [("X-Forwarded-For", "1.2.3.4, 100.64.0.10")]))
        self.assertEqual(address, "100.64.0.10")
        self.assertEqual(browser_gate.forwarded_peer(self.fake("127.0.0.1", [("X-Forwarded-For", "not-an-ip")])),
                         ("127.0.0.1", None))
        self.assertEqual(browser_gate.forwarded_peer(self.fake("127.0.0.1", [])), ("127.0.0.1", None))
        _, via = browser_gate.forwarded_peer(self.fake("127.0.0.1", [("X-Forwarded-For", "100.64.0.9"),
                                                                     ("Tailscale-User-Login", "a‮b\n")]))
        self.assertEqual(via["login"], "ab")

    async def test_card_and_per_source_bound_use_the_forwarded_address(self):
        serve = lambda addr: {"X-Forwarded-For": addr, "Tailscale-User-Login": "alex@example.com"}
        for n in range(2):
            status, row, _ = await self.call("POST", "/v1/pairing/requests", origin=self.origin,
                                             headers=serve("100.64.0.7"),
                                             body={"device_id": f"odomo-{n}", "device_name": "Safari on iPhone"})
            self.assertEqual(status, 200)
            self.assertEqual(row["remote_addr"], "100.64.0.7")
            # NEXT-1: one `via` for both clients - the forwarded peer's road,
            # whois on it, and serve's login where whois has none.
            self.assertEqual(row["via"], {"kind": "tailnet", "peer_name": "phone-100-64-0-7",
                                          "login": "alex@example.com"})
        status, body, _ = await self.call("POST", "/v1/pairing/requests", origin=self.origin,
                                          headers=serve("100.64.0.7"),
                                          body={"device_id": "odomo-3", "device_name": "Safari on iPhone"})
        self.assertEqual((status, body["error"]["code"]), (429, "pairing_source_capacity"))
        # A different phone behind the same serve is not blocked by the first one.
        status, row, _ = await self.call("POST", "/v1/pairing/requests", origin=self.origin,
                                         headers=serve("100.64.0.8"),
                                         body={"device_id": "odomo-4", "device_name": "Safari on iPad"})
        self.assertEqual((status, row["remote_addr"]), (200, "100.64.0.8"))
        # The forwarded identity authorizes nothing: still pending, no credential.
        self.assertEqual(row["status"], "pending")
        self.assertNotIn("credential", row)


class OneViaTests(_Base):
    """NEXT-1: the App's road (TAIL-2) and serve's forwarded peer (LITE-1a) are one `via`."""

    async def test_whois_on_the_forwarded_peer_wins_over_serve_s_login(self):
        self.tailnet_tools.logins["100.64.0.21"] = "owner@example.com"
        status, row, _ = await self.call("POST", "/v1/pairing/requests", origin=self.origin,
                                         headers={"X-Forwarded-For": "100.64.0.21",
                                                  "Tailscale-User-Login": "serve@example.com"},
                                         body={"device_id": "odomo-whois", "device_name": "Safari on iPhone"})
        self.assertEqual(status, 200)
        self.assertEqual(row["via"], {"kind": "tailnet", "peer_name": "phone-100-64-0-21",
                                      "login": "owner@example.com"})
        self.assertEqual(self.tailnet_tools.asked, ["100.64.0.21"])
        self.assertEqual(row["client"]["kind"], "web")

    async def test_a_direct_loopback_request_is_loopback_and_asks_no_whois(self):
        status, row, _ = await self.call("POST", "/v1/pairing/requests",
                                         body={"device_id": "ipad-direct", "device_name": "iPad"})
        self.assertEqual(status, 200)
        self.assertEqual(row["via"], {"kind": "loopback", "peer_name": None, "login": None})
        self.assertEqual(row["client"], {"kind": "native", "label": None})
        self.assertEqual(self.tailnet_tools.asked, [])

    async def test_a_forwarded_lan_address_is_lan_and_keeps_serve_s_login(self):
        status, row, _ = await self.call("POST", "/v1/pairing/requests", origin=self.origin,
                                         headers={"X-Forwarded-For": "192.168.7.9",
                                                  "Tailscale-User-Login": "serve@example.com"},
                                         body={"device_id": "odomo-lan", "device_name": "Safari on iPad"})
        self.assertEqual(status, 200)
        self.assertEqual(row["via"], {"kind": "lan", "peer_name": None, "login": "serve@example.com"})
        self.assertEqual(self.tailnet_tools.asked, [])


class WebRoleTests(_Base):
    """C4."""

    async def test_a_browser_pairs_as_web_and_gets_the_companion_credential_only(self):
        request, claim = await self.pair_web()
        self.assertEqual(request["client"], {"kind": "web", "label": "Safari on iPhone"})
        self.assertEqual(request["client_label"], "Web app · Safari on iPhone")
        self.assertEqual(claim["grants"], {"companion": True, "media": False, "ssh": False})
        self.assertEqual(self.auth.device_role("odomo-a"), "web")
        rows = {row["device_id"]: row for row in self.auth.list_devices()}
        self.assertEqual(rows["odomo-a"]["role"], "web")
        self.assertEqual(rows["ipad-native"]["role"], "companion")
        devices = await self.service.dispatch_local_async("local.devices.list", {}, local_authorize=lambda: True)
        web_row = next(row for row in devices["devices"] if row["device_id"] == "odomo-a")
        self.assertNotIn("media_authorized", web_row)

    async def test_origin_forces_web_whatever_the_request_says(self):
        status, row, _ = await self.call("POST", "/v1/pairing/requests", origin=self.origin,
                                         body={"device_id": "sneaky", "device_name": "iPad",
                                               "client": {"kind": "native"}})
        self.assertEqual((status, row["client"]["kind"]), (200, "web"))
        self.assertEqual(row["client_label"], "Web app")
        status, row, _ = await self.call("POST", "/v1/pairing/requests",
                                         body={"device_id": "app", "device_name": "iPad"})
        self.assertEqual(row["client"], {"kind": "native", "label": None})
        self.assertIsNone(row["client_label"])

    async def test_a_browser_cannot_offer_an_ssh_key(self):
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOuWqrM3Gk7Q5Q9KqgV0xIu3k9gpD6dWm0uQ8f7Qe9cT"
        status, body, _ = await self.call("POST", "/v1/pairing/requests", origin=self.origin,
                                          body={"device_id": "odomo-k", "device_name": "x", "ssh_public_key": key})
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"))
        status, body, _ = await self.call("POST", "/v1/pairing/requests", origin=self.origin,
                                          body={"device_id": "odomo-k", "device_name": "x",
                                                "client": {"kind": "web", "extra": 1}})
        self.assertEqual(status, 400)

    async def test_web_allow_and_deny_matrix(self):
        _, claim = await self.pair_web()
        web = claim["credential"]
        allowed = [("GET", "/v1/state"), ("GET", "/v1/capabilities"), ("GET", "/v1/catalog"),
                   ("GET", "/v1/shortcuts"), ("GET", "/v1/theme"), ("GET", "/v1/pairing/credential"),
                   ("GET", "/v1/herdr")]
        denied = [("GET", "/v1/remote/capabilities"), ("POST", "/v1/remote/sessions"),
                  ("GET", "/v1/remote/sessions/x"), ("POST", "/v1/media/pairing/discover"),
                  ("POST", "/v1/media/pairing/requests"), ("GET", "/v1/ssh/key"), ("PUT", "/v1/ssh/key"),
                  ("GET", "/v1/voice/capabilities"), ("GET", "/v1/audio/input"),
                  ("GET", "/v1/auth/keys"), ("POST", "/v1/auth/keys"), ("GET", "/v1/auth/approvals"),
                  ("GET", "/v1/clipboard"), ("PUT", "/v1/clipboard"), ("GET", "/v1/preferences"),
                  ("POST", "/v1/agent/tasks"), ("POST", "/v1/agent/default/chat/recover-empty"),
                  ("POST", "/v1/herdr/panes/p1/focus"), ("GET", "/v1/herdr/panes/p1/control"),
                  ("POST", "/v1/herdr/workspaces/w1/select"),
                  ("POST", "/v1/control/wake")]
        for method, path in allowed:
            # (the demo host has no theme, so /v1/theme is its own 503; what
            # matters is that the gate let it through)
            status, body, _ = await self.call(method, path, token=web, origin=self.origin)
            self.assertNotIn(status, (401, 403), (method, path, body))
            # without Origin too: the role is on the credential, not the header
            status, body, _ = await self.call(method, path, token=web)
            self.assertNotIn(status, (401, 403), (method, path, body))
        for method, path in denied:
            for origin in (self.origin, None):
                status, body, _ = await self.call(method, path, token=web, origin=origin, body={})
                self.assertEqual((status, body["error"]["code"]), (403, "web_client_forbidden"), (method, path))
        # A native credential presented by a browser is held to the same list.
        status, body, _ = await self.call("GET", "/v1/remote/capabilities", token=self.native, origin=self.origin)
        self.assertEqual((status, body["error"]["code"]), (403, "web_client_forbidden"))
        # ...and without Origin it is the App, unchanged.
        status, _, _ = await self.call("GET", "/v1/remote/capabilities", token=self.native)
        self.assertNotEqual(status, 403)

    def test_allow_list_is_exact(self):
        self.assertTrue(browser_gate.web_allows("HEAD", "/v1/theme"))
        self.assertFalse(browser_gate.web_allows("GET", "/v1/theme/../remote/capabilities"))
        self.assertFalse(browser_gate.web_allows("GET", "/v1/themes"))
        self.assertFalse(browser_gate.web_allows("POST", "/v1/theme"))
        self.assertFalse(browser_gate.web_allows("GET", "/v1/remote"))
        self.assertTrue(browser_gate.web_allows("POST", "/v1/actions/omodachi.shortcut.abc:invoke"))
        self.assertFalse(browser_gate.web_allows("POST", "/v1/actions/a/b:invoke"))

    async def test_approval_gives_web_no_remote_no_ssh_and_grant_remote_refuses(self):
        request, claim = await self.pair_web()
        with self.assertRaises(ServiceError) as caught:
            await self.service.dispatch_local_async(
                "local.media-pairing.grant-remote",
                {"device_id": "odomo-a", "source_request_id": request["request_id"]}, local_authorize=lambda: True)
        self.assertIn(caught.exception.code, {"web_client_no_remote", "media_pairing_unavailable", "route_unavailable"})

    async def test_approve_result_for_web_has_no_remote(self):
        status, request, _ = await self.call("POST", "/v1/pairing/requests", origin=self.origin,
                                             body={"device_id": "odomo-b", "device_name": "Chrome on Mac",
                                                   "client": {"kind": "web", "label": "Chrome on Mac"}})
        result = await self.service.dispatch_local_async("local.pair.approve", {"request_id": request["request_id"]},
                                                         local_authorize=lambda: True)
        self.assertNotIn("remote", result)
        self.assertNotIn("ssh", result)
        self.assertEqual(result["client_label"], "Web app · Chrome on Mac")

    def test_registry_keeps_roles_and_reads_an_old_registry(self):
        path = Path(self.temp.name) / "device.credentials.json"
        self.auth.issue("odomo-z", role="web")
        data = json.loads(path.read_text())
        self.assertEqual(data["roles"], {"odomo-z": "web"})
        fresh = DeviceAuthenticator.from_file(Path(self.temp.name) / "device.secret")
        self.assertEqual(fresh.device_role("odomo-z"), "web")
        # re-pairing as the App clears it; revoke + purge forgets it
        self.auth.issue("odomo-z")
        self.assertEqual(self.auth.device_role("odomo-z"), "companion")
        self.auth.issue("odomo-y", role="web")
        self.auth.revoke_device("odomo-y")
        self.auth.purge_device("odomo-y")
        self.assertNotIn("odomo-y", json.loads(path.read_text()).get("roles", {}))
        # a registry written before roles existed still loads
        del data["roles"]
        path.write_text(json.dumps(data))
        self.assertEqual(DeviceAuthenticator.from_file(Path(self.temp.name) / "device.secret")
                         .device_role("odomo-z"), "companion")
        data["roles"] = {"odomo-z": "root"}
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            DeviceAuthenticator.from_file(Path(self.temp.name) / "device.secret").device_role("odomo-z")

    def test_client_label(self):
        self.assertEqual(client_label({"kind": "web", "label": "Safari on iPad"}), "Web app · Safari on iPad")
        self.assertEqual(client_label({"kind": "web", "label": None}), "Web app")
        self.assertIsNone(client_label({"kind": "native", "label": None}))


class TicketTests(_Base):
    """C5."""

    async def ticket(self, token, path="/v1/events", origin=None):
        return await self.call("POST", "/v1/ws-ticket", token=token, origin=origin or self.origin,
                               body={"path": path})

    async def test_a_ticket_opens_its_socket_once(self):
        _, claim = await self.pair_web()
        status, minted, _ = await self.ticket(claim["credential"])
        self.assertEqual(status, 200)
        self.assertRegex(minted["ticket"], r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual((minted["path"], minted["expires_in"]), ("/v1/events", 30))
        url = self.url + "/v1/events?ticket=" + minted["ticket"]
        ws = await self.client.ws_connect(url, headers={"Origin": self.origin})
        first = await asyncio.wait_for(ws.receive_json(), 3)
        self.assertEqual(first["type"], "snapshot")
        await ws.close()
        # replay
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            await self.client.ws_connect(url, headers={"Origin": self.origin})
        self.assertEqual(caught.exception.status, 401)

    async def test_ticket_with_since_and_heartbeat_query(self):
        _, claim = await self.pair_web()
        _, minted, _ = await self.ticket(claim["credential"])
        with patch.object(network, "HEARTBEAT", 0.05):
            ws = await self.client.ws_connect(
                self.url + f"/v1/events?heartbeat=1&ticket={minted['ticket']}", headers={"Origin": self.origin})
            seen = [(await asyncio.wait_for(ws.receive_json(), 3))["type"] for _ in range(3)]
            await ws.close()
        self.assertEqual(seen[0], "snapshot")
        self.assertIn("heartbeat", seen[1:])

    async def test_heartbeat_query_is_strict(self):
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            await self.client.ws_connect(self.url + "/v1/events?heartbeat=2",
                                         headers={"Authorization": "Bearer " + self.native})
        self.assertEqual(caught.exception.status, 400)

    async def test_expired_wrong_path_and_garbage_are_401(self):
        _, claim = await self.pair_web()
        web = claim["credential"]
        _, minted, _ = await self.ticket(web)
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            await self.client.ws_connect(self.url + "/v1/agent/default/chat/events?ticket=" + minted["ticket"],
                                         headers={"Origin": self.origin})
        self.assertEqual(caught.exception.status, 401)
        # the wrong-path attempt spent it
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            await self.client.ws_connect(self.url + "/v1/events?ticket=" + minted["ticket"],
                                         headers={"Origin": self.origin})
        self.assertEqual(caught.exception.status, 401)
        tickets = browser_gate.WsTickets(clock=lambda: 0.0)
        issued = tickets.mint("d", "t", "/v1/events")
        tickets._clock = lambda: 31.0
        with self.assertRaises(browser_gate.TicketRefused):
            tickets.take(issued["ticket"], "/v1/events")
        for garbage in ("x", "a" * 43, "../../" + "a" * 37):
            with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
                await self.client.ws_connect(self.url + "/v1/events?ticket=" + garbage, headers={"Origin": self.origin})
            self.assertEqual(caught.exception.status, 401)

    async def test_a_ticket_cannot_widen_what_the_credential_reaches(self):
        _, claim = await self.pair_web()
        status, body, _ = await self.ticket(claim["credential"], "/v1/herdr/panes/p1/control")
        self.assertEqual((status, body["error"]["code"]), (403, "web_client_forbidden"))
        status, body, _ = await self.ticket(claim["credential"], "/v1/remote/sessions/x/vnc")
        self.assertEqual(status, 400)
        status, body, _ = await self.ticket(claim["credential"], "/v1/events/../remote")
        self.assertEqual(status, 400)

    async def test_revoked_after_minting_is_refused_at_the_door(self):
        _, claim = await self.pair_web()
        _, minted, _ = await self.ticket(claim["credential"])
        self.auth.revoke_device("odomo-a")
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            await self.client.ws_connect(self.url + "/v1/events?ticket=" + minted["ticket"],
                                         headers={"Origin": self.origin})
        self.assertEqual(caught.exception.status, 401)

    async def test_bearer_plus_ticket_is_not_a_second_way_in(self):
        _, claim = await self.pair_web()
        _, minted, _ = await self.ticket(claim["credential"])
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            await self.client.ws_connect(self.url + "/v1/events?ticket=" + minted["ticket"],
                                         headers={"Authorization": "Bearer " + self.native})
        self.assertEqual(caught.exception.status, 400)

    def test_capacity_and_single_use(self):
        tickets = browser_gate.WsTickets(clock=lambda: 0.0)
        for _ in range(browser_gate.WsTickets.PER_DEVICE):
            tickets.mint("d", "t", "/v1/events")
        with self.assertRaises(ServiceError) as caught:
            tickets.mint("d", "t", "/v1/events")
        self.assertEqual(caught.exception.status, 429)
        other = tickets.mint("e", "t2", "/v1/events")
        self.assertEqual(tickets.take(other["ticket"], "/v1/events"), ("e", "t2"))
        with self.assertRaises(browser_gate.TicketRefused):
            tickets.take(other["ticket"], "/v1/events")
        # nothing is kept in clear
        self.assertNotIn(other["ticket"], json.dumps(list(tickets._rows)))


if __name__ == "__main__":
    unittest.main()
