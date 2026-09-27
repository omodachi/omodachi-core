"""The installer's host-touching steps: the ufw rules and the state migration.

Every sudo call is captured instead of run and every path is a temporary one.
The point of the firewall half is the rule shape - the same private CIDRs and
tailscale0 scoping omarchy-install-service-sunshine uses - and that removal only
ever spends rule numbers Omodachi owns. The point of the migration half is that
it edits an existing file and invents nothing. The point of the apps.json half is
that the fork ends up publishing exactly the entry core advertises, without the
installer becoming a second author of the user's Sunshine configuration.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from omodachi_core import plugin_bridge, protocol
from omodachi_core.auth import DeviceAuthenticator

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("install_host", ROOT / "scripts/install_host.py")
install_host = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_host)


STATUS = """Status: active

     To                         Action      From
     --                         ------      ----
[ 1] 22/tcp                     ALLOW IN    192.168.1.22              # omodachi-dev-mac
[ 2] 53317/udp                  ALLOW IN    Anywhere
[ 3] 172.17.0.1 53/udp          ALLOW IN    172.16.0.0/12              # allow-docker-dns
[ 4] 22/tcp                     LIMIT IN    Anywhere                   # omarchy-sshd
[ 5] 47984,47989,48010/tcp      ALLOW IN    192.168.1.22              # omadochi-mac-pair-check
[ 6] 47998:48000/udp            ALLOW IN    192.168.1.22              # omadochi-mac-pair-check
[ 7] 8099/tcp                   ALLOW IN    10.0.0.0/8                 # omodachi-core
"""


# `ufw show added` for the same rules: what removal reads, active or not.
ADDED = """Added user rules (see 'ufw status' for running firewall):
ufw allow from 192.168.1.22 to any port 22 proto tcp comment 'omodachi-dev-mac'
ufw allow 53317/udp
ufw allow from 172.16.0.0/12 to 172.17.0.1 port 53 proto udp comment 'allow-docker-dns'
ufw limit 22/tcp comment 'omarchy-sshd'
ufw allow from 192.168.1.22 to any port 47984,47989,48010 proto tcp comment 'omadochi-mac-pair-check'
ufw allow from 192.168.1.22 to any port 47998:48000 proto udp comment 'omadochi-mac-pair-check'
ufw allow from 10.0.0.0/8 to any port 8099 proto tcp comment 'omodachi-core'
ufw allow in on tailscale0 to any port 47998:48000 proto udp comment 'omodachi-sunshine'
"""


class Recorder:
    def __init__(self, *, status=STATUS, added=ADDED, tailscale=True, fail_on=None):
        self.calls, self.status, self.tailscale, self.fail_on = [], status, tailscale, fail_on
        self.added = added

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:3] == ["ip", "link", "show"]:
            return type("R", (), {"returncode": 0 if self.tailscale else 1, "stdout": ""})()
        if self.fail_on is not None and self.fail_on in argv:
            raise OSError("ufw refused")
        if argv[:4] == ["sudo", "ufw", "show", "added"]:
            return type("R", (), {"returncode": 0, "stdout": self.added})()
        return type("R", (), {"returncode": 0, "stdout": self.status})()

    def ufw_calls(self):
        return [call for call in self.calls if call[:2] == ["sudo", "ufw"]]


class FirewallTests(unittest.TestCase):
    def install(self, recorder):
        self.patch(recorder)
        return install_host.configure_firewall()

    def patch(self, recorder):
        original_run, original_which = install_host.run, install_host.shutil.which
        original_sub = install_host.subprocess.run
        install_host.run = recorder.run
        install_host.subprocess.run = recorder.run
        install_host.shutil.which = lambda name: "/usr/bin/" + name
        def restore():
            install_host.run = original_run
            install_host.subprocess.run = original_sub
            install_host.shutil.which = original_which
        self.addCleanup(restore)

    def test_core_and_sunshine_ports_open_for_private_lans_and_tailscale_only(self):
        recorder = Recorder()
        self.install(recorder)
        rules = [" ".join(call) for call in recorder.ufw_calls() if call[2] == "allow"]
        for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
            self.assertIn(f"sudo ufw allow in proto tcp from {cidr} to any port 8099 "
                          "comment omodachi-core", rules)
            self.assertIn(f"sudo ufw allow in proto tcp from {cidr} to any port "
                          "47984,47989,48010 comment omodachi-sunshine", rules)
            self.assertIn(f"sudo ufw allow in proto udp from {cidr} to any port "
                          "47998:48000 comment omodachi-sunshine", rules)
        self.assertIn("sudo ufw allow in on tailscale0 to any port 8099 proto tcp "
                      "comment omodachi-core", rules)
        self.assertIn("sudo ufw allow in on tailscale0 to any port 47998:48000 proto udp "
                      "comment omodachi-sunshine", rules)
        # Nothing is ever opened to the whole internet.
        for rule in rules:
            self.assertNotIn("from any", rule)
            self.assertNotIn("Anywhere", rule)
        self.assertIn(["sudo", "ufw", "reload"], recorder.ufw_calls())

    def test_a_host_without_tailscale_gets_only_the_private_lan_rules(self):
        recorder = Recorder(tailscale=False)
        self.install(recorder)
        self.assertFalse([call for call in recorder.ufw_calls() if "tailscale0" in call])

    def test_install_deletes_no_rule_at_all(self):
        # RELEASE-9: hand-written rules, whatever their comment, are not ours.
        recorder = Recorder()
        self.install(recorder)
        self.assertEqual([call for call in recorder.ufw_calls() if "delete" in call], [])

    def test_removal_is_symmetric_and_never_spends_another_owner_s_rule(self):
        recorder = Recorder()
        self.patch(recorder)
        self.assertEqual(install_host.remove_firewall(), 0)
        deletes = [" ".join(call) for call in recorder.ufw_calls() if "delete" in call]
        # Only our two rules in the fixture listing; the dev-mac ssh allowance,
        # the Omarchy sshd rule and the docker DNS rules stay.
        self.assertEqual(deletes, [
            "sudo ufw --force delete allow from 10.0.0.0/8 to any port 8099 proto tcp",
            "sudo ufw --force delete allow in on tailscale0 to any port 47998:48000 proto udp"])

    def test_removal_finds_the_rules_while_ufw_is_not_enabled(self):
        # RELEASE-9 integration (clean VM): `ufw status numbered` lists nothing
        # while ufw is inactive, so the rules Install added were never deleted.
        recorder = Recorder(status="Status: inactive\n")
        self.patch(recorder)
        self.assertEqual(install_host.remove_firewall(), 0)
        self.assertEqual(len([call for call in recorder.ufw_calls() if "delete" in call]), 2)

    def test_a_refused_firewall_warns_but_never_fails_the_install(self):
        recorder = Recorder(fail_on="allow")
        self.assertEqual(self.install(recorder), 0)

    def test_a_host_without_ufw_is_not_an_error(self):
        recorder = Recorder()
        self.patch(recorder)
        install_host.shutil.which = lambda name: None
        self.assertEqual(install_host.configure_firewall(), 0)
        self.assertEqual(install_host.remove_firewall(), 0)
        self.assertFalse(recorder.ufw_calls())


class FirewallTruthTests(unittest.TestCase):
    """RELEASE-9: the summary says whether ufw is filtering anything at all."""

    VERBOSE_ACTIVE = "Status: active\nLogging: on (low)\nDefault: deny (incoming), allow (outgoing), disabled (routed)\n"

    def summary(self, status, *, which=True):
        import contextlib
        import io
        from unittest import mock
        recorder = Recorder(status=status)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(install_host, "run", recorder.run), \
                mock.patch.object(install_host.subprocess, "run", recorder.run), \
                mock.patch.object(install_host.shutil, "which",
                                  lambda name: ("/usr/bin/" + name) if which else None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(install_host.configure_firewall(), 0)
        return out.getvalue() + err.getvalue()

    def test_inactive_ufw_is_reported_as_filtering_nothing(self):
        text = self.summary("Status: inactive\n")
        self.assertIn("NOT active", text)
        self.assertIn("reachable from every network", text)
        self.assertIn("sudo ufw enable", text)
        self.assertNotIn("are in place", text)

    def test_missing_ufw_is_reported_as_filtering_nothing(self):
        text = self.summary("", which=False)
        self.assertIn("ufw is not installed", text)
        self.assertIn("reachable from every network", text)

    def test_active_ufw_with_a_deny_default_names_the_ranges(self):
        text = self.summary(self.VERBOSE_ACTIVE)
        self.assertIn("ufw is active and denies incoming traffic by default", text)
        self.assertIn("10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 and tailscale0", text)
        self.assertIn("47990 is not opened", text)

    def test_active_ufw_that_allows_incoming_by_default_is_not_called_a_filter(self):
        text = self.summary(self.VERBOSE_ACTIVE.replace("deny (incoming)", "allow (incoming)"))
        self.assertIn("default for incoming traffic is allow", text)
        self.assertIn("reachable from every network", text)


class VenvOwnershipTests(unittest.TestCase):
    """RELEASE-9 (after RELEASE-8's src): the venvs are replaced or deleted only
    when this installer can show it made them."""

    def setUp(self):
        from unittest import mock
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.share = self.home / ".local/share/omodachi"
        self.venv = self.share / "venv"
        self.calls = []

        def fake_run(argv, **kwargs):
            self.calls.append(list(argv))
            if argv[2:4] == ["-m", "venv"]:
                Path(argv[4]).mkdir(parents=True)
                (Path(argv[4]) / "pyvenv.cfg").write_text("home = /usr/bin\n")
            return _Completed()
        patcher = mock.patch.object(install_host, "run", fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tree(self, path):
        import hashlib
        digest = hashlib.sha256()
        for item in sorted(path.rglob("*")):
            digest.update(str(item.relative_to(path)).encode())
            if item.is_file():
                digest.update(item.read_bytes())
        return digest.hexdigest()

    def earlier_venv(self, path, *, url=None, extra=None):
        site = path / "lib/python3.14/site-packages"
        (path / "pyvenv.cfg").parent.mkdir(parents=True, exist_ok=True)
        (path / "pyvenv.cfg").write_text("home = /usr/bin\n")
        pins = install_host._lock_pins(ROOT)
        for name, version in list(pins.items()) + [("pip", "25.2")] + list((extra or {}).items()):
            (site / f"{name}-{version}.dist-info").mkdir(parents=True)
        core = site / "omodachi_core-0.2.0.dist-info"
        core.mkdir(parents=True)
        (core / "direct_url.json").write_text(json.dumps(
            {"url": url or (self.home / install_host.REMOTE_SOURCE).as_uri(), "dir_info": {}}))

    def test_a_fresh_install_records_the_venv_it_makes(self):
        install_host.install_venv(ROOT, self.venv, self.home)
        identifier = (self.venv / install_host.VENV_ID_FILE).read_text().strip()
        self.assertEqual(install_host._read_venv_ids(self.home), [identifier])
        self.assertEqual((self.home / install_host.VENV_RECORD).stat().st_mode & 0o777, 0o600)
        self.assertEqual(install_host.venv_ownership(self.home, self.venv)[0], "ours")
        # A reinstall replaces it, and the record names only the new one.
        install_host.install_venv(ROOT, self.venv, self.home)
        second = (self.venv / install_host.VENV_ID_FILE).read_text().strip()
        self.assertNotEqual(second, identifier)
        self.assertEqual(install_host._read_venv_ids(self.home), [second])
        self.assertFalse((self.share / "venv.previous").exists())

    def test_a_venv_somebody_else_made_is_refused_and_byte_identical(self):
        for name in ("venv", "venv.previous"):
            with self.subTest(name=name):
                path = self.share / name
                (path / "bin").mkdir(parents=True)
                (path / "bin/python").write_text("mine")
                (path / "pyvenv.cfg").write_text("home = /usr/bin\n")
                before = self.tree(path)
                refusal = install_host.venv_refusal(self.home, ROOT)
                self.assertIn("is not a virtualenv this installer made", refusal)
                self.assertIn(str(path), refusal)
                with self.assertRaises(SystemExit):
                    install_host.install_venv(ROOT, self.venv, self.home)
                self.assertEqual(self.tree(path), before)
                import shutil
                shutil.rmtree(path)

    def test_an_id_that_is_not_in_the_record_is_not_ours(self):
        (self.venv).mkdir(parents=True)
        (self.venv / install_host.VENV_ID_FILE).write_text("a" * 32 + "\n")
        install_host._write_venv_ids(self.home, ["b" * 32])
        self.assertEqual(install_host.venv_ownership(self.home, self.venv)[0], "foreign")

    def test_a_link_is_never_ours(self):
        target = self.home / "elsewhere"
        target.mkdir()
        self.share.mkdir(parents=True)
        self.venv.symlink_to(target)
        self.assertEqual(install_host.venv_ownership(self.home, self.venv)[0], "foreign")

    def test_an_earlier_installs_venv_is_adopted_and_replaced(self):
        self.earlier_venv(self.venv)
        self.assertEqual(install_host.venv_ownership(self.home, self.venv, ROOT), ("earlier", ""))
        self.assertEqual(install_host.venv_refusal(self.home, ROOT), "")
        install_host.install_venv(ROOT, self.venv, self.home)
        self.assertEqual(install_host.venv_ownership(self.home, self.venv)[0], "ours")

    def test_a_venv_that_only_looks_like_an_earlier_one_is_not_adopted(self):
        for case, options in (("other source", {"url": "file:///home/u/my-checkout"}),
                              ("extra package", {"extra": {"requests": "2.32.0"}})):
            with self.subTest(case=case):
                import shutil
                shutil.rmtree(self.venv, ignore_errors=True)
                self.earlier_venv(self.venv, **options)
                kind, _ = install_host.venv_ownership(self.home, self.venv, ROOT)
                self.assertEqual(kind, "foreign", case)

    def test_remove_deletes_only_ours(self):
        install_host.install_venv(ROOT, self.venv, self.home)
        stranger = self.share / "venv.previous"
        (stranger / "bin").mkdir(parents=True)
        (stranger / "bin/python").write_text("mine")
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            result = install_host.remove_venvs(self.home)
        self.assertEqual(result["removed"], ["venv"])
        self.assertIn(str(stranger), result["kept"])
        self.assertTrue((stranger / "bin/python").exists())
        self.assertFalse((self.home / install_host.VENV_RECORD).exists())

    def test_a_failed_install_puts_the_old_venv_back_and_forgets_the_new_id(self):
        install_host.install_venv(ROOT, self.venv, self.home)
        first = install_host._read_venv_ids(self.home)
        import subprocess
        from unittest import mock

        def failing(argv, **kwargs):
            if argv[2:4] == ["-m", "venv"]:
                Path(argv[4]).mkdir(parents=True)
                return _Completed()
            raise subprocess.CalledProcessError(1, argv)
        with mock.patch.object(install_host, "run", failing), \
                mock.patch.object(install_host.sys, "stderr"), \
                self.assertRaises(subprocess.CalledProcessError):
            install_host.install_venv(ROOT, self.venv, self.home)
        self.assertEqual(install_host._read_venv_ids(self.home), first)
        self.assertEqual(install_host.venv_ownership(self.home, self.venv)[0], "ours")


class InstallerFilesTests(unittest.TestCase):
    """RELEASE-9: Install writes its units, commands, template and hook sources
    only over nothing or over its own, and never through a link."""

    def test_files_that_are_not_the_installers_stop_the_install(self):
        for relative, text in ((".config/systemd/user/omodachid.service", "[Service]\nExecStart=/opt/mine\n"),
                               (".local/bin/omodachi-host", "#!/bin/sh\necho mine\n"),
                               (".config/omarchy/themed/omodachi-theme.json.tpl", '{"mine": 1}\n'),
                               (".local/share/omodachi/hooks/theme-set/omodachi", "#!/bin/sh\n")):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as scratch:
                home = Path(scratch)
                (home / relative).parent.mkdir(parents=True)
                (home / relative).write_text(text)
                self.assertIn(str(home / relative), install_host.files_refusal(home, ROOT))
                with self.assertRaises(install_host.NotOurs):
                    install_host.write(home / relative, "new\n", ours=lambda text: False)
                self.assertEqual((home / relative).read_text(), text)

    def test_earlier_installs_files_are_recognised_as_the_installers(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            old = install_host.DAEMON_UNIT.replace(install_host.UNIT_MARKER + "\n", "")
            (home / ".config/systemd/user").mkdir(parents=True)
            (home / ".config/systemd/user/omodachid.service").write_text(old)
            (home / ".local/bin").mkdir(parents=True)
            (home / ".local/bin/omodachid").write_text(install_host.WRAPPER % "omodachid")
            template = home / install_host.THEMED_DIR / install_host.THEME_TEMPLATE
            template.parent.mkdir(parents=True)
            template.write_text((ROOT / "src/omodachi_core/data" / install_host.THEME_TEMPLATE).read_text())
            self.assertEqual(install_host.files_refusal(home, ROOT), "")

    def test_a_link_is_replaced_not_written_through(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            victim = home / "victim"
            victim.write_text("precious\n")
            link = home / "unit.service"
            link.symlink_to(victim)
            self.assertIn(str(link), install_host.files_refusal(home, ROOT) or str(link))
            install_host.write(link, "ours\n")
            self.assertEqual(victim.read_text(), "precious\n")
            self.assertFalse(link.is_symlink())


class OutdatedPamTests(unittest.TestCase):
    def test_a_pam_entry_from_before_release_9_is_reported_not_installed_over(self):
        with tempfile.TemporaryDirectory() as scratch:
            conf = Path(scratch) / "pam.conf"
            self.assertFalse(install_host.pam_outdated(str(conf)))
            conf.write_text("owner=u\nsocket=/run/omodachi/1000/omodachid.sock\nservices=sudo\n")
            self.assertTrue(install_host.pam_outdated(str(conf)))
            conf.write_text(conf.read_text() + "keys=/etc/omodachi/pam/keys.json\nprotocol=2\n")
            self.assertFalse(install_host.pam_outdated(str(conf)))

    def test_install_exits_partial_while_the_old_helper_is_there(self):
        import contextlib
        import io
        from unittest import mock
        out = io.StringIO()
        with mock.patch.object(install_host.sys, "platform", "linux"), \
                mock.patch.object(install_host, "install_local", return_value=0), \
                mock.patch.object(install_host, "pam_outdated", return_value=True), \
                contextlib.redirect_stdout(out):
            code = install_host.main(["--local"])
        self.assertEqual(code, install_host.PARTIAL)
        self.assertIn("only PARTLY installed", out.getvalue())
        self.assertIn("--local --pam", out.getvalue())


class SunshineBuildCacheTests(unittest.TestCase):
    def test_a_directory_at_the_build_cache_path_that_is_not_ours_is_left_alone(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "sunshine-src"
            root.mkdir()
            (root / "mine.txt").write_text("mine")
            with mock.patch.object(install_host, "run") as run:
                ok, detail = install_host.fetch_sunshine_commit("https://x/y.git", "a" * 40, root)
            self.assertFalse(ok)
            self.assertIn("not this installer's build cache", detail)
            run.assert_not_called()
            self.assertEqual((root / "mine.txt").read_text(), "mine")


class SunshineAppsTests(unittest.TestCase):
    """apps.json is Leo's file; the installer adds one entry and nothing else."""

    NAME = "Omodachi Desktop"

    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.path = self.home / install_host.SUNSHINE_APPS
        self.backup = self.home / install_host.SUNSHINE_APPS_BACKUP
        self.path.parent.mkdir(parents=True)

    def write(self, value):
        import json
        self.path.write_text(json.dumps(value, indent=4, sort_keys=True))

    def read(self):
        import json
        return json.loads(self.path.read_text())

    def ensure(self):
        return install_host.ensure_sunshine_app(self.home, self.NAME)

    def test_the_entry_is_the_one_core_advertises_and_is_never_spelled_twice(self):
        # The installer must not carry its own copy of the string; if the
        # constant moves or changes, this test moves with it.
        self.assertEqual(install_host._sunshine_app_name(), self.NAME)
        source = (Path(install_host.__file__).parent / "install_host.py").read_text()
        self.assertNotIn('"' + self.NAME + '"', source)

    def test_an_existing_file_keeps_every_app_and_its_env_block(self):
        self.write({"apps": [{"image-path": "desktop.png", "name": "Desktop"},
                             {"name": "Steam Big Picture", "detached": ["steam"]}],
                    "env": {"PATH": "$(PATH):$(HOME)/.local/bin"}})
        result = self.ensure()
        self.assertTrue(result["changed"])
        self.assertEqual(result["reason"], "appended")
        value = self.read()
        self.assertEqual(value["env"], {"PATH": "$(PATH):$(HOME)/.local/bin"})
        self.assertEqual([app["name"] for app in value["apps"]],
                         ["Desktop", "Steam Big Picture", self.NAME])
        # The pre-existing entries are carried over key for key, not rebuilt.
        self.assertEqual(value["apps"][1], {"name": "Steam Big Picture", "detached": ["steam"]})
        self.assertEqual(value["apps"][-1], {"name": self.NAME, "image-path": "desktop.png"})

    def test_the_first_write_leaves_the_original_behind_and_later_ones_do_not(self):
        self.write({"apps": [{"name": "Desktop"}], "env": {}})
        before = self.path.read_text()
        self.assertTrue(self.ensure()["backup"])
        self.assertEqual(self.backup.read_text(), before)
        # A second run that does change something must not overwrite the first
        # backup, which is the only copy of what the host looked like.
        self.write({"apps": [{"name": "Desktop"}], "env": {}})
        self.assertFalse(self.ensure()["backup"])
        self.assertEqual(self.backup.read_text(), before)

    def test_a_host_with_no_apps_file_gets_one_holding_just_this_app(self):
        result = self.ensure()
        self.assertTrue(result["changed"])
        self.assertEqual(result["reason"], "created")
        self.assertFalse(result["backup"])
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.read(), {"apps": [{"name": self.NAME, "image-path": "desktop.png"}]})

    def test_an_entry_that_is_already_there_is_not_written_again(self):
        self.write({"apps": [{"image-path": "desktop.png", "name": "Desktop"},
                             {"image-path": "omodachi.png", "name": self.NAME}],
                    "env": {"PATH": "$(PATH)"}})
        before, stamp = self.path.read_text(), self.path.stat().st_mtime_ns
        result = self.ensure()
        self.assertFalse(result["changed"])
        self.assertEqual(result["reason"], "already_published")
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(self.path.stat().st_mtime_ns, stamp)
        # An entry the user pointed at their own image keeps that image.
        self.assertFalse(self.backup.exists())

    def test_running_twice_changes_the_file_once(self):
        self.write({"apps": [{"name": "Desktop"}], "env": {}})
        self.assertTrue(self.ensure()["changed"])
        after = self.path.read_text()
        self.assertFalse(self.ensure()["changed"])
        self.assertEqual(self.path.read_text(), after)

    def test_a_file_the_installer_cannot_read_is_reported_not_replaced(self):
        self.path.write_text("{ this is not json")
        result = self.ensure()
        self.assertFalse(result["changed"])
        self.assertEqual(result["reason"], "unreadable")
        self.assertEqual(self.path.read_text(), "{ this is not json")
        self.assertFalse(self.backup.exists())

    def test_a_file_that_is_not_the_shape_sunshine_writes_is_left_alone(self):
        self.write({"apps": {"name": "Desktop"}})
        before = self.path.read_text()
        result = self.ensure()
        self.assertFalse(result["changed"])
        self.assertEqual(result["reason"], "unexpected_shape")
        self.assertEqual(self.path.read_text(), before)

    def test_no_half_written_file_is_left_next_to_the_real_one(self):
        self.write({"apps": [], "env": {}})
        self.ensure()
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()),
                         ["apps.json", "apps.json.omodachi-bak"])



