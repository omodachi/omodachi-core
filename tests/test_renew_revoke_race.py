"""SEC-RENEW-1: a renewal never outlives a revoke that raced it.

`renew` decides "may this credential be traded in" and mints the new one. If
another actor can take the registry lock between the two, a revoke landing
there is never seen by the mint. These tests put the revoke at every point the
renewal lets go of the registry before it has minted - wherever that is in the
code, so they do not depend on how `renew` is written - and require the same
answer at each: refused as revoked, and nothing new in the registry.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from omodachi_core.auth import CredentialError, CredentialStoreUnavailable, DeviceAuthenticator

DAY = 86400
T0 = 1_790_000_000
RENEW_AT = T0 + 25 * DAY  # inside the last week, so the renewal is due


class RenewRevokeRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.round = 0

    def scenario(self):
        """A fresh host with one paired device, its registry on disk."""
        self.round += 1
        secret = Path(self.temp.name) / str(self.round) / "device.secret"
        renewer = DeviceAuthenticator.from_file(secret)
        token = renewer.issue("ipad", now=T0, device_name="iPad").token
        return secret, renewer, token

    @staticmethod
    def issued(secret):
        registry = json.loads(secret.with_suffix(".credentials.json").read_text())
        return {digest for digest, owner in registry["issued"].items() if owner == "ipad"}, set(registry["revoked"])

    def race(self, revoke, *, cross_process):
        """Run `revoke` just before the renewal's n-th registry transaction, for every n.

        Before a transaction the renewal holds no lock, so that is exactly
        where a concurrent `Revoke` (or another process) gets in. The loop
        ends at the first n the renewal never reaches.
        """
        outcomes = []
        n = 1
        while True:
            secret, renewer, token = self.scenario()
            before, _ = self.issued(secret)
            revoker = DeviceAuthenticator.from_file(secret) if cross_process else renewer
            original = renewer._transaction
            entries = []

            def transaction():
                entries.append(None)
                if len(entries) == n:
                    renewer._transaction = original  # the revoker may be this same object
                    revoke(revoker, token)
                return original()

            renewer._transaction = transaction
            try:
                renewer.renew(token, RENEW_AT)
            except CredentialError as error:
                reason = error.reason
            else:
                reason = None
            finally:
                renewer._transaction = original
            if len(entries) < n:
                break
            after, revoked = self.issued(secret)
            outcomes.append(n)
            with self.subTest(transaction=n):
                self.assertEqual(reason, "credential_revoked",
                                 "a renewal minted after a revoke it should have seen")
                self.assertEqual(after, before, "a refused renewal left a new hash in the registry")
                # And nothing the device holds works any more.
                self.assertIn(next(iter(before)), revoked)
            n += 1
        self.assertTrue(outcomes, "the renewal never touched the registry")

    def test_revoke_device_between_check_and_mint(self):
        self.race(lambda auth, token: auth.revoke_device("ipad"), cross_process=False)

    def test_revoke_token_between_check_and_mint(self):
        self.race(lambda auth, token: auth.revoke(token), cross_process=False)

    def test_revoke_device_from_a_second_registry_instance(self):
        self.race(lambda auth, token: auth.revoke_device("ipad"), cross_process=True)

    def test_revoke_token_from_a_second_registry_instance(self):
        self.race(lambda auth, token: auth.revoke(token), cross_process=True)

    def test_a_renewal_that_wins_the_race_is_revoked_with_the_device(self):
        """The other order: renewal first, then the revoke takes the new one too."""
        secret, renewer, token = self.scenario()
        fresh = renewer.renew(token, RENEW_AT)
        DeviceAuthenticator.from_file(secret).revoke_device("ipad")
        for held in (token, fresh.token):
            with self.assertRaises(CredentialError) as refused:
                renewer.verify(held, RENEW_AT + 1)
            self.assertEqual(refused.exception.reason, "credential_revoked")

    def test_an_unreadable_registry_is_still_the_hosts_problem_not_a_refusal(self):
        """Folding the check into the mint keeps `verify`'s 503-not-401 answer."""
        secret, renewer, token = self.scenario()
        secret.with_suffix(".credentials.json").write_text("{")
        with self.assertRaises(CredentialStoreUnavailable):
            renewer.renew(token, RENEW_AT)


if __name__ == "__main__":
    unittest.main()
