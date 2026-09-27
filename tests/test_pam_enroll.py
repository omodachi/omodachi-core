"""RELEASE-9 B1: the root-owned key store, and the helper that trusts only it."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest

from omodachi_core import pam_enroll, pam_helper, pam_install
from omodachi_core.biometric import approval_message
from omodachi_core.pam_enroll import EnrollError, PamKeyStore, collect_request, parse_request
from omodachi_core.pam_install import PamInstaller, PamInstallError
from tests.test_biometric import keypair, sign

HOST_ID = "0123456789abcdef0123456789abcdef"
ARCH_SUDO = "#%PAM-1.0\nauth\t\tinclude\t\tsystem-auth\naccount\t\tinclude\t\tsystem-auth\n"


class KeyStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "etc/pam.d").mkdir(parents=True)
        (self.root / "etc/pam.d/sudo").write_text(ARCH_SUDO)
        self.installer = PamInstaller(self.root)
        self.installer.install(owner="alex", socket="/run/omodachi/1000/omodachid.sock", services=["sudo"])
        self.store = PamKeyStore(self.root)
        self.private, self.public = keypair()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def request(self, **overrides):
        value = {"owner": "alex", "host_id": HOST_ID,
                 "keys": [{"device_id": "ipad", "public_key": base64.b64encode(self.public).decode(),
                           "label": "Leo’s iPad", "secure_enclave": True}]}
        value.update(overrides)
        return value

    def test_the_config_points_the_helper_at_the_store_and_the_new_protocol(self):
        config = (self.root / "etc/omodachi/pam.conf").read_text()
        self.assertIn("keys=/etc/omodachi/pam/keys.json\n", config)
        self.assertIn("protocol=2\n", config)

    def test_su_can_no_longer_be_given_the_entry(self):
        with self.assertRaises(PamInstallError):
            self.installer.install(owner="alex", socket="/run/omodachi/1000/omodachid.sock", services=["su"])
        self.assertNotIn("su", pam_install.KNOWN_SERVICES)
        self.assertEqual(pam_install.DEFAULT_SERVICES, ("sudo", "polkit-1"))

    def test_enrolment_writes_a_private_store_and_a_readable_index_without_keys(self):
        result = self.store.enroll(self.request())
        self.assertEqual([row["device_id"] for row in result["enrolled"]], ["ipad"])
        self.assertEqual(stat.S_IMODE(self.store.keys_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.index_file.stat().st_mode), 0o644)
        index = json.loads(self.store.index_file.read_text())
        self.assertEqual(index["devices"]["ipad"]["fingerprint"], pam_helper.key_fingerprint(self.public))
        self.assertNotIn(base64.b64encode(self.public).decode(), self.store.index_file.read_text())
        self.assertEqual(index["devices"]["ipad"]["label"], "Leo’s iPad")

    def test_what_root_enrols_is_what_the_helper_verifies_with(self):
        self.store.enroll(self.request())
        trusted = pam_helper.TRUSTED_UID
        pam_helper.TRUSTED_UID = os.getuid()
        try:
            host_id, keys = pam_helper.load_keys(str(self.store.keys_file), "alex")
        finally:
            pam_helper.TRUSTED_UID = trusted
        self.assertEqual(host_id, HOST_ID)
        message = approval_message(host_id=HOST_ID, approval_id="appr_" + "a" * 32, nonce="n" * 43,
                                   service="sudo", user="alex", device_id="ipad")
        reply = {"ok": True, "result": {"approved": True, "approval_id": "appr_" + "a" * 32, "device_id": "ipad",
                                        "signature": base64.b64encode(sign(self.private, message)).decode()}}
        self.assertEqual(pam_helper.decide(reply, approval_id="appr_" + "a" * 32, nonce="n" * 43, host_id=HOST_ID,
                                           service="sudo", user="alex", keys=keys), "ipad")

    def test_nothing_is_enrolled_without_the_pam_entry_or_for_another_owner(self):
        with self.assertRaises(EnrollError) as caught:
            self.store.enroll(self.request(owner="mallory"))
        self.assertEqual(caught.exception.code, "pam_owner_mismatch")
        with self.assertRaises(EnrollError) as caught:
            self.store.enroll(self.request(), invoker="mallory")
        self.assertEqual(caught.exception.code, "pam_owner_mismatch")
        (self.root / "etc/omodachi/pam.conf").unlink()
        with self.assertRaises(EnrollError) as caught:
            self.store.enroll(self.request())
        self.assertEqual(caught.exception.code, "pam_not_installed")
        self.assertFalse(self.store.keys_file.exists())

    def test_every_field_from_the_owners_files_is_checked_again(self):
        good = self.request()["keys"][0]
        for bad in ({**good, "public_key": base64.b64encode(b"\x04" + b"\x01" * 64).decode()},
                    {**good, "public_key": "not base64!"},
                    {**good, "device_id": "../etc"},
                    {**good, "device_id": ""}):
            with self.subTest(bad=bad), self.assertRaises(EnrollError):
                parse_request(self.request(keys=[bad]))
        with self.assertRaises(EnrollError):
            parse_request(self.request(host_id="nothex"))
        with self.assertRaises(EnrollError):
            parse_request(self.request(keys=[good, good]))
        rows = parse_request(self.request(keys=[{**good, "label": "iPad\n‮SUDO ok\x1b[2J"}]))[2]
        self.assertEqual(rows[0]["label"], "iPad SUDO ok[2J")

    def test_unenrol_and_remove_take_only_what_this_program_wrote(self):
        self.store.enroll(self.request())
        foreign = self.store.directory / "someone-elses.txt"
        foreign.write_text("keep me")
        self.assertEqual(self.store.unenroll(["ipad"])["removed"], ["ipad"])
        self.assertFalse(self.store.keys_file.exists())
        self.assertTrue(foreign.exists())
        self.store.enroll(self.request())
        self.store.remove()
        self.assertTrue(foreign.exists())
        foreign.unlink()
        self.store.enroll(self.request())
        self.store.remove()
        self.assertFalse(self.store.directory.exists())

    def test_remove_pam_takes_the_key_store_with_it(self):
        self.store.enroll(self.request())
        result = self.installer.remove()
        self.assertEqual(len(result["keys"]["removed"]), 2)
        self.assertFalse((self.root / "etc/omodachi").exists())

    def test_the_root_loader_entry_point_reaches_the_store(self):
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            code = pam_install.main(["enroll", "--root", str(self.root),
                                     "--keys-json", json.dumps(self.request())])
        self.assertEqual(code, 0, printed.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(pam_install.main(["keys", "--root", str(self.root)]), 0)
        self.assertEqual(json.loads(printed.getvalue())["result"]["devices"][0]["device_id"], "ipad")

    def test_the_user_side_offers_only_keys_whose_device_switch_is_on(self):
        config = self.root / "home/.config/omodachi"
        config.mkdir(parents=True)
        (config / "host-id").write_text(HOST_ID + "\n")
        encoded = base64.b64encode(self.public).decode()
        (config / "biometric-keys.json").write_text(json.dumps({"version": 1, "keys": {
            "ipad": {"public_key": encoded, "label": "iPad", "enabled": True},
            "phone": {"public_key": encoded, "label": "Phone", "enabled": False}}}))
        request = collect_request(config, "alex")
        self.assertEqual([row["device_id"] for row in request["keys"]], ["ipad"])
        self.assertEqual(request["host_id"], HOST_ID)
        self.assertEqual(collect_request(config, "alex", ["phone"])["keys"], [])
        self.assertIn("iPad", pam_enroll.describe(request)[0])


class PanelPamStateTests(unittest.TestCase):
    """RELEASE-9: what the panel is told about the root PAM step."""

    def setUp(self):
        from omodachi_core.bootstrap import create_service
        from omodachi_core.hub import Hub
        import getpass
        self.user = getpass.getuser()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = create_service(Hub(), demo=True)
        self.service.PAM_CONFIG = str(Path(self.temp.name) / "pam.conf")
        self.service.PAM_CONFIG_OWNER = os.getuid()

    def state(self, text):
        Path(self.service.PAM_CONFIG).write_text(text)
        return self.service.pam_integration()

    def test_not_installed_outdated_and_current(self):
        self.assertEqual(self.service.pam_integration(),
                         {"pam_installed": False, "pam_protocol": None, "pam_current": False})
        self.assertEqual(self.state(f"owner={self.user}\n"),
                         {"pam_installed": True, "pam_protocol": 1, "pam_current": False})
        self.assertEqual(self.state(f"owner={self.user}\nkeys=/etc/omodachi/pam/keys.json\nprotocol=2\n"),
                         {"pam_installed": True, "pam_protocol": 2, "pam_current": True})
        # And the panel reads it where it reads every other capability.
        self.assertTrue(self.service._preferences_runtime({})["runtime"]["pam_current"])

    def test_a_config_for_someone_else_or_not_roots_is_not_installed_here(self):
        self.state("owner=somebody-else\nprotocol=2\n")
        self.assertFalse(self.service.pam_integration()["pam_installed"])
        self.state(f"owner={self.user}\nprotocol=2\n")
        self.service.PAM_CONFIG_OWNER = 0 if os.getuid() else 12345
        self.assertFalse(self.service.pam_integration()["pam_installed"])


class RootLoaderTests(unittest.TestCase):
    """The key store runs as root the only way root runs anything here: the
    loader, under `-I -B`, from files handed over on stdin."""

    def test_enrol_list_and_remove_through_the_root_loader(self):
        import importlib.util
        import subprocess
        import sys
        repository = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("install_host_for_loader", repository / "scripts/install_host.py")
        install_host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(install_host)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / "etc/pam.d").mkdir(parents=True)
        (root / "etc/pam.d/sudo").write_text(ARCH_SUDO)
        _private, public = keypair()
        request = {"owner": "alex", "host_id": HOST_ID, "keys": [
            {"device_id": "ipad", "public_key": base64.b64encode(public).decode(), "label": "iPad"}]}
        payload = install_host.pam_payload(repository)

        def loader(*arguments):
            flags = install_host.pam_command(list(arguments))[3:]
            result = subprocess.run([sys.executable, *flags], input=payload, capture_output=True,
                                    text=True, cwd=root, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)["result"]

        loader("install", "--root", str(root), "--owner", "alex",
               "--socket", "/run/omodachi/1000/omodachid.sock", "--services", "sudo")
        self.assertEqual(loader("enroll", "--root", str(root), "--keys-json", json.dumps(request))
                         ["devices"], ["ipad"])
        self.assertEqual(loader("keys", "--root", str(root))["devices"][0]["device_id"], "ipad")
        loader("remove", "--root", str(root))
        self.assertFalse((root / "etc/omodachi").exists())
        self.assertEqual(list(root.glob("**/__pycache__")), [])


if __name__ == "__main__":
    unittest.main()
