"""AGENT-PLAN-1: `/plan` and the Plan chip, on real codex 0.160 frames.

`tests/fixtures/codex-0160-agent-plan1.json` is what a real `codex app-server`
0.160.0 sent to a private CODEX_HOME with a scripted fake model on the Mac
(paths, the model name and codex's built-in mode prompts redacted):

- `plan`: a Default turn; `thread/settings/update` to Plan; a Plan turn - a
  `request_user_input` question, then a reply whose `<proposed_plan>` block
  codex turned into a `plan` item (`item/plan/delta` while it is written); a
  Plan turn with an effort override (the mode stays);
- `leave`: after an app-server restart, `thread/settings/update` back to
  Default and "Implement the plan.";
- `responses`: the answers to `thread/resume` (which says the mode),
  `collaborationMode/list` and `thread/settings/update`;
- `gate`: the errors the same calls get on a connection that did not opt into
  codex's experimental API - why core now does.
"""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import aiohttp

from omodachi_core.agent import ProbeStatus
from omodachi_core.agent_chat_provider import (AgentChatError, CodexDefaultAgentChat, CodexJSONRPC,
                                               DefaultAgentBinding)
from omodachi_core.agent_chat_service import DefaultAgentChatService
from omodachi_core.agent_commands import AgentCommands
from omodachi_core.agent_lifecycle import DefaultAgentManager
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
from omodachi_core.service import ServiceError

CAPTURE = json.loads((Path(__file__).parent / "fixtures" / "codex-0160-agent-plan1.json").read_text())
THREAD = next(frame["params"]["threadId"] for frame in CAPTURE["plan"] if "threadId" in frame.get("params", {}))
# What Leo keeps in ~/.codex and core must never send (AGENT-NEW-1 rule).
FORBIDDEN = {"approvalPolicy", "sandbox", "sandboxPolicy", "approvalsReviewer", "permissions"}


class Peer:
    """codex as core's adapter sees it: the captured resume answer, every
    request recorded, notifications fed from the capture."""
    def __init__(self, resume=None, reject=None):
        self.ordinal = 0
        self.requests = []
        self.answers = []
        self.resume = resume
        self.reject = reject or set()
        self.reviews = list(CAPTURE["responses"]["review/start"])
        self.before_review = None
        self.on_notification = lambda value, ordinal: None
        self.on_server_request = lambda value: None
        self.on_disconnect = lambda: None

    async def request(self, method, params):
        result, _ = await self.request_fenced(method, params)
        return result

    async def request_fenced(self, method, params):
        self.ordinal += 1
        self.requests.append((method, json.loads(json.dumps(params))))
        if method in self.reject:
            raise AgentChatError("provider_request_rejected", rpc_code=-32600)
        if method == "thread/loaded/list":
            return {"data": [THREAD]}, self.ordinal
        if method == "thread/resume":
            return json.loads(json.dumps(self.resume or {"thread": {"id": THREAD, "turns": []},
                                                         "model": "gpt-fixture", "reasoningEffort": "high"})), \
                self.ordinal
        if method == "thread/settings/update":
            return dict(CAPTURE["responses"]["thread/settings/update"]), self.ordinal
        if method == "turn/start":
            return {"turn": {"id": "turn-new", "status": "inProgress", "items": []}}, self.ordinal
        if method == "review/start":
            if self.before_review is not None:
                self.before_review()
            return json.loads(json.dumps(self.reviews.pop(0))), self.ordinal
        return {}, self.ordinal

    async def respond(self, request_id, result):
        self.answers.append((request_id, result))

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


def rethread(frame):
    """A capture of another thread, as if it were this one's (`review`,
    `compact` were captured on threads of their own)."""
    other = frame.get("params", {}).get("threadId")
    return json.loads(json.dumps(frame).replace(other, THREAD)) if other and other != THREAD else frame


def binding():
    return DefaultAgentBinding("omarchy-fixture", THREAD, "ws://127.0.0.1:47311", "f" * 64)


async def attached(peer=None, **options):
    peer = peer or Peer()
    chat = CodexDefaultAgentChat(binding(), peer, **options)
    await chat.attach()
    return chat, peer


