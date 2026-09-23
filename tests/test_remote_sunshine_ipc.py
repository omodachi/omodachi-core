"""Real private Unix-socket framing; simulated peer, no host service access."""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from omodachi_core.remote import host_settings
from omodachi_core.remote.errors import RemoteError
from omodachi_core.remote.sunshine import SunshineDesktopIPC,PROTOCOL,read_private_json


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='omd-ipc-');self.root=Path(self.temp.name)
        self.path=self.root/'p.sock';self.listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.listener.bind(str(self.path));os.chmod(self.path,0o600);self.listener.listen(1)
        self.listener.settimeout(2);self.thread=None;self.received=None
    def tearDown(self):
        self.listener.close()
        if self.thread:self.thread.join(timeout=3)
        self.temp.cleanup()
    def serve(self,response,delay=0):
        def worker():
            try:
                connection,_=self.listener.accept()
                with connection:
                    connection.settimeout(2)
                    raw=b''
                    while not raw.endswith(b'\n'):
                        chunk=connection.recv(4096)
                        if not chunk:return
                        raw+=chunk
                    self.received=json.loads(raw)
                    if delay:time.sleep(delay)
                    connection.sendall(response)
            except (OSError,ValueError):pass
        self.thread=threading.Thread(target=worker);self.thread.start()
    def invoke(self,op,**fields):
        # Production peer process checks are tested separately from byte framing.
        with patch.object(SunshineDesktopIPC,'_verify_peer'):
            return SunshineDesktopIPC(self.path,timeout=.15).request(op,**fields)
    def test_exact_fork_media_get_wire_has_no_extra_protocol_field(self):
        self.serve(b'{"ok":true,"session":{"session_id":"media-1","client_presented":null}}\n')
        result=self.invoke('media.get',session_id='media-1')
        self.assertEqual(self.received,{'op':'media.get','session_id':'media-1'})
        self.assertIsNone(result['client_presented'])
    def test_versioned_control_and_version_mismatch(self):
        self.serve(json.dumps({'protocol':PROTOCOL,'ok':True,'result':{'available':True}}).encode()+b'\n')
        self.assertTrue(self.invoke('desktop.status')['available'])
        self.assertEqual(self.received,{'op':'desktop.status','protocol':PROTOCOL})
    def test_old_pairing_only_endpoint_cannot_be_promoted_to_ready(self):
        self.serve(b'{"ok":false,"error":{"code":"unknown_operation"}}\n')
        with self.assertRaisesRegex(RemoteError,'sunshine_ipc_version_mismatch'):self.invoke('desktop.status')
    def test_bounded_timeout_incomplete_and_extra_line_rejection(self):
        self.serve(b'{}\n',delay=.3)
        start=time.monotonic()
        with self.assertRaisesRegex(RemoteError,'sunshine_ipc_unavailable'):self.invoke('media.list')
        self.assertLess(time.monotonic()-start,.5)
    def test_oversize_reply_is_rejected(self):
        self.serve(b'x'*32769+b'\n')
        with self.assertRaisesRegex(RemoteError,'sunshine_ipc_response_limit'):self.invoke('media.list')
    def test_non_private_endpoint_rejected_before_request(self):
        os.chmod(self.path,0o666)
        with self.assertRaisesRegex(RemoteError,'sunshine_ipc_unsafe'):self.invoke('media.list')
        self.assertIsNone(self.received)
    def test_no_free_shell_pairing_or_unscoped_stop_operation(self):
        with self.assertRaisesRegex(RemoteError,'sunshine_ipc_operation_invalid'):self.invoke('stop_all')
        self.assertIsNone(self.received)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_protocol_and_socket_directory_match_the_fork(self):
        self.assertEqual(PROTOCOL,'omodachi.sunshine.desktop.v1')
        self.assertTrue(host_settings(self.root)['sunshine_socket'].endswith('/omodachi-sunshine/pairing.sock'))
    def test_installed_defaults_need_no_configuration_file(self):
        value=host_settings(self.root)
        self.assertEqual(value['hyprland_instance'],'auto')
        self.assertEqual(value['journal_dir'],str(self.root/'.local/state/omodachi/remote'))
        self.assertEqual(value['encoder_limits']['codecs'],['h264'])
        self.assertFalse((self.root/'.local').exists())
    def test_a_private_config_file_overrides_the_defaults(self):
        path=self.root/'.config/omodachi/desktop-runtime.json';path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'version':1,'render_density':1.5,'journal_dir':'/tmp/x'}));path.chmod(0o600)
        value=host_settings(self.root)
        self.assertEqual((value['render_density'],value['journal_dir']),(1.5,'/tmp/x'))
    def test_an_unknown_config_field_is_refused(self):
        path=self.root/'.config/omodachi/desktop-runtime.json';path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'version':1,'nonsense':True}));path.chmod(0o600)
        with self.assertRaisesRegex(RemoteError,'remote_config_invalid'):host_settings(self.root)
    def test_duplicate_or_symlink_config_rejected(self):
        path=self.root/'config.json';path.write_text('{"version":1,"version":2}');path.chmod(0o600)
        with self.assertRaisesRegex(RemoteError,'desktop_config_invalid'):read_private_json(path)
        alias=self.root/'alias';alias.symlink_to(path)
        with self.assertRaises(OSError):read_private_json(alias)
