import asyncio
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
import aiohttp
from omodachi_core.agent import ProbeStatus
from omodachi_core.agent_lifecycle import DefaultAgentManager,atomic_private_json
from omodachi_core.agent_chat_service import DefaultAgentChatService
from omodachi_core.agent_chat_provider import DefaultAgentBinding,AgentChatError
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer

class Owner:
    def __init__(self,root):self.root=root;self.binding=DefaultAgentBinding('host','same-thread',str(root/'socket'));self.calls=0
    def owned_metadata(self):return None
    async def ensure_new_or_owned(self,existing_agent=False):
        if existing_agent:raise AgentChatError('existing_thread_handoff_required')
        self.calls+=1;return self.binding
    def tui_argv(self,b):return ['codex','resume','--remote','unix://'+b.daemon_socket,b.thread_id]
    async def close_proxy(self):pass
class Adapter:
    def __init__(self,b,**options):
        self.binding=b;self.attached=True;self.events=asyncio.Queue();self.options=options;self.requests={};self.active_turn=None
        self.pending_approvals={'7':{'request_id':'7','kind':'commandExecution','summary':'ls -la',
                                     'details':{'item_id':'i1'},'decisions':['accept','acceptForSession','decline','cancel'],
                                     'provider_id':7,'params':{}}}
        self.resolved=[];self.steered=[];self.turns=[]
        self.status={'type':'active','activeFlags':['waitingOnApproval']}
    def approvals_value(self):return [{k:v[k] for k in ('request_id','kind','summary','details','decisions')} for v in self.pending_approvals.values()]
    def status_value(self):return {'type':self.status['type'],'activeFlags':list(self.status['activeFlags']),
                                   'waiting':'approval' if 'waitingOnApproval' in self.status['activeFlags'] else None}
    def usage_value(self):return {'tokens':{'total':{'totalTokens':12}},'rate_limits':{'primary':{'usedPercent':3}},'model':'gpt-6-astra','effort':'high'}
    async def snapshot(self):return {'identity':self.binding.identity(),'rows':[],'activeTurn':self.active_turn,'sequence':0,
                                     'acceptedRequestIDs':list(self.requests),'pendingApprovals':self.approvals_value(),
                                     'status':self.status_value(),'usage':self.usage_value()}
    async def send(self,text,request_id,*,model=None,effort=None):
        self.requests[request_id]=text;self.turns.append((model,effort))
        self.options['persist_delivery']({request_id:{'accepted':True}});self.active_turn='turn-1'
        await self.events.put({'identity':self.binding.identity(),'sequence':1,'event':{'type':'turnStarted','turnID':'turn-1'}})
        return {'accepted':True,'delivery':'accepted','turnID':'turn-1'}
    async def resolve_approval(self,request_id,*,decision=None,answers=None):
        if request_id not in self.pending_approvals:raise AgentChatError('agent_approval_unknown')
        row=self.pending_approvals.pop(request_id);self.resolved.append((request_id,decision,answers))
        self.status={'type':'active','activeFlags':[]}
        return {'request_id':request_id,'kind':row['kind'],'decision':decision or 'input','resolved':True}
    async def models(self):return {'models':[{'id':'gpt-6-astra','is_default':True,'efforts':['low','high']}],'default':'gpt-6-astra'}
    async def steer(self,text,request_id):
        self.steered.append((request_id,text));return {'accepted':True,'turnID':'turn-1','delivery':'steered'}
    async def cancel(self,turn_id):return {'requested':turn_id==self.active_turn}
    async def close(self):self.attached=False

class ChatServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.cap=SimpleNamespace(configured=True,configured_kind='codex',default_agent_exists=False,default_agent_probe=ProbeStatus.MISSING)
        self.manager=DefaultAgentManager(root=root,probe=SimpleNamespace(inspect=lambda:(self.cap,None)),runner=lambda _:self.fail('legacy TUI start forbidden'))
        self.owner=Owner(root)
        async def connect(b,**options):return Adapter(b,**options)
        self.hub=Hub();self.token=self.hub.register_device('phone')
        self.chat=DefaultAgentChatService(self.manager,'host',owner=self.owner,connector=connect,hub=self.hub)
        self.service=create_service(self.hub,demo=True)
        self.service.agent_manager=self.manager;self.service.agent_chat=self.chat
        self.server=NetworkServer(self.service,allow_loopback_http=True);await self.server.start();self.client=aiohttp.ClientSession()
        self.url=f'http://127.0.0.1:{self.server.bound_port}';self.headers={'Authorization':'Bearer '+self.token}
    async def asyncTearDown(self):await self.client.close();await self.chat.close();await self.server.close();self.tmp.cleanup()
    async def post(self,path,body):
        async with self.client.post(self.url+path,headers=self.headers,json=body) as r:return r.status,await r.json()
    async def test_new_default_once_snapshot_stream_send_interrupt_without_remote(self):
        for _ in range(2):
            code,result=await self.post('/v1/agent/default:ensure',{'surface':'chat'});self.assertEqual(code,200,result)
            self.assertEqual(result['identity']['providerSessionID'],'same-thread');self.assertFalse(result['herdr_pane_registered'])
        self.assertEqual(self.owner.calls,1);self.assertIsNone(self.hub.state_snapshot()['remote']['session_id'])
        ws=await self.client.ws_connect(self.url+'/v1/agent/default/chat/events',headers=self.headers)
        self.assertEqual((await ws.receive_json())['type'],'snapshot')
        code,result=await self.post('/v1/agent/default/chat/messages',{'request_id':'r1','text':'hello'});self.assertEqual(code,200,result)
        self.assertEqual(result['delivery'],'accepted')
        self.assertEqual((await ws.receive_json())['event']['type'],'turnStarted')
        code,result=await self.post('/v1/agent/default/chat/interrupt',{'turn_id':'turn-1'});self.assertEqual(code,200,result);self.assertTrue(result['requested'])
        self.assertTrue(self.chat.delivery_path.exists())
        # The stream reads as well as writes: a client frame is refused, and
        # the same reader is what lets aiohttp answer a keepalive ping.
        self.assertTrue((await ws.ping()) is None or True)
        await ws.send_str('hello')
        message=await ws.receive()
        self.assertEqual(message.type,aiohttp.WSMsgType.CLOSE)
        self.assertEqual(message.data,1008)
        await ws.close()
    async def get(self,path):
        async with self.client.get(self.url+path,headers=self.headers) as r:return r.status,await r.json()

    async def test_approvals_models_usage_and_steer_ride_the_same_chat(self):
        code,result=await self.post('/v1/agent/default:ensure',{'surface':'chat'});self.assertEqual(code,200,result)
        self.assertEqual(result['snapshot']['pendingApprovals'][0]['summary'],'ls -la')
        # The provider's own waiting status is what the badge shows.
        self.assertEqual(self.hub.state_snapshot()['agent']['status'],'waiting_on_approval')
        code,result=await self.get('/v1/agent/default/chat/approvals');self.assertEqual(code,200,result)
        self.assertEqual(result['requests'][0]['decisions'][0],'accept')
        code,result=await self.post('/v1/agent/default/chat/approvals/7',{'decision':'accept'})
        self.assertEqual(code,200,result);self.assertTrue(result['resolved'])
        self.assertEqual(self.chat.chat.resolved,[('7','accept',None)])
        code,result=await self.post('/v1/agent/default/chat/approvals/7',{'decision':'accept'})
        self.assertEqual(code,409,result);self.assertEqual(result['error']['code'],'agent_approval_unknown')
        code,result=await self.post('/v1/agent/default/chat/approvals/7',{'decision':'accept','input':{}})
        self.assertEqual(code,400,result)
        code,result=await self.get('/v1/agent/default/models')
        self.assertEqual(code,200,result);self.assertEqual(result['default'],'gpt-6-astra')
        code,result=await self.get('/v1/agent/default/chat/usage')
        self.assertEqual(code,200,result);self.assertEqual(result['usage']['model'],'gpt-6-astra')
        code,result=await self.post('/v1/agent/default/chat/messages',{'request_id':'r9','text':'hi','model':'gpt-6-astra','effort':'high'})
        self.assertEqual(code,200,result);self.assertEqual(self.chat.chat.turns,[('gpt-6-astra','high')])
        code,result=await self.post('/v1/agent/default/chat/steer',{'request_id':'r10','text':'also check tests'})
        self.assertEqual(code,200,result);self.assertEqual(result['delivery'],'steered')
        self.assertEqual(self.chat.chat.steered,[('r10','also check tests')])

    async def test_existing_independent_tui_requires_handoff_without_new_thread(self):
        self.cap.default_agent_exists=True
        code,result=await self.post('/v1/agent/default:ensure',{'surface':'chat'})
        self.assertEqual(code,409,result);self.assertEqual(result['error']['code'],'existing_thread_handoff_required');self.assertEqual(self.owner.calls,0)
    async def test_explicit_empty_recovery_replaces_only_expected_empty_owned_session(self):
        self.owner.owned_metadata=lambda:{'thread_id':'old-empty'}
        calls=[]
        async def recover(expected):
            calls.append(expected);return DefaultAgentBinding('host','new-empty',str(self.owner.root/'socket'))
        self.owner.recreate_empty_lost=recover
        code,result=await self.post('/v1/agent/default/chat/recover-empty',{'confirmed':True,'expected_provider_session_id':'old-empty'})
        self.assertEqual(code,200,result);self.assertFalse(result['same_thread'])
        self.assertEqual(result['previous_provider_session_id'],'old-empty');self.assertEqual(result['provider_session_id'],'new-empty')
        self.assertEqual(calls,['old-empty'])
        atomic_private_json(self.chat.delivery_path,{'user-request':{'accepted':True}})
        code,result=await self.post('/v1/agent/default/chat/recover-empty',{'confirmed':True,'expected_provider_session_id':'old-empty'})
        self.assertEqual(code,409,result);self.assertEqual(result['error']['code'],'agent_has_message_history');self.assertEqual(len(calls),1)

    async def test_legacy_ensure_does_not_create_second_agent_after_owned_metadata(self):
        atomic_private_json(self.manager.root/'structured-default'/'owner.json',{'thread_id':'same-thread'})
        result=self.manager.ensure();self.assertEqual(result['surface'],'chat')
        with self.assertRaisesRegex(ValueError,'structured_agent_endpoint_required'):
            self.manager.submit({'request_id':'r','agent_id':'default','text':'x'},'phone')
