"""Official Codex slash command semantics, separate from ordinary chat turns.

AGENT-NEW-1 (Leo, /model showed raw JSON): every command in the catalog does
one of three things, and none of them is "print the provider's answer":

* **runs** here and answers with `display` - `{key, args}`, a sentence key the
  App and Odomo say in the reader's language from scalar arguments - plus
  `result.summary`, the same sentence in plain English for an older client;
* **opens the right native thing** - `native_request {action, ...}`: the model
  picker (`/model`), the New conversation confirmation (`/new`, `/clear`), the
  clipboard (`/copy`, `/export`);
* or is **not available on the phone**, with a `reason_code` (one of
  `REASONS`) the clients turn into one warm sentence, and `reason` in English.

`summary` and every `display` argument are plain text or numbers, never a
provider structure (`tests/test_agent_commands_display.py` fails otherwise).
"""
import hashlib
import json
from pathlib import Path
from .service import ServiceError,fields,identifier
from .agent_chat_provider import AgentChatError

# Why a command stays on the computer, as the sentence the clients say.
REASONS = {
    'command_terminal_only': "This one is for Codex in a terminal on the computer, so the phone leaves it there.",
    'command_settings_on_computer': "This changes how Codex is set up, which stays on the computer. Open Codex there to change it.",
    'command_sessions_on_computer': "This one juggles several conversations; the phone keeps one. New conversation starts a fresh one.",
    'command_not_on_phone_yet': "This one isn't on the phone yet. You can ask the agent for it in plain words.",
}
GROUPS = {
    'command_terminal_only': {'ide', 'keymap', 'vim', 'raw', 'title', 'statusline', 'theme', 'pets', 'mention',
                              'diff', 'ps', 'stop', 'rollout', 'debug-config', 'quit', 'exit'},
    'command_settings_on_computer': {'permissions', 'setup-default-sandbox', 'sandbox-add-read-dir', 'experimental',
                                     'memories', 'hooks', 'import', 'apps', 'plugins', 'logout', 'personality', 'cd'},
    'command_sessions_on_computer': {'archive', 'delete', 'resume', 'fork', 'worktree', 'app', 'agents', 'side',
                                     'btw', 'subagents'},
}
# Developer switches codex itself marks "DO NOT USE": not listed at all.
HIDDEN = {'debug-m-drop', 'debug-m-update', 'test-approval'}
# id -> (execution, argument hint, turn policy)
ENABLED = {'status': ('rpc', '', 'allowed'), 'model': ('native', '[model-id] [reasoning-effort]', 'allowed'),
           'skills': ('rpc', '', 'allowed'), 'mcp': ('rpc', '[verbose]', 'allowed'),
           'usage': ('rpc', '', 'allowed'), 'rename': ('rpc', '<name>', 'allowed'),
           'compact': ('rpc', '', 'idle_required'), 'pwd': ('rpc', '', 'allowed'),
           'copy': ('native', '', 'allowed'), 'export': ('native', '', 'idle_required'),
           # AGENT-NEW-1: the phone's "New conversation", with its confirmation
           'new': ('native', '', 'idle_required'), 'clear': ('native', '', 'idle_required'),
           # AGENT-PLAN-1: codex's Plan collaboration mode. Like the TUI's, not
           # while a turn runs; `/plan <words>` switches and sends them.
           'plan': ('rpc', '[prompt]', 'idle_required')}
COVERAGE = {'model': 'native_picker', 'skills': 'list_only', 'mcp': 'list_only', 'usage': 'list_only'}
MAX_NAMES = 40


def reason_code(command_id):
    return next((code for code, ids in GROUPS.items() if command_id in ids), 'command_not_on_phone_yet')


def _names(values):
    return [value[:120] for value in values if isinstance(value, str) and value][:MAX_NAMES]


def _listing(names):
    return ', '.join(names) if names else ''


