"""NOTIFY-1: act on the alert, stay quiet at the desk, say when it's done.

Core's half, all with fakes: the approval fingerprint and the "still pending"
check, the desk reading (a fake Wayland compositor on a socket pair, a fake
/proc), pushes held at the desk and released (or dropped) when it is left,
Herdr's 30 s delay, digest and retraction, and the done-not-seen ledger.
"""
import asyncio
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest

from omodachi_core import desk as desk_module, push, webpush
from omodachi_core.agent_chat_provider import AgentChatError, CodexDefaultAgentChat, approval_fingerprint
from omodachi_core.agent_chat_service import DefaultAgentChatService
from omodachi_core.hub import Hub
from omodachi_core.seen import SeenLedger
from omodachi_core.service import ServiceError

from tests import test_agent_chat_provider as provider_tests
from tests.test_push import APPLE, FCM, browser_keys


# ── 1. act on the alert ──────────────────────────────────────────────────────
class ApprovalFingerprintTests(unittest.IsolatedAsyncioTestCase):
    async def attached(self):
        rpc = provider_tests.ControlledRPC()
        chat = CodexDefaultAgentChat(provider_tests.ProviderTests.binding(None), rpc)
        await chat.attach()
        while not chat.events.empty():
            chat.events.get_nowait()
        return rpc, chat

    async def test_a_decision_is_sent_only_while_the_same_request_reads_the_same(self):
        rpc, chat = await self.attached()
        rpc.server_request("item/commandExecution/requestApproval", 21, threadId="thread-owned",
                           itemId="i1", turnId="turn-one", command="ls -la", cwd="/work")
        [row] = chat.approvals_value()
        self.assertRegex(row["fingerprint"], r"^[0-9a-f]{16}$")
        self.assertEqual(chat.events.get_nowait()["event"]["fingerprint"], row["fingerprint"])
        self.assertEqual(chat.approval_head(), {"request_id": "21", "kind": "commandExecution",
                                                "fingerprint": row["fingerprint"], "decidable": True})
        # What the alert showed is not what is pending now: nothing is sent.
        with self.assertRaisesRegex(AgentChatError, "agent_approval_changed"):
            await chat.resolve_approval("21", decision="accept", fingerprint="0" * 16)
        self.assertEqual(rpc.answers, [])
        result = await chat.resolve_approval("21", decision="decline", fingerprint=row["fingerprint"])
        self.assertEqual((result["decision"], rpc.answers), ("decline", [(21, {"decision": "decline"})]))
        # Answered already (on the computer, or by another device): unknown, nothing sent.
        with self.assertRaisesRegex(AgentChatError, "agent_approval_unknown"):
            await chat.resolve_approval("21", decision="accept", fingerprint=row["fingerprint"])
        self.assertEqual(len(rpc.answers), 1)
        self.assertIsNone(chat.approval_head())

    async def test_the_fingerprint_follows_what_is_shown_and_a_question_is_not_decidable(self):
        base = {"request_id": "7", "kind": "commandExecution", "summary": "ls", "details": {"cwd": "/a"},
                "decisions": ["accept", "decline"]}
        self.assertEqual(approval_fingerprint(base), approval_fingerprint(dict(base)))
        for change in ({"summary": "rm -rf /"}, {"details": {"cwd": "/b"}}, {"request_id": "8"},
                       {"decisions": ["accept"]}):
            self.assertNotEqual(approval_fingerprint(base), approval_fingerprint({**base, **change}), change)
        rpc, chat = await self.attached()
        rpc.server_request("item/tool/requestUserInput", 30, threadId="thread-owned", itemId="q",
                           questions=[{"id": "q1", "question": "Which branch?"}])
        self.assertFalse(chat.approval_head()["decidable"])

    async def test_the_service_checks_the_shape_and_publishes_ids_only(self):
        rpc, chat = await self.attached()
        hub = Hub()
        service = DefaultAgentChatService.__new__(DefaultAgentChatService)
        service.chat, service.hub, service.switching = chat, hub, False
        rpc.server_request("item/commandExecution/requestApproval", 41, threadId="thread-owned",
                           itemId="i", turnId="turn-one", command="make deploy", cwd="/srv")
        service._publish_agent_state()
        approval = hub.state_view("agent")["agent"]["chat"]["approval"]
        self.assertEqual(set(approval), {"request_id", "kind", "fingerprint", "decidable"})
        self.assertNotIn("make deploy", json.dumps(hub.state_view("agent")))
        for bad in ("ABC", "0" * 15, 7, "g" * 16):
            with self.assertRaises(ServiceError):
                await service.approve("41", {"decision": "accept", "fingerprint": bad}, lambda: None)
        with self.assertRaises(ServiceError) as caught:
            await service.approve("41", {"decision": "accept", "fingerprint": "f" * 16}, lambda: None)
        self.assertEqual((caught.exception.code, caught.exception.status), ("agent_approval_changed", 409))
        result = await service.approve("41", {"decision": "accept", "fingerprint": approval["fingerprint"]},
                                       lambda: None)
        self.assertTrue(result["resolved"])
        self.assertIsNone(hub.state_view("agent")["agent"]["chat"]["approval"])


