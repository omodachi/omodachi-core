"""NOTIFY-2: what quiets a push, what is watched, and where a push points.

* `PUT /v1/presence` - the App says whether it is in front and which kinds it
  alerts by itself; Odomo is held back only for those kinds, only then.
* Every running Herdr session is watched for `blocked` from daemon start, and
  a session that stops is let go with an empty `blocked`.
* `navigate` carries the host id, so Odomo can link the App to the same place.
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import aiohttp

from omodachi_core import push, webpush
from omodachi_core.bootstrap import create_service
from omodachi_core.herdr_bridge import HerdrBridge, HerdrSessions, HerdrUnavailable
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
from omodachi_core import service as service_module

ROOT = Path(__file__).parents[1]
SNAPSHOT = json.loads((ROOT / "contracts/fixtures/herdr/snapshot.json").read_text())
APPLE = "https://web.push.apple.com/QGuQyavXutnMH"
FCM = "https://fcm.googleapis.com/fcm/send/abc:def"
HOST_ID = "ab" * 16
NATIVE_KINDS = ["agent_approval", "agent_needs_input", "herdr_blocked", "host_notification"]


def snapshot(blocked=()):
    """The recorded 0.8.2 snapshot, with the named panes' agents blocked."""
    value = copy.deepcopy(SNAPSHOT)
    inner = value["snapshot"]
    panes = {row["pane_id"]: row for row in inner["panes"]}
    for pane in blocked:
        if pane not in panes:
            row = copy.deepcopy(inner["panes"][0])
            row.update({"pane_id": pane, "focused": False})
            inner["panes"].append(row)
            panes[pane] = row
        panes[pane]["agent_status"] = "blocked"
        inner["agents"].append({"name": "claude", "kind": "claude", "status": "blocked", "pane_id": pane})
    return value


def browser_keys():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    import os
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)
    return {"p256dh": webpush.b64u(public), "auth": webpush.b64u(os.urandom(16))}


class PresenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = Hub(auth_check_interval=0.05)
        self.ipad = self.hub.register_device("ipad")
        self.service = create_service(self.hub, demo=True)

    async def listen(self, device):
        """A live `/v1/events` subscription for `device` while the test runs."""
        stream = self.hub.subscribe(device_id=device)
        task = asyncio.create_task(stream.__anext__())
        await asyncio.sleep(0)
        self.addAsyncCleanup(self._close, task, stream)

    @staticmethod
    async def _close(task, stream):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await stream.aclose()

    async def test_a_kind_is_quiet_only_while_the_app_is_in_front_alerting_it(self):
        self.service.set_presence("ipad", {"foreground": True, "alerts": ["herdr_blocked"]})
        self.assertFalse(self.service.native_alerts("herdr_blocked"))   # no live stream: not there
        await self.listen("ipad")
        self.assertTrue(self.service.native_alerts("herdr_blocked"))
        self.assertFalse(self.service.native_alerts("agent_done"))       # not a kind it alerts
        self.service.set_presence("ipad", {"foreground": False, "alerts": ["herdr_blocked"]})
        self.assertFalse(self.service.native_alerts("herdr_blocked"))   # in the background

    async def test_an_app_that_never_said_anything_quiets_nothing(self):
        await self.listen("ipad")
        self.assertTrue(self.service.native_connected())                 # LITE-1b's reading
        for kind in push.KINDS:
            self.assertFalse(self.service.native_alerts(kind))

    async def test_the_reply_says_what_is_now_held_back_and_a_revoke_forgets_it(self):
        value = self.service.set_presence("ipad", {"foreground": True, "alerts": NATIVE_KINDS})
        self.assertEqual(value["quiets"], sorted(NATIVE_KINDS))
        self.assertEqual(value["contract_revision"], "omodachi.v1")
        self.assertEqual(self.service.set_presence("ipad", {"foreground": False, "alerts": NATIVE_KINDS})["quiets"], [])
        self.service._forget_push("ipad")
        self.assertNotIn("ipad", self.service._presence)

    async def test_only_well_formed_presence_from_a_native_client(self):
        for payload in ({}, {"foreground": "yes"}, {"foreground": True, "alerts": "herdr_blocked"},
                        {"foreground": True, "alerts": ["herdr_blocked", "herdr_blocked"]},
                        {"foreground": True, "alerts": ["sms"]}, {"foreground": True, "extra": 1}, []):
            with self.assertRaises(service_module.ServiceError, msg=repr(payload)) as caught:
                self.service.set_presence("ipad", payload)
            self.assertEqual(caught.exception.code, "invalid_request")
        self.hub.auth.device_role = lambda device: "web"
        with self.assertRaises(service_module.ServiceError) as caught:
            self.service.set_presence("ipad", {"foreground": True})
        self.assertEqual((caught.exception.code, caught.exception.status), ("presence_native_only", 403))


class PresenceApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = Hub(auth_check_interval=0.05)
        self.token = self.hub.register_device("ipad")
        self.service = create_service(self.hub, demo=True)
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.url = f"http://127.0.0.1:{self.server.bound_port}/v1/presence"
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))

    async def asyncTearDown(self):
        await self.client.close()
        await asyncio.wait_for(self.server.close(), 3)

    async def test_put_presence(self):
        headers = {"Authorization": "Bearer " + self.token}
        async with self.client.put(self.url, json={"foreground": True, "alerts": ["herdr_blocked"]}) as response:
            self.assertEqual(response.status, 401)
        async with self.client.put(self.url, headers=headers,
                                   json={"foreground": True, "alerts": ["herdr_blocked"]}) as response:
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual((await response.json())["quiets"], ["herdr_blocked"])
        async with self.client.put(self.url + "?x=1", headers=headers, json={"foreground": True}) as response:
            self.assertEqual(response.status, 400)
        async with self.client.put(self.url, headers=headers, json={"foreground": 1}) as response:
            self.assertEqual(response.status, 400)
        async with self.client.get(self.url, headers=headers) as response:
            self.assertIn(response.status, (404, 405))

    def test_a_web_credential_never_reaches_it(self):
        from omodachi_core.browser_gate import web_allows
        self.assertFalse(web_allows("PUT", "/v1/presence"))


class FakeSend:
    def __init__(self):
        self.sent = []

    def __call__(self, subscription, body, vapid, **options):
        self.sent.append({"endpoint": subscription["endpoint"], "body": json.loads(body)})
        return {"status": 201}


class QuietPerKindTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub = Hub()
        self.store = push.PushStore(Path(self.temp.name) / "push-subscriptions.json")
        self.store.subscribe("odomo-iphone", {"endpoint": APPLE, "keys": browser_keys()})
        self.alerting: set[str] = set()
        self.send = FakeSend()
        self.now = [1000.0]
        self.sender = push.PushSender(self.hub, self.store, lambda: webpush.Vapid.generate(),
                                      origin=lambda: "https://omodachi-lab.example.ts.net",
                                      host_name=lambda: "omodachi-lab", host_id=lambda: HOST_ID,
                                      native_alerts=lambda kind: kind in self.alerting,
                                      send=self.send, clock=lambda: self.now[0], debounce=30.0,
                                      sleep=lambda seconds: None, log=lambda line: None,
                                      herdr_delay=0)
        self.sender.watcher.started_ms = 0
        self.sender.start()

    async def asyncTearDown(self):
        await self.sender.stop()
        self.temp.cleanup()

    async def settle(self):
        for _ in range(3):
            await asyncio.sleep(0)
        await self.sender.drain()

    def kinds(self):
        return [row["body"]["notification"]["title"] for row in self.send.sent if "web_push" in row["body"]]

    async def block(self, session, revision, panes):
        self.hub.publish("herdr.layout.changed", {"session": session, "revision": revision,
                                                  "blocked": [{"pane_id": p, "name": "claude"} for p in panes]})
        await self.settle()
        await asyncio.sleep(0.01)   # NOTIFY-1: the Herdr digest leaves after its (here zero) delay
        await self.settle()

    async def test_the_app_alerting_herdr_holds_back_herdr_and_nothing_else(self):
        self.alerting = {"herdr_blocked", "agent_approval", "agent_needs_input"}
        await self.block("default", 1, [])
        await self.block("default", 2, ["w1:p1"])
        self.assertEqual(self.send.sent, [])                         # the App said it
        self.hub.update_state({"agent": {"status": "working"}}, event_type="agent.changed")
        self.hub.update_state({"agent": {"status": "done"}}, event_type="agent.changed")
        await self.settle()
        self.assertEqual(self.kinds(), ["The agent is done"])        # the App never says this one

    async def test_with_the_app_in_the_background_everything_goes(self):
        await self.block("default", 1, [])
        await self.block("default", 2, ["w1:p1"])
        self.assertEqual(self.kinds(), ["claude needs you"])

    async def test_a_coalesced_push_is_checked_again_when_its_window_ends(self):
        self.hub.update_state({"agent": {"status": "waiting_on_approval"}}, event_type="agent.changed")
        await self.settle()
        self.hub.update_state({"agent": {"status": "working"}}, event_type="agent.changed")
        self.hub.update_state({"agent": {"status": "waiting_on_approval"}}, event_type="agent.changed")
        await self.settle()
        self.assertEqual(len(self.kinds()), 1)                       # the second waits out the window
        self.alerting = {"agent_approval"}                           # …while the App comes to the front
        key = next(key for key, window in self.sender._windows.items() if window["timer"])
        self.sender._windows[key]["timer"].cancel()
        self.sender._flush(key)
        await self.settle()
        self.assertEqual(len(self.kinds()), 1)

    async def test_the_push_points_at_this_host_and_the_pane(self):
        await self.block("work", 1, [])
        await self.block("work", 2, ["w2:p3"])
        navigate = self.send.sent[0]["body"]["notification"]["navigate"]
        self.assertEqual(navigate, "https://omodachi-lab.example.ts.net/odomo/?open=herdr&session=work"
                                   "&pane=w2%3Ap3&agent=claude&host=" + HOST_ID)

    def test_only_a_host_id_is_ever_put_in_the_link(self):
        signal = push.Signal("agent_approval", "agent", "agent", panel="agent", ids={"ref": "approval"})
        for value in (None, "", "ABAB" * 8, "../x", "a" * 33):
            self.assertNotIn("host=", push.navigate_url("https://h.ts.net", signal, value))
        self.assertTrue(push.navigate_url("https://h.ts.net", signal, HOST_ID).endswith("&host=" + HOST_ID))


