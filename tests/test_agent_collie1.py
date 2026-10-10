"""AGENT-COLLIE-1: the Agent panel ideas borrowed from Collie, on real codex
0.160 frames.

`tests/fixtures/codex-0160-agent-collie1.json` is what a real `codex
app-server` 0.160 sent (a private CODEX_HOME and a scripted fake model on the
Mac, never a user's daemon; paths and the user name redacted): every
notification and server request, in arrival order, for three turns -

- `edit`: one apply_patch inside the workspace (update, add, delete, move),
  then one outside it, which asks for approval;
- `steps`: six commands in a row (`git status`, `cat`, `ls`, `grep`, `sed`,
  `false`);
- `ask`: `request_user_input` with two questions, the second one secret.

The frames are replayed into the real adapter through a controlled peer.
"""
import asyncio
import json
from pathlib import Path
import unittest

from omodachi_core.agent_chat_provider import CodexDefaultAgentChat, DefaultAgentBinding

FRAMES = json.loads((Path(__file__).parent / "fixtures" / "codex-0160-agent-collie1.json").read_text())


def thread_of(name):
    return next(frame["params"]["threadId"] for frame in FRAMES[name] if "threadId" in frame.get("params", {}))


class Peer:
    def __init__(self, thread):
        self.thread = thread
        self.ordinal = 0
        self.answers = []
        self.on_notification = lambda value, ordinal: None
        self.on_server_request = lambda value: None
        self.on_disconnect = lambda: None

    async def request(self, method, params):
        result, _ = await self.request_fenced(method, params)
        return result

    async def request_fenced(self, method, params):
        self.ordinal += 1
        if method == "thread/loaded/list":
            return {"data": [self.thread]}, self.ordinal
        return {"thread": {"id": self.thread, "turns": []}}, self.ordinal

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


async def replay(name, *, until=None):
    """The adapter after the capture `name`, stopped before the first frame
    whose method is `until` (so a prompt can be left waiting)."""
    peer = Peer(thread_of(name))
    chat = CodexDefaultAgentChat(DefaultAgentBinding("omarchy-fixture", peer.thread,
                                                     "ws://127.0.0.1:47311", "f" * 64), peer)
    await chat.attach()
    events = []
    for frame in FRAMES[name]:
        if until is not None and frame["method"] == until:
            break
        peer.feed(frame)
        while not chat.events.empty():
            events.append(chat.events.get_nowait()["event"])
    chat._flush_output()
    while not chat.events.empty():
        events.append(chat.events.get_nowait()["event"])
    return chat, peer, events


class QuestionCards(unittest.IsolatedAsyncioTestCase):
    """Goal 0 / idea 3: a question keeps what codex sent about it."""

    async def test_real_question_keeps_descriptions_other_and_secret(self):
        chat, peer, events = await replay("ask", until="serverRequest/resolved")
        [requested] = [event for event in events if event["type"] == "agent.approval.requested"]
        branch, token = requested["details"]["questions"]
        # Old clients read `options` as labels; that never changes.
        self.assertEqual(branch["options"], ["main (Recommended)", "release"])
        self.assertEqual(branch["choices"], [
            {"label": "main (Recommended)", "description": "The default branch everyone pulls from."},
            {"label": "release", "description": "Only for tagged builds."}])
        # codex 0.160 sets isOther on every question the model asks.
        self.assertTrue(branch["other"])
        self.assertFalse(branch["secret"])
        self.assertTrue(token["secret"])
        self.assertTrue(token["other"])
        self.assertEqual(token["choices"], [{"label": "Use the saved one",
                                             "description": "The token already in the keychain."}])
        # The same projection is what a reconnecting phone reads.
        [pending] = chat.snapshot_value()["pendingApprovals"]
        self.assertEqual(pending["details"]["questions"], requested["details"]["questions"])
        # A typed answer goes to codex as the person wrote it.
        await chat.resolve_approval(requested["request_id"], answers={"branch": ["release"], "token": ["s3cret"]})
        self.assertEqual(peer.answers[-1][1], {"answers": {"branch": {"answers": ["release"]},
                                                           "token": {"answers": ["s3cret"]}}})

    async def test_a_question_without_descriptions_or_flags(self):
        chat, peer, _ = await replay("ask", until="item/tool/requestUserInput")
        peer.feed({"id": 40, "method": "item/tool/requestUserInput", "params": {
            "threadId": peer.thread, "turnId": "t", "itemId": "i", "isBlocking": True,
            "questions": [{"id": "q", "header": "Pick", "question": "Which one?",
                           "options": [{"label": "A"}, {"label": "B", "description": " "}, "junk"]}]}})
        [question] = chat.approvals_value()[-1]["details"]["questions"]
        self.assertEqual(question["options"], ["A", "B"])
        self.assertEqual(question["choices"], [{"label": "A"}, {"label": "B"}])
        self.assertFalse(question["other"])
        self.assertFalse(question["secret"])


