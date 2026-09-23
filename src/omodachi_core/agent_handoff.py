"""Explicit same-thread handoff of an idle legacy default TUI.

Prepare is read-only. Confirm targets a recorded idle Codex process, leaves its
Herdr pane alive, and uses official agent start/resume arguments. No PTY parsing.
"""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import signal
import time
import uuid
from .agent import ReadOnlyAgentProbe
from .agent_lifecycle import SESSION,atomic_private_json,read_private_json
from .service import ServiceError,fields,identifier

class DefaultAgentHandoff:
    def __init__(self,chat,*,runner=None,terminate=None,process_stamp=None):
        self.chat=chat;self.manager=chat.manager;self.root=self.manager.root/'agent-handoff'
        self.runner=runner or self._run;self.terminate=terminate or (lambda pid:os.kill(pid,signal.SIGTERM))
        self.process_stamp=process_stamp or self._stamp
        self.lock=asyncio.Lock()
    def _run(self,argv):
        value=ReadOnlyAgentProbe._run_process(tuple(argv),timeout_seconds=15,max_bytes=262144)
        try:result=json.loads(value.stdout)
        except (ValueError,TypeError):raise ServiceError('handoff_probe_unavailable',status=503) from None
        if value.returncode or value.error or not isinstance(result,dict):
            code=result.get('error',{}).get('code') if isinstance(result,dict) else None
            raise ServiceError(code if code in {'agent_not_found','agent_pane_busy','agent_start_failed'} else 'handoff_operation_failed',status=409)
        return result.get('result',result)
    @staticmethod
    def _stamp(pid):
        path=Path('/proc')/str(pid)
        if path.stat().st_uid!=os.getuid():raise ServiceError('handoff_target_changed',status=409)
        fields=(path/'stat').read_text().rpartition(') ')[2].split()
        return fields[19]
    def _agent(self):return self.runner(('herdr','--session',SESSION,'agent','get','default')).get('agent',{})
    def _processes(self,pane):
        result=self.runner(('herdr','--session',SESSION,'pane','process-info','--pane',pane))
        return result.get('process_info',{})
    def _target(self):
        agent=self._agent();session=agent.get('agent_session') or {}
        if agent.get('agent')!='codex' or agent.get('name')!='default' or session.get('kind')!='id' or session.get('agent')!='codex':
            raise ServiceError('structured_agent_binding_required',status=409)
        if agent.get('agent_status') not in {'idle','done'}:raise ServiceError('agent_busy',status=409)
        pane=identifier(agent.get('pane_id'));thread=identifier(session.get('value'));info=self._processes(pane)
        candidates=[p for p in info.get('foreground_processes',[]) if p.get('name')=='codex' and type(p.get('pid')) is int]
        if len(candidates)!=1 or not info.get('shell_pid') or info.get('foreground_process_group_id')==info['shell_pid']:
            raise ServiceError('handoff_target_unavailable',status=409)
        pid=candidates[0]['pid']
        return {'pane_id':pane,'terminal_id':identifier(agent.get('terminal_id')),'thread_id':thread,
            'pid':pid,'process_start':self.process_stamp(pid),'shell_pid':info['shell_pid']}
    async def prepare(self,authorize):
        authorize();target=await asyncio.to_thread(self._target)
        plan={'plan_id':'handoff_'+uuid.uuid4().hex,'state':'prepared','created_at':time.time(),'target':target}
        atomic_private_json(self.root/(plan['plan_id']+'.json'),plan)
        return {'plan_id':plan['plan_id'],'status':'prepared','requires_confirmation':True,
            'agent_id':'default','provider':'codex','provider_session_id':target['thread_id'],'pane_id':target['pane_id'],
            'impact':'The idle Codex TUI will stop and reopen in this same Herdr pane, connected to the managed app-server on the same thread. The pane and conversation are retained.',
            'rollback':'If managed attachment fails after the old TUI exits, resume the same thread in the original pane without remote mode.'}
    def _shell_ready(self,target):
        info=self._processes(target['pane_id'])
        if info.get('shell_pid')!=target['shell_pid']:raise ServiceError('handoff_target_changed',status=409)
        return info.get('foreground_process_group_id')==target['shell_pid']
    def _start(self,target,args):
        result=self.runner(('herdr','--session',SESSION,'agent','start','default','--kind','codex','--pane',target['pane_id'],'--timeout','10000','--',*args))
        agent=result.get('agent',{})
        if agent.get('pane_id')!=target['pane_id'] or agent.get('agent')!='codex':raise ServiceError('handoff_attach_unconfirmed',status=409)
        session=agent.get('agent_session')
        if not isinstance(session,dict):
            agent=self._agent();session=agent.get('agent_session') or {}
        if (agent.get('pane_id')!=target['pane_id'] or agent.get('agent')!='codex'
                or session.get('agent')!='codex' or session.get('kind')!='id'
                or session.get('value')!=target['thread_id']):
            raise ServiceError('handoff_thread_unconfirmed',status=409)
    async def confirm(self,payload,authorize):
        fields(payload,('plan_id','confirmed'))
        plan_id=identifier(payload['plan_id'])
        if not plan_id.startswith('handoff_') or payload['confirmed'] is not True:raise ServiceError('handoff_confirmation_required',status=409)
        async with self.lock:
            authorize();path=self.root/(plan_id+'.json');plan=read_private_json(path)
            if plan['state']=='completed':return plan['result']
            if plan['state']!='prepared':raise ServiceError('handoff_not_repeatable',status=409)
            if time.time()-plan['created_at']>300:raise ServiceError('handoff_plan_expired',status=409)
            target=plan['target'];actual=await asyncio.to_thread(self._target)
            if actual!=target:raise ServiceError('handoff_target_changed',status=409)
            plan['state']='stopping_idle_writer';atomic_private_json(path,plan)
            authorize();await asyncio.to_thread(self.terminate,target['pid'])
            deadline=time.monotonic()+5
            while not await asyncio.to_thread(self._shell_ready,target):
                if time.monotonic()>=deadline:
                    plan['state']='stop_unconfirmed';atomic_private_json(path,plan)
                    raise ServiceError('handoff_stop_unconfirmed',status=409)
                await asyncio.sleep(.05)
            plan['state']='old_writer_stopped';atomic_private_json(path,plan)
            try:
                authorize()
                binding=await self.chat.owner.resume_existing(target['thread_id'],handoff_confirmed=True)
                await asyncio.to_thread(self._start,target,self.chat.owner.tui_argv(binding)[1:])
                plan['state']='completed';plan['result']={'status':'completed','provider_session_id':target['thread_id'],'pane_id':target['pane_id'],'same_thread':True,'herdr_pane_registered':True}
                atomic_private_json(path,plan);return plan['result']
            except Exception:
                # Do not send into a running remote TUI if attach outcome is
                # uncertain. Rollback only when the original shell is proven idle.
                if await asyncio.to_thread(self._shell_ready,target):
                    await self.chat.owner.stop_owned()
                    await asyncio.to_thread(self._start,target,['resume',target['thread_id']])
                    record=self.chat.owner.owned_metadata()
                    if record:
                        record['mode']='legacy_handoff_rolled_back';self.chat.owner._save(record)
                    plan['state']='rolled_back';atomic_private_json(path,plan)
                    raise ServiceError('handoff_rolled_back',status=409) from None
                plan['state']='attachment_needs_review';atomic_private_json(path,plan)
                raise ServiceError('handoff_attach_unconfirmed',status=409) from None
