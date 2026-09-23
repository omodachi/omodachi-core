"""Captured workspace layout and persistence tests; no real compositor or HOME."""
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from omodachi_core.catalog import compile_catalog
from omodachi_core.catalog_runtime import CatalogRuntime
from omodachi_core.hub import Hub
from omodachi_core.live_menu_adapter import LiveMenuReaders, CommandResult, WORKSPACE_READ
from omodachi_core.routes import RoutePolicy
from omodachi_core.service import CoreService, ServiceError
from omodachi_core.workspace_actions import WorkspaceActionContext
from omodachi_core.workspace_layout import (WorkspaceLayoutAdapter, WorkspaceLayoutError,
    WorkspaceCommandResult, ENTRY, SOURCE, MARKER, LAYOUT_COMMANDS, ACTIVE_READ,
    WORKSPACES_READ, known_body, run_workspace_command, install_workspace_layout_adapter, atomic_exchange)


class WorkspaceHost:
    def __init__(self):
        self.active=3;self.layouts={3:'dwindle',8:'scrolling'};self.calls=[];self.mutations=[]
        self.apply=True;self.fail_mutation=False;self.after_mutation=None;self.missing_target=False
        self.invalid_active=None
    def run(self,argv,environment,*,mutation=False):
        self.calls.append(argv)
        if mutation:
            self.mutations.append(argv)
            match=next(key for key,value in LAYOUT_COMMANDS.items() if value==argv)
            if self.apply:self.layouts[match[0]]=match[1]
            if self.after_mutation:self.after_mutation()
            return WorkspaceCommandResult(1 if self.fail_mutation else 0)
        if argv==ACTIVE_READ:
            payload=self.invalid_active if self.invalid_active is not None else {'id':self.active,'tiledLayout':self.layouts[self.active],'lastwindowtitle':'DO_NOT_EXPOSE'}
        elif argv==WORKSPACES_READ:
            payload=[{'id':number,'tiledLayout':layout,'lastwindowtitle':'DO_NOT_EXPOSE'} for number,layout in self.layouts.items() if not (self.missing_target and number==3)]
        else:raise AssertionError('unexpected command')
        return WorkspaceCommandResult(0,json.dumps(payload))


class WorkspaceLayoutAdapterTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.home=Path(self.directory.name);self.host=WorkspaceHost()
        self.readers=LiveMenuReaders(home=self.home,graphical=lambda:{'XDG_RUNTIME_DIR':'/run/user/1000','WAYLAND_DISPLAY':'wayland-1'},
            runner=lambda argv,env:CommandResult(0,json.dumps({'id':self.host.active,'tiledLayout':self.host.layouts[self.host.active]})))
        self.adapter=WorkspaceLayoutAdapter(readers=self.readers,runner=self.host.run)
        self.context=WorkspaceActionContext(3,7,'dwindle','scrolling')
        self.argv=(MARKER,'3','dwindle','scrolling')
        self.path=self.home/'.local/state/omarchy/workspace-layouts/3.lua'
    def tearDown(self):self.directory.cleanup()
    def existing(self,body=None):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.path.write_bytes(known_body(3,'dwindle') if body is None else body)
    def invoke(self):return self.adapter.execute(self.argv,self.context)
    def no_temporary(self):self.assertFalse(list(self.home.rglob('.omodachi-layout-*')))
    def service(self):
        hub=Hub();hub.update_state({'workspace':{'active':3},'host':{'connected':True}})
        service=CoreService(hub,runtime=CatalogRuntime(compile_catalog({ENTRY:SOURCE})))
        service.runtime.register_checked_state(ENTRY,lambda:{'status':'available','value':None,'reason':'workspace_layout_'+self.host.layouts[self.host.active]},reviewed_source=SOURCE)
        install_workspace_layout_adapter(service,adapter=self.adapter)
        return service
    def request(self,service,**changes):
        snapshot=service.refresh_catalog(invalidate=True)
        request={'entry_id':ENTRY,'request_id':'layout-request-1','catalog_revision':snapshot['revision'],
            'state_revision':service.hub.state_snapshot()['revision'],
            'params':{'workspace_id':3,'from_layout':'dwindle','layout':'scrolling'}}
        request.update(changes);return request

    def test_single_read_projects_only_id_layout(self):
        self.assertEqual(self.adapter.active_workspace(),{'workspace_id':3,'layout':'dwindle'})
        self.assertEqual(self.host.calls,[ACTIVE_READ]);self.assertEqual(self.host.mutations,[])

    def test_all_twenty_fixed_combinations_have_exact_body_and_no_free_parameters(self):
        self.assertEqual(len(LAYOUT_COMMANDS),20)
        for (number,layout),argv in LAYOUT_COMMANDS.items():
            self.assertEqual(argv,('/usr/bin/hyprctl','eval',known_body(number,layout).decode().rstrip()))
        for argv in [('/bin/sh','-c','true'),('/usr/bin/hyprctl','eval','arbitrary'),(*LAYOUT_COMMANDS[(3,'scrolling')],'extra')]:
            with self.assertRaises(WorkspaceLayoutError):run_workspace_command(argv,{},mutation=True)
        for number in [True,0,11,-1,'3']:
            with self.assertRaises(WorkspaceLayoutError):known_body(number,'dwindle')

    def test_applies_explicit_target_reads_back_and_persists_exact_known_body(self):
        result=self.invoke()
        self.assertEqual(result,{'workspace_id':3,'from_layout':'dwindle','layout':'scrolling',
            'runtime_applied':True,'persistent_applied':True,'readback_confirmed':True,
            'status':'applied','code':'workspace_layout_applied'})
        self.assertEqual(self.path.read_bytes(),known_body(3,'scrolling'))
        self.assertEqual(self.host.mutations,[LAYOUT_COMMANDS[(3,'scrolling')]])
        self.assertNotIn('DO_NOT_EXPOSE',json.dumps(result));self.no_temporary()

    def test_known_existing_layout_is_replaced_and_inverse_request_restores(self):
        self.existing();self.assertEqual(self.invoke()['status'],'applied')
        reverse=WorkspaceActionContext(3,9,'scrolling','dwindle')
        result=self.adapter.execute((MARKER,'3','scrolling','dwindle'),reverse)
        self.assertEqual(result['status'],'applied');self.assertEqual(self.path.read_bytes(),known_body(3,'dwindle'))
        self.assertEqual(len(self.host.mutations),2);self.no_temporary()

    def test_stale_workspace_or_layout_rejects_before_any_file_or_host_mutation(self):
        for active,layout in [(8,'dwindle'),(3,'scrolling')]:
            self.host.active=active;self.host.layouts[3]=layout
            result=self.invoke();self.assertEqual(result['status'],'failed');self.assertEqual(result['code'],'workspace_layout_stale_target')
            self.assertFalse(self.path.parent.exists());self.assertEqual(self.host.mutations,[])

    def test_unknown_or_special_active_shape_remains_unavailable(self):
        for payload in [{'id':-99,'tiledLayout':'dwindle'},{'id':True,'tiledLayout':'dwindle'},
                        {'id':3,'tiledLayout':'master'}, {'id':3},[]]:
            self.host.invalid_active=payload
            with self.assertRaises(WorkspaceLayoutError):self.adapter.active_workspace()
            self.assertFalse(self.adapter.can_execute())
        self.assertEqual(self.host.mutations,[])

    def test_unknown_existing_configuration_is_preserved_before_mutation(self):
        body=b'-- custom user configuration must remain\nhl.custom()\n';self.existing(body)
        result=self.invoke();self.assertEqual(result['status'],'failed');self.assertEqual(result['code'],'workspace_layout_configuration_conflict')
        self.assertEqual(self.path.read_bytes(),body);self.assertEqual(self.host.mutations,[]);self.no_temporary()

    def test_symlink_target_and_parent_are_preserved_and_refused(self):
        foreign=self.home/'foreign';foreign.write_text('preserve');self.path.parent.mkdir(parents=True)
        self.path.symlink_to(foreign);result=self.invoke();self.assertEqual(result['status'],'failed');self.assertTrue(self.path.is_symlink())
        self.assertEqual(foreign.read_text(),'preserve');self.assertEqual(self.host.mutations,[])
        self.path.unlink();self.path.parent.rmdir();self.path.parent.symlink_to(self.home)
        result=self.invoke();self.assertEqual(result['status'],'failed');self.assertEqual(self.host.mutations,[])

    def test_unknown_concurrent_content_after_runtime_is_preserved_as_partial(self):
        self.existing();custom=b'-- concurrent custom content\n'
        self.host.after_mutation=lambda:self.path.write_bytes(custom)
        result=self.invoke();self.assertEqual(result['status'],'partial')
        self.assertTrue(result['runtime_applied']);self.assertTrue(result['readback_confirmed']);self.assertFalse(result['persistent_applied'])
        self.assertEqual(self.path.read_bytes(),custom);self.assertEqual(self.host.layouts[3],'scrolling');self.no_temporary()

    def test_postaccept_focus_change_does_not_retarget_or_read_wrong_workspace(self):
        self.host.after_mutation=lambda:setattr(self.host,'active',8)
        result=self.invoke();self.assertEqual(result['status'],'applied');self.assertEqual(self.host.active,8)
        self.assertEqual(self.host.layouts[3],'scrolling');self.assertEqual(self.host.layouts[8],'scrolling')
        self.assertEqual(self.host.mutations,[LAYOUT_COMMANDS[(3,'scrolling')]])
        self.assertIn(WORKSPACES_READ,self.host.calls);self.no_temporary()

    def test_mutation_return_failure_or_readback_mismatch_is_partial_without_persist_or_retry(self):
        for fail,apply in [(True,True),(False,False)]:
            self.host.fail_mutation=fail;self.host.apply=apply;self.host.layouts[3]='dwindle';self.host.mutations=[]
            result=self.invoke();self.assertEqual(result['status'],'partial')
            self.assertFalse(result['persistent_applied']);self.assertFalse(result['readback_confirmed'])
            self.assertFalse(self.path.exists());self.assertEqual(len(self.host.mutations),1);self.no_temporary()

    def test_persistence_replace_failure_reports_confirmed_runtime_partial(self):
        self.existing();before=self.path.read_bytes()
        with patch('omodachi_core.workspace_layout.atomic_exchange',side_effect=WorkspaceLayoutError('workspace_layout_atomic_exchange_failed')):result=self.invoke()
        self.assertEqual(result['status'],'partial');self.assertTrue(result['runtime_applied']);self.assertFalse(result['persistent_applied'])
        self.assertEqual(self.path.read_bytes(),before);self.no_temporary()

    def test_prepare_failure_cleans_owned_temporary_and_does_not_mutate_runtime(self):
        with patch('omodachi_core.workspace_layout.os.write',side_effect=OSError('synthetic disk full')):result=self.invoke()
        self.assertEqual(result['status'],'failed');self.assertEqual(self.host.mutations,[]);self.no_temporary()

    def test_argument_context_mismatch_or_types_rejects_without_mutation(self):
        for argv in [(MARKER,'8','dwindle','scrolling'),(MARKER,'3','scrolling','dwindle'),self.argv+('extra',)]:
            with self.assertRaises(WorkspaceLayoutError):self.adapter.execute(argv,self.context)
        self.assertEqual(self.host.calls,[]);self.assertFalse(self.path.parent.exists())

    def test_service_accepts_empty_workspace_without_focus_token_and_caches_effects(self):
        service=self.service();request=self.request(service)
        self.assertIsNone(service.hub.state_snapshot().get('focus',{}).get('target_token'))
        result=service.invoke(request,'synthetic-device');self.assertEqual(result['status'],'accepted')
        self.assertEqual(result['workspace_effects']['status'],'applied')
        self.assertEqual(service.invoke(request,'synthetic-device'),result);self.assertEqual(len(self.host.mutations),1)

    def test_service_stale_revision_or_workspace_never_accepts_or_mutates(self):
        service=self.service();request=self.request(service)
        with self.assertRaises(ServiceError):service.invoke(request|{'state_revision':request['state_revision']-1},'synthetic-device')
        self.host.active=8
        with self.assertRaises(ServiceError):service.invoke(request,'synthetic-device')
        self.assertEqual(self.host.mutations,[]);self.assertFalse(self.path.parent.exists())

    def test_service_partial_persistence_result_is_cached_without_repeating_toggle(self):
        self.existing();service=self.service();request=self.request(service)
        self.host.after_mutation=lambda:self.path.write_bytes(b'-- preserve concurrent config\n')
        result=service.invoke(request,'synthetic-device');self.assertEqual(result['status'],'failed')
        self.assertEqual(result['workspace_effects']['status'],'partial')
        self.assertEqual(service.invoke(request,'synthetic-device'),result);self.assertEqual(len(self.host.mutations),1)

    def test_source_drift_or_arbitrary_parameters_cannot_inherit_route(self):
        service=self.service();request=self.request(service)
        with self.assertRaises(ValueError):service.invoke(request|{'params':request['params']|{'lua':'arbitrary'}},'synthetic-device')
        service.runtime.catalog=compile_catalog({ENTRY:SOURCE|{'action':'arbitrary'}})
        row=next(row for row in service.refresh_catalog(invalidate=True)['entries'] if row['id']==ENTRY)
        self.assertFalse(row['route']['ready']);self.assertEqual(self.host.mutations,[])


    def test_atomic_publish_never_replaces_file_created_after_last_absence_check(self):
        original=os.link;custom=b'-- another writer created this file\n'
        def raced(*args,**kwargs):
            self.path.write_bytes(custom)
            return original(*args,**kwargs)
        with patch('omodachi_core.workspace_layout.os.link',side_effect=raced):result=self.invoke()
        self.assertEqual(result['status'],'partial');self.assertEqual(self.path.read_bytes(),custom)
        self.assertFalse(result['persistent_applied']);self.no_temporary()

    def test_atomic_exchange_restores_unknown_content_written_after_last_check(self):
        self.existing();custom=b'-- another writer changed this file\n';first=True
        def raced(fd,source,target):
            nonlocal first
            if first:self.path.write_bytes(custom);first=False
            return atomic_exchange(fd,source,target)
        with patch('omodachi_core.workspace_layout.atomic_exchange',side_effect=raced):result=self.invoke()
        self.assertEqual(result['status'],'partial');self.assertEqual(self.path.read_bytes(),custom)
        self.assertEqual(result['code'],'workspace_layout_persistence_conflict_preserved');self.no_temporary()


    def test_bounded_runner_limits_output_and_deadline_without_returning_private_bytes(self):
        import subprocess,sys
        original=subprocess.Popen
        def oversized(argv,**kwargs):
            return original([sys.executable,'-c','import sys;sys.stdout.write("x"*4096)'],**kwargs)
        with patch('omodachi_core.workspace_layout.subprocess.Popen',side_effect=oversized):
            result=run_workspace_command(ACTIVE_READ,{},max_bytes=1024)
        self.assertEqual(result.error,'workspace_layout_output_limit');self.assertEqual(result.stdout,'')
        def slow(argv,**kwargs):
            return original([sys.executable,'-c','import time;time.sleep(5)'],**kwargs)
        with patch('omodachi_core.workspace_layout.subprocess.Popen',side_effect=slow):
            result=run_workspace_command(ACTIVE_READ,{},timeout=.05)
        self.assertEqual(result.error,'workspace_layout_command_timeout');self.assertEqual(result.stdout,'')

    def test_missing_target_readback_does_not_confirm_or_persist(self):
        self.host.missing_target=True
        result=self.invoke();self.assertEqual(result['status'],'partial')
        self.assertFalse(result['readback_confirmed']);self.assertFalse(result['persistent_applied'])
        self.assertFalse(self.path.exists());self.no_temporary()


if __name__=='__main__':unittest.main()
