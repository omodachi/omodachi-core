"""Synthetic Pulse module inventory and an actual OS pipe; no audio daemon."""
from copy import deepcopy
import os
import selectors
import shlex
import subprocess
import time

class PipeProcess:
    def __init__(self):
        self.read_fd,write_fd=os.pipe();self.stdin=os.fdopen(write_fd,'wb',buffering=0)
        self.returncode=None;self.stop_fails=False;self.received=bytearray()
    def poll(self):return self.returncode
    def terminate(self):
        if self.stop_fails:raise OSError('synthetic stop failure')
        self.returncode=-15
    def kill(self):
        if self.stop_fails:raise OSError('synthetic kill failure')
        self.returncode=-9
    def wait(self,timeout=None):
        if self.returncode is None:raise subprocess.TimeoutExpired('synthetic-writer',timeout)
        return self.returncode
    def read_exact(self,count,timeout=1):
        deadline=time.monotonic()+timeout;result=bytearray();selector=selectors.DefaultSelector()
        selector.register(self.read_fd,selectors.EVENT_READ)
        try:
            while len(result)<count and time.monotonic()<deadline:
                if not selector.select(max(0,deadline-time.monotonic())):break
                data=os.read(self.read_fd,count-len(result))
                if not data:break
                result.extend(data)
        finally:selector.close()
        self.received.extend(result);return bytes(result)
    def close(self):
        try:self.stdin.close()
        except OSError:pass
        if self.read_fd is not None:
            os.close(self.read_fd);self.read_fd=None

class FakePulse:
    def __init__(self):
        self.calls=[];self.spawns=[];self.processes=[];self.next_id=100
        self.modules={};self.sinks={};self.sources={}
        self.fail_load_kind=None;self.fail_unload=set();self.ack_without_removal=set();self.fail_lists=False
        self.module_json_has_index=True;self.short_unindexed_rows=[]
    def runner(self,argv):
        argv=tuple(argv);self.calls.append(argv)
        if argv[:3]==('pactl','--format=json','list'):
            if self.fail_lists:raise OSError('synthetic metadata unavailable')
            rows=deepcopy(list(getattr(self,argv[3]).values()))
            if argv[3]=='modules' and not self.module_json_has_index:
                for row in rows:
                    row.pop('index',None);row['usage_counter']='n/a';row['properties']={}
            return rows
        if argv==('pactl','list','short','modules'):
            if self.fail_lists:raise OSError('synthetic metadata unavailable')
            return [dict(row,index=str(ident)) for ident,row in deepcopy(self.modules).items()]+deepcopy(self.short_unindexed_rows)
        if argv[:2]==('pactl','load-module'):
            kind=argv[2]
            if kind==self.fail_load_kind:raise OSError('synthetic load failure')
            self.next_id+=1;ident=self.next_id
            arguments=dict(a.split('=',1) for a in argv[3:])
            self.modules[ident]={'index':ident,'name':kind,'argument':shlex.join(argv[3:])}
            if kind=='module-null-sink':
                name=arguments['sink_name'];self.sinks[name]={'index':ident+1000,'name':name,'owner_module':ident}
                self.sources[name+'.monitor']={'index':ident+2000,'name':name+'.monitor','owner_module':ident,'monitor_of_sink':ident+1000}
            else:
                name=arguments['source_name'];self.sources[name]={'index':ident+3000,'name':name,'owner_module':ident}
            return ident
        if argv[:2]==('pactl','unload-module'):
            ident=int(argv[2])
            if ident in self.fail_unload:raise OSError('synthetic unload failure')
            if ident in self.ack_without_removal:return None
            self.modules.pop(ident,None)
            self.sinks={n:r for n,r in self.sinks.items() if r['owner_module']!=ident}
            self.sources={n:r for n,r in self.sources.items() if r['owner_module']!=ident}
            return None
        raise AssertionError('unreviewed command')
    def spawn(self,argv):
        self.spawns.append(tuple(argv));p=PipeProcess();self.processes.append(p);return p
    def close(self):
        for p in self.processes:p.stop_fails=False;p.returncode=0;p.close()
