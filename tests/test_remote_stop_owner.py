"""REMOTE-STOP-1: the person at the computer can always end a Remote session.

The incident (2026-09-24): a Take over on a phone got stuck - the app's panel
would not open, so the phone could not end it - and on the computer `remote
stop` with the panel's credential answered `permission_denied`, because only
the device that owns a session could stop it. During a Take over the computer's
own screen is off, so nobody at it could even see the panel.

The rule these tests pin:

* a paired device ends only its own session - on HTTPS, and on the Unix socket
  too if somebody hands its credential to the CLI;
* the local owner - the panel's own credential or the tokenless
  `local.remote.stop`, both only on the verified same-UID Unix socket - ends
  any session, through the one normal restore path;
* the panel's credential on HTTPS is still just a credential: nothing about
  any token's scope changed;
* the device is told `reason: "ended_on_computer"`, and the journal says who
  ended it and nothing else.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import aiohttp
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from omodachi_core.auth import DeviceAuthenticator
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.ipc import JsonLineClient, JsonLineServer
from omodachi_core.network import NetworkServer
from omodachi_core.official_bar_position import OfficialBarPosition
from omodachi_core.protocol import IDLE_REMOTE_BAR, PLUGIN_DEVICE_ID
from omodachi_core.remote import RemoteManager
from omodachi_core.remote.backends import VncBackend
from omodachi_core.remote.hyprland import Hyprland, OWNED_NAME
from omodachi_core.remote.idle import OmarchyIdle
from omodachi_core.remote.profile import EncoderLimits
from tests.remote_fakes import FakeBar, FakeCompositor, FakeIdle, FakeWayVNC, INSTANCE, profile_request

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
ENCODER = EncoderLimits(4096, 4096, 16_777_216, 60, 40_000, 2, 2)
JOURNAL_KEYS = {"omodachi", "event", "session", "mode", "owner", "reason", "ended_by", "errors"}


def validator(name):
    registry = Registry()
    for path in sorted(CONTRACTS.rglob("*.schema.json")):
        document = json.loads(path.read_text())
        registry = registry.with_resource(document["$id"], Resource.from_contents(document))
    return Draft202012Validator(json.loads((CONTRACTS / name).read_text()), registry=registry)


class OwnerStopHarness(unittest.IsolatedAsyncioTestCase):
    """Both transports of one daemon, a synthetic host, two iPads and the panel."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.compositor = FakeCompositor()
        self.bar = FakeBar()
        self.idle = FakeIdle()
        self.now = [1000.0]
        self.manager = RemoteManager(
            hyprland=Hyprland(INSTANCE, runner=self.compositor, sleep=lambda _seconds: None),
            journal_dir=self.root / "remote", encoder=ENCODER,
            vnc=VncBackend(FakeWayVNC.factory()),
            bar_position=OfficialBarPosition(home=self.root, read_position=self.bar.read,
                                             set_position=self.bar.set),
            idle=OmarchyIdle(runner=self.idle.run, sleep=lambda _seconds: None),
            monotonic=lambda: self.now[0])
        self.hub = Hub(authenticator=DeviceAuthenticator.from_file(self.root / "secret"))
        self.ipad_a = self.hub.register_device("ipad-a")
        self.ipad_b = self.hub.register_device("ipad-b")
        self.panel = self.hub.register_device(PLUGIN_DEVICE_ID)
        self.service = create_service(self.hub, demo=True, remote_manager=self.manager)
        self.network = NetworkServer(self.service, allow_loopback_http=True)
        await self.network.start()
        self.socket = str(self.root / "core.sock")
        self.ipc = JsonLineServer(self.hub, self.socket, local_handler=self.service.dispatch_local_async)
        await self.ipc.start()
        self.http = aiohttp.ClientSession()
        self.url = "http://127.0.0.1:" + str(self.network.bound_port)

    async def asyncTearDown(self):
        await self.http.close()
        await self.ipc.close()
        await self.network.close()

    # --- helpers -----------------------------------------------------------

    async def call(self, method, path, token, payload=None):
        headers = {"Authorization": "Bearer " + token}
        async with self.http.request(method, self.url + path, json=payload, headers=headers) as response:
            return response.status, await response.json()

    async def start(self, token=None, **payload):
        status, value = await self.call("POST", "/v1/remote/sessions", token or self.ipad_a,
                                        dict(profile_request(), backend="vnc", **payload))
        self.assertEqual(status, 201, value)
        return value["session"]

    async def local(self, **params):
        """`local.remote.stop` exactly as `omodachi-host remote stop` sends it: no credential."""
        return await JsonLineClient(self.socket, timeout=30).request("local.remote.stop", **params)

    async def over_ipc(self, token, op, **params):
        return await JsonLineClient(self.socket, token, timeout=30).request(op, **params)

    def owned(self):
        return [row for row in self.compositor.rows if OWNED_NAME.fullmatch(row["name"])]

    def physical(self):
        return [row for row in self.compositor.rows if not OWNED_NAME.fullmatch(row["name"])]

    def session_events(self, device):
        return [event.payload for event in self.hub.events_since(0, limit=None, device_id=device)
                if event.type == "remote.session.changed"]

    def assert_live(self, session):
        self.assertIsNotNone(self.manager.current())
        self.assertEqual(self.manager.current().id, session["id"])
        self.assertEqual(len(self.owned()), 1)

    def assert_ended(self, session, reason):
        self.assertIsNone(self.manager.current())
        self.assertEqual(self.owned(), [])
        self.assertEqual(sorted((self.root / "remote").glob("OMODACHI-*.json")), [])
        self.assertEqual(self.hub.state_view("remote")["remote"]["session_id"], None)
        last = self.session_events("ipad-a")[-1]
        self.assertEqual(last, {"id": session["id"], "revision": last["revision"],
                                "state": "released", "reason": reason})