class AgentCommands:
    def __init__(self,chat):
        self.chat=chat
        self.document=json.loads((Path(__file__).parent/'data/codex-slash-0.154.json').read_text())
        self.rows=[dict(r) for r in self.document['commands'] if r['id'] not in HIDDEN];self.results={}
        for r in self.rows:
            if r['id'] in ENABLED:
                execution,hint,policy=ENABLED[r['id']]
                r.update(execution=execution,argument_hint=hint,turn_policy=policy,available=True,reason=None,
                         reason_code=None)
            else:
                code=reason_code(r['id'])
                r.update(reason_code=code,reason=REASONS[code])
            r['coverage']=COVERAGE.get(r['id']) or ('native_callback' if r['execution']=='native'
                                                    else 'implemented' if r['available'] else 'not_implemented')
        self.revision=hashlib.sha256(json.dumps(self.rows,sort_keys=True).encode()).hexdigest()[:16]

    def listing(self):
        return {'revision':self.revision,'provider':'codex','provider_version':self.document['provider_version'],'commands':self.rows}

    async def _models(self,chat):
        models=[];cursor=None
        while True:
            page=await chat.rpc.request('model/list',{'cursor':cursor} if cursor else {})
            models.extend(m for m in page.get('data',[]) if isinstance(m,dict));cursor=page.get('nextCursor')
            if not cursor:break
            if len(models)>2048:raise ServiceError('model_catalog_unavailable',status=503)
        return models

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
        if name not in {'rename','mcp','model','plan'} and args.strip():raise ServiceError('command_arguments_unsupported',status=400)
        if name=='mcp' and args.strip() not in {'','verbose'}:raise ServiceError('invalid_command_arguments')
        try:
            result=await self._run(name,args.strip(),chat,request_id)
        except AgentChatError as error:raise ServiceError(error.code,status=409) from None
        result.update(command_id=name,request_id=request_id)
        self.results[key]=(fingerprint,result)
        while len(self.results)>256:self.results.pop(next(iter(self.results)))
        return result

    async def _run(self,name,args,chat,request_id=None):
        thread=chat.binding.thread_id
        cwd=chat.binding.cwd or str(self.chat.manager.cwd)
        def answer(key,summary,status='completed',**values):
            return {'status':status,'result':{**values,'summary':summary},'display':{'key':key,'args':values}}
        if name=='pwd':
            return answer('pwd','The agent works in '+cwd+'.',cwd=cwd)
        if name=='status':
            value=await chat.rpc.request('thread/read',{'threadId':thread,'includeTurns':False})
            t=value.get('thread') or {}
            kind=(t.get('status') or {}).get('type') if isinstance(t.get('status'),dict) else None
            state='working' if chat.active_turn else {'active':'working','systemError':'error'}.get(kind,'idle')
            where=t.get('cwd') if isinstance(t.get('cwd'),str) else cwd
            title=t.get('name') if isinstance(t.get('name'),str) and t.get('name') else None
            model=chat.settings.get('model') if isinstance(getattr(chat,'settings',None),dict) else None
            words={'working':'Working','error':'Stopped by an error','idle':'Idle'}[state]
            summary=(title+': ' if title else '')+words+' in '+where+(' with '+model if model else '')+'.'
            return answer('status',summary,state=state,cwd=where,name=title,model=model)
        if name=='model':
            if not args:
                return {'status':'native_action_required','native_request':{'action':'choose_model','arguments':''},
                        'result':{'summary':'Pick a model and effort with the Model control at the top.'},
                        'display':{'key':'model.choose','args':{}}}
            parts=args.split()
            if len(parts) not in {1,2}:raise ServiceError('invalid_model_arguments')
            models=await self._models(chat)
            selected=next((m for m in models if parts[0] in {m.get('id'),m.get('model')}),None)
            if selected is None:raise ServiceError('model_unavailable',status=409)
            effort=None
            if len(parts)==2:
                efforts={r.get('reasoningEffort') for r in selected.get('supportedReasoningEfforts',[]) if isinstance(r,dict)}
                if parts[1] not in efforts:raise ServiceError('reasoning_effort_unavailable',status=409)
                effort=parts[1]
            # codex has no model setter (`thread/settings/update` is behind its
            # experimental API): the choice rides the next turn, which is what
            # the phone's picker does. So the phone makes it, preselected.
            model_id=selected.get('id')
            label=selected.get('displayName') or model_id
            return {'status':'native_action_required',
                    'native_request':{'action':'choose_model','arguments':model_id+(' '+effort if effort else ''),
                                      'model':model_id,'effort':effort},
                    'result':{'model':model_id,'effort':effort,'applies_to':'next_turn',
                              'summary':'The next message uses '+label+(' · '+effort if effort else '')+'.'},
                    'display':{'key':'model.selected','args':{'model':label,'effort':effort}}}
        if name=='skills':
            value=await chat.rpc.request('skills/list',{'cwds':[cwd]})
            names=sorted({skill.get('name') for entry in value.get('data',[]) if isinstance(entry,dict)
                          for skill in entry.get('skills',[]) if isinstance(skill,dict) and skill.get('enabled',True)
                          and isinstance(skill.get('name'),str)})
            names=_names(names)
            return answer('skills',('Skills: '+_listing(names)+'.') if names else 'No skills are set up for this folder.',
                          names=names,count=len(names))
        if name=='mcp':
            value=await chat.rpc.request('mcpServerStatus/list',{})
            rows=[row for row in value.get('data',[]) if isinstance(row,dict) and isinstance(row.get('name'),str)]
            rows=rows[:MAX_NAMES]
            names=_names([row['name'] for row in rows])
            tools=[len(row.get('tools') or {}) if isinstance(row.get('tools'),dict) else 0 for row in rows]
            verbose=args=='verbose'
            shown=[n+' ('+str(t)+(' tool' if t==1 else ' tools')+')' for n,t in zip(names,tools)] if verbose else names
            return answer('mcp',('MCP servers: '+_listing(shown)+'.') if names else 'No MCP servers are set up.',
                          names=names,count=len(names),tools=tools if verbose else None)
        if name=='usage':
            value=await chat.rpc.request('account/rateLimits/read',{})
            limits=value.get('rateLimits') if isinstance(value.get('rateLimits'),dict) else {}
            windows=[]
            for part in ('primary','secondary'):
                window=limits.get(part)
                if isinstance(window,dict) and isinstance(window.get('usedPercent'),(int,float)):
                    windows.append({'minutes':window.get('windowDurationMins') if type(window.get('windowDurationMins')) is int else None,
                                    'used_percent':round(float(window['usedPercent']))})
            def label(minutes):
                return {300:'5h',10080:'weekly'}.get(minutes) or ('quota' if minutes is None else
                       str(minutes//1440)+'d' if minutes>=1440 else str(minutes//60)+'h' if minutes>=60 else str(minutes)+'m')
            summary=(' · '.join(label(w['minutes'])+' '+str(w['used_percent'])+'% used' for w in windows)+'.') if windows \
                else 'The account has not reported its usage yet.'
            return answer('usage',summary,windows=windows)
        if name=='rename':
            if not args:raise ServiceError('command_argument_required',status=400)
            await chat.rpc.request('thread/name/set',{'threadId':thread,'name':args})
            return answer('rename','Renamed this conversation to '+args+'.',name=args)
        if name=='compact':
            await chat.rpc.request('thread/compact/start',{'threadId':thread})
            return answer('compact','Compacting the conversation; the agent says when it is done.',status='accepted')
        if name=='plan':
            # AGENT-PLAN-1. `/plan` turns Plan mode on, or off again when it is
            # on (the phone's way back, beside the chip); `/plan <words>` turns
            # it on and sends the words in the same turn, as codex's TUI does.
            if args:
                await chat.send(args,request_id or 'plan-command',mode='plan')
                return answer('plan.sent','Plan mode is on, and your message is on its way to the agent.',
                              status='accepted',mode='plan')
            target='default' if getattr(chat,'mode',None)=='plan' else 'plan'
            await chat.set_mode(target)
            if target=='plan':
                return answer('plan.on','Plan mode is on. The agent works out a plan with you and changes nothing '
                              'until you say so.',mode='plan')
            return answer('plan.off','Plan mode is off. The agent goes back to doing the work.',mode='default')
        if name in {'new','clear'}:
            return {'status':'native_action_required','native_request':{'action':'new_conversation','arguments':''},
                    'result':{'summary':'Start a new conversation with New conversation at the top of the panel.'},
                    'display':{'key':'new','args':{}}}
        # copy / export: the words go to the phone's own clipboard.
        snapshot=await chat.snapshot()
        if name=='copy':
            last=next((r for r in reversed(snapshot['rows']) if r.get('kind')=='message' and r.get('role')=='assistant'),None)
            if last is None:raise ServiceError('no_assistant_response',status=409)
            text=last['text']
            summary='Copied the last reply.'
        else:
            text='\n\n'.join('## '+('You' if r.get('role')=='user' else 'Agent' if r.get('kind')=='message' else
                                    r.get('name') or 'Tool')+'\n\n'+(r.get('text') if r.get('kind')=='message' else r.get('detail') or '')
                             for r in snapshot['rows'])
            summary='Copied the conversation as Markdown.'
        return {'status':'native_action_required',
                'native_request':{'action':'copy_last_response' if name=='copy' else 'export_conversation',
                                  'arguments':'','text':text},
                'result':{'summary':summary},'display':{'key':name,'args':{}}}