class LiveSnapshotShapeTests(unittest.TestCase):
    def test_a_live_0_8_2_agent_row_is_named_in_the_blocked_list(self):
        # Verbatim shape from `herdr api snapshot` on the lab VM (herdr 0.8.2).
        value = copy.deepcopy(SNAPSHOT)
        value["snapshot"]["panes"][0]["agent_status"] = "blocked"
        value["snapshot"]["agents"] = [{"agent": "codex", "agent_status": "blocked", "pane_id": "w1:p1",
                                        "tab_id": "w1:t1", "workspace_id": "w1"}]
        layout = HerdrBridge("probe", runner=lambda argv: value).layout()
        self.assertEqual(service_module.blocked_panes(layout), [{"pane_id": "w1:p1", "name": "codex"}])


class WatchEverySessionTests(unittest.TestCase):
    """Every running session is watched from the first poll, with no device involved."""

    def setUp(self):
        self.hub = Hub()
        self.service = create_service(self.hub, demo=True)
        self.snapshots = {"omodachi": snapshot(), "default": snapshot(), "work": snapshot()}
        self.listing = {"sessions": [
            {"name": "omodachi", "running": True, "default": False},
            {"name": "default", "running": True, "default": True},
            {"name": "work", "running": True, "default": False},
            {"name": "old", "running": False, "default": False}]}
        self.listings = 0

        def listing(argv):
            self.listings += 1
            if isinstance(self.listing, Exception):
                raise self.listing
            return self.listing
        self.service._herdr_sessions = HerdrSessions(runner=listing)
        self.service.herdr_bridge = self.bridge("omodachi")
        self.published = []
        original = self.hub.publish
        def publish(topic, payload, **kwargs):
            self.published.append((topic, copy.deepcopy(payload)))
            return original(topic, payload, **kwargs)
        self.hub.publish = publish
        patcher = mock.patch("omodachi_core.herdr_bridge.HerdrBridge",
                             lambda name, socket_path=None: self.bridge(name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def bridge(self, name):
        def runner(argv):
            value = self.snapshots.get(name)
            if value is None:
                raise HerdrUnavailable("herdr_unavailable")
            return value
        return HerdrBridge(name, runner=runner)

    def poll(self, now):
        with mock.patch.object(service_module.time, "monotonic", return_value=now):
            return self.service.notify_herdr_layout()

    def events(self, session):
        return [payload for topic, payload in self.published if payload.get("session") == session]

    def test_a_session_nobody_opened_is_watched_from_the_first_poll(self):
        self.poll(100.0)
        self.assertEqual(sorted(self.service._herdr_bridges), ["default", "work"])   # "old" is not running
        self.assertEqual(len(self.events("work")), 1)                                # the baseline
        self.snapshots["work"] = snapshot(blocked=["w1:p1"])
        # HERDR-11 §4: a session no device is looking at is read every ten
        # seconds (Herdr's own events poke it sooner when they are up).
        self.poll(102.0)
        self.assertEqual(self.events("work")[-1]["blocked"], [])
        self.poll(110.0)
        self.assertEqual(self.events("work")[-1]["blocked"], [{"pane_id": "w1:p1", "name": "claude"}])

    def test_a_client_that_connects_now_reads_what_is_blocked_as_its_first_look(self):
        self.snapshots["work"] = snapshot(blocked=["w1:p1"])
        self.poll(100.0)
        blocked = self.service.state("phone")["herdr"]["blocked"]
        self.assertEqual(blocked["work"], [{"pane_id": "w1:p1", "name": "claude"}])
        self.assertEqual(blocked["default"], [])
        self.listing["sessions"][2]["running"] = False
        self.snapshots.pop("work")
        self.poll(111.0)
        self.assertNotIn("work", self.service.state("phone")["herdr"]["blocked"])
        self.assertNotIn("blocked", self.hub.state_view("herdr")["herdr"] or {}, "the hub's own state is not touched")

    def test_the_listing_is_read_on_its_own_slower_clock(self):
        self.poll(100.0)
        self.poll(102.0)
        self.poll(104.0)
        self.assertEqual(self.listings, 1)
        self.poll(100.0 + service_module.HERDR_WATCH_LISTING_SECONDS)
        self.assertEqual(self.listings, 2)

    def test_a_session_that_stops_is_let_go_and_says_nothing_is_blocked(self):
        self.snapshots["work"] = snapshot(blocked=["w1:p1"])
        self.poll(100.0)
        self.listing["sessions"][2]["running"] = False
        self.snapshots.pop("work")
        result = self.poll(111.0)
        self.assertNotIn("work", self.service._herdr_bridges)
        self.assertEqual(self.events("work")[-1], {"revision": 1, "session": "work", "blocked": [], "stopped": True})
        self.assertIn("work", result["sessions"])
        self.poll(122.0)
        self.assertEqual(len(self.events("work")), 2)                                 # and then nothing

    def test_a_listing_that_fails_drops_nothing_and_the_owned_session_stays(self):
        self.poll(100.0)
        self.listing = HerdrUnavailable("herdr_unavailable")
        self.poll(111.0)
        self.assertEqual(sorted(self.service._herdr_bridges), ["default", "work"])
        self.listing = {"sessions": []}
        self.poll(122.0)
        self.assertEqual(self.service._herdr_bridges, {})
        self.assertEqual(self.service.herdr_bridge.session, "omodachi")

    def test_a_blocked_pane_in_an_unopened_session_reaches_a_push(self):
        sent = []

        async def run():
            temp = tempfile.TemporaryDirectory()
            store = push.PushStore(Path(temp.name) / "push.json")
            store.subscribe("odomo", {"endpoint": FCM, "keys": browser_keys()})
            sender = push.PushSender(self.hub, store, lambda: webpush.Vapid.generate(),
                                     origin=lambda: "https://lab.example.ts.net", host_name=lambda: "lab",
                                     host_id=lambda: HOST_ID, native_alerts=lambda kind: False,
                                     send=lambda sub, body, vapid, **o: sent.append(json.loads(body)) or {"status": 201},
                                     sleep=lambda s: None, log=lambda line: None, herdr_delay=0)
            sender.start()
            await asyncio.to_thread(self.poll, 100.0)
            self.snapshots["default"] = snapshot(blocked=["w1:p1"])
            # HERDR-11 §4: an unwatched session's next read is ten seconds on.
            await asyncio.to_thread(self.poll, 110.0)
            for _ in range(5):
                await asyncio.sleep(0.01)
            await sender.drain()
            await sender.stop()
            temp.cleanup()
        asyncio.run(run())
        self.assertEqual(len(sent), 1)
        self.assertIn("open=herdr&session=default&pane=w1%3Ap1&agent=claude&host=" + HOST_ID,
                      sent[0]["notification"]["navigate"])


if __name__ == "__main__":
    unittest.main()
