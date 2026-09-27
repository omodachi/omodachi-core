from __future__ import annotations
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import aiohttp

from omodachi_core.auth import DeviceAuthenticator
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
from omodachi_core.pairing import PairingStore
from omodachi_core.service import ServiceError


class PairingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.auth = DeviceAuthenticator.from_file(base / "device.secret")
        self.store = PairingStore(self.auth, base / "pairing.json")
        self.service = create_service(Hub(authenticator=self.auth), demo=True)
        self.service.pairing = self.store
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.client = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.close()
        self.temp.cleanup()

    async def post(self, path, payload):
        async with self.client.post(self.url + path, json=payload) as response:
            return response.status, await response.json()

    async def test_network_needs_local_approval_and_claim_is_single_use(self):
        invitation = self.store.begin()
        code, request = await self.post("/v1/pairing/requests", {"invitation":invitation["invitation"], "device_id":"ipad-real", "device_name":"iPad"})
        self.assertEqual(code, 200)
        path = "/v1/pairing/requests/" + request["request_id"] + "/claim"
        payload = {"request_secret":request["request_secret"]}
        code, pending = await self.post(path, payload)
        self.assertEqual(pending["status"], "pending")
        self.assertNotIn("credential", pending)
        code, _ = await self.post("/v1/pairing/approve", {"request_id":request["request_id"]})
        self.assertEqual(code, 401)
        pending_json = json.dumps(self.store.pending())
        self.assertNotIn(request["request_secret"], pending_json)
        self.assertNotIn(invitation["invitation"], self.store.path.read_text())
        self.assertNotIn(request["request_secret"], self.store.path.read_text())
        self.store.decide(request["request_id"], approve=True)
        code, claimed = await self.post(path, payload)
        self.assertEqual(code, 200)
        self.assertEqual(self.auth.verify(claimed["credential"]), "ipad-real")
        self.assertNotIn(claimed["credential"], self.store.path.read_text())
        self.assertEqual((await self.post(path, payload))[0], 409)
        self.assertEqual(self.auth.list_devices()[0]["status"], "authorized")
        self.auth.revoke_device("ipad-real")
        self.assertEqual(self.auth.list_devices()[0]["status"], "revoked")
        with self.assertRaises(ValueError): self.auth.verify(claimed["credential"])

    async def test_wrong_secret_rejection_expiry_and_invitation_reuse(self):
        invitation = self.store.begin()
        request = self.store.request(invitation["invitation"], "new-device", "New device")
        with self.assertRaises(ServiceError): self.store.request(invitation["invitation"], "other", "Other")
        with self.assertRaises(ServiceError): self.store.claim(request["request_id"], "a" * 43)
        self.store.decide(request["request_id"], approve=False)
        with self.assertRaises(ServiceError): self.store.claim(request["request_id"], request["request_secret"])
        expired = self.store.begin()
        with patch("omodachi_core.pairing.time.time", return_value=expired["expires_at"] + 1):
            with self.assertRaises(ServiceError): self.store.request(expired["invitation"], "expired", "Expired")
        self.assertEqual(self.auth.list_devices(), [])


# A syntactically real ed25519 key with a known body, so a test never publishes
# anybody's key and the fingerprint is reproducible.
KEY_BODY = "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f"
KEY = f"ssh-ed25519 {KEY_BODY} alex@ipad"
OTHER_BODY = "AAAAC3NzaC1lZDI1NTE5AAAAIB8eHRwbGhkYFxYVFBMSERAPDg0MCwoJCAcGBQQDAgEA"


