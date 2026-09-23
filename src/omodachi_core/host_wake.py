"""Wake through Omarchy's existing screensaver exit and system wake paths."""
from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import time
from .graphical import bounded_hyprctl, graphical_environment, GraphicalUnavailable

SCREENSAVER_CLASS='org.omarchy.screensaver'
OFFICIAL_SCRIPTS={'/usr/bin/omarchy-screensaver','/usr/share/omarchy/bin/omarchy-screensaver'}

class HostWake:
    def __init__(self, *, runner=bounded_hyprctl, environment=graphical_environment, proc=Path('/proc'),
                 sender=os.kill, idle_reader=None, cache_seconds=5.0, clock=time.monotonic):
        self.runner,self.environment,self.proc,self.sender=runner,environment,Path(proc),sender
        self.idle_reader=idle_reader or self._idle_status
        # PERF-4 §0. One reading here is five processes: two `hyprctl -j` calls
        # and `omarchy-shell idle status`, which itself spawns `timeout` and
        # `qs ipc`. The workspace probe asked for it twice a second, so the
        # daemon spawned about sixteen processes a second while nothing at all
        # was happening - most of the 45 % CPU it was measured at. Whether the
        # screensaver is up is not a per-frame fact.
        self.cache_seconds,self.clock=cache_seconds,clock
        self._cached=None

    def invalidate(self):
        """Forget the reading: something was just done to the desktop."""
        self._cached=None

    def _idle_status(self,env):
        env={**env,'OMARCHY_PATH':'/usr/share/omarchy'}
        value=json.loads(self.runner(('/usr/bin/omarchy-shell','idle','status'),env))
        if (not isinstance(value,dict) or type(value.get('inIdleCycle')) is not bool
                or type(value.get('screensaverWindows')) is not int
                or type(value.get('processes',{}).get('wake')) is not bool):
            raise GraphicalUnavailable('wake_state_unavailable')
        return value

    def _snapshot(self,env):
        clients=json.loads(self.runner(('/usr/bin/hyprctl','-j','clients'),env))
        monitors=json.loads(self.runner(('/usr/bin/hyprctl','-j','monitors','all'),env))
        if not isinstance(clients,list) or not isinstance(monitors,list):raise GraphicalUnavailable('wake_state_unavailable')
        pids={row['pid'] for row in clients if isinstance(row,dict) and row.get('class')==SCREENSAVER_CLASS
              and type(row.get('pid')) is int and row['pid']>0}
        visible=[m for m in monitors if isinstance(m,dict) and m.get('disabled') is not True]
        if not visible or any(type(m.get('dpmsStatus')) is not bool for m in visible):raise GraphicalUnavailable('wake_state_unavailable')
        idle=self.idle_reader(env)
        pending=idle['inIdleCycle'] or idle['screensaverWindows']>0 or idle['processes']['wake']
        return {'screensaver_active':bool(pids),'display_asleep':any(not m['dpmsStatus'] for m in visible),
                'locked':None,'wake_pending':pending},pids

    def inspect(self,environment=None,*,fresh=False):
        cached=self._cached
        if not fresh and cached is not None and self.clock()-cached[0]<=self.cache_seconds:
            return dict(cached[1])
        value=self._snapshot(environment if environment is not None else self.environment())[0]
        self._cached=(self.clock(),dict(value))
        return dict(value)

    def _process(self,pid):
        path=self.proc/str(pid)
        if path.stat().st_uid!=os.getuid():return None
        raw=(path/'stat').read_text().rpartition(') ')[2].split()
        if len(raw)<20:return None
        with (path/'cmdline').open('rb') as stream:argv=stream.read(8193).split(b'\0')
        if sum(map(len,argv))>8192:return None
        return {'pid':pid,'ppid':int(raw[1]),'start':raw[19],
                'official':any(arg.decode(errors='replace') in OFFICIAL_SCRIPTS for arg in argv[:2])}

    def _screensaver_children(self,terminal_pids):
        processes={}
        for path in self.proc.iterdir():
            if not path.name.isdigit():continue
            try:
                row=self._process(int(path.name))
                if row:processes[row['pid']]=row
            except (OSError,ValueError):continue
        result=[]
        for row in processes.values():
            if not row['official']:continue
            cursor=row['pid'];seen=set()
            for _ in range(32):
                if cursor in terminal_pids:result.append(row);break
                if cursor in seen or cursor not in processes:break
                seen.add(cursor);cursor=processes[cursor]['ppid']
        return result

    def wake(self):
        # Something is about to be done to the desktop; the cached reading is
        # about to be wrong either way.
        self.invalidate()
        env=self.environment();before,terminals=self._snapshot(env)
        if before['screensaver_active']:
            for row in self._screensaver_children(terminals):
                # Recheck the exact official process and its start generation;
                # never signal the terminal itself, lockscreen or an arbitrary PID.
                try:
                    current=self._process(row['pid'])
                    if current and current['official'] and current['start']==row['start']:
                        self.sender(row['pid'],signal.SIGTERM)
                except ProcessLookupError:pass
        if before['display_asleep'] and not before['screensaver_active']:
            self.runner(('/usr/bin/omarchy-system-wake',),env)
        after,_=self._snapshot(env)
        deadline=time.monotonic()+5.0
        while (after['screensaver_active'] or after['display_asleep'] or after['wake_pending']) and time.monotonic()<deadline:
            time.sleep(.05);after,_=self._snapshot(env)
        return {'screensaver_was_active':before['screensaver_active'],
                'exited':before['screensaver_active'] and not after['screensaver_active'],
                'consume_tap':before['screensaver_active'] or before['display_asleep'] or before['wake_pending'],
                'dpms_woken':before['display_asleep'] and not after['display_asleep'],'wake':after}
        # (the reading is refreshed by the next probe, from `after`)
