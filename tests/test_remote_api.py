"""The /v1/remote surface over real authenticated HTTP, against the synthetic host."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import aiohttp
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from omodachi_core.auth import DeviceAuthenticator
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
from omodachi_core.official_bar_position import OfficialBarPosition
from omodachi_core.remote import RemoteManager
from omodachi_core.remote.backends import SunshineBackend, VncBackend
from omodachi_core.remote.hyprland import Hyprland, OWNED_NAME
from omodachi_core.remote.profile import EncoderLimits
from tests.remote_fakes import (FakeBar, FakeCompositor, FakeShell, FakeSunshine, FakeWayVNC, INSTANCE,
                                profile_request)

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def validator(name):
    registry = Registry()
    for path in sorted(CONTRACTS.rglob("*.schema.json")):
        document = json.loads(path.read_text())
        registry = registry.with_resource(document["$id"], Resource.from_contents(document))
    return Draft202012Validator(json.loads((CONTRACTS / name).read_text()), registry=registry)


class RemoteApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.compositor = FakeCompositor()
        self.sunshine = FakeSunshine()
        self.bar = FakeBar()
        self.now = [1000.0]
        self.manager = RemoteManager(
            hyprland=Hyprland(INSTANCE, runner=self.compositor), journal_dir=self.root / "remote",
            encoder=EncoderLimits(4096, 4096, 16_777_216, 60, 40_000, 2, 2),
            sunshine=SunshineBackend(self.sunshine, certificate_resolver=lambda device: "a" * 64,
                                     address=lambda: "192.168.1.11"),
            vnc=VncBackend(FakeWayVNC.factory()),
            bar_position=OfficialBarPosition(home=self.root, read_position=self.bar.read, set_position=self.bar.set),
            monotonic=lambda: self.now[0])
        auth = DeviceAuthenticator.from_file(self.root / "secret")
        self.hub = Hub(authenticator=auth)
        self.token = self.hub.register_device("ipad-a")
        self.other = self.hub.register_device("ipad-b")
        self.service = create_service(self.hub, demo=True, remote_manager=self.manager)
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.http = aiohttp.ClientSession()
        self.url = "http://127.0.0.1:" + str(self.server.bound_port)

    async def asyncTearDown(self):
        await self.http.close()
        await self.server.close()

    async def call(self, method, path, payload=None, token=None):
        headers = {"Authorization": "Bearer " + (token or self.token)}
        async with self.http.request(method, self.url + path, json=payload, headers=headers) as response:
            return response.status, await response.json()

    async def start(self, **payload):
        status, value = await self.call("POST", "/v1/remote/sessions", dict(profile_request(), **payload))
        self.assertEqual(status, 201, value)
        return value["session"]

    async def test_capabilities_lists_backends_modes_and_limits(self):
        status, value = await self.call("GET", "/v1/remote/capabilities")
        self.assertEqual(status, 200, value)
        validator("remote-capabilities.schema.json").validate(value)
        self.assertEqual(sorted(value["backends"]), ["sunshine", "vnc"])
        self.assertEqual(value["modes"], ["extend", "takeover"])

    async def test_create_read_resize_heartbeat_presented_and_release(self):
        session = await self.start(backend="vnc")
        checks = validator("remote-session.schema.json")
        checks.validate(session)
        validator("remote-connection.schema.json").validate(session["connection"])
        self.assertEqual(session["state"], "ready")
        self.assertEqual(session["output"]["mode_pixels"], session["profile"]["output_mode_pixels"])

        status, value = await self.call("GET", "/v1/remote/sessions/" + session["id"])
        self.assertEqual(status, 200, value)
        self.assertEqual(value["session"]["id"], session["id"])

        status, value = await self.call("POST", f"/v1/remote/sessions/{session['id']}/heartbeat")
        self.assertEqual((status, value["state"]), (200, "ready"), value)

        status, value = await self.call("POST", f"/v1/remote/sessions/{session['id']}/resize", {
            "expected_revision": session["revision"], "viewport_points": {"width": 834, "height": 1194},
            "orientation": "portrait"})
        self.assertEqual(status, 200, value)
        checks.validate(value["session"])
        resized = value["session"]
        self.assertGreater(resized["revision"], session["revision"])
        self.assertLess(resized["profile"]["output_mode_pixels"]["width"],
                        resized["profile"]["output_mode_pixels"]["height"])

        pixels = resized["profile"]["stream_pixels"]
        scale = min(834 / pixels["width"], 1194 / pixels["height"])
        width, height = pixels["width"] * scale, pixels["height"] * scale
        status, value = await self.call("POST", f"/v1/remote/sessions/{session['id']}/presented", {
            "revision": resized["revision"], "decoded_pixels": pixels,
            "video_rect_points": {"x": (834 - width) / 2, "y": (1194 - height) / 2,
                                  "width": width, "height": height}})
        self.assertEqual((status, value["accepted"]), (200, True), value)

        status, value = await self.call("DELETE", "/v1/remote/sessions/" + session["id"])
        self.assertEqual((status, value["released"]), (200, True), value)
        self.assertEqual([row for row in self.compositor.rows if OWNED_NAME.fullmatch(row["name"])], [])

    async def test_a_second_session_is_409_with_the_existing_id(self):
        session = await self.start(backend="vnc")
        status, value = await self.call("POST", "/v1/remote/sessions", profile_request())
        self.assertEqual(status, 409, value)
        self.assertEqual(value["error"]["code"], "remote_session_exists")
        # remote-api.md has always promised the existing session_id here; the
        # boundary used to drop RemoteError.detail before serializing. SPEC-I
        # adds who is holding it, because "ask the other device" is only
        # actionable when the screen can name the other device.
        validator("http-error.schema.json").validate(value)
        detail = value["error"]["detail"]
        self.assertEqual(detail["session_id"], session["id"])
        self.assertEqual(detail["owner_device_id"], session["device_id"])
        # This hub's authenticator keeps no display names, so the only honest
        # answer is the id itself - never a blank or a placeholder.
        self.assertEqual(detail["owner_device_name"], session["device_id"])
        self.assertEqual((detail["mode"], detail["backend"]), ("extend", "vnc"))
        self.assertIsInstance(detail["started_at"], int)
        status, value = await self.call("GET", "/v1/remote/sessions/" + session["id"])
        self.assertEqual(value["session"]["revision"], session["revision"])

    async def test_a_stale_revision_names_the_current_one_in_its_detail(self):
        session = await self.start(backend="vnc")
        status, value = await self.call("POST", f"/v1/remote/sessions/{session['id']}/heartbeat", {})
        self.assertEqual(status, 200, value)
        status, value = await self.call("POST", f"/v1/remote/sessions/{session['id']}/resize", {
            "expected_revision": session["revision"] - 1,
            "viewport_points": {"width": 834, "height": 1194}, "orientation": "portrait"})
        self.assertEqual((status, value["error"]["code"]), (409, "stale_revision"), value)
        validator("http-error.schema.json").validate(value)
        self.assertEqual(value["error"]["detail"], {"revision": session["revision"]})

    async def test_an_error_without_detail_stays_a_two_field_error(self):
        status, value = await self.call("GET", "/v1/remote/sessions/rs_" + "0" * 32)
        self.assertEqual((status, value["error"]["code"]), (404, "session_not_found"), value)
        validator("http-error.schema.json").validate(value)
        self.assertEqual(set(value["error"]), {"code", "message"})

    async def test_a_stale_expected_revision_is_409(self):
        session = await self.start(backend="vnc")
        status, value = await self.call("POST", f"/v1/remote/sessions/{session['id']}/resize", {
            "expected_revision": session["revision"] - 1, "viewport_points": {"width": 834, "height": 1194},
            "orientation": "portrait"})
        self.assertEqual(status, 409, value)
        self.assertEqual(value["error"]["code"], "stale_revision")

    async def test_another_device_cannot_read_change_or_release_the_session(self):
        session = await self.start(backend="vnc")
        for method, path, payload in (("GET", "", None), ("DELETE", "", None),
                                      ("POST", "/heartbeat", {}),
                                      ("POST", "/resize", {"expected_revision": session["revision"],
                                                           "viewport_points": {"width": 834, "height": 1194},
                                                           "orientation": "portrait"})):
            status, value = await self.call(method, f"/v1/remote/sessions/{session['id']}{path}", payload, self.other)
            self.assertEqual(status, 403, (method, path, value))
        self.assertEqual(len([row for row in self.compositor.rows if OWNED_NAME.fullmatch(row["name"])]), 1)

    async def test_backend_switch_returns_the_new_connection(self):
        session = await self.start(backend="vnc")
        status, value = await self.call("POST", f"/v1/remote/sessions/{session['id']}/backend",
                                        {"expected_revision": session["revision"], "backend": "sunshine"})
        self.assertEqual(status, 200, value)
        validator("remote-connection.schema.json").validate(value["session"]["connection"])
        self.assertEqual(value["session"]["connection"]["backend"], "sunshine")
        self.assertEqual(value["session"]["connection"]["https_port"], 47984)

    async def test_state_projects_the_session_and_the_remote_bar(self):
        session = await self.start(backend="vnc", mode="takeover")
        status, value = await self.call("GET", "/v1/state")
        self.assertEqual(status, 200, value)
        self.assertEqual(value["remote"], {"session_id": session["id"], "state": "ready",
                                           "mode": "takeover", "backend": "vnc",
                                           "revision": session["revision"]})
        await self.service.remote.refresh_bar()
        bar = self.hub.state_snapshot()["remote_bar"]
        validator("remote-bar.schema.json").validate(bar)
        self.assertTrue(bar["active"])
        self.assertEqual(bar["session_id"], session["id"])
        await self.call("DELETE", "/v1/remote/sessions/" + session["id"])
        await self.service.remote.refresh_bar()
        self.assertFalse(self.hub.state_snapshot()["remote_bar"]["active"])
        self.assertIsNone(self.hub.state_snapshot()["remote"]["session_id"])

    async def test_session_change_is_published_as_an_event(self):
        session = await self.start(backend="vnc")
        events = [event for event in self.hub.events_since(0, limit=None, device_id="ipad-a")
                  if event.type == "remote.session.changed"]
        self.assertTrue(events)
        self.assertEqual(events[-1].payload, {"id": session["id"], "revision": session["revision"],
                                              "state": "ready", "reason": "created"})

    async def test_unknown_session_is_404_and_unknown_fields_are_400(self):
        status, value = await self.call("GET", "/v1/remote/sessions/rs_missing")
        self.assertEqual(status, 404, value)
        status, value = await self.call("POST", "/v1/remote/sessions", dict(profile_request(), nonsense=1))
        self.assertEqual(status, 400, value)

    async def test_the_old_session_routes_are_gone(self):
        for method, path in (("POST", "/v1/sessions"), ("GET", "/v1/sessions/x"),
                             ("POST", "/v1/sessions/x/orientation"), ("DELETE", "/v1/sessions/x")):
            status, _ = await self.call(method, path, {} if method == "POST" else None)
            self.assertIn(status, {404, 405}, (method, path, status))


class RemoteCliTests(unittest.IsolatedAsyncioTestCase):
    """The real omodachi-host remote commands over the real Unix IPC."""

    async def asyncSetUp(self):
        from omodachi_core.ipc import JsonLineServer
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.compositor = FakeCompositor()
        self.manager = RemoteManager(
            hyprland=Hyprland(INSTANCE, runner=self.compositor), journal_dir=self.root / "remote",
            encoder=EncoderLimits(4096, 4096, 16_777_216, 60, 40_000, 2, 2),
            vnc=VncBackend(FakeWayVNC.factory()))
        auth = DeviceAuthenticator.from_file(self.root / "secret")
        self.hub = Hub(authenticator=auth)
        self.token = self.hub.register_device("host-cli")
        self.service = create_service(self.hub, demo=True, remote_manager=self.manager)
        self.socket = str(self.root / "core.sock")
        self.ipc = JsonLineServer(self.hub, self.socket, local_handler=self.service.dispatch_local_async)
        await self.ipc.start()

    async def asyncTearDown(self):
        await self.ipc.close()

    async def cli(self, *args):
        import asyncio as _asyncio
        import os
        import sys
        environment = dict(os.environ, OMODACHI_TOKEN=self.token)
        process = await _asyncio.create_subprocess_exec(
            sys.executable, "-c", "from omodachi_core.cli import host_main; raise SystemExit(host_main())",
            "--socket", self.socket, "remote", *args, env=environment,
            stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.PIPE)
        out, err = await process.communicate()
        self.assertEqual(process.returncode, 0, (out, err))
        return json.loads(out)

    async def test_status_start_resize_stop_and_recover(self):
        value = await self.cli("status")
        self.assertIsNone(value["result"]["session"])
        value = await self.cli("start", "--backend", "vnc", "--viewport", "1194x834",
                               "--orientation", "landscape_left", "--ttl", "60")
        session = value["result"]["session"]
        self.assertEqual((session["mode"], session["backend"], session["ttl_seconds"]), ("extend", "vnc", 60.0))
        self.assertEqual(len([row for row in self.compositor.rows if OWNED_NAME.fullmatch(row["name"])]), 1)
        value = await self.cli("resize", "--viewport", "834x1194")
        resized = value["result"]["session"]
        self.assertEqual(resized["id"], session["id"])
        self.assertGreater(resized["revision"], session["revision"])
        self.assertLess(resized["profile"]["output_mode_pixels"]["width"],
                        resized["profile"]["output_mode_pixels"]["height"])
        value = await self.cli("stop")
        self.assertTrue(value["result"]["released"])
        self.assertEqual([row for row in self.compositor.rows if OWNED_NAME.fullmatch(row["name"])], [])
        value = await self.cli("recover")
        self.assertEqual(value["result"]["recovered"], [])
        self.assertEqual(value["result"]["orphan_outputs"], [])
        # CORE-2 §2: an output this daemon never journaled survives a plain
        # recover and goes only on --orphans.
        stray = "OMODACHI-" + "d" * 16
        self.compositor(("hyprctl", "--instance", INSTANCE, "output", "create", "headless", stray))
        value = await self.cli("recover")
        self.assertEqual((value["result"]["orphan_outputs"], value["result"]["unowned_outputs"]), ([], [stray]))
        value = await self.cli("recover", "--orphans")
        self.assertEqual((value["result"]["orphan_outputs"], value["result"]["unowned_outputs"]), ([stray], []))
        self.assertEqual([row for row in self.compositor.rows if OWNED_NAME.fullmatch(row["name"])], [])

    async def test_stop_without_a_session_says_so_instead_of_guessing(self):
        import asyncio as _asyncio
        import os
        import sys
        environment = dict(os.environ, OMODACHI_TOKEN=self.token)
        process = await _asyncio.create_subprocess_exec(
            sys.executable, "-c", "from omodachi_core.cli import host_main; raise SystemExit(host_main())",
            "--socket", self.socket, "remote", "stop", env=environment,
            stdout=_asyncio.subprocess.PIPE)
        out, _ = await process.communicate()
        self.assertEqual(process.returncode, 1)
        self.assertEqual(json.loads(out)["error"], "session_not_found")


class HostPreferenceTests(RemoteApiTests):
    async def test_the_hosts_quality_preference_reaches_the_planned_profile(self):
        # The client asks for 60 fps / 20000 kbps; the host default is `balanced`.
        session = await self.start()
        self.assertEqual(session["profile"]["bitrate_kbps"], 12000)
        _, preferences = await self.call("GET", "/v1/preferences")
        self.assertEqual(preferences["profile_defaults"]["quality"],
                         {"fps": 60, "bitrate_kbps": 12000})
        self.assertEqual(session["connection"]["fps"], session["profile"]["fps"])

    async def test_switching_the_host_to_performance_changes_the_next_session(self):
        store = self.service.preferences_store
        store.set(expected_revision=store.get()["revision"], changes={"quality": "performance"})
        session = await self.start()
        self.assertEqual((session["profile"]["fps"], session["profile"]["bitrate_kbps"]), (30, 8000))


class DisplayEventTests(unittest.IsolatedAsyncioTestCase):
    """The compositor's `.socket2.sock` is what tells a session the host moved."""

    async def asyncSetUp(self):
        import asyncio
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.compositor = FakeCompositor()
        self.manager = RemoteManager(
            hyprland=Hyprland(INSTANCE, runner=self.compositor), journal_dir=self.root / "remote",
            encoder=EncoderLimits(4096, 4096, 16_777_216, 60, 40_000, 2, 2),
            vnc=VncBackend(FakeWayVNC.factory()))
        self.path = str(self.root / "socket2.sock")
        self.manager.hyprland.event_socket = lambda: self.path
        self.clients = []
        self.listener = await asyncio.start_unix_server(self._accept, path=self.path)
        self.addCleanup(self.listener.close)
        self.addCleanup(lambda: [writer.close() for writer in self.clients])
        auth = DeviceAuthenticator.from_file(self.root / "secret")
        self.hub = Hub(authenticator=auth)
        self.service = create_service(self.hub, demo=True, remote_manager=self.manager).remote

    async def _accept(self, reader, writer):
        self.clients.append(writer)

    async def wait(self, predicate, timeout=5.0):
        import asyncio
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.02)
        return False

    async def emit(self, line):
        self.assertTrue(await self.wait(lambda: bool(self.clients)), "the watcher never connected")
        self.clients[0].write(line)
        await self.clients[0].drain()

    async def test_configreloaded_makes_the_session_adopt_the_new_geometry(self):
        await self.service.attach_transport()
        session = self.manager.create("ipad-a", dict(profile_request(), backend="vnc"))
        planned = json.dumps(self.compositor.row(session.output_name), sort_keys=True)
        self.compositor.row(session.output_name).update(width=1920, height=1080, x=0)
        await self.emit(b"configreloaded>>\n")
        self.assertTrue(await self.wait(
            lambda: json.dumps(self.compositor.row(session.output_name), sort_keys=True) == planned),
            "the owned output was never re-asserted")
        await self.service.detach_transport()

    async def test_an_unrelated_event_does_not_provoke_a_reconcile(self):
        await self.service.attach_transport()
        session = self.manager.create("ipad-a", dict(profile_request(), backend="vnc"))
        await self.emit(b"activewindow>>kitty,shell\n")
        self.assertTrue(await self.wait(lambda: self.service._display_dirty is False))
        self.assertEqual(session.revision, self.manager.session.revision)
        await self.service.detach_transport()

    async def test_a_missing_event_socket_is_not_an_error(self):
        self.manager.hyprland.event_socket = lambda: str(self.root / "absent.sock")
        await self.service.attach_transport()
        session = self.manager.create("ipad-a", dict(profile_request(), backend="vnc"))
        # The periodic reconcile still converges without the event stream.
        self.compositor.row(session.output_name).update(x=0)
        self.assertTrue(await self.wait(
            lambda: self.compositor.row(session.output_name)["x"] == session.position[0]))
        await self.service.detach_transport()


