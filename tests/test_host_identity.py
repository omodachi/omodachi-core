"""The pinned host identity: a real self-signed certificate and its fingerprint.

The certificate here is generated into a temporary directory by the same code
the installer runs; nothing touches the developer's own ~/.config/omodachi.
"""
import hashlib
import ipaddress
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import unittest

import aiohttp

from omodachi_core import host_identity
from omodachi_core.auth import DeviceAuthenticator
from omodachi_core.bootstrap import create_service
from omodachi_core.host_identity import HostIdentity
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
from omodachi_core.pairing import PairingStore


OPENSSL = shutil.which("openssl")


class CertificateTests(unittest.TestCase):
    def setUp(self):
        # openssl is a hard requirement of the installer, so it is a hard
        # requirement here too; a missing one is a failure, not a skip.
        self.assertIsNotNone(OPENSSL, "openssl is required to generate the host certificate")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def generate(self, **kwargs):
        return host_identity.generate_certificate(self.root / "tls", name="omarchy", **kwargs)

    def test_self_signed_certificate_is_p256_private_and_names_every_reachable_identity(self):
        result = self.generate(addresses=["192.168.1.10", "fd00::1"])
        certificate, key = Path(result["certificate"]), Path(result["private_key"])
        self.assertEqual(certificate.stat().st_mode & 0o777, 0o600)
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        text = subprocess.run([OPENSSL, "x509", "-in", str(certificate), "-noout", "-text"],
                              capture_output=True, text=True, check=True).stdout
        self.assertIn("id-ecPublicKey", text)
        self.assertIn("prime256v1", text)
        self.assertIn("DNS:omarchy", text)
        self.assertIn("DNS:omarchy.local", text)
        self.assertIn("IP Address:192.168.1.10", text)
        self.assertIn("CN=omarchy", text.replace("CN = ", "CN="))
        # Ten years: this key is pinned by hand, not rotated by a CA.
        dates = subprocess.run([OPENSSL, "x509", "-in", str(certificate), "-noout", "-dates"],
                               capture_output=True, text=True, check=True).stdout
        start = int(dates.split("notBefore=")[1].split("\n")[0].split()[3])
        end = int(dates.split("notAfter=")[1].split("\n")[0].split()[3])
        self.assertEqual(end - start, 10)

    def test_fingerprint_is_the_sha256_of_the_der_openssl_reports(self):
        result = self.generate(addresses=["192.168.1.10"])
        printed = subprocess.run([OPENSSL, "x509", "-in", result["certificate"], "-noout",
                                  "-fingerprint", "-sha256"], capture_output=True, text=True,
                                 check=True).stdout
        expected = printed.split("=", 1)[1].strip().replace(":", "").lower()
        self.assertEqual(result["tls_fingerprint_sha256"], expected)
        self.assertEqual(host_identity.certificate_fingerprint(result["certificate"]), expected)
        der = ssl.PEM_cert_to_DER_cert(Path(result["certificate"]).read_text())
        self.assertEqual(expected, hashlib.sha256(der).hexdigest())

    def test_rotation_replaces_the_pinned_fingerprint_and_ensure_never_does(self):
        first = host_identity.ensure_certificate(self.root / "tls", name="omarchy",
                                                 addresses=["192.168.1.10"])
        self.assertTrue(first["created"])
        again = host_identity.ensure_certificate(self.root / "tls", name="omarchy",
                                                 addresses=["192.168.1.10"])
        self.assertFalse(again["created"])
        self.assertEqual(again["tls_fingerprint_sha256"], first["tls_fingerprint_sha256"])
        rotated = self.generate(addresses=["192.168.1.10"])
        self.assertNotEqual(rotated["tls_fingerprint_sha256"], first["tls_fingerprint_sha256"])

    def test_unreadable_or_non_certificate_material_has_no_fingerprint(self):
        self.assertIsNone(host_identity.certificate_fingerprint(self.root / "missing.pem"))
        (self.root / "junk.pem").write_text("not a certificate\n")
        self.assertIsNone(host_identity.certificate_fingerprint(self.root / "junk.pem"))

    def test_host_id_is_stable_private_and_regenerated_only_when_absent(self):
        first = host_identity.load_host_id(self.root / "config")
        self.assertRegex(first, r"^[0-9a-f]{32}$")
        self.assertEqual(host_identity.load_host_id(self.root / "config"), first)
        path = self.root / "config/host-id"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.write_text("not-an-id\n")
        self.assertNotEqual(host_identity.load_host_id(self.root / "config"), "not-an-id")

    def test_advertised_addresses_never_include_loopback_or_link_local(self):
        for value in host_identity.host_addresses():
            address = ipaddress.ip_address(value)
            self.assertFalse(address.is_loopback)
            self.assertFalse(address.is_link_local)
            self.assertFalse(address.is_multicast)


class IdentityWireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.authority = DeviceAuthenticator.from_file(self.root / "device.secret")
        self.hub = Hub(authenticator=self.authority)
        self.service = create_service(self.hub, demo=True)
        self.service.pairing = PairingStore(self.authority, self.root / "pairing.json")
        self.identity = HostIdentity(host_id="9" * 32, host_name="omarchy", certificate=None,
                                     port=8099, addresses=["192.168.1.10"])
        self.identity.fingerprint = lambda: "c" * 64
        self.service.install_host_identity(self.identity)
        self.network = NetworkServer(self.service, allow_loopback_http=True)
        await self.network.start()
        self.url = f"http://127.0.0.1:{self.network.bound_port}"
        self.http = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.http.close()
        await self.network.close()
        await self.service.close_media()
        self.temp.cleanup()

    async def test_health_publishes_the_anchor_without_a_credential(self):
        async with self.http.get(self.url + "/health") as response:
            self.assertEqual(response.status, 200)
            body = await response.json()
        self.assertEqual(body["host_id"], "9" * 32)
        self.assertEqual(body["tls_fingerprint_sha256"], "c" * 64)
        self.assertEqual(body["contract_revision"], "omodachi.v1")
        # Health is an anchor, not authorization: it carries no device data.
        self.assertNotIn("endpoints", body)
        self.assertNotIn("devices", body)

    async def test_ipc_health_reports_the_same_identity(self):
        self.assertEqual(self.hub.dispatch("health", {})["tls_fingerprint_sha256"], "c" * 64)

    async def test_claim_hands_the_client_what_it_pins_and_where_to_reconnect(self):
        invitation = self.service.pairing.begin()["invitation"]
        async with self.http.post(self.url + "/v1/pairing/requests", json={
                "invitation": invitation, "device_id": "ipad", "device_name": "iPad"}) as response:
            row = await response.json()
        async with self.http.post(self.url + "/v1/pairing/requests/" + row["request_id"] + "/claim",
                                  json={"request_secret": row["request_secret"]}) as response:
            pending = await response.json()
        # An unapproved request already answers with the host identity, but the
        # client pins nothing until a credential actually comes back.
        self.assertEqual(pending["status"], "pending")
        self.assertNotIn("credential", pending)
        self.service.pairing.decide(row["request_id"], approve=True)
        async with self.http.post(self.url + "/v1/pairing/requests/" + row["request_id"] + "/claim",
                                  json={"request_secret": row["request_secret"]}) as response:
            claimed = await response.json()
        self.assertEqual(claimed["tls_fingerprint_sha256"], "c" * 64)
        self.assertEqual(claimed["host_id"], "9" * 32)
        self.assertEqual(claimed["host_name"], "omarchy")
        self.assertEqual(claimed["endpoints"], [{"host": "192.168.1.10", "port": 8099}])
        self.assertEqual(self.hub.authenticate(claimed["credential"]), "ipad")

    async def test_a_daemon_without_an_installed_identity_reports_unknown(self):
        self.service.host_identity = None
        async with self.http.get(self.url + "/health") as response:
            body = await response.json()
        self.assertIsNone(body["host_id"])
        self.assertIsNone(body["tls_fingerprint_sha256"])
        self.assertEqual(self.service.host_descriptor()["endpoints"], [])


class ContractShapeTests(unittest.TestCase):
    def test_health_and_claim_fixtures_match_the_declared_contracts(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("health.json", "pairing-claim.json"):
            document = json.loads((root / "contracts/fixtures" / name).read_text())
            self.assertEqual(document["contract_revision"], "omodachi.v1")
            self.assertRegex(document["tls_fingerprint_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(document["host_id"], r"^[0-9a-f]{32}$")


if __name__ == "__main__":
    unittest.main()
