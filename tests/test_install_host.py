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


class Recorder:
    def __init__(self, *, status=STATUS, tailscale=True, fail_on=None):
        self.calls, self.status, self.tailscale, self.fail_on = [], status, tailscale, fail_on

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:3] == ["ip", "link", "show"]:
            return type("R", (), {"returncode": 0 if self.tailscale else 1, "stdout": ""})()
        if self.fail_on is not None and self.fail_on in argv:
            raise OSError("ufw refused")
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

    def test_install_deletes_the_legacy_single_mac_rules_and_nothing_else(self):
        recorder = Recorder()
        self.install(recorder)
        deletes = [call for call in recorder.ufw_calls() if "delete" in call]
        # Rule numbers shift on every delete, so they are spent highest first.
        self.assertEqual([call[-1] for call in deletes], ["6", "5"])

    def test_removal_is_symmetric_and_never_spends_another_owner_s_rule(self):
        recorder = Recorder()
        self.patch(recorder)
        self.assertEqual(install_host.remove_firewall(), 0)
        deletes = [call[-1] for call in recorder.ufw_calls() if "delete" in call]
        # Only the omodachi-core rule in the fixture listing; the dev-mac ssh
        # allowance, the Omarchy sshd rule and the docker DNS rules stay.
        self.assertEqual(deletes, ["7"])

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


class RuntimeConfigMigrationTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.path = self.home / install_host.RUNTIME_CONFIG
        self.path.parent.mkdir(parents=True)

    def write(self, value):
        import json
        self.path.write_text(json.dumps(value))

    def read(self):
        import json
        return json.loads(self.path.read_text())

    def test_dead_keys_go_and_the_journal_directory_follows_the_subsystem(self):
        self.write({"version": 1, "hyprland_instance": "auto",
                    "sunshine_socket": "/run/user/1000/omodachi-sunshine/pairing.sock",
                    "journal_dir": str(self.home / install_host.OLD_JOURNAL_DIR),
                    "recovery_output": "eDP-1", "devices": {}})
        result = install_host.migrate_runtime_config(self.home)
        self.assertTrue(result["migrated"])
        value = self.read()
        self.assertNotIn("recovery_output", value)
        self.assertNotIn("devices", value)
        self.assertEqual(value["journal_dir"], str(self.home / install_host.NEW_JOURNAL_DIR))
        # The one thing the spec says to keep.
        self.assertEqual(value["sunshine_socket"], "/run/user/1000/omodachi-sunshine/pairing.sock")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_an_already_migrated_file_is_left_byte_for_byte_alone(self):
        self.write({"version": 1, "journal_dir": str(self.home / install_host.NEW_JOURNAL_DIR)})
        before = self.path.read_text()
        self.assertFalse(install_host.migrate_runtime_config(self.home)["migrated"])
        self.assertEqual(self.path.read_text(), before)

    def test_a_host_with_no_runtime_file_gets_no_invented_one(self):
        self.path.unlink(missing_ok=True)
        self.assertFalse(install_host.migrate_runtime_config(self.home)["migrated"])
        self.assertFalse(self.path.exists())

    def test_an_empty_legacy_journal_directory_goes_and_a_full_one_stays(self):
        self.write({"version": 1})
        old = self.home / install_host.OLD_JOURNAL_DIR
        old.mkdir(parents=True)
        self.assertTrue(install_host.migrate_runtime_config(self.home)["legacy_journal_dir_removed"])
        self.assertFalse(old.exists())
        old.mkdir(parents=True)
        (old / "OMODACHI-0123.json").write_text("{}")
        result = install_host.migrate_runtime_config(self.home)
        self.assertFalse(result["legacy_journal_dir_removed"])
        self.assertTrue(result["legacy_journal_dir_present"])
        self.assertTrue(old.exists())


