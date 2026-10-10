"""REL2-VM-1: what a clean VM had left after `--remove --purge`.

On a clean Omarchy VM (v0.2.0 release gate) a purge printed two paths as
"yours rather than the installer's": ~/.config/omodachi/seen.json, which the
daemon writes (seen.py, NOTIFY-1) and PURGE_RULES did not name, and an empty
~/.local/share/omodachi/agent-workspace, the directory Install itself makes.
The first is now in the purge rule (with its temp file); the second goes when
nothing is in it and stays, listed, the moment anything is.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from omodachi_core.seen import SeenLedger

import test_install_host as base
from test_install_host import install_host


class SeenLedgerPurgeRuleTests(unittest.TestCase):
    def test_the_purge_rule_names_what_the_seen_ledger_writes(self):
        rules = install_host.PURGE_RULES[".config/omodachi"]
        with tempfile.TemporaryDirectory() as scratch:
            ledger = SeenLedger(Path(scratch) / "seen.json", clock=lambda: 1.0)
            ledger.observe_agent("working")
            ledger.observe_agent("done")
            ledger.save()
            written = sorted(os.listdir(scratch))
            temporary = Path(tempfile.mkstemp(prefix=".seen-", dir=scratch)[1]).name
        self.assertEqual(written, ["seen.json"])
        for name in ("seen.json", temporary):
            known, rule = install_host._rule_for(name, rules)
            self.assertTrue(known and rule is None, name)
        for name in ("seen.json.bak", ".seen-toolongname", "my-seen.json"):
            self.assertFalse(install_host._rule_for(name, rules)[0], name)


class EmptyAgentWorkspaceTests(base.RemoveKeepsTheUsersFilesTests):
    """The RELEASE-8 fixture, run with an agent-workspace that is empty."""

    def setUp(self):
        super().setUp()
        self.files["workspace"].unlink()
        self.files["workspace"].parent.rmdir()
        self.workspace = self.share / "agent-workspace"
        self.seen = self.home / ".config/omodachi/seen.json"
        self.seen.write_text('{"version":1,"rows":{}}')

    def test_purge_takes_an_empty_agent_workspace_and_seen_json(self):
        self.own_source()
        out = self.remove(purge=True)
        self.assertFalse(self.workspace.exists())
        self.assertFalse(self.seen.exists())
        self.assertNotIn(str(self.workspace), out)
        self.assertNotIn("seen.json", out)
        # the user's own files are still kept and listed
        self.assertTrue(self.files["menu"].exists())
        self.assertIn(str(self.files["menu"]), out)

    def test_remove_without_purge_takes_an_empty_agent_workspace_only(self):
        self.own_source()
        out = self.remove()
        self.assertFalse(self.workspace.exists())
        self.assertTrue(self.seen.exists())
        self.assertNotIn(str(self.workspace), out)

    def test_an_agent_workspace_with_anything_in_it_stays(self):
        (self.workspace / ".hidden").write_text("mine\n")
        self.own_source()
        out = self.remove(purge=True)
        self.assertEqual((self.workspace / ".hidden").read_text(), "mine\n")
        self.assertIn(str(self.workspace), out)



# Only the fixture is borrowed: the RELEASE-8 tests themselves run in their own
# module, with the non-empty workspace they were written for.
for _name in dir(base.RemoveKeepsTheUsersFilesTests):
    if _name.startswith("test_") and _name not in vars(EmptyAgentWorkspaceTests):
        setattr(EmptyAgentWorkspaceTests, _name, None)


if __name__ == "__main__":
    unittest.main()
