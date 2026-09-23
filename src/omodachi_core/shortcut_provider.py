"""Official Omarchy binding records exposed through the existing catalog.

Display text is never executable. The client sends an opaque `action_ref`; the
dispatcher and the argument stay in the host registry and come from the host's
own `output_binding_records`.

SHORTCUT-1: every record the host publishes with a dispatcher is executable.
The adapter no longer keeps an allowlist of reviewed commands — it keeps a
mapping from the four record shapes Omarchy emits onto the four ways this host
can replay them, and runs the row the same way pressing the key does. Only a
record the host itself publishes with no dispatcher at all stays unusable, and
it says so in `disabled_reason_detail`.

CLIP-1 §1: such a row also carries `hidden`. Omarchy binds five keys to bare
Lua *functions* (`Universal copy/paste/cut`, `Zoom in`, `Reset zoom`), and a
function is not something `omarchy-menu-keybindings` can write into a record —
both its dispatcher and its argument come out empty. Nothing here can run them
and nothing here ever will, so a list that greys them is a list with five dead
rows in it. `hidden` says leave them out; the client has a switch that puts
them back, still greyed, for somebody who wants to see what the host has.
"""
from __future__ import annotations
import hashlib
import json
import re
import subprocess
import time
from .graphical import (bounded_hyprctl,focus_workspace_on_output,graphical_environment,
                        GraphicalUnavailable,OUTPUT_NAME,RELATIVE_WORKSPACE,step_workspace,
                        WORKSPACE_ID)
from .routes import RouteDescriptor

PROVIDER='omodachi.keybindings'
PREFIX='omodachi.shortcut.'
OFFICIAL_SOURCE='source /usr/bin/omarchy-menu-keybindings --print >/dev/null; '
READ_ARGV=('/bin/bash','-c',OFFICIAL_SOURCE+'output_binding_records')
#: `dispatch_binding` is the host's own replay of one record — the same
#: function the desktop keybindings menu calls when the user picks a row.
#: Its status is printed rather than returned so a compositor refusal reads as
#: `shortcut_execution_failed` instead of a generic probe failure.
EXEC_SCRIPT=OFFICIAL_SOURCE+'dispatch_binding "$1" "$2" >/dev/null 2>&1; printf %s "$?"'

#: The public execution kinds, and the record dispatcher each one replays.
#: `eval` is what a Lua binding costs on this host: Hyprland 0.56.2 with
#: Omarchy 4's Lua config reports every bind as `__lua` with a callback index
#: in `hyprctl binds -j`, and `hyprctl dispatch <name> <arg>` no longer parses
#: (it is wrapped as `hl.dispatch(<name> <arg>)` Lua), so the only replayable
#: form is the `hl.*` expression Omarchy's own record carries.
EXECUTION_KINDS=('exec','dispatch','eval','sendshortcut')
#: Lua expressions that act on whatever window has focus. Everything else
#: `hl.dsp.*` addresses the compositor (a workspace, a monitor, the shell).
FOCUSED_LUA=re.compile(r'^hl\.dsp\.(window|group)\.|^hl\.dsp\.layout\(')
#: `Switch to workspace N` / `Next workspace` / `Previous workspace`. These are
#: the only bindings a Remote session rewrites, and the selector is captured so
#: the rewrite can name the workspace. `previous`, `special:*` and the
#: monitor-relative forms are deliberately not matched — see `_remote_command`.
WORKSPACE_FOCUS=re.compile(r'^hl\.dsp\.focus\(\{workspace="([A-Za-z0-9+-]{1,16})"\}\)$')
ADDRESS=re.compile(r'0x[0-9a-fA-F]{1,32}')
#: How long a spawned `exec` is given to fail before the row is reported as
#: running. A launcher that forks returns within a few milliseconds.
#:
#: PERF-4 shortened it. The poll below ends early when the process *exits*, so
#: the full window was paid by exactly the good case - a launcher that is still
#: alive because it became the user's application. A tenth of a second still
#: catches `command not found`, which is the only thing this window is for.
EXEC_SETTLE=0.1
#: The longest the receipt waits for the compositor to show the change it was
#: just asked for. `_settle` stops the moment the reading moves, so this is a
#: ceiling and not a cost: on the live host the change is visible in the first
#: probe. PERF-4's rule is that the effect is reported by the state stream; the
#: receipt only says what the host accepted.
DISPATCH_SETTLE=0.15