class SunshineAppRemovalTests(unittest.TestCase):
    """The apps.json append has to be undoable, and only that append."""

    NAME = "Omodachi Desktop"

    def _home(self, scratch, document):
        home = Path(scratch)
        path = home / install_host.SUNSHINE_APPS
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(document, indent=4, sort_keys=True) + "\n")
        return home, path

    def test_our_entry_goes_and_every_other_app_and_key_stays(self):
        with tempfile.TemporaryDirectory() as scratch:
            document = {"env": {"PATH": "/usr/bin"},
                        "apps": [{"name": "Desktop"}, {"name": self.NAME, "image-path": "desktop.png"},
                                 {"name": "Steam", "detached": ["steam"]}]}
            home, path = self._home(scratch, document)
            result = install_host.remove_sunshine_app(home, self.NAME)
            self.assertTrue(result["changed"])
            after = json.loads(path.read_text())
            self.assertEqual([app["name"] for app in after["apps"]], ["Desktop", "Steam"])
            self.assertEqual(after["env"], {"PATH": "/usr/bin"})
            self.assertEqual(after["apps"][1]["detached"], ["steam"])

    def test_a_second_removal_changes_nothing(self):
        with tempfile.TemporaryDirectory() as scratch:
            home, path = self._home(scratch, {"apps": [{"name": self.NAME}, {"name": "Desktop"}]})
            install_host.remove_sunshine_app(home, self.NAME)
            body = path.read_text()
            result = install_host.remove_sunshine_app(home, self.NAME)
            self.assertFalse(result["changed"])
            self.assertEqual(result["reason"], "not_published")
            self.assertEqual(path.read_text(), body)

    def test_a_file_that_held_only_our_entry_and_no_original_goes(self):
        # RELEASE-9: ensure_sunshine_app created it (it keeps no backup then).
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            install_host.ensure_sunshine_app(home, self.NAME)
            path = home / install_host.SUNSHINE_APPS
            self.assertEqual(install_host.remove_sunshine_app(home, self.NAME)["reason"],
                             "removed_file_we_created")
            self.assertFalse(path.exists())

    def test_the_backup_goes_only_when_it_says_the_same_thing(self):
        with tempfile.TemporaryDirectory() as scratch:
            home, path = self._home(scratch, {"apps": [{"name": "Desktop"}, {"name": self.NAME}]})
            backup = home / install_host.SUNSHINE_APPS_BACKUP
            backup.write_text(json.dumps({"apps": [{"name": "Desktop"}]}) + "\n")
            self.assertTrue(install_host.remove_sunshine_app(home, self.NAME)["backup_removed"])
            self.assertFalse(backup.exists())

    def test_a_backup_that_holds_something_else_is_kept(self):
        with tempfile.TemporaryDirectory() as scratch:
            home, path = self._home(scratch, {"apps": [{"name": "Desktop"}, {"name": self.NAME}]})
            backup = home / install_host.SUNSHINE_APPS_BACKUP
            backup.write_text(json.dumps({"apps": [{"name": "Something the user had"}]}) + "\n")
            result = install_host.remove_sunshine_app(home, self.NAME)
            self.assertTrue(result["changed"])
            self.assertFalse(result["backup_removed"])
            self.assertTrue(backup.is_file())

    def test_a_file_that_is_not_the_shape_sunshine_writes_is_left_alone(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            path = home / install_host.SUNSHINE_APPS
            path.parent.mkdir(parents=True)
            path.write_text("this is not json\n")
            result = install_host.remove_sunshine_app(home, self.NAME)
            self.assertFalse(result["changed"])
            self.assertEqual(path.read_text(), "this is not json\n")


class PluginCredentialTests(unittest.TestCase):
    """INSTALL-1. The panel is a device and needs a device credential.

    On this developer host the file was made by hand once, years of specs ago,
    so nothing noticed that no step in the product could make one. A clean
    machine went `omarchy plugin add` -> Install -> a running daemon -> and a
    panel that said "This panel needs permission from the host service" for
    ever, with no way forward from inside the product.
    """

    def test_a_fresh_install_issues_the_credential_the_panel_reads(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            (home / ".config/omodachi").mkdir(parents=True)
            result = install_host.ensure_plugin_credential(ROOT, home)
            self.assertTrue(result["issued"], result)
            self.assertEqual(result["device_id"], protocol.PLUGIN_DEVICE_ID)
            path = home / install_host.PLUGIN_TOKEN
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            # Exactly what plugin_bridge accepts, verified by the same secret
            # the daemon will load - not a token this test made up.
            token = plugin_bridge.plugin_credential({"OMODACHI_TOKEN_FILE": str(path)})
            authority = DeviceAuthenticator.from_file(home / ".config/omodachi/device.secret")
            self.assertEqual(authority.verify(token), protocol.PLUGIN_DEVICE_ID)

    def test_a_second_install_never_replaces_the_credential_it_has(self):
        # Re-issuing would hand the panel a second identity and leave the first
        # in `devices list` for ever, on every update.
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            (home / ".config/omodachi").mkdir(parents=True)
            first = install_host.ensure_plugin_credential(ROOT, home)
            body = (home / install_host.PLUGIN_TOKEN).read_text()
            second = install_host.ensure_plugin_credential(ROOT, home)
            self.assertTrue(first["issued"])
            self.assertFalse(second["issued"])
            self.assertEqual(second["reason"], "already_present")
            self.assertEqual((home / install_host.PLUGIN_TOKEN).read_text(), body)

    def test_a_failure_is_reported_and_leaves_nothing_half_written(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            # A directory where the secret belongs: `from_file` cannot read it,
            # and the installer has to say so rather than raise out of the run.
            (home / ".config/omodachi/device.secret").mkdir(parents=True)
            result = install_host.ensure_plugin_credential(ROOT, home)
            self.assertFalse(result["issued"])
            self.assertNotEqual(result["reason"], "already_present")
            self.assertFalse((home / install_host.PLUGIN_TOKEN).exists())
            self.assertFalse((home / (install_host.PLUGIN_TOKEN + ".new")).exists())


if __name__ == "__main__":
    unittest.main()


class OmarchySurfaceTests(unittest.TestCase):
    """The three files Omodachi owns under ~/.config/omarchy, and their removal.

    Nothing else in that tree may be written, and `--remove` must take back
    exactly what was installed - including when somebody else's hook happens to
    carry the same name.
    """

    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.calls = []
        self.original = install_host.run
        install_host.run = lambda argv, **kwargs: self.calls.append(list(argv)) or _Completed()
        self.addCleanup(lambda: setattr(install_host, "run", self.original))

    def test_the_template_is_ours_alone_and_is_written_once(self):
        first = install_host.ensure_theme_template(self.home, ROOT)
        self.assertTrue(first["changed"])
        target = self.home / ".config/omarchy/themed/omodachi-theme.json.tpl"
        self.assertEqual(target.read_text(),
                         (ROOT / "src/omodachi_core/data/omodachi-theme.json.tpl").read_text())
        self.assertFalse(install_host.ensure_theme_template(self.home, ROOT)["changed"])
        # Placeholders only: the template can never carry one theme's colours.
        self.assertNotIn("#", target.read_text())

    def test_hooks_are_installed_with_omarchy_own_installer(self):
        import shutil
        original = shutil.which
        shutil.which = lambda name: "/usr/bin/" + name if name == "omarchy-hook-install" else None
        self.addCleanup(lambda: setattr(shutil, "which", original))
        result = install_host.ensure_hooks(self.home)
        self.assertEqual(sorted(result), ["font-set", "theme-set"])
        for hook, command in (("theme-set", "theme-changed"), ("font-set", "font-changed")):
            master = self.home / ".local/share/omodachi/hooks" / hook / "omodachi"
            self.assertIn(command, master.read_text())
            self.assertIn(install_host.HOOK_MARKER, master.read_text())
            self.assertIn(["/usr/bin/omarchy-hook-install", hook, str(master)], self.calls)

    def test_removal_takes_back_exactly_the_three_files(self):
        install_host.ensure_theme_template(self.home, ROOT)
        rendered = self.home / ".local/state/omarchy/current/theme/omodachi-theme.json"
        rendered.parent.mkdir(parents=True, exist_ok=True)
        rendered.write_text("{}")
        for hook in ("theme-set", "font-set"):
            directory = self.home / ".config/omarchy/hooks" / (hook + ".d")
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "omodachi").write_text(install_host.hook_script(hook))
            (directory / "touchbar").write_text("#!/bin/bash\nsomebody else's hook\n")
        (self.home / ".config/omarchy/themed/alacritty.toml.tpl.sample").write_text("sample")
        removed = install_host.remove_omarchy_surfaces(self.home)
        self.assertEqual(removed, {"template": True, "rendered": True,
                                   "hooks": {"font-set": "removed", "theme-set": "removed"}})
        for hook in ("theme-set", "font-set"):
            directory = self.home / ".config/omarchy/hooks" / (hook + ".d")
            self.assertEqual([path.name for path in directory.iterdir()], ["touchbar"])
        self.assertEqual([path.name for path in (self.home / ".config/omarchy/themed").iterdir()],
                         ["alacritty.toml.tpl.sample"])
        self.assertFalse(rendered.exists())

    def test_a_hook_of_the_same_name_that_is_not_ours_is_left_alone(self):
        directory = self.home / ".config/omarchy/hooks/theme-set.d"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "omodachi").write_text("#!/bin/bash\nwritten by somebody else\n")
        removed = install_host.remove_omarchy_surfaces(self.home)
        self.assertEqual(removed["hooks"], {"font-set": "absent", "theme-set": "not_ours"})
        self.assertTrue((directory / "omodachi").is_file())

    def test_an_already_rendered_theme_is_not_re_applied(self):
        rendered = self.home / ".local/state/omarchy/current/theme/omodachi-theme.json"
        rendered.parent.mkdir(parents=True, exist_ok=True)
        rendered.write_text("{}")
        result = install_host.render_current_theme(self.home)
        self.assertEqual(result["reason"], "already_present")
        self.assertEqual(self.calls, [])

    def test_a_host_with_no_current_theme_is_reported_not_guessed(self):
        import shutil
        original = shutil.which
        shutil.which = lambda name: "/usr/bin/" + name
        self.addCleanup(lambda: setattr(shutil, "which", original))
        self.assertEqual(install_host.render_current_theme(self.home)["reason"], "no_current_theme")
        self.assertEqual(self.calls, [])


class _Completed:
    returncode = 0
    stdout = ""
    stderr = ""


class DaemonUnitTests(unittest.TestCase):
    """AUTH-2: the unit and the daemon have to name the same socket.

    The installer writes `%t/omodachi/omodachid.sock` into the unit; the daemon
    computes `$XDG_RUNTIME_DIR/omodachi/omodachid.sock` for itself; the PAM
    config is written from the same helper. If those three ever drift, the
    symptom is a polkit prompt that quietly asks for the password forever, so
    the agreement is asserted rather than assumed.
    """

    @staticmethod
    def _arguments(unit):
        """The ExecStart line and its continuations, without the comments."""
        rows, collecting = [], False
        for row in unit.splitlines():
            if row.startswith("ExecStart="):
                collecting = True
            if collecting:
                rows.append(row)
                if not row.rstrip().endswith("\\"):
                    break
        return "\n".join(rows)

    def test_the_unit_names_no_socket_at_all_and_owns_the_fallback_directory(self):
        unit = install_host.DAEMON_UNIT
        # One decision point. A path in the unit is a second copy of a value
        # the daemon, the client and the root-owned pam.conf all have to agree
        # on, and AUTH-1 shipped a broken feature because two of them did not.
        self.assertNotIn("--socket", self._arguments(unit))
        self.assertNotIn(".cache/omodachi/omodachid.sock", unit)
        self.assertIn("RuntimeDirectory=omodachi\n", unit)
        self.assertIn("RuntimeDirectoryMode=0700\n", unit)

    def test_the_installer_points_pam_at_the_shared_directory_not_the_runtime_one(self):
        from omodachi_core import runtime_paths
        # `install_pam` has to name /run/omodachi/<uid> even before it exists,
        # because the same run creates it. Anything under /run/user would be
        # invisible to the sandbox whatever the drop-in said.
        socket = str(runtime_paths.shared_socket_dir(1000) / runtime_paths.SOCKET_NAME)
        self.assertEqual(socket, "/run/omodachi/1000/omodachid.sock")
        source = Path(install_host.__file__).read_text()
        self.assertIn("paths.shared_socket_dir() / paths.SOCKET_NAME", source)

    def test_the_plugin_copy_of_the_installer_has_not_drifted(self):
        # There are two copies of this unit on this machine. The plugin's is an
        # untracked local dev copy (its whole `tools/` directory is gitignored),
        # which makes it exactly the kind of file that goes stale - and a stale
        # one is how a deploy from that checkout silently moves the socket back
        # into the home cache polkit's sandbox cannot see. Skipped where the
        # plugin checkout is not beside this one.
        other = ROOT.parent / "omodachi-plugin/plugins/com.omodachi.host/tools/install_host.py"
        if not other.is_file():
            self.skipTest("../omodachi-plugin is not checked out beside this one")
        body = other.read_text()
        unit = body.split('DAEMON_UNIT = """', 1)[1].split('"""', 1)[0]
        self.assertNotIn("--socket", self._arguments(unit))
        self.assertIn("RuntimeDirectory=omodachi\n", unit)
        self.assertNotIn(".cache/omodachi/omodachid.sock", body)


class PamSudoTests(unittest.TestCase):
    """RELEASE-3b §2: `--pam` / `--remove-pam` in a visible terminal ask for the
    password once with `sudo -v`; without a terminal they stay `sudo -n` only."""

    class Terminal:
        def __init__(self, tty):
            self.tty = tty

        def isatty(self):
            return self.tty

    def run_step(self, step, *, tty, validate_code=0):
        import contextlib
        import io
        from unittest import mock
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((list(argv), kwargs))
            code = validate_code if list(argv) == ["sudo", "-v"] else 0
            return type("R", (), {"returncode": code, "stdout": "", "stderr": ""})()

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(install_host.subprocess, "run", fake_run), \
                mock.patch.object(install_host.shutil, "which", return_value="/usr/bin/sudo"), \
                mock.patch.object(install_host, "run") as systemctl, \
                mock.patch.object(install_host.sys, "stdin", self.Terminal(tty)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = step(ROOT)
        return code, calls, out.getvalue(), err.getvalue()

    def test_remove_pam_in_a_terminal_validates_first_then_runs_non_interactively(self):
        code, calls, out, _ = self.run_step(install_host.remove_pam, tty=True)
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][0], ["sudo", "-v"])
        # The prompt needs the terminal: nothing captured on the validate call.
        self.assertNotIn("capture_output", calls[0][1])
        self.assertEqual(calls[1][0][:2], ["sudo", "-n"])
        self.assertEqual(calls[1][0][-1], "remove")
        self.assertEqual(len(calls), 2)
        self.assertIn("+ sudo -v", out)

    def test_remove_pam_without_a_terminal_is_sudo_n_only(self):
        code, calls, _, _ = self.run_step(install_host.remove_pam, tty=False)
        self.assertEqual(code, 0)
        self.assertEqual([argv for argv, _ in calls], [calls[0][0]])
        self.assertEqual(calls[0][0][:2], ["sudo", "-n"])
        self.assertNotIn(["sudo", "-v"], [argv for argv, _ in calls])

    def test_install_pam_in_a_terminal_validates_first(self):
        code, calls, _, _ = self.run_step(install_host.install_pam, tty=True)
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][0], ["sudo", "-v"])
        self.assertEqual(calls[1][0][:2], ["sudo", "-n"])
        self.assertIn("install", calls[1][0])

    def test_install_pam_without_a_terminal_is_sudo_n_only(self):
        code, calls, _, _ = self.run_step(install_host.install_pam, tty=False)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][:2], ["sudo", "-n"])

    def test_a_refused_password_changes_nothing(self):
        code, calls, _, err = self.run_step(install_host.remove_pam, tty=True, validate_code=1)
        self.assertEqual(code, 1)
        self.assertEqual([argv for argv, _ in calls], [["sudo", "-v"]])
        self.assertIn("sudo -v failed", err)

    def test_the_remove_advice_names_the_isolated_command(self):
        source = Path(install_host.__file__).read_text()
        self.assertIn('str(share / "src/scripts/install_host.py") + " --local --remove-pam', source)
        self.assertIn('python3 -I -B "', source)


