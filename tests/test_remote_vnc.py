import io
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from omodachi_core.remote.vnc import ManagedWayVNC,HostBackendPreference

class WayVNCRuntimeTests(unittest.TestCase):
    def test_fixed_output_disable_resize_loopback_fd_and_real_control_shapes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);calls=[];client_count=[0]
            class Process:
                stderr=io.BytesIO(b'');ended=False
                def poll(self):return 0 if self.ended else None
                def terminate(self):self.ended=True
                def kill(self):self.ended=True
                def wait(self,timeout):return 0
            process=Process()
            def spawn(argv,**kwargs):
                calls.append(argv);fd=kwargs['pass_fds'][0]
                borrowed=socket.fromfd(fd,socket.AF_INET,socket.SOCK_STREAM)
                try:self.assertEqual(borrowed.getsockname()[0],'127.0.0.1')
                finally:borrowed.close()
                self.assertIn('-R',argv);self.assertEqual(argv[argv.index('-o')+1],'OMODACHI-test')
                return process
            def run(argv,**kwargs):
                if argv[-1]=='--version':return SimpleNamespace(returncode=0,stdout='wayvnc: v0.10.1')
                if argv[-1]=='output-list':data=[{'name':'OMODACHI-test','description':'Owned','height':640,'width':400,'captured':True,'power':'ON'}]
                elif argv[-1]=='client-list':data=[{'id':'1','address':'127.0.0.1'}]*client_count[0]
                else:raise AssertionError(argv)
                return SimpleNamespace(returncode=0,stdout=json.dumps({'code':0,'data':data}))
            vnc=ManagedWayVNC(root,environment=lambda:{},process_factory=spawn,command_runner=run)
            result=vnc.start('OMODACHI-test',{'width':800,'height':1280},{'width':400,'height':640})
            # Host-internal endpoint only: the loopback port is what core's own
            # WSS bridge dials, and it is never part of a client document.
            self.assertEqual(result['host'],'127.0.0.1');self.assertFalse(result['automatic_resizing'])
            self.assertEqual(result['logical_size'],{'width':400,'height':640})
            self.assertNotIn('transport',result);self.assertNotIn('framebuffer_pixels',result)
            self.assertFalse(vnc.verify_frame({'width':800,'height':1280}))
            client_count[0]=1;self.assertTrue(vnc.verify_frame({'width':800,'height':1280}))
            self.assertFalse(vnc.verify_frame({'width':1280,'height':800}))
            self.assertTrue(vnc.stop());self.assertTrue(process.ended)
    def test_backend_choice_is_explicit_and_persistent(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'choice.json';store=HostBackendPreference(path)
            self.assertEqual(store.get(),'sunshine');self.assertFalse(path.exists())
            store.set('vnc');self.assertEqual(HostBackendPreference(path).get(),'vnc')

    def test_cleanup_exact_owned_dead_control_socket(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);control=root/'control.sock'
            dead=socket.socket(socket.AF_UNIX);dead.bind(str(control));dead.close()
            (root/'instance.json').write_text(json.dumps({'output':'OMODACHI-test','port':5901,'pixels':{'width':800,'height':1280}}))
            vnc=ManagedWayVNC(root,environment=lambda:{})
            self.assertTrue(vnc.stop());self.assertFalse(control.exists());self.assertTrue((root/'instance.json').exists())

    def test_owned_process_stop_cleans_stale_socket_before_next_start(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);control=root/'control.sock'
            dead=socket.socket(socket.AF_UNIX);dead.bind(str(control));dead.close()
            class Process:
                ended=False
                def poll(self):return 0 if self.ended else None
                def terminate(self):self.ended=True
                def wait(self,timeout):return 0
            process=Process();vnc=ManagedWayVNC(root,environment=lambda:{})
            vnc.process=process
            self.assertTrue(vnc.stop());self.assertTrue(process.ended);self.assertIsNone(vnc.process)
            self.assertFalse(control.exists())
            # No previous_instance_running error is left for the next generation.
            self.assertTrue(vnc.stop())
