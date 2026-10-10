"""OVERVIEW-1: one "Needs you" list across the Agent and every Herdr pane.

* reading a blocked pane's dialog off its screen (the two shapes HERDR-9's
  strip answers, nothing else), and a finished agent's closing question;
* the projection: three states, the asks, position order, the default
  agent's own pane, counts before the cap;
* the service: every running session, one screen read per pane revision,
  `overview.changed` once per change, the state's first look, a finished pane
  leaving the list when a device opens it, the Agent's own events.
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from omodachi_core.bootstrap import create_service
from omodachi_core.herdr_bridge import HerdrBridge, HerdrSessions, HerdrUnavailable
from omodachi_core.hub import Hub
from omodachi_core import overview as overview_module
from omodachi_core.overview import OverviewProjector, read_ask, reply_question, clean_text
from omodachi_core.seen import SeenLedger
from omodachi_core import service as service_module

ROOT = Path(__file__).parents[1]
SCREENS = ROOT / "odomo/tests/fixtures"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def screen(name: str) -> str:
    """A recorded screen as `pane.read visible` returns it: text, no styling."""
    folder = "herdr10" if name.startswith("herdr10") else "herdr9"
    return ANSI.sub("", (SCREENS / folder / name).read_text())


class ReadAskTests(unittest.TestCase):
    def test_codex_and_claude_dialogs_read_as_a_pick_with_their_question(self):
        cases = {
            "collie--codex--v0156-approval-exec-wrapped-50.txt": "Would you like to run the following command?",
            "collie--claude--permission-bash.txt": "Do you want to proceed?",
            "collie--claude--v2283-permission-amend-focused.txt": "Do you want to proceed?",
            "collie--claude-lab--permission-webfetch--w82.txt": "Do you want to allow Claude to fetch this content?",
            "collie--claude--select-menu.txt": "Which color theme should the dashboard use?",
            "collie--codex--ask-notes-focused.txt": "Tabs or spaces?",
            "herdr10-narrow-ask-q1.visible.txt": "你更喜欢哪种回答风格？",
            "herdr10-narrow-ask-q2.visible.txt": "回答要多详细？",
        }
        for name, question in cases.items():
            with self.subTest(name):
                self.assertEqual(read_ask(screen(name)), {"ask": "pick", "question": question})

    def test_the_wrapped_tail_of_codexs_reason_is_not_the_question(self):
        # "…to create the / requested file?" ends in a question mark and is
        # nearer the options than the dialog's own title.
        text = screen("collie--codex--v0156-approval-exec-wrapped-50.txt")
        self.assertIn("requested file?", text)
        self.assertEqual(read_ask(text)["question"], "Would you like to run the following command?")
        one_line = "\n".join(["  Would you like to run the following command?", "",
                              "  Reason: May I run make to build it?", "", "  $ make", "",
                              "› 1. Yes, proceed (y)", "  2. No (esc)", "", "  Press enter to confirm"])
        self.assertEqual(read_ask(one_line)["question"], "Would you like to run the following command?")

    def test_what_is_not_a_dialog_reads_as_nothing(self):
        for name in ("collie--claude--trust-prompt-unnumbered.txt",          # no numbers: not read, "waiting"
                     "collie--claude-lab--transcript-dialog-lookalike--w82.txt",
                     "herdr10-codex-answer-mixed.visible.txt", "herdr10-narrow-answer.visible.txt"):
            with self.subTest(name):
                self.assertIsNone(read_ask(screen(name)))
        numbered_answer = "Here is the plan:\n1. Read the file\n2. Fix the bug\n3. Run the tests\n› "
        self.assertIsNone(read_ask(numbered_answer), "a list with no marked option is an answer, not a menu")
        two_marked = "Pick one?\n› 1. A\n› 2. B\n"
        self.assertIsNone(read_ask(two_marked))
        self.assertIsNone(read_ask(""))
        self.assertIsNone(read_ask(None))

    def test_a_marked_list_with_no_question_above_is_a_pick_without_words(self):
        self.assertEqual(read_ask("Select a model\n› 1. fast\n  2. deep\n"), {"ask": "pick", "question": None})

    def test_a_yes_no_question_on_the_last_rows(self):
        self.assertEqual(read_ask("$ rm -i notes.txt\nremove notes.txt? (y/n) "),
                         {"ask": "yes_no", "question": "remove notes.txt?"})
        self.assertEqual(read_ask("Overwrite the config? [Y/n]\n"),
                         {"ask": "yes_no", "question": "Overwrite the config?"})
        self.assertEqual(read_ask("Continue [y/N]:"), {"ask": "yes_no", "question": "Continue"})

    def test_a_long_question_is_one_cut_line(self):
        long = "Do you want " + "very " * 60 + "much to proceed?"
        read = read_ask(long + "\n› 1. Yes\n  2. No\n")
        self.assertLessEqual(len(read["question"]), 120)
        self.assertTrue(read["question"].endswith("…"))
        self.assertEqual(clean_text("a\x1b[31m\tb  **c**"), "a [31m b c")


class ReplyQuestionTests(unittest.TestCase):
    def test_the_last_sentence_when_the_reply_ends_on_a_question(self):
        self.assertEqual(reply_question("I sorted the notes.\n\nShould I also archive the old ones?"),
                         "Should I also archive the old ones?")
        self.assertEqual(reply_question("Fixed it. Want me to open a PR?"), "Want me to open a PR?")
        self.assertEqual(reply_question("改好了。要我顺便提交吗？"), "要我顺便提交吗？")
        self.assertEqual(reply_question("**Next:** shall I run the tests?"), "Next: shall I run the tests?")

    def test_anything_else_is_none(self):
        for text in ("All done.", "Is it? It is done.", "", None, "\n\n"):
            with self.subTest(text):
                self.assertIsNone(reply_question(text))


def layout(*workspaces):
    """`layout(("w1", "api", [("w1:p1", "blocked", "codex"), …]), …)` in the bridge's projection shape."""
    out = []
    for ident, label, panes in workspaces:
        rows = []
        for pane, status, kind in panes:
            rows.append({"id": pane, "title": None, "cwd": f"/home/fixture/{label}", "agent_status": status,
                         "revision": 1,
                         "agent": {"name": "default", "kind": kind, "status": status} if kind else None})
        out.append({"id": ident, "label": label, "number": int(ident[1:]), "tabs": [{"panes": rows}]})
    return {"workspaces": out}


class ProjectorTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.projector = OverviewProjector(clock=lambda: self.now[0])

    def states(self, value):
        return [(row["id"], row["state"], row["ask"]) for row in value["rows"]]

    def test_three_states_in_position_order_never_status_order(self):
        self.projector.set_session("work", layout(
            ("w1", "api", [("w1:p1", "working", "claude"), ("w1:p2", "blocked", "codex")]),
            ("w2", "site", [("w2:p1", "idle", "codex"), ("w2:p2", "unknown", None)])))
        self.projector.set_session("debug", layout(("w1", "x", [("w1:p1", "blocked", "codex")])))
        unseen = {"agent": False, "herdr": [{"session": "work", "pane_id": "w2:p1", "name": "codex"}]}
        value = self.projector.value(unseen=unseen)
        self.assertEqual(self.states(value), [
            ("herdr:debug:w1:p1", "needs_you", "waiting"),
            ("herdr:work:w1:p1", "working", "working"),
            ("herdr:work:w1:p2", "needs_you", "waiting"),
            ("herdr:work:w2:p1", "done", "finished")])
        self.assertEqual(value["counts"], {"needs_you": 2, "working": 1, "done": 1})
        self.assertEqual(value["sessions"], [{"name": "debug", "needs_you": 1, "working": 0, "done": 0},
                                             {"name": "work", "needs_you": 1, "working": 1, "done": 1}])
        row = value["rows"][2]
        self.assertEqual((row["workspace_id"], row["workspace"], row["pane"], row["agent"]), ("w1", "api", "api", "codex"))

    def test_an_idle_pane_nobody_missed_and_a_shell_are_not_listed(self):
        self.projector.set_session("work", layout(("w1", "api", [("w1:p1", "idle", "codex"), ("w1:p2", "unknown", None)])))
        value = self.projector.value(unseen={"agent": False, "herdr": []})
        self.assertEqual(value["rows"], [])
        self.assertEqual(value["sessions"], [{"name": "work", "needs_you": 0, "working": 0, "done": 0}])

    def test_the_default_agents_own_pane_is_the_agent_row(self):
        self.projector.set_session("omodachi", layout(("w1", "~", [("w1:p1", "blocked", "codex"), ("w1:p2", "blocked", "claude")])))
        self.projector.set_session("work", layout(("w1", "~", [("w1:p1", "blocked", "codex")])))
        value = self.projector.value(default_pane="w1:p1")
        self.assertEqual([row["id"] for row in value["rows"]], ["herdr:omodachi:w1:p2", "herdr:work:w1:p1"])
        self.assertEqual(self.projector.blocked_needing_a_read(default_pane="w1:p1"),
                         [("omodachi", "w1:p2"), ("work", "w1:p1")])

    def test_the_agents_asks(self):
        def agent(status, kind=None, waiting=None, phase=None):
            chat = {"status": {"waiting": waiting}, "pending_approvals": 1 if kind else 0,
                    "approval": {"request_id": "r1", "kind": kind, "fingerprint": "0" * 16,
                                 "decidable": kind != "userInput"} if kind else None,
                    "activity": {"phase": phase, "turn_id": "t"} if phase else None}
            return {"kind": "codex", "status": status, "chat": chat}
        cases = [
            (agent("waiting_on_approval", "commandExecution", "approval"), ("needs_you", "approve_command")),
            (agent("waiting_on_approval", "fileChange", "approval"), ("needs_you", "approve_files")),
            (agent("waiting_on_approval", "permissions", "approval"), ("needs_you", "approve_permissions")),
            (agent("waiting_on_user_input", "userInput", "user_input"), ("needs_you", "answer")),
            (agent("waiting_on_user_input", None, "user_input"), ("needs_you", "answer")),
            (agent("waiting_on_approval"), ("needs_you", "approve")),
            (agent("working", phase="tool"), ("working", "working")),
            (agent("working", phase="thinking"), ("working", "working")),
        ]
        for value, expected in cases:
            with self.subTest(expected):
                row = self.projector.value(agent=value)["rows"][0]
                self.assertEqual((row["id"], row["source"], row["state"], row["ask"], row["agent"]),
                                 ("agent", "agent", *expected, "codex"))
        idle = agent("idle", phase="done")
        self.assertEqual(self.projector.value(agent=idle)["rows"], [])
        done = self.projector.value(agent=idle, unseen={"agent": True, "herdr": []})["rows"][0]
        self.assertEqual((done["state"], done["ask"]), ("done", "finished"))
        failed = self.projector.value(agent=agent("idle", phase="error"), unseen={"agent": True, "herdr": []})
        self.assertEqual(failed["rows"][0]["ask"], "failed")
        # An approval wins over "finished and not seen".
        waiting = self.projector.value(agent=agent("waiting_on_approval", "fileChange", "approval"),
                                       unseen={"agent": True, "herdr": []})
        self.assertEqual(waiting["rows"][0]["state"], "needs_you")
        self.assertEqual(waiting["counts"], {"needs_you": 1, "working": 0, "done": 0})
        self.assertEqual(waiting["sessions"], [], "the Agent is not a session")
        self.assertEqual(self.projector.value(agent=None)["rows"], [])

    def test_a_blocked_panes_screen_is_read_once_per_revision(self):
        self.projector.set_session("work", layout(("w1", "api", [("w1:p1", "blocked", "codex")])))
        self.assertEqual(self.projector.blocked_needing_a_read(), [("work", "w1:p1")])
        self.projector.note_screen("work", "w1:p1", "Proceed?\n› 1. Yes\n  2. No\n")
        self.assertEqual(self.projector.blocked_needing_a_read(), [])
        row = self.projector.value()["rows"][0]
        self.assertEqual((row["ask"], row["question"]), ("pick", "Proceed?"))
        moved = layout(("w1", "api", [("w1:p1", "blocked", "codex")]))
        moved["workspaces"][0]["tabs"][0]["panes"][0]["revision"] = 2
        self.projector.set_session("work", moved)
        self.assertEqual(self.projector.blocked_needing_a_read(), [], "a moving revision is not read every second")
        self.now[0] += overview_module.SCREEN_REREAD_SECONDS
        self.assertEqual(self.projector.blocked_needing_a_read(), [("work", "w1:p1")])
        self.projector.note_screen("work", "w1:p1", None)                      # the read failed
        self.assertEqual(self.projector.value()["rows"][0]["ask"], "waiting")
        self.assertEqual(self.projector.blocked_needing_a_read(), [], "a failed read is not retried every poll")

    def test_a_finished_panes_closing_question_and_its_retries(self):
        self.projector.set_session("work", layout(("w1", "api", [("w1:p1", "idle", "codex")])))
        unseen = [{"session": "work", "pane_id": "w1:p1", "name": "codex"}]
        self.assertEqual(self.projector.unread_moves(unseen), [("work", "w1:p1")])
        self.projector.note_reply("work", "w1:p1", None)                       # not written yet
        self.assertEqual(self.projector.unread_moves(unseen), [], "not again within two seconds")
        self.now[0] += 2.5
        self.assertEqual(self.projector.unread_moves(unseen), [("work", "w1:p1")])
        self.projector.note_reply("work", "w1:p1", "Done.\nShall I push it?")
        row = self.projector.value(unseen={"agent": False, "herdr": unseen})["rows"][0]
        self.assertEqual((row["state"], row["ask"], row["question"]), ("done", "your_move", "Shall I push it?"))
        self.now[0] += 30
        self.assertEqual(self.projector.unread_moves(unseen), [], "a question once read is kept")
        self.assertEqual(self.projector.unread_moves([]), [])
        self.assertEqual(self.projector.unread_moves(unseen), [("work", "w1:p1")], "seen, then finished again: read again")

    def test_counts_are_taken_before_the_rows_are_cut(self):
        panes = [(f"w1:p{index}", "working", "codex") for index in range(80)]
        self.projector.set_session("big", layout(("w1", "api", panes)))
        value = self.projector.value()
        self.assertEqual(len(value["rows"]), overview_module.ROW_LIMIT)
        self.assertEqual(value["counts"]["working"], 80)
        self.assertEqual(value["sessions"][0]["working"], 80)

    def test_a_session_that_stops_leaves_with_its_reads(self):
        self.projector.set_session("work", layout(("w1", "api", [("w1:p1", "blocked", "codex")])))
        self.projector.note_screen("work", "w1:p1", "Go?\n› 1. Yes\n  2. No")
        self.projector.forget_session("work")
        self.assertEqual(self.projector.value()["rows"], [])
        self.assertEqual(self.projector.value()["sessions"], [])