class TheRuleTests(OwnerStopHarness):
    async def test_a_device_cannot_end_another_devices_session(self):
        session = await self.start()
        status, value = await self.call("DELETE", "/v1/remote/sessions/" + session["id"], self.ipad_b)
        self.assertEqual((status, value["error"]["code"]), (403, "permission_denied"), value)
        self.assert_live(session)

    async def test_a_device_still_ends_its_own_session_with_the_reason_it_always_had(self):
        session = await self.start()
        status, value = await self.call("DELETE", "/v1/remote/sessions/" + session["id"], self.ipad_a)
        self.assertEqual((status, value["released"]), (200, True), value)
        self.assert_ended(session, "released")

    async def test_the_panels_credential_over_https_is_still_just_a_credential(self):
        # Token scopes are unchanged: what makes the panel the local owner is
        # the verified Unix peer, which HTTPS never is.
        session = await self.start()
        status, value = await self.call("DELETE", "/v1/remote/sessions/" + session["id"], self.panel)
        self.assertEqual((status, value["error"]["code"]), (403, "permission_denied"), value)
        self.assert_live(session)

    async def test_a_device_credential_on_the_unix_socket_is_still_that_device(self):
        session = await self.start()
        value = await self.over_ipc(self.ipad_b, "remote.stop", session_id=session["id"])
        self.assertEqual((value["ok"], value["error"]), (False, "permission_denied"), value)
        self.assert_live(session)

    async def test_the_panels_credential_on_the_unix_socket_ends_any_session(self):
        # The incident: exactly this call answered permission_denied.
        session = await self.start()
        value = await self.over_ipc(self.panel, "remote.stop", session_id=session["id"])
        self.assertTrue(value["ok"], value)
        self.assertEqual((value["result"]["released"], value["result"]["errors"]), (True, []))
        self.assert_ended(session, "ended_on_computer")

    async def test_the_local_owner_ends_another_devices_session_without_any_credential(self):
        session = await self.start()
        value = await self.local()
        self.assertTrue(value["ok"], value)
        self.assertEqual(value["result"], {
            "released": True, "errors": [], "ended_by": "local_owner",
            "sessions": [{"session_id": session["id"], "owner_device_id": "ipad-a", "mode": "extend",
                          "released": True, "errors": []}]})
        self.assert_ended(session, "ended_on_computer")

    async def test_every_other_remote_operation_keeps_the_device_rule_for_the_panel_too(self):
        # Only `remote.stop` learned about the local owner.
        session = await self.start()
        for op, params in (("remote.get", {}), ("remote.heartbeat", {}),
                           ("remote.resize", {"expected_revision": session["revision"],
                                              "viewport_points": {"width": 834, "height": 1194},
                                              "orientation": "portrait"})):
            with self.subTest(op=op):
                value = await self.over_ipc(self.panel, op, session_id=session["id"], **params)
                self.assertEqual((value["ok"], value["error"]), (False, "permission_denied"), value)
        self.assert_live(session)


