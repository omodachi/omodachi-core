"""Existing NetworkServer -> discovery lifecycle handoff, no real multicast."""
import asyncio
import socket
from pathlib import Path
import unittest
from types import SimpleNamespace
from omodachi_core.lan_discovery import DiscoveryConfig
from omodachi_core.network_discovery import NetworkDiscoveryBinding

class Publisher:
    def __init__(self,calls):self.calls=calls
    async def register(self,descriptor):self.calls.append(('register',descriptor.port))
    async def unregister(self):self.calls.append(('unregister',))
    async def close(self):self.calls.append(('close',))

class FakeServer:
    def __init__(self,address='0.0.0.0',port=8099,serving=True):
        self._socket=socket.socket();self._socket.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        self._socket.bind((address,port));self._socket.listen();self._serving=serving
    @property
    def sockets(self):return [self._socket]
    def is_serving(self):return self._serving
    def close(self):self._serving=False;self._socket.close()

class BindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls=[];self.server=FakeServer();self.network=SimpleNamespace(site=SimpleNamespace(_server=self.server),certificate='cert',private_key='key')
        self.config=DiscoveryConfig('Omodachi Test','omodachi-test.local.',('192.168.50.10',))
    async def asyncTearDown(self):
        try:self.server.close()
        except OSError:pass
    async def test_secure_non_loopback_server_registers_after_start_and_withdraws_before_stop(self):
        binding=NetworkDiscoveryBinding(self.network,__import__('omodachi_core.lan_discovery',fromlist=['ListenerDiscovery']).ListenerDiscovery(self.config,publisher_factory=lambda:Publisher(self.calls)))
        ready=await binding.start_after_listener();self.assertTrue(ready['advertised']);self.assertEqual(self.calls,[('register',self.server.sockets[0].getsockname()[1])])
        await binding.start_after_listener();self.assertEqual(len(self.calls),1)
        stopped=await binding.stop_before_listener();self.assertFalse(stopped['advertised']);self.assertEqual([x[0] for x in self.calls],['register','unregister','close'])
        self.server.close();await binding.close()
    async def test_loopback_and_insecure_listener_suppress_publisher(self):
        self.server.close();self.server=FakeServer('127.0.0.1');self.network.site._server=self.server
        binding=NetworkDiscoveryBinding(self.network,__import__('omodachi_core.lan_discovery',fromlist=['ListenerDiscovery']).ListenerDiscovery(self.config,publisher_factory=lambda:Publisher(self.calls)))
        state=await binding.start_after_listener();self.assertFalse(state['advertised']);self.assertEqual(self.calls,[])
        self.server.close();self.server=FakeServer();self.network.site._server=self.server;self.network.certificate=None;self.network.private_key=None
        state=await binding.stop_before_listener();self.assertFalse(state['advertised']);self.assertEqual(self.calls,[])
    async def test_ipc_only_no_site_is_inert_and_reports_not_ready(self):
        network=SimpleNamespace(site=None,certificate=None,private_key=None)
        binding=NetworkDiscoveryBinding(network,__import__('omodachi_core.lan_discovery',fromlist=['ListenerDiscovery']).ListenerDiscovery(self.config,publisher_factory=lambda:Publisher(self.calls)))
        with self.assertRaisesRegex(Exception,'listener_not_ready'):await binding.start_after_listener()
        self.assertEqual(self.calls,[])
    async def test_listener_stop_order_is_explicit(self):
        order=[]
        class Ordered(Publisher):
            async def unregister(self):order.append('withdraw');await super().unregister()
        binding=NetworkDiscoveryBinding(self.network,__import__('omodachi_core.lan_discovery',fromlist=['ListenerDiscovery']).ListenerDiscovery(self.config,publisher_factory=lambda:Ordered(self.calls)))
        await binding.start_after_listener();await binding.stop_before_listener();order.append('listener_close');self.server.close()
        self.assertEqual(order,['withdraw','listener_close'])

class NetworkServerHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_server_accepts_explicit_discovery_config_without_constructing_publisher_for_loopback(self):
        from unittest.mock import patch
        from omodachi_core.auth import DeviceAuthenticator
        from omodachi_core.bootstrap import create_service
        from omodachi_core.hub import Hub
        from omodachi_core.network import NetworkServer
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            authority=DeviceAuthenticator.from_file(Path(directory)/'secret')
            service=create_service(Hub(authenticator=authority),demo=True)
            calls=[]
            def factory():calls.append('constructed');return Publisher(calls)
            network=NetworkServer(service,host='127.0.0.1',port=0,allow_loopback_http=True,
                discovery_config=DiscoveryConfig('Fixture','fixture.local.',('192.168.1.20',)),discovery_publisher_factory=factory)
            await network.start();self.assertEqual(calls,[])
            self.assertFalse(network.discovery.last_state['advertised']);await network.close();self.assertEqual(calls,[])
            await service.close_media()
    async def test_network_server_discovery_config_can_exist_without_start_or_publisher(self):
        from omodachi_core.network import NetworkServer
        class Dummy: pass
        value=NetworkServer(Dummy(),discovery_config=DiscoveryConfig('Fixture','fixture.local.',('192.168.1.20',)))
        self.assertIsNotNone(value.discovery);self.assertFalse(value.discovery.last_state is not None)
