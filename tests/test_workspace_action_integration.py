"""Captured workspace action preflight on existing service/IPC/HTTP surfaces.

Fixed test identity, synthetic readers/executor only; no host commands, user
config, credentials/certificates or new protocol family.
"""
import asyncio
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import aiohttp

from omodachi_core.catalog import compile_catalog
from omodachi_core.catalog_runtime import CatalogRuntime
from omodachi_core.hub import Hub
from omodachi_core.ipc import JsonLineServer
from omodachi_core.network import NetworkServer
from omodachi_core.routes import RouteDescriptor,RoutePolicy
from omodachi_core.service import CoreService,ServiceError
from omodachi_core.plugin_actions import invoke_plugin_action
from omodachi_core.workspace_actions import WORKSPACE_LAYOUT_ENTRY as ENTRY, WorkspaceActionContext

class FixedIdentity:
    def verify(self,value):
        if value!='workspace.fixture':raise ValueError('fixture unavailable')
        return 'workspace-client'

class Fixture:
    def __init__(self):
        hub=Hub(authenticator=FixedIdentity());policy=RoutePolicy()
        catalog=compile_catalog({ENTRY:{'label':'Workspace Layout','action':'fixture-layout'}})
        self.service=CoreService(hub,runtime=CatalogRuntime(catalog),policy=policy)
        policy.register(ENTRY,RouteDescriptor('host',True,argv=('omodachi.workspace-layout','{workspace_id}','{from_layout}','{layout}')),
            source_action='fixture-layout',parameter_enums={'workspace_id':tuple(range(1,11)),'from_layout':('dwindle','scrolling'),'layout':('dwindle','scrolling')})
        self.projection={'workspace_id':3,'layout':'dwindle'};self.preflight_calls=0;self.calls=[];self.effects=None;self.execute_hook=None
        self.service.register_executor(ENTRY,self.execute,requires_workspace=True,workspace_preflight=self.read)
        self.service.set_workspace_snapshot(active=3,window_counts={3:0},focused_window=None)
    def read(self):self.preflight_calls+=1;return dict(self.projection)
    def execute(self,argv,context):
        self.calls.append((argv,context))
        if self.execute_hook:return self.execute_hook(argv,context)
        self.projection={'workspace_id':context.workspace_id,'layout':context.layout}
        return self.effects or {'workspace_id':context.workspace_id,'from_layout':context.from_layout,'layout':context.layout,
            'runtime_applied':True,'persistent_applied':True,'readback_confirmed':True,'status':'applied','code':'workspace_layout_applied'}
    def payload(self,request='request-1',**changes):
        catalog=self.service.refresh_catalog();state=self.service.hub.state_snapshot('workspace-client')
        return {'entry_id':ENTRY,'request_id':request,'catalog_revision':catalog['revision'],'state_revision':state['revision'],
                'params':{'workspace_id':3,'from_layout':'dwindle','layout':'scrolling'},**changes}

