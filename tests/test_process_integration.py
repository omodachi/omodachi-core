"""Real CLI process evidence for CORE-01, using isolated demo data and loopback.

Run with the project venv. Tokens stay in captured pipes and request headers;
assertion messages and evidence output intentionally never include credentials.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest

import aiohttp

from omodachi_core.ipc import JsonLineClient

ROOT = Path(__file__).resolve().parents[1]


class RealDaemonProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="omd-")
        self.directory = Path(self.temporary.name)
        self.socket_path = self.directory / "s"
        self.secret_path = self.directory / "device.secret"
        self.environment = dict(os.environ)
        self.environment["PYTHONPATH"] = str(ROOT / "src")
        self.environment["PYTHONUNBUFFERED"] = "1"
        self.environment.pop("OMODACHI_TOKEN", None)
        self.processes = []
        self.websockets = []
        self.ipc_writers = []
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        try:
            await self._start_daemon()
            # Deliberately issue from new CLI processes after the server is ready.
            self.phone = await self._issue("phone")
            self.tablet = await self._issue("tablet")
        except BaseException:
            await self.asyncTearDown()
            raise

    async def asyncTearDown(self):
        for writer in getattr(self, "ipc_writers", ()):
            writer.close()
            with suppress(ConnectionError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 1)
        for websocket in getattr(self, "websockets", ()):
            with suppress(Exception):
                await asyncio.wait_for(websocket.close(), 1)
        if hasattr(self, "http"):
            await self.http.close()
        for process in getattr(self, "processes", ()):
            if process.returncode is None:
                process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            # Drain captured pipes without printing bearer token output.
            with suppress(Exception):
                await asyncio.wait_for(process.communicate(), 1)
        if hasattr(self, "temporary"):
            self.temporary.cleanup()

    async def _spawn(self, *arguments):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "omodachi_core.cli", *map(str, arguments),
            cwd=ROOT, env=self.environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        self.processes.append(process)
        return process

    def _daemon_args(self):
        return ("--demo", "--socket", self.socket_path, "--secret-file", self.secret_path,
                "--listen", "127.0.0.1", "--port", "0", "--allow-loopback-http")

    async def _start_daemon(self):
        self.daemon = await self._spawn(*self._daemon_args())
        line = await asyncio.wait_for(self.daemon.stdout.readline(), 8)
        self.assertTrue(line, "daemon exited before its ready frame")
        ready = json.loads(line)
        self.assertTrue(ready.get("ready"), "daemon did not report ready")
        self.assertTrue(ready.get("demo"), "process tests must use synthetic demo mode")
        self.assertEqual(ready.get("host"), "127.0.0.1")
        self.assertGreater(ready.get("port", 0), 0)
        # A loopback development daemon reports its identity but never claims
        # to be advertising itself on the LAN.
        self.assertRegex(ready.get("host_id", ""), r"^[0-9a-f]{32}$")
        self.assertIn("discovery", ready)
        self.base_url = "http://127.0.0.1:" + str(ready["port"])
        self.ready = ready

    async def _admin(self, *arguments):
        process = await self._spawn("--secret-file", self.secret_path, "--socket", self.socket_path, *arguments)
        stdout, _ = await asyncio.wait_for(process.communicate(), 8)
        self.assertEqual(process.returncode, 0, "admin CLI failed; output withheld to protect credentials")
        return stdout.decode().strip()

    async def _issue(self, device_id):
        credential = await self._admin("--issue-token", device_id)
        self.assertTrue(credential.count(".") == 1, "CLI did not emit a credential")
        return credential

    async def _revoke(self, device_id):
        return json.loads(await self._admin("--revoke-device", device_id))

    @staticmethod
    def _authorization(token):
        return {"Authorization": "Bearer " + token}

    async def _request(self, method, path, token=None, data=None):
        async with self.http.request(method, self.base_url + path,
                                     headers=self._authorization(token) if token else {},
                                     json=data) as response:
            return response.status, await response.json()

    async def _ipc(self, operation, token=None, **params):
        return await JsonLineClient(str(self.socket_path), token).request(operation, **params)

    async def _websocket(self, token, **query):
        websocket = await self.http.ws_connect(self.base_url + "/v1/events",
                                               headers=self._authorization(token), params=query)
        self.websockets.append(websocket)
        message = await asyncio.wait_for(websocket.receive_json(), 2)
        return websocket, message

    async def _ipc_subscription(self, token, **params):
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path), limit=4 * 1024 * 1024)
        self.ipc_writers.append(writer)
        writer.write((json.dumps({"op": "events.subscribe", "token": token, **params}) + "\n").encode())
        await writer.drain()
        acknowledgment = json.loads(await asyncio.wait_for(reader.readline(), 2))
        self.assertTrue(acknowledgment.get("ok"), "IPC subscription was rejected")
        return reader, writer, acknowledgment

    async def _assert_resources(self, token, device_id):
        values = {}
        for name in ("state", "capabilities", "catalog", "herdr"):
            status, value = await self._request("GET", "/v1/" + name, token)
            self.assertEqual(status, 200, "device-scoped resource was not available: " + name)
            local = await self._ipc(name, token)
            self.assertTrue(local.get("ok"), "IPC resource was not available: " + name)
            self.assertEqual(value.get("contract_revision"), self.ready["contract_revision"])
            values[name] = value
            if name == "state":
                self.assertEqual(value["device_id"], device_id)
                self.assertEqual(local["result"]["device_id"], device_id)
                self.assertEqual(value["host"]["name"], "omarchy-fixture")
                self.assertEqual(local["result"]["agent"]["status"], "working")
            elif name == "catalog":
                ids = {entry["id"] for entry in value["entries"]}
                self.assertTrue({"apps", "omodachi.agent", "omodachi.herdr"} <= ids)
                self.assertEqual({entry["id"] for entry in local["result"]["entries"]}, ids)
            elif name == "herdr":
                self.assertTrue(value["available"])
                self.assertTrue(local["result"]["available"])
        self.assertFalse(values["capabilities"]["sunshine"])
        self.assertFalse(values["capabilities"]["desktop"])
        self.assertEqual(values["state"]["agent"]["default_agent"]["actual_kind"], "codex")
        return values

    async def test_cr01_live_issue_and_populated_http_unix_resources_without_sunshine(self):
        status, _ = await self._request("GET", "/health")
        self.assertEqual(status, 200)
        status, _ = await self._request("GET", "/v1/state")
        self.assertEqual(status, 401)
        self.assertFalse((await self._ipc("state"))["ok"])
        await self._assert_resources(self.phone, "phone")
        await self._assert_resources(self.tablet, "tablet")

    async def test_cr02_remote_is_unavailable_in_demo_and_confines_nothing_else(self):
        _, before = await self._request("GET", "/v1/state", self.tablet)
        websocket, _ = await self._websocket(self.tablet)
        status, value = await self._request("GET", "/v1/remote/capabilities", self.phone)
        self.assertEqual(status, 200, value)
        self.assertFalse(value["backends"]["sunshine"]["available"])
        self.assertFalse(value["backends"]["vnc"]["available"])
        status, refused = await self._request("POST", "/v1/remote/sessions", self.phone, {})
        self.assertEqual(status, 503, refused)
        self.assertEqual(refused["error"]["code"], "remote_runtime_unavailable")
        self.assertFalse((await self._ipc("remote.start", self.tablet))["ok"])
        resources = await self._assert_resources(self.tablet, "tablet")
        for entry_id in ("omodachi.agent", "omodachi.herdr"):
            status, result = await self._request("POST", "/v1/actions/" + entry_id + ":invoke", self.tablet,
                {"request_id": "busy-" + entry_id, "catalog_revision": resources["catalog"]["revision"]})
            self.assertEqual(status, 200)
            self.assertEqual(result["status"], "prepared")
            self.assertEqual(result["route"]["route"], "terminal")
        status, missing = await self._request("GET", "/v1/remote/sessions/rs_" + "0" * 32, self.phone)
        self.assertEqual(status, 503, missing)
        _, after = await self._request("GET", "/v1/state", self.tablet)
        self.assertIsNone(after["remote"]["session_id"])
        self.assertGreaterEqual(after["revision"], before["revision"])
        await websocket.close()
        await self._assert_resources(self.tablet, "tablet")

    async def test_host_preferences_survive_daemon_restart(self):
        local = JsonLineClient(str(self.socket_path))
        saved = await local.request("local.preferences.set", expected_revision=0,
            changes={"quality": "performance", "host_audio_playback": True})
        self.assertTrue(saved["ok"], saved)
        self.daemon.send_signal(signal.SIGTERM)
        await asyncio.wait_for(self.daemon.wait(), 3)
        self.assertEqual(self.daemon.returncode, 0)
        await self._start_daemon()
        readback = await JsonLineClient(str(self.socket_path)).request("local.preferences.get")
        self.assertEqual(readback["result"]["revision"], 1)
        self.assertEqual(readback["result"]["values"], saved["result"]["values"])
        status, preferences = await self._request("GET", "/v1/preferences", self.phone)
        self.assertEqual(status, 200, preferences)
        self.assertEqual(preferences["profile_defaults"]["quality"]["fps"], 30)
        self.assertTrue(preferences["values"]["host_audio_playback"])

    async def test_cr03_cli_revocation_closes_old_ws_and_unix_subscriptions(self):
        websocket, _ = await self._websocket(self.phone)
        _, state = await self._request("GET", "/v1/state", self.phone)
        reader, _, _ = await self._ipc_subscription(self.phone, since=state["event_cursor"], instance_id=state["instance_id"])
        result = await self._revoke("phone")
        self.assertGreaterEqual(result["revoked"], 1)
        self.assertFalse(result["media"]["media_authorized"])
        self.assertFalse(result["media"]["certificate_revocation_supported"])
        message = await asyncio.wait_for(websocket.receive(), 3)
        self.assertIn(message.type, (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR))
        rejected = json.loads(await asyncio.wait_for(reader.readline(), 3))
        self.assertFalse(rejected.get("ok"))
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        status, _ = await self._request("GET", "/v1/state", self.phone)
        self.assertEqual(status, 401)
        self.assertFalse((await self._ipc("state", self.phone))["ok"])
        await self._assert_resources(self.tablet, "tablet")

    async def test_cr04_auth_and_revocation_survive_restart_with_new_event_instance(self):
        _, previous = await self._request("GET", "/v1/state", self.phone)
        await self._revoke("tablet")
        self.daemon.send_signal(signal.SIGTERM)
        await asyncio.wait_for(self.daemon.wait(), 3)
        self.assertEqual(self.daemon.returncode, 0)
        await self._start_daemon()
        _, current = await self._request("GET", "/v1/state", self.phone)
        self.assertNotEqual(current["instance_id"], previous["instance_id"])
        self.assertEqual(current["device_id"], "phone")
        status, _ = await self._request("GET", "/v1/state", self.tablet)
        self.assertEqual(status, 401)
        _, first_message = await self._websocket(self.phone, since=previous["event_cursor"], instance_id=previous["instance_id"])
        self.assertEqual(first_message["type"], "snapshot")
        self.assertEqual(first_message["instance_id"], current["instance_id"])
        replay = await self._ipc("events", self.phone, since=previous["event_cursor"], instance_id=previous["instance_id"])
        self.assertEqual(replay["result"]["events"][0]["type"], "resync.required")
        await self._assert_resources(self.phone, "phone")

    async def test_cr05_concurrent_cli_issues_preserve_all_credentials(self):
        devices = ["concurrent-" + str(index) for index in range(8)]
        tokens = await asyncio.gather(*(self._issue(device) for device in devices))
        results = await asyncio.gather(*(self._request("GET", "/v1/state", token) for token in tokens))
        for device, (status, state) in zip(devices, results):
            self.assertEqual(status, 200, "a concurrent registry update was lost")
            self.assertEqual(state["device_id"], device)
        self.assertTrue((await self._ipc("state", self.phone))["ok"])
        self.assertTrue((await self._ipc("state", self.tablet))["ok"])

    async def test_cr06_second_daemon_on_same_socket_does_not_disrupt_first(self):
        before = self.socket_path.stat()
        competitor = await self._spawn(*self._daemon_args())
        stdout, _ = await asyncio.wait_for(competitor.communicate(), 8)
        self.assertNotEqual(competitor.returncode, 0)
        self.assertNotIn(b'"ready": true', stdout)
        after = self.socket_path.stat()
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        self.assertIsNone(self.daemon.returncode)
        await self._assert_resources(self.phone, "phone")

    async def test_cr07_sigterm_with_idle_ws_and_ipc_subscribers_exits_within_three_seconds(self):
        websocket, _ = await self._websocket(self.phone)
        _, state = await self._request("GET", "/v1/state", self.phone)
        reader, _, _ = await self._ipc_subscription(self.phone, since=state["event_cursor"], instance_id=state["instance_id"])
        # Keep both subscribers alive and idle: neither client initiates close.
        started = time.monotonic()
        self.daemon.send_signal(signal.SIGTERM)
        await asyncio.wait_for(self.daemon.wait(), 3)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3)
        self.assertEqual(self.daemon.returncode, 0)
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        message = await asyncio.wait_for(websocket.receive(), 1)
        self.assertIn(message.type, (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR))

    async def test_replay_150_real_fixture_actions_over_ws_and_ipc(self):
        _, state = await self._request("GET", "/v1/state", self.phone)
        _, catalog = await self._request("GET", "/v1/catalog", self.phone)
        expected = ["replay-" + str(index) for index in range(150)]
        for request_id in expected:
            status, result = await self._request("POST", "/v1/actions/omodachi.herdr:invoke", self.phone,
                {"request_id": request_id, "catalog_revision": catalog["revision"]})
            self.assertEqual(status, 200, "synthetic registered action failed")
            self.assertEqual(result["status"], "prepared")
        websocket, ready = await self._websocket(self.phone, since=state["event_cursor"], instance_id=state["instance_id"])
        self.assertEqual(ready["type"], "ready")
        observed = []
        for _ in range(200):
            payload = await asyncio.wait_for(websocket.receive_json(), 2)
            event = payload.get("event", {})
            self.assertNotEqual(event.get("type"), "resync.required")
            if event.get("type") == "action.result":
                observed.append(event["payload"]["request_id"])
            if len(observed) == len(expected):
                break
        self.assertEqual(observed, expected)
        reader, _, acknowledgment = await self._ipc_subscription(self.phone, since=state["event_cursor"], instance_id=state["instance_id"])
        self.assertEqual(acknowledgment["result"]["instance_id"], state["instance_id"])
        local_observed = []
        for _ in range(200):
            payload = json.loads(await asyncio.wait_for(reader.readline(), 2))
            event = payload.get("event", {})
            self.assertNotEqual(event.get("type"), "resync.required")
            if event.get("type") == "action.result":
                local_observed.append(event["payload"]["request_id"])
            if len(local_observed) == len(expected):
                break
        self.assertEqual(local_observed, expected)
        # The other authorized device sees shared state but none of these results.
        unrelated = await self._ipc("events", self.tablet, since=state["event_cursor"], limit=1000,
                                    instance_id=state["instance_id"])
        self.assertFalse(any(event["type"] == "action.result" for event in unrelated["result"]["events"]))


if __name__ == "__main__":
    unittest.main()
