"""ODOMO-2: Odomo's Herdr and Agent panels reply like the App.

The browser gate (`browser_gate.WEB_ALLOWED`) is an allow-list, so the one
thing that must never happen is a route reaching a web credential without
anyone having decided it should. `RouteReviewTests` holds every route the
daemon registers against a table that says, for each, what a web credential
gets - a route added later fails here until somebody writes its row.

The rest is what the new web paths do for a web device: they work, they are
audited with the device that made them, and the two that move the computer's
screen (`pane.focus`, `workspace focus`) and the read-write `control` stream
stay the App's.
"""
from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sys
import unittest

import aiohttp

from omodachi_core import browser_gate
from omodachi_core.bootstrap import create_service
from omodachi_core.herdr_bridge import HerdrWriteAudit
from omodachi_core.hub import Hub
from omodachi_core.network import create_app

sys.path.insert(0, str(Path(__file__).parent))
import test_herdr_bridge  # noqa: E402


# (method, canonical route) -> [(sample path, web allowed?)]. Every route the
# daemon registers is here; anything not marked True is refused to a browser.
REVIEWED = {
    ("GET", "/"): [("/", False)],
    ("GET", "/health"): [("/health", False)],
    ("GET", "/odomo"): [("/odomo", False)],
    ("GET", "/odomo/"): [("/odomo/", False)],
    ("GET", "/odomo/{path}"): [("/odomo/sw.js", False)],
    ("GET", "/lite"): [("/lite", False)],
    ("GET", "/lite/{path}"): [("/lite/x", False)],
    ("GET", "/favicon.ico"): [("/favicon.ico", False)],
    ("GET", "/{name}"): [("/apple-touch-icon.png", False)],
    ("POST", "/v1/ws-ticket"): [("/v1/ws-ticket", True)],
    ("GET", "/v1/push/vapid-public-key"): [("/v1/push/vapid-public-key", True)],
    ("POST", "/v1/push/subscriptions"): [("/v1/push/subscriptions", True)],
    ("DELETE", "/v1/push/subscriptions"): [("/v1/push/subscriptions", True)],
    ("GET", "/v1/notifications/prefs"): [("/v1/notifications/prefs", True)],
    ("PUT", "/v1/notifications/prefs"): [("/v1/notifications/prefs", True)],
    ("PUT", "/v1/presence"): [("/v1/presence", False)],
    ("GET", "/v1/events"): [("/v1/events", True)],
    # pairing is unauthenticated (the boundary's open paths), not the list's
    ("POST", "/v1/pairing/requests"): [("/v1/pairing/requests", False)],
    ("POST", "/v1/pairing/requests/{id}/claim"): [("/v1/pairing/requests/pair_x/claim", False)],
    ("GET", "/v1/pairing/credential"): [("/v1/pairing/credential", True)],
    ("POST", "/v1/pairing/renew"): [("/v1/pairing/renew", True)],
    ("POST", "/v1/actions/{id}:invoke"): [("/v1/actions/system.lock:invoke", True)],
    ("POST", "/v1/control/wake"): [("/v1/control/wake", False)],
    ("GET", "/v1/shortcuts"): [("/v1/shortcuts", True)],
    # LINKS-1: Odomo's "Open on the computer", and the host's own names for
    # rewriting a `localhost` link (the web claim already carries them)
    ("GET", "/v1/host/roads"): [("/v1/host/roads", True)],
    ("POST", "/v1/host/open"): [("/v1/host/open", True)],
    ("POST", "/v1/workspaces/{id}/select"): [("/v1/workspaces/3/select", True)],
    ("POST", "/v1/workspaces/relative/{step}/select"): [("/v1/workspaces/relative/e+1/select", True)],
    ("POST", "/v1/agent/tasks"): [("/v1/agent/tasks", False)],
    ("POST", "/v1/agent/default:ensure"): [("/v1/agent/default:ensure", True)],
    ("GET", "/v1/agent/default/chat/commands"): [("/v1/agent/default/chat/commands", True)],
    ("POST", "/v1/agent/default/chat/commands/{command}:execute"): [
        ("/v1/agent/default/chat/commands/codex.model:execute", True)],
    ("POST", "/v1/agent/default/chat/recover-empty"): [("/v1/agent/default/chat/recover-empty", False)],
    ("POST", "/v1/agent/default/chat/handoff:prepare"): [("/v1/agent/default/chat/handoff:prepare", True)],
    ("POST", "/v1/agent/default/chat/handoff:confirm"): [("/v1/agent/default/chat/handoff:confirm", True)],
    # AGENT-NEW-1: Odomo's "New conversation", the App's
    ("POST", "/v1/agent/default/chat:new"): [("/v1/agent/default/chat:new", True)],
    ("GET", "/v1/agent/default/chat"): [("/v1/agent/default/chat", True)],
    ("GET", "/v1/agent/default/chat/events"): [("/v1/agent/default/chat/events", True)],
    ("POST", "/v1/agent/default/chat/messages"): [("/v1/agent/default/chat/messages", True)],
    ("POST", "/v1/agent/default/chat/interrupt"): [("/v1/agent/default/chat/interrupt", True)],
    ("POST", "/v1/agent/default/chat/steer"): [("/v1/agent/default/chat/steer", True)],
    # AGENT-PLAN-1: Odomo's Plan chip, the App's
    ("POST", "/v1/agent/default/chat/mode"): [("/v1/agent/default/chat/mode", True)],
    # AGENT-QUEUE-1: Odomo's queue and "Earlier messages", the App's
    ("POST", "/v1/agent/default/chat/queue"): [("/v1/agent/default/chat/queue", True)],
    ("POST", "/v1/agent/default/chat/queue/{queue_id}:{action}"): [
        ("/v1/agent/default/chat/queue/01a11330-e90f:send", True),
        ("/v1/agent/default/chat/queue/01a11330-e90f:edit", True),
        ("/v1/agent/default/chat/queue/01a11330-e90f:delete", True)],
    ("GET", "/v1/agent/default/chat/history"): [("/v1/agent/default/chat/history", True)],
    ("GET", "/v1/agent/default/chat/approvals"): [("/v1/agent/default/chat/approvals", True)],
    ("POST", "/v1/agent/default/chat/approvals/{request_id}"): [("/v1/agent/default/chat/approvals/42", True)],
    ("GET", "/v1/agent/default/chat/usage"): [("/v1/agent/default/chat/usage", True)],
    ("GET", "/v1/agent/default/models"): [("/v1/agent/default/models", True)],
    ("POST", "/v1/media/pairing/discover"): [("/v1/media/pairing/discover", False)],
    ("POST", "/v1/media/pairing/requests"): [("/v1/media/pairing/requests", False)],
    ("GET", "/v1/media/pairing/requests/{attempt_id}"): [("/v1/media/pairing/requests/a", False)],
    ("DELETE", "/v1/media/pairing/requests/{attempt_id}"): [("/v1/media/pairing/requests/a", False)],
    ("GET", "/v1/audio/input"): [("/v1/audio/input", False)],
    ("GET", "/v1/voice/capabilities"): [("/v1/voice/capabilities", False)],
    ("GET", "/v1/voice/uplink"): [("/v1/voice/uplink", False)],
    ("POST", "/v1/voice/dictation:{operation}"): [("/v1/voice/dictation:start", False)],
    ("GET", "/v1/notifications"): [("/v1/notifications", True)],
    ("GET", "/v1/notifications/dnd"): [("/v1/notifications/dnd", True)],
    ("POST", "/v1/notifications/dnd"): [("/v1/notifications/dnd", True)],
    ("POST", "/v1/notifications/{id}:{action}"): [("/v1/notifications/1-2:dismiss", True),
                                                   ("/v1/notifications/1-2:invoke", True)],
    ("GET", "/v1/remote/sessions/{id}/audio"): [("/v1/remote/sessions/x/audio", False)],
    ("GET", "/v1/remote/sessions/{id}/vnc"): [("/v1/remote/sessions/x/vnc", False)],
    ("GET", "/v1/herdr/layout"): [("/v1/herdr/layout", True)],
    ("GET", "/v1/herdr/sessions"): [("/v1/herdr/sessions", True)],
    ("GET", "/v1/herdr/operator-rows"): [("/v1/herdr/operator-rows", True)],
    ("POST", "/v1/herdr/sessions/{name}/select"): [("/v1/herdr/sessions/work/select", True)],
    ("POST", "/v1/herdr/sessions/{name}/start"): [("/v1/herdr/sessions/work/start", True)],
    ("POST", "/v1/herdr/sessions"): [("/v1/herdr/sessions", True)],
    # HERDR-6: Odomo's "New workspace", the App's (always --no-focus)
    ("POST", "/v1/herdr/sessions/{name}/workspaces"): [("/v1/herdr/sessions/work/workspaces", True),
                                                       ("/v1/herdr/sessions/../workspaces", False)],
    ("GET", "/v1/herdr/folders"): [("/v1/herdr/folders", True), ("/v1/herdr/folders/x", False)],
    # HERDR-11: Odomo's card stars folders too, and shows a pane's latest reply.
    ("POST", "/v1/herdr/folders/favourites"): [("/v1/herdr/folders/favourites", True),
                                               ("/v1/herdr/folders/favourites/x", False)],
    ("GET", "/v1/herdr/panes/{pane}/reply"): [("/v1/herdr/panes/w1:p1/reply", True),
                                              ("/v1/herdr/panes/w1:p1/reply/x", False)],
    # observe only: `control` (and its takeover) is the App's alone
    ("GET", "/v1/herdr/panes/{pane}/{mode}"): [("/v1/herdr/panes/w1:p1/observe", True),
                                                ("/v1/herdr/panes/w1:p1/control", False)],
    # never `focus`: it moves the computer's own pane, tab and workspace
    ("POST", "/v1/herdr/panes/{pane}/{action}"): [("/v1/herdr/panes/w1:p1/split", True),
                                                   ("/v1/herdr/panes/w1:p1/zoom", True),
                                                   ("/v1/herdr/panes/w1:p1/close", True),
                                                   ("/v1/herdr/panes/w1:p1/focus", False)],
    ("POST", "/v1/herdr/panes/{pane}/keys"): [("/v1/herdr/panes/w1:p1/keys", True)],
    ("POST", "/v1/herdr/panes/{pane}/text"): [("/v1/herdr/panes/w1:p1/text", True)],
    ("POST", "/v1/herdr/workspaces/{id}/select"): [("/v1/herdr/workspaces/w1/select", False)],
    ("POST", "/v1/herdr/workspaces/{id}/tabs"): [("/v1/herdr/workspaces/w1/tabs", True)],
    ("DELETE", "/v1/herdr/workspaces/{id}/tabs/{tab}"): [("/v1/herdr/workspaces/w1/tabs/w1:t2", True)],
    ("GET", "/v1/theme"): [("/v1/theme", True)],
    ("GET", "/v1/theme/background"): [("/v1/theme/background", True)],
    ("GET", "/v1/fonts"): [("/v1/fonts", True)],
    ("GET", "/v1/fonts/{id}"): [("/v1/fonts/mono", True)],
    ("GET", "/v1/icons/{name}"): [("/v1/icons/firefox", True)],
    ("GET", "/v1/auth/keys"): [("/v1/auth/keys", False)],
    ("POST", "/v1/auth/keys"): [("/v1/auth/keys", False)],
    ("DELETE", "/v1/auth/keys"): [("/v1/auth/keys", False)],
    ("POST", "/v1/auth/keys/enabled"): [("/v1/auth/keys/enabled", False)],
    ("GET", "/v1/ssh/key"): [("/v1/ssh/key", False)],
    ("PUT", "/v1/ssh/key"): [("/v1/ssh/key", False)],
    ("GET", "/v1/auth/approvals"): [("/v1/auth/approvals", False)],
    ("POST", "/v1/auth/approvals/{id}"): [("/v1/auth/approvals/appr_" + "0" * 32, False)],
    ("GET", "/v1/preferences"): [("/v1/preferences", False)],
    # ATTACH-1: a file for the Agent or a Herdr pane, and taking it back
    ("POST", "/v1/uploads"): [("/v1/uploads", True)],
    ("DELETE", "/v1/uploads/{id}"): [("/v1/uploads/" + "a" * 32, True), ("/v1/uploads/x", False)],
    ("GET", "/v1/clipboard"): [("/v1/clipboard", False)],
    ("PUT", "/v1/clipboard"): [("/v1/clipboard", False)],
    ("GET", "/v1/remote/capabilities"): [("/v1/remote/capabilities", False)],
    ("POST", "/v1/remote/sessions"): [("/v1/remote/sessions", False)],
    ("GET", "/v1/remote/sessions/{id}"): [("/v1/remote/sessions/x", False)],
    ("DELETE", "/v1/remote/sessions/{id}"): [("/v1/remote/sessions/x", False)],
    ("POST", "/v1/remote/sessions/{id}/{operation}"): [("/v1/remote/sessions/x/resize", False)],
    # the generic read: the menu's three, and Herdr's summary
    ("GET", "/v1/{name}"): [("/v1/state", True), ("/v1/capabilities", True), ("/v1/catalog", True),
                            ("/v1/herdr", True), ("/v1/agent", False), ("/v1/media", False)],
}

