"""Official Codex slash command semantics, separate from ordinary chat turns."""
import hashlib
import json
from pathlib import Path
from .service import ServiceError,fields,identifier
from .agent_chat_provider import AgentChatError

class AgentCommands:
    def __init__(self,chat):
        self.chat=chat
        self.document=json.loads((Path(__file__).parent/'data/codex-slash-0.154.json').read_text())
        self.rows=self.document['commands'];self.results={}
        enabled={'status':('rpc','', 'allowed'),'model':('rpc','[model-id] [reasoning-effort]', 'allowed'),
                 'skills':('rpc','', 'allowed'),'mcp':('rpc','[verbose]', 'allowed'),
                 'usage':('rpc','', 'allowed'),'rename':('rpc','<name>', 'allowed'),
                 'compact':('rpc','', 'idle_required'),'pwd':('rpc','', 'allowed'),
                 'copy':('native','', 'allowed'),'export':('native','', 'idle_required')}
        for r in self.rows:
            if r['id'] in enabled:
                execution,hint,policy=enabled[r['id']]
                r.update(execution=execution,argument_hint=hint,turn_policy=policy,available=True,reason=None)
            if r['id'] in {'new','clear','delete','archive','fork','resume'}:
                r['reason']='需要原生会话选择或确认流程；不会自动替换当前对话'
        for r in self.rows:
            r['coverage']='list_and_select' if r['id']=='model' else 'list_only' if r['id'] in {'skills','mcp','usage'} else 'native_callback' if r['execution']=='native' else 'implemented' if r['available'] else 'not_implemented'
        self.revision=hashlib.sha256(json.dumps(self.rows,sort_keys=True).encode()).hexdigest()[:16]
    def listing(self):
        return {'revision':self.revision,'provider':'codex','provider_version':self.document['provider_version'],'commands':self.rows}
    async def execute(self,name,payload,authorize):
        fields(payload,('revision','arguments','request_id'))
        request_id=identifier(payload['request_id']);args=payload['arguments']
        if payload['revision']!=self.revision:raise ServiceError('stale_command_revision',status=409)
        if not isinstance(args,str) or '\0' in args or len(args)>8192:raise ServiceError('invalid_command_arguments')
        row=next((r for r in self.rows if r['id']==name),None)
        if row is None:raise ServiceError('unknown_agent_command',status=404)
        if not row['available']:raise ServiceError('agent_command_unsupported',status=409)
        chat=await self.chat.require(authorize)
        key=(chat.binding.thread_id,request_id);fingerprint=(name,args)
        if key in self.results:
            previous,result=self.results[key]
            if previous!=fingerprint:raise ServiceError('request_conflict',status=409)
            return result
        if row['turn_policy']=='idle_required' and chat.active_turn is not None:raise ServiceError('agent_busy',status=409)
        if name not in {'rename','mcp','model'} and args.strip():raise ServiceError('command_arguments_unsupported',status=400)
        if name=='mcp' and args.strip() not in {'','verbose'}:raise ServiceError('invalid_command_arguments')
        result={'status':'completed','command_id':name,'request_id':request_id,'result':{}}
        thread=chat.binding.thread_id
        try:
            if name=='status':
                value=await chat.rpc.request('thread/read',{'threadId':thread,'includeTurns':False})
                t=value.get('thread',{});result['result']={'thread_id':t.get('id'),'name':t.get('name'),'cwd':t.get('cwd'),'status':t.get('status'),'active_turn':chat.active_turn}
            elif name=='pwd':result['result']={'cwd':str(self.chat.manager.cwd)}
            elif name=='model':
                models=[];cursor=None
                while True:
                    page=await chat.rpc.request('model/list',{'cursor':cursor} if cursor else {})
                    models.extend(page.get('data',[]));cursor=page.get('nextCursor')
                    if not cursor:break
                    if len(models)>2048:raise ServiceError('model_catalog_unavailable',status=503)
                if not args.strip():result['result']={'data':models,'nextCursor':None}
                else:
                    parts=args.split()
                    if len(parts) not in {1,2}:raise ServiceError('invalid_model_arguments')
                    selected=next((m for m in models if parts[0] in {m.get('id'),m.get('model')}),None)
                    if selected is None:raise ServiceError('model_unavailable',status=409)
                    params={'threadId':thread,'model':selected['model']}
                    if len(parts)==2:
                        efforts={r.get('reasoningEffort') for r in selected.get('supportedReasoningEfforts',[])}
                        if parts[1] not in efforts:raise ServiceError('reasoning_effort_unavailable',status=409)
                        params['effort']=parts[1]
                    await chat.rpc.request('thread/settings/update',params)
                    result['result']={'model':params['model'],'effort':params.get('effort'),
                        'applies_to':'subsequent_turns','summary':'Model set to '+params['model']+(' · '+params['effort'] if 'effort' in params else '')+' for subsequent turns.'}
            elif name=='skills':result['result']=await chat.rpc.request('skills/list',{'cwds':[str(self.chat.manager.cwd)]})
            elif name=='mcp':result['result']=await chat.rpc.request('mcpServerStatus/list',{})
            elif name=='usage':result['result']=await chat.rpc.request('account/rateLimits/read',{})
            elif name=='rename':
                title=args.strip()
                if not title:raise ServiceError('command_argument_required',status=400)
                result['result']=await chat.rpc.request('thread/name/set',{'threadId':thread,'name':title})
                result['result']['name']=title
            elif name=='compact':
                await chat.rpc.request('thread/compact/start',{'threadId':thread})
                result['status']='accepted'
                result['result']={'started':True,'completed':False}
            elif name in {'copy','export'}:
                snapshot=await chat.snapshot()
                if name=='copy':
                    last=next((r for r in reversed(snapshot['rows']) if r.get('kind')=='message' and r.get('role')=='assistant'),None)
                    if last is None:raise ServiceError('no_assistant_response',status=409)
                    text=last['text']
                else:text='\n\n'.join('## '+r.get('role','tool')+'\n'+r.get('text',r.get('detail','')) for r in snapshot['rows'])
                result.update(status='native_action_required',native_request={'action':'copy_last_response' if name=='copy' else 'export_conversation','arguments':args,'text':text})
        except AgentChatError as error:raise ServiceError(error.code,status=409) from None
        if name=='status':
            value=result['result'];value['summary']=' · '.join(str(x) for x in (value.get('name'),value.get('cwd'),value.get('status')) if x is not None)
        elif name=='pwd':result['result']['summary']=str(self.chat.manager.cwd)
        elif name=='rename':result['result']['summary']='Renamed to '+result['result']['name']
        elif name=='compact':result['result']['summary']='Compaction started; completion will arrive from the provider.'
        else:result['result'].setdefault('summary',json.dumps(result['result'],ensure_ascii=False,indent=2))
        self.results[key]=(fingerprint,result)
        while len(self.results)>256:self.results.pop(next(iter(self.results)))
        return result