class ChangeCards(unittest.IsolatedAsyncioTestCase):
    """Idea 1: see the edit before you allow it."""

    async def test_real_file_changes_become_bounded_change_rows(self):
        chat, _, events = await replay("edit")
        rows = {row["id"]: row for row in chat.snapshot_value()["rows"] if row["kind"] == "tool"}
        inside, outside = rows["call-p1-1"], rows["call-p2-2"]
        by_path = {change["path"].rsplit("/", 1)[-1]: change for change in inside["changes"]}
        self.assertEqual(set(by_path), {"app.py", "notes.txt", "plan.md", "old.txt"})
        update = by_path["app.py"]
        self.assertEqual((update["kind"], update["added"], update["removed"]), ("update", 3, 2))
        self.assertTrue(update["diff"].startswith("@@ -4,4 +4,5 @@\n def main():\n-    port = 8080"))
        # An added file is its whole text in codex's item; here it has signs.
        added = by_path["plan.md"]
        self.assertEqual((added["kind"], added["added"], added["removed"]), ("add", 5, 0))
        self.assertEqual(added["diff"].split("\n")[0], "+# Plan")
        deleted = by_path["old.txt"]
        self.assertEqual((deleted["kind"], deleted["added"], deleted["removed"], deleted["diff"]),
                         ("delete", 0, 2, "-remove me\n-second line"))
        # The move codex appends to the diff text is a field, not a diff line.
        moved = by_path["notes.txt"]
        self.assertEqual(moved["move_path"], "/home/user/ac1test/docs/notes.txt")
        self.assertEqual(moved["diff"], "@@ -1 +1 @@\n-draft\n+final")
        self.assertNotIn("Moved to", json.dumps(inside["changes"]))
        self.assertEqual(outside["changes"][0]["diff"], "@@ -1,2 +1,3 @@\n-mode = slow\n+mode = fast\n+retries = 3\n name = demo")
        # Old clients keep what they had: the name, the JSON detail, paths.
        self.assertEqual(inside["name"], "File changes")
        self.assertIsInstance(json.loads(inside["detail"]), list)
        self.assertEqual(len(inside["paths"]), 4)

    async def test_the_change_is_on_screen_before_the_approval_and_the_approval_names_it(self):
        chat, _, events = await replay("edit", until="serverRequest/resolved")
        kinds = [(event["type"], (event.get("value") or {}).get("id") or event.get("kind")) for event in events]
        started = kinds.index(("tool", "call-p2-2"))
        asked = kinds.index(("agent.approval.requested", "fileChange"))
        self.assertLess(started, asked)
        row = events[started]["value"]
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["changes"][0]["added"], 2)
        approval = events[asked]
        self.assertEqual(approval["details"]["item_id"], "call-p2-2")
        self.assertEqual(approval["details"]["files"], ["/home/user/ac1-outside/settings.txt"])
        self.assertEqual((approval["details"]["added"], approval["details"]["removed"]), (2, 1))
        [pending] = chat.snapshot_value()["pendingApprovals"]
        self.assertEqual(pending["details"], approval["details"])

    def test_caps(self):
        from omodachi_core.agent_chat_provider import (
            DIFF_FILE_LINES, DIFF_FILES, DIFF_LINE_CHARS, DIFF_ROW_LINES, project_changes)
        big = {"path": "/w/big.txt", "kind": {"type": "add"}, "diff": "".join("line %d %s\n" % (n, "x" * 900)
                                                                             for n in range(500))}
        [row], omitted = project_changes([big])
        lines = row["diff"].split("\n")
        self.assertEqual(len(lines), DIFF_FILE_LINES)
        self.assertEqual(row["diff_omitted"], 500 - DIFF_FILE_LINES)
        self.assertEqual(row["added"], 500)
        self.assertTrue(all(len(line) <= DIFF_LINE_CHARS for line in lines))
        many = [{"path": "/w/f%d" % n, "kind": {"type": "add"}, "diff": "a\n" * 100} for n in range(DIFF_FILES + 3)]
        rows, omitted = project_changes(many)
        self.assertEqual((len(rows), omitted), (DIFF_FILES, 3))
        self.assertLessEqual(sum(len(row["diff"].split("\n")) for row in rows if row["diff"]), DIFF_ROW_LINES)
        self.assertEqual(sum(row["added"] for row in rows), DIFF_FILES * 100)
        # Junk is skipped, not drawn.
        self.assertEqual(project_changes([None, {"kind": {"type": "add"}}, "x"]), ([], 0))
        self.assertEqual(project_changes("nope"), ([], 0))

    def test_snapshot_budget_drops_old_diffs_but_keeps_counts(self):
        from omodachi_core.agent_chat_provider import SNAPSHOT_DIFF_BUDGET, budget_rows
        line = "+" + "y" * 300
        rows = [{"kind": "tool", "id": "c%d" % n, "turnID": "t", "name": "File changes", "status": "succeeded",
                 "detail": "", "changes": [{"path": "/w/f%d" % n, "kind": "add", "added": 120, "removed": 0,
                                            "diff": "\n".join([line] * 120)}]} for n in range(12)]
        out = budget_rows(rows)
        sizes = [len(row["changes"][0]["diff"]) for row in out]
        self.assertLessEqual(sum(sizes), SNAPSHOT_DIFF_BUDGET)
        self.assertGreater(sizes[-1], 0)
        self.assertEqual(sizes[0], 0)
        self.assertEqual(out[0]["changes"][0]["diff_omitted"], 120)
        self.assertEqual(out[0]["changes"][0]["added"], 120)
        # core's own rows are not cut.
        self.assertEqual(len(rows[0]["changes"][0]["diff"].split("\n")), 120)