class OneApprovalThreeGrantsTests(unittest.IsolatedAsyncioTestCase):
    """SPEC-I §1.1: the SSH public key rides along with the pairing request."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        (self.home / ".ssh").mkdir(parents=True, mode=0o700)
        self.auth = DeviceAuthenticator.from_file(self.base / "device.secret")
        self.store = PairingStore(self.auth, self.base / "pairing.json")
        self.service = create_service(Hub(authenticator=self.auth), demo=True)
        self.service.pairing = self.store
        from omodachi_core.ssh_keys import AuthorizedKeys
        self.service.ssh_keys = AuthorizedKeys(self.home)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def request(self, key=KEY, device="ipad-real", name="Leo's iPad"):
        invitation = self.store.begin()["invitation"]
        return self.store.request(invitation, device, name, ssh_public_key=key)

    def approve(self, request_id, *, remote=False):
        return self.service._dispatch_local("local.pair.approve",
                                            {"request_id": request_id, "remote": remote},
                                            lambda: True)

    async def test_the_key_rides_along_and_the_one_approve_writes_it(self):
        row = self.request()
        # Whoever is about to decide can see the key they are authorizing.
        pending = self.store.pending()["requests"][0]
        self.assertEqual(pending["ssh_public_key"], f"ssh-ed25519 {KEY_BODY}")
        self.assertTrue(pending["ssh_fingerprint"].startswith("SHA256:"))
        self.assertEqual(pending["grants"], {"companion": False, "media": False, "ssh": False})

        result = self.approve(row["request_id"])
        self.assertEqual(result["ssh"]["reason"], "added")
        self.assertEqual(result["grants"], {"companion": False, "media": False, "ssh": True})
        text = (self.home / ".ssh/authorized_keys").read_text()
        # RELEASE-9 (B2): restricted, and ending one credential TTL + a day on.
        self.assertRegex(text, r'^restrict,pty,expiry-time="\d{14}Z" ssh-ed25519 '
                         + KEY_BODY + " # omodachi:ipad-real\n$")
        from omodachi_core.ssh_keys import AuthorizedKeys
        ends = AuthorizedKeys(self.home).listing()[0]["expires_at"]
        self.assertAlmostEqual(ends, time.time() + self.auth.ttl_seconds + 86400, delta=120)

        claim = self.service.pairing_claim(row["request_id"], {"request_secret": row["request_secret"]})
        self.assertEqual(claim["grants"], {"companion": True, "media": False, "ssh": True})
        # Nobody has to type this in any more; SPEC-I deletes the field.
        self.assertEqual(claim["ssh"]["port"], 22)
        self.assertIsInstance(claim["ssh"]["user"], str)
        self.assertEqual(self.auth.verify(claim["credential"]), "ipad-real")
        # And the name it paired under now answers "who is holding the host".
        self.assertEqual(self.auth.device_name("ipad-real"), "Leo's iPad")
        self.assertEqual(self.auth.list_devices()[0]["device_name"], "Leo's iPad")

    async def test_a_request_without_a_key_grants_only_what_it_asked_for(self):
        row = self.request(key=None)
        self.assertIsNone(self.store.pending()["requests"][0]["ssh_public_key"])
        result = self.approve(row["request_id"])
        self.assertNotIn("ssh", result)
        self.assertFalse((self.home / ".ssh/authorized_keys").exists())
        claim = self.service.pairing_claim(row["request_id"], {"request_secret": row["request_secret"]})
        self.assertEqual(claim["grants"], {"companion": True, "media": False, "ssh": False})

    async def test_a_key_sshd_could_not_parse_is_refused_at_the_request(self):
        invitation = self.store.begin()["invitation"]
        for bad in ("ssh-ed25519", "not a key", "ssh-dss " + KEY_BODY, f"ssh-ed25519 {KEY_BODY}\nssh-ed25519 x",
                    "ssh-ed25519 !!!!", 17):
            with self.assertRaises(ServiceError):
                self.store.request(invitation, "ipad-real", "iPad", ssh_public_key=bad)
        # The invitation is still single-use and still unspent.
        self.assertTrue(self.store.request(invitation, "ipad-real", "iPad"))

    async def test_a_key_the_user_already_owns_is_reported_not_claimed(self):
        # authorize refuses to write a second copy of a key it does not own.
        (self.home / ".ssh/authorized_keys").write_text(f"ssh-ed25519 {KEY_BODY} alex@laptop\n")
        row = self.request()
        result = self.approve(row["request_id"])
        self.assertEqual(result["ssh"], {"authorized": False, "changed": False,
                                         "device": "ipad-real", "error": "public_key_not_owned"})
        self.assertEqual((self.home / ".ssh/authorized_keys").read_text(),
                         f"ssh-ed25519 {KEY_BODY} alex@laptop\n")
        claim = self.service.pairing_claim(row["request_id"], {"request_secret": row["request_secret"]})
        # The credential still landed; the claim says the terminal did not.
        self.assertEqual(claim["grants"], {"companion": True, "media": False, "ssh": False})

    async def test_revoking_the_device_takes_the_line_and_the_name_back(self):
        row = self.request()
        self.approve(row["request_id"])
        self.service.pairing_claim(row["request_id"], {"request_secret": row["request_secret"]})
        (self.home / ".ssh/authorized_keys").write_text(
            f"ssh-ed25519 {OTHER_BODY} alex@laptop\n"
            f"ssh-ed25519 {KEY_BODY} # omodachi:ipad-real\n")
        self.assertEqual(self.service.ssh_keys.revoke("ipad-real"),
                         {"revoked": True, "removed": 1, "device": "ipad-real"})
        self.assertEqual((self.home / ".ssh/authorized_keys").read_text(),
                         f"ssh-ed25519 {OTHER_BODY} alex@laptop\n")
        self.auth.revoke_device("ipad-real")
        self.assertIsNone(self.auth.device_name("ipad-real"))


class RequesterNameTests(unittest.TestCase):
    """RELEASE-9 (B4): what a stranger may put in a notification title."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.store = PairingStore(DeviceAuthenticator.from_file(base / "device.secret"), base / "pairing.json")

    def named(self, name, device="ipad-x"):
        return self.store.request(None, device, name, mode="open")["device_name"]

    def test_newlines_controls_and_bidi_are_gone_and_the_length_is_bounded(self):
        self.assertEqual(self.named("Leo\u2019s iPad\n\nApprove: granted"), "Leo\u2019s iPad Approve: granted")
        self.assertEqual(self.named("evil\u202egnp.exe\u200b", "d2"), "evilgnp.exe")
        self.assertEqual(self.named("a\x1b[31mred\x07", "d3"), "a[31mred")
        self.assertEqual(len(self.named("x" * 80, "d4")), 48)
        self.assertEqual(self.named("Pad \U0001F34E", "d5"), "Pad \U0001F34E")

    def test_a_name_that_is_nothing_once_cleaned_is_refused(self):
        for name in ("", "\u202e\u200b", "\n\t ", "x" * 300):
            with self.subTest(name=name), self.assertRaises(ServiceError):
                self.named(name)

    def test_the_panels_own_device_id_cannot_be_requested(self):
        from omodachi_core.protocol import PLUGIN_DEVICE_ID
        with self.assertRaises(ServiceError):
            self.store.request(None, PLUGIN_DEVICE_ID, "Omodachi panel", mode="open")