# ODOMO-2's additions, each by method: the diff Leo reads in the report.
ODOMO2 = [
    ("GET", "/v1/herdr/operator-rows"), ("POST", "/v1/herdr/sessions"),
    ("POST", "/v1/herdr/sessions/work/select"), ("POST", "/v1/herdr/sessions/work/start"),
    ("POST", "/v1/herdr/panes/w1:p1/split"), ("POST", "/v1/herdr/panes/w1:p1/zoom"),
    ("POST", "/v1/herdr/panes/w1:p1/close"), ("POST", "/v1/herdr/panes/w1:p1/keys"),
    ("POST", "/v1/herdr/panes/w1:p1/text"), ("POST", "/v1/herdr/workspaces/w1/tabs"),
    ("DELETE", "/v1/herdr/workspaces/w1/tabs/w1:t2"),
    ("POST", "/v1/agent/default:ensure"), ("POST", "/v1/agent/default/chat/messages"),
    ("POST", "/v1/agent/default/chat/steer"), ("POST", "/v1/agent/default/chat/interrupt"),
    ("POST", "/v1/agent/default/chat/approvals/42"),
    ("POST", "/v1/agent/default/chat/commands/codex.model:execute"),
    ("POST", "/v1/agent/default/chat/handoff:prepare"), ("POST", "/v1/agent/default/chat/handoff:confirm"),
]


