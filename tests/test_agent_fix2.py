"""AGENT-FIX-2: the chat history vanished while the agent ran a tool.

Leo (2026-10-05): "agent 在工具调用的时候，聊天记录会消失". Core answered every
App open, event-stream connect and ensure by reading `thread/read` and
replacing its rows with the answer. codex 0.160 keeps finished items only (a
paginated thread stores an item when it completes), so the running command and
the reply being written left every phone, their later deltas had no row to
land in, and the turn was reordered by finish time. Output deltas re-sent a
tool's whole output each time, so a command that printed a lot flooded the
stream until it broke.

Controlled fakes only: a peer that answers `thread/read` / `thread/resume` the
way codex 0.160 does (finished items, in finish order), the real adapter, the
real service and the real HTTP routes. Never a user's codex daemon.
"""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import aiohttp

from omodachi_core.agent import ProbeStatus
from omodachi_core.agent_chat_provider import (
    AgentChatError, CodexDefaultAgentChat, DefaultAgentBinding, OUTPUT_FLUSH_SECONDS, SNAPSHOT_DETAIL_BUDGET,
    SNAPSHOT_OLD_DETAIL, TOOL_DETAIL_LIMIT, budget_rows, merge_rows)
from omodachi_core.agent_chat_service import DefaultAgentChatService
from omodachi_core.agent_lifecycle import DefaultAgentManager
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer

THREAD = "thread-af2"


class Codex:
    """Finished items only, in the order they finished - as codex 0.160 stores
    a paginated thread. `history` is what a read or resume returns."""

    def __init__(self):
        self.calls = []
        self.history = [{"id": "turn-0", "status": "completed", "items": [
            {"type": "userMessage", "id": "u0", "content": [{"type": "text", "text": "hello"}]},
            {"type": "agentMessage", "id": "a0", "text": "Hi."}]}]
        self.ordinal = 0
        self.on_notification = lambda value, ordinal: None
        self.on_server_request = lambda value: None
        self.on_disconnect = lambda: None

    async def initialize(self): return {}
    async def close(self): pass
    async def respond(self, request_id, result): self.calls.append(("respond", request_id))
    async def respond_error(self, request_id, code, message): pass

    async def request(self, method, params):
        result, _ = await self.request_fenced(method, params)
        return result

    async def request_fenced(self, method, params):
        self.calls.append((method, dict(params)))
        self.ordinal += 1
        if method == "thread/loaded/list":
            return {"data": [THREAD], "nextCursor": None}, self.ordinal
        if method in ("thread/resume", "thread/read"):
            return {"thread": {"id": THREAD, "turns": json.loads(json.dumps(self.history))}}, self.ordinal
        if method == "turn/start":
            return {"turn": {"id": "turn-1", "status": "inProgress"}}, self.ordinal
        if method == "thread/queue/list":  # AGENT-QUEUE-1: a codex without the queue
            raise AgentChatError("provider_request_rejected")
        raise AssertionError(method)

    def notify(self, method, **params):
        self.ordinal += 1
        self.on_notification({"method": method, "params": {"threadId": THREAD, **params}}, self.ordinal)


def command(ident, status="inProgress", output=None):
    return {"type": "commandExecution", "id": ident, "command": "make " + ident, "status": status,
            "aggregatedOutput": output}


