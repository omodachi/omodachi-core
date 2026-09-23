"""Ordinary plugin helper integration with real temporary IPC and fixed identity.

No real credential file, pairing/certificate fixture or host command is used.
"""
import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.ipc import JsonLineServer
from omodachi_core.plugin_actions import invoke_plugin_action,parameters
from omodachi_core.cli import host_main

FIXTURE='ordinary.plugin-fixture'
class FixedIdentity:
    def verify(self,value):
        if value!=FIXTURE:raise ValueError('fixture unavailable')
        return 'plugin-fixture'

class PluginActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.socket=str(Path(self.temp.name)/'core.sock')
        self.hub=Hub(authenticator=FixedIdentity());self.service=create_service(self.hub,demo=True)
        self.server=JsonLineServer(self.hub,self.socket);await self.server.start()
        self.loader=lambda:FIXTURE
    async def asyncTearDown(self):
        await self.server.close();await self.service.close_media();self.temp.cleanup()
    def revision(self):return self.service.refresh_catalog()['revision']
    async def invoke(self,entry='trigger.toggle.notifications',request='click-1',**kwargs):
        return await invoke_plugin_action(self.socket,entry_id=entry,catalog_revision=kwargs.pop('catalog_revision',self.revision()),
            request_id=request,credential_loader=self.loader,**kwargs)

    async def test_actual_daemon_action_and_idempotent_retry(self):
        revision=self.revision();before=self.hub.catalog_snapshot()
        first=await self.invoke(catalog_revision=revision)
        self.assertTrue(first['ok'],first);self.assertEqual(first['result']['status'],'accepted')
        after=self.hub.catalog_snapshot();self.assertNotEqual(before,after)
        retry=await self.invoke(catalog_revision=revision)
        self.assertEqual(first,retry);self.assertEqual(self.hub.catalog_snapshot(),after)
        self.assertNotIn(FIXTURE,json.dumps(first));self.assertNotIn('argv',json.dumps(first))

    async def test_an_older_revision_runs_and_an_unknown_parameter_does_not(self):
        """PERF-5. The revision is a hint the host may correct, not a gate.

        The plugin holds whatever revision its last read gave it, and the
        catalog moves whenever any condition in the menu does - which on a
        live host is several times a minute. Refusing on that was the reason
        a keybinding press had to re-read the whole table before it was
        allowed to do anything. The row is re-resolved by id instead, and the
        receipt says which revision it ran against.
        """
        before=self.hub.catalog_snapshot()
        stale=await self.invoke(catalog_revision='old-revision')
        self.assertTrue(stale['ok'],stale);self.assertEqual(stale['result']['status'],'accepted')
        self.assertEqual(stale['result']['catalog_revision'],before['revision'])
        self.assertNotEqual(self.hub.catalog_snapshot(),before)
        settled=self.hub.catalog_snapshot()
        rejected=await self.invoke(request='click-2',params={'shell':'anything'})
        self.assertFalse(rejected['ok']);self.assertEqual(self.hub.catalog_snapshot(),settled)

    async def test_explicit_displayed_target_is_required_and_revalidated(self):
        state=self.hub.state_snapshot('plugin-fixture')
        missing=await self.invoke(entry='omodachi.workspace.move.6')
        self.assertFalse(missing['ok']);self.assertEqual(missing['error'],'stale_target')
        result=await self.invoke(entry='omodachi.workspace.move.6',request='move-1',
            state_revision=state['revision'],target_token=state['focus']['target_token'])
        self.assertTrue(result['ok'],result);self.assertEqual(result['result']['status'],'accepted')
        # Keep the ten fixture workspaces the demo publishes: a move entry is
        # only ready for a workspace that exists, so an empty snapshot would
        # hide the stale-target rejection behind route_unavailable.
        self.service.set_workspace_snapshot(active=2,window_counts={n:0 for n in range(1,11)},
            focused_window={'id':'fixture-terminal','app_id':'fixture.terminal','app_name':'Terminal','workspace':2})
        old=await self.invoke(entry='omodachi.workspace.move.7',request='move-2',
            state_revision=state['revision'],target_token=state['focus']['target_token'])
        self.assertFalse(old['ok']);self.assertEqual(old['error'],'stale_target')

    async def test_prepared_native_descriptor_is_not_execution_and_argv_is_not_exposed(self):
        result=await self.invoke(entry='learn.keybindings')
        self.assertTrue(result['ok'],result);self.assertEqual(result['result']['status'],'prepared')
        self.assertEqual(result['result']['route']['native_view'],'shortcuts')
        self.assertNotIn('argv',result['result']['route']);self.assertNotIn('command',result['result']['route'])

    async def test_real_cli_uses_internal_plugin_loader_with_no_credential_argument(self):
        env=dict(os.environ);env['OMODACHI_TOKEN']=FIXTURE
        process=await asyncio.create_subprocess_exec(sys.executable,'-c','from omodachi_core.cli import host_main;raise SystemExit(host_main())',
            '--socket',self.socket,'plugin-action','trigger.toggle.notifications','--catalog-revision',self.revision(),
            '--request-id','cli-click',env=env,stdout=asyncio.subprocess.PIPE)
        output,_=await process.communicate();self.assertEqual(process.returncode,0)
        value=json.loads(output);self.assertTrue(value['ok']);self.assertEqual(value['result']['status'],'accepted')
        self.assertNotIn(FIXTURE,output.decode())

    async def test_malformed_requests_fail_before_credential_loader_or_socket(self):
        with self.assertRaises(ValueError):parameters('{"mode":1,"mode":2}')
        with self.assertRaises(ValueError):parameters('{"value":NaN}')
        with self.assertRaises(ValueError):parameters('[]')
        called=[]
        result=await invoke_plugin_action(self.socket,entry_id='bad;id',catalog_revision='current',request_id='x',
            credential_loader=lambda:called.append(True))
        self.assertFalse(result['ok']);self.assertEqual(called,[])