class RouteReviewTests(unittest.TestCase):
    def test_every_route_the_daemon_has_is_reviewed_for_the_browser(self):
        app = create_app(create_service(Hub(), demo=True))
        registered = {(route.method, route.resource.canonical) for route in app.router.routes()
                      if route.method != "HEAD"}
        self.assertEqual(registered - set(REVIEWED), set(), "a new route needs its row in REVIEWED")
        self.assertEqual(set(REVIEWED) - registered, set(), "a row for a route that is gone")

    def test_the_allow_list_says_exactly_what_the_review_says(self):
        for (method, _), samples in REVIEWED.items():
            for path, allowed in samples:
                self.assertEqual(browser_gate.web_allows(method, path), allowed, (method, path))

    def test_odomo2_adds_these_and_nothing_wider(self):
        for method, path in ODOMO2:
            self.assertTrue(browser_gate.web_allows(method, path), (method, path))
        # the method is part of the row
        for method, path in ODOMO2:
            other = "GET" if method != "GET" else "POST"
            if (other, path) == ("GET", "/v1/herdr/sessions"):
                continue  # the listing, a read since LITE-1a
            self.assertFalse(browser_gate.web_allows(other, path), (other, path))
        for path in ("/v1/herdr/panes/w1:p1/focus", "/v1/herdr/panes/w1:p1/control",
                     "/v1/herdr/workspaces/w1/select", "/v1/herdr/panes/w1:p1/keys/x",
                     "/v1/herdr/sessions/../start", "/v1/herdr/sessions/-x/start",
                     "/v1/agent/default/chat/recover-empty", "/v1/agent/tasks",
                     "/v1/agent/default/chat/messages/x", "/v1/agent/other/chat/messages",
                     "/v1/agent/default/chat/commands/a/b:execute"):
            self.assertFalse(browser_gate.web_allows("POST", path), path)
            self.assertFalse(browser_gate.web_allows("GET", path), path)

    def test_a_ticket_is_still_only_for_observe(self):
        self.assertTrue(browser_gate.ticket_path("/v1/herdr/panes/w1:p1/observe"))
        self.assertFalse(browser_gate.web_allows("GET", "/v1/herdr/panes/w1:p1/control"))


