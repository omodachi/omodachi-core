"""HERDR-3: keys and text into a pane without taking it.

The socket tests run a real Unix-socket server that speaks Herdr's one-shot
NDJSON protocol the way herdr 0.8.2 does (probed 2026-10-03, HERDR-3 report
§1): one request per connection, `{"id","result"}` or `{"id","error"}`, a
`pane.read` that answers with the rendered screen. Behind it is a pretend
program with an input line, so the submit proof is exercised against a screen
that really does (or really does not) show what was typed.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import threading
import unittest

import aiohttp
from omodachi_core import browser_gate
from omodachi_core.bootstrap import create_service
from omodachi_core.herdr_bridge import (HerdrBridge, HerdrUnavailable, HerdrWriteAudit, MAX_KEYS,
                                        MAX_TEXT_BYTES, key_list, key_name, pane_text, text_landed)
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer

sys.path.insert(0, str(Path(__file__).parent))
from test_herdr_bridge import FAKE_HERDR, SNAPSHOT  # noqa: E402


class FakeHerdrSocket:
    """A pane running a program with one input line, behind Herdr's socket.

    `mode` is what the program does with typed text:
      - `echo`: it shows it on the input line (a shell, Claude's composer);
      - `dialog`: a dialog has focus and swallows it - nothing changes;
      - `paste`: it shows Claude's `[Pasted text #N +M lines]` token instead.
    Enter moves the input line into the history, which is how the tests see
    whether, and what, was submitted.
    """

    def __init__(self, path: str, mode: str = "echo") -> None:
        self.path = path
        self.mode = mode
        self.history: list[str] = ["$ earlier output"]
        self.line = ""
        self.pastes = 0
        self.requests: list[dict] = []
        self.keys: list[list[str]] = []
        self.fail_reads = 0
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(16)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.close()

    def screen(self) -> str:
        return "\n".join([*self.history, "─" * 20, "> " + self.line, "─" * 20])

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection:
                buffer = b""
                while b"\n" not in buffer:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
                if not buffer:
                    continue
                request = json.loads(buffer.split(b"\n", 1)[0])
                self.requests.append(request)
                reply = self._answer(request)
                connection.sendall((json.dumps(reply) + "\n").encode())

    def _answer(self, request: dict) -> dict:
        method, params, ident = request["method"], request["params"], request["id"]
        if params.get("pane_id") != "w1:p1":
            return {"id": "", "error": {"code": "pane_not_found",
                                        "message": f"pane {params.get('pane_id')} not found"}}
        if method == "pane.read":
            if self.fail_reads:
                self.fail_reads -= 1
                return {"id": "", "error": {"code": "internal", "message": "busy"}}
            return {"id": ident, "result": {"type": "pane_read", "read": {
                "pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "source": params["source"],
                "format": params.get("format", "text"), "text": self.screen(), "revision": 0,
                "truncated": False}}}
        if method == "pane.send_text":
            if self.mode == "echo":
                self.line += params["text"].replace("\n", " ")
            elif self.mode == "paste":
                self.pastes += 1
                self.line += f"[Pasted text #{self.pastes} +{params['text'].count(chr(10))} lines]"
            return {"id": ident, "result": {"type": "ok"}}
        if method == "pane.send_keys":
            self.keys.append(list(params["keys"]))
            if "PageUp" in params["keys"]:
                return {"id": "", "error": {"code": "invalid_key", "message": "unsupported key PageUp"}}
            for key in params["keys"]:
                if key == "Enter":
                    self.history.append("> " + self.line)
                    self.line = ""
            return {"id": ident, "result": {"type": "ok"}}
        if method == "pane.focus":
            return {"id": ident, "result": {"type": "pane_info"}}
        return {"id": "", "error": {"code": "invalid_request", "message": "unknown variant"}}


def short_temp() -> tempfile.TemporaryDirectory:
    # A Unix socket path is limited to ~104 bytes on macOS; the default
    # temporary directory there is longer than that on its own.
    return tempfile.TemporaryDirectory(dir="/tmp" if os.path.isdir("/tmp") else None)


class KeyGrammarTests(unittest.TestCase):
    """Herdr's grammar as probed on 0.8.2, narrowed to the keys a person means."""

    def test_the_keys_row_and_the_herdr_names_pass_in_canonical_spelling(self):
        cases = {"Escape": "Escape", "esc": "Escape", "ESCAPE": "Escape", "Tab": "Tab",
                 "shift+tab": "shift+Tab", "Enter": "Enter", "enter": "Enter", "Space": "Space",
                 "Backspace": "Backspace", "Up": "Up", "down": "Down", "Left": "Left", "Right": "Right",
                 "F1": "F1", "f12": "F12", "ctrl+c": "ctrl+c", "CTRL+d": "ctrl+d", "alt+f": "alt+f",
                 "shift+alt+ctrl+p": "ctrl+alt+shift+p", "alt+Up": "alt+Up", "ctrl+left": "ctrl+Left",
                 "1": "1", "5": "5", "a": "a", "/": "/", "?": "?", "é": "é", "+": "+"}
        for value, canonical in cases.items():
            self.assertEqual(key_name(value), canonical, value)

    def test_the_keys_herdr_cannot_send_are_refused_by_name(self):
        for value in ("PageUp", "pagedown", "Home", "END", "Insert", "Delete", "shift+PageUp", "ctrl+Home"):
            with self.assertRaises(HerdrUnavailable) as caught:
                key_name(value)
            self.assertEqual(caught.exception.args[0], "herdr_key_unsupported", value)

    def test_anything_else_is_not_a_key(self):
        # `cmd+`/`super+` reach Herdr as the bare key and tmux's `C-c` is taken
        # by 0.8.2 too; none of them is what a person pressed.
        for value in ("", "ab", "ctrl+", "ctrl++", "cmd+c", "super+a", "C-c", "BTab", "F13", "F0",
                      "ctrl+ctrl+c", "hyper+x", " ", "\n", "\x1b", "\x7f", "ctrl+\x03", 3, None,
                      ["Enter"], "x" * 40):
            with self.assertRaises(HerdrUnavailable, msg=repr(value)) as caught:
                key_name(value)
            self.assertEqual(caught.exception.args[0], "invalid_key", repr(value))

    def test_a_call_carries_one_to_thirty_two_keys(self):
        self.assertEqual(key_list(["Down", "Down", "2", "Enter"]), ["Down", "Down", "2", "Enter"])
        self.assertEqual(len(key_list(["a"] * MAX_KEYS)), MAX_KEYS)
        with self.assertRaises(HerdrUnavailable) as caught:
            key_list(["a"] * (MAX_KEYS + 1))
        self.assertEqual(caught.exception.args[0], "herdr_too_many_keys")
        for value in ([], "Enter", {"keys": ["Enter"]}, None):
            with self.assertRaises(HerdrUnavailable):
                key_list(value)


class TextRuleTests(unittest.TestCase):
    def test_words_newlines_and_tabs_pass(self):
        for value in ("hello", "two\nlines", "a\tb", "中文的一句话", "emoji 👩‍👧‍👦 ok", "x" * MAX_TEXT_BYTES):
            self.assertEqual(pane_text(value), value)

    def test_escape_codes_the_submit_key_and_empty_text_are_refused(self):
        for value in ("", "   ", "\n", "a\rb", "\x1b[A", "a\x03", "a\x7f", "a\x00b", None, 3, ["x"]):
            with self.assertRaises(HerdrUnavailable, msg=repr(value)) as caught:
                pane_text(value)
            self.assertEqual(caught.exception.args[0], "invalid_text", repr(value))

    def test_the_size_cap_is_bytes_and_is_refused_not_cut(self):
        with self.assertRaises(HerdrUnavailable) as caught:
            pane_text("x" * (MAX_TEXT_BYTES + 1))
        self.assertEqual(caught.exception.args[0], "herdr_text_too_long")
        with self.assertRaises(HerdrUnavailable):
            pane_text("中" * (MAX_TEXT_BYTES // 3 + 1))


class SubmitProofTests(unittest.TestCase):
    """`text_landed`: the end of what was sent, newly on the screen."""

    def test_the_tail_must_be_new_on_screen(self):
        sent = "please run the tests again"
        self.assertTrue(text_landed(sent, "> ", "> please run the tests again"))
        # Already on screen before the send: the screen proves nothing.
        self.assertFalse(text_landed(sent, "please run the tests again\n> ", "please run the tests again\n> "))
        # A second copy appearing does prove it.
        self.assertTrue(text_landed(sent, "please run the tests again\n> ",
                                    "please run the tests again\n> please run the tests again"))
        self.assertFalse(text_landed(sent, "> ", "> please run the"))

    def test_wrapping_indentation_and_box_lines_do_not_hide_the_text(self):
        sent = "a sentence long enough to wrap at the edge of a narrow phone pane"
        after = ("╭" + "─" * 10 + "╮\n│ > a sentence long enough to wrap at the edge │\n"
                 "│   of a narrow phone pane │\n╰" + "─" * 10 + "╯")
        self.assertTrue(text_landed(sent, "", after))

    def test_a_short_answer_has_to_be_there_whole(self):
        self.assertTrue(text_landed("yes", "> ", "> yes"))
        self.assertFalse(text_landed("yes", "> yes", "> yes"))

    def test_claudes_paste_token_counts_when_it_is_new_and_fits(self):
        sent = "\n".join(f"line {n}" for n in range(12))
        self.assertTrue(text_landed(sent, "> ", "> [Pasted text #1 +11 lines]"))
        self.assertFalse(text_landed(sent, "> [Pasted text #1 +11 lines]", "> [Pasted text #1 +11 lines]"))
        self.assertFalse(text_landed(sent, "> ", "> [Pasted text #1 +40 lines]"))
        self.assertTrue(text_landed(sent, "> [Pasted text #1 +11 lines]",
                                    "> [Pasted text #1 +11 lines][Pasted text #2 +11 lines]"))


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.temp = short_temp()
        self.path = os.path.join(self.temp.name, "h.sock")
        self.fake = FakeHerdrSocket(self.path)
        self.bridge = HerdrBridge(socket_path=Path(self.path), runner=lambda argv: SNAPSHOT)
        self.bridge.proof_delay = 0.01
        self.slept: list[float] = []

    def tearDown(self):
        self.fake.close()
        self.temp.cleanup()

    def sleep(self, seconds):
        self.slept.append(seconds)

    def test_keys_are_one_send_keys_rpc_in_order(self):
        result = self.bridge.send_keys("w1:p1", ["Down", "2", "shift+tab", "Enter"])
        self.assertEqual(result, {"action": "keys", "pane": "w1:p1", "sent": 4})
        self.assertEqual(self.fake.requests[-1]["method"], "pane.send_keys")
        self.assertEqual(self.fake.requests[-1]["params"], {"pane_id": "w1:p1",
                                                            "keys": ["Down", "2", "shift+Tab", "Enter"]})
        self.assertIsInstance(self.fake.requests[-1]["id"], str)

    def test_a_refused_key_never_reaches_herdr(self):
        with self.assertRaises(HerdrUnavailable):
            self.bridge.send_keys("w1:p1", ["Enter", "PageUp"])
        self.assertEqual(self.fake.requests, [])

    def test_a_pane_that_is_gone_says_so(self):
        with self.assertRaises(HerdrUnavailable) as caught:
            self.bridge.send_keys("w1:p9", ["Enter"])
        self.assertEqual(caught.exception.args[0], "herdr_pane_gone")

    def test_text_with_proof_is_typed_then_submitted(self):
        result = self.bridge.send_text("w1:p1", "fix the failing test please", submit=True, sleep=self.sleep)
        self.assertEqual(result, {"action": "text", "pane": "w1:p1", "delivered": True, "submitted": True,
                                  "reason": None, "proof": "tail"})
        methods = [request["method"] for request in self.fake.requests]
        self.assertEqual(methods, ["pane.read", "pane.send_text", "pane.read", "pane.send_keys"])
        self.assertEqual(self.fake.keys, [["Enter"]])
        self.assertEqual(self.fake.history[-1], "> fix the failing test please")
        # The proof's first look is immediate; no sleep was needed.
        self.assertEqual(self.slept, [])
        for request in self.fake.requests:
            if request["method"] == "pane.read":
                self.assertEqual(request["params"]["source"], "visible")

    def test_a_dialog_that_swallows_the_text_gets_no_enter(self):
        self.fake.mode = "dialog"
        result = self.bridge.send_text("w1:p1", "fix the failing test please", submit=True, sleep=self.sleep)
        self.assertEqual(result, {"action": "text", "pane": "w1:p1", "delivered": True, "submitted": False,
                                  "reason": "not_seen", "proof": None})
        self.assertEqual(self.fake.keys, [], "no Enter was pressed into the dialog")
        self.assertEqual(len(self.slept), 7)
        self.assertEqual(sum(request["method"] == "pane.read" for request in self.fake.requests), 9)

    def test_a_long_paste_is_proved_by_claudes_token(self):
        self.fake.mode = "paste"
        text = "\n".join(f"step {n}: do the thing" for n in range(20))
        result = self.bridge.send_text("w1:p1", text, submit=True, sleep=self.sleep)
        self.assertTrue(result["submitted"])
        self.assertEqual(self.fake.keys, [["Enter"]])
        # Never chunked: the whole text was one RPC.
        sends = [request for request in self.fake.requests if request["method"] == "pane.send_text"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["params"]["text"], text)

    def test_text_without_submit_is_typed_only(self):
        result = self.bridge.send_text("w1:p1", "draft", submit=False, sleep=self.sleep)
        self.assertEqual(result["reason"], "not_requested")
        self.assertEqual([request["method"] for request in self.fake.requests], ["pane.send_text"])
        self.assertEqual(self.fake.keys, [])

    def test_a_read_that_fails_before_the_send_sends_nothing(self):
        self.fake.fail_reads = 1
        with self.assertRaises(HerdrUnavailable):
            self.bridge.send_text("w1:p1", "fix the failing test please", submit=True, sleep=self.sleep)
        self.assertEqual([request["method"] for request in self.fake.requests], ["pane.read"])

    def test_a_proof_read_that_fails_is_retried_not_fatal(self):
        sent = []
        original = self.bridge.read_visible

        def flaky(pane):
            sent.append(pane)
            if len(sent) == 2:
                raise HerdrUnavailable("herdr_unavailable")
            return original(pane)
        self.bridge.read_visible = flaky
        result = self.bridge.send_text("w1:p1", "fix the failing test please", submit=True, sleep=self.sleep)
        self.assertTrue(result["submitted"])
        self.assertEqual(self.slept, [0.01])

    def test_only_visible_reads_and_the_four_methods_reach_the_socket(self):
        for method, params in (("pane.read", {"pane_id": "w1:p1", "source": "recent"}),
                               ("pane.read", {"pane_id": "w1:p1", "source": "recent_unwrapped"}),
                               ("pane.read", {"pane_id": "w1:p1"}),
                               ("pane.close", {"pane_id": "w1:p1"}),
                               ("session.snapshot", {}),
                               ("agent.send", {"target": "x", "text": "y"})):
            with self.assertRaises(HerdrUnavailable, msg=method) as caught:
                self.bridge.request(method, params)
            self.assertEqual(caught.exception.args[0], "herdr_action_unsupported")
        self.assertEqual(self.fake.requests, [])


class AuditTests(unittest.TestCase):
    def test_a_line_names_who_where_and_how_much_never_what(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "omodachi" / "herdr-writes.jsonl"
            audit = HerdrWriteAudit(path, clock=lambda: "2026-10-03T00:00:00Z")
            audit.record(device="ios-1", session="omodachi", pane="w1:p1", kind="text", length=11,
                         outcome="ok", submitted=True)
            audit.record(device="ios-1", session="omodachi", pane="w1:p1", kind="keys", length=2,
                         outcome="invalid_key")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            lines = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(lines[0], {"at": "2026-10-03T00:00:00Z", "device": "ios-1", "session": "omodachi",
                                        "pane": "w1:p1", "kind": "text", "length": 11, "outcome": "ok",
                                        "submitted": True})
            self.assertEqual(lines[1]["outcome"], "invalid_key")
            self.assertNotIn("submitted", lines[1])

    def test_it_rolls_once_rather_than_growing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "herdr-writes.jsonl"
            audit = HerdrWriteAudit(path)
            audit.MAX_BYTES = 400
            for _ in range(12):
                audit.record(device="d", session="s", pane="w1:p1", kind="keys", length=1, outcome="ok")
            self.assertLessEqual(path.stat().st_size, 400)
            self.assertTrue(path.with_name("herdr-writes.jsonl.1").exists())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class KeysTextApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = short_temp()
        self.path = os.path.join(self.temp.name, "h.sock")
        self.fake = FakeHerdrSocket(self.path)
        self.herdr = Path(self.temp.name) / "herdr-fake"
        self.herdr.write_text(FAKE_HERDR.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        self.herdr.chmod(self.herdr.stat().st_mode | stat.S_IEXEC)
        self.hub = Hub(auth_check_interval=0.05)
        self.token = self.hub.register_device("phone-a")
        self.other = self.hub.register_device("phone-b")
        self.service = create_service(self.hub, demo=True)
        bridge = HerdrBridge(runner=lambda argv: SNAPSHOT, socket_path=Path(self.path))
        bridge.argv = lambda *arguments: (str(self.herdr), "--session", "omodachi", *arguments)
        bridge.proof_delay = 0.01
        self.bridge = self.service.herdr_bridge = bridge
        self.audit_path = Path(self.temp.name) / "herdr-writes.jsonl"
        self.service.herdr_audit = HerdrWriteAudit(self.audit_path)
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

    async def asyncTearDown(self):
        await self.client.close()
        await asyncio.wait_for(self.server.close(), 3)
        self.fake.close()
        self.temp.cleanup()

    def headers(self, token=None):
        return {"Authorization": "Bearer " + (token or self.token)}

    async def post(self, path, payload, token=None):
        async with self.client.post(self.url + path, headers=self.headers(token), json=payload) as response:
            return response.status, await response.json()

    async def test_keys_route(self):
        async with self.client.post(self.url + "/v1/herdr/panes/w1:p1/keys", json={"keys": ["Enter"]}) as response:
            self.assertEqual(response.status, 401)
        status, body = await self.post("/v1/herdr/panes/w1:p1/keys", {"keys": ["Escape", "ctrl+c"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"contract_revision": "omodachi.v1", "action": "keys", "pane": "w1:p1", "sent": 2})
        self.assertEqual(self.fake.keys, [["Escape", "ctrl+c"]])
        cases = [({"keys": ["PageUp"]}, 400, "herdr_key_unsupported"),
                 ({"keys": ["ab"]}, 400, "invalid_key"),
                 ({"keys": ["a"] * 33}, 413, "herdr_too_many_keys"),
                 ({"keys": []}, 400, "invalid_key"),
                 ({}, 400, "invalid_request"),
                 ({"keys": ["Enter"], "text": "x"}, 400, "invalid_request")]
        for payload, expected, code in cases:
            status, body = await self.post("/v1/herdr/panes/w1:p1/keys", payload)
            self.assertEqual((status, body["error"]["code"]), (expected, code), payload)
        status, body = await self.post("/v1/herdr/panes/w1:p9/keys", {"keys": ["Enter"]})
        self.assertEqual((status, body["error"]["code"]), (404, "herdr_pane_gone"))
        status, body = await self.post("/v1/herdr/panes/w1:t1/keys", {"keys": ["Enter"]})
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_pane"))
        self.assertEqual(self.fake.keys, [["Escape", "ctrl+c"]], "nothing refused reached Herdr")

    async def test_text_route_reports_what_happened(self):
        status, body = await self.post("/v1/herdr/panes/w1:p1/text",
                                       {"text": "secret words in a sentence", "submit": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"contract_revision": "omodachi.v1", "action": "text", "pane": "w1:p1",
                                "delivered": True, "submitted": True, "reason": None, "proof": "tail"})
        self.fake.mode = "dialog"
        status, body = await self.post("/v1/herdr/panes/w1:p1/text", {"text": "another sentence here",
                                                                      "submit": True})
        self.assertEqual((status, body["submitted"], body["reason"]), (200, False, "not_seen"))
        status, body = await self.post("/v1/herdr/panes/w1:p1/text", {"text": "typed only"})
        self.assertEqual((body["submitted"], body["reason"]), (False, "not_requested"))
        for payload, expected, code in (({"text": "x" * (MAX_TEXT_BYTES + 1)}, 413, "herdr_text_too_long"),
                                        ({"text": "a\x1b[A"}, 400, "invalid_text"),
                                        ({"text": "ok", "submit": "yes"}, 400, "invalid_request"),
                                        ({"keys": ["Enter"]}, 400, "invalid_request")):
            status, body = await self.post("/v1/herdr/panes/w1:p1/text", payload)
            self.assertEqual((status, body["error"]["code"]), (expected, code), payload)
        self.assertEqual(self.fake.keys, [["Enter"]], "only the proved send was submitted")

        lines = [json.loads(line) for line in self.audit_path.read_text().splitlines()]
        self.assertEqual(stat.S_IMODE(self.audit_path.stat().st_mode), 0o600)
        self.assertEqual([(line["kind"], line["outcome"], line.get("submitted")) for line in lines[:3]],
                         [("text", "ok", True), ("text", "ok", False), ("text", "ok", False)])
        self.assertEqual(lines[0]["device"], "phone-a")
        self.assertEqual(lines[0]["length"], len("secret words in a sentence"))
        self.assertNotIn("secret", self.audit_path.read_text())
        self.assertEqual([line["outcome"] for line in lines[3:]],
                         ["herdr_text_too_long", "invalid_text", "invalid_request"])

    async def test_a_web_credential_types_like_the_app(self):
        # ODOMO-2 (Leo: Odomo replies like the app): both routes are on the
        # browser gate's allow-list now; `control` still is not.
        for path in ("/v1/herdr/panes/w1:p1/keys", "/v1/herdr/panes/w1:p1/text"):
            self.assertTrue(browser_gate.web_allows("POST", path))
        self.assertFalse(browser_gate.web_allows("GET", "/v1/herdr/panes/w1:p1/control"))

    async def test_takeover_ends_the_other_devices_control_stream(self):
        url = self.url + "/v1/herdr/panes/w1:p1/control?cols=80&rows=24"
        async with self.client.ws_connect(url, headers=self.headers()) as first:
            message = await asyncio.wait_for(first.receive(), 5)
            self.assertEqual(json.loads(message.data)["type"], "terminal.frame")
            with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
                await self.client.ws_connect(url, headers=self.headers(self.other))
            self.assertEqual(caught.exception.status, 409)
            async with self.client.ws_connect(url + "&takeover=1", headers=self.headers(self.other)) as second:
                frame = await asyncio.wait_for(second.receive(), 5)
                self.assertEqual(json.loads(frame.data)["type"], "terminal.frame")
                ended = await asyncio.wait_for(first.receive(), 5)
                while ended.type is aiohttp.WSMsgType.TEXT:
                    ended = await asyncio.wait_for(first.receive(), 5)
                self.assertIs(ended.type, aiohttp.WSMsgType.CLOSE)
                self.assertEqual(ended.extra, "taken_over")
                held = self.bridge._controllers["w1:p1"][0]
                await asyncio.sleep(0.2)
                self.assertEqual(self.bridge._controllers["w1:p1"][0], held,
                                 "the evicted stream's cleanup does not free the new holder's pane")


if __name__ == "__main__":
    unittest.main()