def drain(chat):
    events = []
    while not chat.events.empty():
        events.append(chat.events.get_nowait()["event"])
    return events


async def replay(name, chat, peer, until=None):
    events = []
    for frame in CAPTURE[name]:
        if until is not None and until(frame):
            break
        peer.feed(frame)
        events.extend(drain(chat))
    return events


class WhatCodexSaid(unittest.TestCase):
    def test_every_way_to_choose_a_mode_needs_the_experimental_api(self):
        # The reason core opts in: on a stable connection each call is refused.
        for method in ("collaborationMode/list", "thread/settings/update", "turn/start"):
            self.assertIn("requires experimentalApi capability", CAPTURE["gate"][method]["message"], method)
        self.assertEqual(CAPTURE["gate"]["turn/start"]["message"],
                         "turn/start.collaborationMode requires experimentalApi capability")

    def test_the_presets_and_the_resumed_mode(self):
        presets = CAPTURE["responses"]["collaborationMode/list"]["data"]
        self.assertEqual([(row["name"], row["mode"]) for row in presets], [("Plan", "plan"), ("Default", "default")])
        # Resume names the mode on both kinds of connection.
        self.assertEqual(CAPTURE["responses"]["thread/resume"]["collaborationMode"]["mode"], "plan")
        self.assertEqual(CAPTURE["responses"]["thread/resume (stable connection)"]["collaborationMode"]["mode"], "plan")

    def test_core_opts_into_the_experimental_api(self):
        sent = []

        class Recorder(CodexJSONRPC):
            def __init__(self):
                self.pending, self.next_id, self.ordinal, self.timeout = {}, 0, 0, 1

            async def request(self, method, params):
                sent.append((method, params))
                return {}

            async def _write(self, value):
                sent.append((value["method"], value.get("params")))

        asyncio.run(Recorder().initialize())
        self.assertEqual(sent[0][0], "initialize")
        self.assertIs(sent[0][1]["capabilities"]["experimentalApi"], True)
        self.assertEqual(sent[1][0], "initialized")


