"""Public discovery lifecycle without multicast, credentials or trust changes.

All publishers and zeroconf constructors are injected; only one temporary local
HTTP listener is opened to verify actual bound-port observation/suppression.
"""
import asyncio
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch
from aiohttp import web

from omodachi_core.lan_discovery import (DiscoveryConfig,DiscoveryError,ReadyListener,
    ListenerDiscovery,PublicServiceDescriptor,ZeroconfPublisher,descriptor_for,SERVICE_TYPE)


def config(**kwargs):
    return DiscoveryConfig(**({'display_name':'Omodachi Test Host','server_name':'omodachi-test.local.',
        'interface_addresses':('192.168.50.10',)}|kwargs))

class PublisherFixture:
    def __init__(self,calls):self.calls=calls;self.fail_register=False;self.fail_unregister=False;self.fail_close=False
    async def register(self,descriptor):
        self.calls.append(('register',descriptor))
        if self.fail_register:raise RuntimeError('PRIVATE BACKEND DETAIL')
    async def unregister(self):
        self.calls.append(('unregister',))
        if self.fail_unregister:raise RuntimeError('PRIVATE BACKEND DETAIL')
    async def close(self):
        self.calls.append(('close',))
        if self.fail_close:raise RuntimeError('PRIVATE BACKEND DETAIL')

