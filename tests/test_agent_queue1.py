"""AGENT-QUEUE-1: queue a follow-up while codex works, follow its plan, say
why not, open huge threads fast - on real codex 0.160 frames.

`tests/fixtures/codex-0160-agent-queue1.json` is what a real `codex
app-server` 0.160.0 sent to a private CODEX_HOME with a scripted fake model on
the Mac (paths, model name, user agent redacted; see its `_about`):

- `queue` / `queue_calls`: codex's own durable queue (`thread/queue/*`,
  experimental): add, list, update, delete, "Send now" as delete + steer, the
  drain after a completed turn, the queue kept after an interrupt, queue/start;
- `decline` / `decline_model`: a reason steered before and after a decline,
  and what the model was sent next;
- `checklist`: `turn/plan/updated` (the `update_plan` tool, off by default in
  0.160 - the capture's CODEX_HOME turned it on);
- `paging_calls`: `thread/turns/list` pages, `thread/resume` with
  `excludeTurns` + `initialTurnsPage`, a fresh thread's refusal;
- `gate`: the queue on a connection without the experimental API.
"""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import aiohttp

from omodachi_core.agent import ProbeStatus
from omodachi_core.agent_chat_provider import (AgentChatError, CodexDefaultAgentChat, DefaultAgentBinding,
                                               HISTORY_PAGE_TURNS, MESSAGE_TEXT_LIMIT, SNAPSHOT_ROWS,
                                               project_checklist, project_queued)
from omodachi_core.agent_chat_service import DefaultAgentChatService
from omodachi_core.agent_lifecycle import DefaultAgentManager
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
from omodachi_core.service import ServiceError

CAPTURE = json.loads((Path(__file__).parent / "fixtures" / "codex-0160-agent-queue1.json").read_text())
# What Leo keeps in ~/.codex and core must never send (AGENT-NEW-1 rule).
FORBIDDEN = {"approvalPolicy", "sandbox", "sandboxPolicy", "approvalsReviewer", "permissions"}


def thread_of(frames):
    return next(frame["params"]["threadId"] for frame in frames if "threadId" in frame.get("params", {}))


QUEUE_THREAD = thread_of(CAPTURE["queue"])
DECLINE_THREAD = thread_of(CAPTURE["decline"])
CHECKLIST_THREAD = thread_of(CAPTURE["checklist"])
PAGING = [call for call in CAPTURE["paging_calls"] if "result" in call]
PAGED_RESUME = next(call for call in PAGING if call["method"] == "thread/resume" and "initialTurnsPage" in call["params"])
PAGING_THREAD = PAGED_RESUME["params"]["threadId"]


class Codex:
    """codex as core's adapter sees it, with codex 0.160's queue as the
    capture shows it behaving (ids, shapes, refusals), every request kept."""
    def __init__(self, thread, *, queue=True, resume=None, pages=None):
        self.thread = thread
        self.ordinal = 0
        self.requests = []
        self.answers = []
        self.queue = [] if queue else None
        self.serial = 0
        self.resume = resume
        self.pages = pages or {}
        self.refuse = set()
        self.on_notification = lambda value, ordinal: None
        self.on_server_request = lambda value: None
        self.on_disconnect = lambda: None

    async def request(self, method, params):
        result, _ = await self.request_fenced(method, params)
        return result

    async def request_fenced(self, method, params):
        self.ordinal += 1
        self.requests.append((method, json.loads(json.dumps(params))))
        if method in self.refuse:
            raise AgentChatError("provider_request_rejected", rpc_code=-32600)
        if method.startswith("thread/queue/") and self.queue is None:
            # An older codex, or a connection without the experimental API.
            raise AgentChatError("provider_request_rejected", rpc_code=-32600)
        if method == "thread/loaded/list":
            return {"data": [self.thread]}, self.ordinal
        if method == "thread/resume":
            return copy.deepcopy(self.resume or {"thread": {"id": self.thread, "turns": []}, "model": "gpt-fixture",
                                                 "reasoningEffort": "high",
                                                 "initialTurnsPage": {"data": [], "nextCursor": None}}), self.ordinal
        if method == "thread/queue/list":
            return {"data": copy.deepcopy(self.queue), "nextCursor": None}, self.ordinal
        if method == "thread/queue/add":
            self.serial += 1
            item = {"id": "q-%02d" % self.serial, "clientUserMessageId": params["clientUserMessageId"],
                    "input": [dict(part, text_elements=[]) for part in params["input"]]}
            self.queue.append(item)
            return {"queuedSubmission": copy.deepcopy(item)}, self.ordinal
        if method == "thread/queue/update":
            for item in self.queue:
                if item["id"] == params["queuedSubmissionId"]:
                    item["input"] = [dict(part, text_elements=[]) for part in params["input"]]
                    return {"queuedSubmission": copy.deepcopy(item)}, self.ordinal
            raise AgentChatError("provider_request_rejected", rpc_code=-32600)  # "queued submission not found"
        if method == "thread/queue/delete":
            before = len(self.queue)
            self.queue = [item for item in self.queue if item["id"] != params["queuedSubmissionId"]]
            return {"deleted": len(self.queue) != before}, self.ordinal
        if method == "thread/queue/reorder":
            order = {ident: index for index, ident in enumerate(params["queuedSubmissionIds"])}
            self.queue.sort(key=lambda item: order[item["id"]])
            return {}, self.ordinal
        if method == "thread/queue/start":
            self.queue = [item for item in self.queue if item["id"] != params["queuedSubmissionId"]]
            return {"turn": {"id": "turn-from-queue", "items": [], "itemsView": "notLoaded",
                             "status": "inProgress"}}, self.ordinal
        if method == "turn/steer":
            return {"turnId": params["expectedTurnId"]}, self.ordinal
        if method == "turn/start":
            return {"turn": {"id": "turn-new", "status": "inProgress", "items": []}}, self.ordinal
        if method == "thread/turns/list":
            return copy.deepcopy(self.pages[params.get("cursor")]), self.ordinal
        return {}, self.ordinal

    async def respond(self, request_id, result):
        self.answers.append((request_id, result))
        self.requests.append(("<answer>", {"id": request_id, **result}))

    async def respond_error(self, request_id, code, message):
        self.answers.append((request_id, code))

    async def close(self):
        pass

    def feed(self, frame):
        if "id" in frame:
            self.on_server_request(frame)
        else:
            self.ordinal += 1
            self.on_notification(frame, self.ordinal)

    def sent(self, method):
        return [params for name, params in self.requests if name == method]