class PlanMode(unittest.IsolatedAsyncioTestCase):
    async def test_chip_switch_is_codexs_own_settings_update(self):
        chat, peer = await attached()
        self.assertIsNone(chat.mode)
        result = await chat.set_mode("plan")
        self.assertEqual(result, {"mode": "plan", "changed": True})
        method, params = peer.requests[-1]
        self.assertEqual(method, "thread/settings/update")
        # The conversation's own model and effort; codex's built-in Plan words.
        self.assertEqual(params, {"threadId": THREAD, "collaborationMode": {
            "mode": "plan", "settings": {"model": "gpt-fixture", "reasoning_effort": "high",
                                         "developer_instructions": None}}})
        self.assertFalse(FORBIDDEN & set(params))
        self.assertEqual(chat.usage_value()["mode"], "plan")
        self.assertEqual(drain(chat)[-1]["usage"]["mode"], "plan")
        # The way back is the same call with Default.
        self.assertEqual(await chat.set_mode("default"), {"mode": "default", "changed": True})
        self.assertEqual(peer.requests[-1][1]["collaborationMode"]["mode"], "default")

    async def test_plan_with_words_is_one_turn(self):
        chat, peer = await attached()
        reply = await chat.send("plan the port change", "req-1", mode="plan")
        self.assertTrue(reply["accepted"])
        method, params = peer.requests[-1]
        self.assertEqual(method, "turn/start")
        self.assertEqual(params["input"], [{"type": "text", "text": "plan the port change"}])
        self.assertEqual(params["collaborationMode"]["mode"], "plan")
        self.assertEqual(params["collaborationMode"]["settings"],
                         {"model": "gpt-fixture", "reasoning_effort": "high", "developer_instructions": None})
        self.assertFalse(FORBIDDEN & set(params))
        self.assertEqual(chat.mode, "plan")

    async def test_a_model_chosen_for_the_turn_rides_the_mode_too(self):
        chat, peer = await attached()
        await chat.send("plan it", "req-2", model="gpt-other", effort="low", mode="plan")
        params = peer.requests[-1][1]
        self.assertEqual((params["model"], params["effort"]), ("gpt-other", "low"))
        self.assertEqual(params["collaborationMode"]["settings"]["model"], "gpt-other")
        self.assertEqual(params["collaborationMode"]["settings"]["reasoning_effort"], "low")

    async def test_an_ordinary_message_sends_no_mode(self):
        chat, peer = await attached()
        await chat.send("hello", "req-3")
        self.assertNotIn("collaborationMode", peer.requests[-1][1])

    async def test_not_while_a_turn_runs_and_not_on_a_codex_that_refuses(self):
        chat, peer = await attached()
        chat.active_turn = "turn-1"
        with self.assertRaisesRegex(AgentChatError, "agent_busy"):
            await chat.set_mode("plan")
        chat.active_turn = None
        refusing, _ = await attached(Peer(reject={"thread/settings/update"}))
        with self.assertRaisesRegex(AgentChatError, "agent_mode_unavailable"):
            await refusing.set_mode("plan")
        self.assertIsNone(refusing.mode)
        with self.assertRaisesRegex(AgentChatError, "invalid_agent_mode"):
            await chat.set_mode("auto")
        # No model known: core does not guess one for codex's settings.
        blank, _ = await attached(Peer(resume={"thread": {"id": THREAD, "turns": []}}))
        with self.assertRaisesRegex(AgentChatError, "agent_mode_unavailable"):
            await blank.set_mode("plan")

    async def test_the_captured_turns(self):
        chat, peer = await attached()
        events = await replay("plan", chat, peer)
        # thread/settings/updated (threadSettings) is read: mode, model, effort.
        self.assertEqual(chat.mode, "plan")
        self.assertEqual((chat.settings["model"], chat.settings["effort"]), ("gpt-fixture", "low"))
        updates = [e["usage"] for e in events if e["type"] == "agent.usage.updated" and "mode" in e["usage"]]
        # Unknown until codex says; then Plan, and never anything else.
        modes = [u["mode"] for u in updates]
        self.assertEqual(modes[:modes.index("plan")], [None] * modes.index("plan"))
        self.assertEqual(set(modes[modes.index("plan"):]), {"plan"})
        # The effort override turn kept the mode.
        self.assertEqual(updates[-1], {**updates[-1], "mode": "plan", "effort": "low"})
        # The question arrived as a question card.
        asked = [e for e in events if e["type"] == "agent.approval.requested"]
        self.assertEqual([e["kind"] for e in asked], ["userInput", "userInput"])
        question = asked[0]["details"]["questions"][0]
        self.assertEqual(question["header"], "Database")
        self.assertEqual([c["label"] for c in question["choices"]], ["SQLite (Recommended)", "Postgres"])
        self.assertTrue(question["other"])
        # The plan: its own message row, marked, after the words that introduce it.
        rows = chat.snapshot_value()["rows"]
        plans = [r for r in rows if r.get("plan")]
        self.assertEqual(len(plans), 2)
        first = plans[0]
        self.assertEqual(first["kind"], "message")
        self.assertEqual(first["role"], "assistant")
        self.assertTrue(first["text"].startswith("# Read the port from the environment\n\n1. Add a `PORT` setting"))
        self.assertNotIn("proposed_plan", first["text"])
        turn = [r for r in rows if r["turnID"] == first["turnID"]]
        self.assertEqual([r.get("plan", False) for r in turn if r["role"] == "assistant"], [False, True])
        # It streamed: each delta made the row longer, the completion is the whole text.
        streamed = [e["value"]["text"] for e in events if e["type"] == "message" and e["value"].get("plan")
                    and e["value"]["id"] == first["id"]]
        self.assertGreaterEqual(len(streamed), 3)
        self.assertEqual(streamed, sorted(streamed, key=len))
        self.assertEqual(streamed[-1], first["text"])

    async def test_leaving_after_a_restart(self):
        resume = CAPTURE["responses"]["thread/resume"]
        chat, peer = await attached(Peer(resume=resume))
        # Re-attach: the mode is the thread's, read from codex.
        self.assertEqual(chat.mode, "plan")
        self.assertEqual(chat.snapshot_value()["usage"]["mode"], "plan")
        await replay("leave", chat, peer)
        self.assertEqual(chat.mode, "default")
        self.assertEqual(chat.snapshot_value()["usage"]["mode"], "default")

    async def test_history_puts_the_plan_after_its_introduction(self):
        resume = CAPTURE["responses"]["thread/resume"]
        # codex's record lists the plan first (it finished first).
        raw = [[item["type"] for item in turn["items"]] for turn in resume["thread"]["turns"]]
        self.assertIn(["userMessage", "plan", "agentMessage"], raw)
        chat, _ = await attached(Peer(resume=resume))
        rows = chat.snapshot_value()["rows"]
        for turn in {r["turnID"] for r in rows if r.get("plan")}:
            kinds = [("plan" if r.get("plan") else r["role"]) for r in rows if r["turnID"] == turn]
            self.assertEqual(kinds, ["user", "assistant", "plan"])

    async def test_a_plan_row_is_an_ordinary_message_to_an_old_client(self):
        chat, peer = await attached()
        await replay("plan", chat, peer)
        plan = next(r for r in chat.snapshot_value()["rows"] if r.get("plan"))
        # Everything an older App reads is there; `plan` is the only addition.
        self.assertEqual(set(plan), {"kind", "id", "turnID", "role", "text", "plan"})


