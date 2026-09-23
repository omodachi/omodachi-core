import asyncio
import json, tempfile, unittest
from pathlib import Path
from omodachi_core.auth import DeviceAuthenticator
from omodachi_core.hub import Hub
from omodachi_core.ipc import JsonLineClient, JsonLineServer

class HubTests(unittest.TestCase):
    def test_auth_issue_verify_expiry_and_revoke(self):
        auth = DeviceAuthenticator(secret=b"x"*32, ttl_seconds=10)
        cred = auth.issue("phone", now=100)
        self.assertEqual(auth.verify(cred.token, now=105), "phone")
        with self.assertRaises(ValueError): auth.verify(cred.token, now=111)
        auth.revoke(cred.token)
        with self.assertRaises(ValueError): auth.verify(cred.token, now=105)

    def test_remote_projection_starts_offline_and_blocks_nothing(self):
        h = Hub(authenticator=DeviceAuthenticator(secret=b"s"*32))
        h.register_device("a"); h.register_device("b")
        self.assertEqual(h.state_snapshot("a")["remote"],
                         {"session_id": None, "state": "offline", "mode": None, "backend": None, "revision": 0})
        h.update_state({"workspace": {"active": 2}}, device_id="b")
        self.assertEqual(h.state_snapshot("b")["workspace"]["active"], 2)
        self.assertEqual(h.events_since(device_id="b")[-1].device_id, "b")

    def test_the_hub_has_no_built_in_remote_operation(self):
        h = Hub(authenticator=DeviceAuthenticator(secret=b"s"*32))
        h.register_device("a")
        with self.assertRaisesRegex(ValueError, "unknown op"):
            h.dispatch("remote.start", {}, "a")

    def test_a_large_state_patch_is_announced_without_its_contents(self):
        """PERF-4. The history keeps a thousand events; the catalog is not in them.

        The daemon was found at 12 GB resident with 11 GB in swap, growing
        1.34 MB/s. Every `catalog.changed` carried 594 catalog rows, and
        `Hub.publish` deep-copies a payload once for the history and once per
        subscriber queue: 375 KiB of live objects per event, measured.
        """
        hub = Hub(history_limit=1000)
        rows = [{"id": f"row.{n}", "label": "x" * 64} for n in range(594)]
        catalog = {"contract_revision": "omodachi.v1", "revision": "a" * 16,
                   "source_revision": "b" * 16, "entries": rows}
        event = hub.update_state({"catalog": catalog}, event_type="catalog.changed",
                                 event_patch={"catalog": {k: v for k, v in catalog.items() if k != "entries"}})
        # The state is complete: a client reads the rows back from it.
        self.assertEqual(hub.state_snapshot()["catalog"]["entries"], rows)
        # The event is not.
        self.assertEqual(set(event.payload["catalog"]), {"contract_revision", "revision", "source_revision"})
        self.assertNotIn("entries", event.payload["catalog"])
        self.assertLess(len(json.dumps(hub.events_since(0)[-1].payload)), 512)
        with self.assertRaises(ValueError):
            hub.update_state({"catalog": catalog}, event_patch={"revision": 3})

    def test_ipc_requires_auth_and_handles_state(self):
        async def run():
            h = Hub(authenticator=DeviceAuthenticator(secret=b"z"*32))
            token = h.register_device("phone")
            path = str(Path(tempfile.mkdtemp()) / "hub.sock")
            server = JsonLineServer(h, path); await server.start()
            try:
                self.assertFalse((await JsonLineClient(path).request("state"))["ok"])
                response = await JsonLineClient(path, token).request("state")
                self.assertTrue(response["ok"] and response["result"]["device_id"] == "phone")
                self.assertTrue((await JsonLineClient(path, token).request("state"))["ok"])
            finally: await server.close()
        asyncio.run(run())

if __name__ == "__main__": unittest.main()
