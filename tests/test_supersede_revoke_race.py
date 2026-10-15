"""SEC-RENEW-2: a re-pair's SSH key move, and every other grant, never outlives a revoke.

REMOTE-PAIR-1 moves the phone's `authorized_keys` line from its older identity
to the newer one once the Moonlight certificate proved they are the same phone
(`CoreService.media_rebound` -> `_supersede_device`). That callback runs on a
device request's worker thread, outside the `_local_lock` every `devices
revoke` takes, so a revoke can land between its checks and its write.

The tests here put the other side at every point where the side under test
holds none of the state locks - the only places another thread or another
core process on the same files can get in - and then ask what the revoked
device is left with. They hook lock *entries*, not functions of the code under
test, so they do not depend on how the writer is arranged.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest

import aiohttp

from omodachi_core.auth import DeviceAuthenticator
from omodachi_core.biometric import ApprovalBroker, BiometricKeyStore, enrollment_message
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.media_pairing import MediaPairingBridge, MediaPairingStore
from omodachi_core.network import NetworkServer
from omodachi_core.pairing import PairingStore
from omodachi_core.service import ServiceError
from omodachi_core.ssh_keys import AuthorizedKeys
from tests.test_biometric import keypair, sign
from tests.test_media_pairing import FakeFork

OLD, NEW = "ios-old", "ios-new"


def key_line(fill, comment="omodachi-ios"):
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([fill]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode() + " " + comment


KEY = key_line(7)


class _Host:
    """One core process: its own objects over the shared files under `base`."""

    def __init__(self, base: Path, fork: FakeFork):
        self.base = base
        self.auth = DeviceAuthenticator.from_file(base / "device.secret")
        self.store = PairingStore(self.auth, base / "pairing.json")
        self.bridge = MediaPairingBridge(fork, MediaPairingStore(base / "media" / "state.json"))
        self.service = create_service(Hub(authenticator=self.auth), demo=True, media_pairing=self.bridge)
        self.service.pairing = self.store
        self.service.ssh_keys = AuthorizedKeys(base / "home")

    def revoke(self, device):
        return self.service._dispatch_local("local.devices.revoke", {"device_id": device}, lambda: True)

    def local(self, operation, **payload):
        return self.service._dispatch_local(operation, payload, lambda: True)


class _Interleaving:
    """Run `other()` just before the n-th time `target` enters one of the state
    locks while holding none of them. `target` is whatever the test calls next."""

    def __init__(self, host: _Host, n: int, other):
        self.host, self.n, self.other = host, n, other
        self.depth, self.entries, self.injecting, self.fired = 0, 0, False, False
        self.patched = []

    def _wrap(self, owner, name):
        original = getattr(owner, name)

        @contextmanager
        def entered(*args, **kwargs):
            if self.depth == 0 and not self.injecting:
                self.entries += 1
                if self.entries == self.n:
                    self.injecting = True
                    try:
                        self.other()
                    finally:
                        self.injecting = False
                        self.fired = True
            self.depth += 1
            try:
                with original(*args, **kwargs) as value:
                    yield value
            finally:
                self.depth -= 1

        setattr(owner, name, entered)
        self.patched.append((owner, name))

    def __enter__(self):
        host = self.host
        for owner, name in ((host.service.ssh_keys, "_locked"), (host.store, "transaction"),
                            (host.auth, "_transaction"), (host.bridge.store, "transaction")):
            self._wrap(owner, name)
        return self

    def __exit__(self, *exc):
        for owner, name in self.patched:
            delattr(owner, name)
        return False


class _Steps:
    """Run `other()` at the n-th boundary between the revoke's own steps."""

    def __init__(self, host: _Host, n: int, other):
        self.host, self.n, self.other = host, n, other
        self.boundaries, self.busy, self.fired = 0, False, False
        self.patched = []

    def _boundary(self):
        if self.busy:
            return
        self.boundaries += 1
        if self.boundaries == self.n:
            self.busy = True
            try:
                self.other()
            finally:
                self.busy = False
                self.fired = True

    def __enter__(self):
        host = self.host
        steps = ((host.store, "forget_device"), (host.bridge, "revoke_device"),
                 (host.auth, "revoke_device"), (host.service.ssh_keys, "revoke"))
        for owner, name in steps:
            original = getattr(type(owner), name).__get__(owner)

            def call(*args, _original=original, **kwargs):
                self._boundary()
                try:
                    return _original(*args, **kwargs)
                finally:
                    self._boundary()

            setattr(owner, name, call)
            self.patched.append((owner, name))
        return self

    def __exit__(self, *exc):
        for owner, name in self.patched:
            delattr(owner, name)
        return False


