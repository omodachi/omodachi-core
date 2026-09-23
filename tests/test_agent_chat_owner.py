import asyncio
from pathlib import Path
import tempfile
import unittest
from omodachi_core.agent_chat_owner import OwnedCodexAgent
from omodachi_core.agent_chat_provider import AgentChatError

class RPC:
    def __init__(self): self.calls=[]; self.closed=False; self.fail=False
    async def initialize(self): return {}
    async def close(self): self.closed=True
    async def request(self, method, params):
        self.calls.append((method,params))
        if self.fail: raise AgentChatError('provider_disconnected')
        return {'thread':{'id':params.get('threadId','created-default-thread')}}

class OwnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.rpc=RPC()
        async def proxy(): return self.rpc
        self.owner=OwnedCodexAgent(self.root,self.root,'testhost',rpc_factory=proxy)
        # Controlled peer, not an actual daemon: record a listener the
        # injected rpc_factory answers for.
        self.owner._write_endpoint(47311)
    async def asyncTearDown(self): self.temp.cleanup()
    async def test_create_once_and_reuse_identity(self):
        first,second=await asyncio.gather(self.owner.ensure_new_or_owned(),self.owner.ensure_new_or_owned())
        self.assertEqual(first,second)
        self.assertEqual([m for m,_ in self.rpc.calls],['thread/start','thread/resume'])
        self.assertEqual(self.owner.tui_argv(first)[1:4],['resume','--remote','ws://127.0.0.1:47311'])
        self.assertEqual(oct(self.owner.token_file.stat().st_mode & 0o777),'0o600')
        await self.owner.close_proxy();self.assertTrue(self.rpc.closed)
        self.assertTrue(self.owner.metadata.exists())
    async def test_existing_tui_never_started_or_migrated_implicitly(self):
        with self.assertRaisesRegex(AgentChatError,'existing_thread_handoff_required'):
            await self.owner.ensure_new_or_owned(existing_agent=True)
        prepared=self.owner.prepare_existing('old-thread','pane1')
        self.assertEqual(prepared['thread_id'],'old-thread');self.assertEqual(self.rpc.calls,[])
        with self.assertRaises(AgentChatError):await self.owner.resume_existing('old-thread')
        binding=await self.owner.resume_existing('old-thread',handoff_confirmed=True)
        self.assertEqual(binding.thread_id,'old-thread')
        self.assertEqual([m for m,_ in self.rpc.calls],['thread/resume'])
    async def test_uncertain_creation_never_retried_as_new_thread(self):
        self.rpc.fail=True
        with self.assertRaises(AgentChatError):await self.owner.ensure_new_or_owned()
        self.rpc.fail=False
        with self.assertRaisesRegex(AgentChatError,'creation_unconfirmed'):await self.owner.ensure_new_or_owned()
        self.assertEqual(len(self.rpc.calls),1)
    async def test_resume_failure_never_falls_back_to_start(self):
        await self.owner.ensure_new_or_owned();self.rpc.fail=True
        with self.assertRaises(AgentChatError):await self.owner.ensure_new_or_owned()
        self.assertEqual([m for m,_ in self.rpc.calls],['thread/start','thread/resume'])

    async def test_durable_unit_survives_proxy_close_and_stops_only_explicitly(self):
        self.owner.durable_unit=True
        commands=[]
        async def command(*args):
            commands.append(args)
            if args[0]=='systemctl' and 'is-active' in args:return 3,'inactive'
            return 0,''
        self.owner._unit_command=command
        await self.owner._spawn()
        self.assertTrue(any(c[0]=='systemd-run' and '--user' in c for c in commands))
        self.assertTrue(any('--property=Type=exec' in c for c in commands))
        self.owner.rpc=self.rpc
        await self.owner.close_proxy()
        self.assertFalse(any('stop' in c for c in commands))
        await self.owner.stop_owned()
        self.assertTrue(any(c[:3]==('systemctl','--user','stop') for c in commands))

    async def test_empty_recovery_checks_absence_and_records_old_to_new(self):
        self.owner._save(self.owner._record('lost-empty-thread',creation_pending=False))
        original=self.rpc.request
        async def request(method,params):
            if method=='thread/loaded/list':return {'data':[]}
            if method=='thread/read':raise AgentChatError('provider_thread_rollout_missing')
            return await original(method,params)
        self.rpc.request=request
        binding=await self.owner.recreate_empty_lost('lost-empty-thread')
        self.assertEqual(binding.thread_id,'created-default-thread')
        record=self.owner.owned_metadata()
        self.assertEqual(record['previous_thread_id'],'lost-empty-thread')
        self.assertTrue((self.root/record['previous_metadata']).exists())
        self.assertEqual(record['recovery_reason'],'confirmed_empty_thread_lost_with_core_cgroup')

    async def test_empty_recovery_refuses_existing_readable_thread(self):
        self.owner._save(self.owner._record('readable-thread',creation_pending=False))
        async def request(method,params):
            if method=='thread/loaded/list':return {'data':['readable-thread']}
            raise AssertionError('must not read/start after finding loaded thread')
        self.rpc.request=request
        with self.assertRaisesRegex(AgentChatError,'still_available'):
            await self.owner.recreate_empty_lost('readable-thread')
        self.assertEqual(self.owner.owned_metadata()['thread_id'],'readable-thread')

    async def test_not_loaded_resumable_never_replaced(self):
        self.owner._save(self.owner._record('existing-history',creation_pending=False))
        calls=[]
        async def request(method,params):
            calls.append(method)
            if method=='thread/loaded/list':return {'data':[]}
            if method=='thread/read':raise AgentChatError('provider_thread_not_loaded')
            if method=='thread/resume':return {'thread':{'id':'existing-history'}}
            raise AssertionError('must not replace resumable thread')
        self.rpc.request=request
        with self.assertRaisesRegex(AgentChatError,'still_available'):
            await self.owner.recreate_empty_lost('existing-history')
        self.assertEqual(calls,['thread/loaded/list','thread/read','thread/resume'])
        self.assertEqual(self.owner.owned_metadata()['thread_id'],'existing-history')
        self.assertEqual(list(self.root.glob('owner-before*')),[])

    async def test_not_loaded_then_missing_rollout_permits_explicit_empty_recovery(self):
        self.owner._save(self.owner._record('empty-lost',creation_pending=False))
        calls=[]
        async def request(method,params):
            calls.append(method)
            if method=='thread/loaded/list':return {'data':[]}
            if method=='thread/read':raise AgentChatError('provider_thread_not_loaded')
            if method=='thread/resume':raise AgentChatError('provider_thread_rollout_missing')
            if method=='thread/start':return {'thread':{'id':'new-empty'}}
            raise AssertionError(method)
        self.rpc.request=request
        result=await self.owner.recreate_empty_lost('empty-lost')
        self.assertEqual(result.thread_id,'new-empty')
        self.assertEqual(calls,['thread/loaded/list','thread/read','thread/resume','thread/start'])
        self.assertEqual(self.owner.owned_metadata()['previous_thread_id'],'empty-lost')

if __name__=='__main__':unittest.main()