class LocalStopShapeTests(OwnerStopHarness):
    async def test_all_with_nothing_live_is_nothing_to_do(self):
        value = await self.local(all=True)
        self.assertTrue(value["ok"], value)
        self.assertEqual(value["result"], {"released": True, "errors": [], "sessions": [],
                                           "ended_by": "local_owner"})

    async def test_all_ends_the_live_session(self):
        session = await self.start()
        value = await self.local(all=True)
        self.assertEqual([row["session_id"] for row in value["result"]["sessions"]], [session["id"]])
        self.assert_ended(session, "ended_on_computer")

    async def test_nothing_named_and_nothing_live_says_so(self):
        value = await self.local()
        self.assertEqual((value["ok"], value["error"]), (False, "session_not_found"), value)

    async def test_a_named_session_is_ended_and_an_unknown_one_is_refused(self):
        value = await self.local(session_id="rs_" + "0" * 32)
        self.assertEqual((value["ok"], value["error"]), (False, "session_not_found"), value)
        session = await self.start()
        value = await self.local(session_id=session["id"])
        self.assertTrue(value["ok"], value)
        self.assert_ended(session, "ended_on_computer")
        # Idempotent for the session it just ended: nothing more happens.
        again = await self.local(session_id=session["id"])
        self.assertTrue(again["ok"], again)
        self.assertEqual(len([e for e in self.session_events("ipad-a") if e["state"] == "released"]), 1)

    async def test_malformed_requests_are_refused_before_anything_is_touched(self):
        session = await self.start()
        for params in ({"all": "yes"}, {"session_id": 5}, {"session_id": "../x"},
                       {"session_id": session["id"], "all": True}, {"force": True}):
            with self.subTest(params=params):
                value = await self.local(**params)
                self.assertEqual((value["ok"], value["error"]), (False, "invalid_request"), value)
        self.assert_live(session)

    async def test_a_credential_on_the_local_operation_is_refused(self):
        session = await self.start()
        value = await JsonLineClient(self.socket, self.panel).request("local.remote.stop")
        self.assertEqual((value["ok"], value["error"]), (False, "PermissionError"), value)
        self.assertIn("local_token_not_accepted", value["message"])
        self.assert_live(session)

    async def test_nobody_but_this_user_reaches_it(self):
        session = await self.start()
        others = [os.getuid() + 1, None] + ([0] if os.getuid() != 0 else [])
        for uid in others:
            with self.subTest(uid=uid), patch.object(self.ipc, "_peer_uid", return_value=uid):
                value = await self.local(all=True)
                self.assertEqual((value["ok"], value["error"]), (False, "PermissionError"), value)
        self.assert_live(session)


