"""The owned Herdr session: layout projection, fixed argv, and both streams.

The stream tests run a real subprocess that speaks the NDJSON `terminal
session observe|control` protocol recorded from herdr 0.8.2 - one frame per
line on stdout, the four control commands on stdin - so the bridge is exercised
as a pipe between two real processes, not against a mock.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest

import aiohttp
from omodachi_core.bootstrap import create_service
from omodachi_core.herdr_bridge import HerdrBridge, HerdrUnavailable, control_command
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer

SNAPSHOT = json.loads((Path(__file__).parents[1] / "contracts/fixtures/herdr/snapshot.json").read_text())

# A stand-in for `herdr terminal session observe|control`, matching the observed
# 0.8.2 behaviour: observe ignores stdin entirely, control answers
# terminal.input with a frame and terminal.release with terminal.closed.
FAKE_HERDR = '''#!/usr/bin/env python3
import base64, json, sys
argv = sys.argv[1:]
mode = argv[argv.index("session") + 1]
pane = argv[argv.index(mode) + 1]
cols = int(argv[argv.index("--cols") + 1])
rows = int(argv[argv.index("--rows") + 1])
seq = 1
def frame(text, full):
    global seq
    print(json.dumps({"type": "terminal.frame", "seq": seq, "encoding": "ansi", "full": full,
                      "width": cols, "height": rows,
                      "bytes": base64.b64encode(text.encode()).decode()}), flush=True)
    seq += 1
frame(pane + " ready", True)
if mode == "observe":
    sys.stdin.read()
    sys.exit(0)
for line in sys.stdin:
    try:
        command = json.loads(line)
    except ValueError:
        continue
    if command["type"] == "terminal.input":
        frame(command.get("text", ""), False)
    elif command["type"] == "terminal.resize":
        cols, rows = command["cols"], command["rows"]
        frame("resized", True)
    elif command["type"] == "terminal.release":
        print(json.dumps({"type": "terminal.closed", "reason": "detached"}), flush=True)
        break
'''


class LayoutTests(unittest.TestCase):
    def bridge(self, snapshot=None):
        return HerdrBridge(runner=lambda argv: snapshot if snapshot is not None else SNAPSHOT)

    def test_snapshot_becomes_workspaces_tabs_and_panes(self):
        layout = self.bridge().layout()
        self.assertEqual(layout["session"], "omodachi")
        self.assertEqual(layout["protocol"], 20)
        self.assertEqual(layout["focused"]["pane_id"], "w1:p1")
        panes = layout["workspaces"][0]["tabs"][0]["panes"]
        self.assertEqual([pane["id"] for pane in panes], ["w1:p1", "w1:p2"])
        self.assertEqual(panes[0]["size"], {"cols": 47, "rows": 39})
        self.assertEqual(panes[0]["title"], "user@host:~")
        self.assertIsNone(panes[0]["agent"])
        self.assertEqual(panes[1]["agent"], {"name": "default", "kind": "codex", "status": "working"})
        self.assertEqual(panes[1]["agent_status"], "working")

    def test_zoom_belongs_to_the_tab_and_only_its_focused_pane_reads_as_zoomed(self):
        value = json.loads(json.dumps(SNAPSHOT))
        value["snapshot"]["layouts"][0]["zoomed"] = True
        layout = self.bridge(value).layout()
        tab = layout["workspaces"][0]["tabs"][0]
        self.assertTrue(tab["zoomed"])
        self.assertEqual([pane["zoomed"] for pane in tab["panes"]], [True, False])

    def test_revision_moves_only_when_the_projection_moves(self):
        state = {"value": json.loads(json.dumps(SNAPSHOT))}
        bridge = HerdrBridge(runner=lambda argv: state["value"])
        self.assertEqual(bridge.layout()["revision"], 1)
        self.assertEqual(bridge.layout()["revision"], 1)
        state["value"]["snapshot"]["panes"][0]["focused"] = False
        self.assertEqual(bridge.layout()["revision"], 2)

    def test_an_unreadable_session_is_reported(self):
        def runner(argv):
            raise HerdrUnavailable("herdr_unavailable")
        with self.assertRaises(HerdrUnavailable):
            HerdrBridge(runner=runner).layout()


class ArgvTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.bridge = HerdrBridge(runner=lambda argv: self.calls.append(argv) or {"ok": True})

    def test_every_action_is_a_fixed_official_subcommand(self):
        self.bridge.pane_action("w1:p1", "split", {"direction": "down", "ratio": 0.25})
        self.bridge.pane_action("w1:p1", "zoom", {"mode": "on"})
        self.bridge.pane_action("w1:p2", "close", {})
        self.bridge.pane_action("w1:p1", "focus", {"direction": "right"})
        self.bridge.workspace_select("w1")
        self.assertEqual(self.calls, [
            ("herdr", "--session", "omodachi", "pane", "split", "--pane", "w1:p1",
             "--direction", "down", "--ratio", "0.2500"),
            ("herdr", "--session", "omodachi", "pane", "zoom", "--pane", "w1:p1", "--on"),
            ("herdr", "--session", "omodachi", "pane", "close", "w1:p2"),
            ("herdr", "--session", "omodachi", "pane", "focus", "--pane", "w1:p1", "--direction", "right"),
            ("herdr", "--session", "omodachi", "workspace", "focus", "w1"),
        ])

    def test_client_values_can_never_reach_the_command_line(self):
        for pane in ("w1:p1; rm -rf /", "--takeover", "w1:t1", "", "w9:p1 "):
            with self.assertRaises(HerdrUnavailable):
                self.bridge.pane_action(pane, "zoom", {"mode": "on"})
        for payload in ({"mode": "sideways"}, {"mode": "on", "extra": 1}):
            with self.assertRaises(HerdrUnavailable):
                self.bridge.pane_action("w1:p1", "zoom", payload)
        for payload in ({"direction": "sideways"}, {"direction": "right", "ratio": 9}):
            with self.assertRaises(HerdrUnavailable):
                self.bridge.pane_action("w1:p1", "split", payload)
        with self.assertRaises(HerdrUnavailable):
            self.bridge.workspace_select("w1:t1")
        with self.assertRaises(HerdrUnavailable):
            self.bridge.pane_action("w1:p1", "send-keys", {})
        self.assertEqual(self.calls, [])

    def test_only_the_four_control_commands_pass_the_envelope_check(self):
        for value in ({"type": "terminal.input", "text": "ls\\n"},
                      {"type": "terminal.resize", "cols": 100, "rows": 40},
                      {"type": "terminal.scroll", "lines": 3},
                      {"type": "terminal.release"}):
            self.assertEqual(control_command(value), value)
        for value in ({"type": "pane.close"}, {"type": "terminal.input", "text": {"a": 1}},
                      {"method": "server.stop"}, "terminal.release",
                      {"type": "terminal.resize", "cols": 0, "rows": 40}):
            with self.assertRaises(HerdrUnavailable):
                control_command(value)

    def test_one_controller_per_pane(self):
        self.bridge.claim_control("w1:p1", "a")
        with self.assertRaises(HerdrUnavailable):
            self.bridge.claim_control("w1:p1", "b")
        self.bridge.claim_control("w1:p2", "b")
        self.bridge.release_control("w1:p1", "b")  # not the owner: no effect
        with self.assertRaises(HerdrUnavailable):
            self.bridge.claim_control("w1:p1", "c")
        self.bridge.release_control("w1:p1", "a")
        self.bridge.claim_control("w1:p1", "c")


class HerdrApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fake = Path(self.temp.name) / "herdr-fake"
        self.fake.write_text(FAKE_HERDR.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)
        self.calls = []
        self.hub = Hub(auth_check_interval=0.05)
        self.token = self.hub.register_device("phone-a")
        self.service = create_service(self.hub, demo=True)
        bridge = HerdrBridge(runner=lambda argv: self.calls.append(argv) or SNAPSHOT)
        bridge.argv = lambda *arguments: (str(self.fake), "--session", "omodachi", *arguments)
        self.bridge = self.service.herdr_bridge = bridge
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))

    async def asyncTearDown(self):
        await self.client.close()
        await asyncio.wait_for(self.server.close(), 3)
        self.temp.cleanup()

    def headers(self):
        return {"Authorization": "Bearer " + self.token}

    async def frames(self, ws, count, timeout=5.0):
        seen = []
        while len(seen) < count:
            message = await asyncio.wait_for(ws.receive(), timeout)
            if message.type is not aiohttp.WSMsgType.TEXT:
                raise AssertionError(f"stream ended: {message.type} {message.data!r}")
            seen.append(json.loads(message.data))
        return seen

    async def test_tabs_are_created_and_closed_inside_one_owned_workspace(self):
        async with self.client.post(self.url + "/v1/herdr/workspaces/w1/tabs", headers=self.headers(),
                                    json={"label": "Notes", "focus": True}) as response:
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual((await response.json())["action"], "create")
        self.assertEqual(self.calls[-1][3:], ("tab", "create", "--workspace", "w1", "--label", "Notes", "--focus"))
        async with self.client.post(self.url + "/v1/herdr/workspaces/w1/tabs", headers=self.headers(),
                                    json={}) as response:
            self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.calls[-1][3:], ("tab", "create", "--workspace", "w1", "--no-focus"))
        async with self.client.delete(self.url + "/v1/herdr/workspaces/w1/tabs/w1:t2",
                                      headers=self.headers()) as response:
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual((await response.json())["tab"], "w1:t2")
        self.assertEqual(self.calls[-1][3:], ("tab", "close", "w1:t2"))
        # A tab belonging to a different workspace is not this workspace's.
        async with self.client.delete(self.url + "/v1/herdr/workspaces/w1/tabs/w2:t1",
                                      headers=self.headers()) as response:
            self.assertEqual(response.status, 400)
        for payload in ({"label": "a\nb"}, {"focus": "yes"}, {"cwd": "/etc"}):
            async with self.client.post(self.url + "/v1/herdr/workspaces/w1/tabs",
                                        headers=self.headers(), json=payload) as response:
                self.assertEqual(response.status, 400, payload)

    async def test_layout_needs_a_credential_and_reports_the_owned_session(self):
        async with self.client.get(self.url + "/v1/herdr/layout") as response:
            self.assertEqual(response.status, 401)
        async with self.client.get(self.url + "/v1/herdr/layout", headers=self.headers()) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()
        self.assertEqual(payload["session"], "omodachi")
        self.assertEqual(payload["contract_revision"], "omodachi.v1")

    async def test_observe_streams_frames_and_serves_a_resize_by_restarting(self):
        url = self.url + "/v1/herdr/panes/w1:p1/observe?cols=80&rows=24"
        async with self.client.ws_connect(url, headers=self.headers()) as ws:
            first = (await self.frames(ws, 1))[0]
            self.assertEqual(first["type"], "terminal.frame")
            self.assertTrue(first["full"])
            self.assertEqual((first["width"], first["height"]), (80, 24))
            self.assertEqual(base64.b64decode(first["bytes"]).decode(), "w1:p1 ready")
            await ws.send_str(json.dumps({"type": "resize", "cols": 100, "rows": 40}))
            second = (await self.frames(ws, 1))[0]
            self.assertEqual((second["width"], second["height"]), (100, 40))
            self.assertTrue(second["full"])

    async def test_control_passes_the_four_commands_through_and_refuses_anything_else(self):
        url = self.url + "/v1/herdr/panes/w1:p1/control"
        async with self.client.ws_connect(url, headers=self.headers()) as ws:
            await self.frames(ws, 1)
            await ws.send_str(json.dumps({"type": "terminal.input", "text": "echo omodachi\n"}))
            echoed = (await self.frames(ws, 1))[0]
            self.assertEqual(base64.b64decode(echoed["bytes"]).decode(), "echo omodachi\n")
            self.assertFalse(echoed["full"])
            await ws.send_str(json.dumps({"type": "terminal.resize", "cols": 120, "rows": 30}))
            resized = (await self.frames(ws, 1))[0]
            self.assertEqual((resized["width"], resized["height"]), (120, 30))
            await ws.send_str(json.dumps({"type": "pane.close"}))
            closed = await asyncio.wait_for(ws.receive(), 5)
            self.assertIs(closed.type, aiohttp.WSMsgType.CLOSE)
            self.assertEqual(closed.extra, "invalid_control_command")

    async def test_release_closes_the_stream_and_frees_the_pane_for_the_next_client(self):
        url = self.url + "/v1/herdr/panes/w1:p1/control"
        async with self.client.ws_connect(url, headers=self.headers()) as ws:
            await self.frames(ws, 1)
            # A second controller of the same pane is refused before the upgrade.
            with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
                await self.client.ws_connect(url, headers=self.headers())
            self.assertEqual(caught.exception.status, 409)
            await ws.send_str(json.dumps({"type": "terminal.release"}))
            self.assertEqual((await self.frames(ws, 1))[0]["type"], "terminal.closed")
            await asyncio.wait_for(ws.receive(), 5)
        for _ in range(50):
            if "w1:p1" not in self.bridge._controllers:
                break
            await asyncio.sleep(0.05)
        async with self.client.ws_connect(url, headers=self.headers()) as ws:
            await self.frames(ws, 1)

    async def test_observing_the_same_pane_twice_is_allowed(self):
        url = self.url + "/v1/herdr/panes/w1:p2/observe"
        async with self.client.ws_connect(url, headers=self.headers()) as first:
            async with self.client.ws_connect(url, headers=self.headers()) as second:
                self.assertEqual((await self.frames(first, 1))[0]["type"], "terminal.frame")
                self.assertEqual((await self.frames(second, 1))[0]["type"], "terminal.frame")

    async def test_pane_and_geometry_are_validated_before_a_process_is_started(self):
        for path in ("/v1/herdr/panes/w1:t1/observe", "/v1/herdr/panes/w1:p1/observe?cols=0&rows=24",
                     "/v1/herdr/panes/w1:p1/observe?cols=80&rows=100000"):
            with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
                await self.client.ws_connect(self.url + path, headers=self.headers())
            self.assertEqual(caught.exception.status, 400)

    async def test_pane_actions_and_workspace_select_reach_the_official_commands(self):
        async with self.client.post(self.url + "/v1/herdr/panes/w1:p1/split",
                                    headers=self.headers(), json={"direction": "right"}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["action"], "split")
        async with self.client.post(self.url + "/v1/herdr/workspaces/w1/select",
                                    headers=self.headers(), json={}) as response:
            self.assertEqual(response.status, 200)
        async with self.client.post(self.url + "/v1/herdr/panes/w1:p1/zoom",
                                    headers=self.headers(), json={"mode": "sideways"}) as response:
            self.assertEqual(response.status, 400)
        self.assertIn(("pane", "split", "--pane", "w1:p1", "--direction", "right"), [call[3:] for call in self.calls])
        self.assertIn(("workspace", "focus", "w1"), [call[3:] for call in self.calls])

    async def test_a_host_without_the_owned_session_reports_it(self):
        self.service.herdr_bridge = None
        async with self.client.get(self.url + "/v1/herdr/layout", headers=self.headers()) as response:
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())["error"]["code"], "herdr_unavailable")


SESSION_LIST = json.loads((Path(__file__).parents[1]
                           / "contracts/fixtures/herdr/session-list.json").read_text())


class SessionListingTests(unittest.TestCase):
    """HERDR-2 §2: `herdr session list --json` is the whole enumeration."""

    #: The listing was recorded on the real host, so the home a socket must sit
    #: inside is that host's, not this machine's.
    HOME = Path("/home/alex/.config/herdr")

    def rows(self, document=None):
        from omodachi_core.herdr_bridge import HerdrSessions
        return HerdrSessions(runner=lambda argv: document or SESSION_LIST, home=self.HOME).rows()

    def test_the_listing_names_every_session_and_marks_the_one_core_owns(self):
        rows = self.rows()
        self.assertEqual([row["name"] for row in rows], ["omodachi", "default", "herdr2-probe"])
        self.assertEqual([row["owned"] for row in rows], [True, False, False])
        self.assertEqual([row["herdr_default"] for row in rows], [False, True, False])
        self.assertEqual([row["running"] for row in rows], [True, True, False])

    def test_the_default_session_socket_is_read_not_constructed(self):
        # herdr 0.8.2 keeps the default session's socket at the root of its
        # home; constructing `sessions/default/herdr.sock` would miss it.
        rows = {row["name"]: row for row in self.rows()}
        self.assertTrue(rows["default"]["socket_path"].endswith("/.config/herdr/herdr.sock"))
        self.assertTrue(rows["omodachi"]["socket_path"]
                        .endswith("/.config/herdr/sessions/omodachi/herdr.sock"))

    def test_a_socket_outside_herdrs_own_home_is_refused_rather_than_followed(self):
        document = {"sessions": [{"name": "evil", "running": True, "default": False,
                                  "socket_path": "/tmp/anywhere/herdr.sock"}]}
        self.assertIsNone(self.rows(document)[0]["socket_path"])

    def test_a_name_outside_the_alphabet_is_not_listed_at_all(self):
        document = {"sessions": [{"name": "--takeover", "running": True},
                                 {"name": "a/b", "running": True},
                                 {"name": "", "running": True},
                                 {"name": "ok-1", "running": True}]}
        self.assertEqual([row["name"] for row in self.rows(document)], ["ok-1"])

    def test_a_document_that_is_not_a_listing_is_unavailable(self):
        from omodachi_core.herdr_bridge import HerdrSessions
        with self.assertRaises(HerdrUnavailable):
            HerdrSessions(runner=lambda argv: {"result": {}}).rows()


class SessionChoiceTests(unittest.TestCase):
    """A choice is per device, survives a restart, and never invents a name."""

    def store(self):
        from omodachi_core.herdr_bridge import HerdrSessionChoices
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "herdr-sessions.json"
        return HerdrSessionChoices(self.path)

    def test_every_device_starts_on_the_session_core_owns(self):
        self.assertEqual(self.store().selected("phone-a"), "omodachi")

    def test_one_device_choosing_does_not_move_another(self):
        store = self.store()
        store.choose("default", "phone-a")
        self.assertEqual(store.selected("phone-a"), "default")
        self.assertEqual(store.selected("phone-b"), "omodachi")

    def test_the_choice_is_read_back_from_disk_at_0600(self):
        from omodachi_core.herdr_bridge import HerdrSessionChoices
        self.store().choose("default", "phone-a")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(HerdrSessionChoices(self.path).selected("phone-a"), "default")

    def test_the_host_wide_default_is_the_fallback_under_a_device_choice(self):
        store = self.store()
        store.choose("default")
        self.assertEqual(store.selected("phone-b"), "default")
        store.choose("omodachi", "phone-b")
        self.assertEqual(store.selected("phone-b"), "omodachi")
        self.assertEqual(store.selected("phone-c"), "default")

    def test_a_name_that_is_not_a_session_name_is_refused(self):
        store = self.store()
        for value in ("--takeover", "a b", "a/b", "", None, 7, "x" * 65):
            with self.assertRaises(HerdrUnavailable):
                store.choose(value, "phone-a")

    def test_a_corrupt_file_reads_as_no_choice_rather_than_failing(self):
        from omodachi_core.herdr_bridge import HerdrSessionChoices
        self.store().choose("default", "phone-a")
        self.path.write_text("{not json")
        self.assertEqual(HerdrSessionChoices(self.path).selected("phone-a"), "omodachi")


class SessionApiTests(HerdrApiTests):
    """`GET /v1/herdr/sessions` and `POST /v1/herdr/sessions/{name}/select`."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from omodachi_core.herdr_bridge import HerdrSessionChoices, HerdrSessions
        self.choices_path = Path(self.temp.name) / "herdr-sessions.json"
        self.service.herdr_choices = HerdrSessionChoices(self.choices_path)
        self.service._herdr_sessions = HerdrSessions(runner=lambda argv: SESSION_LIST)
        self.other_calls = []
        # A bridge for the other session, wired to the same fake binary so the
        # test can prove which `--session` the argv carried.
        original = self.service._herdr_shape
        def shape(name, socket_path=None):
            return original(name, socket_path) if name == "omodachi" else {
                "readable": True, "workspaces": 2, "tabs": 3, "panes": 5, "agents": 1,
                "protocol": 20, "version": "0.8.2"}
        self.service._herdr_shape = shape

    def named_bridge(self, name):
        bridge = HerdrBridge(name, runner=lambda argv: self.other_calls.append(argv) or SNAPSHOT)
        bridge.argv = lambda *arguments: (str(self.fake), "--session", name, *arguments)
        return bridge

    async def test_the_listing_is_every_session_and_the_one_this_device_is_on(self):
        async with self.client.get(self.url + "/v1/herdr/sessions") as response:
            self.assertEqual(response.status, 401)
        async with self.client.get(self.url + "/v1/herdr/sessions", headers=self.headers()) as response:
            self.assertEqual(response.status, 200, await response.text())
            payload = await response.json()
        self.assertEqual(payload["contract_revision"], "omodachi.v1")
        self.assertEqual(payload["selected"], "omodachi")
        self.assertEqual(payload["owned"], "omodachi")
        self.assertEqual([row["name"] for row in payload["sessions"]],
                         ["omodachi", "default", "herdr2-probe"])
        stopped = payload["sessions"][-1]
        # A stopped session is listed, not hidden, and says it could not be read.
        self.assertFalse(stopped["readable"])
        self.assertIsNone(stopped["panes"])
        self.assertTrue(payload["sessions"][0]["readable"])

    async def test_selecting_moves_this_device_and_leaves_the_other_alone(self):
        other = self.hub.register_device("phone-b")
        self.service._herdr_bridges["default"] = self.named_bridge("default")
        async with self.client.post(self.url + "/v1/herdr/sessions/default/select",
                                    headers=self.headers(), json={}) as response:
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual((await response.json())["selected"], "default")
        async with self.client.get(self.url + "/v1/herdr/layout", headers=self.headers()) as response:
            self.assertEqual((await response.json())["session"], "default")
        async with self.client.get(self.url + "/v1/herdr/layout",
                                   headers={"Authorization": "Bearer " + other}) as response:
            self.assertEqual((await response.json())["session"], "omodachi")

    async def test_the_stream_and_the_actions_follow_the_selected_session(self):
        self.service._herdr_bridges["default"] = self.named_bridge("default")
        async with self.client.post(self.url + "/v1/herdr/sessions/default/select",
                                    headers=self.headers(), json={}) as response:
            self.assertEqual(response.status, 200)
        async with self.client.post(self.url + "/v1/herdr/panes/w1:p1/zoom", headers=self.headers(),
                                    json={"mode": "toggle"}) as response:
            self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.other_calls[-1][:4], (str(self.fake), "--session", "default", "pane"))
        url = self.url + "/v1/herdr/panes/w1:p1/observe?cols=80&rows=24"
        async with self.client.ws_connect(url, headers=self.headers()) as ws:
            await self.frames(ws, 1)
        # The owned session's bridge was never asked to do any of it.
        self.assertNotIn("default", [call[2] for call in self.calls])

    async def test_a_session_that_is_not_running_or_not_listed_is_refused(self):
        for name in ("herdr2-probe", "nothere"):
            async with self.client.post(self.url + f"/v1/herdr/sessions/{name}/select",
                                        headers=self.headers(), json={}) as response:
                self.assertEqual(response.status, 400, name)
                self.assertEqual((await response.json())["error"]["code"], "invalid_session")
        # …and the device is still where it was.
        async with self.client.get(self.url + "/v1/herdr/layout", headers=self.headers()) as response:
            self.assertEqual((await response.json())["session"], "omodachi")

    async def test_a_name_that_could_be_a_flag_never_reaches_the_router(self):
        for name in ("--takeover", "a%2Fb", "x" * 80):
            async with self.client.post(self.url + f"/v1/herdr/sessions/{name}/select",
                                        headers=self.headers(), json={}) as response:
                self.assertIn(response.status, (400, 404), name)

    async def test_the_layout_event_names_the_session_it_moved(self):
        self.service._herdr_bridges["default"] = self.named_bridge("default")
        published = []
        self.hub.publish = lambda topic, payload, **kwargs: published.append((topic, payload))
        await asyncio.to_thread(self.service.notify_herdr_layout)
        self.assertEqual({row[1]["session"] for row in published}, {"omodachi", "default"})
        self.assertTrue(all(row[0] == "herdr.layout.changed" for row in published))
