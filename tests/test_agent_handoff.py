import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from omodachi_core.agent_handoff import DefaultAgentHandoff
from omodachi_core.agent_chat_provider import DefaultAgentBinding
from omodachi_core.service import ServiceError

class HandoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.stopped=False;self.started=[];self.status='idle';self.fail_remote=False;self.owner_record=None;self.wrong_thread=False
        self.target={'name':'default','agent':'codex','agent_status':'idle','pane_id':'p1','terminal_id':'t1','agent_session':{'agent':'codex','kind':'id','value':'existing-thread'}}
        owner=self
        class Provider:
            async def resume_existing(inner,thread_id,handoff_confirmed=False):
                self.assertTrue(self.stopped);self.assertTrue(handoff_confirmed)
                self.owner_record={'thread_id':thread_id}
                return DefaultAgentBinding('host',thread_id,str(self.root/'socket'))
            def tui_argv(inner,b):return ['codex','resume','--remote','unix://'+b.daemon_socket,b.thread_id]
            async def stop_owned(inner):pass
            def owned_metadata(inner):return self.owner_record
            def _save(inner,r):self.owner_record=r
        self.chat=SimpleNamespace(manager=SimpleNamespace(root=self.root),owner=Provider())
        def runner(argv):
            if argv[3:]==('agent','get','default'):return {'agent':{**self.target,'agent_status':self.status}}
            if argv[3:6]==('pane','process-info','--pane'):
                return {'process_info':{'shell_pid':100,'foreground_process_group_id':100 if self.stopped else 200,'foreground_processes':[] if self.stopped else [{'pid':200,'name':'codex'}]}}
            if argv[3:6]==('agent','start','default'):
                self.started.append(argv)
                if '--remote' in argv and self.fail_remote:raise ServiceError('agent_start_failed',status=409)
                self.stopped=False;return {'agent':{'pane_id':'p1','agent':'codex','agent_session':{'agent':'codex','kind':'id','value':'wrong-thread' if self.wrong_thread else 'existing-thread'}}}
            raise AssertionError(argv)
        def terminate(pid):self.assertEqual(pid,200);self.stopped=True
        self.handoff=DefaultAgentHandoff(self.chat,runner=runner,terminate=terminate,process_stamp=lambda pid:'same-start')
    def tearDown(self):self.tmp.cleanup()
    async def test_prepare_has_no_effect_confirm_resumes_same_thread_same_pane(self):
        plan=await self.handoff.prepare(lambda:None);self.assertFalse(self.stopped);self.assertEqual(self.started,[])
        self.assertTrue(plan['requires_confirmation']);self.assertEqual(plan['provider_session_id'],'existing-thread')
        result=await self.handoff.confirm({'plan_id':plan['plan_id'],'confirmed':True},lambda:None)
        self.assertTrue(result['same_thread']);self.assertEqual(result['pane_id'],'p1')
        self.assertIn('--remote',self.started[0]);self.assertEqual(self.started[0][-1],'existing-thread')
        await self.handoff.confirm({'plan_id':plan['plan_id'],'confirmed':True},lambda:None);self.assertEqual(len(self.started),1)
    async def test_busy_after_prepare_preserves_original_work(self):
        plan=await self.handoff.prepare(lambda:None);self.status='working'
        with self.assertRaisesRegex(ServiceError,'agent_busy'):await self.handoff.confirm({'plan_id':plan['plan_id'],'confirmed':True},lambda:None)
        self.assertFalse(self.stopped);self.assertEqual(self.started,[])
    async def test_failed_remote_start_returns_to_same_legacy_thread(self):
        plan=await self.handoff.prepare(lambda:None);self.fail_remote=True
        with self.assertRaisesRegex(ServiceError,'handoff_rolled_back'):await self.handoff.confirm({'plan_id':plan['plan_id'],'confirmed':True},lambda:None)
        self.assertEqual(self.started[-1][-2:],('resume','existing-thread'));self.assertNotIn('--remote',self.started[-1])
        self.assertEqual(self.owner_record['mode'],'legacy_handoff_rolled_back')

    async def test_wrong_thread_readback_is_not_completed(self):
        plan=await self.handoff.prepare(lambda:None);self.wrong_thread=True
        with self.assertRaisesRegex(ServiceError,'handoff_attach_unconfirmed'):
            await self.handoff.confirm({'plan_id':plan['plan_id'],'confirmed':True},lambda:None)
        import json
        record=json.loads((self.root/'agent-handoff'/(plan['plan_id']+'.json')).read_text())
        self.assertEqual(record['state'],'attachment_needs_review');self.assertNotIn('result',record)