def binding(thread):
    return DefaultAgentBinding("omarchy-fixture", thread, "ws://127.0.0.1:47311", "f" * 64)


async def attached(peer, **options):
    chat = CodexDefaultAgentChat(binding(peer.thread), peer, **options)
    await chat.attach()
    return chat


def drain(chat):
    events = []
    while not chat.events.empty():
        events.append(chat.events.get_nowait()["event"])
    return events


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


class WhatCodexSaid(unittest.TestCase):
    def test_the_queue_is_codexs_own_and_needs_the_experimental_api(self):
        gate = {call["method"]: call for call in CAPTURE["gate"]}
        for method in ("thread/queue/list", "thread/queue/add"):
            self.assertIn("requires experimentalApi capability", gate[method]["error"]["message"])
        # Paging is stable; only the resume's first page needs the opt-in.
        self.assertIn("result", gate["thread/turns/list"])
        refused = [call for call in PAGING if call["conn"] == "rp-stable"] or \
            [call for call in CAPTURE["paging_calls"] if call["conn"] == "rp-stable"]
        self.assertIn("initialTurnsPage requires experimentalApi", refused[0]["error"]["message"])

    def test_the_queue_drains_after_a_finished_turn_and_waits_after_a_stop(self):
        frames = CAPTURE["queue"]
        started = [frame["params"]["item"]["clientId"] for frame in frames
                   if frame["method"] == "item/started" and frame["params"]["item"]["type"] == "userMessage"]
        # q2 was sent now (steered into the running turn); q1 (edited) and q3
        # (deleted) - only q1 ran, as its own turn after the first one ended.
        self.assertEqual(started, [None, "u-block-1", "q2", "q1", "u-block-2", "q4", "q5", "q6"])
        statuses = [frame["params"]["turn"]["status"] for frame in frames if frame["method"] == "turn/completed"]
        self.assertIn("interrupted", statuses)
        lists = [call["result"]["data"] for call in CAPTURE["queue_calls"] if call["method"] == "thread/queue/list"]
        # After the interrupt the queue was still there, and a message added
        # then waited too, until queue/start.
        self.assertTrue(any([item["clientUserMessageId"] for item in data] == ["q4", "q5"] for data in lists))

    def test_a_reason_reaches_the_model_beside_the_refusal(self):
        for seen in CAPTURE["decline_model"]:
            if seen["last"] in ("Not there - use a temp dir instead.", "Please keep the workspace read-only."):
                kinds = [entry["type"] for entry in seen["tail"][-3:]]
                self.assertEqual(kinds, ["function_call", "function_call_output", "message"])
                self.assertIn("rejected by user", seen["tail"][-2]["output"])
                self.assertEqual(seen["tail"][-1]["text"], seen["last"])

    def test_the_checklist_reaches_every_connection(self):
        updates = [frame for frame in CAPTURE["checklist"] if frame["method"] == "turn/plan/updated"]
        self.assertEqual(len(updates), 3)
        self.assertEqual(len(CAPTURE["checklist_stable"]), 3)
        self.assertEqual([step["status"] for step in updates[-1]["params"]["plan"]], ["completed"] * 3)


