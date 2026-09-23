"""Local HTTP rollout proof. All app/font/toggle mutation runners are fakes."""
from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import aiohttp
from omodachi_core.bootstrap import create_service
from omodachi_core.catalog import compile_catalog
from omodachi_core.catalog_providers import (FONT_LIST, FONT_CURRENT, FONT_SET,
    ReviewedProviderActionRunner, ProviderUnavailable, validate_action_context)
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
from omodachi_core.live_menu_adapter import (LiveMenuReaders, CommandResult, QUICK_SOURCES,
    FINITE_QUICK_COMMANDS, HYPR_FLAG_SETTER, IDLE_COMMAND, BAR_REFRESH, NOTIFICATION_READ,
    NOTIFICATION_TOGGLE, NOTIFICATION_REFRESH, install_live_menu_adapters)
from omodachi_core.service import CoreService


class FakeProviderHost:
    def __init__(self):
        self.apps = [{"appId": "org.example.Fixture", "label": "Fixture", "icon": "fixture",
                      "appRevision": "a" * 64}]
        self.fonts = ["A Font", "B Font"]
        self.current_font = self.fonts[0]
        self.writes = []
        self.apply_font = True
    def app_reader(self): return {"schema": 1, "apps": deepcopy(self.apps), "stats": {}}
    def read(self, argv, environment):
        if argv == (FONT_LIST,): return "\n".join(self.fonts)
        if argv == (FONT_CURRENT,): return self.current_font
        raise AssertionError("reader attempted unreviewed command")
    def apply(self, argv, environment):
        self.writes.append(tuple(argv))
        if argv[0] == FONT_SET and self.apply_font: self.current_font = argv[1]
        elif argv[:3] != ("/usr/bin/uwsm-app", "--", "/usr/bin/gtk-launch") and argv[0] != FONT_SET:
            raise AssertionError("unreviewed fake mutation")
        return {"status": "submitted"}
    def options(self):
        return {"app_reader": self.app_reader, "runner": self.read,
                "environment": lambda: {"PATH": "/usr/bin"}, "action_runner": self.apply}


class ProviderRunnerTests(unittest.TestCase):
    def setUp(self):
        self.env = {"HOME": "/home/test", "XDG_RUNTIME_DIR": "/run/user/1000",
                    "WAYLAND_DISPLAY": "wayland-1", "HYPRLAND_INSTANCE_SIGNATURE": "fixture"}
        self.launch = ("/usr/bin/uwsm-app", "--", "/usr/bin/gtk-launch", "Fixture App.desktop")
    def context(self):
        return patch.multiple("omodachi_core.catalog_providers.os", getuid=lambda: 1000, geteuid=lambda: 1000)
    def trusted(self):
        return patch("omodachi_core.catalog_providers.Path.stat", return_value=type("Info", (), {"st_uid": 0, "st_mode": stat.S_IFREG | 0o755})())
    def test_uid_home_runtime_and_session_are_mandatory(self):
        with self.context():
            validate_action_context(self.env, 1000, Path("/home/test"))
            for change in [{"HOME": "/home/other"}, {"XDG_RUNTIME_DIR": "/run/user/2"},
                           {"WAYLAND_DISPLAY": "malicious;"}, {"HYPRLAND_INSTANCE_SIGNATURE": ""}]:
                with self.assertRaises(ProviderUnavailable):
                    validate_action_context(self.env | change, 1000, Path("/home/test"))
            with self.assertRaises(ProviderUnavailable):
                validate_action_context(self.env, 1001, Path("/home/test"))
        with patch.multiple("omodachi_core.catalog_providers.os", getuid=lambda: 0, geteuid=lambda: 0):
            with self.assertRaises(ProviderUnavailable):validate_action_context(self.env, 0, Path("/home/test"))
    def test_actual_runner_uses_fixed_argv_and_no_captured_app_output(self):
        process=unittest.mock.Mock()
        spawn=unittest.mock.Mock(return_value=process)
        runner=ReviewedProviderActionRunner(owner_uid=1000, owner_home="/home/test", popen=spawn)
        with self.context(), self.trusted(), patch("omodachi_core.catalog_providers.os.access", return_value=True):
            self.assertEqual(runner(self.launch, self.env), {"status":"submitted"})
        args,kwargs=spawn.call_args
        self.assertEqual(args[0],self.launch)
        self.assertFalse(kwargs['shell']);self.assertTrue(kwargs['start_new_session'])
        self.assertEqual(kwargs['stdout'],subprocess.DEVNULL);self.assertEqual(kwargs['stderr'],subprocess.DEVNULL)
        self.assertEqual(kwargs['stdin'],subprocess.DEVNULL)
    def test_runner_rejects_font_metacharacters_and_untrusted_executable(self):
        runner=ReviewedProviderActionRunner(owner_uid=1000,owner_home="/home/test")
        with self.context(), self.trusted(), patch("omodachi_core.catalog_providers.os.access", return_value=True):
            runner.preflight((FONT_SET,"JetBrainsMono Nerd Font"),self.env)
            for value in ["--help","Bad / Font","Bad & Font","Bad < Font",'Bad " Font',"Bad; Font"]:
                with self.assertRaises(ProviderUnavailable):runner.preflight((FONT_SET,value),self.env)
            with self.assertRaises(ProviderUnavailable):runner.preflight(("/bin/sh","-c","anything"),self.env)
        writable=type("Info",(),{"st_uid":1000,"st_mode":stat.S_IFREG|0o777})()
        with self.context(),patch("omodachi_core.catalog_providers.Path.stat",return_value=writable):
            with self.assertRaises(ProviderUnavailable):runner.preflight(self.launch,self.env)
    def test_font_timeout_kills_wrapper_only_and_never_retries(self):
        process=unittest.mock.Mock();process.wait.side_effect=[subprocess.TimeoutExpired("launcher",12),0]
        spawn=unittest.mock.Mock(return_value=process)
        runner=ReviewedProviderActionRunner(owner_uid=1000,owner_home="/home/test",popen=spawn)
        with self.context(),self.trusted(),patch("omodachi_core.catalog_providers.os.access",return_value=True):
            with self.assertRaisesRegex(ProviderUnavailable,"outcome_unknown"):runner((FONT_SET,"JetBrainsMono Nerd Font"),self.env)
        spawn.assert_called_once();process.kill.assert_called_once()

    def test_an_application_launch_is_detached_and_never_waited_for(self):
        """PERF-4. The launcher's exit was up to five seconds of the user's tap."""
        process=unittest.mock.Mock()
        spawn=unittest.mock.Mock(return_value=process)
        runner=ReviewedProviderActionRunner(owner_uid=1000,owner_home="/home/test",popen=spawn)
        with self.context(),self.trusted(),patch("omodachi_core.catalog_providers.os.access",return_value=True):
            self.assertEqual(runner(self.launch,self.env),{"status":"submitted"})
        spawn.assert_called_once()
        process.wait.assert_not_called();process.poll.assert_not_called();process.kill.assert_not_called()
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])


class CatalogBootstrapHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'menu.jsonc';self.source.write_text(json.dumps({"apps":{"provider":"apps"},"style.font":{"provider":"fonts"}}))
        self.shell=self.root/'shell.json';self.shell.write_text('{"version":1,"bar":{"layout":{}}}')
        self.fake=FakeProviderHost();self.hub=Hub();self.token=self.hub.register_device("test-http-device")
        with patch('omodachi_core.graphical.HyprlandWorkspaceAdapter.refresh',lambda adapter:None):
            self.service=create_service(self.hub,default_menu=self.source,user_menu=self.root/'none',
                omodachi_menu=self.root/'none',shell_config=self.shell,enable_live_menu=False,
                catalog_provider_options=self.fake.options())
        self.server=NetworkServer(self.service,allow_loopback_http=True);await self.server.start()
        self.base=f'http://127.0.0.1:{self.server.bound_port}'
        self.client=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        self.headers={'Authorization':'Bearer '+self.token}
    async def asyncTearDown(self):
        await self.client.close();await self.server.close();self.temp.cleanup()
    async def get_catalog(self):
        self.service.refresh_sources()
        async with self.client.get(self.base+'/v1/catalog',headers=self.headers) as response:
            self.assertEqual(response.status,200);return await response.json()
    async def invoke(self,ident,revision,request_id='request-one',params=None,authorized=True):
        payload={'request_id':request_id,'catalog_revision':revision,'params':params or {}}
        async with self.client.post(self.base+'/v1/actions/'+ident+':invoke',headers=self.headers if authorized else {},json=payload) as response:
            return response.status,await response.json()
    async def test_http_list_prepare_fake_apply_and_identity_readback(self):
        before=await self.get_catalog();row=next(r for r in before['entries'] if r['id']=='apps.org.example.Fixture')
        self.assertEqual(row['kind'],'app');self.assertTrue(row['route']['ready'])
        plan=self.service.catalog_providers.prepare_app(row['id'],self.service.catalog_providers.apps_revision)
        self.assertEqual(self.fake.writes,[])
        status,result=await self.invoke(row['id'],before['revision'])
        self.assertEqual(status,200);self.assertEqual(result['status'],'accepted')
        self.assertEqual(self.fake.writes,[plan.argv])
        self.assertEqual(self.service.catalog_providers.last_action_result['status'],'submitted_identity_confirmed')
        after=await self.get_catalog();self.assertIn(row['id'],[r['id'] for r in after['entries']])
        again=await self.invoke(row['id'],before['revision']);self.assertEqual(again,(status,result));self.assertEqual(len(self.fake.writes),1)
    async def test_http_font_apply_verified_by_current_reader_and_catalog(self):
        before=await self.get_catalog();row=next(r for r in before['entries'] if r['id']=='style.font.b-font')
        self.assertTrue(row['route']['ready']);self.assertNotEqual(row['icon'],'✓')
        status,result=await self.invoke(row['id'],before['revision'])
        self.assertEqual((status,result['status']),(200,'accepted'))
        after=await self.get_catalog();updated=next(r for r in after['entries'] if r['id']==row['id'])
        self.assertEqual(updated['icon'],'✓');self.assertNotEqual(after['revision'],before['revision'])
        self.assertEqual(self.service.catalog_providers.last_action_result['status'],'readback_confirmed')
    async def test_failed_font_readback_is_failed_and_not_retried(self):
        self.fake.apply_font=False;before=await self.get_catalog()
        first=await self.invoke('style.font.b-font',before['revision'])
        self.assertEqual(first[1]['status'],'failed')
        self.assertEqual(await self.invoke('style.font.b-font',before['revision']),first)
        self.assertEqual(len(self.fake.writes),1);self.assertEqual(self.fake.current_font,'A Font')
    async def test_http_exec_change_source_change_delete_and_static_collision_reject_old(self):
        """PERF-5 rewrote the codes here; what the test is for is `writes`.

        Every one of these four is a desktop entry that stopped being the
        entry the client was shown - rewritten, deleted, shadowed by a static
        row, or dropped from the menu source. Before PERF-5 each arrived as a
        409 from the whole-table re-read `invoke` took before authorising
        anything. That read is gone from the tap; the refusals are not, they
        are just narrower and some of them are now a failed receipt rather
        than an HTTP error. `self.fake.writes` is the assertion that matters
        and it is unchanged: nothing was launched, in any of them.
        """
        before=await self.get_catalog();self.fake.apps[0]['appRevision']='b'*64
        status,value=await self.invoke('apps.org.example.Fixture',before['revision'])
        # The provider's own identity re-read, inside the executor, is what
        # refuses a rewritten desktop entry now.
        self.assertEqual((status,value['status']),(200,'failed'));self.assertFalse(self.fake.writes)
        before=await self.get_catalog();self.fake.apps=[]
        status,value=await self.invoke('apps.org.example.Fixture',before['revision'],'deleted')
        # The listing the client was shown is still the current one, so this is
        # the executor's identity re-read again, not a catalog refusal.
        self.assertEqual((status,value['status']),(200,'failed'));self.assertFalse(self.fake.writes)
        self.fake.apps=FakeProviderHost().apps;before=await self.get_catalog()
        self.source.write_text(json.dumps({'apps':{'provider':'apps'},'apps.org.example.Fixture':{'label':'Static override'}}))
        status,value=await self.invoke('apps.org.example.Fixture',before['revision'],'static')
        self.assertEqual((status,value['error']['code']),(409,'route_unavailable'));self.assertFalse(self.fake.writes)
        after=await self.get_catalog();static=next(r for r in after['entries'] if r['id']=='apps.org.example.Fixture')
        self.assertFalse(static['route']['supported']);self.assertNotIn(static['id'],self.service._executors)
        self.source.write_text(json.dumps({'apps':{'provider':'apps','action':'changed'}}))
        after=await self.get_catalog();self.assertNotIn('apps.org.example.Fixture',[r['id'] for r in after['entries']])
    async def test_http_unknown_id_parameters_and_unauthenticated_requests_do_not_execute(self):
        before=await self.get_catalog()
        self.assertEqual((await self.invoke('apps.not-present',before['revision']))[0],404)
        self.assertEqual((await self.invoke('apps.org.example.Fixture',before['revision'],'param',{'argv':['evil']}))[0],400)
        self.assertEqual((await self.invoke('apps.org.example.Fixture',before['revision'],'no-auth',authorized=False))[0],401)
        self.assertEqual(self.fake.writes,[])
    async def test_bootstrap_can_disable_providers_or_install_list_only(self):
        with patch('omodachi_core.graphical.HyprlandWorkspaceAdapter.refresh',lambda adapter:None):
            disabled=create_service(Hub(),default_menu=self.source,user_menu=self.root/'none',omodachi_menu=self.root/'none',
                shell_config=self.shell,enable_live_menu=False,enable_catalog_providers=False)
            readonly=create_service(Hub(),default_menu=self.source,user_menu=self.root/'none',omodachi_menu=self.root/'none',
                shell_config=self.shell,enable_live_menu=False,catalog_provider_options=self.fake.options()|{'actions':False})
        self.assertFalse(hasattr(disabled,'catalog_providers'))
        row=next(r for r in readonly.refresh_catalog()['entries'] if r['id']=='apps.org.example.Fixture')
        self.assertFalse(row['route']['ready']);self.assertFalse(self.fake.writes)


class FiniteToggleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.home=Path(self.temp.name);self.calls=[];self.dnd='off';self.apply=True
        def read(argv,env):
            if argv==NOTIFICATION_READ:return CommandResult(0,self.dnd)
            raise AssertionError('unexpected read')
        def mutation(argv,env):
            self.calls.append(argv)
            if not self.apply:return CommandResult(0)
            if argv==NOTIFICATION_TOGGLE:self.dnd='on' if self.dnd=='off' else 'off'
            elif argv[:1]==IDLE_COMMAND:
                target=self.home/'.local/state/omarchy/indicators/stay-awake'
                target.parent.mkdir(parents=True,exist_ok=True)
                if argv[1]=='stay-awake':target.touch()
                else:target.unlink(missing_ok=True)
            elif argv[0]=='/usr/share/omarchy/bin/omarchy-toggle':
                target=self.home/'.local/state/omarchy/toggles'/argv[1];target.parent.mkdir(parents=True,exist_ok=True)
                if argv[2]=='on':target.touch()
                else:target.unlink(missing_ok=True)
            elif argv[0]==HYPR_FLAG_SETTER:
                # The packaged setter copies or removes one reviewed toggle
                # template under ~/.local/state/omarchy/toggles/hypr.
                target=self.home/'.local/state/omarchy/toggles/hypr'/(argv[1]+'.lua')
                target.parent.mkdir(parents=True,exist_ok=True)
                if argv[2]=='on':target.touch()
                else:target.unlink(missing_ok=True)
            elif argv not in {NOTIFICATION_REFRESH,BAR_REFRESH}:raise AssertionError('unexpected mutation')
            return CommandResult(0)
        self.readers=LiveMenuReaders(home=self.home,runner=read,mutation_runner=mutation,
            graphical=lambda:{'XDG_RUNTIME_DIR':f'/run/user/{os.getuid()}','WAYLAND_DISPLAY':'wayland-1'})
        self.service=CoreService(Hub(),catalog=compile_catalog({key:dict(value) for key,value in QUICK_SOURCES.items()}))
        install_live_menu_adapters(self.service,readers=self.readers)
    def tearDown(self):self.temp.cleanup()
    def test_exactly_six_finite_families_and_readback(self):
        for index,ident in enumerate(FINITE_QUICK_COMMANDS):
            before=self.service.refresh_catalog(invalidate=True);row=next(r for r in before['entries'] if r['id']==ident)
            self.assertTrue(row['route']['ready']);old=row['checked_state']
            result=self.service.invoke({'entry_id':ident,'request_id':f'request-{index}','catalog_revision':before['revision'],'params':{}},'fixture')
            self.assertEqual(result['status'],'accepted')
            after=self.service.refresh_catalog(invalidate=True);new=next(r for r in after['entries'] if r['id']==ident)
            self.assertIs(new['checked_state'],not old)
        self.assertEqual(len(FINITE_QUICK_COMMANDS),6)
        self.assertIn(BAR_REFRESH,self.calls)
        self.assertFalse(any('hyprctl' in command[0] or 'font' in command[0] for command in self.calls))
    def test_source_condition_override_and_failed_readback_do_not_gain_success(self):
        self.apply=False;before=self.service.refresh_catalog(invalidate=True)
        payload={'entry_id':'trigger.toggle.idle-lock','request_id':'fail','catalog_revision':before['revision'],'params':{}}
        result=self.service.invoke(payload,'fixture');self.assertEqual(result['status'],'failed')
        self.assertEqual(self.service.invoke(payload,'fixture'),result);self.assertEqual(len(self.calls),1)
        values={key:dict(value) for key,value in QUICK_SOURCES.items()};values['trigger.toggle.screensaver']['when']='changed'
        self.service.runtime.catalog=compile_catalog(values)
        row=next(r for r in self.service.refresh_catalog(invalidate=True)['entries'] if r['id']=='trigger.toggle.screensaver')
        self.assertFalse(row['route']['supported'])
    def test_symlink_flag_target_is_rejected_before_mutation(self):
        target=self.home/'.local/state/omarchy/indicators';target.mkdir(parents=True)
        outside=self.home/'outside';outside.touch();(target/'stay-awake').symlink_to(outside)
        with self.assertRaisesRegex(ValueError,'untrusted'):
            self.readers.execute_quick('trigger.toggle.idle-lock',IDLE_COMMAND)
        self.assertFalse(self.calls)


if __name__=='__main__':unittest.main()