class UserMenuMigrationTests(unittest.TestCase):
    """The stale SPEC-A copy goes; a menu the user wrote never does."""

    CODEX = """{
  // the codex-era test layer SPEC-A copied under the new name
  "omadochi": {"label": "Omadochi", "icon": "\ue000"},
  "omadochi.desktop": {"label": "Remote screen", "surface": "desktop"},
  "omadochi.workspace.select.1": {"label": "Workspace 1"}
}
"""

    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.path = self.home / install_host.USER_MENU
        self.path.parent.mkdir(parents=True)

    def test_a_file_that_is_entirely_omadochi_is_renamed_aside(self):
        self.path.write_text(self.CODEX)
        result = install_host.migrate_user_menu(self.home)
        self.assertTrue(result["renamed"])
        self.assertEqual(result["entries"], 3)
        self.assertFalse(self.path.exists())
        backup = self.home / install_host.USER_MENU_BACKUP
        self.assertEqual(backup.read_text(), self.CODEX)

    def test_a_menu_the_user_wrote_is_never_touched(self):
        body = '{"omadochi.desktop": {"label": "x"}, "leo.custom": {"label": "mine"}}'
        self.path.write_text(body)
        result = install_host.migrate_user_menu(self.home)
        self.assertEqual(result, {"renamed": False, "reason": "not_the_codex_file"})
        self.assertEqual(self.path.read_text(), body)

    def test_an_absent_or_unreadable_file_is_reported_not_invented(self):
        self.assertEqual(install_host.migrate_user_menu(self.home),
                         {"renamed": False, "reason": "absent"})
        self.path.write_text("{ not json at all")
        self.assertEqual(install_host.migrate_user_menu(self.home),
                         {"renamed": False, "reason": "unreadable"})
        self.assertTrue(self.path.exists())

    def test_a_second_run_keeps_the_first_backup(self):
        self.path.write_text(self.CODEX)
        install_host.migrate_user_menu(self.home)
        self.path.write_text('{"omadochi": {"label": "second"}}')
        result = install_host.migrate_user_menu(self.home)
        self.assertTrue(result["renamed"])
        self.assertTrue(result["backup"].endswith(".codex-bak.2"))
        self.assertEqual((self.home / install_host.USER_MENU_BACKUP).read_text(), self.CODEX)

    def test_the_packaged_layer_alone_still_publishes_the_real_entries(self):
        """With the stale file gone, core reads its own packaged menu."""
        import sys
        sys.path.insert(0, str(ROOT / "src"))
        from omodachi_core.bootstrap import create_service
        from omodachi_core.hub import Hub
        self.path.write_text(self.CODEX)
        install_host.migrate_user_menu(self.home)
        service = create_service(Hub(), demo=True,
                                 omodachi_menu=self.path if self.path.exists() else None)
        ids = {row["id"] for row in service.refresh_catalog()["entries"]}
        self.assertFalse([value for value in ids if value.startswith("omadochi")])
        self.assertIn("omodachi.workspace.select.1", ids)


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
            home, path = self._home(scratch, {"apps": [{"name": self.NAME}]})
            install_host.remove_sunshine_app(home, self.NAME)
            body = path.read_text()
            result = install_host.remove_sunshine_app(home, self.NAME)
            self.assertFalse(result["changed"])
            self.assertEqual(result["reason"], "not_published")
            self.assertEqual(path.read_text(), body)

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

    def test_the_remove_advice_is_unchanged(self):
        source = Path(install_host.__file__).read_text()
        self.assertIn("f\"  python3 {share / 'src/scripts/install_host.py'} --local --remove-pam\\n\"", source)