class SshGrantTests(unittest.IsolatedAsyncioTestCase):
    """RELEASE-9 (B2): `PUT /v1/ssh/key` is for a device an Approve gave a terminal."""

    asyncSetUp = OneApprovalThreeGrantsTests.asyncSetUp
    asyncTearDown = OneApprovalThreeGrantsTests.asyncTearDown
    request = OneApprovalThreeGrantsTests.request
    approve = OneApprovalThreeGrantsTests.approve

    def pair(self, key):
        row = self.request(key=key)
        self.approve(row["request_id"])
        claim = self.service.pairing_claim(row["request_id"], {"request_secret": row["request_secret"]})
        return claim

    async def test_a_device_granted_ssh_at_approval_may_rotate_its_key(self):
        self.pair(KEY)
        result = self.service.ssh_key("ipad-real", {"public_key": f"ssh-ed25519 {OTHER_BODY} new"})
        self.assertEqual(result["reason"], "replaced")
        text = (self.home / ".ssh/authorized_keys").read_text()
        self.assertIn(OTHER_BODY, text)
        self.assertTrue(text.startswith("restrict,pty,expiry-time="))

    async def test_a_device_paired_without_a_key_cannot_add_one_on_its_own(self):
        self.pair(None)
        with self.assertRaises(ServiceError) as caught:
            self.service.ssh_key("ipad-real", {"public_key": f"ssh-ed25519 {OTHER_BODY} sneaky"})
        self.assertEqual((caught.exception.code, caught.exception.status), ("ssh_approval_required", 409))
        self.assertFalse((self.home / ".ssh/authorized_keys").exists())
        # It waits for the person at the computer.
        pending = self.service._dispatch_local("local.ssh.pending", {}, lambda: True)["requests"]
        self.assertEqual([row["device_id"] for row in pending], ["ipad-real"])
        approved = self.service._dispatch_local("local.ssh.approve", {"device_id": "ipad-real"}, lambda: True)
        self.assertTrue(approved["authorized"])
        self.assertTrue(approved["grant_recorded"])
        self.assertIn(OTHER_BODY, (self.home / ".ssh/authorized_keys").read_text())
        # ...after which the device may rotate like any granted one.
        result = self.service.ssh_key("ipad-real", {"public_key": KEY})
        self.assertEqual(result["reason"], "replaced")

    async def test_a_rejected_or_unknown_request_writes_nothing(self):
        self.pair(None)
        with self.assertRaises(ServiceError):
            self.service.ssh_key("ipad-real", {"public_key": f"ssh-ed25519 {OTHER_BODY} x"})
        self.service._dispatch_local("local.ssh.reject", {"device_id": "ipad-real"}, lambda: True)
        with self.assertRaises(ServiceError) as caught:
            self.service._dispatch_local("local.ssh.approve", {"device_id": "ipad-real"}, lambda: True)
        self.assertEqual(caught.exception.code, "ssh_request_unknown")
        self.assertFalse((self.home / ".ssh/authorized_keys").exists())

    async def test_a_revoked_device_has_no_grant_left_to_rotate_with(self):
        self.pair(KEY)
        self.service._dispatch_local("local.devices.revoke", {"device_id": "ipad-real"}, lambda: True) \
            if self.service.media_pairing is not None else self.store.forget_device("ipad-real")
        with self.assertRaises(ServiceError) as caught:
            self.service.ssh_key("ipad-real", {"public_key": f"ssh-ed25519 {OTHER_BODY} again"})
        self.assertEqual(caught.exception.code, "ssh_approval_required")

    async def test_renewal_extends_the_line_with_the_credential(self):
        self.pair(KEY)
        from omodachi_core.ssh_keys import AuthorizedKeys
        later = int(time.time()) + 90 * 86400
        self.service.ssh_key_renewed("ipad-real", later)
        self.assertEqual(AuthorizedKeys(self.home).listing()[0]["expires_at"], later + 86400)


