"""REMOTE-PAIR-1: the same phone pairing again keeps Remote and its terminal.

Synthetic fork, synthetic keys, temporary files only. Nothing here talks to a
real Sunshine or touches a real `authorized_keys`.
"""
import base64
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from omodachi_core.auth import CredentialStoreUnavailable, DeviceAuthenticator
from omodachi_core.media_pairing import MediaPairingBridge, MediaPairingError, MediaPairingStore
from omodachi_core.ssh_keys import AuthorizedKeys, SshKeyError
from tests.test_media_pairing import Clock, FakeFork
from tests import test_media_pairing_routing as routing


def key_line(fill, comment="omodachi-ios"):
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([fill]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode() + " " + comment


class RebindTests(unittest.TestCase):
    """The bridge: a certificate moves only on the fork's proof."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.fork = FakeFork(revocation=True)
        self.store = MediaPairingStore(Path(self.tmp.name) / "private" / "state.json")
        self.moves = []
        self.bridge = MediaPairingBridge(self.fork, self.store, clock=self.clock, monotonic=self.clock,
                                         on_rebind=self.moves.append)
        self.auth = lambda: True
        self.local = lambda: True
        self.fp = "a" * 64

    def tearDown(self):
        self.tmp.cleanup()

    def state(self):
        with self.store.transaction() as state:
            return deepcopy(state)

    def grant(self, device):
        self.bridge.grant_remote(device, remote_allowed=True, source_request_id="pair_" + "1" * 32,
                                 local_authorize=self.local)

    def pair(self, device, *, finish=True):
        """`device` asks the fork for this certificate and (optionally) finishes it."""
        rid = self.fork.add(self.fp)
        found = self.bridge.discover(device, self.fp, authorize=self.auth, pairing_intent=True)
        self.assertEqual(found["request_id"], rid)
        attempt = self.bridge.submit(device, {"request_id": rid, "client_cert_sha256": self.fp, "pin": "7391"},
                                     authorize=self.auth)
        if finish:
            self.fork.rows[rid]["status"] = "paired"
            attempt = self.bridge.status(device, attempt["attempt_id"], authorize=self.auth)
        return rid, attempt

    def test_the_same_certificate_moves_to_the_new_identity_once_the_fork_says_paired(self):
        self.grant("ios-old")
        _, first = self.pair("ios-old")
        self.assertTrue(self.bridge.media_authorized("ios-old", self.fp))
        self.grant("ios-new")
        # Discover and submit no longer refuse; nothing has moved yet.
        rid, pending = self.pair("ios-new", finish=False)
        self.assertEqual(pending["status"], "awaiting_client_proof")
        self.assertEqual(self.state()["bindings"][self.fp]["device_id"], "ios-old")
        self.assertTrue(self.bridge.media_authorized("ios-old", self.fp))
        self.assertEqual(self.moves, [])
        # The fork verified the handshake: now it moves, and only now.
        self.fork.rows[rid]["status"] = "paired"
        final = self.bridge.status("ios-new", pending["attempt_id"], authorize=self.auth)
        self.assertTrue(final["paired"])
        self.assertTrue(self.bridge.media_authorized("ios-new", self.fp))
        self.assertFalse(self.bridge.media_authorized("ios-old", self.fp))
        self.assertTrue(self.bridge.remote_denied("ios-old"))
        self.assertEqual(self.bridge.paired_certificate("ios-new"), self.fp)
        # The old identity's history for this certificate is gone with it.
        self.assertNotIn(first["attempt_id"], self.state()["attempts"])
        self.assertEqual(self.moves, [{"from_device_id": "ios-old", "device_id": "ios-new",
                                       "client_cert_sha256": self.fp}])
        # Revoking the retired identity can no longer take the phone's certificate back.
        self.bridge.revoke_device("ios-old", local_authorize=self.local)
        self.assertIn(self.fp, self.fork.certs)
        self.assertTrue(self.bridge.media_authorized("ios-new", self.fp))

    def test_a_failed_or_cancelled_handshake_moves_nothing(self):
        self.grant("ios-old")
        self.pair("ios-old")
        self.grant("ios-new")
        rid, pending = self.pair("ios-new", finish=False)
        self.fork.rows[rid]["status"] = "failed"
        result = self.bridge.status("ios-new", pending["attempt_id"], authorize=self.auth)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.state()["bindings"][self.fp]["device_id"], "ios-old")
        self.assertTrue(self.bridge.media_authorized("ios-old", self.fp))
        self.assertEqual(self.moves, [])

    def test_a_certificate_whose_owner_was_revoked_does_not_come_back(self):
        self.fork.revocation = False  # the binding stays `revocation_pending`
        self.grant("ios-old")
        self.pair("ios-old")
        self.bridge.revoke_device("ios-old", local_authorize=self.local)
        self.grant("ios-new")
        self.fork.add(self.fp)
        with self.assertRaisesRegex(MediaPairingError, "binding_mismatch"):
            self.bridge.discover("ios-new", self.fp, authorize=self.auth, pairing_intent=True)
        self.assertEqual(self.moves, [])

    def test_an_explicitly_denied_owner_keeps_its_certificate(self):
        self.grant("ios-old")
        self.pair("ios-old")
        with self.store.transaction() as state:
            state["devices"]["ios-old"]["allowed"] = False
            self.store.commit(state)
        self.grant("ios-new")
        rid = self.fork.add(self.fp)
        with self.assertRaisesRegex(MediaPairingError, "binding_mismatch"):
            self.bridge.submit("ios-new", {"request_id": rid, "client_cert_sha256": self.fp, "pin": "7391"},
                               authorize=self.auth)

    def test_a_device_without_a_local_media_approval_waits_and_moves_nothing(self):
        self.grant("ios-old")
        self.pair("ios-old")
        rid = self.fork.add(self.fp)
        attempt = self.bridge.submit("stranger", {"request_id": rid, "client_cert_sha256": self.fp, "pin": "7391"},
                                     authorize=self.auth)
        self.assertEqual(attempt["status"], "awaiting_local_approval")
        self.assertEqual(self.fork.approvals, 1)  # only ios-old's PIN ever reached the fork
        self.fork.rows[rid]["status"] = "paired"  # even a fork claiming success
        self.bridge.status("stranger", attempt["attempt_id"], authorize=self.auth)
        self.assertEqual(self.state()["bindings"][self.fp]["device_id"], "ios-old")
        self.assertEqual(self.moves, [])

    def test_a_move_that_did_not_reach_the_state_file_is_not_announced(self):
        self.grant("ios-old")
        self.pair("ios-old")
        self.grant("ios-new")
        rid, pending = self.pair("ios-new", finish=False)
        self.fork.rows[rid]["status"] = "paired"
        real = self.store.commit

        def failing(state):
            # The disk refuses exactly the write that would record the move.
            if state["bindings"].get(self.fp, {}).get("device_id") == "ios-new":
                raise MediaPairingError("media_pairing_state_limit", 503)
            return real(state)
        with mock.patch.object(self.store, "commit", side_effect=failing):
            with self.assertRaises(MediaPairingError):
                self.bridge.status("ios-new", pending["attempt_id"], authorize=self.auth)
        self.assertEqual(self.moves, [])
        self.assertEqual(self.state()["bindings"][self.fp]["device_id"], "ios-old")


class TakeFromTests(unittest.TestCase):
    """`authorized_keys`: a line moves between two devices, never copies."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keys = AuthorizedKeys(Path(self.tmp.name))
        self.keys.path.parent.mkdir(mode=0o700, parents=True)
        self.mine = key_line(7)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_named_donor_hands_over_its_line(self):
        self.keys.authorize(self.mine, "ios-old")
        self.assertEqual(self.keys.owner_of(self.mine), "ios-old")
        result = self.keys.replace(self.mine, "ios-new", take_from="ios-old")
        self.assertEqual(result["reason"], "moved")
        self.assertEqual(result["moved_from"], "ios-old")
        rows = self.keys.listing()
        self.assertEqual([row["device"] for row in rows], ["ios-new"])
        # One owner: revoking the old id removes nothing, revoking the new one removes it.
        self.assertEqual(self.keys.revoke("ios-old")["removed"], 0)
        self.assertEqual(self.keys.revoke("ios-new")["removed"], 1)

    def test_another_owner_or_a_hand_written_line_is_still_refused(self):
        self.keys.authorize(self.mine, "ipad")
        with self.assertRaises(SshKeyError) as caught:
            self.keys.replace(self.mine, "ios-new", take_from="ios-old")
        self.assertEqual(caught.exception.code, "public_key_owned_by_other_device")
        hand = key_line(9, "alex@mac")
        self.keys.path.write_text(self.keys.path.read_text() + hand + "\n")
        with self.assertRaises(SshKeyError) as caught:
            self.keys.replace(hand, "ios-new", take_from="ios-old")
        self.assertEqual(caught.exception.code, "public_key_not_owned")
        self.assertIsNone(self.keys.owner_of(hand))


class RegistryFaultTests(unittest.TestCase):
    def test_an_unreadable_registry_is_not_a_refusal_of_the_credential(self):
        with tempfile.TemporaryDirectory() as root:
            authority = DeviceAuthenticator.from_file(Path(root) / "device.secret")
            token = authority.issue("ios-phone").token
            registry = Path(root) / "device.credentials.json"
            registry.write_text("{not json")
            with self.assertRaises(CredentialStoreUnavailable):
                authority.verify(token)


class SupersedeRoutingTests(unittest.IsolatedAsyncioTestCase):
    """End to end over the real boundary: one phone, two ids, one ending.

    The harness is `MediaRoutingTests`'s, borrowed rather than inherited so
    its own tests do not run twice.
    """
    request = routing.MediaRoutingTests.request
    companion = routing.MediaRoutingTests.companion
    claim = routing.MediaRoutingTests.claim

    async def asyncSetUp(self):
        await routing.MediaRoutingTests.asyncSetUp(self)
        self.home = tempfile.TemporaryDirectory()
        self.service.ssh_keys = AuthorizedKeys(Path(self.home.name))
        self.service.ssh_keys.path.parent.mkdir(mode=0o700, parents=True)
        self.phone_key = key_line(7)
        # The borrowed harness already put one pending request on the fork
        # for its own fingerprint; the phone here has a certificate of its own.
        self.fp = 'e' * 64

    async def asyncTearDown(self):
        await routing.MediaRoutingTests.asyncTearDown(self)
        self.home.cleanup()

    async def pair_phone(self, device):
        code, row = await self.request('POST', '/v1/pairing/requests', {
            'device_id': device, 'device_name': 'iPhone', 'ssh_public_key': self.phone_key})
        self.assertEqual(code, 200, row)
        approved = await self.local.request('local.pair.approve', request_id=row['request_id'])
        self.assertTrue(approved['ok'], approved)
        return await self.claim(row), approved['result']

    async def stream_pair(self, token):
        rid = self.fork.add(self.fp)
        code, found = await self.request('POST', '/v1/media/pairing/discover',
                                         {'client_cert_sha256': self.fp, 'pairing_intent': True}, token)
        self.assertEqual(code, 200, found)
        code, attempt = await self.request('POST', '/v1/media/pairing/requests', {
            'request_id': rid, 'client_cert_sha256': self.fp, 'pin': '7391'}, token)
        self.assertEqual(code, 200, attempt)
        self.fork.rows[rid]['status'] = 'paired'
        code, final = await self.request('GET', '/v1/media/pairing/requests/' + attempt['attempt_id'], token=token)
        self.assertEqual(code, 200, final)
        return final

    async def test_leos_phone_pairs_again_and_keeps_remote_and_the_terminal(self):
        self.fork.revocation = True
        old, first = await self.pair_phone('ios-4c37679b')
        self.assertTrue(first['ssh']['authorized'])
        self.assertTrue((await self.stream_pair(old))['paired'])
        with self.service.pairing.transaction() as state:  # paired twelve days earlier
            for row in state['requests'].values():
                row['claimed_at'] -= 12 * 86400
        # The phone comes back under a new id with the same Keychain: same
        # SSH key, same Moonlight certificate.
        new, second = await self.pair_phone('ios-47b40305')
        self.assertTrue(second['remote']['media_authorized'])
        self.assertFalse(second['ssh']['authorized'])
        self.assertEqual(second['ssh']['error'], 'public_key_owned_by_other_device')
        # Before any proof: two rows, and the older one says what it is.
        listing = await self.local.request('local.devices.list')
        rows = {row['device_id']: row for row in listing['result']['devices']}
        self.assertEqual(rows['ios-4c37679b'].get('older_pairing_of'), 'ios-47b40305')
        self.assertNotIn('older_pairing_of', rows['ios-47b40305'])
        # Remote: the start refused, the app renews, the fork verifies - done.
        final = await self.stream_pair(new)
        self.assertTrue(final['paired'])
        self.assertTrue(self.bridge.media_authorized('ios-47b40305', self.fp))
        self.assertIn(self.fp, self.fork.certs)
        # The terminal came along with the certificate's proof.
        self.assertEqual([(row['device'], row['fingerprint']) for row in self.service.ssh_keys.listing()],
                         [('ios-47b40305', self.service.ssh_keys.listing()[0]['fingerprint'])])
        with self.service.pairing.transaction() as state:
            grants = [row['grants'] for row in state['requests'].values() if row['device_id'] == 'ios-47b40305']
        self.assertEqual(grants, [{'companion': True, 'media': True, 'ssh': True}])
        # And the older identity is gone rather than left as a second "iPhone".
        self.assertEqual((await self.request('GET', '/v1/state', token=old))[0], 401)
        self.assertEqual((await self.request('GET', '/v1/state', token=new))[0], 200)
        listing = await self.local.request('local.devices.list', all=True)
        self.assertNotIn('ios-4c37679b', {row['device_id'] for row in listing['result']['devices']})

    async def test_a_different_key_is_not_carried_and_the_old_line_goes(self):
        self.fork.revocation = True
        old, _ = await self.pair_phone('ios-old')
        await self.stream_pair(old)
        self.phone_key = key_line(8)  # the new identity offers another key
        new, second = await self.pair_phone('ios-new')
        self.assertTrue(second['ssh']['authorized'])
        await self.stream_pair(new)
        self.assertEqual([row['device'] for row in self.service.ssh_keys.listing()], ['ios-new'])

    async def test_the_local_ssh_approve_moves_the_key_and_names_its_holder_first(self):
        old, _ = await self.pair_phone('ios-old')
        new, _ = await self.pair_phone('ios-new')
        code, refused = await self.request('PUT', '/v1/ssh/key', {'public_key': self.phone_key}, new)
        self.assertEqual(code, 409, refused)
        self.assertEqual(refused['error']['code'], 'ssh_approval_required')
        pending = (await self.local.request('local.ssh.pending'))['result']['requests']
        self.assertEqual([(row['device_id'], row['held_by'], row['held_by_name']) for row in pending],
                         [('ios-new', 'ios-old', 'iPhone')])
        decided = await self.local.request('local.ssh.approve', device_id='ios-new')
        self.assertTrue(decided['ok'], decided)
        self.assertEqual(decided['result']['moved_from'], 'ios-old')
        self.assertEqual([row['device'] for row in self.service.ssh_keys.listing()], ['ios-new'])
        # Revoking the older id no longer silently takes the terminal away.
        await self.local.request('local.devices.revoke', device_id='ios-old')
        self.assertEqual([row['device'] for row in self.service.ssh_keys.listing()], ['ios-new'])

    async def test_a_registry_fault_is_503_not_401(self):
        token, _, _ = await self.companion()
        with mock.patch.object(self.authority, '_reload_locked', side_effect=OSError('busy')):
            code, body = await self.request('GET', '/v1/state', token=token)
            self.assertEqual(code, 503, body)
            self.assertEqual(body['error']['code'], 'auth_unavailable')
            self.assertNotIn('reason', body['error'])
            code, body = await self.request('GET', '/v1/pairing/credential', token=token)
            self.assertEqual(code, 503, body)
        self.assertEqual((await self.request('GET', '/v1/state', token=token))[0], 200)


if __name__ == "__main__":
    unittest.main()
