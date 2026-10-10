"""HERDR-11: launch an agent with a new workspace, a pane's latest reply from
the agent's own log, and a lighter state.

The logs are synthetic, in the shapes read off the host (codex 0.160's
rollout, structure only - no line of anyone's log is in here) and Claude
Code's project log. `/proc` is a scratch directory laid out like the real one:
`stat`, `comm`, `cmdline`, `cwd` and `fd/*` links, so the matcher runs its
real code over real files and links.
"""
from __future__ import annotations

import asyncio
import copy
import gzip
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest import mock

import aiohttp

from omodachi_core import browser_gate
from omodachi_core import service as service_module
from omodachi_core.herdr_bridge import (HerdrBridge, HerdrFolders, HerdrSessionChoices, HerdrSessions,
                                        HerdrUnavailable, HerdrWriteAudit)
from omodachi_core.herdr_events import HerdrEventWatch, HerdrEventWatches, subscription
from omodachi_core.herdr_reply import (AgentReplies, ProcessTable, claude_project, claude_reply, codex_reply,
                                       etag, match_claude, match_codex, read_reply)
from omodachi_core.operator_rows import Launchers

import test_herdr_bridge
import test_herdr6

THREAD = "01a110e1-ee88-7e23-aae2-3c5cdbb8a356"
OTHER = "0b0b0b0b-1111-2222-3333-444455556666"
SESSION_ID = "6f1c2d3e-4a5b-4c6d-8e9f-0a1b2c3d4e5f"


def rollout_lines(*replies, complete=True, flat=False):
    """A codex 0.160 rollout: session meta, a turn, its messages, the end."""
    lines = [{"timestamp": "2026-10-07T10:00:00.000Z", "type": "session_meta",
              "payload": {"id": THREAD, "cwd": "/home/user/h11test", "cli_version": "0.160.0"}},
             {"timestamp": "2026-10-07T10:00:01.000Z", "type": "event_msg",
              "payload": {"type": "task_started", "turn_id": "t1"}},
             {"timestamp": "2026-10-07T10:00:01.100Z", "type": "response_item",
              "payload": {"type": "message", "role": "user",
                          "content": [{"type": "input_text", "text": "please answer"}]}}]
    for index, (text, phase) in enumerate(replies):
        item = {"type": "message", "role": "assistant", "id": f"msg_{index}",
                "content": [{"type": "output_text", "text": text}]}
        if phase:
            item["phase"] = phase
        lines.append(item if flat else {"timestamp": f"2026-10-07T10:00:0{2 + index}.000Z",
                                        "type": "response_item", "payload": item})
        lines.append({"timestamp": "2026-10-07T10:00:05.000Z", "type": "response_item",
                      "payload": {"type": "function_call", "name": "shell", "arguments": "{}"}})
    if complete:
        lines.append({"timestamp": "2026-10-07T10:00:09.000Z", "type": "event_msg",
                      "payload": {"type": "task_complete", "turn_id": "t1",
                                  "last_agent_message": replies[-1][0] if replies else None}})
    return [json.dumps(line, ensure_ascii=False).encode() for line in lines]


