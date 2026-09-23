"""Structured default-agent control using its existing lifecycle manager."""
from __future__ import annotations
import asyncio
from contextlib import suppress
import re
from .agent import ProbeStatus
from .agent_lifecycle import atomic_private_json,read_private_json
from .agent_chat_provider import AgentChatError,CodexDefaultAgentChat
from .agent_chat_owner import OwnedCodexAgent
from .service import ServiceError,fields,identifier

class DefaultAgentChatService:
    def __init__(self,manager,host_id,*,owner=None,connector=None,hub=None):
        self.manager=manager;self.host_id=host_id;self.hub=hub
        self.root=manager.root/'structured-default'
        self.owner=owner or OwnedCodexAgent(self.root,manager.cwd,host_id)
        self.connector=connector or CodexDefaultAgentChat.connect_existing
        self._uses_owned_rpc=connector is None
        self.chat=None;self.lock=asyncio.Lock();self.event_task=None
        self.subscribers=set();self.sequence=0
        self.delivery_path=self.root/'delivery.json';self.sequence_path=self.root/'sequence.json'
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
            if self.chat is not None:
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
                delivery=self._state(self.delivery_path,{})
                sequence=self._state(self.sequence_path,{'sequence':0})['sequence']
                connection_options={'owner_rpc':self.owner.rpc} if self._uses_owned_rpc else {}
                self.chat=await self.connector(binding,**connection_options,delivery_journal=delivery,
                    persist_delivery=lambda value:atomic_private_json(self.delivery_path,value),initial_sequence=sequence)
                self.event_task=asyncio.create_task(self._events())
                snapshot=await self.chat.snapshot()
                self._publish_agent_state()
                return self.result(snapshot)
            except AgentChatError as error:
                if self._uses_owned_rpc:await self.owner.close_proxy()
                raise ServiceError(error.code,status=409) from None

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
        patch={'chat':{'status':self.chat.status_value(),
                       'pending_approvals':len(self.chat.pending_approvals)},
               'usage':self.chat.usage_value()}
        override=self.status_override()
        if override:patch['status']=override
        self.hub.update_state({'agent':patch},event_type='agent.changed')

    async def approvals(self,authorize):
        chat=await self.require(authorize)
        return {'agent_id':'default','requests':chat.approvals_value()}

    async def approve(self,request_id,payload,authorize):
        request_id=identifier(request_id)
        fields(payload,(),('decision','input'))
        if ('decision' in payload)==('input' in payload):raise ServiceError('invalid_request')
        chat=await self.require(authorize)
        try:
            result=await chat.resolve_approval(request_id,decision=payload.get('decision'),
                                               answers=payload.get('input'))
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

    async def steer(self,payload,authorize):
        fields(payload,('request_id','text'))
        request_id=identifier(payload['request_id']);text=payload['text']
        if not isinstance(text,str) or not text.strip() or '\0' in text or len(text.encode())>60000:
            raise ServiceError('invalid_task')
        try:return await (await self.require(authorize)).steer(text,request_id)
        except AgentChatError as error:raise ServiceError(error.code,status=409) from None

    def result(self,snapshot):
        return {'agent_id':'default','surface':'chat','status':'working' if snapshot.get('activeTurn') else 'idle',
                'ready_to_attach':True,'herdr_pane_registered':False,'identity':snapshot['identity'],'snapshot':snapshot,
                'route':{'route':'native','supported':True,'native_view':'agent-chat'},
                'terminal_attach_argv':self.owner.tui_argv(self.chat.binding)}

    async def require(self,authorize):
        authorize()
        if self.chat is None or not self.chat.attached:raise ServiceError('agent_chat_not_ready',status=409)
        return self.chat

    async def snapshot(self,authorize):return await (await self.require(authorize)).snapshot()

    async def send(self,payload,authorize):
        fields(payload,('request_id','text'),('model','effort'))
        request_id=identifier(payload['request_id']);text=payload['text']
        if isinstance(text,str) and text.startswith('/'):
            raise ServiceError('agent_slash_command_required',status=409)
        if not isinstance(text,str) or not text.strip() or '\0' in text or len(text.encode())>60000:raise ServiceError('invalid_task')
        # Model and effort are per-turn overrides the provider advertises in
        # model/list; the boundary only checks their shape, never a whitelist
        # of its own that would go stale the day a model ships.
        model,effort=payload.get('model'),payload.get('effort')
        for value in (model,effort):
            if value is not None and (not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',value)):
                raise ServiceError('invalid_request')
        try:return await (await self.require(authorize)).send(text,request_id,model=model,effort=effort)
        except AgentChatError as error:raise ServiceError(error.code,status=409) from None

    async def cancel(self,payload,authorize):
        fields(payload,('turn_id',));turn_id=identifier(payload['turn_id'])
        try:return await (await self.require(authorize)).cancel(turn_id)
        except AgentChatError as error:raise ServiceError(error.code,status=409) from None

    async def _events(self):
        while True:
            event=await self.chat.events.get()
            atomic_private_json(self.sequence_path,{'sequence':event['sequence']})
            if event['event']['type'] in {'agent.status.changed','agent.usage.updated',
                                          'agent.approval.requested','agent.approval.resolved'}:
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