class Queue(unittest.IsolatedAsyncioTestCase):
    async def test_a_message_while_a_turn_runs_waits_in_codexs_queue(self):
        peer = Codex(QUEUE_THREAD)
        chat = await attached(peer)
        self.assertEqual(chat.snapshot_value()["queue"], [])
        chat.active_turn = "turn-running"
        reply = await chat.enqueue("first queued", "q1")
        captured = next(call for call in CAPTURE["queue_calls"] if call["method"] == "thread/queue/add")
        sent = peer.sent("thread/queue/add")[-1]
        self.assertEqual(set(sent), set(captured["params"]))
        self.assertEqual(sent["input"], [{"type": "text", "text": "first queued"}])
        self.assertEqual(reply["delivery"], "queued")
        self.assertEqual(reply["queue"], [{"id": "q-01", "request_id": "q1", "text": "first queued"}])
        self.assertEqual(chat.snapshot_value()["queue"], reply["queue"])
        # The same request again asks once; other words under it are refused.
        await chat.enqueue("first queued", "q1")
        self.assertEqual(len(peer.sent("thread/queue/add")), 1)
        with self.assertRaisesRegex(AgentChatError, "request_conflict"):
            await chat.enqueue("other words", "q1")
        self.assertFalse(any(FORBIDDEN & set(params) for _, params in peer.requests))

    async def test_codex_says_the_queue_changed_and_phones_hear_what_it_holds(self):
        peer = Codex(QUEUE_THREAD)
        chat = await attached(peer)
        drain(chat)
        # Another client (codex's TUI, another phone) queued two messages.
        peer.queue = [captured["result"]["queuedSubmission"] for captured in CAPTURE["queue_calls"]
                      if captured["method"] == "thread/queue/add"][:2]
        changed = [frame for frame in CAPTURE["queue"] if frame["method"] == "thread/queue/changed"]
        for frame in changed[:3]:  # a burst reads the queue once or twice, never per frame
            peer.feed(frame)
        await settle()
        events = [event for event in drain(chat) if event["type"] == "agent.queue.changed"]
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual([item["text"] for item in events[-1]["queue"]], ["first queued", "second queued"])
        self.assertEqual([item["request_id"] for item in events[-1]["queue"]], ["q1", "q2"])
        self.assertLessEqual(len(peer.sent("thread/queue/list")), 3)

    async def test_send_now_steers_into_the_running_turn(self):
        peer = Codex(QUEUE_THREAD)
        chat = await attached(peer)
        chat.active_turn = "turn-running"
        await chat.enqueue("first queued", "q1")
        await chat.enqueue("second queued", "q2")
        reply = await chat.send_queued_now("q-02")
        self.assertEqual(reply["delivery"], "steered")
        # As the capture did it: off the queue first, then into the turn.
        order = [method for method, _ in peer.requests if method in ("thread/queue/delete", "turn/steer")]
        self.assertEqual(order, ["thread/queue/delete", "turn/steer"])
        steer = peer.sent("turn/steer")[-1]
        captured = next(call["params"] for call in CAPTURE["queue_calls"] if call["method"] == "turn/steer")
        self.assertEqual(set(steer), set(captured))
        self.assertEqual((steer["expectedTurnId"], steer["clientUserMessageId"]), ("turn-running", "q2"))
        self.assertEqual(steer["input"][0]["text"], "second queued")
        self.assertEqual([item["id"] for item in reply["queue"]], ["q-01"])

    async def test_steered_words_a_stopped_turn_never_took_go_back_on_the_queue(self):
        """Real 0.160 (`steer_stop`): words steered into a turn that waits on an
        approval, then a Stop - codex never makes them a user item. Core puts
        them back on codex's queue, first in line, so they are not lost."""
        frames = CAPTURE["steer_stop"]
        self.assertFalse(any(frame["method"] == "item/started" and frame["params"]["item"].get("clientId") == "steer-1"
                             for frame in frames))
        thread = thread_of(frames)
        peer = Codex(thread)
        chat = await attached(peer)
        drain(chat)
        start = [index for index, frame in enumerate(frames) if frame["method"] == "turn/started"][1]
        turn = frames[start]["params"]["turn"]["id"]
        for frame in frames[start:]:
            peer.feed(frame)
            if frame["method"] == "item/started" and frame["params"]["item"]["type"] == "commandExecution":
                break
        await chat.enqueue("first waiting", "q0")
        await chat.enqueue("steered then stopped", "steer-1")
        self.assertEqual((await chat.send_queued_now("q-02"))["delivery"], "steered")
        for frame in frames[start:]:
            if frame["method"] == "turn/completed":
                peer.feed(frame)
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertIsNone(chat.active_turn)
        readded = peer.sent("thread/queue/add")[-1]
        self.assertEqual((readded["clientUserMessageId"], readded["input"][0]["text"]), ("steer-1", "steered then stopped"))
        self.assertEqual([item["text"] for item in chat.queue], ["steered then stopped", "first waiting"])
        # A steer codex did take is not put back.
        await chat.enqueue("taken", "q9")
        chat.active_turn = turn
        await chat.send_queued_now(chat.queue[-1]["id"])
        peer.feed({"method": "item/started", "params": {"threadId": thread, "turnId": turn, "item": {
            "type": "userMessage", "id": "u-taken", "clientId": "q9", "content": [{"type": "text", "text": "taken"}]}}})
        adds = len(peer.sent("thread/queue/add"))
        peer.feed({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn, "status": "interrupted",
                                                                                         "items": []}}})
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(len(peer.sent("thread/queue/add")), adds)

    async def test_send_now_between_turns_starts_it(self):
        peer = Codex(QUEUE_THREAD)
        chat = await attached(peer)
        chat.active_turn = "turn-running"
        await chat.enqueue("after the stop", "q4")
        chat.active_turn = None  # stopped: codex keeps the queue (captured)
        reply = await chat.send_queued_now("q-01")
        self.assertEqual((reply["delivery"], reply["turnID"]), ("started", "turn-from-queue"))
        self.assertEqual(peer.sent("thread/queue/start")[-1], {"threadId": QUEUE_THREAD, "queuedSubmissionId": "q-01"})
        self.assertEqual(chat.active_turn, "turn-from-queue")
        self.assertEqual(reply["queue"], [])

    async def test_send_now_when_the_turn_just_ended_goes_back_first_in_line(self):
        peer = Codex(QUEUE_THREAD)
        chat = await attached(peer)
        chat.active_turn = "turn-running"
        await chat.enqueue("one", "q1")
        await chat.enqueue("two", "q2")
        peer.refuse.add("turn/steer")  # "expected turn ... does not match"
        reply = await chat.send_queued_now("q-02")
        self.assertEqual(reply["delivery"], "queued")
        self.assertEqual([item["text"] for item in reply["queue"]], ["two", "one"])
        readded = peer.sent("thread/queue/add")[-1]
        self.assertEqual(readded["clientUserMessageId"], "q2")

    async def test_edit_and_delete_in_place(self):
        peer = Codex(QUEUE_THREAD)
        chat = await attached(peer)
        chat.active_turn = "turn-running"
        await chat.enqueue("first queued", "q1")
        await chat.enqueue("third queued", "q3")
        reply = await chat.edit_queued("q-01", "first queued, edited")
        self.assertEqual(peer.sent("thread/queue/update")[-1]["input"], [{"type": "text", "text": "first queued, edited"}])
        self.assertEqual([item["text"] for item in reply["queue"]], ["first queued, edited", "third queued"])
        reply = await chat.delete_queued("q-02")
        self.assertTrue(reply["deleted"])
        self.assertEqual([item["id"] for item in reply["queue"]], ["q-01"])
        # It started meanwhile: the edit has nowhere to go, and says so.
        peer.queue = []
        await chat.refresh_queue()
        with self.assertRaisesRegex(AgentChatError, "agent_queue_item_gone"):
            await chat.edit_queued("q-01", "too late")
        self.assertFalse((await chat.delete_queued("q-01"))["deleted"])

    async def test_a_codex_without_the_queue_has_none_and_steering_stays(self):
        peer = Codex(QUEUE_THREAD, queue=False)
        chat = await attached(peer)
        self.assertIsNone(chat.snapshot_value()["queue"])
        chat.active_turn = "turn-running"
        with self.assertRaisesRegex(AgentChatError, "agent_queue_unavailable"):
            await chat.enqueue("words", "r1")
        self.assertEqual((await chat.steer("words", "r2"))["delivery"], "steered")

    async def test_the_drained_message_is_a_turn_of_its_own_on_the_phone(self):
        peer = Codex(QUEUE_THREAD)
        chat = await attached(peer)
        drain(chat)
        frames = CAPTURE["queue"]
        start = next(index for index, frame in enumerate(frames) if frame["method"] == "turn/started"
                     and any(f["method"] == "item/started" and f["params"]["item"].get("clientId") == "q1"
                             and f["params"]["turnId"] == frame["params"]["turn"]["id"] for f in frames))
        turn = frames[start]["params"]["turn"]["id"]
        for frame in frames[start:]:
            if frame.get("params", {}).get("turnId", turn) == turn or frame["params"].get("turn", {}).get("id") == turn:
                peer.feed(frame)
            if frame["method"] == "turn/completed" and frame["params"]["turn"]["id"] == turn:
                break
        events = drain(chat)
        self.assertEqual(events[0], {"type": "turnStarted", "turnID": turn})
        users = {event["value"]["id"]: event["value"] for event in events
                 if event["type"] == "message" and event["value"]["role"] == "user"}
        self.assertEqual([row["text"] for row in users.values()], ["first queued, edited"])
        self.assertEqual(events[-1]["type"], "turnFinished")