class _Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.rounds = 0

    async def asyncTearDown(self):
        self.temp.cleanup()

    def hosts(self):
        """A fresh pair of core processes on one fresh set of state files."""
        self.rounds += 1
        base = Path(self.temp.name) / str(self.rounds)
        (base / "home" / ".ssh").mkdir(parents=True, mode=0o700)
        fork = FakeFork(revocation=True)
        return _Host(base, fork), _Host(base, fork)

    @staticmethod
    def pair(host, device, *, key=KEY, remote=False, claim=True):
        row = host.store.request(host.store.begin()["invitation"], device, "iPhone", ssh_public_key=key)
        host.local("local.pair.approve", request_id=row["request_id"], remote=remote)
        if claim:
            row["credential"] = host.store.claim(row["request_id"], row["request_secret"])["credential"]
        return row

    @staticmethod
    def owners(host):
        return [row["device"] for row in AuthorizedKeys(host.base / "home").listing()]

    @staticmethod
    def owner_of_key(host):
        return AuthorizedKeys(host.base / "home").owner_of(KEY)

    @staticmethod
    def revoked(host, device):
        """Revoked, or revoked and already purged: no credential of it works."""
        rows = [row for row in DeviceAuthenticator.from_file(host.base / "device.secret").list_devices()
                if row["device_id"] == device]
        return not rows or any(row["revoked_credentials"] for row in rows)

    def keeps_nothing(self, host, device):
        self.assertNotIn(device, self.owners(host), "a revoked device kept an authorized_keys line")
        self.assertIsNone(PairingStore(host.auth, host.base / "pairing.json").grants_for(device),
                          "a revoked device kept an approval (and its SSH grant)")


class SupersedeRaceTests(_Case):
    """The reported bug: `_supersede_device` against a `devices revoke`."""

    def phone(self, *, offered="approval"):
        """One phone, paired under OLD with KEY, then again under NEW with the same key."""
        one, two = self.hosts()
        self.pair(one, OLD)
        self.assertEqual(self.owners(one), [OLD])
        self.pair(one, NEW)  # the same key: its own Approve could not land it
        self.assertEqual(self.owners(one), [OLD])
        if offered == "pending":
            # The key offered again through `PUT /v1/ssh/key` and waiting for
            # `ssh approve`, instead of only on the approval.
            with self.assertRaises(ServiceError):
                one.service.ssh_key(NEW, {"public_key": KEY})
            self.assertIn(NEW, one.service._ssh_pending)
        return one, two

    def rebound(self, host):
        return host.service.media_rebound({"from_device_id": OLD, "device_id": NEW})

    def _race_supersede(self, victim, *, cross_process, offered="approval"):
        """The revoke of `victim` lands before each lock the supersede takes."""
        n, seen = 1, 0
        while True:
            one, two = self.phone(offered=offered)
            revoker = two if cross_process else one
            at_injection = {}

            def other():
                at_injection["owner"] = self.owner_of_key(one)
                revoker.revoke(victim)

            with _Interleaving(one, n, other) as race:
                self.rebound(one)
            if not race.fired:
                break
            seen += 1
            with self.subTest(lock_entry=n, revoked=victim, cross_process=cross_process, offered=offered):
                self.keeps_nothing(one, victim)
                self.keeps_nothing(one, OLD)  # the supersede retires it either way
                if victim == NEW or at_injection["owner"] == OLD:
                    # The line was still OLD's when the revoke ran: nobody may
                    # end up holding the key the revoke was taking back.
                    self.assertEqual(self.owners(one), [],
                                     "the key outlived a revoke that ran while it was still the old line")
                else:
                    # Moved before the revoke began: the move itself still works.
                    self.assertEqual(self.owners(one), [NEW])
                    self.assertTrue(one.store.grants_for(NEW)["ssh"])
            n += 1
        self.assertGreater(seen, 3, "the supersede took fewer locks than expected")

    def test_a_revoke_of_the_new_identity_between_the_check_and_the_move(self):
        self._race_supersede(NEW, cross_process=False)

    def test_a_revoke_of_the_new_identity_from_a_second_core_process(self):
        self._race_supersede(NEW, cross_process=True)

    def test_a_revoke_of_the_new_identity_whose_key_waits_for_ssh_approve(self):
        # Another process's revoke cannot clear this process's in-memory
        # offer, so the supersede still finds the key to move.
        self._race_supersede(NEW, cross_process=True, offered="pending")

    def test_a_revoke_of_the_old_identity_between_the_check_and_the_move(self):
        self._race_supersede(OLD, cross_process=False)

    def test_a_revoke_of_the_old_identity_from_a_second_core_process(self):
        self._race_supersede(OLD, cross_process=True)

    def _race_revoke(self, victim):
        """The other order: the whole supersede lands between the revoke's own steps."""
        k, seen = 1, 0
        while True:
            one, two = self.phone()
            before = {}

            def other():
                before["owner"] = self.owner_of_key(one)
                before["revoked"] = self.revoked(one, victim)
                self.rebound(one)

            with _Steps(two, k, other) as steps:
                two.revoke(victim)
            if not steps.fired:
                break
            seen += 1
            with self.subTest(boundary=k, revoked=victim):
                self.keeps_nothing(one, victim)
                if victim == NEW or before["owner"] != OLD or before["revoked"]:
                    # OLD's credentials were already taken (or its line):
                    # its line is going, and a re-pairing does not keep it.
                    self.assertEqual(self.owners(one), [])
                else:
                    self.assertEqual(self.owners(one), [NEW])
            k += 1
        self.assertGreater(seen, 3)

    def test_a_supersede_inside_a_revoke_of_either_identity(self):
        # NEW: the old code already got this order right (its revoke removes
        # the moved line last); it stays as the guard for the new rule.
        # OLD: a move landing after OLD's credentials were taken kept the key.
        for victim in (NEW, OLD):
            self._race_revoke(victim)


