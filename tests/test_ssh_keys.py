"""The owned authorized_keys lines: idempotent, exact, and never anyone else's.

Every path here is a temporary directory and every key is a synthetic base64
body. No real key, no real `~/.ssh` and no ssh process is involved.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import tempfile
import unittest

from omodachi_core.cli import host_main
from omodachi_core.ssh_keys import AuthorizedKeys, MARKER, SshKeyError, duplicate_devices, fingerprint


def _ed25519(seed: int, comment: str) -> str:
    """A well-formed, entirely synthetic ed25519 public key line."""
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([seed]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode() + " " + comment


IPAD = _ed25519(1, "alex@ipad")
PHONE = _ed25519(2, "alex@phone")
USER_KEY = "ssh-rsa " + base64.b64encode(b"\x00\x00\x00\x07ssh-rsa" + b"\xab" * 140).decode() + " alex@mac"


class AuthorizedKeysTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.keys = AuthorizedKeys(self.home)

    def body(self):
        return self.keys.path.read_text()

    def seed(self, text):
        self.keys.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.keys.path.write_text(text)

    def test_authorize_is_idempotent_and_writes_one_marked_line(self):
        first = self.keys.authorize(IPAD, "omodachi-ipad")
        self.assertEqual((first["changed"], first["reason"]), (True, "added"))
        self.assertTrue(first["fingerprint"].startswith("SHA256:"))
        self.assertEqual(self.body(),
                         "ssh-ed25519 " + IPAD.split()[1] + " " + MARKER + "omodachi-ipad\n")
        again = self.keys.authorize(IPAD, "omodachi-ipad")
        self.assertEqual((again["changed"], again["reason"]), (False, "already_authorized"))
        self.assertEqual(self.body().count("ssh-ed25519"), 1)
        self.assertEqual(self.keys.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.keys.path.parent.stat().st_mode & 0o777, 0o700)

    def test_revoke_removes_exactly_one_devices_lines(self):
        self.seed(USER_KEY + "\n# a comment of my own\n")
        self.keys.authorize(IPAD, "omodachi-ipad")
        self.keys.authorize(PHONE, "omodachi-phone")
        self.assertEqual([row["device"] for row in self.keys.listing()],
                         ["omodachi-ipad", "omodachi-phone"])
        result = self.keys.revoke("omodachi-ipad")
        self.assertEqual((result["revoked"], result["removed"]), (True, 1))
        self.assertEqual([row["device"] for row in self.keys.listing()], ["omodachi-phone"])
        # Everything that was not ours is still there, in order, untouched.
        self.assertEqual(self.body().splitlines()[:2], [USER_KEY, "# a comment of my own"])
        # Revoking a device with nothing here is not an error.
        self.assertEqual(self.keys.revoke("omodachi-ipad")["removed"], 0)

    def test_a_key_the_user_put_there_is_never_adopted_or_duplicated(self):
        self.seed(USER_KEY + "\n")
        with self.assertRaises(SshKeyError) as caught:
            self.keys.authorize(USER_KEY, "omodachi-ipad")
        self.assertEqual(caught.exception.code, "public_key_not_owned")
        self.assertEqual(self.body(), USER_KEY + "\n")
        # And the same key cannot be handed to a second device either, which
        # would make a revoke look successful while the key still works.
        self.keys.authorize(IPAD, "omodachi-ipad")
        with self.assertRaises(SshKeyError) as caught:
            self.keys.authorize(IPAD, "omodachi-phone")
        self.assertEqual(caught.exception.code, "public_key_owned_by_other_device")
        self.assertEqual(self.body().count(MARKER), 1)

    def test_a_marker_inside_a_commented_out_line_owns_nothing(self):
        self.seed("#ssh-ed25519 " + IPAD.split()[1] + " " + MARKER + "omodachi-ipad\n")
        self.assertEqual(self.keys.listing(), [])
        self.assertEqual(self.keys.revoke("omodachi-ipad")["removed"], 0)

    def test_input_that_is_not_one_openssh_public_key_is_refused(self):
        for value in ("", "not-a-key", "ssh-dss AAAAB3NzaC1kc3MAAACBAP" + "D" * 40,
                      "ssh-ed25519", "ssh-ed25519 short", "ssh-ed25519 AAA;rm -rf /",
                      IPAD + "\nssh-ed25519 " + PHONE.split()[1], 42, None):
            with self.subTest(value=value):
                with self.assertRaises(SshKeyError):
                    self.keys.authorize(value, "omodachi-ipad")
        for device in ("", "has space", "../escape", "a" * 129, "#comment", 7, None):
            with self.subTest(device=device):
                with self.assertRaises(SshKeyError):
                    self.keys.authorize(IPAD, device)
                with self.assertRaises(SshKeyError):
                    self.keys.revoke(device)
        self.assertFalse(self.keys.path.exists())

    def test_a_symlinked_authorized_keys_is_refused_rather_than_followed(self):
        target = self.home / "elsewhere"
        target.write_text(USER_KEY + "\n")
        self.keys.path.parent.mkdir(mode=0o700, parents=True)
        self.keys.path.symlink_to(target)
        with self.assertRaises(SshKeyError) as caught:
            self.keys.authorize(IPAD, "omodachi-ipad")
        self.assertEqual(caught.exception.code, "authorized_keys_unsafe")
        self.assertEqual(target.read_text(), USER_KEY + "\n")


class KeyDriftTests(unittest.TestCase):
    """UX-4. A device that comes back with a different key than it left with.

    The real shape, from Leo's host: two `authorized_keys` lines the host wrote
    when the iPad paired, and an iPad now offering a third key that appears in
    neither. `replace` is what turns that into one line again without anybody
    pairing, revoking or editing the file by hand.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.keys = AuthorizedKeys(self.home)

    def body(self):
        return self.keys.path.read_text()

    def test_a_device_that_comes_back_with_another_key_ends_up_with_one_line(self):
        self.keys.authorize(IPAD, "omodachi-ipad")
        drifted = _ed25519(9, "alex@ipad-reinstalled")
        result = self.keys.replace(drifted, "omodachi-ipad")
        self.assertEqual((result["reason"], result["changed"], result["removed"]),
                         ("replaced", True, 1))
        rows = self.keys.listing()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fingerprint"], result["fingerprint"])
        # And the key that stopped working is named, not merely gone.
        self.assertEqual(result["replaced"],
                         [fingerprint(IPAD.split()[1])])
        self.assertNotIn(IPAD.split()[1], self.body())

    def test_replacing_with_the_key_already_held_writes_nothing(self):
        self.keys.authorize(IPAD, "omodachi-ipad")
        before = self.keys.path.stat().st_mtime_ns
        result = self.keys.replace(IPAD, "omodachi-ipad")
        self.assertEqual((result["reason"], result["changed"], result["removed"], result["replaced"]),
                         ("already_authorized", False, 0, []))
        self.assertEqual(self.keys.path.stat().st_mtime_ns, before)

    def test_a_device_with_no_line_yet_is_simply_added(self):
        result = self.keys.replace(IPAD, "omodachi-ipad")
        self.assertEqual((result["reason"], result["removed"]), ("added", 0))
        self.assertEqual([row["device"] for row in self.keys.listing()], ["omodachi-ipad"])

    def test_replace_never_touches_another_device_or_the_users_own_lines(self):
        self.keys.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.keys.path.write_text(USER_KEY + "\n# a comment of my own\n")
        self.keys.authorize(IPAD, "omodachi-ipad")
        self.keys.authorize(PHONE, "omodachi-phone")
        self.keys.replace(_ed25519(9, "alex@ipad-reinstalled"), "omodachi-ipad")
        self.assertEqual(self.body().splitlines()[:2], [USER_KEY, "# a comment of my own"])
        self.assertEqual(sorted(row["device"] for row in self.keys.listing()),
                         ["omodachi-ipad", "omodachi-phone"])
        self.assertIn(PHONE.split()[1], self.body())
        # A key the user put there by hand is still never adopted.
        with self.assertRaises(SshKeyError) as caught:
            self.keys.replace(USER_KEY, "omodachi-ipad")
        self.assertEqual(caught.exception.code, "public_key_not_owned")
        with self.assertRaises(SshKeyError) as caught:
            self.keys.replace(PHONE, "omodachi-ipad")
        self.assertEqual(caught.exception.code, "public_key_owned_by_other_device")

    def test_prune_keeps_the_newest_line_and_leaves_one_key_devices_alone(self):
        # Two devices with one key each is exactly what Leo's host holds, and
        # it must come through a prune untouched.
        self.keys.authorize(IPAD, "ios-cd64141c")
        self.keys.authorize(PHONE, "ios-9632b99b")
        self.assertEqual(self.keys.prune(), {"pruned": True, "removed": 0, "devices": {}})
        self.assertEqual(len(self.keys.listing()), 2)
        # Now give one of them a second and a third line, the way repeated
        # pairings did before `replace` existed.
        older, newest = _ed25519(7, "a"), _ed25519(8, "b")
        self.keys.authorize(older, "ios-cd64141c")
        self.keys.authorize(newest, "ios-cd64141c")
        self.assertEqual(duplicate_devices(self.keys.listing()),
                         {"ios-cd64141c": [fingerprint(IPAD.split()[1]),
                                           fingerprint(older.split()[1]),
                                           fingerprint(newest.split()[1])]})
        result = self.keys.prune()
        self.assertEqual(result["removed"], 2)
        self.assertEqual(result["devices"]["ios-cd64141c"]["kept"], fingerprint(newest.split()[1]))
        self.assertEqual(sorted(row["device"] for row in self.keys.listing()),
                         ["ios-9632b99b", "ios-cd64141c"])
        self.assertEqual(duplicate_devices(self.keys.listing()), {})

    def test_prune_can_be_limited_to_one_device(self):
        for seed in (1, 7, 8):
            self.keys.authorize(_ed25519(seed, "x"), "ios-cd64141c")
        for seed in (2, 5):
            self.keys.authorize(_ed25519(seed, "y"), "ios-9632b99b")
        result = self.keys.prune("ios-9632b99b")
        self.assertEqual((result["removed"], list(result["devices"])), (1, ["ios-9632b99b"]))
        self.assertEqual(len(duplicate_devices(self.keys.listing())["ios-cd64141c"]), 3)