def herdr_snapshot(panes):
    """A 0.8.2 `api snapshot` with these panes: `[(pane_id, status, kind or None), …]`, one workspace."""
    agents, rows = [], []
    for index, (pane, status, kind) in enumerate(panes):
        rows.append({"agent_status": status, "cwd": f"/home/user/p{index}", "focused": index == 0,
                     "pane_id": pane, "revision": 1, "tab_id": "w1:t1", "workspace_id": "w1",
                     "terminal_title": None})
        if kind:
            agents.append({"agent": kind, "agent_status": status, "pane_id": pane, "tab_id": "w1:t1",
                           "workspace_id": "w1"})
    return {"snapshot": {"agents": agents, "panes": rows, "layouts": [], "protocol": 20, "version": "0.8.2",
                         "tabs": [{"tab_id": "w1:t1", "workspace_id": "w1", "label": "1", "number": 1}],
                         "workspaces": [{"workspace_id": "w1", "label": "api", "number": 1}]}}


class ServiceTests(unittest.TestCase):
    """The poll feeds the list; the list is published once per change."""

    def setUp(self):
        self.hub = Hub()
        self.service = create_service(self.hub, demo=True)
        self.snapshots = {"omodachi": herdr_snapshot([]),
                          "work": herdr_snapshot([("w1:p1", "working", "codex"), ("w1:p2", "unknown", None)])}
        self.listing = {"sessions": [{"name": "omodachi", "running": True, "default": False},
                                     {"name": "work", "running": True, "default": True}]}
        self.service._herdr_sessions = HerdrSessions(runner=lambda argv: self.listing)
        self.service.herdr_bridge = self.bridge("omodachi")
        self.screens, self.reads = {}, []
        def read(bridge, pane):
            self.reads.append((bridge.session, pane))
            value = self.screens.get((bridge.session, pane))
            if isinstance(value, Exception):
                raise value
            return value or ""
        self.service.overview_screen = read
        self.replies = {}
        self.service.overview_reply = lambda bridge, pane: self.replies.get((bridge.session, pane))
        self.events = []
        self.hub.add_observer(lambda event: self.events.append((event.type, copy.deepcopy(event.payload))))
        patcher = mock.patch("omodachi_core.herdr_bridge.HerdrBridge",
                             lambda name, socket_path=None: self.bridge(name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = 100.0

    def bridge(self, name):
        def runner(argv):
            value = self.snapshots.get(name)
            if value is None:
                raise HerdrUnavailable("herdr_unavailable")
            return value
        return HerdrBridge(name, runner=runner)

    def poll(self, advance=10.0):
        self.clock += advance
        with mock.patch.object(service_module.time, "monotonic", return_value=self.clock):
            return self.service.notify_herdr_layout()

    def overview_events(self):
        return [payload["overview"] for kind, payload in self.events if kind == "overview.changed"]

    def rows(self):
        return [(row["id"], row["state"], row["ask"], row["question"])
                for row in self.service.state("phone")["overview"]["rows"]]

    def test_the_state_carries_the_list_before_anything_was_published(self):
        overview = self.service.state("phone")["overview"]
        self.assertEqual(overview, {"rows": [], "counts": {"needs_you": 0, "working": 0, "done": 0}, "sessions": []})
        self.assertEqual(self.overview_events(), [], "nothing to show is never published")

    def test_every_running_session_feeds_the_list_and_it_is_published_once_per_change(self):
        self.poll()
        self.assertEqual(self.rows(), [("herdr:work:w1:p1", "working", "working", None)])
        self.assertEqual(len(self.overview_events()), 1)
        self.poll()
        self.assertEqual(len(self.overview_events()), 1, "nothing moved, nothing published")
        self.snapshots["work"] = herdr_snapshot([("w1:p1", "blocked", "codex"), ("w1:p2", "unknown", None)])
        self.screens[("work", "w1:p1")] = screen("collie--codex--v0156-approval-exec-wrapped-50.txt")
        self.poll()
        self.assertEqual(self.rows(), [("herdr:work:w1:p1", "needs_you", "pick",
                                        "Would you like to run the following command?")])
        self.assertEqual(self.reads, [("work", "w1:p1")], "only the blocked pane's screen is read")
        self.poll()
        self.poll()
        self.assertEqual(self.reads, [("work", "w1:p1")], "once per pane revision")
        last = self.overview_events()[-1]
        self.assertEqual(last["counts"], {"needs_you": 1, "working": 0, "done": 0})
        self.assertEqual(last["sessions"], [{"name": "omodachi", "needs_you": 0, "working": 0, "done": 0},
                                            {"name": "work", "needs_you": 1, "working": 0, "done": 0}])

    def test_a_screen_that_cannot_be_read_says_waiting(self):
        self.snapshots["work"] = herdr_snapshot([("w1:p1", "blocked", "claude")])
        self.screens[("work", "w1:p1")] = HerdrUnavailable("herdr_pane_gone")
        self.poll()
        self.assertEqual(self.rows(), [("herdr:work:w1:p1", "needs_you", "waiting", None)])

    def test_finished_then_opened_on_a_phone_leaves_the_list(self):
        with tempfile.TemporaryDirectory() as root:
            self.service.install_seen(SeenLedger(Path(root) / "seen.json"))
            self.poll()                                                       # working: the baseline
            self.snapshots["work"] = herdr_snapshot([("w1:p1", "idle", "codex")])
            self.replies[("work", "w1:p1")] = "All green.\n\nShould I open the PR?"
            self.poll()
            self.assertEqual(self.rows(), [("herdr:work:w1:p1", "done", "your_move", "Should I open the PR?")])
            self.assertEqual(self.overview_events()[-1]["counts"]["done"], 1)
            # A device opens the pane's stream: seen, and the row is gone.
            self.service.seen_watch(SeenLedger.herdr_key("work", "w1:p1"), +1)
            self.assertEqual(self.rows(), [])
            self.assertEqual(self.overview_events()[-1]["rows"], [])
            self.service.seen_watch(SeenLedger.herdr_key("work", "w1:p1"), -1)

    def test_keys_sent_to_a_finished_pane_clear_it_too(self):
        with tempfile.TemporaryDirectory() as root:
            self.service.install_seen(SeenLedger(Path(root) / "seen.json"))
            self.poll()
            self.snapshots["work"] = herdr_snapshot([("w1:p1", "idle", "codex")])
            self.poll()
            self.assertEqual(self.rows(), [("herdr:work:w1:p1", "done", "finished", None)])
            self.service.seen_mark("work", "w1:p1")
            self.assertEqual(self.rows(), [])

    def test_a_session_that_stops_leaves_the_list(self):
        self.snapshots["work"] = herdr_snapshot([("w1:p1", "blocked", "codex")])
        self.poll()
        self.assertEqual(len(self.rows()), 1)
        self.listing["sessions"][1]["running"] = False
        self.snapshots.pop("work")
        self.poll()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.overview_events()[-1]["sessions"],
                         [{"name": "omodachi", "needs_you": 0, "working": 0, "done": 0}])

    def test_the_agents_own_events_move_its_row(self):
        self.hub.update_state({"agent": {"kind": "codex", "status": "waiting_on_approval", "chat": {
            "status": {"waiting": "approval"}, "pending_approvals": 1,
            "approval": {"request_id": "r1", "kind": "fileChange", "fingerprint": "0" * 16, "decidable": True}}}},
            event_type="agent.changed")
        self.assertEqual(self.rows(), [("agent", "needs_you", "approve_files", None)])
        self.assertEqual(self.overview_events()[-1]["rows"][0]["ask"], "approve_files")
        self.hub.update_state({"agent": {"status": "working", "chat": {
            "status": {"waiting": None}, "pending_approvals": 0, "approval": None,
            "activity": {"phase": "tool", "turn_id": "t1"}}}}, event_type="agent.changed")
        self.assertEqual(self.rows(), [("agent", "working", "working", None)])
        self.hub.update_state({"agent": {"status": "idle", "chat": {"activity": {"phase": "done", "turn_id": "t1"}}}},
                              event_type="agent.changed")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.overview_events()[-1]["rows"], [])

    def test_the_agent_observer_hands_over_to_the_loop(self):
        async def run():
            self.hub.update_state({"agent": {"kind": "codex", "status": "waiting_on_user_input",
                                             "chat": {"status": {"waiting": "user_input"}}}},
                                  event_type="agent.changed")
            self.assertEqual(self.overview_events(), [], "not from inside the hub's own publish")
            await asyncio.sleep(0)
            self.assertEqual(self.overview_events()[-1]["rows"][0]["ask"], "answer")
        asyncio.run(run())

    def test_the_default_agents_pane_is_not_a_second_row(self):
        self.snapshots["omodachi"] = herdr_snapshot([("w1:p1", "blocked", "codex")])
        self.hub.update_state({"agent": {"default_agent": {"pane_id": "w1:p1"}}}, event_type="agent.changed")
        self.poll()
        self.assertEqual([row[0] for row in self.rows()], ["herdr:work:w1:p1"])
        self.assertEqual(self.reads, [], "its screen is the Agent's, never read for the list")

    def test_the_overview_matches_its_contract(self):
        import jsonschema
        schema = json.loads((ROOT / "contracts/state.schema.json").read_text())["properties"]["overview"]
        self.snapshots["work"] = herdr_snapshot([("w1:p1", "blocked", "codex"), ("w1:p2", "working", "claude")])
        self.screens[("work", "w1:p1")] = screen("herdr10-narrow-ask-q1.visible.txt")
        self.poll()
        jsonschema.validate(self.service.state("phone")["overview"], schema)
        for _, payload in self.events:
            if "overview" in payload:
                jsonschema.validate(payload["overview"], schema)


if __name__ == "__main__":
    unittest.main()