class LocalApproveRaceTests(_Case):
    """Writers a local Approve drives, against a revoke from another core process.

    In one daemon these are serialized with `devices revoke` by `_local_lock`;
    the revoke that is not is another process's (or a supersede's retire).
    """

    def test_a_revoke_between_approve_and_its_grants_leaves_no_line_and_no_remote(self):
        one, two = self.hosts()
        row = one.store.request(one.store.begin()["invitation"], "ipad", "iPad", ssh_public_key=KEY)
        decide = one.store.decide

        def decided(*args, **kwargs):
            result = decide(*args, **kwargs)
            two.revoke("ipad")  # the approval is on disk; nothing has been granted from it yet
            return result

        one.store.decide = decided
        try:
            with self.assertRaises(ServiceError):
                one.local("local.pair.approve", request_id=row["request_id"])  # Approve + Remote
        finally:
            del one.store.decide
        self.keeps_nothing(one, "ipad")
        self.assertNotIn("ipad", one.bridge.authorized_devices(), "a revoked device was left allowed Remote")
        with self.assertRaises(ServiceError):
            one.store.claim(row["request_id"], row["request_secret"])

    def test_a_revoke_between_grant_remotes_check_and_its_write(self):
        one, two = self.hosts()
        row = self.pair(one, "ipad", remote=False)
        self.assertNotIn("ipad", one.bridge.authorized_devices())
        grant = one.bridge.grant_remote

        def granting(*args, **kwargs):
            two.revoke("ipad")  # the route's own approval check has already passed
            return grant(*args, **kwargs)

        one.bridge.grant_remote = granting
        try:
            with self.assertRaises(ServiceError):
                one.local("local.media-pairing.grant-remote", device_id="ipad",
                          source_request_id=row["request_id"])
        finally:
            del one.bridge.grant_remote
        self.assertNotIn("ipad", one.bridge.authorized_devices(), "a revoked device was left allowed Remote")

    def test_a_grant_remote_inside_a_revoke_never_leaves_the_device_allowed(self):
        k, seen = 1, 0
        while True:
            one, two = self.hosts()
            row = self.pair(one, f"ipad-{k}", remote=False)

            def other():
                try:
                    one.local("local.media-pairing.grant-remote", device_id=f"ipad-{k}",
                              source_request_id=row["request_id"])
                except ServiceError:
                    pass

            with _Steps(two, k, other) as steps:
                two.revoke(f"ipad-{k}")
            if not steps.fired:
                break
            seen += 1
            with self.subTest(boundary=k):
                self.assertNotIn(f"ipad-{k}", one.bridge.authorized_devices(),
                                 "a grant made between the revoke's steps outlived it")
            k += 1
        self.assertGreater(seen, 3)


class BiometricEnrollRaceTests(_Case):
    """`POST /v1/auth/keys`: `body()` re-verified the credential; the revoke lands after it."""

    async def test_a_revoke_between_the_routes_check_and_the_enrolment_leaves_no_key(self):
        one, _ = self.hosts()
        token = self.pair(one, "ipad")["credential"]
        keys = BiometricKeyStore(one.base / "biometric-keys.json")
        one.service.biometric = ApprovalBroker(one.service.hub, keys, journal=lambda entry: None,
                                               pam_index=one.base / "enrolled.json")
        server = NetworkServer(one.service, allow_loopback_http=True)
        await server.start()
        client = aiohttp.ClientSession()
        try:
            url = f"http://127.0.0.1:{server.bound_port}/v1/auth/keys"
            headers = {"Authorization": "Bearer " + token}
            async with client.get(url, headers=headers) as response:
                self.assertEqual(response.status, 200)
                challenge = (await response.json())["challenge"]
            private, public = keypair()
            encoded = base64.b64encode(public).decode()
            message = enrollment_message(host_id=one.service.biometric.host_id, device_id="ipad",
                                         challenge=challenge, public_key_b64=encoded)
            enroll = keys.enroll

            def enrolling(*args, **kwargs):
                one.revoke("ipad")  # `devices revoke`, on its worker thread in the daemon
                return enroll(*args, **kwargs)

            keys.enroll = enrolling
            async with client.post(url, headers=headers, json={
                    "public_key": encoded, "label": "iPad", "challenge": challenge, "enabled": True,
                    "secure_enclave": True,
                    "signature": base64.b64encode(sign(private, message)).decode()}) as response:
                status = response.status
                body = await response.json()
        finally:
            await client.close()
            await server.close()
        self.assertEqual(status, 401, body)
        self.assertEqual(BiometricKeyStore(one.base / "biometric-keys.json").list(), [],
                         "a revoked device kept an approval key")


if __name__ == "__main__":
    unittest.main()