class PinnedSunshineTests(unittest.TestCase):
    """CORE-2 §4 + RELEASE-9: a fork already on disk is trusted as the pinned
    build only when it is byte for byte the pinned archive's contents and the
    unit that starts it is ours; then nothing is downloaded."""

    def run_install(self, present, *, pristine="", **options):
        import contextlib
        import io
        from unittest import mock
        from omodachi_core import sunshine_package
        out, err = io.StringIO(), io.StringIO()
        unit = {"restarted": False, "enabled": True, "active": "active", "changed": False,
                "web_locked": True, "strangers": []}
        with mock.patch.object(sunshine_package, "installed_fork", return_value=present), \
                mock.patch.object(sunshine_package, "pristine", return_value=pristine) as check, \
                mock.patch.object(sunshine_package, "ensure_unit", return_value=unit) as ensure, \
                mock.patch.object(sunshine_package, "install",
                                  side_effect=sunshine_package.SunshinePackageError(
                                      "sunshine_package_unreachable", "would have downloaded")) as install, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = install_host.install_sunshine(ROOT, **options)
        self.check, self.ensure = check, ensure
        return result, out.getvalue(), err.getvalue(), install

    OURS = {"version": "328d2313c92dc4db675a8eafe96a7e32460c2758",
            "binary": "/home/u/.local/share/omodachi/sunshine/328d231/sunshine",
            "directory": "/home/u/.local/share/omodachi/sunshine/328d231",
            "arguments": ["capture=wlr", "origin_web_ui_allowed=pc"],
            "written_by_installer": True, "enabled": "enabled", "active": "active"}

    def test_the_pinned_tree_under_our_unit_is_kept_and_nothing_is_downloaded(self):
        result, out, err, install = self.run_install(self.OURS)
        self.assertEqual((result["installed"], result["reason"], result["pinned"]),
                         (False, "already_installed", "328d231"))
        install.assert_not_called()
        # The manifest checked is the one versions.json pins, not the tree's own word.
        self.assertEqual(self.check.call_args.args[1],
                         "701043786fea2a92f66c7a4ac9e419072c80bb530c7e026157d3d88934cdee68")
        # The unit is still brought to what this core writes, without a restart
        # unless that changed it, and keeping the encoder arguments it had.
        self.assertEqual(self.ensure.call_args.kwargs["restart"], False)
        self.assertEqual(self.ensure.call_args.kwargs["arguments"], self.OURS["arguments"])
        self.assertIn("nothing was downloaded", out)
        self.assertEqual(err, "")

    def test_a_tree_that_only_names_the_pin_is_replaced_by_the_pinned_archive(self):
        # RELEASE-9: a SOURCE file or a directory name is not a checksum.
        result, out, _, install = self.run_install(
            self.OURS, pristine="its MANIFEST.sha256 is not the one the pinned archive carries")
        self.assertEqual(install.call_args.args[0].rsplit("/", 1)[-1],
                         "omodachi-sunshine-328d231-x86_64.tar.zst")
        self.assertRegex(install.call_args.kwargs["sha256"], "^0f5a8f0b")
        self.assertIn("is not trusted as the pinned build", out)
        self.ensure.assert_not_called()

    def test_a_drop_in_this_installer_did_not_write_is_never_trusted(self):
        present = dict(self.OURS, written_by_installer=False)
        result, out, _, install = self.run_install(present)
        install.assert_called_once()
        self.assertIn("was not written by this installer", out)

    def test_a_listed_stand_in_is_not_the_pinned_archive_and_is_replaced(self):
        from unittest import mock
        from omodachi_core import sunshine_package
        present = dict(self.OURS, version="17c6043")
        pin = dict(sunshine_package.pinned(), satisfied_by=["17c6043"])
        with mock.patch.object(sunshine_package, "pinned", return_value=pin):
            result, out, _, install = self.run_install(
                present, pristine="its MANIFEST.sha256 is not the one the pinned archive carries")
        install.assert_called_once()

    def test_a_fork_from_before_hevc_is_replaced_by_the_pin(self):
        for version in ("17c6043", "e58627aa73d9bc42d95f121415b96a9a0b3aa0be"):
            present = dict(self.OURS, version=version)
            result, out, err, install = self.run_install(present)
            self.assertEqual(install.call_args.args[0].rsplit("/", 1)[-1],
                             "omodachi-sunshine-328d231-x86_64.tar.zst", version)
            self.assertRegex(install.call_args.kwargs["sha256"], "^0f5a8f0b")

    def test_an_older_fork_or_an_explicit_archive_is_installed(self):
        older = dict(self.OURS, version="a2fd635")
        result, out, err, install = self.run_install(older)
        self.assertEqual(install.call_args.args[0].rsplit("/", 1)[-1], "omodachi-sunshine-328d231-x86_64.tar.zst")
        self.assertIn("pinned 328d231", out)
        self.assertNotIn("/releases/latest/", out)
        result, out, err, install = self.run_install(self.OURS, spec="latest", sha256="e" * 64)
        self.assertIn("/releases/latest/", install.call_args.args[0])
        self.assertEqual(install.call_args.kwargs["sha256"], "e" * 64)

    def test_a_sunshine_somebody_else_set_up_is_reported_and_left_alone(self):
        import contextlib
        import io
        from unittest import mock
        from omodachi_core import sunshine_package
        err = io.StringIO()
        with mock.patch.object(sunshine_package, "installed_fork", return_value=None), \
                mock.patch.object(sunshine_package, "install", side_effect=sunshine_package.SunshinePackageError(
                    "sunshine_not_ours", "Sunshine is already installed on this computer")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            result = install_host.install_sunshine(ROOT)
        self.assertEqual(result["reason"], "sunshine_not_ours")
        self.assertIn("Remote's VNC mode still works.", err.getvalue())
        self.assertNotIn("--sunshine-build", err.getvalue())


class SunshineOverrideTests(unittest.TestCase):
    """RELEASE-6: an optional Sunshine override the installer cannot verify is refused.

    An archive other than the pin needs an explicit sha256 (no `.sha256` sidecar,
    no unchecked fallback, `latest` included); a git URL to build needs an exact
    commit, fetched detached and checked right before its script runs. A local
    build path is the user's own tree.
    """

    def setUp(self):
        import os
        import subprocess
        self.subprocess = subprocess
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.home = Path(self.scratch.name) / "home"
        source = self.home / ".local/share/omodachi/src"
        (source / "requirements").mkdir(parents=True)
        (source / "pyproject.toml").write_text("")
        (source / install_host.HOST_LOCK).write_text("")
        (source / "src").symlink_to(ROOT / "src")
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                        GIT_COMMITTER_EMAIL="t@t")

    def install_local(self, **options):
        import contextlib
        import io
        from unittest import mock
        err = io.StringIO()
        with mock.patch.object(Path, "home", return_value=self.home), \
                mock.patch.object(install_host, "run", side_effect=AssertionError("ran something")), \
                mock.patch.dict("os.environ", {"OMODACHI_SUNSHINE_PACKAGE": "", "OMODACHI_SUNSHINE_SHA256": ""}), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = install_host.install_local(firewall=False, vnc=False, **options)
        return code, err.getvalue()

    def assert_refused_before_anything(self, needle, **options):
        code, err = self.install_local(**options)
        self.assertEqual(code, 2, err)
        self.assertIn("refusing to install", err)
        self.assertIn(needle, err)
        # nothing was created: the refusal comes before the first directory
        self.assertFalse((self.home / ".config/omodachi").exists())

    def test_an_archive_url_or_path_without_a_sha256_is_refused(self):
        for spec in ("https://example.invalid/omodachi-sunshine-x-x86_64.tar.zst",
                     "/tmp/omodachi-sunshine-x-x86_64.tar.zst", "latest"):
            with self.subTest(spec=spec):
                self.assert_refused_before_anything("--sunshine-sha256", sunshine_package=spec)

    def test_the_environment_override_without_a_sha256_is_refused(self):
        import contextlib
        import io
        from unittest import mock
        stderr = io.StringIO()
        with mock.patch.object(Path, "home", return_value=self.home), \
                mock.patch.object(install_host, "run", side_effect=AssertionError("ran something")), \
                mock.patch.dict("os.environ", {"OMODACHI_SUNSHINE_PACKAGE": "http://jump/x.tar.zst",
                                               "OMODACHI_SUNSHINE_SHA256": ""}), \
                contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = install_host.install_local(firewall=False, vnc=False)
        self.assertEqual(code, 2)
        self.assertIn("OMODACHI_SUNSHINE_SHA256", stderr.getvalue())

    def test_a_malformed_or_conflicting_sha256_is_refused(self):
        self.assert_refused_before_anything("not a sha256", sunshine_package="/tmp/x.tar.zst",
                                            sunshine_sha256="abc")
        self.assert_refused_before_anything("pinned archive must hash to", sunshine_sha256="d" * 64)

    def test_a_git_url_build_without_an_exact_commit_is_refused(self):
        self.assert_refused_before_anything("--sunshine-build-commit",
                                            sunshine_build="https://example.invalid/fork.git")
        self.assert_refused_before_anything("--sunshine-build-commit",
                                            sunshine_build="git@example.invalid:fork.git")
        self.assert_refused_before_anything("not a full 40-character commit",
                                            sunshine_build="https://example.invalid/fork.git",
                                            sunshine_build_commit="328d231")
        self.assert_refused_before_anything("only goes with", sunshine_build_commit="a" * 40)
        self.assert_refused_before_anything("only goes with a git URL",
                                            sunshine_build="/home/u/omodachi-sunshine",
                                            sunshine_build_commit="a" * 40)

    def test_a_local_build_path_and_the_plain_pin_are_not_refused(self):
        source = self.home / ".local/share/omodachi/src"
        self.assertEqual(install_host.sunshine_override_refusal(source, build="/home/u/fork"), "")
        self.assertEqual(install_host.sunshine_override_refusal(source), "")
        self.assertEqual(install_host.sunshine_override_refusal(
            source, spec="/tmp/x.tar.zst", sha256="f" * 64), "")

    # --- the git URL build path, against a real repository ------------------

    def git(self, *args, cwd):
        return self.subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True,
                                   capture_output=True, text=True).stdout.strip()

    def fork(self):
        upstream = Path(self.scratch.name) / "fork"
        (upstream / "scripts").mkdir(parents=True)
        (upstream / "scripts/package_release.sh").write_text(
            'mkdir -p "$OMODACHI_PACKAGE_OUT"; echo built > "$OMODACHI_PACKAGE_OUT/ran"\n'
            'git -C "$(dirname "$0")/.." rev-parse HEAD > "$OMODACHI_PACKAGE_OUT/omodachi-sunshine-x-x86_64.tar.zst"\n')
        self.git("init", "-q", cwd=upstream)
        self.git("add", ".", cwd=upstream)
        self.git("commit", "-qm", "one", cwd=upstream)
        first = self.git("rev-parse", "HEAD", cwd=upstream)
        (upstream / "scripts/package_release.sh").write_text("echo branch head > /dev/null\nexit 3\n")
        self.git("commit", "-qam", "two", cwd=upstream)
        return upstream, first

    def build(self, url, commit):
        import contextlib
        import io
        from unittest import mock
        out = Path(self.scratch.name) / "dist"
        err = io.StringIO()
        with mock.patch.object(Path, "home", return_value=self.home), \
                mock.patch.dict("os.environ", dict(self.env, OMODACHI_PACKAGE_OUT=str(out))), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            result = install_host.build_sunshine(url, commit=commit)
        return result, out, err.getvalue()

    def test_a_git_url_is_built_at_exactly_the_pinned_commit_not_its_head(self):
        upstream, first = self.fork()
        result, out, err = self.build(upstream.as_uri(), first)
        self.assertTrue(result["built"], err)
        self.assertEqual(Path(result["archive"]).read_text().strip(), first)
        cache = self.home / install_host.SUNSHINE_BUILD_CACHE
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=cache), first)

    def test_a_commit_the_url_does_not_have_is_refused_and_nothing_runs(self):
        upstream, _ = self.fork()
        result, out, err = self.build(upstream.as_uri(), "b" * 40)
        self.assertEqual(result, {"built": False, "reason": "sunshine_build_unverified"})
        self.assertFalse((out / "ran").exists())

    def test_the_last_check_catches_a_tree_that_is_not_the_commit(self):
        upstream, first = self.fork()
        cache = self.home / install_host.SUNSHINE_BUILD_CACHE
        import contextlib
        import io
        with mock_home(self.home), contextlib.redirect_stdout(io.StringIO()):
            ok, _ = install_host.fetch_sunshine_commit(upstream.as_uri(), first, cache)
        self.verify = lambda commit: self.quiet(install_host.verify_sunshine_checkout, cache, commit)
        self.assertTrue(ok)
        (cache / "scripts/extra.sh").write_text("echo not in the commit\n")
        ok, detail = self.verify(first)
        self.assertFalse(ok)
        self.assertIn("not clean", detail)
        (cache / "scripts/extra.sh").unlink()
        (cache / "scripts/package_release.sh").write_text("echo changed\n")
        self.assertFalse(self.verify(first)[0])
        self.git("checkout", "-q", "--force", "HEAD", cwd=cache)
        self.assertTrue(self.verify(first)[0])
        self.assertIn("not the requested", self.verify("c" * 40)[1])

    @staticmethod
    def quiet(function, *args):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            return function(*args)