class DescriptorTests(unittest.TestCase):
    def test_existing_service_type_and_public_allowlist_do_not_imply_trust(self):
        value=descriptor_for(config(),ReadyListener(True,(('192.168.50.10',8099),)))
        self.assertEqual(SERVICE_TYPE,'_omodachi._tcp.local.')
        self.assertEqual(value.port,8099);self.assertEqual(value.to_dict()['origin_hint'],'https://omodachi-test.local:8099')
        self.assertTrue(value.to_dict()['authorization_required']);self.assertEqual(value.to_dict()['trust_state'],'unverified')
        self.assertEqual(value.to_dict()['pairing_state'],'unknown')
        import jsonschema
        schema=json.loads((Path(__file__).resolve().parents[1]/'contracts/lan-discovery.schema.json').read_text())
        jsonschema.Draft202012Validator(schema).validate(value.to_dict())
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate({**value.to_dict(),'credential':'not-public'})
        self.assertEqual(set(value.txt()),{b'txtvers',b'v',b'scheme',b'path',b'auth',b'port'})
        self.assertEqual(value.txt()[b'v'],b'omodachi.v1')
        for forbidden in ('token','pin','certificate','device_id','ssh','media_ready'):
            self.assertNotIn(forbidden,json.dumps(value.to_dict()))
    def test_identity_travels_as_a_label_and_the_fingerprint_only_as_a_prefix(self):
        settings=config(host_id='0'*32,host_name='omarchy',fingerprint='ab'*32)
        value=descriptor_for(settings,ReadyListener(True,(('192.168.50.10',8099),)))
        self.assertEqual(value.txt()[b'host_id'],b'0'*32)
        self.assertEqual(value.txt()[b'host_name'],b'omarchy')
        # Only a 16-hex prefix: enough to tell hosts apart, never enough to pin.
        self.assertEqual(value.txt()[b'fp'],b'ab'*8)
        self.assertNotIn('ab'*32,json.dumps(value.to_dict()))
        self.assertEqual(value.to_dict()['fingerprint_prefix'],'ab'*8)
        import jsonschema
        schema=json.loads((Path(__file__).resolve().parents[1]/'contracts/lan-discovery.schema.json').read_text())
        jsonschema.Draft202012Validator(schema).validate(value.to_dict())
    def test_malformed_identity_is_refused_rather_than_advertised(self):
        for patching in ({'host_id':'not-hex'},{'fingerprint':'ab'*31},{'host_name':'x'*64},
                         {'host_name':'bad\nname'}):
            with self.assertRaises(DiscoveryError):config(**patching)
    def test_bound_specific_ip_and_family_limit_advertised_addresses(self):
        settings=config(interface_addresses=('192.168.50.10','10.0.0.3','fd00::3'))
        specific=descriptor_for(settings,ReadyListener(True,(('192.168.50.10',9000),)))
        self.assertEqual(specific.addresses,('192.168.50.10',))
        wildcard=descriptor_for(settings,ReadyListener(True,(('0.0.0.0',9001),)))
        self.assertEqual(wildcard.addresses,('192.168.50.10','10.0.0.3'))
        ipv6=descriptor_for(settings,ReadyListener(True,(('::',9002),)))
        self.assertEqual(ipv6.addresses,('fd00::3',))
    def test_invalid_names_addresses_and_ports_are_rejected(self):
        for patching in ({'display_name':'a'*64},{'display_name':'host\nsecret'}, {'server_name':'arbitrary.example.'},
                         {'interface_addresses':('127.0.0.1',)},{'interface_addresses':('224.0.0.1',)},
                         {'interface_addresses':('fe80::1%eth0',)},{'interface_addresses':('fe80::1',)},
                         {'interface_addresses':('0.0.0.0',)},{'interface_addresses':([],)}):
            with self.subTest(patching=patching),self.assertRaises(DiscoveryError):config(**patching)
        for endpoints in ((),(('0.0.0.0',0),),(('0.0.0.0',True),),(('0.0.0.0',8099),('::',9000))):
            with self.assertRaises(DiscoveryError):ReadyListener(True,endpoints)
    def test_bound_but_not_listening_socket_is_not_ready(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            with self.assertRaisesRegex(DiscoveryError,'listener_not_ready'):
                ReadyListener.from_server(type('Unready',(),{'sockets':[sock],'is_serving':lambda self:False})(),secure=True)

    def test_closed_socket_is_not_listener_readiness(self):
        sock=socket.socket();sock.close()
        with self.assertRaisesRegex(DiscoveryError,'listener_not_ready'):ReadyListener.from_server(type('Unready',(),{'sockets':[sock],'is_serving':lambda self:False})(),secure=True)

class DiscoveryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls=[];self.publishers=[]
        def factory():
            value=PublisherFixture(self.calls);self.publishers.append(value);return value
        self.lifecycle=ListenerDiscovery(config(),publisher_factory=factory)
    async def asyncTearDown(self):
        for publisher in self.publishers:publisher.fail_unregister=False;publisher.fail_close=False
        await self.lifecycle.close()
    async def test_construct_query_and_non_https_loopback_are_inert(self):
        self.assertEqual(self.calls,[]);self.assertFalse(self.lifecycle.snapshot()['advertised'])
        self.assertEqual((await self.lifecycle.listener_ready(ReadyListener(False,(('0.0.0.0',8099),))))['reason'],'discovery_https_required')
        self.assertEqual((await self.lifecycle.listener_ready(ReadyListener(True,(('127.0.0.1',8099),))))['reason'],'discovery_no_lan_listener')
        self.assertEqual(self.calls,[])
    async def test_ready_duplicate_port_change_and_stop_withdraw_in_order(self):
        ready=ReadyListener(True,(('0.0.0.0',9300),))
        result=await self.lifecycle.listener_ready(ready);self.assertTrue(result['advertised'])
        await self.lifecycle.listener_ready(ready);self.assertEqual(len(self.calls),1)
        await self.lifecycle.listener_ready(ReadyListener(True,(('0.0.0.0',9301),)))
        self.assertEqual([row[0] for row in self.calls],['register','unregister','close','register'])
        self.assertEqual(self.calls[-1][1].port,9301)
        stopped=await self.lifecycle.listener_stopping();self.assertFalse(stopped['advertised']);self.assertIsNone(stopped['service'])
        self.assertEqual([row[0] for row in self.calls][-2:],['unregister','close'])
    async def test_unreachable_listener_transition_withdraws_previous(self):
        await self.lifecycle.listener_ready(ReadyListener(True,(('0.0.0.0',8099),)))
        value=await self.lifecycle.listener_ready(ReadyListener(True,(('127.0.0.1',8099),)))
        self.assertEqual(value['status'],'suppressed');self.assertFalse(value['advertised'])
        self.assertEqual([row[0] for row in self.calls],['register','unregister','close'])
    async def test_registration_failure_is_safe_unavailable_without_listener_effect(self):
        def factory():
            publisher=PublisherFixture(self.calls);publisher.fail_register=True;self.publishers.append(publisher);return publisher
        lifecycle=ListenerDiscovery(config(),publisher_factory=factory)
        result=await lifecycle.listener_ready(ReadyListener(True,(('0.0.0.0',8099),)))
        self.assertEqual(result['status'],'unavailable');self.assertEqual(result['reason'],'discovery_registration_failed')
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertEqual([row[0] for row in self.calls],['register','unregister','close'])
        await lifecycle.close()
    async def test_withdrawal_failure_remains_honest_on_repeated_close(self):
        await self.lifecycle.listener_ready(ReadyListener(True,(('0.0.0.0',8099),)))
        self.publishers[0].fail_unregister=True
        result=await self.lifecycle.close();self.assertEqual(result['status'],'withdrawal_unconfirmed')
        self.assertEqual((await self.lifecycle.close())['status'],'withdrawal_unconfirmed')
        with self.assertRaisesRegex(DiscoveryError,'lifecycle_closed'):
            await self.lifecycle.listener_ready(ReadyListener(True,(('0.0.0.0',8099),)))
    async def test_registration_cancellation_withdraws_owned_publisher(self):
        entered=asyncio.Event();release=asyncio.Event();calls=self.calls
        class Pending(PublisherFixture):
            async def register(self,descriptor):
                calls.append(('register',descriptor));entered.set();await release.wait()
        lifecycle=ListenerDiscovery(config(),publisher_factory=lambda:Pending(calls))
        task=asyncio.create_task(lifecycle.listener_ready(ReadyListener(True,(('0.0.0.0',8099),))))
        await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual([row[0] for row in calls],['register','unregister','close'])
        self.assertFalse(lifecycle.snapshot()['advertised']);await lifecycle.close()

    async def test_registration_timeout_is_bounded_and_cleans_publisher(self):
        calls=self.calls
        class Never(PublisherFixture):
            async def register(self,descriptor):
                calls.append(('register',descriptor));await asyncio.Event().wait()
        lifecycle=ListenerDiscovery(config(),publisher_factory=lambda:Never(calls),operation_timeout=.02)
        value=await asyncio.wait_for(lifecycle.listener_ready(ReadyListener(True,(('0.0.0.0',8099),))),1)
        self.assertEqual(value['reason'],'discovery_registration_timeout');self.assertFalse(value['advertised'])
        self.assertEqual([row[0] for row in calls],['register','unregister','close']);await lifecycle.close()

    async def test_disabled_discovery_never_constructs_publisher(self):
        lifecycle=ListenerDiscovery(config(enabled=False),publisher_factory=lambda:self.fail('unexpected multicast constructor'))
        value=await lifecycle.listener_ready(ReadyListener(True,(('0.0.0.0',8099),)))
        self.assertEqual(value['reason'],'discovery_disabled');await lifecycle.close()
    async def test_actual_ephemeral_http_listener_port_is_observed_but_not_announced(self):
        application=web.Application()
        async def health(request):return web.json_response({'service':'fixture'})
        application.router.add_get('/health',health)
        runner=web.AppRunner(application);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        try:
            facts=ReadyListener.from_server(site._server,secure=False)
            self.assertGreater(facts.endpoints[0][1],0)
            self.assertEqual(facts.endpoints[0][1],site._server.sockets[0].getsockname()[1])
            result=await self.lifecycle.listener_ready(facts)
            self.assertEqual(result['reason'],'discovery_https_required');self.assertEqual(self.calls,[])
        finally:await runner.cleanup()

class ZeroconfAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_api_registration_and_goodbye_await_nested_completion(self):
        calls=[]
        class Info:
            def __init__(self,*args,**kwargs):calls.append(('info',args,kwargs))
        class ZC:
            def __init__(self,**kwargs):calls.append(('construct',kwargs))
            async def async_register_service(self,info,**kwargs):
                calls.append(('register',kwargs))
                async def sent():calls.append(('announcement_complete',))
                return sent()
            async def async_unregister_service(self,info):
                calls.append(('unregister',))
                async def sent():calls.append(('goodbye_complete',))
                return sent()
            async def async_close(self):calls.append(('close',))
        adapter=ZeroconfPublisher();self.assertEqual(calls,[])
        with patch.object(ZeroconfPublisher,'_load',return_value=(Info,ZC)):
            descriptor=descriptor_for(config(),ReadyListener(True,(('192.168.50.10',9443),)))
            await adapter.register(descriptor);await adapter.unregister();await adapter.close()
        self.assertEqual(calls[0],('construct',{'interfaces':['192.168.50.10']}))
        self.assertEqual(calls[1][1],(SERVICE_TYPE,'Omodachi Test Host.'+SERVICE_TYPE))
        self.assertEqual(calls[1][2]['port'],9443);self.assertEqual(calls[1][2]['properties'][b'auth'],b'required')
        self.assertEqual([row[0] for row in calls],['construct','info','register','announcement_complete','unregister','goodbye_complete','close'])
        self.assertFalse(calls[2][1]['allow_name_change'])
    async def test_missing_optional_dependency_never_claims_advertised(self):
        lifecycle=ListenerDiscovery(config())
        with patch.object(ZeroconfPublisher,'_load',side_effect=DiscoveryError('discovery_dependency_unavailable')):
            result=await lifecycle.listener_ready(ReadyListener(True,(('0.0.0.0',8099),)))
        self.assertFalse(result['advertised']);self.assertEqual(result['reason'],'discovery_dependency_unavailable')
        await lifecycle.close()
