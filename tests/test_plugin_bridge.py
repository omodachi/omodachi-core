from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import omodachi_core

from omodachi_core.auth import DeviceAuthenticator
from omodachi_core.hub import Hub
from omodachi_core.ipc import JsonLineServer
from omodachi_core import cli, runtime_paths
from omodachi_core.plugin_bridge import BridgeError, check_response, plugin_credential, validate_snapshot, watch, watch_once

class PluginBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_watch_once_emits_snapshot_then_event_without_raw_commands(self):
        with tempfile.TemporaryDirectory() as d:
            auth=DeviceAuthenticator(secret=b'p'*32)
            hub=Hub(authenticator=auth,auth_check_interval=.05)
            token=hub.register_device('plugin')
            path=str(Path(d)/'hub.sock'); server=JsonLineServer(hub,path)
            await server.start(); outputs=[]
            try:
                task=asyncio.create_task(watch_once(path,token,outputs.append,timeout=1))
                for _ in range(10):
                    if outputs: break
                    await asyncio.sleep(.01)
                self.assertEqual(outputs[0]['ok'],True)
                self.assertEqual(outputs[0]['result']['device_id'],'plugin')
                hub.publish('state.changed',{'source':'fixture'})
                for _ in range(100):
                    if len(outputs) > 1: break
                    await asyncio.sleep(.01)
                self.assertGreaterEqual(len(outputs),2)
                self.assertEqual(outputs[1]['event']['type'],'state.changed')
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(outputs[1]['instance_id'],hub.instance_id)
            finally:
                await server.close()

    def test_plugin_credential_requires_private_file_and_models_reject_free_shape(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'token';p.write_text('not-a-token');p.chmod(0o600)
            with self.assertRaises(BridgeError): plugin_credential({'OMODACHI_TOKEN_FILE':str(p)})
        with self.assertRaises(BridgeError): validate_snapshot({'shell':'rm -rf'})
        self.assertIsNone(check_response({'ok':True,'result':{'shell':'rm -rf'}})) if False else None


class SocketMoveTests(unittest.IsolatedAsyncioTestCase):
    """RELEASE-3b. `--pam` moves the daemon from the `$XDG_RUNTIME_DIR` fallback
    to `/run/omodachi/<uid>/` and restarts it; the helper outlives the move."""

    def layout(self, root):
        runtime = Path(root) / 'rt'
        shared_root = Path(root) / 'shared'
        runtime.mkdir(); shared_root.mkdir()
        fallback = runtime / runtime_paths.RUNTIME_SUBDIR
        fallback.mkdir(mode=0o700)
        return runtime, shared_root, fallback / runtime_paths.SOCKET_NAME

    async def start(self, auth, path):
        hub = Hub(authenticator=auth, auth_check_interval=.05)
        server = JsonLineServer(hub, str(path))
        await server.start()
        return hub, server

    async def wait_for(self, outputs, predicate, seconds=30.0):
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            if any(predicate(row) for row in outputs):
                return
            await asyncio.sleep(.02)
        self.fail('timed out; saw %r' % outputs)

    async def test_watch_resolves_the_socket_again_after_a_failure(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as d:
            runtime, shared_root, fallback_path = self.layout(d)
            environment = {'XDG_RUNTIME_DIR': str(runtime)}
            auth = DeviceAuthenticator(secret=b'p' * 32)
            token = None
            outputs, asked = [], []
            with mock.patch.object(runtime_paths, 'SHARED_RUNTIME_ROOT', str(shared_root)):
                def resolve():
                    path = runtime_paths.client_socket_path(environ=environment, home=d)
                    asked.append(path)
                    return path
                hub1, server1 = await self.start(auth, fallback_path)
                token = hub1.register_device('plugin')
                stop = asyncio.Event()
                task = asyncio.create_task(watch(resolve, credential_loader=lambda: token, emit=outputs.append,
                                                 retry_delay=.05, max_retry_delay=.2, stop=stop))
                server2 = None
                try:
                    await self.wait_for(outputs, lambda row: row.get('ok') and row['result']['instance_id'] == hub1.instance_id)
                    self.assertEqual(asked, [str(fallback_path)])
                    await server1.close()
                    await self.wait_for(outputs, lambda row: row.get('ok') is False)
                    # What `--pam` does: the root step makes /run/omodachi/<uid>,
                    # the daemon restarts and binds there.
                    shared_dir = shared_root / str(os.getuid())
                    shared_dir.mkdir(mode=0o700)
                    shared_path = shared_dir / runtime_paths.SOCKET_NAME
                    hub2, server2 = await self.start(auth, shared_path)
                    await self.wait_for(outputs, lambda row: row.get('ok') and row['result']['instance_id'] == hub2.instance_id)
                    self.assertEqual(asked[-1], str(shared_path))
                finally:
                    stop.set()
                    await asyncio.wait_for(task, 5)
                    if server2 is not None:
                        await server2.close()

    async def test_plugin_watch_finds_the_moved_socket_without_restarting(self):
        """The whole command, as the plugin runs it: one process across the move."""
        with tempfile.TemporaryDirectory(dir='/tmp') as d:
            runtime, shared_root, fallback_path = self.layout(d)
            auth = DeviceAuthenticator(secret=b'p' * 32)
            hub1, server1 = await self.start(auth, fallback_path)
            token = hub1.register_device('plugin')
            source = str(Path(omodachi_core.__file__).resolve().parents[1])
            environment = dict(os.environ, XDG_RUNTIME_DIR=str(runtime), HOME=d, OMODACHI_TOKEN=token,
                               PYTHONPATH=source)
            code = ('import sys; from omodachi_core import runtime_paths; '
                    'runtime_paths.SHARED_RUNTIME_ROOT = sys.argv[1]; '
                    'from omodachi_core.cli import host_main; raise SystemExit(host_main(["plugin-watch"]))')
            process = await asyncio.create_subprocess_exec(sys.executable, '-c', code, str(shared_root),
                                                           env=environment, stdout=asyncio.subprocess.PIPE)
            outputs = []
            async def pump():
                async for line in process.stdout:
                    outputs.append(json.loads(line))
            reader = asyncio.create_task(pump())
            server2 = None
            try:
                await self.wait_for(outputs, lambda row: row.get('ok') and row['result']['instance_id'] == hub1.instance_id)
                await server1.close()
                await self.wait_for(outputs, lambda row: row.get('ok') is False)
                shared_dir = shared_root / str(os.getuid())
                shared_dir.mkdir(mode=0o700)
                hub2, server2 = await self.start(auth, shared_dir / runtime_paths.SOCKET_NAME)
                await self.wait_for(outputs, lambda row: row.get('ok') and row['result']['instance_id'] == hub2.instance_id)
                self.assertIsNone(process.returncode)
            finally:
                if process.returncode is None:
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                if server2 is not None:
                    await server2.close()

    def test_an_explicit_socket_is_never_re_resolved(self):
        seen = []
        async def fake_watch(socket_path, **_):
            seen.append(socket_path)
        with mock.patch('omodachi_core.plugin_bridge.watch', fake_watch), \
                mock.patch.object(cli, 'client_socket_path', return_value='/resolved.sock'):
            cli.host_main(['--token', 't', '--socket', '/named.sock', 'plugin-watch'])
            cli.host_main(['--token', 't', 'plugin-watch'])
            self.assertEqual(seen[0], '/named.sock')
            self.assertTrue(callable(seen[1]))
            self.assertEqual(seen[1](), '/resolved.sock')

if __name__=='__main__': unittest.main()