class ReviewAndDiff(unittest.IsolatedAsyncioTestCase):
    """`/review` is codex's `review/start`; `/diff` is core's own record."""
    def split(self):
        frames = CAPTURE["review"]
        starts = [i for i, f in enumerate(frames) if f["method"] == "item/started"
                  and f["params"]["item"]["type"] == "enteredReviewMode"]
        ends = [i for i, f in enumerate(frames) if f["method"] == "turn/completed"]
        return frames, starts, ends

    def feed(self, chat, peer, frames):
        events = []
        for frame in frames:
            peer.feed(rethread(frame))
            events.extend(drain(chat))
        return events

    async def test_a_review_is_one_turn_whatever_codex_calls_it(self):
        frames, starts, ends = self.split()
        chat, peer = await attached()
        self.feed(chat, peer, frames[:starts[0]])
        drain(chat)
        review_turn = CAPTURE["responses"]["review/start"][0]["turn"]["id"]
        # The answer first, then codex's frames (the review's announcement
        # carries another turn id).
        reply = await chat.review()
        self.assertEqual(reply, {"accepted": True, "turnID": review_turn, "delivery": "accepted"})
        self.assertEqual(peer.requests[-1], ("review/start", {"threadId": THREAD, "target": {"type": "uncommittedChanges"}}))
        events = drain(chat) + self.feed(chat, peer, frames[starts[0]:ends[1] + 1])
        started = [e["turnID"] for e in events if e["type"] == "turnStarted"]
        self.assertEqual(started, [review_turn])
        rows = [e["value"] for e in events if e["type"] == "message"]
        self.assertTrue(rows and all(row["turnID"] == review_turn for row in rows))
        self.assertTrue(any(row["role"] == "user" and row["text"].startswith("Review the current code changes")
                            for row in rows))
        self.assertTrue(any(row["role"] == "assistant" and "[P2] Port is hard-coded" in row["text"] for row in rows))
        self.assertEqual([e["turnID"] for e in events if e["type"] == "turnFinished"], [review_turn])
        self.assertIsNone(chat.active_turn)
        self.assertEqual(chat.activity_value()["phase"], "done")

    async def test_a_review_whose_frames_come_before_its_answer(self):
        frames, starts, ends = self.split()
        chat, peer = await attached()
        # The first review was started by another client (codex's TUI): core
        # still follows it as one turn, and it ends.
        events = self.feed(chat, peer, frames[:starts[1]])
        first = CAPTURE["responses"]["review/start"][0]["turn"]["id"]
        self.assertEqual([e["turnID"] for e in events if e["type"] in ("turnStarted", "turnFinished")][-2:], [first, first])
        self.assertIsNone(chat.active_turn)
        peer.reviews.pop(0)
        review_turn = peer.reviews[0]["turn"]["id"]
        # codex's first frame of the review arrives while core waits for the answer.
        peer.before_review = lambda: peer.feed(rethread(frames[starts[1]]))
        await chat.review("check the port handling")
        self.assertEqual(peer.requests[-1][1]["target"], {"type": "custom", "instructions": "check the port handling"})
        events = drain(chat) + self.feed(chat, peer, frames[starts[1] + 1:ends[2] + 1])
        self.assertEqual([e["turnID"] for e in events if e["type"] == "turnStarted"], [review_turn])
        self.assertEqual([e["turnID"] for e in events if e["type"] == "turnFinished"], [review_turn])
        self.assertIsNone(chat.active_turn)

    async def test_compaction_is_a_turn_of_its_own(self):
        chat, peer = await attached()
        await chat.compact()
        self.assertEqual(peer.requests[-1], ("thread/compact/start", {"threadId": THREAD}))
        events = []
        for frame in CAPTURE["compact"]:
            peer.feed(rethread(frame))
            events.extend(drain(chat))
        self.assertEqual([e["type"] for e in events if e["type"] in ("turnStarted", "turnFinished")],
                         ["turnStarted", "turnFinished"] * 2)
        self.assertIsNone(chat.active_turn)

    async def test_diff_is_the_conversations_change_cards(self):
        chat, _ = await attached()
        chat.rows = {
            "m": {"kind": "message", "id": "m", "turnID": "t1", "role": "user", "text": "edit it"},
            "f1": {"kind": "tool", "id": "f1", "turnID": "t1", "name": "File changes", "status": "succeeded", "detail": "[]",
                   "changes": [{"path": "/w/app.py", "kind": "update", "added": 1, "removed": 1,
                                "diff": "@@ -1 +1 @@\n-port = 8080\n+port = 9090"}]},
            "c1": {"kind": "tool", "id": "c1", "turnID": "t1", "name": "ls", "status": "succeeded", "detail": ""},
            "f2": {"kind": "tool", "id": "f2", "turnID": "t2", "name": "File changes", "status": "succeeded", "detail": "[]",
                   "changes": [{"path": "/w/notes.md", "kind": "add", "added": 2, "removed": 0, "diff": "+a\n+b"}]},
        }
        changes, omitted = chat.conversation_changes()
        self.assertEqual(omitted, 0)
        self.assertEqual([(c["path"], c["kind"], c["added"], c["removed"], c["turnID"]) for c in changes],
                         [("/w/app.py", "update", 1, 1, "t1"), ("/w/notes.md", "add", 2, 0, "t2")])
        self.assertEqual(changes[0]["diff"], "@@ -1 +1 @@\n-port = 8080\n+port = 9090")
        # bounded: the newest kept, the count of the rest said
        changes, omitted = chat.conversation_changes(limit=1)
        self.assertEqual(([c["path"] for c in changes], omitted), (["/w/notes.md"], 1))
        # core's rows are not touched
        self.assertNotIn("turnID", chat.rows["f1"]["changes"][0])


