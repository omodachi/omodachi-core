"""Real local HTTP/WS + fixed identity stub and bounded synthetic PCM backend.

No microphone, credential/certificate fixture, host audio or managed pairing.
"""
import asyncio
from collections import deque
from copy import deepcopy
import os
import json
from pathlib import Path
import tempfile
import unittest

import aiohttp
from aiohttp import WSMsgType

from omodachi_core.audio_uplink import AuthorizedAudioUplink, AudioUplinkError, FORMAT
from omodachi_core.audio_virtual_input import VirtualMicrophoneSession, FRAME_BYTES
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.ipc import JsonLineServer
from omodachi_core.network import NetworkServer
from omodachi_core.remote import RemoteManager
from omodachi_core.remote.backends import VncBackend
from omodachi_core.remote.hyprland import Hyprland
from omodachi_core.remote.profile import EncoderLimits
from tests.remote_fakes import FakeCompositor, FakeWayVNC, INSTANCE, profile_request

class FixtureIdentity:
    valid=True
    def verify(self, text):
        if not self.valid or text not in {'owner-fixture','other-fixture'}: raise ValueError('fixture unavailable')
        return 'owner' if text=='owner-fixture' else 'other'

class SyntheticBackend:
    def __init__(self):
        self.frames=deque();self.closed=True;self.metadata=None;self.end_calls=[];self.fail_cleanup=False
    def begin(self, *, generation):
        self.closed=False;self.metadata=type('Metadata',(),{'generation':generation})()
        return {'state':'active','generation':generation,**FORMAT}
    def accept(self, frame):
        if self.closed: raise AssertionError('closed backend write')
        if len(self.frames)>=3:return False
        self.frames.append(frame);return True
    def end(self, *, generation, reason):
        if self.metadata.generation!=generation:raise AssertionError('wrong generation cleanup')
        self.end_calls.append((generation,reason));self.frames.clear()
        if self.fail_cleanup:return {'state':'cleanup_pending','cleanup_complete':False,'input_closed':True}
        self.closed=True;self.metadata=None
        return {'state':'closed','cleanup_complete':True,'generation':generation}

class SyntheticFactory:
    def __init__(self):self.ready=True;self.instances=[];self.probes=0
    def available(self):self.probes+=1;return self.ready
    def __call__(self):
        backend=SyntheticBackend();self.instances.append(backend);return backend

class PipePulseFixture:
    """Synthetic pactl metadata + a real anonymous PCM pipe; no host commands."""
    def __init__(self):
        self.modules={};self.sinks={};self.sources={};self.calls=[];self.counter=40
        self.read_fd,self.write_fd=os.pipe();self.process=None;self.instances=[]
    def available(self):return True
    def runner(self,argv):
        self.calls.append(tuple(argv))
        if argv[1]=='load-module':
            self.counter+=1;ident=self.counter;name=argv[2]
            arguments={item.split('=',1)[0]:item.split('=',1)[1] for item in argv[3:] if '=' in item}
            self.modules[ident]={'index':ident,'name':name,'argument':' '.join(argv[3:])}
            if name=='module-null-sink':
                sink=arguments['sink_name'];index=1000+ident
                self.sinks[sink]={'index':index,'name':sink,'owner_module':ident,'monitor_source_name':sink+'.monitor','monitor_source':2000+ident}
                self.sources[sink+'.monitor']={'index':2000+ident,'name':sink+'.monitor','owner_module':ident,'monitor_of_sink':index,'monitor_of_sink_name':sink}
            else:
                source=arguments['source_name'];self.sources[source]={'index':3000+ident,'name':source,'owner_module':ident,'monitor_of_sink':None}
            return ident
        if argv[1]=='unload-module':
            ident=int(argv[2]);self.modules.pop(ident,None)
            self.sinks={k:v for k,v in self.sinks.items() if v['owner_module']!=ident}
            self.sources={k:v for k,v in self.sources.items() if v['owner_module']!=ident}
            return None
        if argv[:3]==('pactl','--format=json','list'):
            return deepcopy(list(getattr(self,argv[3]).values()))
        raise AssertionError('unexpected synthetic pactl command')
    def spawn(self,argv):
        fixture=self
        class Process:
            def __init__(self):self.stdin=os.fdopen(os.dup(fixture.write_fd),'wb',buffering=0);self.terminated=False
            def poll(self):return 0 if self.terminated else None
            def terminate(self):self.terminated=True
            def kill(self):self.terminated=True
            def wait(self,timeout=None):return 0
        self.process=Process();return self.process
    def __call__(self):
        backend=VirtualMicrophoneSession(runner=self.runner,process_factory=self.spawn)
        self.instances.append(backend);return backend
    def read_exact(self,length):
        import select,time
        value=bytearray();deadline=time.monotonic()+2
        while len(value)<length:
            remaining=deadline-time.monotonic()
            if remaining<=0 or not select.select([self.read_fd],[],[],remaining)[0]:raise AssertionError('PCM pipe timed out')
            value.extend(os.read(self.read_fd,length-len(value)))
        return bytes(value)
    def close(self):
        if self.process is not None and not self.process.stdin.closed:self.process.stdin.close()
        os.close(self.read_fd);os.close(self.write_fd)

class AudioWebsocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.identity=FixtureIdentity();self.factory=SyntheticFactory()
        self.hub=Hub(authenticator=self.identity)
        self.now=[1000.0]
        self.manager=RemoteManager(hyprland=Hyprland(INSTANCE,runner=FakeCompositor()),
            journal_dir=Path(self.temp.name)/'remote',
            encoder=EncoderLimits(4096,4096,16_777_216,60,40_000,2,2),
            vnc=VncBackend(FakeWayVNC.factory()),monotonic=lambda:self.now[0])
        self.service=create_service(self.hub,demo=True,audio_session_factory=self.factory,remote_manager=self.manager)
        self.server=NetworkServer(self.service,allow_loopback_http=True);await self.server.start()
        self.url=f'http://127.0.0.1:{self.server.bound_port}';self.http=aiohttp.ClientSession()
        self.headers={'Authorization':'Bearer owner-fixture'};self.websockets=[]
        self.session=await self.new_session()
        self.path='/v1/remote/sessions/'+self.session['id']+'/audio'

    async def new_session(self):
        async with self.http.post(self.url+'/v1/remote/sessions',headers=self.headers,
                                  json=dict(profile_request(),backend='vnc',ttl_seconds=30)) as r:
            self.assertEqual(r.status,201)
            return (await r.json())['session']
    async def asyncTearDown(self):
        for backend in self.factory.instances:backend.fail_cleanup=False
        for ws in self.websockets:await ws.close()
        await self.http.close();await self.server.close();await self.service.close_media();self.temp.cleanup()
    def validate_wire(self, kind, value):
        import jsonschema
        schema=json.loads((Path(__file__).resolve().parents[1]/'contracts/audio-input.schema.json').read_text())
        jsonschema.Draft202012Validator({**schema, '$ref':'#/$defs/'+kind}).validate(value)

    async def connect(self, token='owner-fixture'):
        ws=await self.http.ws_connect(self.url+self.path,headers={'Authorization':'Bearer '+token})
        self.websockets.append(ws);return ws
    async def begin(self, generation=1):
        ws=await self.connect();await ws.send_json({'generation':generation,'format':'s16le','rate':48000,'channels':1,'frame_samples':960})
        response=await ws.receive_json(timeout=2);self.assertEqual(response['type'],'begun',response)
        self.validate_wire('begun',response)
        for key,value in FORMAT.items():self.assertEqual(response[key],value)
        return ws
    async def wait_closed_backend(self, index=0):
        for _ in range(100):
            if self.factory.instances[index].closed:return
            await asyncio.sleep(.01)
        self.fail('backend cleanup did not complete')

    async def test_capability_and_idle_socket_never_create_sink(self):
        async with self.http.get(self.url+'/v1/audio/input',headers=self.headers) as r:
            value=await r.json();self.validate_wire('capability',value);self.assertTrue(value['supported']);self.assertTrue(value['available']);self.assertFalse(value['active'])
        ws=await self.connect();await asyncio.sleep(.01);self.assertEqual(self.factory.instances,[])
        await ws.close();self.assertEqual(self.factory.instances,[])
        ws=await self.begin(1)
        async with self.http.get(self.url+'/v1/audio/input',headers=self.headers) as r:self.assertTrue((await r.json())['active'])
        await ws.send_json({'type':'end','generation':1,'reason':'user_disabled'})
        self.assertEqual((await ws.receive_json())['type'],'ended');await self.wait_closed_backend()

    async def test_three_frames_backpressure_local_drops_and_ack_ordinals(self):
        ws=await self.begin(4);frames=[bytes([i])*1920 for i in (10,20,90)]
        # Capture seq10/20/90 illustrates local skipped frames: only actual wire
        # sends consume the server receive ordinal1/2/3.
        for seq,frame in enumerate(frames,1):
            await ws.send_bytes(frame);self.assertEqual(await ws.receive_json(),{'type':'accepted','generation':4,'sequence':seq})
        self.assertEqual(list(self.factory.instances[0].frames),frames)
        await ws.send_bytes(b'x'*1920)
        self.assertEqual(await ws.receive_json(),{'type':'rejected','generation':4,'sequence':4,'reason':'audio_input_backpressure'})
        self.assertEqual(len(self.factory.instances[0].frames),3)
        self.factory.instances[0].frames.popleft()
        await ws.send_bytes(b'z'*1920)
        self.assertEqual(await ws.receive_json(),{'type':'accepted','generation':4,'sequence':5})
        await ws.close();await self.wait_closed_backend()

    async def test_missing_auth_other_owner_and_unavailable_backend_no_sink(self):
        for headers,code in (({},401),({'Authorization':'Bearer other-fixture'},403)):
            with self.assertRaises(aiohttp.WSServerHandshakeError) as error:
                await self.http.ws_connect(self.url+self.path,headers=headers)
            self.assertEqual(error.exception.status,code)
        self.factory.ready=False;self.service.remote.audio._probe_time=0
        with self.assertRaises(aiohttp.WSServerHandshakeError) as error:await self.connect()
        self.assertEqual(error.exception.status,503);self.assertEqual(self.factory.instances,[])
        async with self.http.get(self.url+'/v1/audio/input',headers=self.headers) as r:
            value=await r.json();self.assertFalse(value['available']);self.assertFalse(value['active'])

    async def test_strict_begin_formats_fields_and_binary_before_begin(self):
        for value in ({'generation':True,'format':'s16le','rate':48000,'channels':1,'frame_samples':960},
                      {'generation':1,'format':'s16le','rate':48000,'channels':2,'frame_samples':960},
                      {'generation':1,'format':'s16le','rate':48000,'channels':1,'frame_samples':960,'authorized':True}):
            ws=await self.connect();await ws.send_json(value)
            self.assertEqual((await ws.receive_json())['type'],'rejected');await ws.close()
        ws=await self.connect();await ws.send_bytes(b'0'*1920)
        error=await ws.receive_json();self.assertEqual(error['reason'],'audio_input_begin_required');self.assertEqual(error['sequence'],1)
        self.assertEqual(self.factory.instances,[])

    async def test_oversize_frame_rejected_once_then_cleanup(self):
        ws=await self.begin(7);await ws.send_bytes(b'x'*1921)
        error=await ws.receive_json();self.assertEqual(error,{'type':'rejected','generation':7,'sequence':1,'reason':'audio_input_frame_invalid'})
        await ws.close();await self.wait_closed_backend()

    async def test_stale_generation_foreign_end_and_stale_socket_cannot_clear_new_session(self):
        audio=self.service.remote.audio;ws=await self.begin(10);original=audio.active
        for device,generation in [('other',10),('owner',9)]:
            with self.assertRaises(AudioUplinkError):
                await audio.end(device,self.session['id'],generation,'session_end',lambda:None)
            self.assertIs(audio.active,original);self.assertFalse(original.backend.closed)
        old_channel=original.uplink.channel_id
        await ws.send_json({'type':'end','generation':10,'reason':'route_changed'});self.assertEqual((await ws.receive_json())['type'],'ended')
        stale=await self.connect();await stale.send_json({'generation':10,'format':'s16le','rate':48000,'channels':1,'frame_samples':960})
        self.assertEqual((await stale.receive_json())['reason'],'audio_input_generation_stale');await stale.close()
        replacement=await self.begin(11);current=audio.active
        await audio.close_channel(old_channel);self.assertIs(audio.active,current)
        await replacement.close();await self.wait_closed_backend(1)

    async def test_session_release_no_nested_lock_and_idle_expiry_cleanup(self):
        ws=await self.begin(1)
        async with self.http.delete(self.url+'/v1/remote/sessions/'+self.session['id'],headers=self.headers) as r:
            self.assertEqual(r.status,200);self.assertTrue((await r.json())['released'])
        await self.wait_closed_backend();await ws.close()
        self.session=await self.new_session()
        self.path='/v1/remote/sessions/'+self.session['id']+'/audio'
        ws=await self.begin(1);self.now[0]+=120
        await self.wait_closed_backend(1);self.assertIsNone(self.service.remote.audio.active)

    async def test_cleanup_failure_retains_owned_state_for_retry(self):
        ws=await self.begin(1);backend=self.factory.instances[0];backend.fail_cleanup=True
        await ws.close();await asyncio.sleep(.04)
        self.assertIsNotNone(self.service.remote.audio.active);self.assertTrue(self.service.remote.audio.active.ending)
        backend.fail_cleanup=False;await self.service.remote.audio.maintain()
        self.assertIsNone(self.service.remote.audio.active);self.assertTrue(backend.closed)

    async def test_shared_ipc_transport_close_does_not_end_live_websocket(self):
        ipc=JsonLineServer(self.hub,str(Path(self.temp.name)/'core.sock'));await ipc.start()
        ws=await self.begin(1);await ipc.close()
        await ws.send_bytes(b'1'*1920);self.assertEqual((await ws.receive_json())['type'],'accepted')
        self.assertFalse(self.factory.instances[0].closed)
        await ws.close();await self.wait_closed_backend()

    async def test_same_device_stale_generation_frame_and_other_owner_api_do_not_mutate(self):
        ws=await self.begin(3);active=self.service.remote.audio.active
        with self.assertRaises(AudioUplinkError):
            await self.service.remote.audio.accept('owner',self.session['id'],2,b'0'*1920,lambda:None)
        with self.assertRaises(AudioUplinkError):
            await self.service.remote.audio.accept('other',self.session['id'],3,b'0'*1920,lambda:None)
        self.assertEqual(len(active.backend.frames),0);self.assertIs(self.service.remote.audio.active,active)
        await ws.close();await self.wait_closed_backend()

    async def test_malformed_json_wrong_generation_end_and_same_lease_competing_socket(self):
        ws=await self.begin(3);other=await self.connect()
        await other.send_json({'generation':4,'format':'s16le','rate':48000,'channels':1,'frame_samples':960})
        self.assertEqual((await other.receive_json())['reason'],'audio_input_busy');await other.close()
        await ws.send_bytes(b'1'*1920);self.assertEqual((await ws.receive_json())['sequence'],1)
        await ws.send_json({'type':'end','generation':2})
        self.assertEqual((await ws.receive_json())['reason'],'audio_input_generation_mismatch')
        await ws.close();await self.wait_closed_backend()
        malformed=await self.connect();await malformed.send_str('{"generation":4,"generation":5}')
        self.assertEqual((await malformed.receive_json())['type'],'rejected');await malformed.close()
        self.assertEqual(len(self.factory.instances),1)

    async def test_existing_authorization_rechecked_during_idle_channel(self):
        ws=await self.begin(1);self.identity.valid=False
        error=await ws.receive_json(timeout=2)
        self.assertEqual(error['reason'],'permission_denied')
        await ws.close();await self.wait_closed_backend()

    async def test_backend_readiness_probe_never_instantiates_backend(self):
        from unittest.mock import patch
        from omodachi_core.audio_uplink import PulseVirtualInputFactory
        factory=PulseVirtualInputFactory()
        with patch('omodachi_core.audio_uplink.sys.platform','linux'),patch('omodachi_core.audio_uplink.shutil.which',return_value='/fixture/tool'),\
             patch('omodachi_core.audio_uplink.Path.stat') as path_stat,patch('omodachi_core.audio_uplink.subprocess.run') as run:
            import stat
            path_stat.return_value.st_mode=stat.S_IFSOCK | 0o600;path_stat.return_value.st_uid=os.getuid()
            run.return_value.returncode=0
            self.assertTrue(factory.available())
            self.assertEqual(run.call_args.args[0][0],'pactl');self.assertEqual(run.call_args.args[0][-1],'info')
            self.assertTrue(run.call_args.args[0][1].startswith('--server=unix:'))
            self.assertEqual(run.call_args.kwargs['stdout'],__import__('subprocess').DEVNULL)
            run.return_value.returncode=1
            self.assertFalse(factory.available())
        self.assertEqual(self.factory.instances,[])

    async def test_real_virtual_input_three_pcm_frames_pipe_readback_and_owned_cleanup(self):
        fixture=PipePulseFixture();self.service.remote.audio.factory=fixture;self.service.remote.audio._probe_time=0
        ws=None
        try:
            ws=await self.begin(1);frames=[bytes([value])*1920 for value in (17,34,51)]
            for ordinal,frame in enumerate(frames,1):
                await ws.send_bytes(frame);self.assertEqual(await ws.receive_json(),{'type':'accepted','generation':1,'sequence':ordinal})
            self.assertEqual(await asyncio.to_thread(fixture.read_exact,5760),b''.join(frames))
            self.assertEqual(len(fixture.modules),2)
            await ws.send_json({'type':'end','generation':1,'reason':'route_changed'})
            self.assertEqual((await ws.receive_json())['type'],'ended')
            self.assertEqual(fixture.modules,{})
            self.assertEqual([a[2] for a in fixture.calls if a[1]=='unload-module'],['42','41'])
            self.assertIsNone(self.service.remote.audio.active)
        finally:
            if ws is not None:await ws.close()
            fixture.close()

    async def test_continuous_pcm_writer_failure_cleans_source_then_allows_new_generation(self):
        fixture=PipePulseFixture();audio=self.service.remote.audio;audio.factory=fixture;audio._probe_time=0
        ws=None;replacement=None
        try:
            ws=await self.begin(1)
            # 100 consecutive 20ms frames, with a real OS-pipe consumer. No
            # microphone/Pulse server is opened, and no PCM is saved or logged.
            for ordinal in range(1,101):
                frame=bytes([ordinal%251])*1920
                await ws.send_bytes(frame)
                self.assertEqual((await ws.receive_json())['type'],'accepted')
                self.assertEqual(await asyncio.to_thread(fixture.read_exact,1920),frame)
                await asyncio.sleep(.02)
            self.assertEqual(fixture.instances[0].stats()['written_frames'],100)
            fixture.process.terminate()  # playback process dies between frames
            error=await ws.receive_json(timeout=2)
            self.assertEqual(error['type'],'rejected')
            self.assertEqual(error['reason'],'audio_input_not_active')
            self.assertIsNone(audio.active);self.assertEqual(fixture.modules,{})
            replacement=await self.begin(2)
            await replacement.send_bytes(bytes([7])*1920)
            self.assertEqual((await replacement.receive_json())['type'],'accepted')
            self.assertEqual(await asyncio.to_thread(fixture.read_exact,1920),bytes([7])*1920)
            await replacement.send_json({'type':'end','generation':2,'reason':'user_disabled'})
            self.assertEqual((await replacement.receive_json())['type'],'ended')
            self.assertEqual(fixture.modules,{})
        finally:
            if ws:await ws.close()
            if replacement:await replacement.close()
            if audio.active:await audio.close_channel(audio.active.uplink.channel_id)
            fixture.close()
