"""CORE-2 §1: a credential says why it is refused, can be renewed, and expires visibly."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import aiohttp

from omodachi_core.auth import (CREDENTIAL_REASONS, CredentialError, CredentialRenewalRefused,
                                DeviceAuthenticator)
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
from omodachi_core.pairing import PairingStore
from omodachi_core.protocol import PLUGIN_DEVICE_ID
from omodachi_core.service import ServiceError

DAY = 86400
T0 = 1_790_000_000


class ReasonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "device.secret"
        self.auth = DeviceAuthenticator.from_file(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def reason(self, token, now):
        try:
            self.auth.verify(token, now)
        except CredentialError as error:
            self.assertIn(error.reason, CREDENTIAL_REASONS)
            # Every caller that only asks "was it refused" still sees the old words.
            self.assertIsInstance(error, ValueError)
            self.assertEqual(str(error), "invalid device credential")
            return error.reason
        return None

    def test_a_live_credential_verifies(self):
        token = self.auth.issue("ipad", now=T0).token
        self.assertEqual(self.auth.verify(token, T0 + 10), "ipad")

    def test_an_expired_credential_says_expired(self):
        token = self.auth.issue("ipad", now=T0).token
        self.assertEqual(self.reason(token, T0 + 30 * DAY), "credential_expired")

    def test_a_revoked_credential_says_revoked_even_once_it_has_also_expired(self):
        token = self.auth.issue("ipad", now=T0).token
        self.auth.revoke_device("ipad")
        self.assertEqual(self.reason(token, T0 + 1), "credential_revoked")
        self.assertEqual(self.reason(token, T0 + 31 * DAY), "credential_revoked")

    def test_a_purged_device_says_purged(self):
        token = self.auth.issue("ipad", now=T0).token
        self.auth.revoke_device("ipad")
        self.auth.purge_device("ipad")
        self.assertEqual(self.reason(token, T0 + 1), "device_purged")

    def test_a_purged_credential_that_had_expired_anyway_says_expired(self):
        token = self.auth.issue("ipad", now=T0).token
        self.auth.revoke_device("ipad")
        self.auth.purge_device("ipad")
        self.assertEqual(self.reason(token, T0 + 30 * DAY), "credential_expired")

    def test_a_token_this_host_never_signed_is_unknown(self):
        token = self.auth.issue("ipad", now=T0).token
        stranger = DeviceAuthenticator(secret=b"q" * 32)
        self.assertEqual(self.reason(stranger.issue("ipad", now=T0).token, T0 + 1), "unknown_credential")
        self.assertEqual(self.reason(token[:-3] + "AAA", T0 + 1), "unknown_credential")
        self.assertEqual(self.reason("not-a-token", T0 + 1), "unknown_credential")
        self.assertEqual(self.reason(token, T0 - 100), "unknown_credential")  # issued in the future

    def test_a_registry_from_before_core2_learns_the_iat_when_the_token_comes_back(self):
        token = self.auth.issue("ipad", now=T0).token
        registry = self.path.with_suffix(".credentials.json")
        data = json.loads(registry.read_text())
        # The shape a pre-CORE-2 core wrote: no issued_at, no retire_at.
        registry.write_text(json.dumps({"issued": data["issued"], "revoked": data["revoked"],
                                        "names": data["names"]}))
        auth = DeviceAuthenticator.from_file(self.path)
        row = auth.list_devices(now=T0 + 1)[0]
        self.assertEqual((row["status"], row["active_credentials"], row["expires_at"]), ("authorized", 1, None))
        self.assertEqual(auth.verify(token, T0 + 5), "ipad")
        self.assertEqual(auth.list_devices(now=T0 + 6)[0]["expires_at"], T0 + 30 * DAY)
        # Only the stamp was added; the credential itself is the same hash.
        after = json.loads(registry.read_text())
        self.assertEqual(after["issued"], data["issued"])
        self.assertEqual(after["issued_at"], {hashlib.sha256(token.encode()).hexdigest(): T0})

    def test_an_old_registry_that_expired_unseen_is_learned_on_the_refusal(self):
        token = self.auth.issue("ipad", now=T0).token
        registry = self.path.with_suffix(".credentials.json")
        data = json.loads(registry.read_text())
        registry.write_text(json.dumps({"issued": data["issued"], "revoked": [], "names": {}}))
        auth = DeviceAuthenticator.from_file(self.path)
        self.assertEqual(auth.list_devices(now=T0 + 31 * DAY)[0]["status"], "authorized")  # cannot know yet
        with self.assertRaises(CredentialError):
            auth.verify(token, T0 + 31 * DAY)
        row = auth.list_devices(now=T0 + 31 * DAY)[0]
        self.assertEqual((row["status"], row["expired_credentials"], row["active_credentials"]), ("expired", 1, 0))


class ListingTests(unittest.TestCase):
    def test_devices_list_carries_expiry_and_an_expired_status(self):
        auth = DeviceAuthenticator(secret=b"s" * 32)
        auth.issue("ipad", now=T0, device_name="iPad")
        auth.issue("phone", now=T0 - 29 * DAY)
        auth.issue("gone", now=T0 - 31 * DAY)
        rows = {row["device_id"]: row for row in auth.list_devices(now=T0)}
        self.assertEqual(rows["ipad"]["expires_at"], T0 + 30 * DAY)
        self.assertEqual(rows["phone"]["expires_at"], T0 + DAY)
        self.assertEqual((rows["gone"]["status"], rows["gone"]["expires_at"]), ("expired", T0 - DAY))
        self.assertEqual((rows["gone"]["active_credentials"], rows["gone"]["expired_credentials"]), (0, 1))

    def test_the_panels_own_credential_has_no_expiry_on_the_local_socket_only(self):
        auth = DeviceAuthenticator(secret=b"s" * 32)
        token = auth.issue(PLUGIN_DEVICE_ID, now=T0).token
        later = T0 + 90 * DAY
        self.assertEqual(auth.verify(token, later, local=True), PLUGIN_DEVICE_ID)
        with self.assertRaises(CredentialError) as refused:
            auth.verify(token, later)  # the same token over the network
        self.assertEqual(refused.exception.reason, "credential_expired")
        row = auth.list_devices(now=later)[0]
        self.assertEqual((row["status"], row["expires_at"]), ("authorized", None))
        # `local` means nothing for a companion.
        companion = auth.issue("ipad", now=T0).token
        with self.assertRaises(CredentialError):
            auth.verify(companion, later, local=True)

    def test_an_expired_device_can_pair_again(self):
        with tempfile.TemporaryDirectory() as temp:
            auth = DeviceAuthenticator.from_file(Path(temp) / "device.secret")
            store = PairingStore(auth, Path(temp) / "pairing.json")
            auth.issue("ipad", now=1)  # long expired
            request = store.request(device_id="ipad", device_name="iPad")
            self.assertEqual(request["status"], "pending")
            store.decide(request["request_id"], approve=True)
            claimed = store.claim(request["request_id"], request["request_secret"])
            self.assertEqual(auth.verify(claimed["credential"]), "ipad")
            # Re-pairing forgot the dead hash instead of carrying it for ever.
            self.assertEqual(auth.list_devices()[0]["expired_credentials"], 0)

    def test_a_device_with_a_live_credential_still_cannot_pair_twice(self):
        with tempfile.TemporaryDirectory() as temp:
            auth = DeviceAuthenticator.from_file(Path(temp) / "device.secret")
            store = PairingStore(auth, Path(temp) / "pairing.json")
            auth.issue("ipad")
            with self.assertRaises(ServiceError) as refused:
                store.request(device_id="ipad", device_name="iPad")
            self.assertEqual(refused.exception.code, "pairing_device_exists")


class RenewalTests(unittest.TestCase):
    def setUp(self):
        self.auth = DeviceAuthenticator(secret=b"r" * 32)
        self.token = self.auth.issue("ipad", now=T0, device_name="iPad").token

    def test_a_credential_is_not_renewable_before_its_last_week(self):
        with self.assertRaises(CredentialRenewalRefused) as refused:
            self.auth.renew(self.token, T0 + 22 * DAY)
        self.assertEqual(refused.exception.code, "credential_renewal_not_due")
        self.assertEqual(refused.exception.expires_at, T0 + 30 * DAY)
        self.assertEqual(refused.exception.renewable_at, T0 + 23 * DAY)
        info = self.auth.credential_info(self.token, T0 + 22 * DAY)
        self.assertEqual((info["renewable"], info["renewable_at"], info["superseded"]),
                         (False, T0 + 23 * DAY, False))

    def test_renewal_hands_over_a_new_credential_and_the_old_one_has_a_day(self):
        now = T0 + 25 * DAY
        self.assertTrue(self.auth.credential_info(self.token, now)["renewable"])
        fresh = self.auth.renew(self.token, now)
        self.assertEqual((fresh.device_id, fresh.issued_at, fresh.expires_at), ("ipad", now, now + 30 * DAY))
        self.assertEqual(self.auth.verify(fresh.token, now + 1), "ipad")
        self.assertEqual(self.auth.verify(self.token, now + DAY - 1), "ipad")
        self.assertTrue(self.auth.credential_info(self.token, now + 1)["superseded"])
        with self.assertRaises(CredentialError) as refused:
            self.auth.verify(self.token, now + DAY)
        self.assertEqual(refused.exception.reason, "credential_expired")
        row = self.auth.list_devices(now=now + DAY)[0]
        self.assertEqual((row["active_credentials"], row["expires_at"], row["device_name"]),
                         (1, now + 30 * DAY, "iPad"))

    def test_renewing_again_from_the_old_credential_leaves_one_live_credential(self):
        now = T0 + 25 * DAY
        first = self.auth.renew(self.token, now)
        second = self.auth.renew(self.token, now + 60)  # the reply to the first was lost
        self.assertEqual(self.auth.verify(second.token, now + 2 * DAY), "ipad")
        with self.assertRaises(CredentialError):
            self.auth.verify(first.token, now + 2 * DAY)
        self.assertEqual(self.auth.list_devices(now=now + 2 * DAY)[0]["active_credentials"], 1)

    def test_revoked_purged_and_expired_are_never_renewed(self):
        with self.assertRaises(CredentialError) as expired:
            self.auth.renew(self.token, T0 + 30 * DAY)
        self.assertEqual(expired.exception.reason, "credential_expired")
        self.auth.revoke_device("ipad")
        with self.assertRaises(CredentialError) as revoked:
            self.auth.renew(self.token, T0 + 25 * DAY)
        self.assertEqual(revoked.exception.reason, "credential_revoked")
        self.auth.purge_device("ipad")
        with self.assertRaises(CredentialError) as purged:
            self.auth.renew(self.token, T0 + 25 * DAY)
        self.assertEqual(purged.exception.reason, "device_purged")

    def test_the_panels_credential_is_not_renewed(self):
        token = self.auth.issue(PLUGIN_DEVICE_ID, now=T0 + 25 * DAY).token
        with self.assertRaises(CredentialRenewalRefused) as refused:
            self.auth.renew(token, T0 + 50 * DAY)
        self.assertEqual(refused.exception.code, "plugin_credential")


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    """The wire: 401 bodies with a reason, and the two new routes."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        # A short life so "in its last week" is now, as the test daemon runs it.
        self.auth = DeviceAuthenticator.from_file(base / "device.secret", ttl_seconds=600, grace_seconds=120)
        self.service = create_service(Hub(authenticator=self.auth), demo=True)
        self.service.pairing = PairingStore(self.auth, base / "pairing.json")
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.client = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.close()
        self.temp.cleanup()

    async def call(self, method, path, token, payload=None):
        headers = {"Authorization": "Bearer " + token}
        async with self.client.request(method, self.url + path, headers=headers, json=payload) as response:
            return response.status, await response.json()

    async def test_every_refusal_names_its_reason(self):
        token = self.auth.issue("ipad").token
        self.assertEqual((await self.call("GET", "/v1/state", token))[0], 200)
        self.auth.revoke_device("ipad")
        status, body = await self.call("GET", "/v1/state", token)
        self.assertEqual((status, body["error"]["code"], body["error"]["reason"]),
                         (401, "permission_denied", "credential_revoked"))
        self.auth.purge_device("ipad")
        status, body = await self.call("GET", "/v1/state", token)
        self.assertEqual((status, body["error"]["reason"]), (401, "device_purged"))
        status, body = await self.call("GET", "/v1/state", "x.y")
        self.assertEqual((status, body["error"]["reason"]), (401, "unknown_credential"))
        stale = self.auth.issue("phone", now=1).token
        status, body = await self.call("GET", "/v1/capabilities", stale)
        self.assertEqual((status, body["error"]["reason"]), (401, "credential_expired"))
        status, body = await self.call("POST", "/v1/pairing/renew", stale, {})
        self.assertEqual((status, body["error"]["reason"]), (401, "credential_expired"))

    async def test_a_missing_header_is_still_pairing_required_without_a_reason(self):
        async with self.client.get(self.url + "/v1/state") as response:
            body = await response.json()
        self.assertEqual((response.status, body["error"]["code"]), (401, "pairing_required"))
        self.assertNotIn("reason", body["error"])

    async def test_read_then_renew_then_the_old_credential_runs_out(self):
        token = self.auth.issue("ipad", device_name="iPad").token
        status, info = await self.call("GET", "/v1/pairing/credential", token)
        self.assertEqual(status, 200)
        self.assertEqual((info["device_id"], info["renewable"], info["superseded"]), ("ipad", True, False))
        self.assertEqual(info["expires_at"] - info["issued_at"], 600)
        status, renewed = await self.call("POST", "/v1/pairing/renew", token, {})
        self.assertEqual(status, 200)
        self.assertEqual(renewed["credential_expires_at"] - renewed["issued_at"], 600)
        self.assertEqual(renewed["previous_credential_expires_at"] - renewed["issued_at"], 120)
        self.assertEqual((await self.call("GET", "/v1/state", renewed["credential"]))[0], 200)
        self.assertEqual((await self.call("GET", "/v1/state", token))[0], 200)  # grace
        status, info = await self.call("GET", "/v1/pairing/credential", token)
        self.assertTrue(info["superseded"])
        self.assertNotIn(renewed["credential"], json.dumps(self.auth.list_devices()))

    async def test_renewal_that_is_not_due_says_when_it_will_be(self):
        auth = self.auth
        auth.renew_window_seconds = 60
        token = auth.issue("ipad").token
        status, body = await self.call("POST", "/v1/pairing/renew", token, {})
        self.assertEqual((status, body["error"]["code"]), (409, "credential_renewal_not_due"))
        self.assertEqual(set(body["error"]["detail"]), {"expires_at", "renewable_at"})
        status, body = await self.call("POST", "/v1/pairing/renew", token, {"unexpected": 1})
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"))

    async def test_the_panel_cannot_renew_over_the_network(self):
        token = self.auth.issue(PLUGIN_DEVICE_ID).token
        status, body = await self.call("POST", "/v1/pairing/renew", token, {})
        self.assertEqual((status, body["error"]["code"]), (409, "plugin_credential"))


if __name__ == "__main__":
    unittest.main()


class LocalSocketTests(unittest.IsolatedAsyncioTestCase):
    """The panel's credential over the same-UID socket, past the TTL a companion would have."""

    async def test_the_panel_keeps_working_on_the_socket_after_day_thirty(self):
        from omodachi_core.ipc import JsonLineClient, JsonLineServer
        with tempfile.TemporaryDirectory() as temp:
            auth = DeviceAuthenticator(secret=b"l" * 32, ttl_seconds=60)
            hub = Hub(authenticator=auth)
            panel = auth.issue(PLUGIN_DEVICE_ID, now=1).token      # long past 60 s
            phone = auth.issue("phone", now=1).token
            path = str(Path(temp) / "hub.sock")
            server = JsonLineServer(hub, path, request_timeout=0.5)
            await server.start()
            try:
                self.assertTrue((await JsonLineClient(path, panel).request("state"))["ok"])
                refused = await JsonLineClient(path, phone).request("state")
                self.assertFalse(refused["ok"])
            finally:
                await server.close()