class Steps(unittest.IsolatedAsyncioTestCase):
    """Idea 2: each step in a few words, with its exit code and duration."""

    async def test_real_commands_carry_their_reading(self):
        chat, _, _ = await replay("steps")
        rows = [row for row in chat.snapshot_value()["rows"] if row["kind"] == "tool"]
        self.assertEqual([row["command"] for row in rows],
                         ["git status", "cat app.py", "ls -la", "grep -rn TODO .", "sed -n 1,3p app.py", "false"])
        self.assertEqual([row["actions"] for row in rows], [
            [{"type": "unknown"}], [{"type": "read", "name": "app.py"}], [{"type": "listFiles"}],
            [{"type": "search", "query": "TODO", "path": "."}], [{"type": "read", "name": "app.py"}],
            [{"type": "unknown"}]])
        self.assertEqual([row["exit_code"] for row in rows], [0, 0, 0, 0, 0, 1])
        self.assertEqual(rows[-1]["status"], "failed")
        self.assertTrue(all(isinstance(row["duration_ms"], int) for row in rows))
        # The raw command line is still the row's name.
        self.assertEqual(rows[0]["name"], "/bin/zsh -lc 'git status'")

    def test_inner_command(self):
        from omodachi_core.agent_chat_provider import inner_command
        self.assertEqual(inner_command("/bin/zsh -lc 'git status'"), "git status")
        self.assertEqual(inner_command("bash -lc \"echo 'a b'\""), "echo 'a b'")
        self.assertEqual(inner_command("/bin/zsh -lc false"), "false")
        self.assertEqual(inner_command("make all"), "make all")
        self.assertEqual(inner_command("bash -lc 'unterminated"), "bash -lc 'unterminated")
        self.assertIsNone(inner_command(None))
        self.assertEqual(len(inner_command("bash -lc '" + "x" * 900 + "'")), 400)


if __name__ == "__main__":
    unittest.main()
