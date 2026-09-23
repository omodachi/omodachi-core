import unittest
from types import SimpleNamespace
from pathlib import Path
from omodachi_core.agent_commands import AgentCommands
from omodachi_core.service import ServiceError
class CommandsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls=[]
        async def rpc(method,params):
            self.calls.append((method,params))
            return {'thread':{'id':'existing','cwd':'/work','name':'Original'}} if method=='thread/read' else {}
        self.adapter=SimpleNamespace(binding=SimpleNamespace(thread_id='existing'),active_turn=None,rpc=SimpleNamespace(request=rpc))
        async def require(auth):auth();return self.adapter
        self.commands=AgentCommands(SimpleNamespace(require=require,manager=SimpleNamespace(cwd=Path('/work'))))
    def payload(self,args='',request='one'):return {'revision':self.commands.revision,'arguments':args,'request_id':request}
    async def test_all_official_commands_list_and_status_executes_rpc_not_prompt(self):
        listing=self.commands.listing();self.assertEqual(len(listing['commands']),60)
        self.assertTrue(any(r['name']=='goal' and not r['available'] for r in listing['commands']))
        result=await self.commands.execute('status',self.payload(),lambda:None)
        self.assertEqual(result['result']['thread_id'],'existing');self.assertEqual(self.calls,[('thread/read',{'threadId':'existing','includeTurns':False})])
    async def test_rename_preserves_argument_content_and_deduplicates(self):
        p=self.payload('  My  New Name  ')
        await self.commands.execute('rename',p,lambda:None);await self.commands.execute('rename',p,lambda:None)
        self.assertEqual(self.calls,[('thread/name/set',{'threadId':'existing','name':'My  New Name'})])
    async def test_busy_compact_and_unsupported_never_send(self):
        self.adapter.active_turn='turn'
        with self.assertRaisesRegex(ServiceError,'agent_busy'):await self.commands.execute('compact',self.payload(),lambda:None)
        with self.assertRaisesRegex(ServiceError,'unsupported'):await self.commands.execute('new',self.payload(),lambda:None)
        self.assertEqual(self.calls,[])

    async def test_model_selection_updates_same_thread_settings_after_catalog_validation(self):
        async def rpc(method,params):
            self.calls.append((method,params))
            if method=='model/list':return {'data':[{'id':'model-choice','model':'actual-model','supportedReasoningEfforts':[{'reasoningEffort':'high'}]}],'nextCursor':None}
            return {}
        self.adapter.rpc.request=rpc
        self.adapter.active_turn='existing-turn'
        result=await self.commands.execute('model',self.payload(' model-choice high '),lambda:None)
        self.assertEqual(self.calls[-1],('thread/settings/update',{'threadId':'existing','model':'actual-model','effort':'high'}))
        self.assertEqual(result['result']['applies_to'],'subsequent_turns');self.assertEqual(self.adapter.active_turn,'existing-turn')
        with self.assertRaisesRegex(ServiceError,'model_unavailable'):
            await self.commands.execute('model',self.payload('invented','two'),lambda:None)
        self.assertEqual(sum(method=='thread/settings/update' for method,_ in self.calls),1)
    async def test_compact_reports_accepted_until_provider_completion(self):
        result=await self.commands.execute('compact',self.payload(),lambda:None)
        self.assertEqual(result['status'],'accepted');self.assertFalse(result['result']['completed'])
        self.assertEqual(self.calls,[('thread/compact/start',{'threadId':'existing'})])
