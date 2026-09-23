"""Closed live-state adapter tests; all mutation checks use a fake runner."""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from omodachi_core.catalog import compile_catalog
from omodachi_core.catalog_runtime import CatalogRuntime
from omodachi_core.hub import Hub
from omodachi_core.live_menu_adapter import (CommandResult,LiveMenuReaders,MenuReadUnavailable,GETTERS,
    QUICK_SOURCES,REVIEWED_CHECKS,REVIEWED_GUARDS,PACKAGE_READ,PACKAGES,NOTIFICATION_READ,
    NOTIFICATION_TOGGLE,NOTIFICATION_REFRESH,NIGHTLIGHT_READ,WORKSPACE_READ,DEVICE_READ,
    bounded_command,install_live_menu_adapters)
from omodachi_core.service import CoreService,ServiceError


class Clock:
    value=100.0
    def __call__(self):return self.value


class FakeHost:
    def __init__(self):
        self.calls=[];self.mutations=[];self.dnd='off';self.temperature=None;self.layout='dwindle'
        self.failed=set();self.missing=set();self.mutate=True
        self.defaults={'agent':'codex','browser':'firefox','terminal':'foot','editor':'nvim','dns':'DHCP','channel':'stable'}
    def read(self,argv,env):
        self.calls.append(argv)
        if argv in self.failed:return CommandResult(1,error='reader_failed')
        if argv==NOTIFICATION_READ:return CommandResult(0,self.dnd+'\n')
        if argv==NIGHTLIGHT_READ:return CommandResult(0,json.dumps({'enabled':False,'temperature':self.temperature}))
        if argv==WORKSPACE_READ:return CommandResult(0,json.dumps({'tiledLayout':self.layout,'lastwindowtitle':'PRIVATE TITLE'}))
        if argv==DEVICE_READ:return CommandResult(0,json.dumps({'mice':[{'name':'Sensitive trackpad name'}],'touch':[],'tablets':[]}))
        if argv==PACKAGE_READ:return CommandResult(127 if self.missing else 0,'\n'.join(sorted(self.missing)))
        for family,path in GETTERS.items():
            if argv==(path,):return CommandResult(0,self.defaults[family]+'\n')
        if argv[0].startswith('/usr/share/omarchy/bin/omarchy-hw-'):return CommandResult(1)
        raise AssertionError('unexpected read '+repr(argv))
    def mutation(self,argv,env):
        self.mutations.append(argv)
        if argv==NOTIFICATION_TOGGLE and self.mutate:self.dnd='on' if self.dnd=='off' else 'off'
        return CommandResult(0,self.dnd)


def reviewed_catalog():
    rows={key:{field:value for field,value in source.items() if value or field!='surface'} for key,source in QUICK_SOURCES.items()}
    for key,value in REVIEWED_CHECKS.items():
        rows[key]={'action':value['action'],'when':value['when'],'checked':value['expression']}
    for key,expression in REVIEWED_GUARDS.items():
        rows.setdefault(key,{})['when']=expression
    return compile_catalog(rows)


class LiveMenuAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.home=Path(self.temp.name);self.clock=Clock();self.host=FakeHost()
        self.reader=LiveMenuReaders(home=self.home,runner=self.host.read,mutation_runner=self.host.mutation,
            graphical=lambda:{'XDG_RUNTIME_DIR':'/run/user/1000','WAYLAND_DISPLAY':'wayland-1'},clock=self.clock)
        config=self.home/'.config/omarchy';config.mkdir(parents=True)
        (config/'shell.json').write_text(json.dumps({'bar':{'layout':{'right':['omarchy.power']}}}))
    def tearDown(self):self.temp.cleanup()
    def state(self,name):return self.reader.quick_state('trigger.toggle.'+name)
    def service(self):
        service=CoreService(Hub(),runtime=CatalogRuntime(reviewed_catalog(),clock=self.clock))
        install_live_menu_adapters(service,readers=self.reader)
        return service

    def test_all_ten_states_have_correct_polarity_and_enum_is_not_fake_bool(self):
        flags=self.home/'.local/state/omarchy'
        for suffix in ['indicators/stay-awake','toggles/screensaver-off','toggles/hypr/single-window-aspect-ratio.lua']:
            path=flags/suffix;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('')
        expected={'idle-lock':True,'notifications':True,'crash-capture':True,'screensaver':False,'top-bar':True,
                  'battery-percentage':False,'window-gaps':True,'one-window-ratio':True}
        for name,value in expected.items():
            with self.subTest(name=name):self.assertIs(self.state(name)['value'],value)
        self.assertEqual(self.state('nightlight'),{'status':'unavailable','value':None,'reason':'nightlight_temperature_unknown'})
        self.assertEqual(self.state('workspace-layout'),{'status':'available','value':None,'reason':'workspace_layout_dwindle'})
        self.assertEqual(len(QUICK_SOURCES),10)
        self.assertEqual(self.host.mutations,[])

    def test_notifications_is_inverse_of_dnd_and_failures_stay_unknown(self):
        # No state file (DND never toggled on this install): the shell is asked.
        self.assertTrue(self.state('notifications')['value'])
        self.host.dnd='on';self.reader.invalidate()
        # CORE-2 §3: ...but at most every 30 s, however often the menu refreshes.
        self.assertTrue(self.state('notifications')['value'])
        self.assertEqual(self.host.calls.count(NOTIFICATION_READ),1)
        self.clock.value+=31;self.reader.invalidate()
        self.assertFalse(self.state('notifications')['value'])
        self.host.dnd='unrecognized';self.clock.value+=31;self.reader.invalidate()
        self.assertEqual(self.state('notifications')['status'],'unavailable')
        self.assertIsNone(self.state('notifications')['value'])
        self.host.failed.add(NOTIFICATION_READ);self.clock.value+=31;self.reader.invalidate()
        self.assertIsNone(self.state('notifications')['value'])

    def test_the_shells_state_file_answers_without_a_process(self):
        """CORE-2 §3 (G17): the shell writes DND to notifications.json; reading it spawns nothing."""
        state=self.home/'.local/state/omarchy';state.mkdir(parents=True,exist_ok=True)
        (state/'notifications.json').write_text('{\n  "version": 3,\n  "dnd": true\n}\n')
        self.host.failed.add(NOTIFICATION_READ)
        for _ in range(20):
            self.reader.invalidate();self.clock.value+=16
            self.assertIs(self.state('notifications')['value'],False)
        (state/'notifications.json').write_text('{"version": 3, "dnd": false}')
        self.reader.invalidate()
        self.assertIs(self.state('notifications')['value'],True)
        self.assertNotIn(NOTIFICATION_READ,self.host.calls)
        # A file that cannot say is the same as no file: the shell is asked.
        self.host.failed.clear();self.host.dnd='on'
        (state/'notifications.json').write_text('{"version": 3}')
        self.reader.invalidate()
        self.assertIs(self.state('notifications')['value'],False)
        self.assertEqual(self.host.calls.count(NOTIFICATION_READ),1)

    def test_qs_is_run_with_a_utf8_locale(self):
        seen={}
        def runner(argv,env):
            seen[argv]=(env['LANG'],env['LC_ALL'])
            return self.host.read(argv,env)
        self.reader.runner=runner
        self.state('notifications');self.reader.getter('browser')
        self.assertEqual(seen[NOTIFICATION_READ],('C.UTF-8','C.UTF-8'))
        self.assertEqual(seen[(GETTERS['browser'],)],('C','C'))

    def test_a_toggle_reads_back_the_shells_own_answer_not_the_lagging_file(self):
        state=self.home/'.local/state/omarchy';state.mkdir(parents=True,exist_ok=True)
        (state/'notifications.json').write_text('{"version": 3, "dnd": false}')
        self.assertEqual(self.reader.toggle_notifications(NOTIFICATION_TOGGLE),{'checked_state':False})
        self.assertNotIn(NOTIFICATION_READ,self.host.calls)
        self.assertIs(self.state('notifications')['value'],False)  # the file has not caught up; the cache has

    def test_nightlight_uses_valid_temperature_not_helper_false_when_null(self):
        for temperature,value in [(4000,True),(5999,True),(6000,False),(6500,False)]:
            self.host.temperature=temperature;self.reader.invalidate()
            self.assertIs(self.state('nightlight')['value'],value)
        for temperature in [None,True,'4000',float('nan'),0,50000]:
            self.host.temperature=temperature;self.reader.invalidate()
            self.assertEqual(self.state('nightlight')['status'],'unavailable')
            self.assertIsNone(self.state('nightlight')['value'])

    def test_power_percentage_reads_only_canonical_inline_setting_and_false_default(self):
        path=self.home/'.config/omarchy/shell.json'
        for row,value in [('omarchy.power',False),({'id':'omarchy.power'},False),
                          ({'id':'omarchy.power','showPercentage':True,'secret':'PRIVATE'},True),
                          ({'id':'omarchy.power','showPercentage':'true'},False)]:
            path.write_text(json.dumps({'bar':{'layout':{'right':[row]}}}));self.reader.invalidate()
            result=self.state('battery-percentage');self.assertIs(result['value'],value)
            self.assertNotIn('PRIVATE',json.dumps(result))
        path.write_text('{broken');self.reader.invalidate()
        self.assertIsNone(self.state('battery-percentage')['value'])

    def test_six_getters_share_cache_for_all_checked_rows_and_unset_agent_is_valid(self):
        for key,row in REVIEWED_CHECKS.items():
            value=self.reader.default_checked(key,row['expression'])
            self.assertIs(type(value),bool)
        for path in GETTERS.values():self.assertEqual(self.host.calls.count((path,)),1)
        self.assertEqual(len(GETTERS),6)
        self.host.defaults['agent']='';self.reader.invalidate()
        agent_rows=[(key,row) for key,row in REVIEWED_CHECKS.items() if key.startswith('setup.default.agent.')]
        self.assertTrue(all(not self.reader.default_checked(key,row['expression']) for key,row in agent_rows))
        self.host.defaults['channel']='unknown';self.reader.invalidate()
        with self.assertRaises(MenuReadUnavailable):self.reader.getter('channel')

    def test_package_guards_batch_deptest_for_provides_and_preserve_inversion(self):
        # pacman deptest's missing set excludes packages satisfied by Provides.
        package=next(iter(PACKAGES));self.host.missing={package}
        for key,expression in REVIEWED_GUARDS.items():
            if 'omarchy-pkg-present ' not in expression:continue
            actual=self.reader.guard(key,expression)
            present=expression.split()[-1] not in self.host.missing
            self.assertEqual(actual,not present if expression.startswith('! ') else present)
        self.assertEqual(self.host.calls.count(PACKAGE_READ),1)
        self.host.failed.add(PACKAGE_READ);self.reader.invalidate()
        key=next(k for k,v in REVIEWED_GUARDS.items() if 'omarchy-pkg-present ' in v)
        with self.assertRaises(MenuReadUnavailable):self.reader.guard(key,REVIEWED_GUARDS[key])

    def test_command_guard_only_checks_metadata_and_unknown_expressions_cannot_execute(self):
        key=next(k for k,v in REVIEWED_GUARDS.items() if v=='omarchy-cmd-present foot')
        with patch('omodachi_core.live_menu_adapter.shutil.which',return_value='/usr/bin/foot') as which:
            self.assertTrue(self.reader.guard(key,REVIEWED_GUARDS[key]));which.assert_called_once()
        self.assertEqual(self.host.calls,[])
        for expression in ['omarchy-cmd-present foot; touch /tmp/x','! bash -c true','omarchy-pkg-present ../../bad']:
            with self.assertRaises(MenuReadUnavailable):self.reader.guard(key,expression)
        with self.assertRaises(MenuReadUnavailable):self.reader.getter('arbitrary')
        with self.assertRaises(MenuReadUnavailable):bounded_command(('/bin/sh','-c','true'),{})

    def test_hardware_devices_project_only_boolean_and_failure_is_unknown(self):
        self.assertTrue(self.reader.hardware('omarchy-hw-touchpad'))
        self.assertFalse(self.reader.hardware('omarchy-hw-touchscreen'))
        self.host.failed.add(DEVICE_READ);self.reader.invalidate()
        with self.assertRaises(MenuReadUnavailable):self.reader.hardware('omarchy-hw-touchpad')
        with self.assertRaises(MenuReadUnavailable):self.reader.hardware('omarchy-hw-unreviewed')

    def test_no_graphical_session_returns_unknown_not_false_or_mutation(self):
        def absent():raise ValueError('missing')
        self.reader.graphical=absent
        self.assertIsNone(self.state('notifications')['value'])
        self.assertIsNone(self.state('workspace-layout')['value'])
        self.assertIsNone(self.state('nightlight')['value'])
        self.assertEqual(self.host.calls,[])
        self.assertEqual(self.host.mutations,[])

    def test_runtime_hook_preserves_source_and_publishes_same_catalog_revision_events(self):
        # CORE-2 §3: a desktop DND change reaches the menu through the file the
        # shell saves it to, which is how the real host sees it.
        state=self.home/'.local/state/omarchy';state.mkdir(parents=True,exist_ok=True)
        (state/'notifications.json').write_text('{"version": 3, "dnd": false}')
        service=self.service();first=service.refresh_catalog(invalidate=True)
        row=next(row for row in first['entries'] if row['id']=='trigger.toggle.notifications')
        self.assertEqual(row['checked'],'');self.assertTrue(row['checked_state'])
        self.assertEqual(row['conditions']['checked'],{'status':'available','value':True})
        cursor=service.hub.event_cursor
        (state/'notifications.json').write_text('{"version": 3, "dnd": true}');self.clock.value+=3
        second=service.refresh_catalog()
        row=next(row for row in second['entries'] if row['id']=='trigger.toggle.notifications')
        self.assertFalse(row['checked_state']);self.assertNotEqual(first['revision'],second['revision'])
        self.assertTrue(any(event.type=='catalog.changed' for event in service.hub.events_since(cursor)))
        self.assertNotIn('toggles',service.state('ipad'))
        self.assertNotIn('PRIVATE',json.dumps(second))
        self.assertIs(install_live_menu_adapters(service),self.reader)

    def test_source_action_or_condition_drift_cannot_inherit_state_or_executor(self):
        service=self.service()
        catalog=reviewed_catalog()
        rows={entry.id:entry.as_dict() for entry in catalog.entries}
        rows['trigger.toggle.notifications']['action']='arbitrary-command'
        rows['trigger.toggle.idle-lock']['when']='client-controlled-condition'
        service.runtime.catalog=compile_catalog(rows)
        state=service.refresh_catalog(invalidate=True)
        for name in ['notifications','idle-lock']:
            row=next(row for row in state['entries'] if row['id']=='trigger.toggle.'+name)
            self.assertIsNone(row['checked_state'])
            self.assertEqual(row['conditions']['checked']['reason'],'state_adapter_source_changed')
        row=next(row for row in state['entries'] if row['id']=='trigger.toggle.notifications')
        self.assertFalse(row['route']['supported'])
        self.assertEqual(self.host.mutations,[])

    def test_notification_executor_fake_readback_and_existing_service_idempotency(self):
        service=self.service();snapshot=service.refresh_catalog(invalidate=True)
        payload={'entry_id':'trigger.toggle.notifications','request_id':'fake-toggle-one',
                 'catalog_revision':snapshot['revision'],'params':{}}
        first=service.invoke(payload,'ipad')
        self.assertEqual(first['status'],'accepted');self.assertEqual(self.host.dnd,'on')
        self.assertEqual(self.host.mutations,[NOTIFICATION_TOGGLE,NOTIFICATION_REFRESH])
        self.assertEqual(service.invoke(payload,'ipad'),first)
        self.assertEqual(self.host.mutations,[NOTIFICATION_TOGGLE,NOTIFICATION_REFRESH])
        after=service.refresh_catalog()
        row=next(row for row in after['entries'] if row['id']=='trigger.toggle.notifications')
        self.assertFalse(row['checked_state'])
        with self.assertRaises(ValueError):service.invoke(payload|{'request_id':'fake-two','params':{'command':'sh'}},'ipad')
        self.assertEqual(len(self.host.mutations),2)

    def test_notification_unknown_before_toggle_and_failed_readback_do_not_auto_retry(self):
        self.host.failed.add(NOTIFICATION_READ)
        with self.assertRaises(MenuReadUnavailable):self.reader.toggle_notifications(NOTIFICATION_TOGGLE)
        self.assertEqual(self.host.mutations,[])
        # CORE-2 §3: a failed ask is not repeated inside 30 s.
        self.host.failed.clear();self.reader.invalidate();self.host.mutate=False;self.clock.value+=31
        service=self.service();snapshot=service.refresh_catalog(invalidate=True)
        payload={'entry_id':'trigger.toggle.notifications','request_id':'fake-failed',
                 'catalog_revision':snapshot['revision'],'params':{}}
        result=service.invoke(payload,'ipad')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(service.invoke(payload,'ipad'),result)
        self.assertEqual(self.host.mutations,[NOTIFICATION_TOGGLE])

    def test_checked_hook_rejects_invalid_or_unbounded_payload_and_keeps_null(self):
        runtime=CatalogRuntime(compile_catalog({'trigger.toggle.notifications':QUICK_SOURCES['trigger.toggle.notifications']}))
        for payload in [{'status':'available','value':1},{'status':'unavailable','value':False,'reason':'bad'},
                        {'status':'available','value':True,'secret':'PRIVATE'},
                        {'status':'unavailable','value':None,'reason':'PRIVATE SECRET!'}]:
            runtime.register_checked_state('trigger.toggle.notifications',lambda payload=payload:payload,
                                           reviewed_source=QUICK_SOURCES['trigger.toggle.notifications'])
            row=next(row for row in runtime.refresh(invalidate=True)['entries'] if row['id']=='trigger.toggle.notifications')
            self.assertIsNone(row['checked_state'])
            self.assertEqual(row['conditions']['checked']['reason'],'state_adapter_failed')

    def test_all_live_state_projection_validates_existing_catalog_schema(self):
        from jsonschema import Draft202012Validator
        from referencing import Registry,Resource
        base=Path(__file__).parents[1]/'contracts'
        resources=[]
        for path in base.glob('*.schema.json'):
            value=json.loads(path.read_text())
            resources.append((value.get('$id',path.name),Resource.from_contents(value)))
        schema=json.loads((base/'catalog.schema.json').read_text())
        validator=Draft202012Validator(schema,registry=Registry().with_resources(resources))
        service=self.service();value=service.refresh_catalog(invalidate=True)
        validator.validate(value)

    def test_default_checked_source_action_drift_is_unknown(self):
        service=self.service()
        rows={entry.id:entry.as_dict() for entry in service.runtime.catalog.entries}
        rows['setup.default.agent.codex']['action']='unexpected setter'
        service.runtime.catalog=compile_catalog(rows)
        snapshot=service.refresh_catalog(invalidate=True)
        row=next(row for row in snapshot['entries'] if row['id']=='setup.default.agent.codex')
        self.assertIsNone(row['checked_state'])
        self.assertEqual(row['conditions']['checked']['status'],'unavailable')
        self.assertEqual(self.host.mutations,[])

    def test_read_surface_rejects_setters_and_only_one_mutation_family_exists(self):
        for command in [('omarchy-dns','Cloudflare'),('/usr/share/omarchy/bin/omarchy-default-agent','claude'),
                        NOTIFICATION_TOGGLE,('/usr/bin/hyprctl','dispatch','anything')]:
            with self.assertRaises(MenuReadUnavailable):self.reader.read(command)
        with self.assertRaises(MenuReadUnavailable):self.reader.toggle_notifications(NOTIFICATION_READ)
        self.assertEqual(self.host.calls,[])
        self.assertEqual(self.host.mutations,[])

    def test_bounded_runner_enforces_deadline_and_byte_cap_without_emitting_output(self):
        import subprocess,sys
        real_popen=subprocess.Popen
        def oversized(argv,**kwargs):
            return real_popen([sys.executable,'-c','import sys;sys.stdout.write("x"*4096)'],**kwargs)
        with patch('omodachi_core.live_menu_adapter.subprocess.Popen',side_effect=oversized):
            result=bounded_command(NOTIFICATION_READ,{},max_bytes=1024)
        self.assertEqual(result.error,'reader_output_limit')
        self.assertEqual(result.stdout,'')
        def slow(argv,**kwargs):
            return real_popen([sys.executable,'-c','import time;time.sleep(5)'],**kwargs)
        with patch('omodachi_core.live_menu_adapter.subprocess.Popen',side_effect=slow):
            result=bounded_command(NOTIFICATION_READ,{},timeout=.05)
        self.assertEqual(result.error,'reader_timeout')
        self.assertEqual(result.stdout,'')


class ConcurrentReaderCacheTests(unittest.TestCase):
    """PERF-4 §0: one script, one process, however many expressions want it."""

    def test_concurrent_callers_share_one_read(self):
        from concurrent.futures import ThreadPoolExecutor
        from omodachi_core.live_menu_adapter import LiveMenuReaders
        import threading as _t
        reads = []
        started = _t.Event()
        def slow():
            reads.append(1)
            started.set()
            time.sleep(0.05)      # long enough for the others to pile up
            return "chromium"
        readers = LiveMenuReaders(home=Path("/nonexistent"), runner=lambda *a, **k: None,
                                  mutation_runner=lambda *a, **k: None, graphical=lambda: {})
        with ThreadPoolExecutor(max_workers=8) as pool:
            answers = [future.result() for future in
                       [pool.submit(readers.cached, "getter:browser", slow) for _ in range(8)]]
        self.assertEqual(answers, ["chromium"] * 8)
        self.assertEqual(len(reads), 1, "fourteen `when` expressions must not be fourteen processes")
