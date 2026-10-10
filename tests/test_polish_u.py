"""POLISH-U: the recent folders survive.

HERDR-6's "recent" folders were the folders the running sessions' panes were in
at that moment, so a folder used for a new workspace yesterday was gone as soon
as its pane closed, its session stopped or core restarted. Core now keeps its
own short list of the folders new workspaces were made in
(`~/.config/omodachi/herdr-folders.json`) and merges it with the live panes'
folders. These tests drive `HerdrFolders` over a real scratch home folder, the
service and router the way HERDR-6's do, and the installer's purge rule.
"""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

import aiohttp

from omodachi_core.bootstrap import create_service
from omodachi_core.herdr_bridge import (HerdrBridge, HerdrFolders, HerdrSessionChoices, HerdrSessions,
                                        HerdrWriteAudit)
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer

from test_herdr6 import LISTING, created

ROOT = Path(__file__).resolve().parents[1]


class RememberedFolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(os.path.realpath(self.temp.name))
        self.home = base / "home"
        for name in ("proj", "code", "Work/notes", ".ssh"):
            (self.home / name).mkdir(parents=True)
        self.file = base / "config" / "herdr-folders.json"
        self.clock = [1000.0]

    def folders(self) -> HerdrFolders:
        return HerdrFolders(self.home, clock=lambda: self.clock[0], path=self.file)

    def test_a_folder_a_workspace_was_made_in_is_offered_after_a_restart(self):
        first = self.folders()
        first.touch(self.home / "proj")
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.file.read_text()),
                         {"version": 1, "folders": [{"path": str(self.home / "proj"), "at": 1000.0}]})
        # A new daemon, no pane anywhere near the folder: still offered.
        again = self.folders()
        self.assertEqual([row["display"] for row in again.recent([])], ["~/proj"])
        # Nothing but the temp file's own name pattern is ever left beside it.
        self.assertEqual(sorted(path.name for path in self.file.parent.iterdir()), ["herdr-folders.json"])

    def test_live_and_remembered_folders_merge_newest_first(self):
        folders = self.folders()
        folders.touch(self.home / "proj")           # used at 1000
        self.clock[0] = 2000.0
        live = folders.note("a", [str(self.home / "code"), str(self.home / "proj")])  # first seen at 2000
        # `code` is newer (seen 2000); `proj` is both, at its later time (2000):
        # the live order breaks the tie, and it is listed once.
        self.assertEqual([row["display"] for row in folders.recent(live)], ["~/code", "~/proj"])
        self.clock[0] = 3000.0
        folders.touch(self.home / "Work/notes")
        self.assertEqual([row["display"] for row in folders.recent(live)], ["~/Work/notes", "~/code", "~/proj"])
        # The pane in `code` closes: `code` was never used for a workspace,
        # so it goes; the two used folders stay.
        live = folders.note("a", [])
        self.assertEqual([row["display"] for row in folders.recent(live)], ["~/Work/notes", "~/proj"])

    def test_the_list_is_bounded_and_newest_first(self):
        folders = self.folders()
        for number in range(HerdrFolders.MAX_USED + 4):
            (self.home / f"f{number:02d}").mkdir()
            self.clock[0] = 1000.0 + number
            folders.touch(self.home / f"f{number:02d}")
        kept = json.loads(self.file.read_text())["folders"]
        self.assertEqual(len(kept), HerdrFolders.MAX_USED)
        self.assertEqual(kept[0]["path"], str(self.home / f"f{HerdrFolders.MAX_USED + 3:02d}"))
        self.assertEqual([entry["at"] for entry in kept], sorted((entry["at"] for entry in kept), reverse=True))
        # Used again: moved to the front, never listed twice.
        self.clock[0] = 9999.0
        folders.touch(self.home / "f10")
        kept = json.loads(self.file.read_text())["folders"]
        self.assertEqual(kept[0], {"path": str(self.home / "f10"), "at": 9999.0})
        self.assertEqual(len({entry["path"] for entry in kept}), len(kept))
        self.assertEqual(len(self.folders().recent([])), HerdrFolders.MAX_RECENT)

    def test_a_folder_that_is_gone_private_or_outside_home_is_not_offered_and_is_dropped(self):
        outside = Path(os.path.realpath(self.temp.name)) / "elsewhere"
        outside.mkdir()
        (self.home / "out").symlink_to(outside)
        self.file.parent.mkdir(parents=True)
        self.file.write_text(json.dumps({"version": 1, "folders": [
            {"path": str(self.home / "proj"), "at": 5.0},
            {"path": str(self.home / "gone"), "at": 4.0},
            {"path": str(self.home / ".ssh"), "at": 3.0},
            {"path": "/etc", "at": 2.0},
            {"path": str(self.home / "out"), "at": 1.5},
            {"path": str(self.home), "at": 1.0},
        ]}))
        folders = self.folders()
        self.assertEqual([row["display"] for row in folders.recent([])], ["~/proj"])
        folders.touch(self.home / "code")
        self.assertEqual([entry["path"] for entry in json.loads(self.file.read_text())["folders"]],
                         [str(self.home / "code"), str(self.home / "proj")])
        # A workspace in the home folder itself: it is always offered first
        # among the common ones, so it is not kept here.
        folders.touch(self.home)
        self.assertEqual([entry["path"] for entry in json.loads(self.file.read_text())["folders"]],
                         [str(self.home / "code"), str(self.home / "proj")])

    def test_a_damaged_file_is_read_as_empty_and_rewritten(self):
        self.file.parent.mkdir(parents=True)
        for text in ("not json", "[]", '{"folders": 7}',
                     json.dumps({"folders": [7, {"path": "relative", "at": 1}, {"path": "/x\u0000", "at": 1},
                                             {"path": str(self.home / "proj"), "at": "soon"}]})):
            self.file.write_text(text)
            self.assertEqual(self.folders().recent([]), [], text)
        folders = self.folders()
        folders.touch(self.home / "proj")
        self.assertEqual(json.loads(self.file.read_text())["folders"][0]["path"], str(self.home / "proj"))

    def test_an_unwritable_place_costs_the_suggestion_not_the_workspace(self):
        blocked = Path(os.path.realpath(self.temp.name)) / "blocked"
        blocked.write_text("a file where the directory would go")
        folders = HerdrFolders(self.home, clock=lambda: 1.0, path=blocked / "herdr-folders.json")
        folders.touch(self.home / "proj")  # does not raise
        self.assertEqual([row["display"] for row in folders.recent([])], ["~/proj"])

    def test_without_a_path_nothing_is_written(self):
        folders = HerdrFolders(self.home, clock=lambda: 1.0)
        folders.touch(self.home / "proj")
        self.assertEqual([row["display"] for row in folders.recent([])], ["~/proj"])
        self.assertFalse(self.file.exists())
        with self.assertRaises(ValueError):
            HerdrFolders(self.home, path="relative/herdr-folders.json")