class PinnedSunshineTests(unittest.TestCase):
    """CORE-2 §4: a host already running a fork that satisfies the pin is not touched."""

    def run_install(self, present, **options):
        import contextlib
        import io
        from unittest import mock
        from omodachi_core import sunshine_package
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sunshine_package, "installed_fork", return_value=present), \
                mock.patch.object(sunshine_package, "install",
                                  side_effect=sunshine_package.SunshinePackageError(
                                      "sunshine_package_unreachable", "would have downloaded")) as install, \
                mock.patch.object(sunshine_package, "_systemctl") as systemctl, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = install_host.install_sunshine(ROOT, **options)
        return result, out.getvalue(), err.getvalue(), install, systemctl

    def test_leos_drop_in_fork_is_left_alone_and_nothing_is_downloaded(self):
        # STREAM-1b: what omarchy runs after the 328d231 install - the release
        # archive's SOURCE carries the full commit.
        present = {"version": "328d2313c92dc4db675a8eafe96a7e32460c2758",
                   "binary": "/home/u/.local/share/omodachi/sunshine/328d231/sunshine",
                   "directory": "/home/u/.local/share/omodachi/sunshine/328d231",
                   "written_by_installer": True, "enabled": "enabled", "active": "active"}
        result, out, err, install, systemctl = self.run_install(present)
        self.assertEqual((result["installed"], result["reason"], result["pinned"]),
                         (False, "already_installed", "328d231"))
        install.assert_not_called()
        systemctl.assert_not_called()
        self.assertIn("328d2313c92d", out)
        self.assertIn("it is the pinned build", out)
        self.assertIn("nothing was downloaded", out)
        self.assertEqual(err, "")

    def test_a_listed_stand_in_is_left_alone_and_says_so(self):
        from unittest import mock
        from omodachi_core import sunshine_package
        present = {"version": "17c6043", "binary": "/home/u/.local/share/omodachi/sunshine/17c6043/sunshine",
                   "directory": "/home/u/.local/share/omodachi/sunshine/17c6043",
                   "written_by_installer": False, "enabled": "enabled", "active": "active"}
        pin = dict(sunshine_package.pinned(), satisfied_by=["17c6043"])
        with mock.patch.object(sunshine_package, "pinned", return_value=pin):
            result, out, err, install, systemctl = self.run_install(present)
        self.assertEqual((result["installed"], result["reason"]), (False, "already_installed"))
        install.assert_not_called()
        systemctl.assert_not_called()
        self.assertIn("satisfies the pinned 328d231", out)

    def test_a_fork_from_before_hevc_is_replaced_by_the_pin(self):
        # 17c6043 and e58627a are what omarchy ran before STREAM-1b: H.264-only
        # forks. With the pin at 328d231 neither may be skipped for.
        for version in ("17c6043", "e58627aa73d9bc42d95f121415b96a9a0b3aa0be"):
            present = {"version": version, "binary": "/x/sunshine", "directory": "/x",
                       "written_by_installer": False, "enabled": "enabled", "active": "active"}
            result, out, err, install, _ = self.run_install(present)
            self.assertEqual(install.call_args.args[0].rsplit("/", 1)[-1],
                             "omodachi-sunshine-328d231-x86_64.tar.zst", version)
            self.assertRegex(install.call_args.kwargs["sha256"], "^0f5a8f0b")

    def test_a_fork_that_is_not_enabled_is_enabled_but_not_restarted(self):
        present = {"version": "328d231", "binary": "/x/sunshine", "directory": "/x",
                   "written_by_installer": True, "enabled": "disabled", "active": "active"}
        result, out, _, install, systemctl = self.run_install(present)
        install.assert_not_called()
        self.assertEqual([call.args[0] for call in systemctl.call_args_list],
                         [["enable", "app-dev.lizardbyte.app.Sunshine.service"]])
        self.assertIn("it is the pinned build", out)

    def test_an_older_fork_or_an_explicit_archive_is_installed(self):
        older = {"version": "a2fd635", "binary": "/x/sunshine", "directory": "/x",
                 "written_by_installer": False, "enabled": "enabled", "active": "active"}
        result, out, err, install, _ = self.run_install(older)
        self.assertEqual(install.call_args.args[0].rsplit("/", 1)[-1], "omodachi-sunshine-328d231-x86_64.tar.zst")
        self.assertRegex(install.call_args.kwargs["sha256"], "^0f5a8f0b")
        self.assertIn("pinned 328d231", out)
        self.assertNotIn("/releases/latest/", out)
        current = dict(older, version="328d231")
        result, out, err, install, _ = self.run_install(current, spec="latest", sha256="e" * 64)
        self.assertIn("/releases/latest/", install.call_args.args[0])
        self.assertEqual(install.call_args.kwargs["sha256"], "e" * 64)


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