def _workspace_view(value):
    if not isinstance(value,dict) or type(value.get('id')) is not int:return None
    name,monitor=value.get('name'),value.get('monitor')
    return {'id':value['id'],
            'name':name[:64] if isinstance(name,str) and not any(ord(c)<32 for c in name) else None,
            'monitor':monitor[:64] if isinstance(monitor,str) and not any(ord(c)<32 for c in monitor) else None}


def _window_view(value):
    """The focused window, without its title — same redaction as the snapshot."""
    if not isinstance(value,dict):return None
    address=value.get('address')
    if not isinstance(address,str) or not ADDRESS.fullmatch(address) or not int(address,16):return None
    app=value.get('class')
    workspace=value.get('workspace') if isinstance(value.get('workspace'),dict) else {}
    return {'address':address,
            'app_id':app[:128] if isinstance(app,str) and not any(ord(c)<32 for c in app) else None,
            'fullscreen':value['fullscreen'] if type(value.get('fullscreen')) is int else None,
            'floating':value['floating'] if type(value.get('floating')) is bool else None,
            'workspace_id':workspace['id'] if type(workspace.get('id')) is int else None}


class ShortcutProvider:
    def __init__(self,service,*,reader=None,runner=bounded_hyprctl,environment=graphical_environment,
                 spawner=None,sleeper=time.sleep):
        self.service=service;self.runner=runner;self.environment=environment;self.sleeper=sleeper
        self.spawner=spawner or self._spawn
        self.reader=reader or (lambda:self.runner(READ_ARGV,self._environment()))
        self.records={};self.public=[];self.owned={};self.loaded_at=-float('inf')

    def _environment(self):
        env=dict(self.environment());env['OMARCHY_PATH']='/usr/share/omarchy'
        return env

    def _retire(self):
        for entry_id,(descriptor,callback) in self.owned.items():
            self.service.policy.unregister(entry_id,expected=descriptor)
            if callback is not None and self.service._executors.get(entry_id,(None,))[0] is callback:
                self.service._executors.pop(entry_id,None)
        self.owned={}

    # SHORTCUT-1. The truth about a binding comes from Hyprland, by way of the
    # record Omarchy publishes for it. On Omarchy 4.0.4 / Hyprland 0.56.2 the
    # compositor's own `hyprctl binds -j` is *not* that truth: every one of the
    # 228 binds is `{"dispatcher": "__lua", "arg": "<callback index>"}`, an
    # index into the Lua config's closure table with no replayable expression
    # in it. `omarchy-menu-keybindings`' `output_binding_records` rebuilds the
    # missing half from the Lua config source, which is why this adapter reads
    # records rather than `binds -j`, and why the report carries both samples.
    #
    # Four record shapes exist on this host, and each has exactly one replay:
    #
    #   exec          run `arg` through a shell in the graphical session, as
    #                 the user, the way Hyprland's own exec does — and keep the
    #                 pid, so the answer can say whether it survived.
    #   lua           `hyprctl dispatch "<hl.* expression>"`, which on a Lua
    #                 config is an eval of that expression. Published as
    #                 `eval`.
    #   sendshortcut  the host's `hl.dsp.send_key_state` down/up pair.
    #   anything else `hyprctl dispatch <dispatcher> <arg>`, the classic form.
    #                 No row on this host uses it and on a Lua config it does
    #                 not parse; it is kept so a non-Lua host is not silently
    #                 dropped.
    #
    # Everything goes through the host's own `dispatch_binding` except `exec`,
    # which core runs itself so that the receipt can carry a pid and an exit
    # status instead of an unobservable "ok".

    #: The two rows this client replaces with its own panel. Everything else
    #: runs on the host, including `omarchy-menu toggle system` and the two
    #: other keybinding menus (`…-tmux-…`, `…-herdr-…`).
    PANEL_COMMANDS=frozenset({'omarchy-menu','omarchy-menu toggle','omarchy menu'})
    SHORTCUTS_COMMANDS=frozenset({'omarchy-menu-keybindings'})

    @classmethod
    def classify(cls,dispatcher,arg):
        """The adapter kind for one binding record, or None when unusable.

        `None` is still a listed row — it just has no `action_ref`, and its
        `disabled_reason_detail` repeats the host's own dispatcher and argument.
        """
        if not isinstance(dispatcher,str) or not isinstance(arg,str):return None
        dispatcher=dispatcher.strip()
        if not dispatcher or not arg.strip():return None
        if dispatcher=='exec':
            command=arg.strip()
            if command in cls.PANEL_COMMANDS:return 'panel'
            if command in cls.SHORTCUTS_COMMANDS:return 'shortcuts'
            return 'exec'
        if dispatcher=='lua':
            return 'focused_window' if FOCUSED_LUA.match(re.sub(r'\s+','',arg)) else 'compositor'
        if dispatcher=='sendshortcut':return 'sendshortcut'
        return 'dispatch'

    @classmethod
    def execution(cls,dispatcher,arg):
        """The public `{kind, detail}` for one record, or None when unusable."""
        kind=cls.classify(dispatcher,arg)
        if kind is None:return None
        if kind in {'panel','shortcuts','exec'}:return {'kind':'exec','detail':arg.strip()}
        if kind in {'compositor','focused_window'}:return {'kind':'eval','detail':arg.strip()}
        if kind=='sendshortcut':return {'kind':'sendshortcut','detail':arg.strip()}
        return {'kind':'dispatch','detail':dispatcher.strip()+' '+arg.strip()}

    #: A row whose kind is in here acts on the focused window, so a client can
    #: say so up front — but it is never greyed out for it, and executing with
    #: nothing focused answers `no_focused_window` rather than `stale_target`.
    TARGET_KINDS=frozenset({'focused_window','sendshortcut'})
    #: Kinds core executes itself. `panel`/`shortcuts` stay native routes.
    HOST_KINDS=frozenset({'exec','compositor','focused_window','sendshortcut','dispatch'})

    def rows(self,*,force=False):
        if not force and time.monotonic()-self.loaded_at<1:return list(self.public)
        raw=self.reader()
        if not isinstance(raw,str) or len(raw.encode())>524288:raise GraphicalUnavailable('keybindings_unavailable')
        incoming={};rows=[]
        for index,line in enumerate(raw.splitlines()):
            if not line:continue
            parts=line.split('\t',2)
            if len(parts)!=3 or ' → ' not in parts[0]:raise GraphicalUnavailable('keybinding_record_invalid')
            display,dispatcher,arg=parts;shortcut,label=display.split(' → ',1)
            if not label.strip() or len(display)>1024:raise GraphicalUnavailable('keybinding_record_invalid')
            ref=PREFIX+hashlib.sha256(line.encode()).hexdigest()[:24]
            if ref in incoming:continue
            kind=self.classify(dispatcher,arg);execution=self.execution(dispatcher,arg)
            incoming[ref]={'dispatcher':dispatcher,'arg':arg,'kind':kind,'execution':execution}
            meta={'id':ref,'label':label.strip(),'shortcut_display':shortcut.strip(),'order':index,
                  'enabled':kind is not None,'disabled_reason':None if kind else 'binding_adapter_unavailable',
                  'disabled_reason_detail':None if kind else
                      'host record carries no executable binding: dispatcher='+json.dumps(dispatcher)+' arg='+json.dumps(arg),
                  'action_ref':ref if kind else None,'requires_target':kind in self.TARGET_KINDS,
                  # CLIP-1 §1. A row nothing here can run is not a row to grey
                  # out; it is a row to leave out. `hidden` is the hint, and it
                  # is the whole of the hint: a client hides on this field, not
                  # on a reason code, so a future reason that *is* executable
                  # cannot disappear from a list by accident.
                  'hidden':kind is None,
                  'execution':execution}
            rows.append({'id':ref,'parent':'learn.keybindings','label':meta['label'],'description':meta['shortcut_display'],
                         'aliases':[meta['shortcut_display']],'order':index,'action':'omodachi-keybinding '+ref,
                         'shortcut':meta})
        if len(rows)>512:raise GraphicalUnavailable('keybindings_limit')
        if incoming!=self.records:
            self._retire();self.records=incoming
            for row in rows:
                ref=row['id'];kind=incoming[ref]['kind']
                if kind is None:continue
                callback=None
                if kind in self.HOST_KINDS:
                    # SHORTCUT-1 item 2. No row carries a client-held window
                    # token any more: a shortcut is what the physical key does,
                    # which is "act on whatever has focus now". Focus is read
                    # at execution time and reported in `observed`.
                    self.service.policy.register(ref,RouteDescriptor('host',True,argv=('omodachi-keybinding',ref)),source_action=row['action'])
                    def callback(argv,owner=self):return owner.perform(argv[1])
                    self.service.register_executor(ref,callback)
                else:self.service.policy.register_native(ref,kind,source_action=row['action'])
                self.owned[ref]=(self.service.policy.resolve(row),callback)
        self.public=rows;self.loaded_at=time.monotonic();return list(rows)

    def _probe(self,env):
        """`activeworkspace` and `activewindow`, or nulls. Never raises.

        An observation that cannot be taken must not turn a binding that ran
        into a failure, so every fault here degrades to `None`.
        """
        result={'workspace':None,'window':None}
        for key,query in (('workspace','activeworkspace'),('window','activewindow')):
            try:value=json.loads(self.runner(('/usr/bin/hyprctl','-j',query),env))
            except Exception:continue
            result[key]=_workspace_view(value) if key=='workspace' else _window_view(value)
        return result

    def _settle(self,env,before,budget=DISPATCH_SETTLE):
        """The compositor's reading once it has moved, or once `budget` is up.

        PERF-4. This replaced a blind `sleep(budget)` followed by one probe.
        The sleep was paid in full on every single invoke - the host had
        already done the thing before the first millisecond of it - so a
        keybinding cost 150 ms of nothing, and an `exec` 500 ms.
        """
        after=self._probe(env)
        deadline=time.monotonic()+budget
        while after==before and time.monotonic()<deadline:
            self.sleeper(0.02)
            after=self._probe(env)
        return after

    def _spawn(self,command,env):
        """Run one `exec` record's command line the way Hyprland's exec does.

        Same shell, same graphical session, same user; detached so the row's
        window outlives this request. The pid and an early exit are the only
        thing the compositor cannot tell us, which is exactly why core runs
        this one kind itself.
        """
        process=subprocess.Popen(('/bin/bash','-c',command),env=env,stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        deadline=time.monotonic()+EXEC_SETTLE
        while time.monotonic()<deadline and process.poll() is None:self.sleeper(0.02)
        code=process.poll()
        return {'pid':process.pid,'exited':code is not None,'exit_code':code}

    def owned_output(self):
        """The Remote session's own output, or None when there is no session."""
        manager=getattr(self.service.remote,'manager',None)
        session=manager.current() if manager is not None else None
        name=getattr(session,'output_name',None) if session is not None else None
        return name if isinstance(name,str) and name else None

    def _remote_command(self,row,env):
        """Rewrite one workspace focus so a Remote session lands on its own output.

        Outside a session nothing is rewritten: pressing `SUPER+3` on the host
        means "go to whichever screen shows workspace 3", and that is still the
        answer for the physical keyboard. Inside a session the user is looking
        at our output, and Study 04 A-64 says the workspace controls act on the
        screen they are looking at — so workspace N is pulled here first, which
        is what Hyprland's own `focusworkspaceoncurrentmonitor` does.

        `Move window to workspace N` is untouched: it carries the focused window
        with it, and following it to the physical screen is the point.
        """
        output=self.owned_output()
        if output is None:return None
        match=WORKSPACE_FOCUS.fullmatch(re.sub(r'\s+','',row['arg']))
        if match is None:return None
        selector=match.group(1)
        if selector in RELATIVE_WORKSPACE:
            selector=self._relative_workspace(output,RELATIVE_WORKSPACE[selector],env)
            if selector is None:return None
        if not WORKSPACE_ID.fullmatch(selector):return None
        try:return focus_workspace_on_output(selector,output),output
        except GraphicalUnavailable:return None

    def _relative_workspace(self,output,delta,env):
        """`e+1`/`e-1` inside a session: step the workspaces on our own output."""
        try:
            monitors=json.loads(self.runner(('/usr/bin/hyprctl','-j','monitors'),env))
            workspaces=json.loads(self.runner(('/usr/bin/hyprctl','-j','workspaces'),env))
        except Exception:return None
        if not isinstance(monitors,list) or not isinstance(workspaces,list):return None
        current=next((row.get('activeWorkspace',{}).get('id') for row in monitors
                      if isinstance(row,dict) and row.get('name')==output
                      and isinstance(row.get('activeWorkspace'),dict)),None)
        mine=[row.get('id') for row in workspaces
              if isinstance(row,dict) and row.get('monitor')==output]
        number=step_workspace(current,mine,delta)
        return None if number is None else str(number)

    def perform(self,reference):
        """Replay one binding and answer with what the host did.

        Returns the `observed` body of the action result: the compositor before
        and after, and for `exec` the process core started.
        """
        from .service import ServiceError
        self.rows(force=True)
        row=self.records.get(reference)
        if not row or row['kind'] not in self.HOST_KINDS:raise ServiceError('stale_binding',status=409)
        env=self._environment()
        before=self._probe(env)
        if row['kind'] in self.TARGET_KINDS and before['window'] is None:
            raise ServiceError('no_focused_window',status=409)
        observed={'kind':row['execution']['kind'],'before':before}
        redirect=self._remote_command(row,env) if row['kind']=='compositor' else None
        settled=None
        if row['kind']=='exec':
            observed['process']=self.spawner(row['arg'].strip(),env)
        elif redirect is not None:
            command,destination=redirect
            # A compound expression cannot go through `dispatch_binding`, whose
            # `lua` arm is a single `hyprctl dispatch`. `eval` runs the pair and
            # the guard inside it turns a compositor refusal into a non-zero exit.
            try:answer=self.runner(('/usr/bin/hyprctl','eval',command),env)
            except GraphicalUnavailable:raise ServiceError('shortcut_execution_failed',status=503) from None
            if answer.strip().lower()!='ok':raise ServiceError('shortcut_execution_failed',status=503)
            observed['redirected_output']=destination
        else:
            output=self.runner(('/bin/bash','-c',EXEC_SCRIPT,'omodachi-shortcut',row['dispatcher'],row['arg']),env)
            if output.strip() not in {'','0'}:raise ServiceError('shortcut_execution_failed',status=503)
        settled=self._settle(env,before)
        observed['after']=settled
        observed['changed']=settled!=before
        return observed

    def validate_context(self,context,device):
        from .service import ServiceError
        if not isinstance(context,dict) or context.get('surface') not in {'omarchy','remote'}:
            raise ServiceError('shortcut_context_required',status=400)
        if context['surface']=='omarchy':
            if set(context)!={'surface'}:raise ServiceError('invalid_request')
            return
        if set(context)!={'surface','session_id','revision'}:raise ServiceError('invalid_request')
        manager=getattr(self.service.remote,'manager',None)
        session=manager.current() if manager is not None else None
        if session is None or session.id!=context['session_id'] or session.device_id!=device:
            raise ServiceError('stale_session',status=409)
        if session.state!='ready':raise ServiceError('remote_not_ready',status=409)
        if type(context['revision']) is not int or context['revision']!=session.revision:
            raise ServiceError('stale_revision',status=409)


def validate_observation(value):
    """Accept only the adapter's own observation shape onto the wire."""
    if not isinstance(value,dict) or value.get('kind') not in EXECUTION_KINDS:return None
    if set(value)-{'kind','before','after','changed','process','redirected_output'}:return None
    if type(value.get('changed')) is not bool:return None
    for side in ('before','after'):
        state=value.get(side)
        if not isinstance(state,dict) or set(state)!={'workspace','window'}:return None
    process=value.get('process')
    if process is not None and (not isinstance(process,dict) or set(process)!={'pid','exited','exit_code'}):return None
    output=value.get('redirected_output')
    if output is not None and (not isinstance(output,str) or not OUTPUT_NAME.fullmatch(output)):return None
    return value


def install_shortcut_provider(service,**options):
    provider=ShortcutProvider(service,**options);service.shortcut_provider=provider
    service.runtime.register_provider(PROVIDER,provider.rows)
    service.policy.register_native('learn.keybindings','shortcuts',source_action='omarchy-menu-keybindings')
    native=list(service.hub.capabilities_snapshot().get('native',[]))
    service.hub.update_state({'capabilities':{'native':list(dict.fromkeys(native+['shortcuts','panel']))}})
    return provider