class TheNormalStopPathTests(OwnerStopHarness):
    """Ending a session from the computer is the same restore a device's own End runs."""

    async def takeover_and_end(self, end):
        physical = json.dumps(self.physical(), sort_keys=True)
        workspaces = [row[:2] for row in self.compositor.workspaces]
        wake = dict(self.compositor.wake_options)
        aspect = list(self.compositor.single_window_aspect)
        session = await self.start(mode="takeover", lock_local_input=True,
                                   viewport_points={"width": 393, "height": 852}, orientation="portrait",
                                   bar_occlusion_points={"top": 40, "bottom": 40, "left": 40, "right": 40})
        # What a takeover does to the room: dark screen, everything moved to
        # the phone's output, keyboard and mouse off, the bar on the long edge.
        self.assertFalse(self.physical()[0]["dpmsStatus"])
        self.assertTrue(self.compositor.disabled_devices)
        self.assertEqual(self.bar.position, "left")
        self.assertFalse(self.idle.state)
        await self.service.remote.refresh_bar()
        self.assertTrue(self.hub.state_view("remote_bar")["remote_bar"]["active"])
        await end(session)
        await self.service.remote.refresh_bar()
        self.assertEqual(json.dumps(self.physical(), sort_keys=True), physical, "screen back on, as it was")
        self.assertEqual([row[:2] for row in self.compositor.workspaces], workspaces, "workspaces home")
        self.assertEqual(self.compositor.disabled_devices, [], "input unlocked")
        self.assertEqual(self.bar.position, "top", "A-02 bar position restored")
        self.assertEqual(self.compositor.wake_options, wake)
        self.assertEqual(self.compositor.single_window_aspect, aspect)
        self.assertTrue(self.idle.state, "idle back on")
        self.assertEqual(self.hub.state_view("remote_bar")["remote_bar"], dict(IDLE_REMOTE_BAR))
        return session

    async def test_the_local_owner_gives_back_exactly_what_the_device_would(self):
        async def local(session):
            value = await self.local()
            self.assertTrue(value["ok"], value)
            self.assertEqual(value["result"]["sessions"][0]["mode"], "takeover")
        session = await self.takeover_and_end(local)
        self.assert_ended(session, "ended_on_computer")

    async def test_the_devices_own_end_for_comparison(self):
        async def device(session):
            status, value = await self.call("DELETE", "/v1/remote/sessions/" + session["id"], self.ipad_a)
            self.assertEqual(status, 200, value)
        session = await self.takeover_and_end(device)
        self.assert_ended(session, "released")


class WhatTheDeviceAndTheJournalAreToldTests(OwnerStopHarness):
    async def test_the_device_gets_a_contract_shaped_ended_event_and_can_read_why(self):
        session = await self.start()
        await self.local()
        event = next(event for event in reversed(self.hub.events_since(0, limit=None, device_id="ipad-a"))
                     if event.type == "remote.session.changed")
        validator("events.schema.json").validate(dataclasses.asdict(event))
        self.assertEqual(event.payload["reason"], "ended_on_computer")
        self.assertEqual(event.payload["state"], "released")
        self.assertIsNone(event.device_id, "broadcast, so the holding device receives it")
        status, value = await self.call("GET", "/v1/remote/sessions/" + session["id"], self.ipad_a)
        self.assertEqual(status, 200, value)
        validator("remote-session.schema.json").validate(value["session"])
        self.assertEqual((value["session"]["state"], value["session"]["reason"]), ("released", "ended_on_computer"))

    async def journal_of(self, action):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            await action()
        return [json.loads(line) for line in buffer.getvalue().splitlines()
                if line.startswith("{") and '"session_ended"' in line]

    async def test_the_journal_says_the_local_owner_ended_it_and_nothing_else(self):
        session = await self.start(mode="takeover")
        lines = await self.journal_of(lambda: self.local())
        self.assertEqual(lines, [{"omodachi": "remote", "event": "session_ended", "session": session["id"],
                                  "mode": "takeover", "owner": "ipad-a", "reason": "ended_on_computer",
                                  "ended_by": "local_owner", "errors": []}])
        self.assertEqual(set(lines[0]), JOURNAL_KEYS)
        text = json.dumps(lines)
        for secret in (self.ipad_a, self.ipad_b, self.panel):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret.split(".")[1], text)

    async def test_the_panels_credential_is_journaled_as_the_local_owner_too(self):
        session = await self.start()
        lines = await self.journal_of(lambda: self.over_ipc(self.panel, "remote.stop", session_id=session["id"]))
        self.assertEqual([(row["ended_by"], row["owner"], row["reason"]) for row in lines],
                         [("local_owner", "ipad-a", "ended_on_computer")])

    async def test_a_devices_own_end_is_journaled_as_that_device(self):
        session = await self.start()
        lines = await self.journal_of(
            lambda: self.call("DELETE", "/v1/remote/sessions/" + session["id"], self.ipad_a))
        self.assertEqual([(row["ended_by"], row["owner"], row["reason"]) for row in lines],
                         [("ipad-a", "ipad-a", "released")])

    async def test_a_heartbeat_timeout_is_journaled_as_the_host(self):
        await self.start()
        self.now[0] += 3600

        async def expire():
            await asyncio.to_thread(self.manager.maintain)
        lines = await self.journal_of(expire)
        self.assertEqual([(row["ended_by"], row["reason"]) for row in lines], [("host", "heartbeat_timeout")])


