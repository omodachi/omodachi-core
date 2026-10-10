"""HERDR-6: a new Herdr workspace from the phone, in a folder it picked.

The folder is the client's, so it is checked here before Herdr sees it: herdr
0.8.2 takes a missing folder, a file or a relative path without a word and
opens the shell in `$HOME` instead (probed on the host). These tests drive the
real service and router over a real scratch home folder; Herdr's side is the
recorded `workspace create` reply.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
import tempfile
import unittest

from omodachi_core import browser_gate
from omodachi_core.herdr_bridge import (HerdrBridge, HerdrFolders, HerdrSessionChoices, HerdrSessions,
                                        HerdrUnavailable, HerdrWriteAudit, workspace_label)
from omodachi_core.host_paths import FolderRefused, display, folder, private

import test_herdr_bridge

LISTING = {"sessions": [
    {"default": True, "name": "default", "running": False,
     "session_dir": "/home/u/.config/herdr", "socket_path": "/home/u/.config/herdr/herdr.sock"},
    {"default": False, "name": "h6test", "running": True,
     "session_dir": "/home/u/.config/herdr/sessions/h6test",
     "socket_path": "/home/u/.config/herdr/sessions/h6test/herdr.sock"},
    {"default": False, "name": "omodachi", "running": True,
     "session_dir": "/home/u/.config/herdr/sessions/omodachi",
     "socket_path": "/home/u/.config/herdr/sessions/omodachi/herdr.sock"}]}


def created(workspace_id="w2", label="proj", cwd="/home/u/proj", focused=False):
    """`herdr workspace create`'s `result`, as herdr 0.8.2 printed it on the host."""
    number = int(workspace_id[1:])
    return {"type": "workspace_created",
            "workspace": {"workspace_id": workspace_id, "label": label, "number": number, "focused": focused,
                          "active_tab_id": workspace_id + ":t1", "agent_status": "unknown",
                          "pane_count": 1, "tab_count": 1},
            "tab": {"tab_id": workspace_id + ":t1", "workspace_id": workspace_id, "label": "1", "number": 1},
            "root_pane": {"pane_id": workspace_id + ":p1", "tab_id": workspace_id + ":t1",
                          "workspace_id": workspace_id, "cwd": cwd, "foreground_cwd": cwd,
                          "focused": focused, "agent_status": "unknown", "revision": 0}}