def claude_lines(*messages, sidechain_after=False):
    """Claude Code's project log: one line per content block of a message."""
    lines = [{"type": "user", "isSidechain": False, "message": {"role": "user", "content": "hi"},
              "timestamp": "2026-10-07T10:00:00.000Z", "sessionId": SESSION_ID}]
    for index, blocks in enumerate(messages):
        for block in blocks:
            lines.append({"type": "assistant", "isSidechain": False, "sessionId": SESSION_ID,
                          "timestamp": f"2026-10-07T10:00:0{index + 1}.000Z",
                          "message": {"id": f"msg_{index}", "role": "assistant", "stop_reason": "end_turn"
                                      if block is blocks[-1] else None, "content": [block]}})
        lines.append({"type": "user", "isSidechain": False, "timestamp": "2026-10-07T10:00:08.000Z",
                      "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}})
    if sidechain_after:
        lines.append({"type": "assistant", "isSidechain": True, "timestamp": "2026-10-07T10:00:09.000Z",
                      "message": {"id": "side", "role": "assistant",
                                  "content": [{"type": "text", "text": "a subagent's words"}]}})
    return [json.dumps(line, ensure_ascii=False).encode() for line in lines]


class LogReadingTests(unittest.TestCase):
    def test_codex_reply_is_the_last_assistant_message_and_says_when_the_turn_ended(self):
        reply = codex_reply(rollout_lines(("先看一下目录。", "commentary"), ("完成了：表格如下\n| a | b |", "final_answer")))
        self.assertEqual(reply["text"], "完成了：表格如下\n| a | b |")
        self.assertEqual((reply["phase"], reply["complete"], reply["at"]),
                         ("final_answer", True, "2026-10-07T10:00:03.000Z"))
        running = codex_reply(rollout_lines(("still working", "commentary"), complete=False))
        self.assertEqual((running["text"], running["complete"]), ("still working", False))

    def test_codex_reply_reads_the_older_flat_rollout_and_skips_empty_messages(self):
        reply = codex_reply(rollout_lines(("older format", None), ("   ", None), flat=True))
        self.assertEqual(reply["text"], "older format")
        self.assertIsNone(codex_reply(rollout_lines()))
        self.assertIsNone(codex_reply([b"not json", b"", b"[1,2]"]))

    def test_claude_reply_joins_one_messages_text_blocks_and_ignores_tools_and_subagents(self):
        reply = claude_reply(claude_lines(
            [{"type": "text", "text": "first"}],
            [{"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": "中文第一段"},
             {"type": "tool_use", "name": "Bash", "input": {}}, {"type": "text", "text": "second part"}],
            sidechain_after=True))
        self.assertEqual(reply["text"], "中文第一段\n\nsecond part")
        self.assertEqual(reply["at"], "2026-10-07T10:00:02.000Z")
        self.assertIsNone(claude_reply(claude_lines([{"type": "tool_use", "name": "x", "input": {}}])))

    def test_only_the_tail_is_read_and_the_window_grows_once_when_the_reply_is_further_back(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / f"rollout-2026-10-07T10-00-00-{THREAD}.jsonl"
            filler = json.dumps({"type": "event_msg", "payload": {"type": "token_count", "pad": "x" * 2000}})
            body = b"\n".join(rollout_lines(("far back", "final_answer"), complete=False))
            body += b"\n" + "\n".join([filler] * 400).encode() + b"\n"
            path.write_bytes(body)
            self.assertGreater(path.stat().st_size, 512 * 1024)
            self.assertEqual(read_reply("codex", path)["text"], "far back")

    def test_claude_project_names_the_folder_the_way_claude_does(self):
        self.assertEqual(claude_project("/home/user/my.repo_2"), "-home-user-my-repo-2")


class FakeProc:
    """A scratch `/proc`: processes with a parent, a name, a folder and open files."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        (root / "stat").write_text("cpu 1 2 3\nbtime 1790000000\n")

    def add(self, pid, ppid, name, *, cwd="/", files=(), cmdline=None, ticks=100):
        base = self.root / str(pid)
        (base / "fd").mkdir(parents=True)
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(ticks)]
        (base / "stat").write_text(f"{pid} ({name}) " + " ".join(fields) + "\n")
        (base / "comm").write_text(name + "\n")
        (base / "cmdline").write_bytes(b"\0".join(part.encode() for part in (cmdline or [name])) + b"\0")
        os.symlink(cwd, base / "cwd")
        for number, target in enumerate(files):
            os.symlink(target, base / "fd" / str(number + 3))


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(os.path.realpath(self.temp.name))
        self.home = root / "home"
        self.codex = self.home / ".codex" / "sessions" / "2026" / "10" / "07"
        self.codex.mkdir(parents=True)
        (self.home / ".codex" / "thread-writer-locks").mkdir(parents=True)
        self.claude = self.home / ".claude" / "projects"
        self.claude.mkdir(parents=True)
        self.proc = FakeProc(root / "proc")
        self.table = ProcessTable(root / "proc")

    def tearDown(self):
        self.temp.cleanup()

    def rollout(self, thread=THREAD, *replies):
        path = self.codex / f"rollout-2026-10-07T10-00-00-{thread}.jsonl"
        path.write_bytes(b"\n".join(rollout_lines(*(replies or (("hello", "final_answer"),)))) + b"\n")
        return path

    def test_codex_is_matched_by_the_rollout_open_in_its_process_tree(self):
        path = self.rollout()
        self.proc.add(10, 1, "codex")
        self.proc.add(11, 10, "codex-app-serv", files=[str(path), "/dev/null"])
        match = match_codex([10], self.table, self.home / ".codex" / "sessions")
        self.assertEqual((match.path, match.how), (path, "open_rollout"))

    def test_codex_falls_back_to_its_one_thread_lock_and_refuses_two_rollouts(self):
        path = self.rollout()
        lock = self.home / ".codex" / "thread-writer-locks" / f"{THREAD}.lock"
        lock.write_text("")
        self.proc.add(20, 1, "codex", files=[str(lock)])
        match = match_codex([20], self.table, self.home / ".codex" / "sessions")
        self.assertEqual((match.path, match.how), (path, "thread_lock"))
        other = self.rollout(OTHER)
        self.proc.add(30, 1, "codex", files=[str(path), str(other)])
        self.assertEqual(match_codex([30], self.table, self.home / ".codex" / "sessions").reason, "ambiguous")
        self.proc.add(40, 1, "zsh")
        self.assertEqual(match_codex([40], self.table, self.home / ".codex" / "sessions").reason, "no_match")

    def test_a_rollout_outside_the_sessions_folder_is_never_followed(self):
        stray = Path(self.temp.name) / f"rollout-2026-10-07T10-00-00-{THREAD}.jsonl"
        stray.write_bytes(b"\n".join(rollout_lines(("x", None))))
        self.proc.add(50, 1, "codex", files=[str(stray)])
        self.assertEqual(match_codex([50], self.table, self.home / ".codex" / "sessions").reason, "no_match")

    def test_claude_is_matched_by_its_folder_when_it_is_the_only_claude_there(self):
        cwd = "/home/user/h11test"
        project = self.claude / claude_project(cwd)
        project.mkdir()
        old = project / f"{OTHER}.jsonl"
        old.write_bytes(b"\n".join(claude_lines([{"type": "text", "text": "old"}])))
        os.utime(old, (1789000000, 1789000000))      # before the process started
        log = project / f"{SESSION_ID}.jsonl"
        log.write_bytes(b"\n".join(claude_lines([{"type": "text", "text": "new"}])))
        self.proc.add(60, 1, "claude", cwd=cwd)
        match = match_claude([60], self.table, self.claude)
        self.assertEqual((match.path, match.how), (log, "project_folder"))
        # A second claude in the same folder: whose log is whose is unknowable.
        self.proc.add(70, 1, "claude", cwd=cwd)
        self.assertEqual(match_claude([60], self.table, self.claude).reason, "ambiguous")

    def test_claude_resume_on_the_command_line_names_the_log_while_it_is_the_newest(self):
        cwd = "/home/user/repo"
        project = self.claude / claude_project(cwd)
        project.mkdir()
        log = project / f"{SESSION_ID}.jsonl"
        log.write_bytes(b"\n".join(claude_lines([{"type": "text", "text": "resumed"}])))
        self.proc.add(80, 1, "claude", cwd=cwd, cmdline=["claude", "--resume", SESSION_ID])
        self.proc.add(81, 1, "claude", cwd=cwd)
        self.assertEqual(match_claude([80], self.table, self.claude).how, "command_line")

    def test_the_replies_service_caches_the_match_and_the_parse(self):
        path = self.rollout(THREAD, ("第一版", "final_answer"))
        self.proc.add(90, 1, "codex", files=[str(path)])
        clock = [100.0]
        replies = AgentReplies(home=self.home, table=self.table, clock=lambda: clock[0])
        info = {"foreground_processes": [{"pid": 90, "name": "codex"}]}
        first = replies.reply(session="h11-test", pane="w1:p1", agent="codex", process_info=info)
        self.assertEqual((first["available"], first["agent"], first["text"]), (True, "codex", "第一版"))
        with mock.patch("omodachi_core.herdr_reply.match_codex", side_effect=AssertionError("matched twice")):
            again = replies.reply(session="h11-test", pane="w1:p1", agent="codex", process_info=info)
        self.assertEqual(etag(again), etag(first))
        path.write_bytes(b"\n".join(rollout_lines(("第二版", "final_answer"))) + b"\n")
        self.assertEqual(replies.reply(session="h11-test", pane="w1:p1", agent="codex",
                                       process_info=info)["text"], "第二版")
        self.assertNotEqual(etag(replies.reply(session="h11-test", pane="w1:p1", agent="codex",
                                               process_info=info)), etag(first))

    def test_nothing_found_is_remembered_for_a_second_only(self):
        # codex opens its rollout with the first prompt: the pane that had no
        # log a moment ago has one now.
        self.proc.add(96, 1, "codex")
        clock = [100.0]
        replies = AgentReplies(home=self.home, table=self.table, clock=lambda: clock[0])
        info = {"foreground_processes": [{"pid": 96}]}
        self.assertFalse(replies.reply(session="s", pane="w1:p1", agent="codex", process_info=info)["available"])
        path = self.rollout(THREAD, ("now there is one", "final_answer"))
        os.symlink(str(path), Path(self.temp.name) / "proc" / "96" / "fd" / "9")
        clock[0] += 1.5
        self.assertEqual(replies.reply(session="s", pane="w1:p1", agent="codex", process_info=info)["text"],
                         "now there is one")

    def test_anything_unmatched_is_unavailable_with_a_reason_and_no_words(self):
        replies = AgentReplies(home=self.home, table=self.table)
        for agent, info, reason in (("vim", {}, "not_an_agent"), (None, {}, "not_an_agent"),
                                    ("codex", {}, "no_match"), ("claude", None, "no_match")):
            reply = replies.reply(session="s", pane="w1:p1", agent=agent, process_info=info)
            self.assertEqual((reply["available"], reply["reason"], reply["text"]), (False, reason, None))

    def test_herdrs_own_agent_session_is_believed_first(self):
        path = self.rollout()
        replies = AgentReplies(home=self.home, table=self.table)
        reply = replies.reply(session="s", pane="w1:p1", agent="codex", process_info={},
                              agent_session={"agent": "codex", "kind": "id", "value": THREAD, "source": "hook"})
        self.assertEqual(reply["text"], "hello")
        outside = replies.reply(session="s", pane="w1:p1", agent="codex", process_info={},
                                agent_session={"agent": "codex", "kind": "path", "value": "/etc/passwd"})
        self.assertFalse(outside["available"])

    def test_a_long_reply_is_cut_and_says_so(self):
        path = self.rollout(THREAD, ("字" * 40000, "final_answer"))
        self.proc.add(95, 1, "codex", files=[str(path)])
        reply = AgentReplies(home=self.home, table=self.table).reply(
            session="s", pane="w1:p1", agent="codex", process_info={"foreground_processes": [{"pid": 95}]})
        self.assertTrue(reply["truncated"])
        self.assertLessEqual(len(reply["text"]), 32 * 1024 + 2)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ours = Path(self.temp.name) / "omodachi"
        self.collie = Path(self.temp.name) / "collie"
        self.ours.mkdir(); self.collie.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_without_a_file_codex_and_claude_are_offered_only_when_installed(self):
        installed = {"codex"}
        launchers = Launchers(self.ours, self.collie, which=lambda name, path=None: name in installed and "/x/" + name)
        self.assertEqual(launchers.rows(), [{"command": "codex", "label": "codex"}])
        self.assertIsNone(launchers.allowed("claude"))
        self.assertIsNotNone(launchers.allowed("codex"))
        self.assertIsNone(launchers.allowed("codex "))

    def test_the_operators_file_is_the_whole_allowlist_and_bad_rows_are_dropped(self):
        (self.collie / "launchers.toml").write_text(
            '[[launchers]]\ncommand = "codex --profile fast"\nlabel = "codex fast"\n\n'
            '[[launchers]]\ncommand = "claude"\ncwd = "~/x"\n\n'
            '[[launchers]]\ncommand = "rm\\u0007"\n\n'
            '[[launchers]]\ncommand = "-x"\n\n'
            '[[launchers]]\ncommand = "  spaced  "\n')
        launchers = Launchers(self.ours, self.collie, which=lambda name, path=None: "/x/" + name)
        self.assertEqual(launchers.rows(), [{"command": "codex --profile fast", "label": "codex fast"},
                                            {"command": "claude", "label": "claude"}])
        self.assertIsNone(launchers.allowed("codex"))     # not a row of the file
        (self.ours / "launchers.toml").write_text("")        # ours wins, and says: none
        self.assertEqual(launchers.rows(), [])


class EventWatchTests(unittest.TestCase):
    def test_the_subscription_asks_for_structure_and_each_panes_status(self):
        request = subscription(["w1:p2", "w1:p1"])
        rows = request["params"]["subscriptions"]
        self.assertEqual(request["method"], "events.subscribe")
        self.assertIn({"type": "pane.agent_status_changed", "pane_id": "w1:p1"}, rows)
        self.assertIn({"type": "pane.created"}, rows)
        self.assertNotIn({"type": "pane.updated"}, rows)
        self.assertEqual(len(subscription([f"w1:p{n}" for n in range(200)])["params"]["subscriptions"]),
                         len(rows) - 2 + 64)

    def serve(self, lines, *, panes=("w1:p1",)):
        """One `serve_once` against a socketpair standing in for Herdr."""
        ours, theirs = socket.socketpair()
        seen = []
        watch = HerdrEventWatch("h11-test", "/nowhere", lambda session, event: seen.append((session, event)),
                                connect=lambda path: ours)
        watch.set_panes(panes)
        received = []

        def herdr():
            data = b""
            while b"\n" not in data:
                data += theirs.recv(65536)
            received.append(json.loads(data.split(b"\n")[0]))
            for line in lines:
                theirs.sendall(line + b"\n")
            theirs.close()
        thread = threading.Thread(target=herdr)
        thread.start()
        started = watch.serve_once()
        thread.join(5)
        return watch, started, seen, received

    def test_an_acknowledged_subscription_pokes_on_every_event(self):
        watch, started, seen, received = self.serve([
            b'{"id":"x","result":{"type":"subscription_started"}}',
            b'{"event":"pane.agent_status_changed","data":{"pane_id":"w1:p1","workspace_id":"w1","agent_status":"blocked"}}',
            b'{"event":"pane.created","data":{"type":"pane_created"}}', b"garbage"])
        self.assertTrue(started)
        self.assertEqual(seen, [("h11-test", "pane.agent_status_changed"), ("h11-test", "pane.created")])
        self.assertIn({"type": "pane.agent_status_changed", "pane_id": "w1:p1"},
                      received[0]["params"]["subscriptions"])
        self.assertFalse(watch.healthy)   # the connection ended

    def test_a_herdr_that_refuses_the_method_is_left_to_the_poll(self):
        watch, started, seen, _ = self.serve([b'{"id":"x","error":{"code":"unknown_method","message":"no"}}'])
        self.assertFalse(started)
        self.assertTrue(watch.unsupported)
        self.assertEqual(seen, [])

    def test_watches_are_kept_per_session_and_only_for_protocol_20_and_later(self):
        made = []

        class Fake:
            def __init__(self, session, path, handler):
                self.session, self.socket_path, self.healthy, self.unsupported = session, str(path), True, False
                self.panes, self.stopped = None, False
            def set_panes(self, panes): self.panes = sorted(panes)
            def stop(self): self.stopped = True
        watches = HerdrEventWatches(lambda s, e: None,
                                    factory=lambda s, p, h: made.append(Fake(s, p, h)) or made[-1])
        layout = {"protocol": 20, "workspaces": [{"tabs": [{"panes": [{"id": "w1:p1"}, {"id": "w1:p2"}]}]}]}
        watches.ensure("a", "/s/a.sock", layout)
        watches.ensure("a", "/s/a.sock", layout)
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0].panes, ["w1:p1", "w1:p2"])
        self.assertTrue(watches.healthy("a"))
        self.assertIsNone(watches.ensure("old", "/s/o.sock", {**layout, "protocol": 19}))
        watches.forget("a")
        self.assertTrue(made[0].stopped)
        self.assertFalse(watches.healthy("a"))


class CadenceTests(unittest.TestCase):
    """§4: watched sessions every two seconds, the rest every ten, a poked one at once."""

    def setUp(self):
        from omodachi_core.bootstrap import create_service
        from omodachi_core.hub import Hub
        self.service = create_service(Hub(), demo=True)
        self.service.herdr_bridge = HerdrBridge("omodachi", runner=lambda argv: {})
        self.service.herdr_choices = HerdrSessionChoices()

    def test_due(self):
        service = self.service
        service._herdr_read_at = {"omodachi": 100.0, "work": 100.0}
        watched = service._herdr_watched()
        self.assertEqual(watched, {"omodachi"})
        self.assertTrue(service._herdr_due("omodachi", 102.0, set(), watched))
        self.assertFalse(service._herdr_due("work", 102.0, set(), watched))
        self.assertTrue(service._herdr_due("work", 110.0, set(), watched))
        self.assertTrue(service._herdr_due("work", 100.5, {"work"}, watched))
        self.assertTrue(service._herdr_due("new", 100.5, set(), watched))
        # A device chose `work`, or has a pane of it on screen: watched.
        service.herdr_choices.choose("work", "phone")
        self.assertIn("work", service._herdr_watched())
        service.herdr_choices.forget("phone")
        service.seen_watch("herdr:work:w1:p1", +1)
        self.assertIn("work", service._herdr_watched())
        service.seen_watch("herdr:work:w1:p1", -1)
        self.assertNotIn("work", service._herdr_watched())

        # Herdr's events up for the owned session: it is only reconciled.
        class Healthy:
            def healthy(self, session): return session == "omodachi"
        service.herdr_events = Healthy()
        self.assertFalse(service._herdr_due("omodachi", 102.0, set(), watched))
        self.assertTrue(service._herdr_due("omodachi", 110.0, set(), watched))


class ApiTests(test_herdr6.WorkspaceApiTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.typed = []
        self.screen = ["", "$ "]

        def request(method, params):
            self.typed.append((method, params))
            if method == "pane.read":
                return {"read": {"text": self.screen.pop(0) if len(self.screen) > 1 else self.screen[0]}}
            return {}
        for bridge in (self.service.herdr_bridge, *self.service._herdr_bridges.values()):
            bridge.request = request
            bridge.LAUNCH_DELAY_SECONDS = 0
        self.service.herdr_launchers = Launchers(Path("/nonexistent"), Path("/nonexistent"),
                                                 which=lambda name, path=None: "/x/" + name)

    async def test_a_workspace_starts_with_an_allowed_launcher_typed_into_its_new_pane(self):
        status, body = await self.post("/v1/herdr/sessions/h6test/workspaces", {"cwd": "~/code", "launch": "codex"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["launch"], {"command": "codex", "pane": "w2:p1", "sent": True, "drawn": True,
                                          "reason": None})
        writes = [(method, params) for method, params in self.typed if method != "pane.read"]
        self.assertEqual(writes, [("pane.send_text", {"pane_id": "w2:p1", "text": "codex"}),
                                  ("pane.send_keys", {"pane_id": "w2:p1", "keys": ["Enter"]})])
        line = self.service.herdr_audit.lines[-1]
        self.assertEqual((line["kind"], line["pane"], line["length"], line["outcome"]), ("launch", "w2:p1", 5, "ok"))
        self.assertNotIn("codex", json.dumps(line))

    async def test_a_command_that_is_not_a_launcher_is_refused_before_anything_is_made(self):
        for launch in ("bash -c 'rm -rf ~'", "codex ", 7, ""):
            status, body = await self.post("/v1/herdr/sessions/h6test/workspaces", {"cwd": "~", "launch": launch})
            self.assertEqual((status, body["error"]["code"]), (400, "herdr_launch_not_allowed"), launch)
        self.assertEqual(self.created, [])
        self.assertEqual([m for m, _ in self.typed if m != "pane.read"], [])

    async def test_an_empty_folder_is_the_home_folder_and_no_launch_is_a_plain_shell(self):
        for payload in ({}, {"cwd": ""}, {"cwd": "  "}):
            status, body = await self.post("/v1/herdr/sessions/h6test/workspaces", payload)
            self.assertEqual(status, 200, body)
            self.assertEqual(self.created[-1][1][6], str(self.home))
            self.assertNotIn("launch", body)
        self.assertEqual([m for m, _ in self.typed if m != "pane.read"], [])

    async def test_folders_carry_favourites_and_launchers_and_a_star_is_kept(self):
        status, body = await self.get("/v1/herdr/folders")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["favourites"], [])
        self.assertEqual(body["launchers"], [{"command": "codex", "label": "codex"},
                                             {"command": "claude", "label": "claude"}])
        status, body = await self.post("/v1/herdr/folders/favourites", {"path": "~/code", "favourite": True})
        self.assertEqual(status, 200, body)
        self.assertEqual([row["display"] for row in body["favourites"]], ["~/code"])
        status, body = await self.post("/v1/herdr/folders/favourites", {"path": "~", "favourite": True})
        self.assertEqual([row["display"] for row in body["favourites"]], ["~/code", "~"])
        status, body = await self.post("/v1/herdr/folders/favourites", {"path": "~/code", "favourite": False})
        self.assertEqual([row["display"] for row in body["favourites"]], ["~"])
        for payload, code in (({"path": "~/.ssh", "favourite": True}, "folder_private"),
                              ({"path": "/etc", "favourite": True}, "folder_outside_home"),
                              ({"path": "~", "favourite": "yes"}, "invalid_request"),
                              ({"path": "~"}, "invalid_request")):
            status, body = await self.post("/v1/herdr/folders/favourites", payload)
            self.assertEqual((status, body["error"]["code"]), (400, code), payload)
        status, body = await self.get("/v1/herdr/folders?under=~")
        self.assertEqual((body["favourites"], body["launchers"]), (None, None))
        self.assertTrue(browser_gate.web_allows("POST", "/v1/herdr/folders/favourites"))
        self.assertTrue(browser_gate.web_allows("GET", "/v1/herdr/panes/w1:p1/reply"))


class ReplyApiTests(test_herdr_bridge.HerdrApiTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        snapshot = copy.deepcopy(test_herdr_bridge.SNAPSHOT)["snapshot"]
        self.snapshot = snapshot
        self.bridge.runner = lambda argv: self.calls.append(argv) or {"snapshot": snapshot}
        self.panes = [row["pane_id"] for row in snapshot.get("panes") or []]
        self.replies = mock.Mock()
        self.replies.reply.return_value = {"available": True, "reason": None, "agent": "codex",
                                           "text": "你好", "at": "2026-10-07T10:00:00Z", "complete": True,
                                           "phase": "final_answer", "truncated": False}
        self.replies.unavailable = AgentReplies().unavailable
        self.service.herdr_replies = self.replies
        self.bridge.request = lambda method, params: {"process_info": {"foreground_processes": [{"pid": 1}]}}

    async def test_the_reply_route_answers_304_to_the_etag_it_gave(self):
        agents = {row["pane_id"]: row for row in self.snapshot.get("agents") or []}
        pane = next(iter(agents), None)
        if pane is None:
            self.skipTest("the recorded snapshot has no agent")
        async with self.client.get(self.url + f"/v1/herdr/panes/{pane}/reply", headers=self.headers()) as response:
            self.assertEqual(response.status, 200, await response.text())
            body = await response.json()
            tag = response.headers["ETag"]
        self.assertEqual((body["available"], body["text"], body["pane"]), (True, "你好", pane))
        headers = {**self.headers(), "If-None-Match": tag}
        async with self.client.get(self.url + f"/v1/herdr/panes/{pane}/reply", headers=headers) as response:
            self.assertEqual(response.status, 304)
        async with self.client.get(self.url + "/v1/herdr/panes/w9:p9/reply", headers=self.headers()) as response:
            self.assertEqual(response.status, 404)
        async with self.client.get(self.url + "/v1/herdr/panes/nope/reply", headers=self.headers()) as response:
            self.assertEqual(response.status, 400)

    async def test_a_pane_without_an_agent_has_no_reply_and_no_error(self):
        agents = {row.get("pane_id") for row in self.snapshot.get("agents") or []}
        plain = next((pane for pane in self.panes if pane not in agents), None)
        if plain is None:
            self.skipTest("the recorded snapshot has no plain pane")
        async with self.client.get(self.url + f"/v1/herdr/panes/{plain}/reply", headers=self.headers()) as response:
            self.assertEqual(response.status, 200)
            body = await response.json()
        self.assertEqual((body["available"], body["reason"], body["text"]), (False, "not_an_agent", None))
        self.replies.reply.assert_not_called()

    async def test_the_layout_is_served_from_the_poll_and_answers_304(self):
        self.bridge.layout()
        before = len(self.calls)
        async with self.client.get(self.url + "/v1/herdr/layout", headers=self.headers()) as response:
            self.assertEqual(response.status, 200)
            tag = response.headers["ETag"]
            revision = (await response.json())["revision"]
        self.assertEqual(len(self.calls), before)          # no second `api snapshot`
        self.assertEqual(tag, f'"omodachi:{revision}"')
        async with self.client.get(self.url + "/v1/herdr/layout",
                                   headers={**self.headers(), "If-None-Match": tag}) as response:
            self.assertEqual(response.status, 304)


class StateTests(test_herdr_bridge.HerdrApiTests):
    async def test_a_client_holding_the_catalog_is_told_unchanged_and_the_rest_is_gzipped(self):
        async with self.client.get(self.url + "/v1/state", headers=self.headers()) as response:
            self.assertEqual(response.status, 200)
            full = await response.read()
            state = json.loads(full)
        revision = state["catalog"]["revision"]
        self.assertTrue(state["catalog"]["entries"])
        headers = {**self.headers(), "X-Omodachi-Catalog": revision, "Accept-Encoding": "gzip"}
        async with self.client.get(self.url + "/v1/state", headers=headers, auto_decompress=False) as response:
            self.assertEqual(response.status, 200)
            raw = await response.read()
            encoding = response.headers.get("Content-Encoding")
            tag = response.headers["ETag"]
        light = json.loads(gzip.decompress(raw) if encoding == "gzip" else raw)
        self.assertEqual(light["catalog"]["entries"], [])
        self.assertIs(light["catalog"]["unchanged"], True)
        self.assertEqual(light["catalog"]["revision"], revision)
        self.assertLess(len(raw), len(full) / 4)
        async with self.client.get(self.url + "/v1/state", headers={**headers, "If-None-Match": tag}) as response:
            self.assertEqual(response.status, 304)
        # A stale revision gets the rows again; so does a client that names none.
        async with self.client.get(self.url + "/v1/state",
                                   headers={**self.headers(), "X-Omodachi-Catalog": "old"}) as response:
            self.assertTrue((await response.json())["catalog"]["entries"])

    async def test_a_large_read_is_gzipped_only_for_a_client_that_takes_it(self):
        async with self.client.get(self.url + "/v1/catalog", headers={**self.headers(), "Accept-Encoding": "gzip"},
                                   auto_decompress=False) as response:
            self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
        async with self.client.get(self.url + "/v1/catalog", headers={**self.headers(), "Accept-Encoding": "identity"},
                                   auto_decompress=False) as response:
            self.assertIsNone(response.headers.get("Content-Encoding"))
            json.loads(await response.read())


if __name__ == "__main__":
    unittest.main()