class DeclineWithAReason(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = Codex(DECLINE_THREAD)
        self.chat = await attached(self.peer)
        frames = CAPTURE["decline"]
        start = next(index for index, frame in enumerate(frames) if frame["method"] == "item/started"
                     and frame["params"]["item"].get("content", [{}])[0].get("text") == "block three")
        for frame in frames[start - 1:]:
            self.peer.feed(frame)
            if frame["method"] == "item/commandExecution/requestApproval":
                break
        self.approval = next(iter(self.chat.pending_approvals))
        self.turn = self.chat.active_turn
        drain(self.chat)

    async def test_the_reason_is_steered_in_before_the_decline(self):
        result = await self.chat.resolve_approval(self.approval, decision="decline",
                                                  reason="Not there - use a temp dir instead.")
        self.assertEqual((result["decision"], result["delivery"]), ("decline", "steered"))
        order = [method for method, _ in self.peer.requests if method in ("turn/steer", "<answer>")]
        self.assertEqual(order, ["turn/steer", "<answer>"])
        steer = self.peer.sent("turn/steer")[-1]
        self.assertEqual(steer["expectedTurnId"], self.turn)
        self.assertEqual(steer["input"], [{"type": "text", "text": "Not there - use a temp dir instead."}])
        self.assertTrue(steer["clientUserMessageId"].startswith("reason-"))
        self.assertEqual(self.peer.answers[-1][1], {"decision": "decline"})
        # codex then says the steered words as the person's message, in the turn.
        frames = CAPTURE["decline"]
        after = next(index for index, frame in enumerate(frames) if frame["method"] == "serverRequest/resolved")
        for frame in frames[after:]:
            self.peer.feed(frame)
            if frame["method"] == "turn/completed":
                break
        users = {event["value"]["id"]: event["value"] for event in drain(self.chat) if event["type"] == "message"
                 and event["value"]["role"] == "user"}
        self.assertEqual([row["text"] for row in users.values()], ["Not there - use a temp dir instead."])
        self.assertEqual({row["turnID"] for row in users.values()}, {self.turn})

    async def test_no_reason_is_todays_decline(self):
        result = await self.chat.resolve_approval(self.approval, decision="decline", reason="   ")
        self.assertNotIn("delivery", result)
        self.assertEqual(self.peer.sent("turn/steer"), [])
        self.assertEqual(self.peer.answers[-1][1], {"decision": "decline"})

    async def test_a_turn_that_will_not_take_it_gets_it_as_the_next_message(self):
        self.peer.refuse.add("turn/steer")
        result = await self.chat.resolve_approval(self.approval, decision="decline", reason="Use /tmp.")
        self.assertEqual(result["delivery"], "queued")
        self.assertEqual(self.peer.answers[-1][1], {"decision": "decline"})
        self.assertEqual(self.peer.sent("thread/queue/add")[-1]["input"], [{"type": "text", "text": "Use /tmp."}])

    async def test_with_nowhere_to_go_the_phone_keeps_the_words(self):
        self.peer.refuse.add("turn/steer")
        self.chat.queue = None
        result = await self.chat.resolve_approval(self.approval, decision="decline", reason="Use /tmp.")
        self.assertEqual(result["delivery"], "unsent")
        self.assertEqual(self.peer.answers[-1][1], {"decision": "decline"})

    async def test_only_a_plain_decline_takes_words(self):
        for decision in ("accept", "acceptForSession", "cancel"):
            with self.assertRaisesRegex(AgentChatError, "agent_approval_decision_invalid"):
                await self.chat.resolve_approval(self.approval, decision=decision, reason="why")
        with self.assertRaisesRegex(AgentChatError, "agent_approval_decision_invalid"):
            await self.chat.resolve_approval(self.approval, decision="decline", reason="x" * 2001)
        self.assertEqual(self.peer.answers, [])


class Checklist(unittest.IsolatedAsyncioTestCase):
    async def test_codexs_plan_updates_tick_one_checklist(self):
        peer = Codex(CHECKLIST_THREAD)
        chat = await attached(peer)
        self.assertIsNone(chat.snapshot_value()["checklist"])
        drain(chat)
        for frame in CAPTURE["checklist"]:
            peer.feed(frame)
        events = [event for event in drain(chat) if event["type"] == "agent.checklist.updated"]
        self.assertEqual(len(events), 3)
        first = events[0]["checklist"]
        self.assertEqual(first["explanation"], "Three small steps.")
        self.assertEqual([step["status"] for step in first["steps"]], ["inProgress", "pending", "pending"])
        self.assertEqual([step["step"] for step in first["steps"]],
                         ["Read the config", "Change the port", "Run the tests"])
        final = chat.snapshot_value()["checklist"]
        self.assertEqual([step["status"] for step in final["steps"]], ["completed"] * 3)
        self.assertEqual(final["turnID"], next(frame["params"]["turnId"] for frame in CAPTURE["checklist"]
                                               if frame["method"] == "turn/plan/updated"))

    def test_bounded_and_lenient(self):
        value = project_checklist({"turnId": "t", "plan": [{"step": "a", "status": "weird"}, "x", {"status": "pending"}]
                                   + [{"step": "s%d" % n, "status": "completed"} for n in range(40)]})
        self.assertEqual(value["steps"][0], {"step": "a", "status": "pending"})
        self.assertEqual(len(value["steps"]), 32)
        self.assertIsNone(project_checklist({"turnId": "t", "plan": []}))
        self.assertIsNone(project_checklist({"plan": "nope"}))

    async def test_core_keeps_it_across_its_own_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = SimpleNamespace(root=root, cwd=root / "agent-workspace", probe=None)

            class Owner:
                def owned_metadata(self): return None
                async def ensure_new_or_owned(self, existing_agent=False): return binding(CHECKLIST_THREAD)
                def tui_argv(self, b): return ["codex"]
                async def close_proxy(self): pass

            peers = []

            async def connect(b, **options):
                peers.append(Codex(CHECKLIST_THREAD))
                chat = CodexDefaultAgentChat(b, peers[-1], **options)
                await chat.attach()
                return chat
            service = DefaultAgentChatService(manager, "host", owner=Owner(), connector=connect)
            await service._attach(binding(CHECKLIST_THREAD))
            for frame in CAPTURE["checklist"]:
                peers[-1].feed(frame)
            for _ in range(50):
                await asyncio.sleep(0.01)
                if (root / "structured-default" / "checklist.json").exists():
                    break
            await service.close()
            again = DefaultAgentChatService(manager, "host", owner=Owner(), connector=connect)
            await again._attach(binding(CHECKLIST_THREAD))
            kept = again.chat.snapshot_value()["checklist"]
            self.assertEqual([step["status"] for step in kept["steps"]], ["completed"] * 3)
            await again.close()


class History(unittest.IsolatedAsyncioTestCase):
    def pages(self):
        """codex's answers to `thread/turns/list`, by cursor, from the capture."""
        lists = [call for call in PAGING if call["method"] == "thread/turns/list" and call["conn"] == "cold"
                 and call["params"].get("limit") == 5]
        last = dict(lists[1]["result"], nextCursor=None)  # the capture's thread, as if it began there
        return {None: lists[0]["result"], lists[0]["result"]["nextCursor"]: last}

    async def test_the_thread_opens_on_its_newest_page(self):
        peer = Codex(PAGING_THREAD, resume=PAGED_RESUME["result"], pages=self.pages())
        chat = await attached(peer)
        resume = peer.sent("thread/resume")[0]
        self.assertTrue(resume["excludeTurns"])
        self.assertEqual(resume["initialTurnsPage"], {"limit": HISTORY_PAGE_TURNS, "sortDirection": "desc",
                                                      "itemsView": "full"})
        self.assertFalse(FORBIDDEN & set(resume))
        snapshot = chat.snapshot_value()
        users = [row["text"] for row in snapshot["rows"] if row["kind"] == "message" and row["role"] == "user"]
        page = PAGED_RESUME["result"]["initialTurnsPage"]["data"]
        # codex answers newest first; the phone reads oldest first.
        self.assertEqual(users, [turn["items"][0]["content"][0]["text"] for turn in reversed(page)])
        self.assertTrue(snapshot["earlier"])
        self.assertEqual(peer.sent("thread/turns/list"), [])  # one request, not two

    async def test_earlier_messages_come_a_page_at_a_time(self):
        pages = self.pages()
        resume = copy.deepcopy(PAGED_RESUME["result"])
        first = pages[None]
        resume["initialTurnsPage"] = first  # hello 11 back to hello 7
        peer = Codex(PAGING_THREAD, resume=resume, pages=pages)
        chat = await attached(peer)
        oldest = chat.snapshot_value()["rows"][0]["id"]
        page = await chat.history(oldest, 4)
        self.assertEqual(len(page["rows"]), 4)
        texts = [row["text"] for row in page["rows"] if row["kind"] == "message" and row["role"] == "user"]
        self.assertEqual(texts, ["hello 5", "hello 6"])  # oldest first, just before hello 7
        listed = peer.sent("thread/turns/list")[-1]
        self.assertEqual(listed["cursor"], first["nextCursor"])
        self.assertTrue(page["earlier"])
        # Then all the rest, until codex has no more.
        everything = await chat.history(page["rows"][0]["id"], 200)
        self.assertFalse(everything["earlier"])
        users = [row["text"] for row in everything["rows"] if row["kind"] == "message" and row["role"] == "user"]
        self.assertEqual(users[0], "hello 2")
        with self.assertRaisesRegex(AgentChatError, "agent_history_row_unknown"):
            await chat.history("no-such-row", 10)

    async def test_a_codex_that_will_not_page_is_read_whole_and_cut_by_core(self):
        """Degrade: no paging - core holds the whole thread and still sends
        the newest rows first, the rest from its own copy on request."""
        turns = [{"id": "turn-%03d" % n, "status": "completed", "items": [
            {"type": "userMessage", "id": "u-%03d" % n, "content": [{"type": "text", "text": "hello %d" % n}]},
            {"type": "agentMessage", "id": "a-%03d" % n, "text": "Got it: hello %d" % n}]} for n in range(120)]
        peer = Codex(PAGING_THREAD, resume={"thread": {"id": PAGING_THREAD, "turns": turns}})
        peer.refuse.add("thread/turns/list")
        original = peer.request_fenced

        async def older_codex(method, params):
            if method == "thread/resume" and "initialTurnsPage" in params:
                raise AgentChatError("provider_request_rejected", rpc_code=-32600)
            return await original(method, params)
        peer.request_fenced = older_codex
        chat = await attached(peer)
        snapshot = chat.snapshot_value()
        self.assertEqual(len(snapshot["rows"]), SNAPSHOT_ROWS)
        self.assertEqual(snapshot["rows"][-1]["id"], "a-119")
        self.assertTrue(snapshot["earlier"])
        page = await chat.history(snapshot["rows"][0]["id"], 100)
        self.assertEqual(len(page["rows"]), 90)
        self.assertEqual(page["rows"][0]["id"], "u-000")
        self.assertFalse(page["earlier"])
        self.assertEqual(peer.sent("thread/turns/list"), [])

    async def test_a_giant_message_travels_cut_and_says_so(self):
        text = "x" * (MESSAGE_TEXT_LIMIT + 10)
        turns = [{"id": "turn-1", "status": "completed", "items": [
            {"type": "agentMessage", "id": "a-1", "text": text}]}]
        peer = Codex(PAGING_THREAD, resume={"thread": {"id": PAGING_THREAD, "turns": turns}})
        chat = await attached(peer)
        row = chat.snapshot_value()["rows"][0]
        self.assertEqual((len(row["text"]), row["text_truncated"]), (MESSAGE_TEXT_LIMIT, 10))
        self.assertEqual(len(chat.rows["a-1"]["text"]), MESSAGE_TEXT_LIMIT + 10)  # core keeps it whole


class OverHTTP(unittest.IsolatedAsyncioTestCase):
    """The new routes, the real service and adapter."""
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        cap = SimpleNamespace(configured=True, configured_kind="codex", default_agent_exists=False,
                              default_agent_probe=ProbeStatus.MISSING)
        manager = DefaultAgentManager(root=root, probe=SimpleNamespace(inspect=lambda: (cap, None)),
                                      runner=lambda _: None)
        self.peer = Codex(QUEUE_THREAD)

        class Owner:
            def owned_metadata(self): return None
            async def ensure_new_or_owned(self, existing_agent=False): return binding(QUEUE_THREAD)
            def tui_argv(self, b): return ["codex"]
            async def close_proxy(self): pass

        async def connect(b, **options):
            chat = CodexDefaultAgentChat(b, self.peer, **options)
            await chat.attach()
            return chat
        self.hub = Hub()
        token = self.hub.register_device("phone")
        self.service = DefaultAgentChatService(manager, "host", owner=Owner(), connector=connect, hub=self.hub)
        app_service = create_service(self.hub, demo=True)
        app_service.agent_manager = manager
        app_service.agent_chat = self.service
        self.server = NetworkServer(app_service, allow_loopback_http=True)
        await self.server.start()
        self.client = aiohttp.ClientSession()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.headers = {"Authorization": "Bearer " + token}

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.close()
        await self.server.close()
        self.tmp.cleanup()

    async def call(self, method, path, body=None):
        async with self.client.request(method, self.url + path, headers=self.headers, json=body) as response:
            return response.status, await response.json()

    async def test_queue_routes(self):
        code, result = await self.call("POST", "/v1/agent/default:ensure", {"surface": "chat"})
        self.assertEqual((code, result["snapshot"]["queue"]), (200, []))
        self.service.chat.active_turn = "turn-running"
        code, result = await self.call("POST", "/v1/agent/default/chat/queue", {"request_id": "q1", "text": "later"})
        self.assertEqual(code, 200, result)
        self.assertEqual(result["queue"], [{"id": "q-01", "request_id": "q1", "text": "later"}])
        code, result = await self.call("POST", "/v1/agent/default/chat/queue/q-01:edit", {"text": "later, edited"})
        self.assertEqual((code, result["queue"][0]["text"]), (200, "later, edited"))
        code, result = await self.call("POST", "/v1/agent/default/chat/queue/q-01:send", {})
        self.assertEqual((code, result["delivery"]), (200, "steered"))
        code, result = await self.call("POST", "/v1/agent/default/chat/queue", {"request_id": "q2", "text": "x"})
        code, result = await self.call("POST", "/v1/agent/default/chat/queue/q-02:delete", {})
        self.assertEqual((code, result["deleted"], result["queue"]), (200, True, []))
        # The quiet commands wait for the turn to end, as on the steer route.
        code, result = await self.call("POST", "/v1/agent/default/chat/queue", {"request_id": "q3", "text": "/plan"})
        self.assertEqual((code, result["error"]["code"]), (409, "agent_command_while_working"))
        code, result = await self.call("POST", "/v1/agent/default/chat/queue/q-09:send", {})
        self.assertEqual((code, result["error"]["code"]), (409, "agent_queue_item_gone"))
        code, result = await self.call("POST", "/v1/agent/default/chat/queue", {"request_id": "q4", "text": "x",
                                                                                 "sandbox": "danger"})
        self.assertEqual(code, 400)
        self.assertFalse(any(FORBIDDEN & set(params) for _, params in self.peer.requests))

    async def test_history_route_and_decline_reason(self):
        code, result = await self.call("POST", "/v1/agent/default:ensure", {"surface": "chat"})
        self.assertEqual(code, 200, result)
        code, result = await self.call("GET", "/v1/agent/default/chat/history?limit=5")
        self.assertEqual((code, result["rows"], result["earlier"]), (200, [], False))
        code, result = await self.call("GET", "/v1/agent/default/chat/history?limit=500")
        self.assertEqual(code, 400)
        code, result = await self.call("GET", "/v1/agent/default/chat/history?before=nope&limit=5")
        self.assertEqual((code, result["error"]["code"]), (409, "agent_history_row_unknown"))
        # A pending command approval, then Decline with a reason.
        frames = CAPTURE["queue"]
        start = next(index for index, frame in enumerate(frames) if frame["method"] == "item/started"
                     and frame["params"]["item"].get("clientId") == "u-block-1")
        for frame in frames[start - 1:]:
            self.peer.feed(frame)
            if frame["method"] == "item/commandExecution/requestApproval":
                break
        request = next(iter(self.service.chat.pending_approvals))
        code, result = await self.call("POST", "/v1/agent/default/chat/approvals/" + request,
                                       {"decision": "decline", "reason": "Not there."})
        self.assertEqual((code, result["delivery"]), (200, "steered"), result)
        self.assertEqual(self.peer.sent("turn/steer")[-1]["input"], [{"type": "text", "text": "Not there."}])


class Projection(unittest.TestCase):
    def test_a_queued_message_as_a_phone_reads_it(self):
        item = next(call["result"]["queuedSubmission"] for call in CAPTURE["queue_calls"]
                    if call["method"] == "thread/queue/add")
        self.assertEqual(project_queued(item), {"id": item["id"], "request_id": "q1", "text": "first queued"})
        long = dict(item, input=[{"type": "text", "text": "y" * 20000}, {"type": "localImage", "path": "/p.png"}])
        row = project_queued(long)
        self.assertEqual((len(row["text"]), row["text_clipped"], row["images"]), (16000, True, 1))
        self.assertIsNone(project_queued({"input": []}))


if __name__ == "__main__":
    unittest.main()


class TheQuestionCardLoop(unittest.IsolatedAsyncioTestCase):
    """Leo (2026-10-07): "askuserquestion 的卡片 会循环 我回答了好多圈然后能同步" - a
    three-question card came back at question 1 after he answered, several
    times. Real 0.160 (`ask_replay`): codex sends a request it is still
    waiting on again, identical, whenever the thread is resumed. If core's
    answer did not reach codex (a connection that went down with it), every
    re-attach put the same questions back on the phone."""
    def frames(self):
        frames = CAPTURE["ask_replay"]
        asks = [frame for frame in frames if frame["method"] == "item/tool/requestUserInput"]
        return frames, asks

    def test_codex_re_sends_a_waiting_request_identically(self):
        _, asks = self.frames()
        self.assertEqual(len(asks), 2)
        self.assertEqual(asks[0], asks[1])
        self.assertEqual(len(asks[0]["params"]["questions"]), 3)
        other = [frame for frame in CAPTURE["ask_replay_other"] if frame["method"] == "item/tool/requestUserInput"]
        self.assertEqual(other[0]["params"], asks[0]["params"])

    async def test_an_answered_request_sent_again_gets_the_same_answer_not_the_phone(self):
        frames, asks = self.frames()
        thread = thread_of(frames)
        peer = Codex(thread)
        answered = {}
        chat = await attached(peer, answered=answered)
        drain(chat)
        peer.feed(asks[0])
        request = next(iter(chat.pending_approvals))
        answers = {question["id"]: [question["options"][1]["label"]] for question in asks[0]["params"]["questions"]}
        await chat.resolve_approval(request, answers=answers)
        sent = peer.answers[-1]
        drain(chat)
        # The connection went down with that answer; core re-attaches (a new
        # adapter, the service's memory) and codex asks again, identically.
        again = CodexDefaultAgentChat(binding(thread), Codex(thread), answered=answered)
        await again.attach()
        drain(again)
        again.rpc.feed(asks[1])
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(again.pending_approvals, {}, "the phone is not asked again")
        self.assertEqual(again.rpc.answers, [sent], "the same answer goes to codex again")
        events = drain(again)
        self.assertEqual([event["type"] for event in events], ["agent.approval.resolved"])
        self.assertEqual(events[0]["source"], "client")

    async def test_a_different_request_under_the_same_id_is_asked(self):
        frames, asks = self.frames()
        thread = thread_of(frames)
        peer = Codex(thread)
        answered = {}
        chat = await attached(peer, answered=answered)
        peer.feed(asks[0])
        request = next(iter(chat.pending_approvals))
        await chat.resolve_approval(request, answers={"db": ["Postgres"]})
        drain(chat)
        # codex numbers requests per connection: a new question may reuse the id.
        other = copy.deepcopy(asks[0])
        other["params"]["itemId"] = "call-new"
        other["params"]["questions"] = other["params"]["questions"][:1]
        peer.feed(other)
        self.assertIn(str(other["id"]), chat.pending_approvals)
        self.assertEqual([event["type"] for event in drain(chat)], ["agent.approval.requested"])

    async def test_a_request_still_waiting_and_sent_again_is_the_same_card(self):
        frames, asks = self.frames()
        thread = thread_of(frames)
        peer = Codex(thread)
        chat = await attached(peer)
        drain(chat)
        peer.feed(asks[0])
        peer.feed(asks[1])
        self.assertEqual(len(chat.pending_approvals), 1)
        requested = [event for event in drain(chat) if event["type"] == "agent.approval.requested"]
        self.assertEqual({event["request_id"] for event in requested}, {str(asks[0]["id"])})

    async def test_the_service_keeps_the_answers_across_a_re_attach(self):
        frames, asks = self.frames()
        thread = thread_of(frames)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = SimpleNamespace(root=root, cwd=root / "agent-workspace", probe=None)

            class Owner:
                def owned_metadata(self): return None
                async def ensure_new_or_owned(self, existing_agent=False): return binding(thread)
                def tui_argv(self, b): return ["codex"]
                async def close_proxy(self): pass

            peers = []

            async def connect(b, **options):
                peers.append(Codex(thread))
                chat = CodexDefaultAgentChat(b, peers[-1], **options)
                await chat.attach()
                return chat
            service = DefaultAgentChatService(manager, "host", owner=Owner(), connector=connect)
            await service._attach(binding(thread))
            peers[-1].feed(asks[0])
            request = next(iter(service.chat.pending_approvals))
            await service.approve(request, {"input": {"db": ["Postgres"]}}, lambda: None)
            await service._attach(binding(thread))  # the provider connection was replaced
            peers[-1].feed(asks[1])
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(service.chat.pending_approvals, {})
            self.assertEqual(peers[-1].answers[-1][1], {"answers": {"db": {"answers": ["Postgres"]}}})
            await service.close()