def mock_home(home):
    from unittest import mock
    return mock.patch.object(Path, "home", return_value=home)


class RemoveKeepsTheUsersFilesTests(unittest.TestCase):
    """RELEASE-8: --remove and --purge delete what the installer made, and
    nothing else - above all not agent-workspace, the hand-written files in
    ~/.config/omodachi, or a src the plugin's bootstrap cannot show it made."""

    def setUp(self):
        import contextlib
        import io
        from unittest import mock
        self.mock = mock
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        for target, value in ((Path, "home"), (install_host, "run"), (install_host, "remove_firewall")):
            patcher = mock.patch.object(target, value, **(
                {"return_value": self.home} if value == "home" else
                {"new": (lambda *a, **k: 0)} if value == "remove_firewall" else
                {"new": (lambda *a, **k: _Completed())}))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.out = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.out)
        h = self.home
        self.share = h / ".local/share/omodachi"
        self.files = {
            # the user's own
            "workspace": self.share / "agent-workspace/notes/plan.md",
            "menu": h / ".config/omodachi/omodachi-menu.jsonc",
            "menu_backup": h / ".config/omodachi/omodachi-menu.jsonc.codex-bak",
            "runtime": h / ".config/omodachi/desktop-runtime.json",
            "stranger": self.share / "my-scratch/keep.txt",
            "foreign_hook": self.share / "hooks/theme-set/mine.sh",
            # the installer's and the daemon's
            "secret": h / ".config/omodachi/device.secret",
            "cert": h / ".config/omodachi/tls/server.pem",
            "token": h / ".config/omodachi/plugin.token",
            "preferences": h / ".config/omodachi/preferences/state.json",
            "cache": h / ".cache/omodachi/install-status.json",
            "state": h / ".local/state/omodachi/remote/OMODACHI-0123456789abcdef.json",
            "venv": self.share / "venv/bin/python",
            "previous": self.share / "venv.previous/bin/python",
            "src": self.share / "src/pyproject.toml",
        }
        for path in self.files.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n")
        for hook in ("theme-set", "font-set"):
            master = self.share / "hooks" / hook / "omodachi"
            master.parent.mkdir(parents=True, exist_ok=True)
            master.write_text(install_host.hook_script(hook))
        (self.share / "src/.git").mkdir()
        # RELEASE-9: both venvs are this installer's (id inside, record outside).
        ids = []
        for name, identifier in (("venv", "1" * 32), ("venv.previous", "2" * 32)):
            (self.share / name / install_host.VENV_ID_FILE).write_text(identifier + "\n")
            ids.append(identifier)
        install_host._write_venv_ids(h, ids)
        # And no PAM entry on this pretend host, whatever the machine running
        # the tests has in /etc.
        patcher = mock.patch.object(install_host, "pam_present", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def own_source(self, identifier="0123456789abcdef" * 2):
        (self.share / "src/.git/omodachi-install-id").write_text(identifier + "\n")
        record = self.home / ".local/state/omodachi/core-source.json"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"schema": 1, "path": str(self.share / "src"),
                                      "id": identifier, "pending": []}))
        return record

    def remove(self, **kwargs):
        with self.redirect:
            self.assertEqual(install_host.remove_local(sunshine=False, **kwargs), 0)
        return self.out.getvalue()

    def exists(self, *names):
        return {name: self.files[name].exists() for name in names}

    def test_purge_keeps_agent_workspace_and_the_users_config(self):
        self.own_source()
        out = self.remove(purge=True)
        self.assertEqual(self.exists("workspace", "menu", "menu_backup", "runtime", "stranger",
                                     "foreign_hook"), dict.fromkeys(
            ("workspace", "menu", "menu_backup", "runtime", "stranger", "foreign_hook"), True))
        self.assertEqual(self.exists("secret", "cert", "token", "preferences", "cache", "state",
                                     "venv", "previous", "src"), dict.fromkeys(
            ("secret", "cert", "token", "preferences", "cache", "state", "venv", "previous",
             "src"), False))
        self.assertFalse((self.share / "hooks/font-set").exists())
        self.assertIn(str(self.share / "agent-workspace"), out)
        self.assertIn(str(self.files["menu"]), out)
        self.assertEqual(self.files["workspace"].read_text(), "x\n")

    def test_remove_without_purge_keeps_config_and_workspace(self):
        self.own_source()
        out = self.remove()
        self.assertTrue(all(self.exists("workspace", "menu", "secret", "cert", "token", "state",
                                        "stranger", "foreign_hook").values()))
        self.assertFalse(any(self.exists("venv", "previous", "src").values()))
        self.assertIn(str(self.share / "agent-workspace"), out)

    def assert_src_kept(self):
        out = self.remove(purge=True)
        self.assertIn("nothing shows the Omodachi plugin's installer made it", out)
        self.assertTrue(self.files["src"].exists())

    def test_a_src_without_a_record_is_kept(self):
        self.assert_src_kept()

    def test_a_src_with_another_id_is_kept(self):
        self.own_source()
        (self.share / "src/.git/omodachi-install-id").write_text("f" * 32 + "\n")
        self.assert_src_kept()

    def test_a_src_that_is_a_link_is_kept_and_so_is_its_target(self):
        import shutil
        shutil.rmtree(self.share / "src")
        other = self.home / "elsewhere"
        (other / ".git").mkdir(parents=True)
        (other / "pyproject.toml").write_text("x\n")
        (self.share / "src").symlink_to(other)
        self.own_source()
        out = self.remove(purge=True)
        self.assertIn("nothing shows the Omodachi plugin's installer made it", out)
        self.assertTrue((self.share / "src").is_symlink())
        self.assertTrue((other / "pyproject.toml").exists())

    def test_the_record_survives_a_purge_while_the_checkout_it_names_is_kept(self):
        from unittest import mock
        record = self.own_source()
        with mock.patch.object(install_host, "pam_present", return_value=["/etc/omodachi/pam.conf"]), \
                mock.patch.object(install_host, "remove_pam", return_value=1):
            with self.redirect:
                code = install_host.remove_local(sunshine=False, purge=True)
        self.assertEqual(code, install_host.PARTIAL)
        self.assertTrue(self.files["src"].exists(), "the PAM entry keeps the sources")
        self.assertTrue(record.exists())
        self.assertFalse(self.files["state"].exists())
        self.assertNotIn(str(record), self.out.getvalue().split("kept, because")[-1])

    # RELEASE-9 (marketplace #8330, 2026-09-26): --purge deletes only the
    # files the program creates, in every directory it touches.
    def plant(self, relative):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("the user's own\n")
        return path

    def test_purge_keeps_a_file_the_user_put_in_each_directory(self):
        self.own_source()
        made = {relative: self.plant(relative) for relative in (
            ".config/omodachi/pairing.json", ".config/omodachi/tls/.server-abcd1234.pem",
            ".config/omodachi/agent/ws-token", ".cache/omodachi/voice/transcript-0123456789abcdef.txt",
            ".cache/omodachi/sunshine/omodachi-sunshine-328d231-x86_64.tar.zst",
            ".local/state/omodachi/sunshine-unit.json",
            ".local/state/omodachi/remote/vnc/rs_" + "a" * 32 + "/instance.json",
            ".local/state/omodachi/remote/vnc/rs_" + "a" * 32 + "/rfb.sock")}
        planted = [self.plant(relative) for relative in (
            ".config/omodachi/notes.txt", ".config/omodachi/tls/my-ca.pem",
            ".config/omodachi/agent/my-agent.toml", ".config/omodachi/pairing.json.bak",
            ".cache/omodachi/my-cache.bin", ".cache/omodachi/voice/memo.txt",
            ".cache/omodachi/sunshine/my-build.tar.zst",
            ".local/state/omodachi/notes.md", ".local/state/omodachi/remote/my.log")]
        out = self.remove(purge=True)
        for path in planted:
            self.assertEqual(path.read_text(), "the user's own\n", path)
            self.assertIn(str(path), out)
        for relative, path in made.items():
            self.assertFalse(path.exists(), relative)
        # A directory of ours with a stranger in it stays, holding only that.
        self.assertEqual(sorted(p.name for p in (self.home / ".config/omodachi/tls").iterdir()),
                         ["my-ca.pem"])
        self.assertFalse((self.home / ".local/state/omodachi/remote/vnc").exists())

    def test_every_file_a_vnc_session_makes_is_on_the_purge_list(self):
        # RELEASE-9 integration: B3 moved the RFB listener to rfb.sock in the
        # session directory; a daemon that dies mid-session leaves it there.
        from omodachi_core.remote.vnc import ManagedWayVNC
        session = self.home / ".local/state/omodachi/remote/vnc" / ("rs_" + "b" * 32)
        vnc = ManagedWayVNC(session, environment={})
        rules = install_host.PURGE_RULES[".local/state/omodachi"]["remote"]["vnc"]
        known, inner = install_host._rule_for(session.name, rules)
        self.assertTrue(known)
        for path in (vnc.control, vnc.listener, vnc.state_path):
            self.assertTrue(install_host._rule_for(path.name, inner)[0], path.name)

    def test_purge_with_nothing_of_the_users_leaves_no_directory_behind(self):
        self.own_source()
        import shutil
        for name in ("workspace", "menu", "menu_backup", "runtime", "stranger", "foreign_hook"):
            self.files[name].unlink()
        shutil.rmtree(self.share / "agent-workspace")
        shutil.rmtree(self.share / "my-scratch")
        self.remove(purge=True)
        for relative in (".config/omodachi", ".cache/omodachi", ".local/state/omodachi",
                         ".local/share/omodachi"):
            self.assertFalse((self.home / relative).exists(), relative)

    def test_a_link_in_place_of_a_directory_is_not_followed(self):
        self.own_source()
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "server.pem").write_text("not ours\n")
        import shutil
        shutil.rmtree(self.home / ".config/omodachi/tls")
        (self.home / ".config/omodachi/tls").symlink_to(elsewhere)
        self.remove(purge=True)
        self.assertEqual((elsewhere / "server.pem").read_text(), "not ours\n")

    # RELEASE-9: the venvs are this installer's only when it can show it.
    def test_a_venv_this_installer_did_not_make_is_kept(self):
        self.own_source()
        (self.share / "venv" / install_host.VENV_ID_FILE).unlink()
        (self.share / "venv.previous" / install_host.VENV_ID_FILE).write_text("f" * 32 + "\n")
        out = self.remove(purge=True)
        self.assertTrue(self.files["venv"].exists())
        self.assertTrue(self.files["previous"].exists())
        self.assertIn("is not a virtualenv this installer made", out)

    # RELEASE-9: --remove takes back the authorized_keys lines it wrote.
    def test_remove_takes_back_our_authorized_keys_lines_and_nothing_else(self):
        import base64
        self.own_source()
        body = base64.b64encode(b"\0\0\0\x0bssh-ed25519" + b"k" * 36).decode()
        other = base64.b64encode(b"\0\0\0\x0bssh-ed25519" + b"u" * 36).decode()
        keys = self.home / ".ssh/authorized_keys"
        keys.parent.mkdir(mode=0o700)
        keys.write_text(f"ssh-ed25519 {other} me@laptop\n"
                        f"ssh-ed25519 {body} # omodachi:ipad-1\n"
                        f'restrict,pty ssh-ed25519 {body[:-4]}AAAA # omodachi:iphone\n'
                        f"# ssh-ed25519 {body} # omodachi:commented-out\n")
        out = self.remove()
        self.assertEqual(keys.read_text(), f"ssh-ed25519 {other} me@laptop\n"
                                           f"# ssh-ed25519 {body} # omodachi:commented-out\n")
        self.assertIn("removed 2 Omodachi line(s)", out)
        self.assertIn(" ipad-1", out)

    def test_an_authorized_keys_that_cannot_be_read_safely_makes_the_removal_partial(self):
        self.own_source()
        (self.home / ".ssh").mkdir(mode=0o700)
        (self.home / ".ssh/authorized_keys").symlink_to(self.home / "elsewhere-keys")
        with self.redirect:
            code = install_host.remove_local(sunshine=False)
        self.assertEqual(code, install_host.PARTIAL)
        self.assertIn("only PARTLY removed", self.out.getvalue())

    def test_remove_puts_voxtype_back_when_dictation_was_cut_off(self):
        from omodachi_core import voice
        self.assertTrue(install_host.VOXTYPE_BACKUP.endswith(voice.BACKUP_SUFFIX))
        self.own_source()
        config = self.home / install_host.VOXTYPE_CONFIG
        config.parent.mkdir(parents=True)
        config.write_text('[audio]\ndevice = "omodachi_mic"\n')
        (self.home / install_host.VOXTYPE_BACKUP).write_text('[audio]\ndevice = "default"\n')
        self.remove()
        self.assertEqual(config.read_text(), '[audio]\ndevice = "default"\n')
        self.assertFalse((self.home / install_host.VOXTYPE_BACKUP).exists())

    def test_a_voxtype_config_edited_since_dictation_is_kept(self):
        self.own_source()
        config = self.home / install_host.VOXTYPE_CONFIG
        config.parent.mkdir(parents=True)
        config.write_text('[audio]\ndevice = "omodachi_mic"\n[model]\nname = "large"\n')
        backup = self.home / install_host.VOXTYPE_BACKUP
        backup.write_text('[audio]\ndevice = "default"\n')
        out = self.remove()
        self.assertEqual(config.read_text(), '[audio]\ndevice = "omodachi_mic"\n[model]\nname = "large"\n')
        self.assertTrue(backup.exists())
        self.assertIn("it was changed after dictation", out)

    def test_a_unit_file_of_that_name_the_installer_did_not_write_is_kept(self):
        self.own_source()
        unit = self.home / ".config/systemd/user/omodachid.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\nExecStart=/opt/mine\n")
        out = self.remove()
        self.assertEqual(unit.read_text(), "[Service]\nExecStart=/opt/mine\n")
        self.assertIn("it is not a unit this installer wrote", out)

    def test_a_template_somebody_edited_is_kept(self):
        self.own_source()
        template = self.home / install_host.THEMED_DIR / install_host.THEME_TEMPLATE
        template.parent.mkdir(parents=True)
        template.write_text('{"mine": true}\n')
        self.remove()
        self.assertEqual(template.read_text(), '{"mine": true}\n')

    def test_firewall_rules_that_could_not_be_deleted_make_the_removal_partial(self):
        mock = self.mock
        self.own_source()
        with mock.patch.object(install_host, "remove_firewall", return_value=1):
            with self.redirect:
                code = install_host.remove_local(sunshine=False)
        self.assertEqual(code, install_host.PARTIAL)
        self.assertIn("sudo ufw delete", self.out.getvalue())

    # RELEASE-9: an installed PAM entry is removed as part of --remove.
    def test_remove_runs_the_pam_root_step_and_finishes_when_it_worked(self):
        mock = self.mock
        self.own_source()
        answers = [["/etc/omodachi/pam.conf"], []]
        with mock.patch.object(install_host, "pam_present", side_effect=lambda: answers.pop(0)), \
                mock.patch.object(install_host, "remove_pam", return_value=0) as step:
            out = self.remove()
        step.assert_called_once_with(install_host.ROOT)
        self.assertFalse(self.files["src"].exists())
        self.assertNotIn("PARTLY", out)

    def test_remove_says_partial_and_fails_when_the_pam_entry_stays(self):
        mock = self.mock
        self.own_source()
        with mock.patch.object(install_host, "pam_present", return_value=["/etc/pam.d/sudo"]), \
                mock.patch.object(install_host, "remove_pam", return_value=1):
            with self.redirect:
                code = install_host.remove_local(sunshine=False)
        out = self.out.getvalue()
        self.assertEqual(code, install_host.PARTIAL)
        self.assertIn("only PARTLY removed", out)
        self.assertIn("--local --remove-pam", out)
        self.assertTrue(self.files["src"].exists(), "the PAM step runs out of the sources")