class HubLocalFlagTests(unittest.TestCase):
    def test_only_a_local_aware_handler_is_told_and_only_when_the_transport_says_so(self):
        hub = Hub()
        seen = []
        hub.register_handler("plain.op", lambda device, params: seen.append(("plain", device)))
        hub.register_handler("aware.op", lambda device, params, local=False: seen.append(("aware", device, local)),
                             local_aware=True)
        hub.dispatch("plain.op", {}, "phone", local=True)
        hub.dispatch("aware.op", {}, "phone")
        hub.dispatch("aware.op", {}, "phone", local=True)
        hub.dispatch("aware.op", {}, "phone", local="yes")  # only a real True counts
        self.assertEqual(seen, [("plain", "phone"), ("aware", "phone", False), ("aware", "phone", True),
                                ("aware", "phone", False)])


class RemoteStopCliTests(OwnerStopHarness):
    """The real `omodachi-host remote stop` in a subprocess, with no credential at all."""

    async def cli(self, *args, expect=0):
        environment = {key: value for key, value in os.environ.items() if key != "OMODACHI_TOKEN"}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "from omodachi_core.cli import host_main; raise SystemExit(host_main())",
            "--socket", self.socket, "remote", "stop", *args, env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(process.communicate(), 60)
        self.assertEqual(process.returncode, expect, (out, err))
        return json.loads(out) if out.strip() else None

    async def test_stop_ends_another_devices_session_with_no_token(self):
        session = await self.start()
        value = await self.cli()
        self.assertEqual(value["result"]["sessions"][0]["owner_device_id"], "ipad-a")
        self.assert_ended(session, "ended_on_computer")

    async def test_stop_all_with_nothing_live_exits_zero(self):
        value = await self.cli("--all")
        self.assertEqual(value["result"]["sessions"], [])

    async def test_stop_all_ends_a_takeover(self):
        session = await self.start(mode="takeover")
        value = await self.cli("--all")
        self.assertEqual(value["result"]["sessions"][0]["mode"], "takeover")
        self.assert_ended(session, "ended_on_computer")

    async def test_stop_with_nothing_live_says_so(self):
        value = await self.cli(expect=1)
        self.assertEqual(value["error"], "session_not_found")

    async def test_a_session_and_all_together_is_a_usage_error(self):
        await self.cli("--all", "--session-id", "rs_" + "0" * 32, expect=2)

    async def test_a_token_in_the_environment_changes_nothing(self):
        # Somebody who exported a device's credential for other commands still
        # gets the owner's stop: the token path could only end its own.
        session = await self.start()
        environment = dict(os.environ, OMODACHI_TOKEN=self.ipad_b)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "from omodachi_core.cli import host_main; raise SystemExit(host_main())",
            "--socket", self.socket, "remote", "stop", env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(process.communicate(), 60)
        self.assertEqual(process.returncode, 0, (out, err))
        self.assert_ended(session, "ended_on_computer")


if __name__ == "__main__":
    unittest.main()