class SlashPlan(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.chat, self.peer = await attached()

        async def require(authorize):
            return self.chat
        self.commands = AgentCommands(SimpleNamespace(require=require, manager=SimpleNamespace(cwd=Path("/w"))))

    async def run_plan(self, arguments, request="p1"):
        return await self.commands.execute("plan", {"revision": self.commands.revision, "arguments": arguments,
                                                    "request_id": request}, lambda: None)

    async def test_listed_and_available(self):
        row = next(r for r in self.commands.listing()["commands"] if r["id"] == "plan")
        self.assertTrue(row["available"])
        self.assertEqual((row["argument_hint"], row["turn_policy"]), ("[prompt]", "idle_required"))
        self.assertEqual(row["description"], "switch to Plan mode")

    async def test_plan_toggles_and_plan_with_words_sends(self):
        on = await self.run_plan("", "p1")
        self.assertEqual((on["status"], on["display"]), ("completed", {"key": "plan.on", "args": {"mode": "plan"}}))
        self.assertEqual(self.peer.requests[-1][0], "thread/settings/update")
        off = await self.run_plan("", "p2")
        self.assertEqual(off["display"], {"key": "plan.off", "args": {"mode": "default"}})
        self.assertEqual(self.chat.mode, "default")
        sent = await self.run_plan("  plan the port change", "p3")
        self.assertEqual((sent["status"], sent["display"]["key"]), ("accepted", "plan.sent"))
        method, params = self.peer.requests[-1]
        self.assertEqual(method, "turn/start")
        self.assertEqual(params["clientUserMessageId"], "p3")
        self.assertEqual(params["input"][0]["text"], "plan the port change")
        self.assertEqual(params["collaborationMode"]["mode"], "plan")

    async def test_refused_while_a_turn_runs(self):
        self.chat.active_turn = "turn-1"
        with self.assertRaisesRegex(ServiceError, "agent_busy"):
            await self.run_plan("")


class ChipOverHTTP(unittest.IsolatedAsyncioTestCase):
    """`POST /v1/agent/default/chat/mode`, the real service and adapter."""
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        cap = SimpleNamespace(configured=True, configured_kind="codex", default_agent_exists=False,
                              default_agent_probe=ProbeStatus.MISSING)
        manager = DefaultAgentManager(root=root, probe=SimpleNamespace(inspect=lambda: (cap, None)),
                                      runner=lambda _: None)
        self.peer = Peer()

        class Owner:
            def owned_metadata(self): return None
            async def ensure_new_or_owned(self, existing_agent=False): return binding()
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

    async def post(self, path, body):
        async with self.client.post(self.url + path, headers=self.headers, json=body) as response:
            return response.status, await response.json()

    async def test_the_chip_route(self):
        code, result = await self.post("/v1/agent/default:ensure", {"surface": "chat"})
        self.assertEqual(code, 200, result)
        self.assertIsNone(result["snapshot"]["usage"]["mode"])
        code, result = await self.post("/v1/agent/default/chat/mode", {"mode": "plan"})
        self.assertEqual(code, 200, result)
        self.assertEqual((result["mode"], result["changed"], result["usage"]["mode"]), ("plan", True, "plan"))
        # The host's state says it too, for every client watching.
        self.assertEqual(self.hub.state_snapshot()["agent"]["usage"]["mode"], "plan")
        code, result = await self.post("/v1/agent/default/chat/mode", {"mode": "plan"})
        self.assertEqual((code, result["changed"]), (200, False))
        code, result = await self.post("/v1/agent/default/chat/mode", {"mode": "default"})
        self.assertEqual((code, result["mode"]), (200, "default"))
        code, result = await self.post("/v1/agent/default/chat/mode", {"mode": "auto"})
        self.assertEqual(code, 400, result)
        code, result = await self.post("/v1/agent/default/chat/mode", {"mode": "plan", "sandbox": "x"})
        self.assertEqual(code, 400, result)
        self.service.chat.active_turn = "turn-1"
        code, result = await self.post("/v1/agent/default/chat/mode", {"mode": "plan"})
        self.assertEqual((code, result["error"]["code"]), (409, "agent_busy"))
        sent = [params for method, params in self.peer.requests if method == "thread/settings/update"]
        self.assertEqual(len(sent), 3)
        self.assertFalse(any(FORBIDDEN & set(params) for params in sent))

    async def test_what_is_typed_goes_through_and_two_commands_are_quiet(self):
        """Leo (2026-10-07): no slash list on the phone - what is typed goes to
        codex as typed. `/plan` and `/compact` are the two that cannot work as
        words; core handles them in the message route."""
        code, result = await self.post("/v1/agent/default:ensure", {"surface": "chat"})
        self.assertEqual(code, 200, result)
        chat = self.service.chat

        def started():
            return [params for method, params in self.peer.requests if method == "turn/start"]

        # Any other slash command is the person's words, exactly as typed.
        for number, words in enumerate(["/vim", "/model gpt-other high", "/new", "/planning the week", "/compact now"]):
            code, result = await self.post("/v1/agent/default/chat/messages", {"request_id": "w%d" % number, "text": words})
            self.assertEqual(code, 200, result)
            self.assertEqual(result["delivery"], "accepted")
            self.assertNotIn("command", result)
            self.assertEqual(started()[-1]["input"], [{"type": "text", "text": words}])
            self.assertNotIn("collaborationMode", started()[-1])
            chat.active_turn = None
        turns = len(started())
        # `/plan` alone: Plan mode on, no turn, and the answer says so.
        code, result = await self.post("/v1/agent/default/chat/messages", {"request_id": "p1", "text": "/plan"})
        self.assertEqual((code, result["delivery"], result["command"], result["mode"]), (200, "command", "plan", "plan"))
        self.assertEqual(len(started()), turns)
        self.assertEqual(self.peer.requests[-1][0], "thread/settings/update")
        self.assertEqual(self.hub.state_snapshot()["agent"]["usage"]["mode"], "plan")
        # The same request again answers the same: a retry never switches back.
        code, again = await self.post("/v1/agent/default/chat/messages", {"request_id": "p1", "text": "/plan"})
        self.assertEqual((code, again["mode"], chat.mode), (200, "plan", "plan"))
        # `/plan` again: off.
        code, result = await self.post("/v1/agent/default/chat/messages", {"request_id": "p2", "text": " /plan "})
        self.assertEqual((result["delivery"], result["mode"], chat.mode), ("command", "default", "default"))
        # `/plan <words>`: on, and the words (only) in the same turn.
        code, result = await self.post("/v1/agent/default/chat/messages",
                                       {"request_id": "p3", "text": "/plan read the port\nfrom the environment"})
        self.assertEqual((code, result["delivery"], result["command"], result["mode"]), (200, "accepted", "plan", "plan"))
        self.assertEqual(result["turnID"], "turn-new")
        self.assertEqual(started()[-1]["input"], [{"type": "text", "text": "read the port\nfrom the environment"}])
        self.assertEqual(started()[-1]["collaborationMode"]["mode"], "plan")
        self.assertFalse(FORBIDDEN & set(started()[-1]))
        # While that turn runs, `/plan` and `/compact` wait - never words added to it.
        for words in ("/plan", "/compact"):
            code, result = await self.post("/v1/agent/default/chat/steer", {"request_id": "s-" + words[1:], "text": words})
            self.assertEqual((code, result["error"]["code"]), (409, "agent_command_while_working"))
        code, result = await self.post("/v1/agent/default/chat/messages", {"request_id": "p4", "text": "/plan"})
        self.assertEqual((code, result["error"]["code"]), (409, "agent_busy"))
        chat.active_turn = None
        # `/compact`: codex's own compaction, no words sent.
        code, result = await self.post("/v1/agent/default/chat/messages", {"request_id": "c1", "text": "/compact"})
        self.assertEqual((code, result["delivery"], result["command"]), (200, "command", "compact"))
        self.assertEqual(self.peer.requests[-1], ("thread/compact/start", {"threadId": THREAD}))
        # `/review`: codex's review of the uncommitted changes, a turn of this conversation.
        code, result = await self.post("/v1/agent/default/chat/messages", {"request_id": "r1", "text": "/review"})
        self.assertEqual((code, result["delivery"], result["command"]), (200, "accepted", "review"))
        self.assertEqual(self.peer.requests[-1][1]["target"], {"type": "uncommittedChanges"})
        chat.active_turn = None
        code, result = await self.post("/v1/agent/default/chat/messages", {"request_id": "r2", "text": "/review the port"})
        self.assertEqual(self.peer.requests[-1][1]["target"], {"type": "custom", "instructions": "the port"})
        chat.active_turn = None
        # `/diff`: the conversation's changes from core's own rows; no turn.
        chat.rows["f"] = {"kind": "tool", "id": "f", "turnID": "t9", "name": "File changes", "status": "succeeded",
                          "detail": "[]", "changes": [{"path": "/w/app.py", "kind": "update", "added": 1, "removed": 1,
                                                       "diff": "-a\n+b"}]}
        turns = len(started())
        code, result = await self.post("/v1/agent/default/chat/messages", {"request_id": "d1", "text": "/diff"})
        self.assertEqual((code, result["delivery"], result["command"]), (200, "command", "diff"))
        self.assertEqual([c["path"] for c in result["changes"]], ["/w/app.py"])
        self.assertEqual(len(started()), turns)
        # `/diff something` and `/compact now` are words.
        code, result = await self.post("/v1/agent/default/chat/messages", {"request_id": "d2", "text": "/diff of what"})
        self.assertEqual(started()[-1]["input"][0]["text"], "/diff of what")


if __name__ == "__main__":
    unittest.main()