# ── 2. quiet while Leo is at the desk ────────────────────────────────────────
def _event(obj, opcode, body=b""):
    return desk_module._message(obj, opcode, body)


class FakeCompositor:
    """The compositor's side of `WaylandIdleClient`, on a socket pair."""

    def __init__(self, *, notifier=True, version=2):
        self.server, self.client = socket.socketpair()
        self.notifier, self.version = notifier, version
        self.requests = []

    def _read(self, count):
        data = b""
        while len(data) < count:
            chunk = self.server.recv(count - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def read_request(self):
        obj, word = struct.unpack("<II", self._read(8))
        body = self._read((word >> 16) - 8)
        self.requests.append((obj, word & 0xFFFF, body))
        return obj, word & 0xFFFF, body

    def handshake(self):
        self.read_request()  # get_registry
        self.read_request()  # sync
        globals_ = [(1, "wl_seat", 7), (2, "wl_compositor", 6)]
        if self.notifier:
            globals_.append((3, "ext_idle_notifier_v1", self.version))
        out = b""
        for name, interface, version in globals_:
            out += _event(2, 0, struct.pack("<I", name) + desk_module._string(interface) + struct.pack("<I", version))
        out += _event(3, 0, struct.pack("<I", 1))
        self.server.sendall(out)

    def close(self):
        for end in (self.server, self.client):
            try:
                end.close()
            except OSError:
                pass

    def say(self, word):
        self.server.sendall(_event(6, 0 if word == "idled" else 1))


class DeskTests(unittest.TestCase):
    def test_the_idle_client_registers_one_input_idle_notification_and_reads_two_words(self):
        compositor = FakeCompositor()
        thread = threading.Thread(target=compositor.handshake)
        thread.start()
        client = desk_module.WaylandIdleClient(Path("/nonexistent"), 120000, sock=compositor.client).connect()
        thread.join()
        self.assertEqual(client.version, 2)
        binds = [compositor.read_request() for _ in range(3)]
        self.assertEqual([(obj, opcode) for obj, opcode, _ in binds], [(2, 0), (2, 0), (5, 2)])  # v2: input idle
        self.assertEqual(struct.unpack("<III", binds[2][2]), (6, 120000, 4))
        compositor.say("idled")
        time.sleep(0.05)
        self.assertEqual(client.read_events(), ["idled"])
        compositor.say("resumed")
        time.sleep(0.05)
        self.assertEqual(client.read_events(), ["resumed"])
        client.close()
        compositor.close()

    def test_a_compositor_without_the_protocol_is_unavailable(self):
        compositor = FakeCompositor(notifier=False)
        thread = threading.Thread(target=compositor.handshake)
        thread.start()
        with self.assertRaises(desk_module.WaylandError):
            desk_module.WaylandIdleClient(Path("/x"), 1000, sock=compositor.client).connect()
        thread.join()
        compositor.close()

    def test_a_v1_compositor_gets_the_plain_notification(self):
        compositor = FakeCompositor(version=1)
        thread = threading.Thread(target=compositor.handshake)
        thread.start()
        desk_module.WaylandIdleClient(Path("/x"), 5000, sock=compositor.client).connect()
        thread.join()
        binds = [compositor.read_request() for _ in range(3)]
        self.assertEqual(binds[2][:2], (5, 1))
        compositor.close()

    def test_the_locker_is_read_from_proc_for_this_user_only(self):
        with tempfile.TemporaryDirectory() as root:
            proc = Path(root)
            for pid, comm in (("100", "Hyprland"), ("200", "bash"), ("self", "x")):
                (proc / pid).mkdir()
                (proc / pid / "comm").write_text(comm + "\n")
            self.assertFalse(desk_module.locked(proc))
            (proc / "300").mkdir()
            (proc / "300" / "comm").write_text("hyprlock\n")
            self.assertTrue(desk_module.locked(proc))
            self.assertFalse(desk_module.locked(proc, uid=os.getuid() + 1))

    def test_the_answer_combines_lock_remote_and_idle(self):
        state = {"locked": False}
        presence = desk_module.DeskPresence(locked_probe=lambda: state["locked"])
        self.assertEqual(presence.evaluate(), {"present": False, "state": "unknown", "source": "unavailable"})
        presence._set(False, "wayland")
        self.assertEqual(presence.evaluate(), {"present": True, "state": "using", "source": "wayland"})
        self.assertEqual(presence.evaluate(remote_live=True)["state"], "remote")   # the phone is driving it
        presence._set(True, "wayland")
        self.assertEqual(presence.evaluate()["state"], "idle")
        state["locked"] = True
        presence._set(False, "wayland")
        self.assertEqual(presence.evaluate(), {"present": False, "state": "locked", "source": "wayland"})

    def test_the_watch_thread_follows_the_compositor(self):
        compositor = FakeCompositor()
        with tempfile.TemporaryDirectory() as root:
            socket_path = Path(root) / "wayland-1"
            socket_path.write_text("")
            factory = lambda path, ms: desk_module.WaylandIdleClient(path, ms, sock=compositor.client)
            presence = desk_module.DeskPresence(env={"XDG_RUNTIME_DIR": root, "WAYLAND_DISPLAY": "wayland-1"},
                                                locked_probe=lambda: False, client_factory=factory)
            server = threading.Thread(target=lambda: (compositor.handshake(),
                                                      [compositor.read_request() for _ in range(3)]))
            server.start()
            presence.start()
            server.join(2)
            deadline = time.monotonic() + 2
            while presence.evaluate()["state"] != "using" and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(presence.evaluate()["state"], "using")
            compositor.say("idled")
            deadline = time.monotonic() + 2
            while presence.evaluate()["state"] != "idle" and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(presence.evaluate()["state"], "idle")
            compositor.say("resumed")
            deadline = time.monotonic() + 2
            while presence.evaluate()["state"] != "using" and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(presence.evaluate()["present"], True)
            presence.stop()
            self.assertEqual(presence.evaluate()["state"], "unknown")
        compositor.close()


class FakeSend:
    def __init__(self):
        self.sent = []
        self.lock = threading.Lock()

    def __call__(self, subscription, body, vapid, **options):
        with self.lock:
            self.sent.append({"endpoint": subscription["endpoint"], "body": json.loads(body), **options})
        return {"status": 201}


class HoldAtTheDeskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub = Hub()
        self.store = push.PushStore(Path(self.temp.name) / "push.json")
        _, _, keys = browser_keys()
        self.store.subscribe("odomo-chrome", {"endpoint": FCM, "keys": keys})
        self.send = FakeSend()
        self.desk = {"present": True, "state": "using", "source": "wayland"}
        self.sender = push.PushSender(self.hub, self.store, lambda: webpush.Vapid.generate(),
                                      origin=lambda: "https://lab.example.ts.net", host_name=lambda: "lab",
                                      native_alerts=lambda kind: False, send=self.send, desk=lambda: self.desk,
                                      debounce=30.0, sleep=lambda s: None, log=lambda line: None,
                                      herdr_delay=0.05)
        self.sender.start()

    async def asyncTearDown(self):
        await self.sender.stop()
        self.temp.cleanup()

    async def settle(self, wait=0.0):
        if wait:
            await asyncio.sleep(wait)
        for _ in range(3):
            await asyncio.sleep(0)
        await self.sender.drain()

    def titles(self):
        return [row["body"]["notification"]["title"] for row in self.send.sent if "web_push" in row["body"]]

    def leave_desk(self, reason="idle"):
        self.desk = {"present": False, "state": reason, "source": "wayland"}
        self.hub.update_state({"desk": dict(self.desk)}, event_type="desk.changed")

    def agent(self, status):
        self.hub.update_state({"agent": {"status": status}}, event_type="agent.changed")

    async def test_held_at_the_desk_and_sent_when_it_is_left_if_still_pending(self):
        self.agent("waiting_on_approval")
        await self.settle()
        self.assertEqual(self.send.sent, [])                              # Leo is at the computer
        self.leave_desk("locked")
        await self.settle()
        self.assertEqual(self.titles(), ["The agent needs your approval"])  # still pending: delivered

    async def test_answered_at_the_computer_is_dropped(self):
        self.agent("waiting_on_approval")
        await self.settle()
        self.agent("working")                                             # approved at the desk
        await self.settle()
        self.leave_desk()
        await self.settle()
        self.assertEqual(self.titles(), [])

    async def test_the_setting_turns_holding_off(self):
        self.store.set_prefs("odomo-chrome", 0, {"quiet_at_desk": False})
        self.agent("waiting_on_approval")
        await self.settle()
        self.assertEqual(self.titles(), ["The agent needs your approval"])

    async def test_unknown_presence_never_holds(self):
        self.desk = {"present": False, "state": "unknown", "source": "unavailable"}
        self.agent("waiting_on_approval")
        await self.settle()
        self.assertEqual(len(self.titles()), 1)

    async def test_herdr_waits_out_the_desk_too_and_goes_as_one_digest(self):
        self.hub.publish("herdr.layout.changed", {"session": "s", "revision": 1, "blocked": []})
        self.hub.publish("herdr.layout.changed", {"session": "s", "revision": 2, "blocked": [
            {"pane_id": "w1:p1", "name": "codex"}, {"pane_id": "w1:p2", "name": "claude"}]})
        await self.settle(0.1)
        self.assertEqual(self.send.sent, [])                              # delay over, but at the desk
        self.assertIn("odomo-chrome", self.sender._herdr_held)
        self.leave_desk()
        await self.settle()
        [only] = [row for row in self.send.sent if "web_push" in row["body"]]
        self.assertEqual(only["body"]["notification"]["title"], "2 agents need you")
        self.assertEqual(only["body"]["notification"]["tag"], "herdr")
        self.assertIn("open=herdr&session=s&pane=w1%3Ap1", only["body"]["notification"]["navigate"])

    async def test_snooze_also_silences_what_was_already_waiting(self):
        self.desk = {"present": False, "state": "idle", "source": "wayland"}
        self.agent("waiting_on_approval")
        await self.settle()
        self.agent("working")
        self.agent("waiting_on_approval")                                 # waits out the 30 s window
        await self.settle()
        self.assertEqual(len(self.titles()), 1)
        self.store.set_prefs("odomo-chrome", 0, {"snooze_until": int(time.time()) + 600})
        key = next(key for key, window in self.sender._windows.items() if window["timer"])
        self.sender._windows[key]["timer"].cancel()
        self.sender._flush(key)
        await self.settle()
        self.assertEqual(len(self.titles()), 1)


class HerdrDigestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub = Hub()
        self.store = push.PushStore(Path(self.temp.name) / "push.json")
        _, _, keys = browser_keys()
        self.store.subscribe("odomo-chrome", {"endpoint": FCM, "keys": keys})
        self.store.subscribe("odomo-iphone", {"endpoint": APPLE, "keys": keys})
        self.send = FakeSend()
        self.sender = push.PushSender(self.hub, self.store, lambda: webpush.Vapid.generate(),
                                      origin=lambda: "https://lab.example.ts.net", host_name=lambda: "lab",
                                      native_alerts=lambda kind: False, send=self.send,
                                      sleep=lambda s: None, log=lambda line: None, herdr_delay=0.05)
        self.sender.start()
        self.hub.publish("herdr.layout.changed", {"session": "s", "revision": 1, "blocked": []})
        self.hub.publish("herdr.layout.changed", {"session": "t", "revision": 1, "blocked": []})

    async def asyncTearDown(self):
        await self.sender.stop()
        self.temp.cleanup()

    async def settle(self, wait=0.0):
        if wait:
            await asyncio.sleep(wait)
        for _ in range(3):
            await asyncio.sleep(0)
        await self.sender.drain()

    def block(self, session, revision, *panes):
        self.hub.publish("herdr.layout.changed", {"session": session, "revision": revision,
                                                  "blocked": [{"pane_id": p, "name": "codex"} for p in panes]})

    async def test_nothing_before_the_delay_then_one_digest_per_device(self):
        self.block("s", 2, "w1:p1")
        await self.settle()
        self.assertEqual(self.send.sent, [])
        self.block("t", 2, "w3:p1")                    # another session, same host, same delay
        await self.settle(0.1)
        pushes = [row for row in self.send.sent if "web_push" in row["body"]]
        self.assertEqual(sorted(row["endpoint"] for row in pushes), sorted([FCM, APPLE]))
        self.assertTrue(all(row["body"]["notification"]["title"] == "2 agents need you" for row in pushes))
        self.assertTrue(all(row["topic"] == push.topic_for("herdr") for row in pushes))

    async def test_one_pane_is_named_and_the_digest_is_withdrawn_when_all_are_answered(self):
        self.block("s", 2, "w1:p1")
        await self.settle(0.1)
        note = next(row for row in self.send.sent if row["endpoint"] == FCM)["body"]["notification"]
        self.assertEqual((note["title"], note["tag"]), ("codex needs you", "herdr"))
        self.block("s", 3)
        await self.settle()
        retracts = [row for row in self.send.sent if "omodachi" in row["body"]]
        self.assertEqual([row["endpoint"] for row in retracts], [FCM])           # never on Apple
        # A retraction lives as long as the push it takes back.
        self.assertEqual((retracts[0]["ttl"], retracts[0]["urgency"], retracts[0]["topic"]),
                         (3600, "high", push.topic_for("herdr")))

    async def test_a_new_pane_while_others_wait_sends_a_fresh_digest(self):
        self.block("s", 2, "w1:p1")
        await self.settle(0.1)
        self.block("s", 3, "w1:p1", "w1:p2")
        await self.settle(0.1)
        titles = [row["body"]["notification"]["title"] for row in self.send.sent
                  if row["endpoint"] == FCM and "web_push" in row["body"]]
        self.assertEqual(titles, ["codex needs you", "2 agents need you"])
        self.block("s", 4, "w1:p2")                                               # one answered, one left
        await self.settle(0.1)
        self.assertEqual(len([row for row in self.send.sent if row["endpoint"] == FCM]), 2)  # nothing new to say


# ── 3. done, not yet seen ─────────────────────────────────────────────────────
def layout(*panes):
    return {"workspaces": [{"tabs": [{"panes": [
        {"id": pane, "agent_status": status, "agent": {"name": "default", "kind": "codex", "status": status}}
        for pane, status in panes]}]}]}


class SeenLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [1000.0]
        self.ledger = SeenLedger(Path(self.temp.name) / "seen.json", clock=lambda: self.now[0])

    def tearDown(self):
        self.temp.cleanup()

    def look(self, *panes):
        self.now[0] += 2
        return self.ledger.observe_herdr("s", layout(*panes))

    def test_working_to_idle_is_finished_and_unseen_until_opened(self):
        self.assertEqual(self.look(("w1:p1", "working"), ("w1:p2", "idle")), [])   # first sighting: a baseline
        self.assertEqual(self.ledger.value(), {"agent": False, "herdr": []})
        finished = self.look(("w1:p1", "idle"), ("w1:p2", "idle"))                  # Herdr 0.9: done reads idle
        self.assertEqual(finished, [{"session": "s", "pane_id": "w1:p1", "name": "codex"}])
        self.assertEqual(self.ledger.value()["herdr"], [{"session": "s", "pane_id": "w1:p1", "name": "codex"}])
        self.now[0] += 1
        self.ledger.watch("herdr:s:w1:p1", +1)                                       # opened on a phone
        self.assertEqual(self.ledger.value()["herdr"], [])

    def test_blocked_to_done_counts_and_working_again_clears(self):
        self.look(("w1:p1", "blocked"))
        self.look(("w1:p1", "done"))
        self.assertEqual(len(self.ledger.value()["herdr"]), 1)
        self.look(("w1:p1", "working"))
        self.assertEqual(self.ledger.value()["herdr"], [])

    def test_a_pane_watched_while_it_finishes_is_seen_and_keys_count(self):
        self.look(("w1:p1", "working"), ("w1:p2", "working"))
        self.ledger.watch("herdr:s:w1:p1", +1)
        self.assertEqual(self.look(("w1:p1", "idle"), ("w1:p2", "idle")),
                         [{"session": "s", "pane_id": "w1:p2", "name": "codex"}])
        self.ledger.seen("herdr:s:w1:p2")                                            # keys sent to it
        self.assertEqual(self.ledger.value()["herdr"], [])

    def test_idle_to_idle_and_idle_shells_never_count(self):
        self.look(("w1:p1", "idle"))
        self.look(("w1:p1", "idle"))
        self.look(("w1:p1", None))
        self.assertEqual(self.ledger.value()["herdr"], [])

    def test_the_agent_conversation(self):
        self.assertFalse(self.ledger.observe_agent("idle"))
        self.assertFalse(self.ledger.observe_agent("thinking"))
        self.assertTrue(self.ledger.observe_agent("done"))
        self.assertTrue(self.ledger.value()["agent"])
        self.ledger.watch("agent:default", +1)
        self.assertFalse(self.ledger.value()["agent"])
        self.ledger.watch("agent:default", -1)
        self.ledger.observe_agent("working")
        self.assertFalse(self.ledger.observe_agent("stopped"))                         # interrupted by the user

    def test_it_survives_a_restart_and_gone_panes_are_dropped(self):
        self.look(("w1:p1", "working"), ("w1:p2", "working"))
        self.look(("w1:p1", "idle"), ("w1:p2", "working"))
        self.ledger.save()
        self.assertEqual(os.stat(self.ledger.path).st_mode & 0o777, 0o600)
        again = SeenLedger(self.ledger.path, clock=lambda: self.now[0])
        self.assertEqual(again.value()["herdr"], [{"session": "s", "pane_id": "w1:p1", "name": "codex"}])
        again.observe_herdr("s", layout(("w1:p2", "working")))                       # w1:p1 closed
        self.assertEqual(again.value()["herdr"], [])
        later = SeenLedger(self.ledger.path, clock=lambda: self.now[0] + 31 * 86400)  # a month later
        self.assertEqual(later.value(), {"agent": False, "herdr": []})


class FinishedPushTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub = Hub()
        self.store = push.PushStore(Path(self.temp.name) / "push.json")
        _, _, keys = browser_keys()
        self.store.subscribe("odomo-chrome", {"endpoint": FCM, "keys": keys})
        self.send = FakeSend()
        self.desk = None
        self.sender = push.PushSender(self.hub, self.store, lambda: webpush.Vapid.generate(),
                                      origin=lambda: "https://lab.example.ts.net", host_name=lambda: "lab",
                                      native_alerts=lambda kind: False, send=self.send, desk=lambda: self.desk,
                                      sleep=lambda s: None, log=lambda line: None)
        self.sender.start()

    async def asyncTearDown(self):
        await self.sender.stop()
        self.temp.cleanup()

    async def settle(self):
        for _ in range(3):
            await asyncio.sleep(0)
        await self.sender.drain()

    def unseen(self, *panes):
        self.hub.update_state({"unseen": {"agent": False, "herdr": [
            {"session": "s", "pane_id": pane, "name": "codex"} for pane in panes]}}, event_type="unseen.changed")

    def titles(self):
        return [row["body"]["notification"]["title"] for row in self.send.sent if "web_push" in row["body"]]

    async def test_off_by_default_and_said_once_asked_for(self):
        self.unseen()
        self.unseen("w1:p1")
        await self.settle()
        self.assertEqual(self.titles(), [])
        self.store.set_prefs("odomo-chrome", 0, {"kinds": {"herdr_done": True}})
        self.unseen("w1:p1", "w1:p2")
        await self.settle()
        self.assertEqual(self.titles(), ["codex finished"])

    async def test_held_at_the_desk_and_dropped_once_seen(self):
        self.store.set_prefs("odomo-chrome", 0, {"kinds": {"herdr_done": True}})
        self.desk = {"present": True, "state": "using", "source": "wayland"}
        self.unseen()
        self.unseen("w1:p1")
        await self.settle()
        self.unseen()                                                              # looked at on the phone
        self.desk = {"present": False, "state": "idle", "source": "wayland"}
        self.hub.update_state({"desk": self.desk}, event_type="desk.changed")
        await self.settle()
        self.assertEqual(self.titles(), [])


class ServiceUnseenTests(unittest.TestCase):
    def test_the_service_publishes_unseen_and_the_desk_once_per_change(self):
        from omodachi_core.service import CoreService
        hub = Hub()
        service = CoreService.__new__(CoreService)
        service.hub = hub
        service.seen_ledger = None
        service._unseen_value = None
        service._unseen_lock = threading.Lock()
        service._desk_value = None
        events = []
        hub.add_observer(lambda event: events.append(event.type))
        with tempfile.TemporaryDirectory() as root:
            ledger = SeenLedger(Path(root) / "seen.json")
            service.install_seen(ledger)
            self.assertEqual(hub.state_view("unseen")["unseen"], {"agent": False, "herdr": []})
            ledger.observe_herdr("s", layout(("w1:p1", "working")))
            ledger.observe_herdr("s", layout(("w1:p1", "idle")))
            service.publish_unseen()
            service.publish_unseen()
            self.assertEqual(events.count("unseen.changed"), 2)
            service.seen_mark("s", "w1:p1")
            self.assertEqual(hub.state_view("unseen")["unseen"]["herdr"], [])
            # The Agent's turn ending, read off `agent.changed`.
            hub.update_state({"agent": {"chat": {"activity": {"phase": "working", "turn_id": "t"}}}},
                             event_type="agent.changed")
            hub.update_state({"agent": {"chat": {"activity": {"phase": "done", "turn_id": "t"}}}},
                             event_type="agent.changed")
            self.assertTrue(hub.state_view("unseen")["unseen"]["agent"])
        service.apply_desk({"present": True, "state": "using", "source": "wayland"})
        service.apply_desk({"present": True, "state": "using", "source": "wayland"})
        self.assertEqual(events.count("desk.changed"), 1)
        self.assertTrue(service.desk_state()["present"])


if __name__ == "__main__":
    unittest.main()