async def tool_turn(codex):
    """turn-1: the user's words, a commentary line, then a command still running."""
    codex.notify("turn/started", turn={"id": "turn-1", "status": "inProgress"})
    codex.notify("item/started", turnId="turn-1",
                 item={"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "build it"}]})
    codex.notify("item/completed", turnId="turn-1",
                 item={"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "build it"}]})
    codex.notify("item/started", turnId="turn-1", item={"type": "agentMessage", "id": "a1", "text": ""})
    codex.notify("item/agentMessage/delta", turnId="turn-1", itemId="a1", delta="Building now.")
    codex.notify("item/completed", turnId="turn-1", item={"type": "agentMessage", "id": "a1", "text": "Building now."})
    codex.notify("item/started", turnId="turn-1", item=command("c1"))
    # What codex has stored so far: the finished items of turn-1.
    codex.history.append({"id": "turn-1", "status": "inProgress", "items": [
        {"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "build it"}]},
        {"type": "agentMessage", "id": "a1", "text": "Building now."}]})


def drain(chat):
    events = []
    while not chat.events.empty():
        events.append(chat.events.get_nowait()["event"])
    return events


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.codex = Codex()
        self.chat = CodexDefaultAgentChat(DefaultAgentBinding("host", THREAD, "ws://127.0.0.1:47311", "f" * 64),
                                          self.codex)
        await self.chat.attach()
        drain(self.chat)

    def ids(self, snapshot):
        return [(row["id"], row.get("status") or row.get("role")) for row in snapshot["rows"]]

    async def test_a_snapshot_during_a_tool_keeps_the_history_and_the_running_command(self):
        await tool_turn(self.codex)
        reads = len([call for call in self.codex.calls if call[0] == "thread/read"])
        for _ in range(3):  # App open, events connect, a second device's ensure
            snapshot = await self.chat.snapshot()
            self.assertEqual(self.ids(snapshot), [("u0", "user"), ("a0", "assistant"), ("u1", "user"),
                                                  ("a1", "assistant"), ("c1", "running")])
            self.assertEqual(snapshot["activeTurn"], "turn-1")
        self.assertEqual(len([call for call in self.codex.calls if call[0] == "thread/read"]), reads,
                         "the live conversation is not re-read from codex")
        # The command's output after the snapshot still lands in its row.
        self.codex.notify("item/commandExecution/outputDelta", turnId="turn-1", itemId="c1", delta="ok\n")
        snapshot = await self.chat.snapshot()
        self.assertEqual(snapshot["rows"][-1]["detail"], "ok\n")

    async def test_approvals_and_an_interrupt_leave_the_history_in_place(self):
        await tool_turn(self.codex)
        self.codex.on_server_request({"id": 0, "method": "item/commandExecution/requestApproval",
                                      "params": {"threadId": THREAD, "turnId": "turn-1", "itemId": "c1",
                                                 "command": "make c1"}})
        snapshot = await self.chat.snapshot()
        self.assertEqual(len(snapshot["rows"]), 5)
        self.assertEqual(snapshot["pendingApprovals"][0]["details"]["item_id"], "c1")
        await self.chat.resolve_approval("0", decision="accept")
        self.codex.notify("turn/completed", turn={"id": "turn-1", "status": "interrupted"})
        snapshot = await self.chat.snapshot()
        self.assertEqual(self.ids(snapshot), [("u0", "user"), ("a0", "assistant"), ("u1", "user"),
                                              ("a1", "assistant"), ("c1", "interrupted")])
        self.assertIsNone(snapshot["activeTurn"])

    async def test_output_streams_as_increments_and_lands_before_the_end(self):
        await tool_turn(self.codex)
        drain(self.chat)
        for index in range(200):
            self.codex.notify("item/commandExecution/outputDelta", turnId="turn-1", itemId="c1",
                              delta="line %d\n" % index)
        self.assertEqual(drain(self.chat), [], "gathered, not one event per delta")
        await asyncio.sleep(OUTPUT_FLUSH_SECONDS * 3)
        events = drain(self.chat)
        self.assertEqual([event["type"] for event in events], ["tool.output"])
        self.assertEqual(events[0]["value"]["delta"], "".join("line %d\n" % i for i in range(200)))
        # An App or Odomo older than `tool.output` reads every event's value
        # as a row: it needs `kind` and `turnID` to decode it and move on.
        self.assertEqual((events[0]["value"]["kind"], events[0]["value"]["turnID"]), ("tool", "turn-1"))
        # Output still waiting goes out before the item's completion.
        self.codex.notify("item/commandExecution/outputDelta", turnId="turn-1", itemId="c1", delta="tail\n")
        self.codex.notify("item/completed", turnId="turn-1", item=command("c1", "completed", "all output"))
        self.assertEqual([event["type"] for event in drain(self.chat)], ["tool.output", "tool"])

    async def test_a_huge_output_is_clipped_to_its_newest_part_and_says_so(self):
        await tool_turn(self.codex)
        drain(self.chat)
        chunk = "x" * 50_000 + "\n"
        for _ in range(40):  # ~2 MB
            self.codex.notify("item/commandExecution/outputDelta", turnId="turn-1", itemId="c1", delta=chunk)
        await asyncio.sleep(OUTPUT_FLUSH_SECONDS * 3)
        events = drain(self.chat)
        self.assertTrue(all(len(json.dumps(event)) < 2 * TOOL_DETAIL_LIMIT for event in events))
        row = self.chat.rows["c1"]
        self.assertEqual(len(row["detail"]), TOOL_DETAIL_LIMIT)
        self.assertEqual(row["detail_truncated"], 40 * len(chunk) - TOOL_DETAIL_LIMIT)
        self.assertEqual(events[-1]["value"]["detail_truncated"], row["detail_truncated"])
        # A completed item carrying the whole output is clipped the same way.
        self.codex.notify("item/completed", turnId="turn-1", item=command("c1", "completed", chunk * 40))
        self.assertEqual(len(self.chat.rows["c1"]["detail"]), TOOL_DETAIL_LIMIT)

    async def test_a_snapshot_never_nears_a_phones_frame_limit(self):
        self.codex.notify("turn/started", turn={"id": "turn-1", "status": "inProgress"})
        for index in range(40):
            self.codex.notify("item/completed", turnId="turn-1",
                              item=command("c%d" % index, "completed", ("%d" % index) * 200_000))
        snapshot = await self.chat.snapshot()
        self.assertLess(len(json.dumps(snapshot)), 1_048_576 * 3 // 4)
        tools = [row for row in snapshot["rows"] if row["kind"] == "tool"]
        self.assertEqual(len(tools), 40, "every row is still there")
        self.assertEqual(len(tools[-1]["detail"]), TOOL_DETAIL_LIMIT, "the newest keeps the most")
        self.assertLessEqual(len(tools[0]["detail"]), SNAPSHOT_OLD_DETAIL)
        self.assertTrue(all(row.get("detail_truncated") for row in tools))
        self.assertEqual(len(self.chat.rows["c39"]["detail"]), TOOL_DETAIL_LIMIT, "core's own rows are not cut")

    async def test_a_reply_that_began_before_attach_still_streams(self):
        await tool_turn(self.codex)
        self.codex.notify("item/agentMessage/delta", turnId="turn-1", itemId="a9", delta="Late words")
        self.assertEqual(self.chat.rows["a9"]["text"], "Late words")


class MergeTests(unittest.TestCase):
    def row(self, ident, turn, status=None):
        if status:
            return {"kind": "tool", "id": ident, "turnID": turn, "name": ident, "status": status, "detail": ""}
        return {"kind": "message", "id": ident, "turnID": turn, "role": "assistant", "text": ident}

    def test_the_turn_in_flight_keeps_its_places_and_its_running_rows(self):
        shown = {r["id"]: r for r in [self.row("u0", "t0"), self.row("u1", "t1"), self.row("a1", "t1"),
                                      self.row("c1", "t1", "running"), self.row("a2", "t1")]}
        # codex: finished items only, in finish order (a2 finished before c1 did not)
        provider = {r["id"]: r for r in [self.row("u0", "t0"), self.row("u1", "t1"), self.row("a2", "t1"),
                                         self.row("a1", "t1")]}
        merged = merge_rows(provider, shown, "t1")
        self.assertEqual(list(merged), ["u0", "u1", "a1", "c1", "a2"])

    def test_a_finished_turn_is_the_providers(self):
        shown = {r["id"]: r for r in [self.row("u1", "t1"), self.row("x", "t1", "running")]}
        provider = {r["id"]: r for r in [self.row("u1", "t1"), self.row("c1", "t1", "succeeded")]}
        self.assertEqual(list(merge_rows(provider, shown, None)), ["u1", "c1"])

    def test_budget_cuts_old_rows_first(self):
        rows = [self.row("c%d" % i, "t", "succeeded") for i in range(3)]
        for row in rows:
            row["detail"] = "y" * 100
        cut = budget_rows(rows, budget=150, older=10)
        self.assertEqual([len(row["detail"]) for row in cut], [10, 50, 100])
        self.assertEqual([row.get("detail_truncated") for row in cut], [90, 50, None])
        self.assertEqual(len(rows[0]["detail"]), 100, "a copy")
        self.assertGreater(SNAPSHOT_DETAIL_BUDGET, TOOL_DETAIL_LIMIT)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    """The real service and HTTP routes: two phones and a reconnect during a
    tool turn."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        cap = SimpleNamespace(configured=True, configured_kind="codex", default_agent_exists=False,
                              default_agent_probe=ProbeStatus.MISSING)
        self.manager = DefaultAgentManager(root=root, probe=SimpleNamespace(inspect=lambda: (cap, None)),
                                           runner=lambda _: self.fail("no TUI"))
        self.codex = Codex()
        binding = DefaultAgentBinding("host", THREAD, "ws://127.0.0.1:47311", "f" * 64)

        class Owner:
            def owned_metadata(self): return None
            async def ensure_new_or_owned(self, existing_agent=False): return binding
            def tui_argv(self, b): return ["codex"]
            async def close_proxy(self): pass

        async def connect(b, **options):
            options.pop("owner_rpc", None)
            chat = CodexDefaultAgentChat(b, self.codex, **options)
            await chat.attach()
            return chat
        self.hub = Hub()
        self.token = self.hub.register_device("phone")
        self.chat = DefaultAgentChatService(self.manager, "host", owner=Owner(), connector=connect, hub=self.hub)
        self.service = create_service(self.hub, demo=True)
        self.service.agent_manager = self.manager
        self.service.agent_chat = self.chat
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.client = aiohttp.ClientSession()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.headers = {"Authorization": "Bearer " + self.token}

    async def asyncTearDown(self):
        await self.client.close()
        await self.chat.close()
        await self.server.close()
        self.tmp.cleanup()

    async def ensure(self):
        async with self.client.post(self.url + "/v1/agent/default:ensure", headers=self.headers,
                                    json={"surface": "chat"}) as response:
            self.assertEqual(response.status, 200)
            return (await response.json())["snapshot"]

    async def test_phones_opening_during_a_tool_see_the_whole_conversation(self):
        await self.ensure()
        await tool_turn(self.codex)
        for _ in range(2):
            snapshot = await self.ensure()
            self.assertEqual([row["id"] for row in snapshot["rows"]], ["u0", "a0", "u1", "a1", "c1"])
        ws = await self.client.ws_connect(self.url + "/v1/agent/default/chat/events", headers=self.headers)
        first = await ws.receive_json()
        self.assertEqual([row["id"] for row in first["snapshot"]["rows"]], ["u0", "a0", "u1", "a1", "c1"])
        self.codex.notify("item/commandExecution/outputDelta", turnId="turn-1", itemId="c1", delta="compiling\n")
        frame = await asyncio.wait_for(ws.receive_json(), 2)
        self.assertEqual(frame["event"], {"type": "tool.output",
                                          "value": {"kind": "tool", "id": "c1", "turnID": "turn-1",
                                                    "delta": "compiling\n"}})
        await ws.close()

    async def test_a_lost_provider_connection_keeps_the_running_row_on_reattach(self):
        await self.ensure()
        await tool_turn(self.codex)
        self.chat.chat._disconnected()  # the app-server connection dropped
        snapshot = await self.ensure()
        self.assertEqual([(row["id"], row.get("status")) for row in snapshot["rows"]],
                         [("u0", None), ("a0", None), ("u1", None), ("a1", None), ("c1", "running")])


if __name__ == "__main__":
    unittest.main()
