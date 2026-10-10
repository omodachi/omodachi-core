"""Structured default-agent control using its existing lifecycle manager."""
from __future__ import annotations
import asyncio
from contextlib import suppress
import json
import re
from .agent import ProbeStatus
from .agent_lifecycle import atomic_private_json,read_private_json
from .agent_chat_provider import AgentChatError,CodexDefaultAgentChat
from .agent_chat_owner import OwnedCodexAgent
from .service import ServiceError,fields,identifier

# AGENT-PLAN-1 (Leo, 2026-10-07: "甚至是 / 的优化整个都可以砍掉 只透传" and "如果
# 基础 plan compact review diff 都记不了的话 别用了"): no slash list on the phone;
# what the person types goes to codex as typed - a slash command too. Four
# basics cannot work as words, because each needs the app-server (or core's
# own record) rather than the model: `/plan` (codex's Plan collaboration mode;
# the header chip shows it), `/compact`, `/review [words]` and `/diff`. Core
# handles those quietly, here, in the message route.
QUIET_COMMANDS=('plan','compact','review','diff')

def quiet_command(text):
    """`(name, words)` when `text` is one of QUIET_COMMANDS - the command word
    alone or followed by whitespace - else None. `/compact` and `/diff` take
    no words; with words they are the person's words."""
    if not isinstance(text,str):return None
    match=re.fullmatch(r'/([a-z]+)(?:\s+(.*))?',text.strip(),re.S)
    if match is None or match.group(1) not in QUIET_COMMANDS:return None
    name,words=match.group(1),(match.group(2) or '').strip()
    if name in ('compact','diff') and words:return None
    return name,words

def journal(entry:dict)->None:
    """One JSON line on the daemon's journal: who started a new conversation,
    from which thread to which, and how it ended - never any words."""
    try:print(json.dumps({'omodachi':'agent',**entry},separators=(',',':'),default=str),flush=True)
    except (OSError,ValueError):pass