class SshCommandTests(unittest.TestCase):
    """`omodachi-host ssh ...` is a local admin command: no token, no daemon."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def run_command(self, *argv):
        import contextlib
        import io
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = host_main([*argv, "--home", str(self.home)])
        return code, json.loads(stream.getvalue())

    def test_authorize_list_and_revoke_round_trip_without_a_credential(self):
        code, result = self.run_command("ssh", "authorize", IPAD, "--device", "omodachi-ipad")
        self.assertEqual((code, result["ok"], result["result"]["changed"]), (0, True, True))
        code, result = self.run_command("ssh", "list")
        self.assertEqual([row["device"] for row in result["result"]["keys"]], ["omodachi-ipad"])
        code, result = self.run_command("ssh", "revoke", "--device", "omodachi-ipad")
        self.assertEqual((code, result["result"]["removed"]), (0, 1))
        code, result = self.run_command("ssh", "list")
        self.assertEqual(result["result"]["keys"], [])

    def test_list_names_the_devices_holding_more_than_one_key(self):
        for seed, device in ((1, "ios-cd64141c"), (2, "ios-9632b99b"), (3, "ios-cd64141c")):
            self.run_command("ssh", "authorize", _ed25519(seed, "x"), "--device", device)
        _, result = self.run_command("ssh", "list")
        self.assertEqual(list(result["result"]["duplicates"]), ["ios-cd64141c"])
        code, result = self.run_command("ssh", "list", "--prune")
        self.assertEqual((code, result["result"]["pruned"]["removed"]), (0, 1))
        self.assertEqual(result["result"]["duplicates"], {})
        # The device that only ever had one line still has it.
        self.assertEqual(sorted(row["device"] for row in result["result"]["keys"]),
                         ["ios-9632b99b", "ios-cd64141c"])

    def test_a_refused_key_is_a_named_code_and_a_nonzero_exit(self):
        code, result = self.run_command("ssh", "authorize", "not-a-key", "--device", "omodachi-ipad")
        self.assertEqual((code, result["ok"], result["error"]), (1, False, "invalid_public_key"))


if __name__ == "__main__":
    unittest.main()