class Home(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(os.path.realpath(self.temp.name)) / "home"
        for name in ("Work/omodachi", "Work/.hidden", "code", ".ssh/keys", ".config/omodachi",
                     ".local/state/omodachi/uploads", "dotfiles", "Zeta", "alpha"):
            (self.home / name).mkdir(parents=True)
        (self.home / "notes.txt").write_text("x")
        (self.home / "Work" / "readme.md").write_text("x")
        self.outside = Path(os.path.realpath(self.temp.name)) / "outside"
        self.outside.mkdir()
        os.symlink(self.outside, self.home / "out-link")
        os.symlink(self.home / ".ssh", self.home / "keys-link")
        os.symlink(self.home / "Work" / "omodachi", self.home / "proj-link")

    def tearDown(self):
        self.temp.cleanup()


class FolderTests(Home):
    def test_a_folder_is_read_the_way_a_person_types_it(self):
        work = self.home / "Work"
        for typed in ("~/Work", "Work", str(work), " ~/Work ", "~/Work/", "~/code/../Work"):
            self.assertEqual(folder(typed, home=self.home), work, typed)
        self.assertEqual(folder("~", home=self.home), self.home)
        # A link inside the home folder is followed to where it leads.
        self.assertEqual(folder("~/proj-link", home=self.home), self.home / "Work/omodachi")

    def test_the_resolved_folder_must_be_in_the_home_folder(self):
        for typed in ("/etc", "/", "~/..", "~/out-link", str(self.outside), "~root", "~root/x"):
            with self.assertRaises(FolderRefused) as caught:
                folder(typed, home=self.home)
            self.assertEqual(caught.exception.args[0], "folder_outside_home", typed)

    def test_private_places_are_refused_whatever_the_path_says(self):
        for typed in ("~/.ssh", "~/.ssh/keys", "~/keys-link", "~/.config/omodachi", "~/Work/../.ssh",
                      "~/.ssh/not-there", "~/.gnupg"):
            with self.assertRaises(FolderRefused) as caught:
                folder(typed, home=self.home)
            # Refused as private before anyone asks whether it is there.
            self.assertEqual(caught.exception.args[0], "folder_private", typed)
        # ATTACH-2's exception: what Omodachi hands an agent is not private.
        self.assertEqual(folder("~/.local/state/omodachi/uploads", home=self.home),
                         self.home / ".local/state/omodachi/uploads")
        self.assertFalse(private(self.home / ".sshx", self.home))
        self.assertTrue(private(self.home / ".ssh", self.home))

    def test_a_missing_folder_or_a_file_is_not_a_folder(self):
        for typed in ("~/nope", "~/notes.txt", "~/Work/readme.md"):
            with self.assertRaises(FolderRefused) as caught:
                folder(typed, home=self.home)
            self.assertEqual(caught.exception.args[0], "folder_not_found", typed)

    def test_what_is_not_a_path_at_all(self):
        for typed in (None, 7, "", "   ", "a\x00b", "a\nb", "x" * 5000, ["~"]):
            with self.assertRaises(FolderRefused) as caught:
                folder(typed, home=self.home)
            self.assertEqual(caught.exception.args[0], "invalid_folder", repr(typed)[:20])

    def test_display(self):
        self.assertEqual(display(self.home, self.home), "~")
        self.assertEqual(display(self.home / "Work/omodachi", self.home), "~/Work/omodachi")


class FolderListTests(Home):
    def test_browse_is_one_level_of_folders_sorted_without_dotfiles_unless_asked(self):
        folders = HerdrFolders(self.home)
        listing = folders.browse("~")
        names = [row["name"] for row in listing["dirs"]]
        # Files, the link out of home and the link into ~/.ssh are not offered.
        self.assertEqual(names, ["alpha", "code", "dotfiles", "proj-link", "Work", "Zeta"])
        self.assertEqual((listing["display"], listing["parent"], listing["hidden"], listing["truncated"]),
                         ("~", None, False, False))
        link = next(row for row in listing["dirs"] if row["name"] == "proj-link")
        self.assertEqual((link["display"], link["path"]), ("~/proj-link", str(self.home / "Work/omodachi")))
        hidden = [row["name"] for row in folders.browse("~", hidden=True)["dirs"]]
        # Dot folders when asked - but never a private one.
        self.assertIn(".config", hidden)
        self.assertNotIn(".ssh", hidden)
        inner = folders.browse("~/Work")
        self.assertEqual([row["name"] for row in inner["dirs"]], ["omodachi"])
        self.assertEqual(inner["parent"]["display"], "~")
        self.assertEqual([row["name"] for row in folders.browse("~/.config", hidden=True)["dirs"]], [])
        with self.assertRaises(FolderRefused):
            folders.browse("~/.ssh", hidden=True)

    def test_browse_stops_at_a_hundred(self):
        for number in range(130):
            (self.home / "many" / f"d{number:03d}").mkdir(parents=True)
        listing = HerdrFolders(self.home).browse("~/many")
        self.assertEqual(len(listing["dirs"]), 100)
        self.assertTrue(listing["truncated"])
        self.assertEqual(listing["dirs"][0]["name"], "d000")

    def test_common_folders_are_the_ones_this_host_has(self):
        rows = HerdrFolders(self.home).common()
        self.assertEqual([row["display"] for row in rows], ["~", "~/Work", "~/code"])

    def test_recent_is_newest_first_deduplicated_and_at_most_eight(self):
        clock = [100.0]
        folders = HerdrFolders(self.home, clock=lambda: clock[0])
        work, code, proj = str(self.home / "Work"), str(self.home / "code"), str(self.home / "Work/omodachi")
        # Newest pane first, as the service hands them over.
        paths = folders.note("a", [code, work, str(self.home), code, "/etc", str(self.home / ".ssh"),
                                   str(self.home / "gone"), None])
        self.assertEqual([row["display"] for row in folders.recent(paths)], ["~/code", "~/Work"])
        # A pane moves to a folder no session had: it is the newest now.
        clock[0] = 200.0
        paths = folders.note("a", [code, work]) + folders.note("b", [proj])
        self.assertEqual([row["display"] for row in folders.recent(paths)],
                         ["~/Work/omodachi", "~/code", "~/Work"])
        # One a workspace was just made in comes first.
        clock[0] = 300.0
        folders.touch(Path(work))
        self.assertEqual(folders.recent(paths)[0]["display"], "~/Work")
        for number in range(12):
            (self.home / f"r{number}").mkdir()
        many = folders.note("c", [str(self.home / f"r{number}") for number in range(12)])
        self.assertEqual(len(folders.recent(many + paths)), 8)


class LabelTests(unittest.TestCase):
    def test_a_workspace_name_is_a_label_or_nothing(self):
        self.assertEqual(workspace_label(" proj "), "proj")
        self.assertEqual(workspace_label("项目 2"), "项目 2")
        for empty in (None, "", "   "):
            self.assertIsNone(workspace_label(empty))
        for bad in ("a\nb", "x" * 65, 7, ["x"]):
            with self.assertRaises(HerdrUnavailable) as caught:
                workspace_label(bad)
            self.assertEqual(caught.exception.args[0], "invalid_workspace_name")

    def test_create_is_never_focused_and_reads_herdrs_reply(self):
        calls = []
        bridge = HerdrBridge("h6test", runner=lambda argv: calls.append(argv) or created())
        result = bridge.workspace_create(Path("/home/u/proj"), "proj")
        self.assertEqual(calls, [("herdr", "--session", "h6test", "workspace", "create", "--cwd", "/home/u/proj",
                                  "--label", "proj", "--no-focus")])
        self.assertEqual(result, {"id": "w2", "label": "proj", "number": 2, "focused": False, "pane": "w2:p1",
                                  "cwd": "/home/u/proj"})
        bridge.workspace_create(Path("/home/u/proj"))
        self.assertEqual(calls[-1][-3:], ("--cwd", "/home/u/proj", "--no-focus"))
        with self.assertRaises(HerdrUnavailable) as caught:
            HerdrBridge("h6test", runner=lambda argv: {"type": "ok"}).workspace_create("/home/u")
        self.assertEqual(caught.exception.args[0], "herdr_request_failed")


class WorkspaceApiTests(test_herdr_bridge.HerdrApiTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.home = Path(os.path.realpath(self.temp.name)) / "home"
        for name in ("h6test/proj", "code", ".ssh", "Work"):
            (self.home / name).mkdir(parents=True)
        self.listing = copy.deepcopy(LISTING)
        self.service.herdr_choices = HerdrSessionChoices(Path(self.temp.name) / "herdr-sessions.json")
        self.service._herdr_sessions = HerdrSessions(runner=lambda argv: self.listing,
                                                     home=Path("/home/u/.config/herdr"))
        self.service.herdr_audit = HerdrWriteAudit()
        self.service.herdr_folders_memory = HerdrFolders(self.home)
        self.created = []
        self.snapshots = {"h6test": {"panes": [{"pane_id": "w1:p1", "cwd": str(self.home / "code")},
                                               {"pane_id": "w2:p1", "cwd": str(self.home / "h6test/proj")}]},
                          "omodachi": {"panes": [{"pane_id": "w1:p1", "cwd": str(self.home)}]}}

        def runner(session):
            def run(argv):
                if argv[3:5] == ("workspace", "create"):
                    self.created.append((session, argv))
                    return created(cwd=argv[6], label=argv[8] if "--label" in argv else Path(argv[6]).name)
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

    async def post(self, path, body=None, token=None, headers=None):
        async with self.client.post(self.url + path, json=body if body is not None else {},
                                    headers=headers or {"Authorization": "Bearer " + (token or self.token)}) as response:
            return response.status, await response.json()

    async def get(self, path, headers=None):
        async with self.client.get(self.url + path, headers=headers or self.headers()) as response:
            return response.status, await response.json()

    async def test_a_workspace_is_made_in_the_folder_without_focus_and_this_device_lands_there(self):
        other = self.hub.register_device("phone-b")
        status, body = await self.post("/v1/herdr/sessions/h6test/workspaces",
                                       {"cwd": "~/h6test/proj", "name": "proj"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.created, [("h6test", ("herdr", "--session", "h6test", "workspace", "create", "--cwd",
                                                    str(self.home / "h6test/proj"), "--label", "proj",
                                                    "--no-focus"))])
        self.assertEqual(body, {"contract_revision": "omodachi.v1", "selected": "h6test", "owned": "omodachi",
                                "scope": "device",
                                "workspace": {"id": "w2", "label": "proj", "number": 2, "focused": False,
                                              "pane": "w2:p1", "cwd": str(self.home / "h6test/proj"),
                                              "display": "~/h6test/proj"}})
        # Only this device moved, and only its own view.
        self.assertEqual(self.service.herdr_choices.selected("phone-a"), "h6test")
        self.assertEqual(self.service.herdr_choices.selected("phone-b"), "omodachi")
        line = self.service.herdr_audit.lines[-1]
        self.assertEqual((line["kind"], line["session"], line["pane"], line["outcome"], line["device"], line["length"]),
                         ("workspace_create", "h6test", "w2", "ok", "phone-a", 0))
        self.assertNotIn("cwd", line)
        # No name: Herdr names it after its folder.
        status, body = await self.post("/v1/herdr/sessions/h6test/workspaces", {"cwd": "~/code", "name": " "},
                                       token=other)
        self.assertEqual(status, 200, body)
        self.assertNotIn("--label", self.created[-1][1])

    async def test_a_folder_that_is_not_allowed_is_refused_before_herdr_runs(self):
        cases = (("/etc", 400, "folder_outside_home"), ("~/..", 400, "folder_outside_home"),
                 ("~/.ssh", 400, "folder_private"), ("~/nope", 404, "folder_not_found"),
                 # HERDR-11: an empty folder is the home folder now, not a refusal.
                 (7, 400, "invalid_folder"))
        for cwd, code, reason in cases:
            status, body = await self.post("/v1/herdr/sessions/h6test/workspaces", {"cwd": cwd})
            self.assertEqual((status, body["error"]["code"]), (code, reason), cwd)
        self.assertEqual(self.created, [])
        self.assertEqual(self.service.herdr_choices.selected("phone-a"), "omodachi")

    async def test_a_session_that_is_not_running_or_a_bad_body_is_refused(self):
        for name in ("default", "nothere"):
            status, body = await self.post(f"/v1/herdr/sessions/{name}/workspaces", {"cwd": "~"})
            self.assertEqual((status, body["error"]["code"]), (400, "invalid_session"), name)
        # HERDR-11: `{}` is a workspace in the home folder now.
        for payload in ({"cwd": "~", "focus": True}, {"cwd": "~", "label": "x"}):
            status, body = await self.post("/v1/herdr/sessions/h6test/workspaces", payload)
            self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"), payload)
        status, body = await self.post("/v1/herdr/sessions/h6test/workspaces", {"cwd": "~", "name": "a\nb"})
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_workspace_name"))
        async with self.client.post(self.url + "/v1/herdr/sessions/h6test/workspaces?focus=1", json={"cwd": "~"},
                                    headers=self.headers()) as response:
            self.assertEqual(response.status, 400)
        self.assertEqual(self.created, [])

    async def test_folders_offer_recent_and_common_then_one_level_at_a_time(self):
        status, body = await self.get("/v1/herdr/folders")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["home"], str(self.home))
        self.assertIsNone(body["browse"])
        # The panes' folders, this device's session first, newest pane first;
        # the home folder is a common one, not a recent one.
        self.assertEqual([row["display"] for row in body["recent"]], ["~/h6test/proj", "~/code"])
        self.assertEqual([row["display"] for row in body["common"]], ["~", "~/Work", "~/code"])
        status, body = await self.get("/v1/herdr/folders?under=~/h6test")
        self.assertEqual(status, 200, body)
        self.assertEqual((body["recent"], body["common"]), (None, None))
        self.assertEqual([row["name"] for row in body["browse"]["dirs"]], ["proj"])
        for query, code, reason in (("under=/etc", 400, "folder_outside_home"),
                                    ("under=~/.ssh&hidden=1", 400, "folder_private"),
                                    ("under=~/nope", 404, "folder_not_found"),
                                    ("hidden=maybe", 400, "invalid_request"),
                                    ("cwd=~", 400, "invalid_request")):
            status, body = await self.get("/v1/herdr/folders?" + query)
            self.assertEqual((status, body["error"]["code"]), (code, reason), query)

    async def test_herdr_not_answering_still_leaves_the_common_folders(self):
        def fail(argv):
            raise HerdrUnavailable("herdr_unavailable")
        self.service._herdr_sessions = HerdrSessions(runner=fail)
        status, body = await self.get("/v1/herdr/folders")
        self.assertEqual(status, 200, body)
        self.assertEqual((body["recent"], [row["display"] for row in body["common"]]), ([], ["~", "~/Work", "~/code"]))

    async def test_both_are_odomo_s_too_and_still_need_a_credential(self):
        self.assertTrue(browser_gate.web_allows("POST", "/v1/herdr/sessions/h6test/workspaces"))
        self.assertTrue(browser_gate.web_allows("GET", "/v1/herdr/folders"))
        self.assertFalse(browser_gate.web_allows("GET", "/v1/herdr/sessions/h6test/workspaces"))
        self.assertFalse(browser_gate.web_allows("POST", "/v1/herdr/folders"))
        self.assertFalse(browser_gate.web_allows("POST", "/v1/herdr/sessions/../workspaces"))
        async with self.client.post(self.url + "/v1/herdr/sessions/h6test/workspaces", json={"cwd": "~"}) as response:
            self.assertEqual(response.status, 401)
        async with self.client.get(self.url + "/v1/herdr/folders") as response:
            self.assertEqual(response.status, 401)

    async def test_the_poll_remembers_where_panes_are(self):
        self.service._herdr_note_folders("h6test", {"workspaces": [{"tabs": [{"panes": [
            {"id": "w9:p1", "cwd": str(self.home / "Work")}]}]}]})
        self.assertIn(str(self.home / "Work"), self.service.herdr_folders_memory._seen)


# The bridge's own API tests run in their module; only these run here.
for _name in dir(test_herdr_bridge.HerdrApiTests):
    if _name.startswith("test_") and _name not in WorkspaceApiTests.__dict__:
        setattr(WorkspaceApiTests, _name, None)


if __name__ == "__main__":
    unittest.main()
