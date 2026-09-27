"""One user-selected WayVNC instance bound to an existing owned output.

The only listener is a pre-bound Unix socket, `rfb.sock`, in this session's own
0700 directory, handed to WayVNC as `fd:N`; core's authenticated WSS bridge is
the one thing that dials it. Host owns geometry (-R); WayVNC never creates a
second desktop.

RELEASE-9 (B3). It used to be a loopback TCP port with no RFB authentication
(`-C /dev/null`), and loopback is not private: any local account, and any
container sharing the host network, could find the port and drive the owner's
keyboard and mouse for as long as a session was up. A Unix socket in a 0700
directory owned by the owner can only be connected to by the owner (and root),
which is exactly who the WSS bridge already is.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import time
import threading
from collections import deque
from .errors import RemoteError

class ManagedWayVNC:
    def __init__(self,root,*,environment,process_factory=subprocess.Popen,command_runner=subprocess.run):
        self.root=Path(root);self.environment=environment;self.process_factory=process_factory;self.command_runner=command_runner
        self.process=None;self.control=self.root/'control.sock';self.listener=self.root/'rfb.sock';self.socket_path=None;self.output=None;self.pixels=None;self.logical_size=None
        self.state_path=self.root/"instance.json";self._errors=deque(maxlen=128)
        try:
            state=json.loads(self.state_path.read_text());self.output=state["output"];self.socket_path=state.get("socket");self.pixels=state["pixels"];self.logical_size=state.get("logical_size")
        except FileNotFoundError:pass
    def available(self):
        try:
            p=self.command_runner(['/usr/bin/wayvnc','--version'],capture_output=True,text=True,timeout=2,check=False)
            return p.returncode==0 and '0.10.1' in p.stdout
        except (OSError,subprocess.SubprocessError):return False
    def _query(self,command):
        result=self.command_runner(['/usr/bin/wayvncctl','--socket',str(self.control),'--json',command],capture_output=True,text=True,timeout=2,check=False)
        if result.returncode:raise RemoteError('vnc_control_unavailable')
        value=json.loads(result.stdout)
        if isinstance(value,dict):
            if value.get('code',0)!=0:raise RemoteError('vnc_control_unavailable')
            value=value.get('data')
        return value
    def _capture_errors(self,stream):
        try:
            for line in iter(lambda:stream.readline(512),b''):
                self._errors.append(line[:512])
        finally:
            stream.close()
            if self._errors:
                path=self.root/'last-error.txt'
                fd=os.open(path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
                with os.fdopen(fd,'wb') as target:target.write(b''.join(self._errors)[-16384:])

    def start(self,output,pixels,logical_size=None):
        if self.process is not None or self.control.exists():
            if not self.stop():raise RemoteError('vnc_previous_instance_running')
        if not self.available():raise RemoteError('wayvnc_0_10_1_required')
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.control.exists():raise RemoteError('vnc_previous_instance_running')
        listener=self._bind_private()
        self.socket_path=str(self.listener);self.output=output;self.pixels=dict(pixels);self.logical_size=dict(logical_size) if logical_size else None
        fd=os.open(self.state_path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as record:json.dump({'output':output,'socket':self.socket_path,'pixels':self.pixels,'logical_size':self.logical_size},record)
        args=['/usr/bin/wayvnc','-C','/dev/null','-R','-o',output,'-S',str(self.control),'-L','error','fd:'+str(listener.fileno())]
        try:
            self.process=self.process_factory(args,pass_fds=(listener.fileno(),),env=self.environment(),
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,start_new_session=True)
            threading.Thread(target=self._capture_errors,args=(self.process.stderr,),daemon=True).start()
        finally:listener.close()
        deadline=time.monotonic()+4
        try:
            while time.monotonic()<deadline:
                if self.process.poll() is not None:raise RemoteError('vnc_start_failed')
                try:
                    if self.capture_ready():return self.connection()
                except (OSError,ValueError,RemoteError):pass
                time.sleep(.05)
            raise RemoteError('vnc_capture_not_ready')
        except BaseException:self.stop();raise
    def _bind_private(self):
        """The RFB listener: a Unix socket only the owner can reach.

        The directory is this session's own and must be 0700 and ours - a
        directory anybody else could enter, or one somebody else made, is
        refused rather than trusted. A stale socket from a crashed instance is
        removed only if it is ours and nothing answers on it.
        """
        info=os.lstat(self.root)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid():raise RemoteError('vnc_socket_directory_unsafe')
        if stat.S_IMODE(info.st_mode)&0o077:os.chmod(self.root,0o700)
        try:
            old=os.lstat(self.listener)
        except FileNotFoundError:
            old=None
        if old is not None:
            if not stat.S_ISSOCK(old.st_mode) or old.st_uid!=os.getuid():raise RemoteError('vnc_socket_directory_unsafe')
            probe=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            try:
                probe.connect(str(self.listener));raise RemoteError('vnc_previous_instance_running')
            except (ConnectionRefusedError,FileNotFoundError):os.unlink(self.listener)
            finally:probe.close()
        listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        previous=os.umask(0o177)
        try:listener.bind(str(self.listener))
        except BaseException:listener.close();raise
        finally:os.umask(previous)
        os.chmod(self.listener,0o600);listener.listen(1)
        return listener
    def _unlink_listener(self):
        try:
            info=os.lstat(self.listener)
            if stat.S_ISSOCK(info.st_mode) and info.st_uid==os.getuid():os.unlink(self.listener)
        except FileNotFoundError:pass
    def settle(self,timeout=4.0):
        """Take WayVNC's mid-stream resize for the real client (REMOTE-6).

        WayVNC 0.10.1 opens its FIRST client at the compositor's logical size
        and corrects itself to the output's buffer pixels one update in. Every
        later client is served the settled size from its own ServerInit, which
        is what a session's real client should get: the vendored LibVNCClient
        on iOS refuses the correction with `Rect too large` and the session then
        has to be re-dialled to recover, which is a reconnect nobody asked for.

        So this connects once on the owned socket, speaks the minimum of
        RFB 3.8, asks for one full update and lets WayVNC do its correction
        against a client that does not care, then goes away. Answers the size
        WayVNC ended up serving, or None when it could not be determined - a
        prime that does not work is not a reason to fail a session.
        """
        if self.socket_path is None:return None
        import struct
        deadline=time.monotonic()+timeout
        probe=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        probe.settimeout(timeout)
        try:
            probe.connect(self.socket_path)
            buffer=bytearray()
            def read(count):
                while len(buffer)<count:
                    if time.monotonic()>deadline:raise TimeoutError
                    chunk=probe.recv(65536)
                    if not chunk:raise ConnectionError
                    buffer.extend(chunk)
                value=bytes(buffer[:count]);del buffer[:count];return value
            read(12);probe.sendall(b'RFB 003.008\n')
            types=read(read(1)[0])
            if 1 not in types:return None
            probe.sendall(b'\x01')
            if struct.unpack('>I',read(4))[0]!=0:return None
            probe.sendall(b'\x01')
            head=read(20)
            width,height=struct.unpack('>HH',head[:4])
            read(struct.unpack('>I',read(4))[0])
            probe.sendall(struct.pack('>BBH',2,0,2)+struct.pack('>i',0)+struct.pack('>i',-223))
            probe.sendall(struct.pack('>BBHHHH',3,0,0,0,width,height))
            wanted=(self.pixels or {}).get('width'),(self.pixels or {}).get('height')
            while time.monotonic()<deadline:
                if (width,height)==wanted:return {'width':width,'height':height}
                kind=read(1)[0]
                # WayVNC greets with a ServerCutText before the first update,
                # so the three messages that are not FramebufferUpdate are
                # skipped rather than treated as the end of the prime.
                if kind==1:
                    read(3);read(struct.unpack('>H',read(2))[0]*6);continue
                if kind==2:continue
                if kind==3:
                    read(3);read(struct.unpack('>I',read(4))[0]);continue
                if kind!=0:return None
                read(1)
                for _ in range(struct.unpack('>H',read(2))[0]):
                    x,y,w,h,encoding=struct.unpack('>HHHHi',read(12))
                    if encoding==-223:
                        width,height=w,h
                        probe.sendall(struct.pack('>BBHHHH',3,0,0,0,width,height))
                        continue
                    if encoding!=0:return None
                    remaining=w*h*4
                    while remaining:
                        if not buffer:
                            if time.monotonic()>deadline:raise TimeoutError
                            chunk=probe.recv(65536)
                            if not chunk:raise ConnectionError
                            buffer.extend(chunk)
                        step=min(remaining,len(buffer))
                        del buffer[:step];remaining-=step
                probe.sendall(struct.pack('>BBHHHH',3,1,0,0,width,height))
            return None
        except (OSError,TimeoutError,ConnectionError,ValueError,struct.error):return None
        finally:probe.close()
    def capture_ready(self):
        if self.process is None or self.process.poll() is not None:return False
        # WayVNC0.10.1 output-list reports compositor logical size, not RFB
        # framebuffer pixels. Selected output readiness must not compare these
        # coordinate spaces; actual pixels are checked with the native ACK.
        rows=self._query('output-list')
        return isinstance(rows,list) and any(r.get('name')==self.output and r.get('captured') is True
            and type(r.get('width')) is int and r['width']>0
            and type(r.get('height')) is int and r['height']>0
            and (self.logical_size is None or abs(r['width']-self.logical_size['width'])<=1 and abs(r['height']-self.logical_size['height'])<=1) for r in rows if isinstance(r,dict))
    def connection(self):
        # Host-internal only. The socket path never leaves this process: the
        # client reaches WayVNC through the authenticated WSS bridge instead.
        return {'socket':self.socket_path,'output_id':self.output,
                'pixels':self.pixels,'logical_size':self.logical_size,'automatic_resizing':False}
    def verify_frame(self,pixels):
        if pixels!=self.pixels or not self.capture_ready():return False
        clients=self._query('client-list')
        return isinstance(clients,list) and len(clients)==1
    def stop(self):
        if self.process is None:
            if not self.control.exists():
                self._unlink_listener();return True
            # A terminated owned instance can leave its control socket inode.
            # Confirm no listener on that exact private socket before unlinking.
            before=self.control.lstat()
            probe=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            try:probe.connect(str(self.control))
            except ConnectionRefusedError:
                after=self.control.lstat()
                if (not stat.S_ISSOCK(after.st_mode) or after.st_uid!=os.getuid()
                        or (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino)):
                    raise RemoteError('vnc_recovery_identity_unavailable')
                self.control.unlink();self._unlink_listener();return True
            finally:probe.close()
            rows=self._query('output-list')
            if not self.output or not any(r.get('name')==self.output and r.get('captured') is True for r in rows):
                raise RemoteError('vnc_recovery_identity_unavailable')
            self._query('wayvnc-exit')
            deadline=time.monotonic()+3
            while self.control.exists() and time.monotonic()<deadline:time.sleep(.05)
            if self.control.exists():return False
            self._unlink_listener();return True
        if self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill();self.process.wait(timeout=2)
        self.process=None
        # terminate/wait only proves the owned process ended. Its Unix socket
        # may remain, so complete the same identity-checked stop path before
        # declaring quiescence and allowing the next generation to start.
        return self.stop()


class HostBackendPreference:
    def __init__(self,path):self.path=Path(path)
    def get(self):
        try:value=json.loads(self.path.read_text()).get('backend')
        except (FileNotFoundError,ValueError):return 'sunshine'
        return value if value in {'sunshine','vnc'} else 'sunshine'
    def set(self,backend):
        if backend not in {'sunshine','vnc'}:raise ValueError('invalid_backend')
        import tempfile
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd,name=tempfile.mkstemp(prefix='backend-',dir=self.path.parent)
        try:
            with os.fdopen(fd,'w') as stream:json.dump({'backend':backend},stream);stream.flush();os.fsync(stream.fileno())
            os.replace(name,self.path)
        finally:
            if os.path.exists(name):os.unlink(name)
