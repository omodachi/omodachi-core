"""AUTH-1: the PAM file surgery, and the helper's refusal to ever say yes by accident.

Everything runs against a `--root` sandbox, so no test here can touch the
machine's real `/etc/pam.d` even if it is run as root by mistake.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import unittest

from omodachi_core import pam_helper
from omodachi_core.pam_install import (DROPIN_DIR, DROPIN_NAME, MARKER, MARKER_COMMENT,
                                       POLKIT_HELPER_UNIT, TMPFILES_PATH, PamInstallError,
                                       PamInstaller, pam_line)

ARCH_SUDO = """#%PAM-1.0
auth\t\tinclude\t\tsystem-auth
account\t\tinclude\t\tsystem-auth
session\t\tinclude\t\tsystem-auth
session\t\toptional\tpam_systemd.so class=none
"""
VENDOR_POLKIT = """#%PAM-1.0

auth       include      system-auth
account    include      system-auth
password   include      system-auth
session    include      system-auth
"""


class PamInstallerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "etc/pam.d").mkdir(parents=True)
        (self.root / "usr/lib/pam.d").mkdir(parents=True)
        (self.root / "etc/pam.d/sudo").write_text(ARCH_SUDO)
        (self.root / "etc/pam.d/system-auth").write_text("auth required pam_unix.so\n")
        (self.root / "usr/lib/pam.d/polkit-1").write_text(VENDOR_POLKIT)
        self.installer = PamInstaller(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def install(self, **overrides):
        return self.installer.install(**{"owner": "alex", "socket": "/home/alex/.cache/s.sock",
                                         "services": ("sudo",), **overrides})

    def sudo(self):
        return (self.root / "etc/pam.d/sudo").read_text()

    def test_the_rule_goes_above_the_first_auth_line_and_carries_no_trailing_comment(self):
        self.install()
        lines = self.sudo().splitlines()
        self.assertEqual(lines[0], "#%PAM-1.0")
        self.assertEqual(lines[1], MARKER_COMMENT)
        self.assertEqual(lines[2], pam_line(45))
        self.assertTrue(lines[3].startswith("auth"))
        # Linux-PAM only honours `#` at the start of a line. A trailing comment
        # becomes two extra arguments to the helper, which then refuses every
        # prompt - the exact bug the Docker harness caught.
        self.assertFalse(lines[2].rstrip().endswith(MARKER))
        self.assertNotIn("#", lines[2])

    def test_the_shared_stacks_are_never_touched(self):
        before = (self.root / "etc/pam.d/system-auth").read_bytes()
        self.install()
        self.assertEqual((self.root / "etc/pam.d/system-auth").read_bytes(), before)
        for name in ("system-auth", "system-login", "passwd"):
            with self.assertRaises(PamInstallError):
                self.install(services=(name,))

    def test_installing_twice_changes_nothing_the_second_time(self):
        self.install()
        once = self.sudo()
        self.assertFalse(self.install()["services"][0]["changed"])
        self.assertEqual(self.sudo(), once)

    def test_a_second_install_does_not_overwrite_the_original_backup(self):
        self.install()
        self.install(timeout=90)
        self.assertIn("--timeout 90", self.sudo())
        self.assertNotIn("--timeout 45", self.sudo())
        self.assertEqual(self.installer.backup_file("sudo").read_text(), ARCH_SUDO)
        self.installer.remove()
        self.assertEqual(self.sudo(), ARCH_SUDO)

    def test_remove_restores_byte_for_byte(self):
        original = (self.root / "etc/pam.d/sudo").read_bytes()
        self.install()
        self.assertNotEqual((self.root / "etc/pam.d/sudo").read_bytes(), original)
        result = self.installer.remove()
        self.assertEqual([row["how"] for row in result["services"]], ["restored"])
        self.assertEqual((self.root / "etc/pam.d/sudo").read_bytes(), original)
        self.assertFalse((self.root / "usr/local/bin/omodachi-pam").exists())
        self.assertFalse((self.root / "etc/omodachi/pam.conf").exists())
        self.assertFalse((self.root / "etc/omodachi").exists())

    def test_a_vendor_only_service_is_shadowed_and_the_shadow_is_deleted_again(self):
        self.install(services=("sudo", "polkit-1"))
        shadow = self.root / "etc/pam.d/polkit-1"
        self.assertTrue(shadow.exists())
        stripped = "".join(row for row in shadow.read_text().splitlines(keepends=True)
                           if MARKER not in row and "omodachi-pam" not in row)
        self.assertEqual(stripped, VENDOR_POLKIT)
        self.installer.remove()
        self.assertFalse(shadow.exists())
        self.assertEqual((self.root / "usr/lib/pam.d/polkit-1").read_text(), VENDOR_POLKIT)

    def test_a_service_with_no_file_anywhere_is_refused_rather_than_invented(self):
        with self.assertRaises(PamInstallError) as caught:
            self.install(services=("hyprlock",))
        self.assertEqual(caught.exception.code, "pam_service_missing")

    def test_remove_with_a_lost_manifest_still_strips_our_lines(self):
        self.install()
        self.installer.manifest_file().unlink()
        self.installer.remove()
        self.assertEqual(self.sudo(), ARCH_SUDO)

    def test_remove_without_a_backup_strips_only_our_own_lines(self):
        self.install()
        self.installer.backup_file("sudo").unlink()
        self.installer.remove()
        self.assertEqual(self.sudo(), ARCH_SUDO)

    def test_the_helper_and_config_are_root_shaped_and_the_config_says_what_it_should(self):
        result = self.install(services=("sudo",), timeout=30)
        helper = self.root / "usr/local/bin/omodachi-pam"
        self.assertEqual(helper.stat().st_mode & 0o777, 0o755)
        self.assertEqual(helper.read_bytes(), Path(__import__("omodachi_core.pam_helper", fromlist=["x"]).__file__).read_bytes())
        config = (self.root / "etc/omodachi/pam.conf").read_text()
        self.assertIn("owner=alex\n", config)
        self.assertIn("socket=/home/alex/.cache/s.sock\n", config)
        self.assertIn("services=sudo\n", config)
        self.assertIn("timeout=30\n", config)
        self.assertEqual(result["manifest"]["timeout"], 30)

    def test_bad_arguments_are_refused(self):
        for bad in ({"owner": ""}, {"owner": "a/b"}, {"socket": "relative"},
                    {"timeout": 1}, {"timeout": 9999}, {"services": ()}, {"services": ("telnet",)}):
            with self.assertRaises(PamInstallError):
                self.install(**bad)

    def test_status_and_verify_restored_report_the_truth(self):
        self.assertFalse(self.installer.status()["installed"])
        self.install()
        status = self.installer.status()
        self.assertTrue(status["installed"])
        self.assertTrue(status["services"][0]["line_installed"])
        self.assertFalse(self.installer.verify_restored()["all_identical"])
        self.installer.remove(keep_backups=True)
        self.assertTrue(self.installer.verify_restored()["all_identical"])


class PolkitSandboxTests(unittest.TestCase):
    """AUTH-2: the two files this installer writes outside /etc/pam.d.

    `polkit-agent-helper@.service` runs the PAM stack under `ProtectHome=yes`,
    which blanks `/home`, `/root` and `/run/user` with an *inaccessible* mount
    that nothing can be mounted back under. So the socket has to live in a
    fourth place, `/run/omodachi/<uid>`, a `tmpfiles.d` fragment has to make it
    (a user cannot), and a drop-in declares it on the unit. Everything here is
    about those two files existing only when they can do something, saying
    exactly what they do, and disappearing again on `remove()`.
    """
    SHARED_SOCKET = "/run/omodachi/1000/omodachid.sock"
    RUNTIME_SOCKET = "/run/user/1000/omodachi/omodachid.sock"
    HOME_SOCKET = "/home/alex/.cache/omodachi/omodachid.sock"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "etc/pam.d").mkdir(parents=True)
        (self.root / "usr/lib/pam.d").mkdir(parents=True)
        (self.root / "etc/pam.d/sudo").write_text(ARCH_SUDO)
        (self.root / "usr/lib/pam.d/polkit-1").write_text(VENDOR_POLKIT)
        self.installer = PamInstaller(self.root)
        self.dropin = self.root / DROPIN_DIR / DROPIN_NAME
        self.tmpfiles = self.root / TMPFILES_PATH

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def install(self, **overrides):
        return self.installer.install(**{"owner": "alex", "socket": self.SHARED_SOCKET,
                                         "services": ("sudo", "polkit-1"), **overrides})

    def test_the_drop_in_lands_beside_the_unit_and_names_the_socket_directory(self):
        result = self.install()
        self.assertTrue(self.dropin.is_file())
        self.assertEqual(self.dropin.parent.name, POLKIT_HELPER_UNIT + ".d")
        body = self.dropin.read_text()
        # Every line that is not a comment is the section header and one
        # directive, and that directive is one directory.
        directives = [row for row in body.splitlines()
                      if row.strip() and not row.startswith("#") and row != "[Service]"]
        self.assertEqual(directives, ["ReadWritePaths=-/run/omodachi/1000"])
        self.assertIn("[Service]\n", body)
        self.assertEqual(self.dropin.stat().st_mode & 0o777, 0o644)
        self.assertEqual(result["dropin"]["path"], str(self.dropin))
        self.assertEqual(result["manifest"]["dropin"], str(self.dropin))

    def test_the_tmpfiles_fragment_makes_a_root_parent_and_a_private_child(self):
        result = self.install()
        rows = [row for row in self.tmpfiles.read_text().splitlines()
                if row.strip() and not row.startswith("#")]
        self.assertEqual(rows, ["d /run/omodachi 0755 root root -",
                                "d /run/omodachi/1000 0700 alex alex -"])
        self.assertEqual(self.tmpfiles.stat().st_mode & 0o777, 0o644)
        self.assertEqual(result["manifest"]["tmpfiles"], str(self.tmpfiles))

    def test_the_leading_dash_is_there_so_a_stopped_daemon_cannot_break_the_helper(self):
        # Without it, a missing directory is a unit that refuses to start, and
        # a polkit agent helper that refuses to start is a prompt nobody can
        # answer - worse than the password prompt this feature replaces.
        self.install()
        self.assertIn("ReadWritePaths=-/", self.dropin.read_text())

    def test_no_polkit_in_the_service_list_means_neither_file(self):
        result = self.installer.install(owner="alex", socket=self.SHARED_SOCKET,
                                        services=("sudo",))
        self.assertFalse(self.dropin.exists())
        self.assertFalse(self.tmpfiles.exists())
        self.assertEqual(result["dropin"]["reason"], "polkit_not_configured")
        self.assertEqual(result["runtime_dir"]["reason"], "polkit_not_configured")
        self.assertIsNone(result["manifest"]["dropin"])
        self.assertIsNone(result["manifest"]["tmpfiles"])

    def test_a_socket_the_sandbox_could_never_see_gets_the_reason_not_a_file(self):
        # The home is AUTH-1's shape. `/run/user` is the one that looks like it
        # should work and does not: ProtectHome=yes blanks it too, and no
        # ReadWritePaths= or BindPaths= reaches under an inaccessible mount.
        for socket in (self.HOME_SOCKET, self.RUNTIME_SOCKET):
            result = self.install(socket=socket)
            self.assertFalse(self.dropin.exists(), socket)
            self.assertFalse(self.tmpfiles.exists(), socket)
            self.assertEqual(result["dropin"]["reason"], "socket_not_in_shared_runtime_root")
            self.assertEqual(result["runtime_dir"]["reason"], "socket_not_in_shared_runtime_root")
            self.assertIsNone(result["manifest"]["dropin"])

    def test_a_reinstall_pointed_elsewhere_takes_the_old_grant_away(self):
        # Found by the Docker harness. A root-owned file that grants a
        # permission must not outlive the configuration that asked for it.
        self.install()
        self.assertTrue(self.dropin.is_file())
        self.assertTrue(self.tmpfiles.is_file())
        result = self.install(socket=self.HOME_SOCKET)
        self.assertFalse(self.dropin.exists())
        self.assertFalse(self.tmpfiles.exists())
        self.assertTrue(result["dropin"]["removed"])
        self.assertTrue(result["runtime_dir"]["removed"])

    def test_dropping_polkit_from_the_service_list_takes_them_away_too(self):
        self.install()
        self.install(services=("sudo",))
        self.assertFalse(self.dropin.exists())
        self.assertFalse(self.tmpfiles.exists())

    def test_installing_twice_writes_the_same_bytes_and_says_nothing_changed(self):
        self.install()
        first = (self.dropin.read_bytes(), self.tmpfiles.read_bytes())
        result = self.install()
        self.assertEqual((self.dropin.read_bytes(), self.tmpfiles.read_bytes()), first)
        self.assertFalse(result["dropin"]["changed"])
        self.assertFalse(result["runtime_dir"]["changed"])
        self.assertEqual(result["dropin"]["daemon_reload"], "unchanged")

    def test_remove_deletes_both_and_the_directory_the_drop_in_made(self):
        self.install()
        result = self.installer.remove()
        self.assertFalse(self.dropin.exists())
        self.assertFalse(self.dropin.parent.exists())
        self.assertFalse(self.tmpfiles.exists())
        self.assertTrue(result["dropin"]["removed"])
        self.assertTrue(result["dropin"]["directory_pruned"])
        self.assertTrue(result["runtime_dir"]["removed"])

    def test_remove_leaves_somebody_elses_drop_in_alone(self):
        self.install()
        neighbour = self.dropin.parent / "10-someone-else.conf"
        neighbour.write_text("[Service]\nNice=5\n")
        self.installer.remove()
        self.assertFalse(self.dropin.exists())
        self.assertTrue(neighbour.is_file())
        self.assertEqual(neighbour.read_text(), "[Service]\nNice=5\n")

    def test_remove_with_no_install_is_quiet(self):
        result = self.installer.remove()
        self.assertFalse(result["dropin"]["removed"])
        self.assertFalse(result["runtime_dir"]["removed"])
        self.assertEqual(result["dropin"]["daemon_reload"], "unchanged")

    def test_status_reports_whether_they_are_there(self):
        status = self.installer.status()
        self.assertFalse(status["dropin_present"])
        self.assertFalse(status["tmpfiles_present"])
        self.install()
        status = self.installer.status()
        self.assertTrue(status["dropin_present"])
        self.assertTrue(status["tmpfiles_present"])
        self.installer.remove()
        self.assertFalse(self.installer.status()["dropin_present"])
        self.assertFalse(self.installer.status()["tmpfiles_present"])

    def test_a_root_prefixed_run_never_touches_the_real_systemd(self):
        # Every test in this file runs against a --root sandbox; none of them
        # may reload the machine's own service manager or create a directory
        # in its real /run.
        result = self.install()
        self.assertEqual(result["dropin"]["daemon_reload"], "skipped_root_prefix")
        self.assertEqual(result["runtime_dir"]["tmpfiles"], "skipped_root_prefix")


class _Daemon:
    """A one-shot socket that answers whatever the test tells it to."""

    def __init__(self, directory, reply, *, delay=0.0):
        self.path = str(Path(directory) / "daemon.sock")
        self.reply, self.delay = reply, delay
        self.requests = []
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(self.path)
        self.socket.listen(4)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                connection, _ = self.socket.accept()
            except OSError:
                return
            with connection:
                try:
                    self.requests.append(json.loads(connection.recv(65536).split(b"\n")[0]))
                    if self.delay:
                        import time
                        time.sleep(self.delay)
                    if self.reply is not None:
                        connection.sendall(self.reply)
                except OSError:
                    pass

    def close(self):
        self.socket.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


class PamHelperTests(unittest.TestCase):
    """The helper's only job is to never say yes when it should not."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.owner = __import__("pwd").getpwuid(os.getuid()).pw_name
        self.daemon = None
        self.environment = dict(os.environ)

    def tearDown(self):
        if self.daemon:
            self.daemon.close()
        os.environ.clear()
        os.environ.update(self.environment)
        shutil.rmtree(self.directory, ignore_errors=True)

    def config(self, **overrides):
        values = {"owner": self.owner, "socket": str(self.directory / "daemon.sock"),
                  "services": "sudo", "timeout": "5", **overrides}
        path = self.directory / "pam.conf"
        path.write_text("".join(f"{key}={value}\n" for key, value in values.items() if value is not None))
        return str(path)

    def environ(self, **overrides):
        os.environ.clear()
        os.environ.update({"PAM_TYPE": "auth", "PAM_SERVICE": "sudo", "PAM_USER": self.owner,
                           "PAM_RUSER": self.owner, **overrides})

    def run_helper(self, config=None, argv=("--timeout", "5")):
        return pam_helper.main(["--config", config or self.config(), *argv])

    def approve(self, device_name="Leo's iPad"):
        body = json.dumps({"ok": True, "result": {"approved": True, "device_name": device_name}})
        self.daemon = _Daemon(self.directory, (body + "\n").encode())

    def test_an_explicit_approval_is_the_only_success(self):
        self.environ()
        self.approve()
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.daemon.requests[0]["op"], "local.auth.approve")
        self.assertEqual(self.daemon.requests[0]["service"], "sudo")
        self.assertEqual(self.daemon.requests[0]["requester"], self.owner)

    def test_every_negative_answer_is_a_refusal(self):
        self.environ()
        for body in ('{"ok": true, "result": {"approved": false}}',
                     '{"ok": false, "error": "permission_denied"}',
                     '{"ok": true, "result": {}}',
                     '{"ok": true}', '{"result": {"approved": true}}',
                     'not json at all', ''):
            with self.subTest(body=body):
                self.daemon = _Daemon(self.directory, (body + "\n").encode() if body else b"")
                self.assertEqual(self.run_helper(), 1)
                self.daemon.close()

    def test_no_daemon_is_an_immediate_refusal(self):
        self.environ()
        self.assertEqual(self.run_helper(), 1)

    def test_a_socket_owned_by_somebody_else_is_not_the_daemon(self):
        self.environ()
        self.approve()
        # uid 0 is not this test process, and the helper checks the socket's
        # owner against the uid the root-owned config names.
        self.assertEqual(self.run_helper(self.config(owner="root")), 1)

    def test_a_requester_who_is_not_the_owner_never_reaches_the_daemon(self):
        self.approve()
        self.environ(PAM_RUSER="somebody-else", PAM_USER="root")
        self.assertEqual(self.run_helper(), 1)
        self.assertEqual(self.daemon.requests, [])

    def test_an_unconfigured_service_never_reaches_the_daemon(self):
        self.approve()
        self.environ(PAM_SERVICE="sshd")
        self.assertEqual(self.run_helper(), 1)
        self.assertEqual(self.daemon.requests, [])

    def test_a_non_auth_stack_is_refused(self):
        self.approve()
        self.environ(PAM_TYPE="account")
        self.assertEqual(self.run_helper(), 1)
        self.assertEqual(self.daemon.requests, [])

    def test_an_unreadable_or_incomplete_config_is_a_refusal(self):
        self.environ()
        self.approve()
        self.assertEqual(self.run_helper(str(self.directory / "missing.conf")), 1)
        self.assertEqual(self.run_helper(self.config(owner=None)), 1)
        self.assertEqual(self.run_helper(self.config(socket="not-absolute")), 1)

    def test_an_argument_it_does_not_understand_is_a_refusal(self):
        """This is the trailing-`#`-comment bug, guarded from the helper's side."""
        self.environ()
        self.approve()
        self.assertEqual(self.run_helper(argv=("--timeout", "5", "#", MARKER)), 1)
        self.assertEqual(self.daemon.requests, [])

    def test_a_daemon_that_never_answers_ends_anyway(self):
        self.environ()
        self.daemon = _Daemon(self.directory, None)
        self.assertEqual(pam_helper.main(["--config", self.config(timeout="5"), "--timeout", "5"]), 1)

    def test_the_timeout_it_asks_for_is_clamped(self):
        self.environ()
        self.approve()
        self.assertEqual(self.run_helper(argv=("--timeout", "100000")), 0)
        self.assertEqual(self.daemon.requests[-1]["timeout"], pam_helper.MAX_TIMEOUT)
        self.assertEqual(self.run_helper(argv=("--timeout", "0")), 0)
        self.assertEqual(self.daemon.requests[-1]["timeout"], pam_helper.MIN_TIMEOUT)
        self.assertEqual(self.run_helper(argv=("--timeout", "nonsense")), 0)
        self.assertEqual(self.daemon.requests[-1]["timeout"], pam_helper.DEFAULT_TIMEOUT)

    def test_a_lock_screen_names_the_owner_as_the_user_and_that_is_enough(self):
        self.approve()
        self.environ(PAM_SERVICE="sudo", PAM_RUSER="", PAM_USER=self.owner)
        self.assertEqual(self.run_helper(), 0)

    def test_an_empty_ruser_with_a_different_target_user_is_not_decidable(self):
        self.approve()
        self.environ(PAM_RUSER="", PAM_USER="root")
        self.assertEqual(self.run_helper(), 1)
        self.assertEqual(self.daemon.requests, [])

    def test_a_hostile_device_name_is_not_printed(self):
        self.environ()
        self.approve(device_name="ok\r\nSUDO: granted")
        self.assertEqual(self.run_helper(), 0)
        self.assertIsNone(pam_helper.decide({"ok": True, "result": {
            "approved": True, "device_name": "bad\nname"}})[1])


if __name__ == "__main__":
    unittest.main()
