"""The WSS <-> TCP bridge that carries WayVNC over the pinned TLS connection.

Everything here runs against a real aiohttp server, a real WebSocket and a real
loopback TCP server standing in for WayVNC. Nothing is stubbed at the transport
layer, because the whole point of the bridge is that the bytes are untouched.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

import aiohttp

from omodachi_core.auth import DeviceAuthenticator
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
from omodachi_core.official_bar_position import OfficialBarPosition
from omodachi_core.remote import RemoteManager
from omodachi_core.remote.backends import SunshineBackend, VncBackend
from omodachi_core.remote.hyprland import Hyprland
from omodachi_core.remote.profile import EncoderLimits
from tests.remote_fakes import FakeBar, FakeCompositor, FakeSunshine, FakeWayVNC, INSTANCE, profile_request

GREETING = b"RFB 003.008\n"


class LoopbackWayVNC:
    """A loopback TCP server that speaks the first two RFB moves and echoes."""

    def __init__(self):
        self.server = None
        self.port = 0
        self.received = bytearray()
        self.connections = 0
        self.eof = asyncio.Event()
        self.writers: list[asyncio.StreamWriter] = []

    async def start(self):
        async def serve(reader, writer):
            self.connections += 1
            self.writers.append(writer)
            writer.write(GREETING)
            await writer.drain()
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    self.received += data
                    # Echo the bytes unchanged: any framing the bridge added in
                    # either direction would show up as a difference here.
                    writer.write(data)
                    await writer.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                self.eof.set()
                writer.close()
        self.server = await asyncio.start_server(serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self):
        for writer in self.writers:
            writer.close()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


class VncBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.wayvnc = LoopbackWayVNC()
        await self.wayvnc.start()
        self.addAsyncCleanup(self.wayvnc.close)
        build = FakeWayVNC.factory()

        def factory(session_id):
            instance = build(session_id)
            instance.port = self.wayvnc.port
            return instance

        self.compositor = FakeCompositor()
        self.manager = RemoteManager(
            hyprland=Hyprland(INSTANCE, runner=self.compositor), journal_dir=self.root / "remote",
            encoder=EncoderLimits(4096, 4096, 16_777_216, 60, 40_000, 2, 2),
            sunshine=SunshineBackend(FakeSunshine(), certificate_resolver=lambda device: "a" * 64,
                                     address=lambda: "192.168.1.11"),
            vnc=VncBackend(factory),
            bar_position=OfficialBarPosition(home=self.root, read_position=FakeBar().read,
                                             set_position=FakeBar().set))
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

    async def start(self, **payload):
        headers = {"Authorization": "Bearer " + self.token}
        body = {**profile_request(), "backend": "vnc", **payload}
        async with self.http.post(self.url + "/v1/remote/sessions", json=body,
                                  headers=headers) as response:
            value = await response.json()
            self.assertEqual(response.status, 201, value)
            return value["session"]

    def bridge(self, session, token=None):
        return self.http.ws_connect(self.url + session["connection"]["path"],
                                    headers={"Authorization": "Bearer " + (token or self.token)})

    async def test_the_session_document_points_at_the_bridge_and_the_served_size(self):
        session = await self.start()
        connection = session["connection"]
        self.assertEqual(connection["transport"], "wss")
        self.assertEqual(connection["path"], "/v1/remote/sessions/%s/vnc" % session["id"])
        self.assertNotIn("port", connection)
        self.assertNotIn("host", connection)
        profile = session["profile"]
        self.assertEqual(profile["output_scale"], 2.0)
        # REMOTE-6: core settles WayVNC before the client gets a bridge, so
        # what ServerInit will announce is already the buffer pixels.
        self.assertEqual(connection["initial_framebuffer_pixels"], profile["output_mode_pixels"])
        self.assertEqual(connection["framebuffer_pixels"], profile["output_mode_pixels"])
        self.assertEqual(connection["framebuffer_pixels"], profile["stream_pixels"])

    async def test_bytes_cross_in_both_directions_with_no_framing_added(self):
        session = await self.start()
        async with self.bridge(session) as ws:
            first = await asyncio.wait_for(ws.receive(), 5)
            self.assertEqual(first.type, aiohttp.WSMsgType.BINARY)
            self.assertEqual(first.data, GREETING)
            await ws.send_bytes(b"RFB 003.008\n\x01")
            answer = await asyncio.wait_for(ws.receive(), 5)
            self.assertEqual(answer.type, aiohttp.WSMsgType.BINARY)
            self.assertEqual(answer.data, b"RFB 003.008\n\x01")
        self.assertEqual(bytes(self.wayvnc.received), b"RFB 003.008\n\x01")

    async def test_a_large_run_of_pixels_crosses_intact(self):
        session = await self.start()
        payload = bytes(range(256)) * 400  # 102,400 bytes, past one TCP segment
        async with self.bridge(session) as ws:
            self.assertEqual((await asyncio.wait_for(ws.receive(), 5)).data, GREETING)
            await ws.send_bytes(payload)
            seen = bytearray()
            while len(seen) < len(payload):
                message = await asyncio.wait_for(ws.receive(), 5)
                self.assertEqual(message.type, aiohttp.WSMsgType.BINARY)
                seen += message.data
        self.assertEqual(bytes(seen), payload)
        self.assertEqual(bytes(self.wayvnc.received), payload)

    async def test_only_the_session_owner_can_open_the_bridge(self):
        session = await self.start()
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            async with self.bridge(session, token=self.other):
                pass
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.wayvnc.connections, 0)

    async def test_an_unauthenticated_client_never_reaches_the_backend(self):
        session = await self.start()
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            async with self.http.ws_connect(self.url + session["connection"]["path"]):
                pass
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(self.wayvnc.connections, 0)

    async def test_one_bridge_at_a_time_on_one_session(self):
        session = await self.start()
        async with self.bridge(session) as first:
            self.assertEqual((await asyncio.wait_for(first.receive(), 5)).data, GREETING)
            with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
                async with self.bridge(session):
                    pass
            self.assertEqual(caught.exception.status, 409)
            self.assertEqual(self.wayvnc.connections, 1)
        # The slot is given back, so a reconnect after a drop still works.
        for _ in range(50):
            try:
                async with self.bridge(session) as second:
                    self.assertEqual((await asyncio.wait_for(second.receive(), 5)).data, GREETING)
                break
            except aiohttp.WSServerHandshakeError:
                await asyncio.sleep(0.05)
        else:
            self.fail("the bridge slot was never released")

    async def test_a_sunshine_session_has_no_bridge(self):
        session = await self.start(backend="sunshine")
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            async with self.http.ws_connect(
                    self.url + "/v1/remote/sessions/%s/vnc" % session["id"],
                    headers={"Authorization": "Bearer " + self.token}):
                pass
        self.assertEqual(caught.exception.status, 409)

    async def test_an_unknown_session_is_a_404_and_touches_nothing(self):
        await self.start()
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            async with self.http.ws_connect(
                    self.url + "/v1/remote/sessions/rs_" + "0" * 32 + "/vnc",
                    headers={"Authorization": "Bearer " + self.token}):
                pass
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.wayvnc.connections, 0)

    async def test_the_client_closing_closes_the_backend_socket(self):
        session = await self.start()
        async with self.bridge(session) as ws:
            self.assertEqual((await asyncio.wait_for(ws.receive(), 5)).data, GREETING)
            await ws.close()
        await asyncio.wait_for(self.wayvnc.eof.wait(), 5)

    async def test_the_backend_closing_closes_the_websocket(self):
        session = await self.start()
        async with self.bridge(session) as ws:
            self.assertEqual((await asyncio.wait_for(ws.receive(), 5)).data, GREETING)
            await self.wayvnc.close()
            message = await asyncio.wait_for(ws.receive(), 5)
            self.assertIn(message.type, {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED})

    async def test_releasing_the_session_ends_the_bridge(self):
        session = await self.start()
        async with self.bridge(session) as ws:
            self.assertEqual((await asyncio.wait_for(ws.receive(), 5)).data, GREETING)
            async with self.http.delete(self.url + "/v1/remote/sessions/" + session["id"],
                                        headers={"Authorization": "Bearer " + self.token}) as response:
                self.assertEqual(response.status, 200)
            message = await asyncio.wait_for(ws.receive(), 5)
            self.assertIn(message.type, {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED})

    async def test_a_text_frame_is_not_a_control_channel(self):
        session = await self.start()
        async with self.bridge(session) as ws:
            self.assertEqual((await asyncio.wait_for(ws.receive(), 5)).data, GREETING)
            await ws.send_str("please stop")
            message = await asyncio.wait_for(ws.receive(), 5)
            self.assertIn(message.type, {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED})
        self.assertEqual(bytes(self.wayvnc.received), b"")

    async def test_a_revoked_credential_ends_a_live_bridge(self):
        session = await self.start()
        async with self.bridge(session) as ws:
            self.assertEqual((await asyncio.wait_for(ws.receive(), 5)).data, GREETING)
            self.hub.auth.revoke_device("ipad-a")
            message = await asyncio.wait_for(ws.receive(), 8)
            self.assertIn(message.type, {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED})


if __name__ == "__main__":
    unittest.main()