class WorkspacePreflightTests(unittest.TestCase):
    def setUp(self):self.fixture=Fixture();self.service=self.fixture.service
    def invoke(self,payload):return self.service.invoke(payload,'workspace-client')
    def test_empty_workspace_uses_immutable_workspace_context_without_focus(self):
        payload=self.fixture.payload();result=self.invoke(payload)
        self.assertEqual(result['status'],'accepted');self.assertEqual(result['workspace_effects']['status'],'applied')
        argv,context=self.fixture.calls[0]
        self.assertIsInstance(context,WorkspaceActionContext);self.assertEqual(context.workspace_id,3)
        self.assertEqual(context.state_revision,payload['state_revision']);self.assertEqual(argv,('omodachi.workspace-layout','3','dwindle','scrolling'))
        self.assertIsNone(self.service.hub.state_snapshot()['focus']['target_token'])
        with self.assertRaises(FrozenInstanceError):context.workspace_id=4
    def test_state_revision_stale_rejected_before_accept_or_executor(self):
        payload=self.fixture.payload();self.service.hub.update_state({'host':{'catalog_stale':False}})
        with self.assertRaisesRegex(ServiceError,'stale_workspace_revision'):self.invoke(payload)
        self.assertEqual(self.fixture.calls,[]);self.assertEqual(len(self.service._requests),0)
    def test_changed_workspace_or_layout_projection_rejected_without_acceptance(self):
        payload=self.fixture.payload()
        for changed,code in [({'workspace_id':4,'layout':'dwindle'},'stale_workspace'),({'workspace_id':3,'layout':'scrolling'},'workspace_layout_changed')]:
            self.fixture.projection=changed
            with self.assertRaisesRegex(ServiceError,code):self.invoke(payload)
        self.assertEqual(self.fixture.calls,[]);self.assertEqual(len(self.service._requests),0)
    def test_live_workspace_refresh_precedes_revision_check(self):
        payload=self.fixture.payload();service=self.service
        class Refresh:
            def refresh(self):service.set_workspace_snapshot(active=4,window_counts={4:0},focused_window=None)
        self.service.set_workspace_adapter(Refresh())
        with self.assertRaisesRegex(ServiceError,'stale_workspace_revision'):self.invoke(payload)
        self.assertEqual(self.fixture.calls,[])
    def test_types_extra_fields_same_layout_focus_token_and_missing_revision_rejected(self):
        original=self.fixture.payload()
        cases=[{**original,'state_revision':True},{k:v for k,v in original.items() if k!='state_revision'},
            {**original,'target_token':None},{**original,'target_token':'window-token'}]
        for params in ({'workspace_id':True,'from_layout':'dwindle','layout':'scrolling'},
                       {'workspace_id':11,'from_layout':'dwindle','layout':'scrolling'},
                       {'workspace_id':3,'from_layout':'dwindle','layout':'dwindle'},
                       {'workspace_id':3,'from_layout':'dwindle','layout':'scrolling','lua':'injected'}):
            cases.append({**original,'params':params})
        for payload in cases:
            with self.subTest(payload=payload),self.assertRaises((ServiceError,ValueError)):self.invoke(payload)
        self.assertEqual(self.fixture.calls,[]);self.assertEqual(len(self.service._requests),0)
    def test_partial_effects_are_cached_and_retry_does_not_repeat_side_effect(self):
        effects={'workspace_id':3,'from_layout':'dwindle','layout':'scrolling','runtime_applied':True,
            'persistent_applied':False,'readback_confirmed':True,'status':'partial','code':'workspace_persistence_failed'}
        self.fixture.effects=effects;payload=self.fixture.payload();result=self.invoke(payload)
        self.assertEqual(result['status'],'failed');self.assertEqual(result['code'],'workspace_persistence_failed')
        self.assertEqual(result['workspace_effects'],effects)
        self.assertEqual(self.invoke(payload),result);self.assertEqual(len(self.fixture.calls),1);self.assertEqual(self.fixture.preflight_calls,1)
    def test_executor_unknown_failure_never_invents_no_mutation_receipt_or_retries(self):
        def fail(*_):raise OSError('synthetic outcome unknown')
        self.fixture.execute_hook=fail;payload=self.fixture.payload();result=self.invoke(payload)
        self.assertEqual(result['code'],'workspace_layout_outcome_unknown');self.assertNotIn('workspace_effects',result)
        self.assertEqual(self.invoke(payload),result);self.assertEqual(len(self.fixture.calls),1)
    def test_post_mutation_catalog_failure_keeps_final_effect_receipt_and_no_retry(self):
        payload=self.fixture.payload();refresh=self.service.refresh_catalog;count=[0]
        def fail_after_execution(**kwargs):
            count[0]+=1
            if count[0]>1:raise OSError('synthetic catalog unavailable')
            return refresh(**kwargs)
        self.service.refresh_catalog=fail_after_execution
        result=self.invoke(payload)
        self.assertEqual(result['status'],'accepted');self.assertTrue(result['workspace_effects']['persistent_applied'])
        self.assertTrue(self.service.hub.state_snapshot()['host']['catalog_stale'])
        self.assertEqual(self.invoke(payload),result);self.assertEqual(len(self.fixture.calls),1)

    def test_contradictory_executor_receipt_is_not_accepted(self):
        self.fixture.effects={'workspace_id':3,'from_layout':'dwindle','layout':'scrolling','runtime_applied':True,
            'persistent_applied':False,'readback_confirmed':True,'status':'applied','code':'workspace_layout_applied'}
        result=self.invoke(self.fixture.payload());self.assertEqual(result['status'],'failed')
        self.assertEqual(result['code'],'workspace_layout_outcome_unknown');self.assertNotIn('workspace_effects',result)
    def test_workspace_registration_is_narrow_and_cannot_replace_window_authority(self):
        for kwargs in ({},{'requires_workspace':True},{'requires_workspace':True,'requires_target':True,'workspace_preflight':self.fixture.read}):
            with self.assertRaises(ValueError):self.service.register_executor(ENTRY,self.fixture.execute,**kwargs)
        with self.assertRaises(ValueError):self.service.register_executor('other',self.fixture.execute,requires_workspace=True,workspace_preflight=self.fixture.read)
    def test_workspace_projection_unknown_fails_closed(self):
        for value in ({'workspace_id':-99,'layout':'dwindle'},{'workspace_id':3,'layout':[]},{'workspace_id':3,'layout':'unknown'}):
            self.fixture.projection=value
            with self.assertRaisesRegex(ServiceError,'workspace_preflight_unavailable'):self.invoke(self.fixture.payload())
        self.assertEqual(self.fixture.calls,[])

class WorkspaceTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.fixture=Fixture();self.service=self.fixture.service
        self.socket=str(Path(self.tmp.name)/'core.sock');self.ipc=JsonLineServer(self.service.hub,self.socket);await self.ipc.start()
        self.server=NetworkServer(self.service,allow_loopback_http=True);await self.server.start();self.http=aiohttp.ClientSession()
        self.url=f'http://127.0.0.1:{self.server.bound_port}';self.headers={'Authorization':'Bearer workspace.fixture'}
    async def asyncTearDown(self):
        await self.http.close();await self.server.close();await self.ipc.close()
        await (self.service.close_media() if hasattr(self.service,'close_media') else self.service.close_preferences())
        self.tmp.cleanup()
    async def test_http_revision_only_empty_workspace_and_partial_result(self):
        payload=self.fixture.payload();payload.pop('entry_id')
        self.fixture.effects={'workspace_id':3,'from_layout':'dwindle','layout':'scrolling','runtime_applied':True,
            'persistent_applied':False,'readback_confirmed':True,'status':'partial','code':'workspace_persistence_failed'}
        async with self.http.post(self.url+'/v1/actions/'+ENTRY+':invoke',headers=self.headers,json=payload) as response:
            self.assertEqual(response.status,200);result=await response.json()
        self.assertEqual(result['status'],'failed');self.assertFalse(result['workspace_effects']['persistent_applied'])
        self.assertEqual(len(self.fixture.calls),1)
    async def test_real_cli_allows_revision_only_for_fixed_workspace_entry(self):
        payload=self.fixture.payload();environment=dict(os.environ);environment['OMODACHI_TOKEN']='workspace.fixture'
        process=await asyncio.create_subprocess_exec(sys.executable,'-c','from omodachi_core.cli import host_main;raise SystemExit(host_main())',
            '--socket',self.socket,'plugin-action',ENTRY,'--catalog-revision',payload['catalog_revision'],'--request-id','cli-workspace',
            '--state-revision',str(payload['state_revision']),'--params-json',json.dumps(payload['params']),env=environment,stdout=asyncio.subprocess.PIPE)
        out,_=await process.communicate();self.assertEqual(process.returncode,0,out)
        result=json.loads(out);self.assertEqual(result['result']['workspace_effects']['workspace_id'],3)
        self.assertNotIn('workspace.fixture',out.decode());self.assertNotIn('argv',result['result']['route'])
        called=[]
        rejected=await invoke_plugin_action(self.socket,entry_id='another.action',catalog_revision='rev',request_id='id',state_revision=3,
            credential_loader=lambda:called.append(True))
        self.assertFalse(rejected['ok']);self.assertEqual(called,[])
    async def test_plugin_helper_preserves_partial_workspace_effects_and_retry(self):
        payload=self.fixture.payload();self.fixture.effects={'workspace_id':3,'from_layout':'dwindle','layout':'scrolling',
            'runtime_applied':True,'persistent_applied':False,'readback_confirmed':True,'status':'partial','code':'workspace_persistence_failed'}
        options=dict(entry_id=ENTRY,catalog_revision=payload['catalog_revision'],request_id=payload['request_id'],params=payload['params'],
            state_revision=payload['state_revision'],credential_loader=lambda:'workspace.fixture')
        result=await invoke_plugin_action(self.socket,**options)
        self.assertTrue(result['ok']);self.assertEqual(result['result']['code'],'workspace_persistence_failed')
        self.assertEqual(result['result']['workspace_effects'],self.fixture.effects)
        self.assertEqual(await invoke_plugin_action(self.socket,**options),result);self.assertEqual(len(self.fixture.calls),1)
    async def test_request_and_partial_receipt_schemas(self):
        import jsonschema
        root=Path(__file__).resolve().parents[1]/'contracts'
        schema=json.loads((root/'workspace-layout-action.schema.json').read_text());payload=self.fixture.payload()
        jsonschema.Draft202012Validator({**schema,'$ref':'#/$defs/request'}).validate(payload)
        for patch in ({'state_revision':True},{'target_token':'a'},{'params':{**payload['params'],'layout':'dwindle'}}):
            with self.assertRaises(jsonschema.ValidationError):jsonschema.Draft202012Validator({**schema,'$ref':'#/$defs/request'}).validate({**payload,**patch})
        effects=self.fixture.execute((),WorkspaceActionContext(3,payload['state_revision'],'dwindle','scrolling'))
        jsonschema.Draft202012Validator({**schema,'$ref':'#/$defs/effects'}).validate(effects)