class RememberedFolderApiTests(unittest.IsolatedAsyncioTestCase):
    """The route: make a workspace, restart core (a new `HerdrFolders` on the
    same file), close the pane - the folder is still offered."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub = Hub(auth_check_interval=0.05)
        self.token = self.hub.register_device("phone-a")
        self.service = create_service(self.hub, demo=True)
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        self.home = Path(os.path.realpath(self.temp.name)) / "home"
        for name in ("h6test/proj", "code", ".ssh"):
            (self.home / name).mkdir(parents=True)
        self.file = Path(self.temp.name) / "herdr-folders.json"
        self.listing = copy.deepcopy(LISTING)
        self.service.herdr_choices = HerdrSessionChoices(Path(self.temp.name) / "herdr-sessions.json")
        self.service._herdr_sessions = HerdrSessions(runner=lambda argv: self.listing,
                                                     home=Path("/home/u/.config/herdr"))
        self.service.herdr_audit = HerdrWriteAudit()
        self.service.herdr_folders_memory = HerdrFolders(self.home, path=self.file)
        self.snapshots = {"h6test": {"panes": [{"pane_id": "w1:p1", "cwd": str(self.home / "code")}]},
                          "omodachi": {"panes": [{"pane_id": "w1:p1", "cwd": str(self.home)}]}}

        def runner(session):
            def run(argv):
                if argv[3:5] == ("workspace", "create"):
                    self.snapshots[session]["panes"].append({"pane_id": "w2:p1", "cwd": argv[6]})
                    return created(cwd=argv[6], label=Path(argv[6]).name)
                if argv[3:5] == ("api", "snapshot"):
                    return {"snapshot": self.snapshots[session]}
                raise AssertionError(argv)
            return run
        for name in ("h6test", "omodachi"):
            bridge = HerdrBridge(name, runner=runner(name))
            if name == "omodachi":
                self.service.herdr_bridge = bridge
            else:
                self.service._herdr_bridges[name] = bridge

    async def asyncTearDown(self):
        await self.client.close()
        await asyncio.wait_for(self.server.close(), 3)
        self.temp.cleanup()

    def headers(self):
        return {"Authorization": "Bearer " + self.token}

    async def test_a_used_folder_survives_its_pane_and_a_restart(self):
        async with self.client.post(self.url + "/v1/herdr/sessions/h6test/workspaces",
                                    json={"cwd": "~/h6test/proj"}, headers=self.headers()) as response:
            self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(json.loads(self.file.read_text())["folders"][0]["path"], str(self.home / "h6test/proj"))
        # Core restarts; the new workspace's pane is closed meanwhile.
        self.service.herdr_folders_memory = HerdrFolders(self.home, path=self.file)
        self.snapshots["h6test"]["panes"] = [{"pane_id": "w1:p1", "cwd": str(self.home / "code")}]
        async with self.client.get(self.url + "/v1/herdr/folders", headers=self.headers()) as response:
            self.assertEqual(response.status, 200)
            body = await response.json()
        # The live pane in `code` was first seen after the restart, so it is
        # the newer; the remembered folder is right behind it.
        self.assertEqual([row["display"] for row in body["recent"]], ["~/code", "~/h6test/proj"])
        # The session stops too: the remembered folder is still there, alone.
        for row in self.listing["sessions"]:
            row["running"] = row["name"] == "omodachi"
        async with self.client.get(self.url + "/v1/herdr/folders", headers=self.headers()) as response:
            body = await response.json()
        self.assertEqual([row["display"] for row in body["recent"]], ["~/h6test/proj"])


class OpenOutsideTheDaemonTests(unittest.TestCase):
    """A file or page opened from a phone must outlive a core restart: it runs
    in its own transient scope, not in `omodachid.service`'s cgroup (which
    `KillMode=control-group` empties on every stop)."""

    def test_the_opener_starts_its_program_in_a_scope_of_its_own(self):
        from unittest import mock
        from omodachi_core.host_open import DesktopOpener
        with tempfile.TemporaryDirectory() as scratch:
            runner = Path(scratch) / "systemd-run"
            runner.write_text("#!/bin/sh\n")
            runner.chmod(0o755)
            env = {"PATH": scratch, "WAYLAND_DISPLAY": "wayland-1"}
            started = []

            class Process:
                pid = 4242
                def wait(self, timeout=None):
                    return 0

            def popen(argv, **kwargs):
                started.append((argv, kwargs))
                return Process()
            with mock.patch("omodachi_core.host_open.subprocess.Popen", popen):
                self.assertEqual(DesktopOpener._spawn(("/usr/bin/xdg-open", "https://example.com/"), env, scratch), 0)
            argv, kwargs = started[0]
            self.assertEqual(argv[:5], [str(runner), "--user", "--scope", "--quiet", "--collect"])
            self.assertEqual(argv[5], "--unit")
            self.assertRegex(argv[6], r"^omodachi-open-[0-9a-f]{12}$")
            self.assertEqual(argv[7:], ["--", "/usr/bin/xdg-open", "https://example.com/"])
            self.assertTrue(kwargs["start_new_session"])
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            # No systemd-run on PATH: the program itself, as before.
            self.assertEqual(DesktopOpener.scoped(("/usr/bin/xdg-open", "x"), {"PATH": "/nonexistent"}),
                             ["/usr/bin/xdg-open", "x"])


    def test_a_keybinding_run_from_a_phone_starts_in_a_scope_of_its_own(self):
        from unittest import mock
        from omodachi_core.shortcut_provider import ShortcutProvider, exec_scoped
        with tempfile.TemporaryDirectory() as scratch:
            runner = Path(scratch) / "systemd-run"
            runner.write_text("#!/bin/sh\n")
            runner.chmod(0o755)
            env = {"PATH": scratch}
            started = []

            class Process:
                pid = 4343
                def poll(self):
                    return None

            def popen(argv, **kwargs):
                started.append((argv, kwargs))
                return Process()
            provider = ShortcutProvider(object(), sleeper=lambda _: None)
            with mock.patch("omodachi_core.shortcut_provider.subprocess.Popen", popen):
                result = provider._spawn("omarchy-launch-browser", env)
            self.assertEqual(result, {"pid": 4343, "exited": False, "exit_code": None})
            argv, kwargs = started[0]
            self.assertEqual(argv[:5], [str(runner), "--user", "--scope", "--quiet", "--collect"])
            self.assertRegex(argv[6], r"^omodachi-keybinding-[0-9a-f]{12}$")
            self.assertEqual(argv[7:], ["--", "/bin/bash", "-c", "omarchy-launch-browser"])
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(exec_scoped(("/bin/true",), {"PATH": "/nonexistent"}), ["/bin/true"])


class PurgeRuleTests(unittest.TestCase):
    @staticmethod
    def installer(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_purge_rule_names_the_files_herdr_state_makes(self):
        installer = self.installer("core_install_host_pu", ROOT / "scripts/install_host.py")
        rules = installer.PURGE_RULES[".config/omodachi"]
        with tempfile.TemporaryDirectory() as scratch:
            folders = HerdrFolders(Path(os.path.realpath(scratch)), clock=lambda: 1.0,
                                   path=Path(scratch) / "conf" / "herdr-folders.json")
            (Path(os.path.realpath(scratch)) / "proj").mkdir()
            folders.touch(Path(os.path.realpath(scratch)) / "proj")
            temporary = tempfile.mkstemp(dir=scratch, prefix=".herdr-folders")[1]
        for name in ("herdr-folders.json", Path(temporary).name, "herdr-writes.jsonl", "herdr-writes.jsonl.1",
                     "herdr-sessions.json"):
            known, rule = installer._rule_for(name, rules)
            self.assertTrue(known and rule is None, name)
        for name in ("herdr-folders.json.bak", "herdr-writes.jsonl.2", "my-notes.txt"):
            self.assertFalse(installer._rule_for(name, rules)[0], name)

    def test_the_plugin_copy_matches_when_a_plugin_checkout_is_at_hand(self):
        plugin = os.environ.get("OMODACHI_PLUGIN_TREE")
        script = Path(plugin or "") / "tools/install_host.py"
        if not plugin or not script.is_file():
            self.skipTest("set OMODACHI_PLUGIN_TREE to a plugin checkout")
        self.assertEqual(self.installer("plugin_install_host_pu", script).PURGE_RULES,
                         self.installer("core_install_host_pu2", ROOT / "scripts/install_host.py").PURGE_RULES)


if __name__ == "__main__":
    unittest.main()
