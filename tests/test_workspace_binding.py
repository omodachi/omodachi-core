"""Regression for live heartbeat churn invalidating workspace confirmation.

Existing action protocol, fixed identity and synthetic workspace observations;
no live host action, config, credentials/certificate or media tests.
"""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import unittest
from omodachi_core.service import ServiceError
from omodachi_core.workspace_actions import WORKSPACE_LAYOUT_ENTRY as ENTRY
from tests import test_workspace_action_integration as fixtures

SOURCE={'action':'fixture-layout','when':'','checked':'','target':'','provider':'','surface':''}

def enable_binding(fixture):
    def read():
        layout=fixture.projection['layout']
        return {'status':'available','value':None,'reason':'workspace_layout_'+layout}
    fixture.service.runtime.register_checked_state(ENTRY,read,reviewed_source=SOURCE)
    fixture.service.refresh_catalog(invalidate=True)

def capture(fixture,request_id='scoped-click'):
    payload=fixture.payload(request_id)
    binding=fixture.service.hub.state_snapshot()['workspace']['layout_binding']
    assert binding is not None
    payload['workspace_binding']={key:binding[key] for key in ('revision','instance_id')}
    return payload

class WorkspaceBindingTests(unittest.TestCase):
    def setUp(self):self.fixture=fixtures.Fixture();self.service=self.fixture.service;enable_binding(self.fixture)
    def invoke(self,payload):return self.service.invoke(payload,'workspace-client')
    def heartbeat(self):
        # Unrelated state churn from another surface must not expire the binding.
        self.service.hub.update_state({'herdr':{'agent_count':0}})
    def test_heartbeat_and_unrelated_status_churn_does_not_expire_scoped_confirmation(self):
        payload=capture(self.fixture);old_revision=payload['state_revision'];binding=deepcopy(payload['workspace_binding'])
        for _ in range(3):self.heartbeat()
        self.service.hub.update_state({'agent':{'status':'idle'}})
        self.service.set_workspace_snapshot(active=3,window_counts={3:0},focused_window={'id':'popup','app_id':'quickshell','app_name':'Panel'})
        state=self.service.hub.state_snapshot();self.assertGreater(state['revision'],old_revision)
        self.assertEqual(state['workspace']['layout_binding']['revision'],binding['revision'])
        result=self.invoke(payload);self.assertEqual(result['status'],'accepted')
        self.assertEqual(len(self.fixture.calls),1);self.assertEqual(self.fixture.calls[0][1].workspace_id,3)
        self.assertEqual(self.fixture.calls[0][1].state_revision,old_revision)
    def test_observed_workspace_away_and_back_invalidates_same_id_and_layout(self):
        payload=capture(self.fixture)
        self.service.set_workspace_snapshot(active=4,window_counts={4:0},focused_window=None)
        self.service.set_workspace_snapshot(active=3,window_counts={3:0},focused_window=None)
        with self.assertRaisesRegex(ServiceError,'stale_workspace_binding'):self.invoke(payload)
        self.assertEqual(self.fixture.calls,[])
    def test_observed_layout_away_and_back_invalidates_binding_even_same_final_catalog(self):
        payload=capture(self.fixture)
        self.fixture.projection['layout']='scrolling';self.service.refresh_catalog(invalidate=True)
        self.fixture.projection['layout']='dwindle';self.service.refresh_catalog(invalidate=True)
        self.assertEqual(self.service.refresh_catalog()['revision'],payload['catalog_revision'])
        with self.assertRaisesRegex(ServiceError,'stale_workspace_binding'):self.invoke(payload)
        self.assertEqual(self.fixture.calls,[])
    def test_unknown_readiness_and_restore_invalidates_binding(self):
        payload=capture(self.fixture);self.service._publish_workspace_layout_binding(self.service._catalog,unavailable=True)
        self.assertIsNone(self.service.hub.state_snapshot()['workspace']['layout_binding'])
        self.service.refresh_catalog()
        with self.assertRaisesRegex(ServiceError,'stale_workspace_binding'):self.invoke(payload)
        self.assertEqual(self.fixture.calls,[])
    def test_instance_or_revision_mismatch_and_future_global_revision_fail(self):
        payload=capture(self.fixture)
        for patch in ({'workspace_binding':{**payload['workspace_binding'],'instance_id':'old-process'}},
                      {'workspace_binding':{**payload['workspace_binding'],'revision':9999}}, {'state_revision':2**52}):
            with self.assertRaisesRegex(ServiceError,'stale_workspace_binding'):self.invoke({**payload,**patch})
        self.assertEqual(self.fixture.calls,[])
    def test_malformed_proof_or_cross_entry_proof_never_weakens_legacy_checks(self):
        payload=capture(self.fixture)
        for proof in (None,{}, {'revision':True,'instance_id':self.service.instance_id},
                      {**payload['workspace_binding'],'workspace_id':3}):
            with self.assertRaisesRegex(ServiceError,'invalid_workspace_binding'):self.invoke({**payload,'workspace_binding':proof})
        with self.assertRaisesRegex(ServiceError,'invalid_request'):self.invoke({**payload,'entry_id':'other'})
        self.assertEqual(self.fixture.calls,[])
    def test_legacy_request_retains_exact_global_revision_rule(self):
        payload=self.fixture.payload()
        self.fixture.service.hub.update_state({'herdr':{'agent_count':0}})
        with self.assertRaisesRegex(ServiceError,'stale_workspace_revision'):self.invoke(payload)
        self.assertEqual(self.fixture.calls,[])
    def test_fresh_reader_mismatch_is_not_hidden_by_valid_scoped_version(self):
        payload=capture(self.fixture)
        self.fixture.projection['workspace_id']=4
        with self.assertRaisesRegex(ServiceError,'stale_workspace'):self.invoke(payload)
        self.assertIsNone(self.service.hub.state_snapshot()['workspace']['layout_binding'])
        self.assertEqual(self.fixture.calls,[])
    def test_successful_retry_keeps_original_scope_and_does_not_repeat_after_binding_changes(self):
        payload=capture(self.fixture);result=self.invoke(payload);self.heartbeat()
        self.assertEqual(self.invoke(payload),result);self.assertEqual(len(self.fixture.calls),1)

class WorkspaceBindingTransportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.WorkspaceTransportTests.asyncSetUp
    asyncTearDown=fixtures.WorkspaceTransportTests.asyncTearDown
    async def test_actual_cli_scoped_request_survives_unrelated_state_churn(self):
        enable_binding(self.fixture);payload=capture(self.fixture)
        self.fixture.service.hub.update_state({'herdr':{'agent_count':0}})
        environment=dict(os.environ);environment['OMODACHI_TOKEN']='workspace.fixture'
        process=await asyncio.create_subprocess_exec(sys.executable,'-c','from omodachi_core.cli import host_main;raise SystemExit(host_main())',
            '--socket',self.socket,'plugin-action',ENTRY,'--catalog-revision',payload['catalog_revision'],'--request-id',payload['request_id'],
            '--state-revision',str(payload['state_revision']),'--workspace-revision',str(payload['workspace_binding']['revision']),
            '--workspace-instance',payload['workspace_binding']['instance_id'],'--params-json',json.dumps(payload['params']),
            env=environment,stdout=asyncio.subprocess.PIPE)
        out,_=await process.communicate();self.assertEqual(process.returncode,0,out)
        self.assertEqual(json.loads(out)['result']['workspace_effects']['status'],'applied')
    async def test_actual_http_snapshot_binding_and_heartbeat_churn_then_scoped_action(self):
        enable_binding(self.fixture)
        async with self.http.get(self.url+'/v1/state',headers=self.headers) as response:state=await response.json()
        binding=state['workspace']['layout_binding']
        self.assertEqual(binding['workspace_id'],3);self.assertEqual(binding['layout'],'dwindle')
        payload=capture(self.fixture);payload.pop('entry_id')
        self.fixture.service.hub.update_state({'herdr':{'agent_count':0}})
        async with self.http.post(self.url+'/v1/actions/'+ENTRY+':invoke',headers=self.headers,json=payload) as response:
            self.assertEqual(response.status,200);result=await response.json()
        self.assertEqual(result['workspace_effects']['status'],'applied')
