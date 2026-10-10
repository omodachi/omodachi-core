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
            return {'thread':{'id':'existing','cwd':'/work','name':'Original','status':{'type':'idle'}}} if method=='thread/read' else {}
        async def snapshot():return {'rows':[]}
        self.adapter=SimpleNamespace(binding=SimpleNamespace(thread_id='existing',cwd='/work'),active_turn=None,
                                     rpc=SimpleNamespace(request=rpc),settings={'model':'gpt-6-astra','effort':'high'},
                                     snapshot=snapshot)
        async def require(auth):auth();return self.adapter
        self.commands=AgentCommands(SimpleNamespace(require=require,manager=SimpleNamespace(cwd=Path('/work'))))
    def payload(self,args='',request='one'):return {'revision':self.commands.revision,'arguments':args,'request_id':request}
    async def test_all_official_commands_list_and_status_executes_rpc_not_prompt(self):
        listing=self.commands.listing()
        # 60 official commands, less the three codex marks as developer-only.
        self.assertEqual(len(listing['commands']),57)
        self.assertFalse({'debug-m-drop','debug-m-update','test-approval'}&{r['id'] for r in listing['commands']})
        self.assertTrue(any(r['name']=='goal' and not r['available'] for r in listing['commands']))
        result=await self.commands.execute('status',self.payload(),lambda:None)
        self.assertEqual(result['display'],{'key':'status','args':{'state':'idle','cwd':'/work','name':'Original','model':'gpt-6-astra'}})
        self.assertEqual(result['result']['summary'],'Original: Idle in /work with gpt-6-astra.')
        self.assertEqual(self.calls,[('thread/read',{'threadId':'existing','includeTurns':False})])
    async def test_rename_preserves_argument_content_and_deduplicates(self):
        p=self.payload('  My  New Name  ')
        await self.commands.execute('rename',p,lambda:None);await self.commands.execute('rename',p,lambda:None)
        self.assertEqual(self.calls,[('thread/name/set',{'threadId':'existing','name':'My  New Name'})])
    async def test_busy_compact_and_new_and_unsupported_never_send(self):
        self.adapter.active_turn='turn'
        with self.assertRaisesRegex(ServiceError,'agent_busy'):await self.commands.execute('compact',self.payload(),lambda:None)
        # /new is the New conversation confirmation, which waits for the turn.
        with self.assertRaisesRegex(ServiceError,'agent_busy'):await self.commands.execute('new',self.payload(),lambda:None)
        with self.assertRaisesRegex(ServiceError,'unsupported'):await self.commands.execute('goal',self.payload(),lambda:None)
        self.assertEqual(self.calls,[])

    async def test_model_opens_the_picker_and_a_valid_argument_preselects_it(self):
        async def rpc(method,params):
            self.calls.append((method,params))
            if method=='model/list':return {'data':[{'id':'model-choice','model':'actual-model','displayName':'Model Choice',
                                                     'supportedReasoningEfforts':[{'reasoningEffort':'high'}]}],'nextCursor':None}
            return {}
        self.adapter.rpc.request=rpc
        self.adapter.active_turn='existing-turn'
        plain=await self.commands.execute('model',self.payload(''),lambda:None)
        self.assertEqual(plain['native_request'],{'action':'choose_model','arguments':''})
        self.assertEqual(plain['display']['key'],'model.choose');self.assertEqual(self.calls,[])
        result=await self.commands.execute('model',self.payload(' model-choice high ','two'),lambda:None)
        self.assertEqual(result['native_request'],{'action':'choose_model','arguments':'model-choice high',
                                                   'model':'model-choice','effort':'high'})
        self.assertEqual(result['display'],{'key':'model.selected','args':{'model':'Model Choice','effort':'high'}})
        self.assertEqual(result['result']['applies_to'],'next_turn');self.assertEqual(self.adapter.active_turn,'existing-turn')
        # codex has no stable setter: nothing but the catalog was asked.
        self.assertEqual([m for m,_ in self.calls],['model/list'])
        with self.assertRaisesRegex(ServiceError,'model_unavailable'):
            await self.commands.execute('model',self.payload('invented','three'),lambda:None)
        with self.assertRaisesRegex(ServiceError,'reasoning_effort_unavailable'):
            await self.commands.execute('model',self.payload('model-choice ludicrous','four'),lambda:None)
    async def test_compact_reports_accepted_until_provider_completion(self):
        result=await self.commands.execute('compact',self.payload(),lambda:None)
        self.assertEqual(result['status'],'accepted');self.assertEqual(result['display']['key'],'compact')
        self.assertEqual(self.calls,[('thread/compact/start',{'threadId':'existing'})])
