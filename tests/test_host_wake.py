import asyncio
import json
from pathlib import Path
import signal
import tempfile
import unittest
from omodachi_core.host_wake import HostWake,SCREENSAVER_CLASS
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
import aiohttp

class HostWakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.proc=Path(self.tmp.name)
        self.clients=[];self.dpms=True;self.signals=[];self.commands=[]
        def runner(argv,env):
            self.commands.append(argv)
            if argv==('/usr/bin/omarchy-system-wake',):self.dpms=True;return ''
            if argv[-1]=='clients':return json.dumps(self.clients)
            if argv[-2:]==('monitors','all'):return json.dumps([{'disabled':False,'dpmsStatus':self.dpms}])
            raise AssertionError(argv)
        def sender(pid,sig):self.signals.append((pid,sig));self.clients=[]
        self.adapter=HostWake(runner=runner,environment=lambda:{},proc=self.proc,sender=sender,idle_reader=lambda env:{'inIdleCycle':False,'screensaverWindows':0,'processes':{'wake':False}})
    def tearDown(self):self.tmp.cleanup()
    def process(self,pid,parent,argv):
        p=self.proc/str(pid);p.mkdir();(p/'stat').write_text(f'{pid} (fixture) S {parent} '+'0 '*17+str(pid)+'\n');(p/'cmdline').write_bytes(b'\0'.join(x.encode() for x in argv)+b'\0')
    def test_awake_tap_not_consumed_or_injected(self):
        value=self.adapter.wake();self.assertFalse(value['consume_tap']);self.assertEqual(self.signals,[])
        self.assertNotIn(('/usr/bin/omarchy-system-wake',),self.commands)
    def test_only_actual_screensaver_window_official_child_gets_sigterm(self):
        self.clients=[{'pid':10,'class':SCREENSAVER_CLASS,'title':'private'}, {'pid':99,'class':'ordinary-terminal'}]
        self.process(10,1,['foot']);self.process(11,10,['/bin/bash','/usr/share/omarchy/bin/omarchy-screensaver'])
        self.process(20,1,['hyprlock']);self.process(99,1,['foot']);self.process(100,99,['/bin/bash','/usr/bin/omarchy-screensaver'])
        value=self.adapter.wake();self.assertTrue(value['exited']);self.assertTrue(value['consume_tap'])
        self.assertEqual(self.signals,[(11,signal.SIGTERM)]);self.assertIsNone(value['wake']['locked'])
        self.assertNotIn('private',json.dumps(value));self.assertNotIn(('/usr/bin/omarchy-system-wake',),self.commands)
    def test_wake_waits_for_official_idle_process_after_screensaver_window_exits(self):
        states=iter([{'inIdleCycle':True,'screensaverWindows':0,'processes':{'wake':True}},
                     {'inIdleCycle':False,'screensaverWindows':0,'processes':{'wake':True}},
                     {'inIdleCycle':False,'screensaverWindows':0,'processes':{'wake':False}}])
        self.adapter.idle_reader=lambda env:next(states)
        result=self.adapter.wake()
        self.assertTrue(result['consume_tap']);self.assertFalse(result['wake']['wake_pending'])
        self.assertNotIn(('/usr/bin/omarchy-system-wake',),self.commands)

    def test_dpms_off_uses_official_wake_and_consumes_first_tap(self):
        self.dpms=False;value=self.adapter.wake()
        self.assertTrue(value['dpms_woken']);self.assertTrue(value['consume_tap']);self.assertEqual(self.signals,[])
        self.assertIn(('/usr/bin/omarchy-system-wake',),self.commands)

class WakeHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_starting_a_remote_session_waits_for_official_wake_completion(self):
        hub=Hub();token=hub.register_device('phone');service=create_service(hub,demo=True)
        service.wake_adapter=type('Wake',(),{'inspect':lambda self:{'screensaver_active':False,'display_asleep':False,'wake_pending':True}})()
        server=NetworkServer(service,allow_loopback_http=True);await server.start()
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(f'http://127.0.0.1:{server.bound_port}/v1/remote/sessions',headers={'Authorization':'Bearer '+token},json={}) as r:
                    self.assertEqual(r.status,409);self.assertEqual((await r.json())['error']['code'],'host_waking')
                self.assertIsNone(hub.state_snapshot()['remote']['session_id'])
        finally:await server.close()

    async def test_authenticated_nonremote_wake_publishes_state_without_a_session(self):
        hub=Hub();token=hub.register_device('phone');service=create_service(hub,demo=True)
        class Adapter:
            def wake(self):return {'screensaver_was_active':True,'exited':True,'consume_tap':True,'dpms_woken':False,'wake':{'screensaver_active':False,'display_asleep':False,'locked':None}}
        service.wake_adapter=Adapter();server=NetworkServer(service,allow_loopback_http=True);await server.start()
        try:
            async with aiohttp.ClientSession() as client:
                url=f'http://127.0.0.1:{server.bound_port}/v1/control/wake'
                async with client.post(url,headers={'Authorization':'Bearer '+token},json={}) as r:
                    self.assertEqual(r.status,200);self.assertTrue((await r.json())['consume_tap'])
                self.assertIsNone(hub.state_snapshot()['remote']['session_id'])
                self.assertEqual(hub.state_snapshot()['wake'],{'screensaver_active':False,'display_asleep':False,'locked':None})
                async with client.post(url,headers={'Authorization':'Bearer '+token},json={'command':'ignored'}) as r:self.assertEqual(r.status,400)
        finally:await server.close()


class WakeReadingCacheTests(unittest.TestCase):
    """PERF-4 §0: one wake reading is five processes; it is not a per-frame fact."""

    def build(self):
        from omodachi_core.host_wake import HostWake
        now = {"value": 100.0}
        calls = {"n": 0}

        def runner(argv, env):
            calls["n"] += 1
            if argv[:3] == ("/usr/bin/hyprctl", "-j", "clients"):
                return "[]"
            if argv[:3] == ("/usr/bin/hyprctl", "-j", "monitors"):
                return '[{"disabled": false, "dpmsStatus": true}]'
            return "ok"

        wake = HostWake(runner=runner, environment=lambda: {}, sender=lambda *_: None,
                        idle_reader=lambda env: {"inIdleCycle": False, "screensaverWindows": 0,
                                                 "processes": {"wake": False}},
                        cache_seconds=5.0, clock=lambda: now["value"])
        return wake, now, calls

    def test_the_reading_is_taken_once_per_window(self):
        wake, now, calls = self.build()
        first = wake.inspect()
        for _ in range(9):
            self.assertEqual(wake.inspect(), first)
        taken = calls["n"]
        self.assertEqual(taken, 2, "one pass is two hyprctl reads plus the idle reader")
        now["value"] += 6.0
        wake.inspect()
        self.assertEqual(calls["n"], taken * 2)

    def test_an_explicit_fresh_reading_and_an_invalidate_both_reread(self):
        wake, now, calls = self.build()
        wake.inspect()
        taken = calls["n"]
        wake.inspect(fresh=True)
        self.assertGreater(calls["n"], taken)
        taken = calls["n"]
        wake.inspect()
        self.assertEqual(calls["n"], taken, "a fresh reading refills the cache")
        wake.invalidate()
        wake.inspect()
        self.assertGreater(calls["n"], taken)
