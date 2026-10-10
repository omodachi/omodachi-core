"""HERDR-4: the operator's Quick / keys / commands rows, and an honest proof
for short replies.

The operator files are Collie's own format (Collie `docs/configure.md`,
`*.toml.example`, ADR 0018/0043); the rules checked here are Collie's:
replace per surface, never merge; the narrowest scope wins; a bad row is
dropped, a bad file holds its last good rows.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

import aiohttp
from omodachi_core import browser_gate
from omodachi_core.bootstrap import create_service
from omodachi_core.herdr_bridge import text_proof
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
from omodachi_core.operator_rows import (OperatorRows, canonical_agent, rows_for, validate_commands,
                                         validate_keys, validate_quick_replies)

sys.path.insert(0, str(Path(__file__).parent))


class Dirs:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ours = Path(self.temp.name) / "omodachi"
        self.collie = Path(self.temp.name) / "collie"
        self.ours.mkdir()
        self.collie.mkdir()

    def write(self, where: Path, name: str, text: str) -> None:
        path = where / name
        path.write_text(text)
        # Two writes inside one clock tick must still read as a change.
        stamp = path.stat().st_mtime_ns + 1_000_000
        os.utime(path, ns=(stamp, stamp))


def labels(rows, key):
    return [row[key] for row in rows]


class ShippedRowsTests(unittest.TestCase):
    def setUp(self):
        self.dirs = Dirs()
        self.rows = OperatorRows(self.dirs.ours, self.dirs.collie)

    def tearDown(self):
        self.dirs.temp.cleanup()

    def test_an_agent_pane_gets_collies_dock_presets_and_its_harness(self):
        claude = self.rows.resolve("claude")
        self.assertEqual([(row["id"], row["items"]) for row in claude["quick"]],
                         [("confirm", ["yes", "no"]), ("common", ["continue", "commit and push", "retry", "skip"])])
        self.assertEqual(labels(claude["keys"], "label"), ["Ctrl C", "Ctrl D", "Ctrl U", "Ctrl R", "Ctrl L", "Ctrl Z"])
        self.assertEqual([row["label"] for row in claude["keys"] if row["danger"]], ["Ctrl D", "Ctrl Z"])
        self.assertEqual(labels(claude["harness"], "command"), ["/model", "/effort", "/compact", "/resume"])
        self.assertTrue(all(row["label"] is None and not row["operator"] for row in claude["harness"]))
        self.assertEqual(labels(self.rows.resolve("codex")["harness"], "command"), ["/model", "/compact", "/resume"])
        # A variant name folds onto its family for what ships.
        self.assertEqual(labels(self.rows.resolve("claude-code")["harness"], "id"),
                         ["model", "effort", "compact", "resume"])
        self.assertEqual(self.rows.resolve("opencode")["harness"], [])
        self.assertEqual([source["origin"] for source in claude["sources"]], ["shipped"] * 3)

    def test_a_shell_gets_y_and_n_and_no_harness(self):
        shell = self.rows.resolve(None)
        self.assertEqual([row["items"] for row in shell["quick"]], [["y", "n"]])
        self.assertEqual(shell["harness"], [])
        self.assertEqual((shell["agent"], shell["family"]), (None, "shell"))


class OperatorRowsTests(unittest.TestCase):
    def setUp(self):
        self.dirs = Dirs()
        self.rows = OperatorRows(self.dirs.ours, self.dirs.collie)

    def tearDown(self):
        self.dirs.temp.cleanup()

    def test_rows_replace_what_ships_on_the_panes_they_address_only(self):
        self.dirs.write(self.dirs.ours, "quick-replies.toml",
                        '[[replies]]\nscope = "codex"\ntitle = "confirm"\nitems = ["approve", "reject"]\n')
        codex, claude = self.rows.resolve("codex"), self.rows.resolve("claude")
        self.assertEqual([(row["title"], row["items"], row["operator"]) for row in codex["quick"]],
                         [("confirm", ["approve", "reject"], True)])
        # Never merged: the shipped "common" group is gone on the codex pane...
        self.assertNotIn("common", labels(codex["quick"], "id"))
        # ...and untouched on a pane the row does not address.
        self.assertEqual(labels(claude["quick"], "id"), ["confirm", "common"])

    def test_the_narrowest_scope_wins_one_row_per_name(self):
        rows = [{"label": "Yes", "scope": None, "n": 1}, {"label": "Yes", "scope": "claude", "n": 2},
                {"label": "Yes", "scope": "claude-code", "n": 3}, {"label": "No", "scope": None, "n": 4},
                {"label": "Other", "scope": "omp", "n": 5}]
        pick = lambda agent: [row["n"] for row in rows_for(rows, agent, lambda row: row["label"])]
        self.assertEqual(pick("claude-code"), [3, 4])
        self.assertEqual(pick("claude"), [2, 4])
        self.assertEqual(pick("codex"), [1, 4])
        self.assertEqual(pick("shell"), [1, 4])
        # A family scope is the catalog's own name; an arbitrary prefix is not.
        rows.append({"label": "Mine", "scope": "claude-local", "n": 6})
        self.assertNotIn(6, pick("claude-code"))
        self.assertEqual(canonical_agent("claude-code"), "claude")
        self.assertEqual(canonical_agent("omp"), "omp")
        self.assertEqual(canonical_agent("pi-dev"), "pi")

    def test_a_shell_is_addressed_as_shell_and_by_unscoped_rows(self):
        self.dirs.write(self.dirs.ours, "quick-replies.toml",
                        '[[replies]]\nscope = "shell"\ntitle = "bestätigen"\nitems = ["j", "n"]\n')
        self.assertEqual(self.rows.resolve(None)["quick"][0]["items"], ["j", "n"])
        self.assertEqual(labels(self.rows.resolve("claude")["quick"], "id"), ["confirm", "common"])

    def test_collies_directory_is_read_when_ours_has_no_such_file(self):
        self.dirs.write(self.dirs.collie, "keys.toml", '[[keys]]\nlabel = "Yes"\nkeys = ["Down", "Enter"]\n')
        self.dirs.write(self.dirs.collie, "quick-replies.toml", '[[replies]]\ntitle = "x"\nitems = ["from collie"]\n')
        self.dirs.write(self.dirs.ours, "quick-replies.toml", '[[replies]]\ntitle = "x"\nitems = ["from ours"]\n')
        view = self.rows.resolve("claude")
        self.assertEqual(view["keys"], [{"label": "Yes", "keys": ["Down", "Enter"], "danger": False, "operator": True}])
        self.assertEqual(view["quick"][0]["items"], ["from ours"])
        self.assertEqual({row["file"]: row["origin"] for row in view["sources"]},
                         {"quick-replies.toml": "omodachi", "keys.toml": "collie", "commands.toml": "shipped"})

    def test_edits_are_live_and_a_broken_file_keeps_its_last_good_rows(self):
        self.dirs.write(self.dirs.ours, "keys.toml", '[[keys]]\nlabel = "One"\nkeys = ["1"]\n')
        self.assertEqual(labels(self.rows.resolve("claude")["keys"], "label"), ["One"])
        self.dirs.write(self.dirs.ours, "keys.toml", '[[keys]]\nlabel = "Two"\nkeys = ["2"]\n')
        self.assertEqual(labels(self.rows.resolve("claude")["keys"], "label"), ["Two"])
        self.dirs.write(self.dirs.ours, "keys.toml", '[[keys]]\nlabel = "Three\nkeys = ["3"]\n')
        view = self.rows.resolve("claude")
        self.assertEqual(labels(view["keys"], "label"), ["Two"], "the last good rows are kept")
        warning = view["sources"][1]["warning"]
        self.assertEqual(warning["code"], "operator_file_unparsed")
        self.assertIn("line 2", warning["detail"])
        # Fixed again: the warning goes with the problem.
        self.dirs.write(self.dirs.ours, "keys.toml", '[[keys]]\nlabel = "Four"\nkeys = ["4"]\n')
        view = self.rows.resolve("claude")
        self.assertEqual((labels(view["keys"], "label"), view["sources"][1]["warning"]), (["Four"], None))
        # Deleted: back to what ships.
        (self.dirs.ours / "keys.toml").unlink()
        self.assertEqual(self.rows.resolve("claude")["keys"][0]["label"], "Ctrl C")

    def test_a_file_that_never_parsed_ships_the_defaults_and_says_why(self):
        self.dirs.write(self.dirs.ours, "commands.toml", "this is not toml")
        view = self.rows.resolve("codex")
        self.assertEqual(labels(view["harness"], "id"), ["model", "compact", "resume"])
        self.assertEqual(view["sources"][2]["warning"]["code"], "operator_file_unparsed")

    def test_one_bad_row_is_dropped_not_the_file(self):
        warnings = []
        rows = validate_keys({"keys": [
            {"label": "Back", "keys": ["shift+tab"]},
            {"label": "Page", "keys": ["PageUp"]},         # herdr cannot send it
            {"label": "Tmux", "keys": ["C-c"]},            # tmux spelling
            {"label": "Cmd", "keys": ["cmd+c"]},           # herdr drops the modifier
            {"label": "Half", "keys": ["Down", "nope"]},   # one bad step drops the row
            {"label": "", "keys": ["a"]},
            {"label": "Sure", "keys": ["y"], "danger": "yes"},
            {"label": "Wide", "keys": ["y"], "scope": ""},
            "not a table",
        ]}, warnings.append)
        self.assertEqual(rows, [{"label": "Back", "keys": ["shift+Tab"], "danger": False, "scope": None}])
        self.assertEqual(len(warnings), 8)

    def test_quick_reply_and_command_grammar(self):
        warnings = []
        quick = validate_quick_replies({"replies": [
            {"title": "ok", "items": ["yes", " no "]},
            {"title": "blank", "items": ["yes", "  "]},
            {"title": "empty", "items": []},
            {"title": "ok", "items": ["later wins"]},
        ]}, warnings.append)
        self.assertEqual(quick, [{"title": "ok", "items": ["later wins"], "scope": None}])
        self.assertEqual(len(warnings), 3)
        warnings = []
        commands = validate_commands({"commands": [
            {"command": "/model <name>", "description": "Pick one"},
            {"command": "model"},
            {"command": "/deploy", "confirm": "true"},
            {"command": "/x", "bar": 1},
            {"command": "/status", "bar": True, "bar_label": "A very long label indeed"},
        ]}, warnings.append)
        self.assertEqual([(row["command"], row["arg_hint"]) for row in commands],
                         [("/model", "<name>"), ("/status", None)])
        self.assertEqual(commands[1]["bar_label"], "A very long…")
        self.assertEqual(len(warnings), 4)

    def test_bar_rows_replace_the_harness_and_inherit_a_shipped_brake(self):
        self.dirs.write(self.dirs.ours, "commands.toml",
                        '[[commands]]\nscope = "claude"\ncommand = "/statusline"\nbar = true\nbar_label = "Status"\n\n'
                        '[[commands]]\nscope = "claude"\ncommand = "/clear"\nbar = true\nconfirm = false\n\n'
                        '[[commands]]\ncommand = "/standup"\n')
        claude = self.rows.resolve("claude")
        self.assertEqual([(row["label"], row["command"], row["confirm"]) for row in claude["harness"]],
                         [("Status", "/statusline", False), ("clear", "/clear", True)])
        self.assertEqual(labels(claude["commands"], "command"), ["/statusline", "/clear", "/standup"])
        # The unscoped /standup is not a bar row, so codex keeps its harness.
        codex = self.rows.resolve("codex")
        self.assertEqual(labels(codex["harness"], "id"), ["model", "compact", "resume"])
        self.assertEqual(labels(codex["commands"], "command"), ["/standup"])


class ShortReplyProofTests(unittest.TestCase):
    """HERDR-3 counted a short reply anywhere on the screen; `yes` in new output
    elsewhere was a proof. HERDR-4 asks for the input line instead."""

    def test_a_short_reply_is_proved_by_the_line_it_lands_on(self):
        self.assertEqual(text_proof("yes", "> ", "> yes"), "line")
        # Claude's empty box shows a hint; typing replaces it on the same line.
        self.assertEqual(text_proof("yes", '│ > Try "fix lint errors"   │', "│ > yes                     │"), "line")
        self.assertEqual(text_proof("y", "Continue? [y/N] ", "Continue? [y/N] y"), "line")
        self.assertEqual(text_proof("/model", "> ", "> /model"), "line")

    def test_the_word_turning_up_in_new_output_is_not_a_proof(self):
        before = "Shall I run the migration? Answer below.\n> "
        after = "Shall I run the migration? Answer below.\nThe tests say yes, all green.\n> "
        self.assertIsNone(text_proof("yes", before, after))
        # Output that only *contains* the word, on a changed line, is not it either.
        self.assertIsNone(text_proof("yes", "> \nlog: ", "> \nlog: yes we did it"))
        self.assertIsNone(text_proof("yes", "> yes", "> yes"))

    def test_a_line_that_only_moved_or_a_busy_screen_is_not_a_proof(self):
        # Lab, HERDR-4: a menu swallowed `yes` but logged three keys, which
        # pushed an old `SUBMITTED: yes` down a row - that row is not news.
        before = "h4menu\nSUBMITTED: yes\nCANCELLED (Esc)\n\nDo you want to run it?\n> 1. Yes"
        after = "h4menu\nKEY 79\nSUBMITTED: yes\nCANCELLED (Esc)\n\nDo you want to run it?\n> 1. Yes"
        self.assertIsNone(text_proof("yes", before, after))
        busy = "> \n" + "\n".join(f"line {n}" for n in range(6))
        self.assertIsNone(text_proof("yes", "> \n", busy.replace("> ", "> yes", 1)))

    def test_long_text_still_uses_the_tail_and_the_paste_token(self):
        self.assertEqual(text_proof("please run the tests again", "> ", "> please run the tests again"), "tail")
        self.assertEqual(text_proof("a\n" * 11 + "b", "> ", "> [Pasted text #1 +11 lines]"), "paste")


class PushNamesTheAgentNotThePaneTests(unittest.TestCase):
    """Odomo's Herd card said "Session work, pane w1:p2.": the push now carries
    the agent's name for it (the title already says it), ids still for the link."""

    def test_the_herdr_push_carries_a_plain_agent_name_only(self):
        from types import SimpleNamespace
        from omodachi_core.push import Watcher, navigate_url
        hub = Hub()
        watcher = Watcher()
        event = lambda rows: SimpleNamespace(type="herdr.layout.changed",
                                             payload={"session": "work", "blocked": rows})
        watcher.signals(event([]), hub)
        signals = watcher.signals(event([{"pane_id": "w1:p2", "name": "claude"},
                                         {"pane_id": "w1:p3", "name": "two words"},
                                         {"pane_id": "w1:p4"}]), hub)
        ids = {signal.ids["pane"]: signal.ids for signal in signals}
        self.assertEqual(ids["w1:p2"], {"session": "work", "pane": "w1:p2", "agent": "claude"})
        self.assertNotIn("agent", ids["w1:p3"])
        self.assertNotIn("agent", ids["w1:p4"])
        url = navigate_url("https://host.example", signals[0], "0" * 32)
        self.assertIn("agent=claude", url)


class OperatorRowsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dirs = Dirs()
        self.hub = Hub(auth_check_interval=0.05)
        self.token = self.hub.register_device("phone-a")
        self.service = create_service(self.hub, demo=True)
        self.service.operator_rows = OperatorRows(self.dirs.ours, self.dirs.collie)
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.close()
        self.dirs.temp.cleanup()

    async def get(self, query="", token=None):
        headers = {"Authorization": "Bearer " + token} if token else {}
        async with self.client.get(self.url + "/v1/herdr/operator-rows" + query, headers=headers) as response:
            return response.status, await response.json()

    async def test_the_route(self):
        status, _ = await self.get("?agent=claude")
        self.assertEqual(status, 401)
        status, body = await self.get("?agent=claude", self.token)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["contract_revision"], "omodachi.v1")
        self.assertEqual(labels(body["harness"], "id"), ["model", "effort", "compact", "resume"])
        status, body = await self.get("", self.token)
        self.assertEqual((status, body["family"]), (200, "shell"))
        self.dirs.write(self.dirs.ours, "quick-replies.toml", '[[replies]]\ntitle = "go"\nitems = ["weiter"]\n')
        status, body = await self.get("?agent=codex", self.token)
        self.assertEqual(body["quick"][0]["items"], ["weiter"], "the edit is live, no restart")
        for query in ("?agent=", "?agent=a%20b", "?agent=../x", "?agent=claude&x=1", "?other=1"):
            status, body = await self.get(query, self.token)
            self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"), query)

    async def test_a_web_credential_reads_them_too(self):
        # ODOMO-2: Odomo's Quick dock is the same rows.
        self.assertTrue(browser_gate.web_allows("GET", "/v1/herdr/operator-rows"))
        self.assertFalse(browser_gate.web_allows("PUT", "/v1/herdr/operator-rows"))


if __name__ == "__main__":
    unittest.main()
