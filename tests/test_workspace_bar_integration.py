from __future__ import annotations
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import aiohttp
from omodachi_core.bootstrap import create_service, DATA
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
from omodachi_core.service import ServiceError


class WorkspaceBarIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.hub=Hub()
        self.a=self.hub.register_device('phone-a')
        self.b=self.hub.register_device('phone-b')
        self.service=create_service(self.hub,demo=True)
        self.server=NetworkServer(self.service,allow_loopback_http=True)
        await self.server.start()
        self.url=f'http://127.0.0.1:{self.server.bound_port}'
        self.client=aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.close()
        self.temp.cleanup()

    def request(self, state, item, *, move=False, request_id='work-1'):
        data={'entry_id':item['move_entry_id' if move else 'select_entry_id'],
              'request_id':request_id,'catalog_revision':state['catalog']['revision'],'params':{}}
        if move:
            data.update(state_revision=state['revision'],target_token=state['focus']['target_token'])
        return data

    async def test_actual_network_search_three_uses_single_catalog_id(self):
        async with self.client.get(self.url+'/v1/catalog?q=3',headers={'Authorization':'Bearer '+self.a}) as response:
            data=await response.json()
            self.assertEqual(response.status,200)
        self.assertEqual(data['entries'][0]['id'],'omodachi.workspace.select.3')
        state=self.service.state('phone-a')
        self.assertEqual(data['entries'][0]['id'],state['workspace']['items'][2]['select_entry_id'])
        self.assertEqual(data['revision'],state['catalog']['revision'])
        roots=[row['id'] for row in state['catalog']['entries'] if row['parent_id']=='root']
        self.assertTrue(set(('apps','learn','trigger','style','setup','install','remove','update','about','system')) <= set(roots))
        ids={row['id'] for row in state['catalog']['entries']}
        self.assertTrue({'install.demo','update.demo','about.demo'}<=ids)

    async def test_nonremote_http_select_uses_existing_real_workspace_ids(self):
        from omodachi_core.graphical import HyprlandWorkspaceAdapter
        active=[23];calls=[]
        def runner(argv,env):
            calls.append(argv)
            if argv[1]=='eval':active[0]=42;return 'ok'
            if argv[-1]=='workspaces':return json.dumps([{'id':23,'windows':2},{'id':42,'windows':0}])
            if argv[-1]=='activeworkspace':return json.dumps({'id':active[0]})
            return '{}'
        adapter=HyprlandWorkspaceAdapter(self.service,runner=runner,environment=lambda:{})
        self.service.set_workspace_adapter(adapter);adapter.refresh()
        headers={'Authorization':'Bearer '+self.a}
        async with self.client.post(self.url+'/v1/workspaces/42/select',headers=headers,json={}) as response:
            self.assertEqual(response.status,200)
            self.assertEqual((await response.json())['workspace']['active'],42)
        count=sum(c[1]=='eval' for c in calls)
        for number,status in [(24,409),(0,400),(-1,400),(2147483648,400)]:
            async with self.client.post(self.url+f'/v1/workspaces/{number}/select',headers=headers,json={}) as response:
                self.assertEqual(response.status,status,await response.text())
        self.assertEqual(sum(c[1]=='eval' for c in calls),count)
        before=self.service.hub.state_snapshot()['revision'];adapter.refresh()
        self.assertEqual(self.service.hub.state_snapshot()['revision'],before)

    async def test_the_relative_route_switches_without_the_client_naming_a_number(self):
        """ARCH-1 / A-64: the three-finger swipe posts a direction, not a guess."""
        from omodachi_core.graphical import HyprlandWorkspaceAdapter
        state={'active':5}
        def runner(argv,env):
            if argv[1]=='eval':
                state['active']=int(argv[2].split('hl.get_workspace(')[1].split(')')[0]);return 'ok'
            if argv[-1]=='workspaces':return json.dumps([{'id':n,'windows':1} for n in (1,2,3,4,5,23)])
            if argv[-1]=='activeworkspace':return json.dumps({'id':state['active']})
            return '{}'
        adapter=HyprlandWorkspaceAdapter(self.service,runner=runner,environment=lambda:{})
        self.service.set_workspace_adapter(adapter);adapter.refresh()
        headers={'Authorization':'Bearer '+self.a}
        async with self.client.post(self.url+'/v1/workspaces/relative/e%2B1/select',
                                    headers=headers,json={}) as response:
            self.assertEqual(response.status,200,await response.text())
            self.assertEqual((await response.json())['workspace']['active'],23)
        async with self.client.post(self.url+'/v1/workspaces/relative/e-1/select',
                                    headers=headers,json={}) as response:
            self.assertEqual((await response.json())['workspace']['active'],5)
        async with self.client.post(self.url+'/v1/workspaces/relative/e+2/select',
                                    headers=headers,json={}) as response:
            self.assertEqual(response.status,404,await response.text())

    async def test_bar_is_safe_projection_and_changes_push_without_client_poll(self):
        shell=Path(self.temp.name)/'shell.json'
        source=json.loads((DATA/'demo-shell.json').read_text())
        shell.write_text(json.dumps(source))
        hub=Hub()
        service=create_service(hub,demo=True,shell_config=shell)
        state=service.state('phone-a')
        self.assertEqual(state['bar']['left'][1],{'id':'omarchy.workspaces','role':'workspaces'})
        self.assertNotIn('maxWidth',json.dumps(state['bar']))
        self.assertNotIn('format',json.dumps(state['bar']))
        cursor=hub.event_cursor
        source['bar']['layout']['left']=[{'id':'omarchy.active-window','exec':'PRIVATE SHELL BODY'}]
        shell.write_text(json.dumps(source))
        service.refresh_sources()
        events=hub.events_since(cursor,device_id='phone-a')
        event=next(e for e in events if e.type=='bar.changed')
        self.assertEqual(event.payload['bar']['left'][0]['role'],'unsupported')
        self.assertNotIn('PRIVATE',json.dumps(event.payload))

    async def test_an_invoke_takes_no_bar_readings(self):
        """PERF-5. The bar modules are three subprocesses and a tap is not about them.

        `refresh_sources()` was called from `invoke`, and it starts with the
        bar snapshot - `wpctl`, `omarchy-network-status`, the battery - which
        is throttled to two seconds, so roughly every other tap ran three
        processes on the event loop before it was allowed to do anything.
        None of them decides whether an action may run. The *source stamp*
        does, and that part is still taken.
        """
        from unittest.mock import patch
        shell=Path(self.temp.name)/'bar-readings-shell.json'
        shell.write_text((DATA/'demo-shell.json').read_text())
        root=Path(self.temp.name)
        hub=Hub()
        reads=[]
        counted=type('Counted',(),{'snapshot':lambda _self:(reads.append(1),{})[1]})
        with patch('omodachi_core.bootstrap.BarModules',counted), \
             patch('omodachi_core.graphical.HyprlandWorkspaceAdapter.refresh',lambda adapter:None):
            service=create_service(hub,default_menu=DATA/'demo-menu.jsonc',user_menu=root/'none',
                omodachi_menu=root/'none',shell_config=shell,enable_live_menu=False,
                enable_catalog_providers=False)
        from omodachi_core.routes import RouteDescriptor
        row=service.runtime.catalog.by_id('trigger.toggle.notifications').as_dict()
        service.policy.register('trigger.toggle.notifications',
            RouteDescriptor('host',True,argv=('fixed-command',)),source_action=row.get('action',''))
        service.register_executor('trigger.toggle.notifications',lambda argv:None)
        reads.clear()
        catalog=service.refresh_catalog()
        service.refresh_sources()
        self.assertEqual(len(reads),1)
        result=service.dispatch('actions.invoke',{'entry_id':'trigger.toggle.notifications',
            'request_id':'no-bar','catalog_revision':catalog['revision']},'phone-a')
        self.assertEqual(result['status'],'accepted')
        self.assertEqual(len(reads),1)

    async def test_long_press_moves_only_explicit_current_token_and_updates_occupancy(self):
        state=self.service.state('phone-a')
        token=state['focus']['target_token']
        self.assertEqual(state['focus']['app_id'],'fixture.editor')
        self.assertIsNone(state['focus']['window'])
        self.assertNotIn('title',state['focus'])
        params=self.request(state,state['workspace']['items'][2],move=True)
        result=self.service.dispatch('actions.invoke',params,'phone-a')
        self.assertEqual(result['status'],'accepted')
        after=self.service.state('phone-a')
        self.assertEqual(after['workspace']['active'],2)
        self.assertEqual(after['workspace']['items'][1]['window_count'],0)
        self.assertEqual(after['workspace']['items'][2]['window_count'],2)
        self.assertIsNone(after['focus']['target_token'])
        with self.assertRaises(ServiceError):
            self.service.resolve_window_target(token)

    async def test_stale_or_missing_token_never_falls_back_to_focused_window(self):
        state=self.service.state('phone-a')
        move=self.request(state,state['workspace']['items'][2],move=True)
        move['target_token']='another-window'
        with self.assertRaisesRegex(ServiceError,'stale_target'):
            self.service.dispatch('actions.invoke',move,'phone-a')
        del move['target_token']
        with self.assertRaisesRegex(ServiceError,'stale_target'):
            self.service.dispatch('actions.invoke',move,'phone-a')
        # A second device changes focus; an old valid token/revision cannot move it.
        select=self.request(state,state['workspace']['items'][0],request_id='select-other')
        self.service.dispatch('actions.invoke',select,'phone-b')
        old=self.request(state,state['workspace']['items'][2],move=True,request_id='old-target')
        with self.assertRaises(ServiceError):
            self.service.dispatch('actions.invoke',old,'phone-a')
        self.assertEqual(self.service.state('phone-a')['workspace']['items'][1]['window_count'],1)

    async def test_workspace_select_without_remote_does_not_start_remote_or_terminal(self):
        state = self.service.state('phone-a')
        original_remote = state['remote']
        request = self.request(state, state['workspace']['items'][2])
        entry_id = request.pop('entry_id')
        async with self.client.post(
            self.url + '/v1/actions/' + entry_id + ':invoke',
            headers={'Authorization': 'Bearer ' + self.a}, json=request,
        ) as response:
            result = await response.json()
            self.assertEqual(response.status, 200)
        after = self.service.state('phone-a')
        self.assertEqual(after['workspace']['active'], 3)
        self.assertEqual(after['remote'], original_remote)
        self.assertIsNone(after['remote']['session_id'])
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['route']['route'], 'host')
        self.assertNotIn('remote.session.changed', [
            event.type for event in self.hub.events_since(state['event_cursor'], device_id='phone-a')
        ])

    async def test_a_remote_session_elsewhere_does_not_block_a_workspace_action(self):
        self.hub.update_state({'remote':{'session_id':'rs_'+'0'*32,'state':'ready','mode':'extend',
                                         'backend':'vnc','revision':2}})
        ws=await self.client.ws_connect(self.url+'/v1/events',headers={'Authorization':'Bearer '+self.b})
        await ws.receive_json()
        state=self.service.state('phone-b')
        original_remote = state['remote']
        result = self.service.dispatch('actions.invoke',self.request(state,state['workspace']['items'][2]),'phone-b')
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['route']['route'], 'host')
        # A workspace selection does not create, change or end a Remote session
        # or turn into a terminal/desktop launch descriptor. Real media is
        # outside this local adapter test; only the unchanged state is asserted.
        self.assertEqual(self.service.state('phone-b')['remote'], original_remote)
        message=await asyncio.wait_for(ws.receive_json(),1)
        self.assertEqual(message['event']['type'],'workspace.changed')
        self.assertEqual(message['event']['payload']['workspace']['active'],3)
        self.assertEqual(self.service.state('phone-b')['focus']['app_id'],'fixture.browser')
        await ws.close()

    async def test_unknown_occupancy_and_deleted_menu_reference_are_not_fabricated(self):
        # No compositor reading at all: the persistent rows still exist, because
        # the official bar always draws them, but nothing claims to know whether
        # they hold a window.
        self.service.set_workspace_snapshot(active=None,window_counts={},snapshot_available=False)
        state=self.service.state('phone-a')
        self.assertEqual([item['id'] for item in state['workspace']['items']],[1,2,3,4,5])
        self.assertTrue(all(item['persistent'] for item in state['workspace']['items']))
        self.assertTrue(all(item['occupied'] is None for item in state['workspace']['items']))
        self.assertIsNone(state['focus']['target_token'])
        from omodachi_core.catalog import compile_catalog
        self.service.runtime.catalog=compile_catalog({})
        self.service.refresh_catalog()
        state=self.service.state('phone-a')
        self.assertTrue(all(item['select_entry_id'] is None and item['move_entry_id'] is None for item in state['workspace']['items']))

class FullOmarchySourceFixtureTests(unittest.TestCase):
    def test_source_pinned_menu_is_not_the_demo_catalog(self):
        from hashlib import sha256
        from omodachi_core.catalog import compile_catalog_from_jsonc
        source=Path(__file__).parents[1]/'contracts/fixtures/catalog/omarchy-default-v4.0.3.jsonc'
        meta=json.loads((source.with_suffix('.meta.json')).read_text())
        self.assertEqual(sha256(source.read_bytes()).hexdigest(),meta['sha256'])
        catalog=compile_catalog_from_jsonc(source)
        self.assertGreater(len(catalog.entries),46)
        self.assertEqual(meta['source_revision'],'omarchy-v4.0.3')