class DefaultAgentChatService:
    def __init__(self,manager,host_id,*,owner=None,connector=None,hub=None):
        self.manager=manager;self.host_id=host_id;self.hub=hub
        self.root=manager.root/'structured-default'
        self.owner=owner or OwnedCodexAgent(self.root,manager.cwd,host_id)
        self.connector=connector or CodexDefaultAgentChat.connect_existing
        self._uses_owned_rpc=connector is None
        self.chat=None;self.lock=asyncio.Lock();self.event_task=None
        self.uploads=None  # ATTACH-1: the daemon's UploadStore
        self.subscribers=set();self.sequence=0
        self.switching=False  # AGENT-NEW-1: a new conversation is being made
        self._quiet={}  # AGENT-PLAN-1: request_id -> the answer a quiet command gave
        # AGENT-QUEUE-1: the answers core gave, kept across re-attaches to the
        # same thread (`CodexDefaultAgentChat._answered`).
        self._answered={};self._answered_thread=None
        self.delivery_path=self.root/'delivery.json';self.sequence_path=self.root/'sequence.json'
        # AGENT-QUEUE-1: codex's record holds no plan updates; core keeps the
        # newest checklist of its thread so a restart does not lose it.
        self.checklist_path=self.root/'checklist.json'
        manager.structured_chat=self
        from .agent_handoff import DefaultAgentHandoff
        self.handoff=DefaultAgentHandoff(self)
        from .agent_commands import AgentCommands
        self.commands=AgentCommands(self)

    def _state(self,path,default):
        try:return read_private_json(path)
        except FileNotFoundError:return default

    async def ensure(self,authorize):
        async with self.lock:
            authorize()
            if self.chat is not None and self.chat.attached:
                snapshot=await self.chat.snapshot()
                return self.result(snapshot)
            previous=None
            if self.chat is not None:
                # AGENT-FIX-2: what the phones were already showing for this
                # thread is joined with codex's record when it is read again.
                previous=(self.chat.binding.thread_id,dict(getattr(self.chat,'rows',{}) or {}))
                await self.chat.close();self.chat=None
                await self.owner.close_proxy()
            record=self.owner.owned_metadata()
            cap,_=await asyncio.to_thread(self.manager.probe.inspect)
            if not cap.configured:raise ServiceError('agent_kind_unset',status=409)
            if cap.configured_kind!='codex':raise ServiceError('structured_agent_kind_unsupported',status=409)
            # Do not spawn a TUI first and then create an unrelated chat default.
            if record is None and not cap.default_agent_exists and cap.default_agent_probe!=ProbeStatus.MISSING:
                raise ServiceError('agent_state_unknown',status=409)
            try:
                binding=await self.owner.ensure_new_or_owned(existing_agent=cap.default_agent_exists)
                authorize()
                rows=previous[1] if previous and previous[0]==binding.thread_id else None
                snapshot=await self._attach(binding,previous_rows=rows)
                return self.result(snapshot)
            except AgentChatError as error:
                if self._uses_owned_rpc:await self.owner.close_proxy()
                raise ServiceError(error.code,status=409) from None

    async def _attach(self,binding,*,sequence=None,previous_rows=None):
        delivery=self._state(self.delivery_path,{})
        if sequence is None:sequence=self._state(self.sequence_path,{'sequence':0})['sequence']
        connection_options={'owner_rpc':self.owner.rpc} if self._uses_owned_rpc else {}
        if previous_rows:connection_options['previous_rows']=previous_rows
        if self._answered_thread!=binding.thread_id:
            self._answered={};self._answered_thread=binding.thread_id
        connection_options['answered']=self._answered
        self.chat=await self.connector(binding,**connection_options,delivery_journal=delivery,
            persist_delivery=lambda value:atomic_private_json(self.delivery_path,value),initial_sequence=sequence)
        started=getattr(self.owner,'started_settings',None)
        if started and hasattr(self.chat,'seed_settings'):
            self.chat.seed_settings(started.get('model'),started.get('effort'))
        kept=self._state(self.checklist_path,{})
        if isinstance(kept,dict) and kept.get('thread_id')==binding.thread_id and hasattr(self.chat,'seed_checklist'):
            self.chat.seed_checklist(kept.get('checklist'))
        self.owner.started_settings=None
        self.event_task=asyncio.create_task(self._events())
        snapshot=await self.chat.snapshot()
        self._publish_agent_state()
        return snapshot

    async def new_conversation(self,payload,authorize):
        """AGENT-NEW-1: `POST /v1/agent/default/chat:new`.

        A fresh default conversation, asked for in so many words
        (`confirmed: true`) and for the conversation the client is looking at
        (`expected_provider_session_id`), so two phones pressing at once make
        one. Refused while that conversation has a turn running
        (`agent_turn_active`) or a question waiting for the person
        (`agent_approval_pending`): starting over would leave either hanging.
        The old thread is kept by codex; only core's record moves on.
        """
        fields(payload,('confirmed','expected_provider_session_id'))
        if payload['confirmed'] is not True:raise ServiceError('new_conversation_confirmation_required',status=409)
        expected=identifier(payload['expected_provider_session_id'])
        async with self.lock:
            authorize()
            record=self.owner.owned_metadata()
            current=(record or {}).get('thread_id') or (record or {}).get('previous_thread_id')
            if not record or current!=expected:raise ServiceError('agent_identity_changed',status=409)
            if self.chat is not None and not self.chat.attached:
                # The provider connection behind it is gone: start from a
                # fresh one, as ensure does.
                await self._retire_chat()
                if self._uses_owned_rpc:await self.owner.close_proxy()
            chat=self.chat if self.chat is not None and self.chat.attached else None
            send_lock=getattr(chat,'_send_lock',None) or asyncio.Lock()
            # A message already on its way to the old thread finishes first,
            # and none can start until the switch is over.
            async with send_lock:
                if chat is not None:
                    if chat.pending_approvals:raise ServiceError('agent_approval_pending',status=409)
                    if chat.active_turn is not None:raise ServiceError('agent_turn_active',status=409)
                self.switching=True
                try:
                    try:binding=await self.owner.start_new(expected)
                    except AgentChatError as error:raise ServiceError(error.code,status=409) from None
                    sequence=max(getattr(self.chat,'sequence',0) or 0,
                                  self._state(self.sequence_path,{'sequence':0})['sequence'])
                    await self._retire_chat()
                    # Request ids belong to the conversation they were sent to.
                    atomic_private_json(self.delivery_path,{})
                    try:snapshot=await self._attach(binding,sequence=sequence)
                    except AgentChatError as error:
                        # The new thread exists and is recorded; the next
                        # ensure attaches it. Say what happened, not more.
                        self.chat=None
                        raise ServiceError(error.code,status=409) from None
                finally:
                    self.switching=False
            # Everyone watching the old conversation reads the host again
            # and finds the new one.
            for queue in tuple(self.subscribers):
                while not queue.empty():queue.get_nowait()
                queue.put_nowait({'type':'resync_required'})
            return {'agent_id':'default','status':'new_conversation_started',
                    'previous_provider_session_id':expected,'provider_session_id':binding.thread_id,
                    'same_thread':False,'previous_kept':True,'identity':snapshot['identity'],'snapshot':snapshot}

    async def _retire_chat(self):
        """Stop following the old conversation. The owner's connection to the
        app-server stays open: the new thread lives in it until its first
        message gives it a rollout on disk."""
        if self.event_task:
            self.event_task.cancel()
            with suppress(asyncio.CancelledError):await self.event_task
            self.event_task=None
        chat,self.chat=self.chat,None
        if chat is None:return
        if self._uses_owned_rpc:
            chat.attached=False
            for task in tuple(getattr(chat,'_answers',())):task.cancel()
        else:
            await chat.close()

    async def recover_empty(self,payload,authorize):
        fields(payload,('confirmed','expected_provider_session_id'))
        if payload['confirmed'] is not True:raise ServiceError('empty_recovery_confirmation_required',status=409)
        expected=identifier(payload['expected_provider_session_id'])
        async with self.lock:
            authorize();record=self.owner.owned_metadata()
            if not record or record.get('thread_id')!=expected:raise ServiceError('agent_identity_changed',status=409)
            if self._state(self.delivery_path,{}):raise ServiceError('agent_has_message_history',status=409)
            if self.chat is not None and self.chat.attached:raise ServiceError('agent_session_available',status=409)
            try:
                # The owner verifies that this exact thread is unavailable in
                # the provider before archiving it and creating the replacement.
                result=await self.owner.recreate_empty_lost(expected)
            except AgentChatError as error:raise ServiceError(error.code,status=409) from None
            self.chat=None
            return {'status':'empty_session_recreated','previous_provider_session_id':expected,
                'provider_session_id':result.thread_id,'same_thread':False,'user_messages_preserved':False,
                'reason':'unmaterialized_empty_session_lost_after_host_restart'}

    def status_override(self):
        """What the provider itself says, for the badge the phone renders.

        Herdr's pane heuristic cannot see an approval prompt; codex's own
        `thread/status/changed` can, so it wins whenever it is waiting.
        """
        chat=self.chat
        if chat is None or not chat.attached:return None
        waiting=chat.status_value().get('waiting')
        return {'approval':'waiting_on_approval','user_input':'waiting_on_user_input'}.get(waiting)

    def _publish_agent_state(self):
        if self.hub is None or self.chat is None:return
        activity=getattr(self.chat,'activity_value',None)
        chat={'status':self.chat.status_value(),'pending_approvals':len(self.chat.pending_approvals)}
        # NOTIFY-1: the oldest pending approval's ids, so an alert can offer
        # Allow / Deny on exactly that request (never what it asks).
        head=getattr(self.chat,'approval_head',None)
        if head is not None:chat['approval']=head()
        # MOTION-1: the face needs thinking / working / tool / done / error,
        # which only the provider's own turn and item events can tell apart.
        if activity is not None:chat['activity']=activity();self._published_activity=chat['activity']
        patch={'chat':chat,
               'usage':self.chat.usage_value()}
        override=self.status_override()
        if override:patch['status']=override
        self.hub.update_state({'agent':patch},event_type='agent.changed')

    async def approvals(self,authorize):
        chat=await self.require(authorize)
        return {'agent_id':'default','requests':chat.approvals_value()}

    async def approve(self,request_id,payload,authorize):
        request_id=identifier(request_id)
        fields(payload,(),('decision','input','fingerprint','reason'))
        if ('decision' in payload)==('input' in payload):raise ServiceError('invalid_request')
        # NOTIFY-1: a decision from an alert names what the alert showed.
        fingerprint=payload.get('fingerprint')
        if fingerprint is not None and (not isinstance(fingerprint,str) or not re.fullmatch(r'[0-9a-f]{16}',fingerprint)):
            raise ServiceError('invalid_request')
        chat=await self.require(authorize)
        try:
            extra={'fingerprint':fingerprint} if fingerprint is not None else {}
            # AGENT-QUEUE-1: a decline's optional one-line reason.
            if payload.get('reason') is not None:extra['reason']=payload['reason']
            result=await chat.resolve_approval(request_id,decision=payload.get('decision'),
                                               answers=payload.get('input'),**extra)
        except AgentChatError as error:raise ServiceError(error.code,status=409) from None
        self._publish_agent_state()
        return result

    async def models(self,authorize):
        chat=await self.require(authorize)
        try:return await chat.models()
        except AgentChatError as error:raise ServiceError(error.code,status=409) from None

    async def usage(self,authorize):
        chat=await self.require(authorize)
        return {'agent_id':'default','usage':chat.usage_value(),'status':chat.status_value()}

    async def set_mode(self,payload,authorize):
        """AGENT-PLAN-1: `POST /v1/agent/default/chat/mode {"mode": "plan" |
        "default"}` - the header's Plan chip. The thread's own collaboration
        mode, changed in codex (`thread/settings/update`) for the turns that
        follow; refused while a turn runs (`agent_busy`), as codex's TUI does."""
        fields(payload,('mode',))
        if payload['mode'] not in ('plan','default'):raise ServiceError('invalid_agent_mode')
        chat=await self.require(authorize)
        try:result=await chat.set_mode(payload['mode'])
        except AgentChatError as error:raise ServiceError(error.code,status=409) from None
        self._publish_agent_state()
        return {'agent_id':'default',**result,'usage':chat.usage_value()}

    async def steer(self,payload,authorize):
        fields(payload,('request_id','text'))
        request_id=identifier(payload['request_id']);text=payload['text']
        if not isinstance(text,str) or not text.strip() or '\0' in text or len(text.encode())>60000:
            raise ServiceError('invalid_task')
        # AGENT-PLAN-1: the quiet commands wait for the turn to end, as in
        # codex's TUI; they are never words added to the running turn.
        if quiet_command(text) is not None:
            await self.require(authorize)
            raise ServiceError('agent_command_while_working',status=409)
        try:return await (await self.require(authorize)).steer(text,request_id)
        except AgentChatError as error:raise ServiceError(error.code,status=409) from None

    # --- AGENT-QUEUE-1: the queue, history a page at a time --------------------
    async def queue_add(self,payload,authorize):
        """`POST /v1/agent/default/chat/queue {request_id, text}` - the
        composer's Send while a turn runs. codex keeps the message and starts
        it when the turn ends; `queue` in the answer is the queue now."""
        fields(payload,('request_id','text'))
        request_id=identifier(payload['request_id']);text=payload['text']
        if not isinstance(text,str) or not text.strip() or '\0' in text or len(text.encode())>60000:
            raise ServiceError('invalid_task')
        # The quiet commands are not words for the model, and wait for the
        # turn to end as they do on the steer route.
        if quiet_command(text) is not None:
            await self.require(authorize)
            raise ServiceError('agent_command_while_working',status=409)
        chat=await self.require(authorize)
        try:return {'agent_id':'default',**await chat.enqueue(text,request_id)}
        except AgentChatError as error:raise ServiceError(error.code,status=409) from None

    async def queue_item(self,queue_id,action,payload,authorize):
        """`POST /v1/agent/default/chat/queue/{id}:send | :edit | :delete`."""
        queue_id=identifier(queue_id)
        if action=='edit':
            fields(payload,('text',))
            text=payload['text']
            if not isinstance(text,str) or not text.strip() or '\0' in text or len(text.encode())>60000:
                raise ServiceError('invalid_task')
            if quiet_command(text) is not None:raise ServiceError('agent_command_while_working',status=409)
        else:
            fields(payload,())
        chat=await self.require(authorize)
        try:
            if action=='send':result=await chat.send_queued_now(queue_id)
            elif action=='edit':result=await chat.edit_queued(queue_id,payload['text'])
            else:result=await chat.delete_queued(queue_id)
        except AgentChatError as error:raise ServiceError(error.code,status=409) from None
        return {'agent_id':'default',**result}

    async def history(self,before,limit,authorize):
        """`GET /v1/agent/default/chat/history?before=<row id>&limit=<n>`."""
        if before is not None:before=identifier(before)
        try:limit=int(limit) if limit is not None else 100
        except (TypeError,ValueError):raise ServiceError('invalid_request') from None
        if not 1<=limit<=200:raise ServiceError('invalid_request')
        chat=await self.require(authorize)
        try:return {'agent_id':'default',**await chat.history(before,limit)}
        except AgentChatError as error:raise ServiceError(error.code,status=409) from None

    def result(self,snapshot):
        return {'agent_id':'default','surface':'chat','status':'working' if snapshot.get('activeTurn') else 'idle',
                'ready_to_attach':True,'herdr_pane_registered':False,'identity':snapshot['identity'],'snapshot':snapshot,
                'route':{'route':'native','supported':True,'native_view':'agent-chat'},
                'terminal_attach_argv':self.owner.tui_argv(self.chat.binding)}

    async def require(self,authorize):
        authorize()
        if getattr(self,'switching',False):raise ServiceError('agent_conversation_changing',status=409)
        if self.chat is None or not self.chat.attached:raise ServiceError('agent_chat_not_ready',status=409)
        return self.chat

    async def snapshot(self,authorize):return await (await self.require(authorize)).snapshot()

    async def send(self,payload,authorize):
        fields(payload,('request_id','text'),('model','effort','attachments'))
        request_id=identifier(payload['request_id']);text=payload['text']
        # AGENT-PLAN-1: a slash command is no longer refused here; it is the
        # person's words, except the two quiet commands (`quiet_command`).
        images=()
        if payload.get('attachments'):
            # ATTACH-1: an image goes to codex as its own image input; a text
            # file or a PDF as its path in the words (`uploads.compose_agent`).
            from .uploads import attachment_refs,compose_agent
            refs=attachment_refs(payload['attachments'])
            store=getattr(self,'uploads',None)
            if store is None:raise ServiceError('upload_unavailable',status=503)
            if not isinstance(text,str):raise ServiceError('invalid_task')
            text,images=compose_agent(text,[(marker,store.resolve(upload_id)) for upload_id,marker in refs])
        if not isinstance(text,str) or not text.strip() or '\0' in text or len(text.encode())>60000:raise ServiceError('invalid_task')
        # Model and effort are per-turn overrides the provider advertises in
        # model/list; the boundary only checks their shape, never a whitelist
        # of its own that would go stale the day a model ships.
        model,effort=payload.get('model'),payload.get('effort')
        for value in (model,effort):
            if value is not None and (not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',value)):
                raise ServiceError('invalid_request')
        chat=await self.require(authorize)
        quiet=quiet_command(text)
        try:
            if quiet is not None:return await self._quiet_command(chat,quiet,request_id,model,effort,tuple(images))
            if images:return await chat.send(text,request_id,model=model,effort=effort,images=tuple(images))
            return await chat.send(text,request_id,model=model,effort=effort)
        except AgentChatError as error:raise ServiceError(error.code,status=409) from None

    async def _quiet_command(self,chat,quiet,request_id,model,effort,images):
        """AGENT-PLAN-1. `/plan` turns codex's Plan mode on, or off when it is
        on; `/plan <words>` turns it on and sends the words in the same turn
        (codex's TUI does both). `/compact` is `thread/compact/start`;
        `/review [words]` is `review/start` (a turn of this conversation; a
        codex that refuses it gets the words as typed); `/diff` answers with
        the conversation's file changes (`changes`). The answer says which
        (`command`) and, with no turn of its own,
        `delivery: "command"`, so a client never waits for a turn that is not
        coming. The same request id answers the same way again: a retried
        `/plan` must not switch back."""
        name,words=quiet
        if request_id in self._quiet:return dict(self._quiet[request_id])
        if name=='plan' and words:
            options={'model':model,'effort':effort,'mode':'plan'}
            if images:options['images']=images
            result={**await chat.send(words,request_id,**options),'command':'plan','mode':'plan'}
        elif name=='plan':
            target='default' if getattr(chat,'mode',None)=='plan' else 'plan'
            await chat.set_mode(target)
            result={'accepted':True,'delivery':'command','command':'plan','mode':target}
            self._publish_agent_state()
        elif name=='review':
            try:result={**await chat.review(words),'command':'review'}
            except AgentChatError as error:
                if error.code!='provider_request_rejected':raise
                # A codex that will not review here: the words go as typed.
                text='/review'+(' '+words if words else '')
                result={**await chat.send(text,request_id,model=model,effort=effort),'command':None}
                result.pop('command')
        elif name=='diff':
            changes,omitted=chat.conversation_changes()
            result={'accepted':True,'delivery':'command','command':'diff','changes':changes}
            if omitted:result['changes_omitted']=omitted
        else:
            await chat.compact()
            result={'accepted':True,'delivery':'command','command':'compact'}
        self._quiet[request_id]=dict(result)
        while len(self._quiet)>64:self._quiet.pop(next(iter(self._quiet)))
        return result

    async def cancel(self,payload,authorize):
        fields(payload,('turn_id',));turn_id=identifier(payload['turn_id'])
        try:return await (await self.require(authorize)).cancel(turn_id)
        except AgentChatError as error:raise ServiceError(error.code,status=409) from None

    async def _events(self):
        while True:
            event=await self.chat.events.get()
            atomic_private_json(self.sequence_path,{'sequence':event['sequence']})
            if event['event']['type']=='agent.checklist.updated':
                with suppress(OSError):
                    atomic_private_json(self.checklist_path,{'thread_id':getattr(getattr(self.chat,'binding',None),'thread_id',None),
                                                             'checklist':event['event'].get('checklist')})
            if event['event']['type'] in {'agent.status.changed','agent.usage.updated',
                                          'agent.approval.requested','agent.approval.resolved'}:
                self._publish_agent_state()
            elif hasattr(self.chat,'activity_value') and \
                    self.chat.activity_value()!=getattr(self,'_published_activity',None):
                # MOTION-1: only a change of phase is news; a streamed delta
                # that leaves the phase alone publishes nothing.
                self._publish_agent_state()
            for queue in tuple(self.subscribers):
                if queue.full():
                    while not queue.empty():queue.get_nowait()
                    queue.put_nowait({'type':'resync_required'})
                else:queue.put_nowait(event)
            if event['event']['type']=='connectionLost':return

    async def close(self):
        if self.event_task:
            self.event_task.cancel()
            with suppress(asyncio.CancelledError):await self.event_task
        if self.chat:await self.chat.close();self.chat=None
        await self.owner.close_proxy()  # retain the manager-owned default daemon