if __name__ == "__main__":
    unittest.main()


class ShellRepairWatchdogTests(unittest.IsolatedAsyncioTestCase):
    """HOST-1 §4: the daemon's own watchdog is what notices the dead shell."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.compositor = FakeCompositor()
        self.shell = FakeShell(self.root)
        self.manager = RemoteManager(
            hyprland=Hyprland(INSTANCE, runner=self.compositor), journal_dir=self.root / "remote",
            encoder=EncoderLimits(4096, 4096, 16_777_216, 60, 40_000, 2, 2),
            vnc=VncBackend(FakeWayVNC.factory()), shell=self.shell)
        self.hub = Hub(authenticator=DeviceAuthenticator.from_file(self.root / "secret"))
        self.service = create_service(self.hub, demo=True, remote_manager=self.manager).remote


    async def wait(self, predicate, timeout=5.0):
        import asyncio
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.02)
        return False

    async def test_the_watchdog_restarts_the_shell_and_says_why(self):
        await self.service.attach_transport()
        self.manager.create("ipad-a", dict(profile_request(), backend="vnc", mode="takeover"))
        self.shell.crash("c61r66olt")
        self.assertTrue(await self.wait(lambda: self.shell.restarts == 1), "the shell was never repaired")
        self.assertTrue(await self.wait(lambda: any(
            event.type == "remote.session.changed" and event.payload.get("reason") == "shell_restarted"
            for event in self.hub.events_since(0, limit=None))),
            "no client was told the host had blinked")
        await self.service.detach_transport()

    async def test_a_quiet_shell_is_never_restarted(self):
        await self.service.attach_transport()
        self.manager.create("ipad-a", dict(profile_request(), backend="vnc", mode="takeover"))
        self.assertFalse(await self.wait(lambda: self.shell.restarts > 0, timeout=0.8))
        await self.service.detach_transport()