class OpenPairingTests(unittest.IsolatedAsyncioTestCase):
    """PAIR-2: the handshake's boundary is the local Approve, not the invitation."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.auth = DeviceAuthenticator.from_file(base / "device.secret")
        self.store = PairingStore(self.auth, base / "pairing.json")
        from omodachi_core.preferences import HostPreferencesStore
        self.preferences = HostPreferencesStore()
        self.service = create_service(Hub(authenticator=self.auth), demo=True,
                                      preferences_store=self.preferences)
        self.service.pairing = self.store
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.client = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.close()
        self.temp.cleanup()

    async def post(self, path, payload):
        async with self.client.post(self.url + path, json=payload) as response:
            return response.status, await response.json()

    async def get(self, path):
        async with self.client.get(self.url + path) as response:
            return response.status, await response.json()

    def lock(self):
        revision = self.preferences.get()["revision"]
        self.preferences.set(expected_revision=revision, changes={"pairing_mode": "invite"})

    async def test_a_request_without_an_invitation_is_pending_and_the_approve_is_still_local(self):
        code, request = await self.post("/v1/pairing/requests",
                                        {"device_id": "ipad-open", "device_name": "iPad"})
        self.assertEqual(code, 200)
        self.assertEqual(request["status"], "pending")
        # TTL runs from the request, not from somebody's invitation.
        self.assertTrue(0 < request["expires_at"] - int(time.time()) <= PairingStore.TTL)
        self.assertEqual(request["remote_addr"], "127.0.0.1")
        self.assertEqual(self.store.pending()["requests"][0]["remote_addr"], "127.0.0.1")
        # The network still cannot approve itself.
        self.assertEqual((await self.post("/v1/pairing/approve",
                                          {"request_id": request["request_id"]}))[0], 401)
        path = "/v1/pairing/requests/" + request["request_id"] + "/claim"
        payload = {"request_secret": request["request_secret"]}
        self.assertEqual((await self.post(path, payload))[1]["status"], "pending")
        self.store.decide(request["request_id"], approve=True)
        code, claimed = await self.post(path, payload)
        self.assertEqual(code, 200)
        self.assertEqual(self.auth.verify(claimed["credential"]), "ipad-open")

    async def test_the_same_device_asking_twice_replaces_its_own_request(self):
        _, first = await self.post("/v1/pairing/requests",
                                   {"device_id": "ipad-open", "device_name": "iPad"})
        _, second = await self.post("/v1/pairing/requests",
                                    {"device_id": "ipad-open", "device_name": "iPad"})
        rows = self.store.pending()["requests"]
        self.assertEqual([row["request_id"] for row in rows], [second["request_id"]])
        # The replaced request's secret is worth nothing afterwards.
        self.assertEqual((await self.post("/v1/pairing/requests/" + first["request_id"] + "/claim",
                                          {"request_secret": first["request_secret"]}))[0], 403)

    async def test_one_source_may_hold_two_pending_requests_and_no_more(self):
        for index in range(2):
            code, _ = await self.post("/v1/pairing/requests",
                                      {"device_id": f"ipad-{index}", "device_name": "iPad"})
            self.assertEqual(code, 200)
        code, body = await self.post("/v1/pairing/requests",
                                     {"device_id": "ipad-3", "device_name": "iPad"})
        self.assertEqual(code, 429)
        self.assertEqual(body["error"]["code"], "pairing_source_capacity")
        # An invitation is an explicit local act, so it is not bounded this way.
        invitation = self.store.begin()["invitation"]
        code, _ = await self.post("/v1/pairing/requests",
                                  {"invitation": invitation, "device_id": "ipad-3", "device_name": "iPad"})
        self.assertEqual(code, 200)

    async def test_invite_mode_refuses_the_bare_request_and_still_takes_the_invitation(self):
        self.lock()
        code, body = await self.post("/v1/pairing/requests",
                                     {"device_id": "ipad-open", "device_name": "iPad"})
        self.assertEqual(code, 403)
        self.assertEqual(body["error"]["code"], "pairing_invitation_required")
        self.assertEqual(self.store.pending()["requests"], [])
        invitation = self.store.begin()["invitation"]
        code, request = await self.post("/v1/pairing/requests",
                                        {"invitation": invitation, "device_id": "ipad-open", "device_name": "iPad"})
        self.assertEqual(code, 200)
        self.assertEqual(request["status"], "pending")

    async def test_health_publishes_the_mode_and_nothing_else_about_pairing(self):
        code, health = await self.get("/health")
        self.assertEqual(code, 200)
        self.assertEqual(health["pairing"], {"mode": "open"})
        self.lock()
        self.assertEqual((await self.get("/health"))[1]["pairing"], {"mode": "invite"})

    async def test_an_expired_request_is_cleared_rather_than_counted(self):
        _, first = await self.post("/v1/pairing/requests",
                                   {"device_id": "ipad-a", "device_name": "iPad"})
        with patch("omodachi_core.pairing.time.time", return_value=first["expires_at"] + 1):
            self.assertEqual(self.store.pending()["requests"], [])
            for index in range(2):
                self.store.request(device_id=f"ipad-{index}", device_name="iPad", remote_addr="127.0.0.1")


class _MediaStub:
    """Just enough of the media bridge for a revoke to reach the registry."""

    def revoke_device(self, device_id, *, local_authorize=None):
        return {"device_id": device_id, "media_authorized": False,
                "pending_media_revocations": 0, "media_revocation_complete": True}


class PluginCredentialTests(unittest.IsolatedAsyncioTestCase):
    """PLUG-3: the panel's own credential is labelled, not offered a button."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.auth = DeviceAuthenticator.from_file(base / "device.secret")
        self.service = create_service(Hub(authenticator=self.auth), demo=True,
                                      media_pairing=_MediaStub())
        from omodachi_core.ssh_keys import AuthorizedKeys
        home = base / "home"
        (home / ".ssh").mkdir(parents=True, mode=0o700)
        self.service.ssh_keys = AuthorizedKeys(home)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def revoke(self, device_id, **extra):
        return self.service._dispatch_local("local.devices.revoke",
                                            {"device_id": device_id, **extra}, lambda: True)

    async def test_the_plugins_own_row_is_a_plugin_and_everything_else_is_a_companion(self):
        self.auth.issue("com.omodachi.host")
        self.auth.issue("ios-companion", device_name="Leo 的 iPad")
        roles = {row["device_id"]: row["role"] for row in self.auth.list_devices()}
        self.assertEqual(roles, {"com.omodachi.host": "plugin", "ios-companion": "companion"})

    async def test_revoking_the_plugin_credential_takes_force(self):
        self.auth.issue("com.omodachi.host")
        with self.assertRaises(ServiceError) as raised:
            self.revoke("com.omodachi.host")
        self.assertEqual((raised.exception.code, raised.exception.status), ("plugin_credential", 409))
        self.assertEqual(self.auth.list_devices()[0]["status"], "authorized")
        self.assertEqual(self.revoke("com.omodachi.host", force=True)["revoked"], 1)
        self.assertEqual(self.auth.list_devices()[0]["status"], "revoked")

    async def test_force_is_a_boolean_and_a_companion_needs_no_flag(self):
        self.auth.issue("ios-companion")
        with self.assertRaises(ServiceError):
            self.revoke("ios-companion", force="yes")
        self.assertEqual(self.revoke("ios-companion")["revoked"], 1)
