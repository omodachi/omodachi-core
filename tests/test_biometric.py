"""AUTH-1: the verifier, the key store, and every way an approval says no."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import shutil
import unittest

from omodachi_core.auth import DeviceAuthenticator
from omodachi_core.biometric import (ApprovalBroker, BiometricKeyStore, _G, _N, _multiply,
                                     approval_message, enrollment_message, public_point,
                                     verify_signature)
from omodachi_core.hub import Hub
from omodachi_core.service import ServiceError


def sign(private, message):
    digest = int.from_bytes(hashlib.sha256(message).digest(), "big")
    while True:
        k = secrets.randbelow(_N - 1) + 1
        r = _multiply(k, _G)[0] % _N
        if not r:
            continue
        s = pow(k, _N - 2, _N) * (digest + r * private) % _N
        if s:
            return der(r, s)


def der(r, s):
    def integer(value):
        raw = value.to_bytes((value.bit_length() + 8) // 8 or 1, "big")
        return bytes([0x02, len(raw)]) + raw
    body = integer(r) + integer(s)
    return bytes([0x30, len(body)]) + body


def keypair():
    private = secrets.randbelow(_N - 1) + 1
    point = _multiply(private, _G)
    return private, b"\x04" + point[0].to_bytes(32, "big") + point[1].to_bytes(32, "big")


class SignatureTests(unittest.TestCase):
    def test_a_good_signature_verifies_and_a_changed_message_does_not(self):
        private, public = keypair()
        message = b"omodachi"
        signature = sign(private, message)
        self.assertTrue(verify_signature(public, message, signature))
        self.assertFalse(verify_signature(public, message + b"!", signature))

    def test_another_key_cannot_answer(self):
        private, _ = keypair()
        _, other = keypair()
        self.assertFalse(verify_signature(other, b"omodachi", sign(private, b"omodachi")))

    def test_openssl_agrees_with_this_verifier(self):
        """The one independent check.

        Everything else in this file signs with the same curve arithmetic it
        verifies with, so a bug in `_add`/`_multiply` would pass both halves.
        This vector is produced by OpenSSL and verified by us; if they disagree,
        the arithmetic is wrong and not merely self-consistent.
        """
        if shutil.which("openssl") is None:
            self.skipTest("openssl is not installed")
        directory = Path(tempfile.mkdtemp())
        try:
            key, message = directory / "k.pem", directory / "m.bin"
            message.write_bytes(b"omodachi-auth-approval-v1\nhost\napproval\nnonce\nsudo\nroot\nipad\n")
            subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout",
                            "-out", str(key)], check=True, capture_output=True)
            raw = subprocess.run(["openssl", "ec", "-in", str(key), "-pubout", "-outform", "DER"],
                                 check=True, capture_output=True).stdout
            signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key), str(message)],
                                       check=True, capture_output=True).stdout
            self.assertTrue(verify_signature(raw, message.read_bytes(), signature))
            self.assertFalse(verify_signature(raw, message.read_bytes() + b"x", signature))
            # And the SPKI DER OpenSSL emits parses to the same point as the
            # bare X9.63 point an iPhone would send.
            self.assertEqual(public_point(raw), public_point(raw[-65:]))
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_malformed_inputs_are_refused_rather_than_raising(self):
        _, public = keypair()
        for signature in (b"", b"\x30\x00", b"\x30\x06\x02\x01\x01\x02\x01\x01",
                          b"\x31\x08\x02\x01\x01\x02\x01\x01", b"\x30\x08\x02\x02\x00\x01\x02\x01\x01"):
            self.assertFalse(verify_signature(public, b"m", signature))
        for key in (b"", b"\x04" + b"\x00" * 64, b"\x02" + b"\x01" * 32):
            self.assertFalse(verify_signature(key, b"m", sign(1, b"m")))

    def test_zero_and_out_of_range_scalars_are_refused(self):
        _, public = keypair()
        self.assertFalse(verify_signature(public, b"m", der(0, 1)))
        self.assertFalse(verify_signature(public, b"m", der(1, 0)))
        self.assertFalse(verify_signature(public, b"m", der(_N, 1)))

    def test_the_signed_bytes_name_everything_that_scopes_the_approval(self):
        message = approval_message(host_id="h", approval_id="a", nonce="n", service="sudo",
                                   user="root", device_id="ipad")
        self.assertEqual(message, b"omodachi-auth-approval-v1\nh\na\nn\nsudo\nroot\nipad\n")
        # A different service is a different message, so a signature for one
        # prompt is not a signature for another.
        self.assertNotEqual(message, approval_message(host_id="h", approval_id="a", nonce="n",
                                                      service="hyprlock", user="root", device_id="ipad"))

    def test_a_newline_cannot_be_smuggled_into_a_field(self):
        with self.assertRaises(ServiceError):
            approval_message(host_id="h\nx", approval_id="a", nonce="n", service="sudo",
                             user="root", device_id="ipad")


class RealDeviceKeyTests(unittest.TestCase):
    """AUTH-2 §7.7 said no real Secure Enclave key had ever been checked here.

    This is one: the public half of the key Leo's iPad enrolled on `omarchy`,
    read off `~/.config/omodachi/biometric-keys.json` on 2026-09-21. Only the
    public half exists outside the enclave, so there is nothing secret in it,
    and it is the evidence that the encoding was never the problem - the host
    accepted this key because a real enclave signature over the enrolment
    message verified against it with the parser below.
    """

    REAL_IPAD_KEY = ("BDyL0tQQHMaZF/kEvOGWJflKFsvi8au4OOW+1lTPxBQp"
                     "+GE3qd/GcS9rd5iU0mVaSvrZ2xOIFPB4KwiHMgGS1Lc=")

    def test_the_enclave_key_is_an_x963_point_this_verifier_reads(self):
        raw = base64.b64decode(self.REAL_IPAD_KEY, validate=True)
        self.assertEqual(len(raw), 65)
        self.assertEqual(raw[0], 0x04)
        x, y = public_point(raw)
        self.assertEqual(raw, b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big"))

    def test_the_same_key_wrapped_as_spki_der_parses_to_the_same_point(self):
        raw = base64.b64decode(self.REAL_IPAD_KEY, validate=True)
        prefix = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")
        self.assertEqual(public_point(prefix + raw), public_point(raw))

    def test_a_raw_r_s_signature_is_refused_rather_than_silently_accepted(self):
        """Core parses DER and only DER, and iOS emits DER. Pin both halves.

        `.ecdsaSignatureMessageX962SHA256` gives DER; the raw 64-byte `r || s`
        form some ECDSA APIs return is not a `SEQUENCE` and must not verify,
        because accepting both would make the wire format unwritable-down.
        """
        private, public = keypair()
        message = b"omodachi-auth-approval-v1\nh\na\nn\nsudo\nroot\nipad\n"
        encoded = sign(private, message)
        self.assertEqual(encoded[0], 0x30)
        body = encoded[2:]
        r = int.from_bytes(body[2:2 + body[1]], "big")
        rest = body[2 + body[1]:]
        s = int.from_bytes(rest[2:2 + rest[1]], "big")
        self.assertTrue(verify_signature(public, message, encoded))
        self.assertFalse(verify_signature(public, message,
                                          r.to_bytes(32, "big") + s.to_bytes(32, "big")))


class _Journal:
    def __init__(self):
        self.entries = []

    def __call__(self, entry):
        self.entries.append(entry)

    def kinds(self):
        return [entry["event"] for entry in self.entries]


class _Preferences:
    def __init__(self, enabled):
        self.enabled = enabled

    def get(self):
        return {"revision": 0, "values": {"biometric_auth": self.enabled}}


class _Identity:
    host_id = "0123456789abcdef0123456789abcdef"
    host_name = "omarchy"


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.authority = DeviceAuthenticator(secret=b"\x11" * 32,
                                             state_path=self.directory / "credentials.json")
        self.hub = Hub(authenticator=self.authority)
        self.keys = BiometricKeyStore(self.directory / "biometric-keys.json")
        self.journal = _Journal()
        self.preferences = _Preferences(True)
        self.broker = ApprovalBroker(self.hub, self.keys, preferences=self.preferences,
                                     host_identity=_Identity(), journal=self.journal)
        self.private, self.public = keypair()
        self.connected = set()
        self.hub.connected_devices = lambda: set(self.connected)

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def enrol(self, device_id="ipad", enabled=True, private=None, public=None):
        private = self.private if private is None else private
        public = self.public if public is None else public
        self.authority.issue(device_id, device_name="Leo's iPad")
        challenge = self.broker.challenge(device_id)
        encoded = base64.b64encode(public).decode()
        message = enrollment_message(host_id=_Identity.host_id, device_id=device_id,
                                     challenge=challenge, public_key_b64=encoded)
        return self.broker.enroll(device_id, {
            "public_key": encoded, "label": "iPad", "challenge": challenge, "enabled": enabled,
            "secure_enclave": True, "signature": base64.b64encode(sign(private, message)).decode()})

    async def raise_approval(self, **overrides):
        payload = {"service": "sudo", "user": "alex", "requester": "alex",
                   "tty": "pts/3", "timeout": 5, **overrides}
        return await self.broker.request(payload)

    def pending_id(self):
        return next(iter(self.broker._pending))

    def answer(self, device_id="ipad", private=None, approval_id=None, service="sudo", user="alex"):
        approval_id = approval_id or self.pending_id()
        message = approval_message(host_id=_Identity.host_id, approval_id=approval_id,
                                   nonce=self.broker._pending[approval_id]["nonce"],
                                   service=service, user=user, device_id=device_id)
        signature = base64.b64encode(sign(self.private if private is None else private, message)).decode()
        return self.broker.resolve(approval_id, device_id, {"decision": "approve", "signature": signature})

    # -- enrolment --------------------------------------------------------
    def test_enrolment_needs_the_challenge_and_a_signature_that_matches_the_key(self):
        row = self.enrol()
        self.assertEqual(row["device_id"], "ipad")
        self.assertTrue(row["secure_enclave"])
        self.assertNotIn("public_key", row)
        self.assertIn("auth.key.enrolled", self.journal.kinds())

    def test_a_signature_from_another_key_does_not_enrol(self):
        other, _ = keypair()
        with self.assertRaises(ServiceError) as caught:
            self.enrol(private=other)
        self.assertEqual(caught.exception.code, "biometric_signature_invalid")

    def test_a_challenge_is_spent_even_when_the_signature_was_wrong(self):
        challenge = self.broker.challenge("ipad")
        encoded = base64.b64encode(self.public).decode()
        bad = {"public_key": encoded, "label": "iPad", "challenge": challenge, "enabled": True,
               "signature": base64.b64encode(sign(keypair()[0], b"nope")).decode()}
        with self.assertRaises(ServiceError):
            self.broker.enroll("ipad", bad)
        with self.assertRaises(ServiceError) as caught:
            self.broker.enroll("ipad", bad)
        self.assertEqual(caught.exception.code, "biometric_challenge_invalid")

    def test_the_device_must_report_its_own_switch(self):
        challenge = self.broker.challenge("ipad")
        encoded = base64.b64encode(self.public).decode()
        message = enrollment_message(host_id=_Identity.host_id, device_id="ipad",
                                     challenge=challenge, public_key_b64=encoded)
        with self.assertRaises(ServiceError):
            self.broker.enroll("ipad", {"public_key": encoded, "label": "iPad",
                                        "challenge": challenge,
                                        "signature": base64.b64encode(sign(self.private, message)).decode()})

    # -- the two switches -------------------------------------------------
    async def test_the_host_switch_off_refuses_before_anything_is_published(self):
        self.enrol()
        self.connected.add("ipad")
        self.preferences.enabled = False
        cursor = self.hub.event_cursor
        result = await self.raise_approval()
        self.assertEqual(result, {"approved": False, "approval_id": None, "outcome": "disabled",
                                  "device_id": None, "device_name": None})
        self.assertEqual(self.hub.event_cursor, cursor, "nothing may be published when the host says no")

    async def test_the_device_switch_off_keeps_it_out_of_the_audience(self):
        self.enrol(enabled=False)
        self.connected.add("ipad")
        result = await self.raise_approval()
        self.assertEqual(result["outcome"], "no_connected_device")
        self.assertEqual(self.broker.status()["eligible_devices"], [])

    async def test_flipping_the_device_switch_back_on_makes_it_eligible(self):
        self.enrol(enabled=False)
        self.connected.add("ipad")
        self.assertEqual(self.broker.set_enabled("ipad", {"enabled": True})["enabled"], True)
        self.assertEqual(self.broker.status()["eligible_devices"], ["ipad"])
        self.broker.set_enabled("ipad", {"enabled": False})
        self.assertEqual((await self.raise_approval())["outcome"], "no_connected_device")

    async def test_a_disconnected_device_is_never_asked(self):
        self.enrol()
        self.assertEqual((await self.raise_approval())["outcome"], "no_connected_device")

    async def test_a_revoked_credential_takes_the_audience_with_it(self):
        self.enrol()
        self.connected.add("ipad")
        self.authority.revoke_device("ipad")
        self.assertEqual((await self.raise_approval())["outcome"], "no_connected_device")

    # -- approving --------------------------------------------------------
    async def test_a_signed_answer_approves_and_is_journaled(self):
        self.enrol()
        self.connected.add("ipad")
        task = asyncio.create_task(self.raise_approval())
        await asyncio.sleep(0.05)
        self.assertEqual(self.answer()["outcome"], "approved")
        result = await task
        self.assertTrue(result["approved"])
        self.assertEqual(result["device_id"], "ipad")
        self.assertEqual(result["device_name"], "Leo's iPad")
        self.assertIn("auth.approval.requested", self.journal.kinds())
        decided = [row for row in self.journal.entries if row["event"] == "auth.approval.decided"][0]
        self.assertEqual((decided["outcome"], decided["service"], decided["user"]),
                         ("approved", "sudo", "alex"))

    async def test_the_same_approval_cannot_be_answered_twice(self):
        self.enrol()
        self.connected.add("ipad")
        task = asyncio.create_task(self.raise_approval())
        await asyncio.sleep(0.05)
        approval_id = self.pending_id()
        nonce = self.broker._pending[approval_id]["nonce"]
        message = approval_message(host_id=_Identity.host_id, approval_id=approval_id, nonce=nonce,
                                   service="sudo", user="alex", device_id="ipad")
        body = {"decision": "approve", "signature": base64.b64encode(sign(self.private, message)).decode()}
        self.assertEqual(self.broker.resolve(approval_id, "ipad", body)["outcome"], "approved")
        await task
        with self.assertRaises(ServiceError) as caught:
            self.broker.resolve(approval_id, "ipad", body)
        self.assertEqual(caught.exception.code, "auth_approval_unknown")

    async def test_a_signature_for_another_service_does_not_approve_this_one(self):
        self.enrol()
        self.connected.add("ipad")
        task = asyncio.create_task(self.raise_approval())
        await asyncio.sleep(0.05)
        with self.assertRaises(ServiceError) as caught:
            self.answer(service="hyprlock")
        self.assertEqual(caught.exception.code, "biometric_signature_invalid")
        self.assertFalse((await task)["approved"])

    async def test_a_device_that_was_not_asked_cannot_answer(self):
        self.enrol()
        self.enrol(device_id="phone")
        self.connected.add("ipad")
        task = asyncio.create_task(self.raise_approval())
        await asyncio.sleep(0.05)
        with self.assertRaises(ServiceError) as caught:
            self.answer(device_id="phone")
        self.assertEqual(caught.exception.code, "auth_approval_unknown")
        await task

    async def test_declining_is_immediate_and_needs_no_signature(self):
        self.enrol()
        self.connected.add("ipad")
        task = asyncio.create_task(self.raise_approval(timeout=30))
        await asyncio.sleep(0.05)
        self.broker.resolve(self.pending_id(), "ipad", {"decision": "decline"})
        result = await asyncio.wait_for(task, 2)
        self.assertEqual((result["approved"], result["outcome"]), (False, "declined"))

    async def test_a_timeout_ends_the_wait_and_tells_the_device(self):
        self.enrol()
        self.connected.add("ipad")
        result = await self.broker.request({"service": "sudo", "user": "alex",
                                            "requester": "alex", "timeout": 5})
        self.assertEqual((result["approved"], result["outcome"]), (False, "timeout"))
        published = [event.type for event in self.hub.events_since(0, limit=None, device_id="ipad")]
        self.assertIn("auth.approval.requested", published)
        self.assertIn("auth.approval.resolved", published)
        self.assertEqual(self.broker._pending, {})

    async def test_the_timeout_is_clamped_into_a_range_a_person_can_live_with(self):
        self.enrol()
        self.connected.add("ipad")
        for requested in (0, -5, "nonsense", None):
            task = asyncio.create_task(self.raise_approval(timeout=requested))
            await asyncio.sleep(0.05)
            record = self.broker._pending[self.pending_id()]
            self.assertGreaterEqual(record["expires_at"] - record["created_at"], ApprovalBroker.MIN_TIMEOUT)
            self.broker.resolve(self.pending_id(), "ipad", {"decision": "decline"})
            await task

    async def test_an_unsupported_service_is_refused_without_asking_anybody(self):
        self.enrol()
        self.connected.add("ipad")
        cursor = self.hub.event_cursor
        self.assertEqual((await self.raise_approval(service="sshd"))["outcome"], "unsupported_service")
        self.assertEqual(self.hub.event_cursor, cursor)

    async def test_the_approval_only_reaches_the_device_it_is_addressed_to(self):
        self.enrol()
        self.enrol(device_id="phone", enabled=False)
        self.connected.update({"ipad", "phone"})
        task = asyncio.create_task(self.raise_approval())
        await asyncio.sleep(0.05)
        for event in self.hub.events_since(0, limit=None, device_id="phone"):
            self.assertNotEqual(event.type, "auth.approval.requested")
        self.broker.resolve(self.pending_id(), "ipad", {"decision": "decline"})
        await task

    # -- UX-3 §1: the device is told which identity to sign as ------------
    async def test_the_frame_states_the_identity_the_device_must_sign_as(self):
        """The one signed field a device cannot derive is the one we now send.

        `device_id` is the credential this connection authenticated with. A
        device that re-paired, or was reinstalled onto a Keychain it kept,
        holds a *different* local id and signed that instead - which is what
        made every approval on Leo's iPad fail (UX-3 §1). Saying it per device
        removes the guess.
        """
        self.enrol()
        self.enrol(device_id="phone")
        self.connected.update({"ipad", "phone"})
        task = asyncio.create_task(self.raise_approval())
        await asyncio.sleep(0.05)
        for device_id in ("ipad", "phone"):
            frames = [event for event in self.hub.events_since(0, limit=None, device_id=device_id)
                      if event.type == "auth.approval.requested"]
            self.assertEqual(len(frames), 1)
            self.assertEqual(frames[0].payload["device_id"], device_id)
        self.broker.resolve(self.pending_id(), "ipad", {"decision": "decline"})
        await task

    async def test_the_pending_list_states_it_too(self):
        self.enrol()
        self.connected.add("ipad")
        task = asyncio.create_task(self.raise_approval(timeout=30))
        await asyncio.sleep(0.05)
        self.assertEqual(self.broker.pending_for("ipad")["approvals"][0]["device_id"], "ipad")
        self.broker.resolve(self.pending_id(), "ipad", {"decision": "decline"})
        await task

    async def test_a_signature_made_under_another_identity_is_refused_and_named(self):
        """The rejection says *why*, and nothing about it approves anything.

        Before this, a device signing as the wrong identity and a device
        signing garbage printed the same line, so the journal on the real host
        could not tell Leo's round apart from a tampered submission.
        """
        self.enrol(device_id="ipad-new")
        # The same physical device, still holding the credential it paired
        # with first. Both ids are known to the host; only one is enrolled.
        self.authority.issue("ipad-old", device_name="Leo's iPad")
        self.connected.add("ipad-new")
        task = asyncio.create_task(self.raise_approval())
        await asyncio.sleep(0.05)
        approval_id = self.pending_id()
        message = approval_message(host_id=_Identity.host_id, approval_id=approval_id,
                                   nonce=self.broker._pending[approval_id]["nonce"],
                                   service="sudo", user="alex", device_id="ipad-old")
        signature = base64.b64encode(sign(self.private, message)).decode()
        with self.assertRaises(ServiceError) as caught:
            self.broker.resolve(approval_id, "ipad-new",
                                {"decision": "approve", "signature": signature})
        self.assertEqual(caught.exception.code, "biometric_signature_invalid")
        rejected = [entry for entry in self.journal.entries
                    if entry["event"] == "auth.approval.signature_rejected"]
        self.assertEqual(rejected[-1]["reason"], "device_id_mismatch")
        self.assertEqual(rejected[-1]["signed_as"], "ipad-old")
        # Refused means refused: the approval is still pending and still unspent.
        self.assertIsNone(self.broker._pending[approval_id]["outcome"])
        self.broker.resolve(approval_id, "ipad-new", {"decision": "decline"})
        await task

    async def test_a_signature_that_is_simply_wrong_says_so_without_guessing(self):
        self.enrol()
        self.authority.issue("phone", device_name="Leo's phone")
        self.connected.add("ipad")
        task = asyncio.create_task(self.raise_approval())
        await asyncio.sleep(0.05)
        approval_id = self.pending_id()
        other, _ = keypair()
        message = approval_message(host_id=_Identity.host_id, approval_id=approval_id,
                                   nonce=self.broker._pending[approval_id]["nonce"],
                                   service="sudo", user="alex", device_id="ipad")
        with self.assertRaises(ServiceError):
            self.broker.resolve(approval_id, "ipad", {
                "decision": "approve", "signature": base64.b64encode(sign(other, message)).decode()})
        rejected = [entry for entry in self.journal.entries
                    if entry["event"] == "auth.approval.signature_rejected"]
        self.assertEqual(rejected[-1]["reason"], "signature")
        self.assertNotIn("signed_as", rejected[-1])
        self.broker.resolve(approval_id, "ipad", {"decision": "decline"})
        await task

    async def test_a_reconnecting_device_can_read_what_it_is_still_being_asked(self):
        self.enrol()
        self.connected.add("ipad")
        task = asyncio.create_task(self.raise_approval(timeout=30))
        await asyncio.sleep(0.05)
        pending = self.broker.pending_for("ipad")["approvals"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["service"], "sudo")
        self.assertEqual(self.broker.pending_for("phone")["approvals"], [])
        self.broker.resolve(self.pending_id(), "ipad", {"decision": "decline"})
        await task

    async def test_there_is_a_ceiling_on_pending_approvals(self):
        self.enrol()
        self.connected.add("ipad")
        tasks = [asyncio.create_task(self.raise_approval(timeout=5))
                 for _ in range(ApprovalBroker.MAX_PENDING)]
        await asyncio.sleep(0.2)
        self.assertEqual((await self.raise_approval())["outcome"], "too_many_pending")
        for approval_id in list(self.broker._pending):
            self.broker.resolve(approval_id, "ipad", {"decision": "decline"})
        await asyncio.gather(*tasks)

    def test_the_store_survives_a_round_trip_and_refuses_a_corrupt_file(self):
        self.enrol()
        again = BiometricKeyStore(self.directory / "biometric-keys.json")
        self.assertEqual([row["device_id"] for row in again.list()], ["ipad"])
        (self.directory / "biometric-keys.json").write_text(json.dumps({"keys": {"ipad": {"public_key": "!!"}}}))
        with self.assertRaises(ServiceError):
            again.list()

    def test_revoking_forgets_the_key(self):
        self.enrol()
        self.assertTrue(self.broker.revoke("ipad")["revoked"])
        self.assertEqual(self.keys.list(), [])
        self.assertFalse(self.broker.revoke("ipad")["revoked"])


if __name__ == "__main__":
    unittest.main()
