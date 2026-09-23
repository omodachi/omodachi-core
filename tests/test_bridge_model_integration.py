from __future__ import annotations
import asyncio
import json
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest

from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.ipc import JsonLineServer
from omodachi_core.plugin_bridge import BridgeError, encode_frame, watch_once

ROOT=Path(__file__).resolve().parents[1]
DRIVER=ROOT.parent/'omodachi-plugin/tests/model_frame_driver.mjs'

class BridgeModelIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_unix_bridge_frames_drive_plugin_model_with_filtered_gap_and_resync(self):
        with tempfile.TemporaryDirectory(prefix='omodachi-bridge-') as d:
            hub=Hub(auth_check_interval=.05)
            token=hub.register_device('plugin')
            service=create_service(hub,demo=True)
            server=JsonLineServer(hub,str(Path(d)/'hub.sock'))
            await server.start()
            frames=[]
            try:
                task=asyncio.create_task(watch_once(str(Path(d)/'hub.sock'),token,frames.append,timeout=1))
                for _ in range(100):
                    if frames: break
                    await asyncio.sleep(.01)
                self.assertTrue(frames and frames[0]['ok'])
                initial=frames[0]['result']
                # Private event is not visible to the plugin. The broadcast after
                # it must carry after_cursor=initial.event_cursor.
                hub.publish('private.action',{'secret':'never serialized to plugin'},device_id='other')
                hub.update_state({'bar':initial['bar']},event_type='bar.changed')
                for _ in range(100):
                    if len(frames)>=2: break
                    await asyncio.sleep(.01)
                self.assertGreaterEqual(len(frames),2)
                self.assertEqual(frames[1]['event']['type'],'bar.changed')
                self.assertEqual(frames[1]['after_cursor'],initial['event_cursor'])
                self.assertNotIn('secret',json.dumps(frames))
                hub.publish('resync.required',{'snapshot_required':True,'since':initial['event_cursor']},device_id='plugin')
                await asyncio.wait_for(task,1)
                self.assertEqual(frames[-1]['event']['type'],'resync.required')
                self.assertIn('after_cursor',frames[-1])
            finally:
                if not task.done(): task.cancel()
                await asyncio.gather(task,return_exceptions=True)
                await server.close()
            # Cross-repository: the frames this bridge just produced are fed
            # through the plugin's own model. A shape core changes on its side
            # fails here instead of on the host.
            if not DRIVER.exists() or shutil.which('node') is None:
                self.skipTest('../omodachi-plugin or node is not available')
            payload=json.dumps({'snapshot':frames[0]['result'],'frames':frames[1:]})
            proc=subprocess.run(['node',str(DRIVER)],input=payload,text=True,capture_output=True,check=True)
            model=json.loads(proc.stdout)
            self.assertEqual([item['kind'] for item in model['results']],['event','resync'])
            self.assertIsNone(model['snapshot'])
            self.assertEqual(model['state'],'loading')

    def test_bridge_utf8_byte_limit_is_explicit(self):
        small={'ok':True,'result':{'payload':'😀'*500000}}
        self.assertGreater(len(encode_frame(small).encode()),1_000_000)
        with self.assertRaises(BridgeError): encode_frame({'ok':True,'result':{'payload':'😀'*1_100_000}})

if __name__=='__main__': unittest.main()
