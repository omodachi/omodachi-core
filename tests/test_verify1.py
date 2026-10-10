"""VERIFY-1: what the real host showed that the lab could not.

A Quick `continue` sent into codex's approval dialog got no Enter (the proof
held), but the dialog read the letters as its own shortcuts and cancelled the
request. While Herdr reports the pane's agent as blocked, a reply that asks to
be submitted is not typed at all.
"""
from __future__ import annotations

import unittest

from omodachi_core.herdr_bridge import HerdrBridge


def bridge(status: str | None, *, snapshot_ok: bool = True):
    calls: list[tuple[str, dict]] = []
    screen = {"text": "───\n> "}

    def request(method, params):
        calls.append((method, params))
        if method == "pane.read":
            return {"type": "pane_read", "read": {"text": screen["text"]}}
        if method == "pane.send_text":
            screen["text"] += params["text"]
        return {"type": "ok"}

    def runner(argv):
        if not snapshot_ok:
            return {}
        return {"snapshot": {"panes": [{"pane_id": "w1:p1", "agent_status": status},
                                        {"pane_id": "w1:p2", "agent_status": "idle"}]}}

    value = HerdrBridge(runner=runner)
    value.request = request
    return value, calls


class AgentBlockedTests(unittest.TestCase):
    quiet = staticmethod(lambda seconds: None)

    def test_a_reply_is_not_typed_into_a_blocked_agent(self):
        value, calls = bridge("blocked")
        reply = value.send_text("w1:p1", "continue", submit=True, sleep=self.quiet)
        self.assertEqual(reply, {"action": "text", "pane": "w1:p1", "delivered": False, "submitted": False,
                                 "reason": "herdr_agent_blocked", "proof": None})
        self.assertEqual(calls, [], "nothing reached the pane: no read, no text, no Enter")

    def test_other_states_send_as_before(self):
        for status in ("idle", "working", "done", "unknown", None):
            value, calls = bridge(status)
            reply = value.send_text("w1:p1", "please run the tests again", submit=True, sleep=self.quiet)
            self.assertTrue(reply["submitted"], status)
            self.assertIn(("pane.send_keys", {"pane_id": "w1:p1", "keys": ["Enter"]}), calls)

    def test_only_the_pane_itself_counts(self):
        value, _ = bridge("blocked")
        self.assertTrue(value.send_text("w1:p2", "please run the tests", submit=True, sleep=self.quiet)["submitted"])

    def test_an_unreadable_snapshot_leaves_it_to_the_proof(self):
        value, _ = bridge("blocked", snapshot_ok=False)
        self.assertTrue(value.send_text("w1:p1", "please run the tests again", submit=True,
                                        sleep=self.quiet)["submitted"])

    def test_typing_without_submit_is_not_held_back(self):
        # Type mode's long runs: the person is typing live and sees the pane.
        value, calls = bridge("blocked")
        reply = value.send_text("w1:p1", "abcdefghij", submit=False)
        self.assertEqual(reply["reason"], "not_requested")
        self.assertEqual(calls, [("pane.send_text", {"pane_id": "w1:p1", "text": "abcdefghij"})])


if __name__ == "__main__":
    unittest.main()
