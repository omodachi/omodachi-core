"""HERDR-5: a stopped session is started from the phone, and a new one made.

The start path is herdr 0.8.2's own headless server (`herdr --session <name>
server`) in a transient user unit, then the device is moved into it. These
tests drive the real service and router against a recorded listing that the
fake runner flips from stopped to running, exactly as `herdr session list`
does on the host once the unit is up.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from omodachi_core import browser_gate
from omodachi_core.agent import ProbeOutput
from omodachi_core.herdr_bridge import (HerdrSessionChoices, HerdrSessions, HerdrSessionStarter,
                                        HerdrUnavailable, HerdrWriteAudit, new_session_name)

import test_herdr_bridge

LISTING = {"sessions": [
    {"default": True, "name": "default", "running": False,
     "session_dir": "/home/u/.config/herdr", "socket_path": "/home/u/.config/herdr/herdr.sock"},
    {"default": False, "name": "omodachi", "running": True,
     "session_dir": "/home/u/.config/herdr/sessions/omodachi",
     "socket_path": "/home/u/.config/herdr/sessions/omodachi/herdr.sock"}]}


class NewSessionNameTests(unittest.TestCase):
    def test_a_name_a_person_types_is_herdrs_alphabet_narrowed(self):
        for good in ("h5test", "my.proj_1", "A-b", "x" * 32, "7"):
            self.assertEqual(new_session_name(good), good)
        # `..` would be `sessions/..`, the default session's own directory; a
        # leading `-` is a flag to Herdr; 33 characters is past the limit.
        for bad in (".", "..", ".hidden", "-x", "_x", "a/b", "a b", "", "x" * 33, None, 7, "名字"):
            with self.assertRaises(HerdrUnavailable) as caught:
                new_session_name(bad)
            self.assertEqual(caught.exception.args[0], "invalid_session_name")


class StarterTests(unittest.TestCase):
    def starter(self, runner, **kwargs):
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds
        return HerdrSessionStarter(runner=runner, home=Path("/home/u"), sleep=sleep,
                                   clock=lambda: clock[0], **kwargs)

    def test_the_server_is_herdrs_own_headless_server_in_a_unit_of_its_own(self):
        starter = HerdrSessionStarter(home=Path("/home/u"))
        self.assertEqual(starter.server_argv("h5test"), (
            "systemd-run", "--user", "--unit=omodachi-herdr-session-h5test", "--collect",
            "--property=Type=exec", "--working-directory=/home/u",
            "--description=Herdr session h5test (started from Omodachi)",
            "herdr", "--session", "h5test", "server"))
        # The owned session keeps its own service.
        self.assertEqual(starter.server_argv("omodachi"),
                         ("systemctl", "--user", "start", "omodachi-herdr.service"))
        self.assertEqual(starter.workspace_argv("h5test"),
                         ("herdr", "--session", "h5test", "workspace", "create", "--cwd", "/home/u", "--no-focus"))
        with self.assertRaises(HerdrUnavailable):
            starter.server_argv("--takeover")

    def test_a_running_session_is_not_started_again(self):
        calls = []
        started = self.starter(lambda argv: calls.append(argv) or ProbeOutput(0)).start(
            "default", running=lambda: True, snapshot=lambda: {"workspaces": [{}]})
        self.assertFalse(started)
        self.assertEqual(calls, [])

    def test_a_session_that_comes_up_with_its_workspaces_is_left_as_it_was(self):
        state = {"up": False}
        calls = []

        def runner(argv):
            calls.append(argv)
            state["up"] = True
            return ProbeOutput(0)
        started = self.starter(runner).start("default", running=lambda: state["up"],
                                             snapshot=lambda: {"workspaces": [{"workspace_id": "w4"}]})
        self.assertTrue(started)
        self.assertEqual([call[0] for call in calls], ["systemd-run"])

    def test_a_session_with_no_workspace_gets_one_in_home(self):
        state = {"up": False}
        calls = []

        def runner(argv):
            calls.append(argv)
            state["up"] = True
            return ProbeOutput(0)
        with tempfile.TemporaryDirectory() as home:
            starter = HerdrSessionStarter(runner=runner, home=Path(home), sleep=lambda _: None)
            self.assertTrue(starter.start("h5test", running=lambda: state["up"], snapshot=lambda: {"workspaces": []}))
        self.assertEqual(calls[-1][3:6], ("workspace", "create", "--cwd"))

    def test_the_server_answering_late_is_waited_for(self):
        state = {"polls": 0}

        def running():
            state["polls"] += 1
            return state["polls"] > 6

        def snapshot():
            if state["polls"] < 9:
                raise HerdrUnavailable("herdr_unavailable")
            return {"workspaces": [{}]}
        self.assertTrue(self.starter(lambda argv: ProbeOutput(0)).start("default", running=running, snapshot=snapshot))

    def test_a_unit_that_fails_with_nothing_running_is_a_failed_start(self):
        with self.assertRaises(HerdrUnavailable) as caught:
            self.starter(lambda argv: ProbeOutput(1)).start("default", running=lambda: False,
                                                            snapshot=lambda: {})
        self.assertEqual(caught.exception.args[0], "herdr_session_start_failed")

    def test_a_server_that_never_answers_is_a_failed_start_not_a_hang(self):
        with self.assertRaises(HerdrUnavailable) as caught:
            self.starter(lambda argv: ProbeOutput(0), wait_seconds=3).start(
                "default", running=lambda: False, snapshot=lambda: {})
        self.assertEqual(caught.exception.args[0], "herdr_session_start_failed")

    def test_a_lost_race_is_still_a_start(self):
        # Another starter won: our unit failed ("server is already running"),
        # but the session is up, which is all the caller asked for.
        state = {"polls": 0}

        def running():
            state["polls"] += 1
            return state["polls"] > 1
        self.assertTrue(self.starter(lambda argv: ProbeOutput(1)).start(
            "default", running=running, snapshot=lambda: {"workspaces": [{}]}))


class SessionStartApiTests(test_herdr_bridge.HerdrApiTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.listing = copy.deepcopy(LISTING)
        self.choices_path = Path(self.temp.name) / "herdr-sessions.json"
        self.service.herdr_choices = HerdrSessionChoices(self.choices_path)
        self.service._herdr_sessions = HerdrSessions(runner=lambda argv: self.listing, home=Path("/home/u/.config/herdr"))
        self.service.herdr_audit = HerdrWriteAudit()
        self.unit_calls = []
        self.snapshots = {}

        def runner(argv):
            self.unit_calls.append(argv)
            if argv[0] == "systemd-run":
                name = argv[-2]
                rows = [row for row in self.listing["sessions"] if row["name"] == name]
                if rows:
                    rows[0]["running"] = True
                else:
                    self.listing["sessions"].append({
                        "default": False, "name": name, "running": True,
                        "session_dir": f"/home/u/.config/herdr/sessions/{name}",
                        "socket_path": f"/home/u/.config/herdr/sessions/{name}/herdr.sock"})
            return ProbeOutput(0)
        self.home = Path(self.temp.name) / "home"
        self.service.herdr_starter = HerdrSessionStarter(runner=runner, home=self.home, sleep=lambda _: None)
        self.service._herdr_fresh_snapshot = lambda name, socket_path=None: self.snapshots.get(
            name, {"workspaces": [{"workspace_id": "w1"}]})

    async def post(self, path, body=None, token=None):
        async with self.client.post(self.url + path, json=body if body is not None else {},
                                    headers={"Authorization": "Bearer " + (token or self.token)}) as response:
            return response.status, await response.json()

    async def test_a_stopped_session_is_started_and_this_device_lands_in_it(self):
        other = self.hub.register_device("phone-b")
        status, body = await self.post("/v1/herdr/sessions/default/start")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"contract_revision": "omodachi.v1", "selected": "default", "owned": "omodachi",
                                "scope": "device", "started": True, "created": False})
        self.assertEqual(self.unit_calls[0][:3], ("systemd-run", "--user", "--unit=omodachi-herdr-session-default"))
        # It had its workspaces back from Herdr's own session.json: none made.
        self.assertEqual(len(self.unit_calls), 1)
        self.assertEqual(self.service.herdr_choices.selected("phone-a"), "default")
        self.assertEqual(self.service.herdr_choices.selected("phone-b"), "omodachi")
        line = self.service.herdr_audit.lines[-1]
        self.assertEqual((line["kind"], line["session"], line["outcome"], line["device"]),
                         ("session_start", "default", "ok", "phone-a"))
        # Again: it is running, nothing is started, the device is still moved.
        status, body = await self.post("/v1/herdr/sessions/default/start", token=other)
        self.assertEqual((status, body["started"]), (200, False))
        self.assertEqual(len(self.unit_calls), 1)
        self.assertEqual(self.service.herdr_choices.selected("phone-b"), "default")

    async def test_start_never_invents_a_session(self):
        status, body = await self.post("/v1/herdr/sessions/nothere/start")
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_session"))
        self.assertEqual(self.unit_calls, [])
        for name in ("--takeover", "x" * 80):
            async with self.client.post(self.url + f"/v1/herdr/sessions/{name}/start", json={},
                                        headers=self.headers()) as response:
                self.assertIn(response.status, (400, 404), name)

    async def test_a_new_session_is_made_in_home_and_this_device_lands_in_it(self):
        self.snapshots["h5test"] = {"workspaces": []}
        status, body = await self.post("/v1/herdr/sessions", {"name": "h5test"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["selected"], body["started"], body["created"]), ("h5test", True, True))
        self.assertEqual(self.unit_calls[0][-4:], ("herdr", "--session", "h5test", "server"))
        self.assertIn("--working-directory=" + str(self.home), self.unit_calls[0])
        self.assertEqual(self.unit_calls[1], ("herdr", "--session", "h5test", "workspace", "create",
                                              "--cwd", str(self.home), "--no-focus"))
        self.assertEqual(self.service.herdr_audit.lines[-1]["kind"], "session_create")
        self.assertEqual(self.service.herdr_choices.selected("phone-a"), "h5test")

    async def test_a_name_the_host_has_is_not_made_again(self):
        for name in ("default", "omodachi"):
            status, body = await self.post("/v1/herdr/sessions", {"name": name})
            self.assertEqual((status, body["error"]["code"]), (409, "herdr_session_exists"), name)
        self.assertEqual(self.unit_calls, [])

    async def test_a_bad_name_or_body_is_refused_before_anything_runs(self):
        for name in ("..", "-x", "a/b", "x" * 33, "", 7):
            status, body = await self.post("/v1/herdr/sessions", {"name": name})
            self.assertEqual((status, body["error"]["code"]), (400, "invalid_session_name"), name)
        for payload in ({}, {"name": "ok", "cwd": "/etc"}):
            status, body = await self.post("/v1/herdr/sessions", payload)
            self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"), payload)
        async with self.client.post(self.url + "/v1/herdr/sessions?cwd=/etc", json={"name": "ok"},
                                    headers=self.headers()) as response:
            self.assertEqual(response.status, 400)
        self.assertEqual(self.unit_calls, [])

    async def test_a_start_that_fails_says_so_and_moves_nobody(self):
        self.service.herdr_starter = HerdrSessionStarter(runner=lambda argv: ProbeOutput(1), home=self.home,
                                                         sleep=lambda _: None)
        status, body = await self.post("/v1/herdr/sessions/default/start")
        self.assertEqual((status, body["error"]["code"]), (503, "herdr_session_start_failed"))
        self.assertEqual(self.service.herdr_choices.selected("phone-a"), "omodachi")
        self.assertEqual(self.service.herdr_audit.lines[-1]["outcome"], "herdr_session_start_failed")

    async def test_the_two_writes_are_odomo_s_too_and_still_need_a_credential(self):
        # ODOMO-2: Odomo's Herdr panel starts and makes sessions like the App.
        self.assertTrue(browser_gate.web_allows("POST", "/v1/herdr/sessions"))
        self.assertTrue(browser_gate.web_allows("POST", "/v1/herdr/sessions/default/start"))
        self.assertFalse(browser_gate.web_allows("POST", "/v1/herdr/sessions/../start"))
        async with self.client.post(self.url + "/v1/herdr/sessions/default/start", json={}) as response:
            self.assertEqual(response.status, 401)


# The bridge's own API tests run in their module; only these run here.
for _name in dir(test_herdr_bridge.HerdrApiTests):
    if _name.startswith("test_") and _name not in SessionStartApiTests.__dict__:
        setattr(SessionStartApiTests, _name, None)


if __name__ == "__main__":
    unittest.main()
