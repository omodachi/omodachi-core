"""Regression checks for IPC, credential sharing, and lossless device events."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest.mock import patch

from omodachi_core.auth import DeviceAuthenticator
from omodachi_core.hub import Hub
from omodachi_core.ipc import JsonLineClient, JsonLineServer


class CredentialSecurityTests(unittest.TestCase):
    def test_existing_daemon_observes_issue_and_revoke_from_another_process_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            daemon = DeviceAuthenticator.from_file(path)
            cli = DeviceAuthenticator.from_file(path)
            first = cli.issue("phone").token
            self.assertEqual(daemon.verify(first), "phone")
            second = daemon.issue("tablet").token
            self.assertEqual(cli.verify(second), "tablet")
            cli.revoke(first)
            with self.assertRaises(ValueError):
                daemon.verify(first)
            self.assertEqual(daemon.verify(second), "tablet")
            for credential_file in Path(directory).iterdir():
                self.assertEqual(stat.S_IMODE(credential_file.stat().st_mode), 0o600)
            registry = path.with_suffix(".credentials.json").read_text()
            self.assertNotIn(first, registry)
            self.assertNotIn(second, registry)

    def test_secret_and_registry_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            victim = directory / "victim"
            victim.write_bytes(b"a" * 32)
            secret = directory / "secret"
            secret.symlink_to(victim)
            with self.assertRaises(OSError):
                DeviceAuthenticator.from_file(secret)
            self.assertEqual(victim.read_bytes(), b"a" * 32)
            secret.unlink()
            auth = DeviceAuthenticator.from_file(secret)
            registry = secret.with_suffix(".credentials.json")
            registry.symlink_to(victim)
            with self.assertRaises(OSError):
                auth.issue("phone")
            self.assertEqual(victim.read_bytes(), b"a" * 32)

    def test_registry_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            auth = DeviceAuthenticator.from_file(path)
            token = auth.issue("phone").token
            path.with_suffix(".credentials.json").write_text("broken")
            with self.assertRaises(ValueError):
                auth.verify(token)

    def test_failed_atomic_registry_replace_preserves_previous_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            auth = DeviceAuthenticator.from_file(path)
            token = auth.issue("phone").token
            before = path.with_suffix(".credentials.json").read_bytes()
            with patch("omodachi_core.auth.os.replace", side_effect=OSError("simulated write failure")):
                with self.assertRaises(OSError):
                    auth.issue("tablet")
            self.assertEqual(path.with_suffix(".credentials.json").read_bytes(), before)
            self.assertEqual(auth.verify(token), "phone")
            self.assertEqual(len(list(Path(directory).glob(".secret.credentials.json.*"))), 0)


class EventSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscription_replays_more_than_100_and_queues_during_replay(self):
        hub = Hub()
        for number in range(180):
            hub.publish("sample", {"number": number})
        subscription = hub.subscribe(device_id="phone")
        try:
            first = await anext(subscription)
            self.assertEqual(first.seq, 1)
            hub.publish("sample", {"number": 180})
            remaining = [await asyncio.wait_for(anext(subscription), 1) for _ in range(180)]
            self.assertEqual([first.seq] + [event.seq for event in remaining], list(range(1, 182)))
        finally:
            await subscription.aclose()

    async def test_target_filter_and_payload_copies(self):
        hub = Hub()
        original = {"nested": {"secret": "original"}}
        returned = hub.publish("panel.summon", original, device_id="owner")
        original["nested"]["secret"] = "changed"
        returned.payload["nested"]["secret"] = "also changed"
        self.assertEqual(hub.events_since(device_id="other"), [])
        self.assertEqual(hub.events_since(), [])
        history = hub.events_since(device_id="owner")
        self.assertEqual(history[0].payload["nested"]["secret"], "original")
        history[0].payload.clear()
        self.assertTrue(hub.events_since(device_id="owner")[0].payload)
        with self.assertRaises(ValueError):
            hub.publish("panel.summon", {})
        patch_value = {"host": {"name": "fixture"}}
        hub.update_state(patch_value)
        patch_value["host"]["name"] = "mutated"
        self.assertEqual(hub.state_snapshot()["host"]["name"], "fixture")

    async def test_overflow_and_missing_history_require_resync(self):
        hub = Hub(subscriber_queue_limit=2)
        subscription = hub.subscribe(device_id="phone")
        first = asyncio.create_task(anext(subscription))
        await asyncio.sleep(0)
        hub.publish("sample", {})
        await first
        for number in range(3):
            hub.publish("sample", {"number": number})
        event = await anext(subscription)
        self.assertEqual(event.type, "resync.required")
        self.assertEqual(event.payload["reason"], "queue_overflow")
        with self.assertRaises(StopAsyncIteration):
            await anext(subscription)
        trimmed = Hub(history_limit=2)
        for number in range(4):
            trimmed.publish("sample", {"number": number})
        self.assertEqual(trimmed.events_since(0, limit=2, device_id="phone")[0].type, "resync.required")

    async def test_idle_stream_revalidates_revoked_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            daemon = DeviceAuthenticator.from_file(path)
            token = daemon.issue("phone").token
            hub = Hub(authenticator=daemon, auth_check_interval=0.01)
            subscription = hub.subscribe(token=token)
            pending = asyncio.create_task(anext(subscription))
            await asyncio.sleep(0)
            DeviceAuthenticator.from_file(path).revoke(token)
            with self.assertRaises(ValueError):
                await asyncio.wait_for(pending, 1)
            self.assertFalse(hub._subscribers)

    async def test_remote_projection_changes_broadcast_a_revision(self):
        hub = Hub()
        revision = hub.state_snapshot("tablet")["revision"]
        hub.update_state({"remote": {"session_id": "rs_" + "0" * 32, "state": "ready",
                                     "mode": "extend", "backend": "vnc", "revision": 2}},
                         event_type="remote.changed")
        snapshot = hub.state_snapshot("tablet")
        self.assertEqual(snapshot["revision"], revision + 1)
        self.assertEqual(snapshot["remote"]["state"], "ready")
        event = hub.events_since(device_id="tablet")[-1]
        self.assertEqual(event.type, "remote.changed")
        self.assertIsNone(event.device_id)

    async def test_dispatch_extension_receives_explicit_device_and_copy(self):
        hub = Hub()
        def handler(device_id, params):
            params["nested"]["value"] = "handled"
            return {"device_id": device_id, "params": params}
        hub.register_handler("test.operation", handler)
        original = {"nested": {"value": "original"}}
        result = hub.dispatch("test.operation", original, "phone")
        self.assertEqual(result["device_id"], "phone")
        self.assertEqual(original["nested"]["value"], "original")
        with self.assertRaises(ValueError):
            hub.dispatch("test.operation", original)
        with self.assertRaises(ValueError):
            hub.register_handler("state", handler)


class IPCSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temporary.name) / "hub.sock")
        self.hub = Hub(auth_check_interval=0.01)
        self.token = self.hub.register_device("phone")
        self.server = JsonLineServer(self.hub, self.path, request_timeout=0.05)
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.close()
        self.temporary.cleanup()

    async def test_second_daemon_does_not_unlink_active_socket(self):
        before = os.lstat(self.path)
        other = JsonLineServer(Hub(), self.path)
        with self.assertRaises(OSError):
            await other.start()
        await other.close()
        after = os.lstat(self.path)
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        self.assertTrue((await JsonLineClient(self.path).request("health"))["ok"])

    async def test_a_dead_socket_is_reclaimed_but_a_regular_file_is_not(self):
        # kill -9 leaves the bound inode behind; a daemon that cannot rebind can
        # never run the Remote journal recovery at startup either.
        dead = str(Path(self.temporary.name) / "dead.sock")
        abandoned = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        abandoned.bind(dead)
        abandoned.listen(1)
        abandoned.close()  # the process is gone; nothing unlinked the path
        self.assertTrue(os.path.exists(dead))
        replacement = JsonLineServer(Hub(), dead)
        await replacement.start()
        try:
            self.assertTrue((await JsonLineClient(dead).request("health"))["ok"])
        finally:
            await replacement.close()
        blocked = Path(self.temporary.name) / "regular"
        blocked.write_text("not a socket")
        with self.assertRaises(OSError):
            await JsonLineServer(Hub(), str(blocked)).start()
        self.assertEqual(blocked.read_text(), "not a socket")

    async def test_unknown_and_different_uid_fail_closed(self):
        for uid in (None, os.getuid() + 1):
            with patch.object(self.server, "_peer_uid", return_value=uid):
                response = await JsonLineClient(self.path).request("health")
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"], "PermissionError")

    async def test_oversized_frame_times_out_and_server_recovers(self):
        for frame in (b"x" * (self.server.max_frame_bytes + 1), b'{"op":'):
            reader, writer = await asyncio.open_unix_connection(self.path)
            try:
                writer.write(frame)
                await writer.drain()
                result = json.loads(await asyncio.wait_for(reader.readline(), 1))
                self.assertFalse(result["ok"])
                self.assertEqual(await reader.read(), b"")
            finally:
                writer.close()
                await writer.wait_closed()
        self.assertTrue((await JsonLineClient(self.path).request("health"))["ok"])

    async def test_subscription_disconnect_removes_handler_and_subscriber(self):
        reader, writer = await asyncio.open_unix_connection(self.path)
        writer.write((json.dumps({"op": "events.subscribe", "token": self.token}) + "\n").encode())
        await writer.drain()
        self.assertTrue(json.loads(await reader.readline())["ok"])
        await asyncio.sleep(0)
        writer.close()
        await writer.wait_closed()
        for _ in range(100):
            if not self.server._connections:
                break
            await asyncio.sleep(0.001)
        self.assertFalse(self.server._connections)
        self.assertFalse(self.hub._subscribers)

    async def test_subscriber_closes_after_credential_revocation(self):
        reader, writer = await asyncio.open_unix_connection(self.path)
        try:
            writer.write((json.dumps({"op": "events.subscribe", "token": self.token}) + "\n").encode())
            await writer.drain()
            self.assertTrue(json.loads(await reader.readline())["ok"])
            self.hub.auth.revoke(self.token)
            rejected = json.loads(await asyncio.wait_for(reader.readline(), 1))
            self.assertFalse(rejected["ok"])
            self.assertEqual(rejected["error"], "ValueError")
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_close_preserves_replacement_file(self):
        os.unlink(self.path)
        Path(self.path).write_text("replacement")
        await self.server.close()
        self.assertEqual(Path(self.path).read_text(), "replacement")


if __name__ == "__main__":
    unittest.main()

class RestartCursorTests(unittest.IsolatedAsyncioTestCase):
    async def test_previous_instance_cursor_requires_snapshot_even_when_seq_matches(self):
        first, restarted = Hub(), Hub()
        first.publish("sample", {})
        restarted.publish("sample", {})
        self.assertNotEqual(first.instance_id, restarted.instance_id)
        subscription = restarted.subscribe(since=1, device_id="phone", instance_id=first.instance_id)
        event = await anext(subscription)
        self.assertEqual(event.type, "resync.required")
        self.assertEqual(event.payload["reason"], "daemon_restarted")
        self.assertEqual(event.payload["instance_id"], restarted.instance_id)
        await subscription.aclose()
        self.assertEqual(restarted.state_snapshot()["instance_id"], restarted.instance_id)

    async def test_close_with_idle_subscriber_finishes_before_one_second(self):
        with tempfile.TemporaryDirectory() as directory:
            hub = Hub()
            token = hub.register_device("phone")
            path = str(Path(directory) / "hub.sock")
            server = JsonLineServer(hub, path, write_timeout=0.25)
            await server.start()
            reader, writer = await asyncio.open_unix_connection(path)
            try:
                writer.write((json.dumps({"op": "events.subscribe", "token": token}) + "\n").encode())
                await writer.drain()
                ack = json.loads(await reader.readline())
                self.assertEqual(ack["result"]["instance_id"], hub.instance_id)
                await asyncio.sleep(0)
                await asyncio.wait_for(server.close(), 1)
                self.assertFalse(server._connections)
                self.assertFalse(hub._subscribers)
                self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
            finally:
                writer.close()
                await writer.wait_closed()
                await server.close()