class WebHerdrWriteTests(test_herdr_bridge.HerdrApiTests):
    """A web credential's writes into Herdr, through the real router."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.web = self.hub.auth.issue("odomo-web1", role="web").token
        self.service.herdr_audit = HerdrWriteAudit()

    def web_headers(self):
        return {"Authorization": "Bearer " + self.web}

    async def test_pane_actions_and_tabs_work_and_carry_the_web_device(self):
        for path, payload in (("/v1/herdr/panes/w1:p1/split", {"direction": "right"}),
                              ("/v1/herdr/panes/w1:p1/zoom", {"mode": "toggle"}),
                              ("/v1/herdr/workspaces/w1/tabs", {"focus": False})):
            async with self.client.post(self.url + path, headers=self.web_headers(), json=payload) as response:
                self.assertEqual(response.status, 200, (path, await response.text()))
        async with self.client.delete(self.url + "/v1/herdr/workspaces/w1/tabs/w1:t2",
                                      headers=self.web_headers()) as response:
            self.assertEqual(response.status, 200, await response.text())
        lines = self.service.herdr_audit.lines
        self.assertEqual([(line["device"], line["kind"], line["pane"], line["outcome"]) for line in lines],
                         [("odomo-web1", "split", "w1:p1", "ok"), ("odomo-web1", "zoom", "w1:p1", "ok"),
                          ("odomo-web1", "tab_create", "w1", "ok"), ("odomo-web1", "tab_close", "w1:t2", "ok")])

    async def test_the_two_that_move_the_computer_and_control_are_refused(self):
        calls = len(self.calls)
        for method, path in (("POST", "/v1/herdr/panes/w1:p1/focus"), ("POST", "/v1/herdr/workspaces/w1/select")):
            async with self.client.request(method, self.url + path, headers=self.web_headers(),
                                           json={}) as response:
                self.assertEqual(response.status, 403, path)
                self.assertEqual((await response.json())["error"]["code"], "web_client_forbidden")
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            await self.client.ws_connect(self.url + "/v1/herdr/panes/w1:p1/control?cols=80&rows=24",
                                         headers=self.web_headers())
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(len(self.calls), calls, "nothing reached Herdr")
        self.assertEqual(self.service.herdr_audit.lines, [])

    async def test_observe_streams_for_a_web_device(self):
        url = self.url + "/v1/herdr/panes/w1:p1/observe?cols=80&rows=24"
        async with self.client.ws_connect(url, headers=self.web_headers()) as ws:
            frames = await self.frames(ws, 1)
        self.assertEqual(frames[0]["type"], "terminal.frame")

    async def test_a_failed_action_is_audited_with_its_reason(self):
        async with self.client.post(self.url + "/v1/herdr/panes/w1:p1/split", headers=self.web_headers(),
                                    json={"direction": "sideways"}) as response:
            self.assertEqual(response.status, 400)
        line = self.service.herdr_audit.lines[-1]
        self.assertEqual((line["device"], line["kind"], line["outcome"]), ("odomo-web1", "split", "invalid_request"))


class WebAgentJournalTests(unittest.IsolatedAsyncioTestCase):
    """A web device's agent writes are on the journal with the device."""

    async def asyncSetUp(self):
        from omodachi_core.network import NetworkServer
        self.hub = Hub(auth_check_interval=0.05)
        self.service = create_service(self.hub, demo=True)
        self.web = self.hub.auth.issue("odomo-web2", role="web").token
        self.native = self.hub.register_device("phone-a")
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))

    async def asyncTearDown(self):
        await self.client.close()
        await asyncio.wait_for(self.server.close(), 3)

    async def post(self, token, path, body):
        out = io.StringIO()
        with redirect_stdout(out):
            async with self.client.post(self.url + path, json=body,
                                        headers={"Authorization": "Bearer " + token}) as response:
                status = response.status
        lines = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
        return status, [line for line in lines if line.get("omodachi") == "web"]

    async def test_a_web_send_is_journalled_without_its_words(self):
        status, lines = await self.post(self.web, "/v1/agent/default/chat/messages",
                                        {"request_id": "r1", "text": "a secret sentence"})
        self.assertNotIn(status, (401, 403))
        self.assertEqual(len(lines), 1)
        self.assertEqual({key: lines[0][key] for key in ("event", "device", "route", "status")},
                         {"event": "agent.write", "device": "odomo-web2",
                          "route": "/v1/agent/default/chat/messages", "status": status})
        self.assertNotIn("secret", json.dumps(lines))

    async def test_the_app_s_own_writes_are_not_journalled_here(self):
        _, lines = await self.post(self.native, "/v1/agent/default/chat/messages", {"request_id": "r2", "text": "x"})
        self.assertEqual(lines, [])

    async def test_a_refused_web_route_is_not_reached(self):
        status, lines = await self.post(self.web, "/v1/agent/tasks", {"request_id": "r3", "text": "x"})
        self.assertEqual(status, 403)
        self.assertEqual(lines[0]["status"], 403)


if __name__ == "__main__":
    unittest.main()