class Release7bIsolationTests(unittest.TestCase):
    """RELEASE-7b: every Python core starts - the root PAM step, the venv and pip,
    the daemon unit, the ~/.local/bin wrappers, the app scanner and the PAM helper -
    ignores PYTHON*, user site-packages and the directories around it.

    The functional tests plant code where a default interpreter picks it up (a
    PYTHONPATH sitecustomize, a user-site .pth, a json.py in the working directory)
    and show it running under the plain interpreter first (the control), then
    not running under the exact flags core now uses.
    """

    PYTHON = getattr(__import__("sys"), "_base_executable", None) or __import__("sys").executable

    def setUp(self):
        import os
        import subprocess
        self.scratch = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.scratch, True)
        self.marker = self.scratch / "markers"
        plant = f"import os\nopen({str(self.marker)!r}, 'a').write('%s\\n')\n"
        evil = self.scratch / "evil-path"
        evil.mkdir()
        (evil / "sitecustomize.py").write_text(plant % "pythonpath-sitecustomize")
        self.cwd = self.scratch / "cwd"
        self.cwd.mkdir()
        (self.cwd / "json.py").write_text(plant % "cwd-json" + "from importlib import import_module as _i\n")
        userbase = self.scratch / "userbase"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
        self.env.update({"PYTHONPATH": str(evil), "PYTHONUSERBASE": str(userbase),
                         "PYTHONSTARTUP": str(evil / "sitecustomize.py"),
                         "PYTHONPYCACHEPREFIX": str(self.scratch / "evil-cache")})
        site = subprocess.run([self.PYTHON, "-c", "import site; print(site.getusersitepackages())"],
                              env=self.env, capture_output=True, text=True, check=True).stdout.strip()
        Path(site).mkdir(parents=True)
        (Path(site) / "zz-r7b.pth").write_text(
            f"import os; open({str(self.marker)!r}, 'a').write('user-site-pth\\n')\n")

    def markers(self):
        return sorted(set(self.marker.read_text().split())) if self.marker.exists() else []

    def run_python(self, argv, **kwargs):
        import subprocess
        return subprocess.run(argv, env=kwargs.pop("env", self.env), cwd=kwargs.pop("cwd", self.cwd),
                              capture_output=True, text=True, timeout=60, **kwargs)

    # -- the root PAM step ---------------------------------------------------

    def pam_root(self):
        root = self.scratch / "root"
        (root / "etc/pam.d").mkdir(parents=True)
        (root / "usr/lib/pam.d").mkdir(parents=True)
        (root / "etc/pam.d/sudo").write_text("#%PAM-1.0\nauth\t\tinclude\t\tsystem-auth\n")
        return root

    def test_root_runs_the_pam_step_isolated_from_code_it_is_handed_as_bytes(self):
        command = install_host.pam_command(["remove"])
        self.assertEqual(command[:7], ["sudo", "-n", "/usr/bin/python3", "-I", "-B", "-c",
                                       install_host.PAM_LOADER])
        self.assertEqual(command[7:], ["remove"])
        # No path of the checkout is in what root runs.
        self.assertFalse([part for part in command if "omodachi_core" in part or part.endswith(".py")])
        self.assertIn('dir="/tmp"', install_host.PAM_LOADER)

    def test_the_pam_step_hands_root_the_two_files_byte_for_byte(self):
        from unittest import mock
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((list(argv), kwargs))
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with mock.patch.object(install_host.subprocess, "run", fake_run), \
                mock.patch.object(install_host.shutil, "which", return_value="/usr/bin/sudo"), \
                mock.patch.object(install_host, "_visible_terminal", return_value=False), \
                __import__("contextlib").redirect_stdout(__import__("io").StringIO()):
            self.assertEqual(install_host.remove_pam(ROOT), 0)
        argv, kwargs = calls[0]
        self.assertEqual(argv, install_host.pam_command(["remove"]))
        files = json.loads(kwargs["input"])
        self.assertEqual(sorted(files), ["pam_enroll.py", "pam_helper.py", "pam_install.py"])
        for name, text in files.items():
            self.assertEqual(text.encode(), (ROOT / "src/omodachi_core" / name).read_bytes())

    def test_the_pam_loader_runs_pam_install_from_its_own_copy_and_plants_nothing(self):
        # The payload is read from a copy that is gone before the loader runs:
        # whatever the loader executes or installs came through stdin.
        copy = self.scratch / "checkout"
        (copy / "src").mkdir(parents=True)
        __import__("shutil").copytree(ROOT / "src/omodachi_core", copy / "src/omodachi_core")
        payload = install_host.pam_payload(copy)
        __import__("shutil").rmtree(copy)
        arguments = ["install", "--root", str(self.pam_root()), "--owner", "alex",
                     "--socket", "/run/omodachi/1000/omodachid.sock", "--services", "sudo"]
        before = set(Path("/tmp").glob("omodachi-pam-*"))

        # Control: the same loader under a plain interpreter runs all three plants.
        control = self.run_python([self.PYTHON, "-c", install_host.PAM_LOADER, *arguments], input=payload)
        self.assertEqual(self.markers(), ["cwd-json", "pythonpath-sitecustomize", "user-site-pth"],
                         control.stderr)
        self.marker.unlink()

        flags = install_host.pam_command(arguments)[3:]
        result = self.run_python([self.PYTHON, *flags], input=payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertEqual(self.markers(), [])
        helper = self.scratch / "root/usr/local/bin/omodachi-pam"
        self.assertEqual(helper.read_bytes(), (ROOT / "src/omodachi_core/pam_helper.py").read_bytes())
        self.assertEqual(set(Path("/tmp").glob("omodachi-pam-*")), before)
        self.assertEqual(list(self.cwd.glob("**/__pycache__")), [])

    def test_the_pam_helper_starts_isolated(self):
        helper = ROOT / "src/omodachi_core/pam_helper.py"
        self.assertEqual(helper.read_text().splitlines()[0], "#!/usr/bin/python3 -IB")
        arguments = [str(helper), "--config", str(self.scratch / "absent.conf")]
        env = {**self.env, "PAM_TYPE": "auth"}
        self.run_python([self.PYTHON, *arguments], env=env)
        self.assertIn("pythonpath-sitecustomize", self.markers())
        self.marker.unlink()
        result = self.run_python([self.PYTHON, "-IB", *arguments], env=env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.markers(), [])

    # -- the venv, the daemon unit and the wrappers ---------------------------

    def test_python_children_get_no_caller_python_variables(self):
        from unittest import mock
        with mock.patch.dict(__import__("os").environ, self.env, clear=True):
            with mock.patch.object(install_host.sys, "pycache_prefix", None):
                plain = install_host.python_environment()
            with mock.patch.object(install_host.sys, "pycache_prefix", "/tmp/omodachi-core-x/bytecode"):
                prefixed = install_host.python_environment()
        python = lambda env: {k: v for k, v in env.items() if k.startswith("PYTHON")}
        self.assertEqual(python(plain), {"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        # The bootstrap's -X pycache_prefix is this interpreter's own, and carried on.
        self.assertEqual(python(prefixed), {"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                                            "PYTHONPYCACHEPREFIX": "/tmp/omodachi-core-x/bytecode"})
        self.assertEqual(plain["PATH"], self.env["PATH"])

    def test_the_desktop_entry_step_and_the_remote_install_start_python_isolated(self):
        source = Path(install_host.__file__).read_text()
        self.assertIn('run([str(venv / "bin/python"), "-I", str(venv / "bin/omodachi-host"), '
                      '"desktop-entry", "install"],\n        env=python_environment())', source)
        self.assertIn('remote = f"python3 -I -B {REMOTE_SOURCE}/scripts/install_host.py --local"', source)

    @staticmethod
    def unit_value(unit, key):
        joined = unit.replace("\\\n", " ")
        rows = [row.split("=", 1)[1] for row in joined.splitlines() if row.startswith(key + "=")]
        return " ".join(rows).split()

    def test_the_daemon_unit_starts_the_interpreter_isolated_and_drops_python_variables(self):
        unit = install_host.DAEMON_UNIT
        start = self.unit_value(unit, "ExecStart")
        self.assertEqual(start[:3], ["%h/.local/share/omodachi/venv/bin/python", "-I",
                                     "%h/.local/share/omodachi/venv/bin/omodachid"])
        unset = set(self.unit_value(unit, "UnsetEnvironment"))
        for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONPLATLIBDIR",
                     "PYTHONPYCACHEPREFIX", "PYTHONWARNINGS", "PYTHONBREAKPOINT", "PYTHONINSPECT"):
            self.assertIn(name, unset)
        # The herdr unit runs no Python of ours.
        self.assertNotIn("python", install_host.HERDR_UNIT)

    def fake_venv(self, home):
        import os
        venv = home / ".local/share/omodachi/venv/bin"
        venv.mkdir(parents=True)
        os.symlink(self.PYTHON, venv / "python")
        probe = ("import json, sys\n"
                 "print(json.dumps({'isolated': sys.flags.isolated, 'path': sys.path}))\n")
        for name in ("omodachid", "omodachi-host"):
            (venv / name).write_text("#!/usr/bin/env python3\n" + probe)
            (venv / name).chmod(0o755)
        return venv

    def test_the_daemon_command_and_the_wrappers_ignore_a_poisoned_environment(self):
        home = self.scratch / "home"
        venv = self.fake_venv(home)
        env = {**self.env, "HOME": str(home)}
        self.run_python([str(venv / "python"), str(venv / "omodachid")], env=env)
        self.assertIn("pythonpath-sitecustomize", self.markers())
        self.marker.unlink()
        # ExecStart as systemd runs it, %h expanded.
        start = [part.replace("%h", str(home))
                 for part in self.unit_value(install_host.DAEMON_UNIT, "ExecStart")]
        daemon = self.run_python(start[:3], env=env)
        self.assertEqual(json.loads(daemon.stdout)["isolated"], 1, daemon.stderr)
        self.assertNotIn(str(venv), json.loads(daemon.stdout)["path"])
        for name in ("omodachid", "omodachi-host"):
            wrapper = self.scratch / name
            wrapper.write_text(install_host.WRAPPER % name)
            result = self.run_python(["/bin/sh", str(wrapper)], env=env)
            self.assertEqual(json.loads(result.stdout)["isolated"], 1, result.stderr)
        self.assertEqual(self.markers(), [])

    # -- the daemon's app scanner --------------------------------------------

    def test_the_app_scanner_runs_isolated(self):
        from omodachi_core import catalog_providers
        self.assertEqual(catalog_providers.SCANNER_COMMAND, ("/usr/bin/python3", "-I", "-B"))
        scanner = Path(catalog_providers.__file__).resolve()
        self.run_python([self.PYTHON, str(scanner), "--scan-apps"])
        self.assertIn("user-site-pth", self.markers())
        self.marker.unlink()
        self.run_python([self.PYTHON, *catalog_providers.SCANNER_COMMAND[1:], str(scanner), "--scan-apps"])
        self.assertEqual(self.markers(), [])
