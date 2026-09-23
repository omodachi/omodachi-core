"""Ordinary host policy tests; fixed HTTP test identity, synthetic output only.

No credential/certificate fixture, pairing validation, live host or real media.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
import aiohttp
from omodachi_core.bootstrap import create_service
from omodachi_core.remote import RemoteManager
from omodachi_core.remote.backends import VncBackend
from omodachi_core.remote.hyprland import Hyprland
from omodachi_core.remote.profile import (DesktopProfilePlanner, EncoderLimits, OutputChoice, PixelSize,
                                          PointSize, QualityBudget)
from omodachi_core.hub import Hub
from omodachi_core.ipc import JsonLineServer
from omodachi_core.network import NetworkServer
from omodachi_core.preferences import HostPreferencesStore, PreferencesError, profile_defaults
from tests.remote_fakes import FakeCompositor, FakeWayVNC, INSTANCE, profile_request
from tests.test_remote_profile import encoder, request

class PreferencePolicyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'prefs'/'state.json'
        self.store=HostPreferencesStore(self.path)
    def tearDown(self):self.temp.cleanup()
    def test_private_atomic_store_cas_noop_restart(self):
        initial=self.store.get();self.assertEqual(initial['revision'],0)
        self.assertTrue(initial['values']['allow_dynamic_resolution'])
        saved=self.store.set(expected_revision=0,changes={'allow_dynamic_resolution':False,'quality':'performance'})
        self.assertEqual(saved['revision'],1)
        self.assertEqual(HostPreferencesStore(self.path).get(),saved)
        self.assertEqual(self.path.stat().st_mode&0o777,0o600)
        self.assertEqual(self.path.parent.stat().st_mode&0o777,0o700)
        self.assertEqual(self.store.set(expected_revision=1,changes={'quality':'performance'})['revision'],1)
        with self.assertRaisesRegex(PreferencesError,'preferences_revision_conflict'):
            self.store.set(expected_revision=0,changes={'quality':'quality'})
    def test_legacy_migration_removes_every_resolution_cap_and_follow_default(self):
        for resolution in ('auto','720p','1080p'):
            self.path.parent.mkdir(mode=0o700,exist_ok=True)
            self.path.write_text(json.dumps({'version':1,'revision':7,'values':{'resolution':resolution,
                'follow_orientation':False,'quality':'quality','host_audio_playback':True}}));self.path.chmod(0o600)
            current=self.store.get();self.assertEqual(current['revision'],7)
            self.assertEqual(current['values'],{'allow_dynamic_resolution':True,'quality':'quality',
                                                'host_audio_playback':True,'voice_uplink':False,
                                                'remote_backend':'sunshine','pairing_mode':'open',
                                                'biometric_auth':False,'clipboard_sync':'off'})
            self.assertEqual(current['profile_defaults']['quality'],{'fps':60,'bitrate_kbps':20000})
            self.assertEqual(current['profile_defaults']['preset'],'quality')
            raw=self.path.read_text();self.assertNotIn('max_pixels',raw);self.assertNotIn('follow_orientation',raw)
            self.assertNotIn('720p',raw);self.assertNotIn('1080p',raw)
            self.assertEqual(json.loads(raw)['version'],2)
    def test_open_pairing_is_the_default_and_invite_is_the_only_other_answer(self):
        # PAIR-2: the handshake's boundary is the local Approve, so the shipped
        # host does not ask for an invitation. `invite` is a deliberate lock.
        self.assertEqual(self.store.get()['values']['pairing_mode'],'open')
        self.assertEqual(self.store.set(expected_revision=0,changes={'pairing_mode':'invite'})
                         ['values']['pairing_mode'],'invite')
        for bad in ('Open','none','',None,True):
            with self.assertRaises(PreferencesError):
                self.store.set(expected_revision=1,changes={'pairing_mode':bad})
        # A store written before this control existed still loads, with open.
        self.path.write_text(json.dumps({'version':2,'revision':3,'values':{
            'allow_dynamic_resolution':True,'quality':'balanced','host_audio_playback':False,
            'voice_uplink':False,'remote_backend':'sunshine'}}));self.path.chmod(0o600)
        self.assertEqual(self.store.get()['values']['pairing_mode'],'open')
    def test_clip1_the_clipboard_is_off_until_the_host_says_otherwise(self):
        # CLIP-1: a clipboard carries passwords and one-time codes, so the
        # shipped host shares nothing. `host_to_device` is the one-way half and
        # `both` is the whole of it; there is no fourth answer.
        self.assertEqual(self.store.get()['values']['clipboard_sync'],'off')
        for mode in ('host_to_device','both','off'):
            current=self.store.set(expected_revision=self.store.get()['revision'],
                                   changes={'clipboard_sync':mode})
            self.assertEqual(current['values']['clipboard_sync'],mode)
        for bad in ('device_to_host','on','','Both',None,True,1):
            with self.assertRaises(PreferencesError):
                self.store.set(expected_revision=self.store.get()['revision'],
                               changes={'clipboard_sync':bad})
        # A store written before this control existed still loads, and it loads
        # as off: an old host does not start sharing a clipboard on upgrade.
        self.path.write_text(json.dumps({'version':2,'revision':3,'values':{
            'allow_dynamic_resolution':True,'quality':'balanced','host_audio_playback':False,
            'voice_uplink':False,'remote_backend':'sunshine'}}));self.path.chmod(0o600)
        self.assertEqual(self.store.get()['values']['clipboard_sync'],'off')

    def test_the_host_default_backend_is_sunshine_and_only_the_two_are_valid(self):
        # Study 03 open question 5: core said sunshine, the client said vnc.
        # One store answers it, and it is the store the plugin already writes.
        self.assertEqual(self.store.get()['values']['remote_backend'],'sunshine')
        current=self.store.set(expected_revision=0,changes={'remote_backend':'vnc'})
        self.assertEqual(current['values']['remote_backend'],'vnc')
        with self.assertRaises(PreferencesError):
            self.store.set(expected_revision=current['revision'],changes={'remote_backend':'moonlight'})
        with self.assertRaises(PreferencesError):
            self.store.set(expected_revision=current['revision'],changes={'remote_backend':True})

    def test_a_store_written_before_the_backend_default_existed_gains_it(self):
        self.path.parent.mkdir(mode=0o700,exist_ok=True)
        self.path.write_text(json.dumps({'version':2,'revision':9,'values':{'allow_dynamic_resolution':False,
            'quality':'balanced','host_audio_playback':True,'voice_uplink':True}}));self.path.chmod(0o600)
        current=self.store.get()
        self.assertEqual((current['revision'],current['values']['remote_backend']),(9,'sunshine'))

    def test_a_store_written_before_voice_uplink_existed_gains_its_default(self):
        self.path.parent.mkdir(mode=0o700,exist_ok=True)
        self.path.write_text(json.dumps({'version':2,'revision':4,'values':{'allow_dynamic_resolution':False,
            'quality':'balanced','host_audio_playback':True}}));self.path.chmod(0o600)
        current=self.store.get()
        self.assertEqual(current['revision'],4)
        self.assertIs(current['values']['voice_uplink'],False)
        self.assertIs(current['values']['allow_dynamic_resolution'],False)
        self.assertIs(json.loads(self.path.read_text())['values']['voice_uplink'],False)

    def test_pixel_budget_orientation_and_preset_controls_rejected(self):
        for changes in ({},{'allow_dynamic_resolution':1},{'resolution':'720p'},{'follow_orientation':True},
                        {'max_pixels':921600},{'microphone_enabled':True},{'quality':'sharp'}):
            with self.assertRaises(PreferencesError):self.store.set(expected_revision=0,changes=changes)
        self.assertEqual(set(profile_defaults(self.store.get()['values'])['quality']),{'fps','bitrate_kbps'})
    def test_the_device_choices_are_published_beside_the_host_default(self):
        # STREAM-1: a client shows the three presets and the custom range
        # without carrying a copy of either.
        self.store.set(expected_revision=0,changes={'quality':'performance'})
        defaults=self.store.get()['profile_defaults']
        self.assertEqual(defaults['quality'],{'fps':30,'bitrate_kbps':8000})
        self.assertEqual(defaults['preset'],'performance')
        self.assertEqual(defaults['presets'],{'performance':{'fps':30,'bitrate_kbps':8000},
                                              'balanced':{'fps':60,'bitrate_kbps':12000},
                                              'quality':{'fps':60,'bitrate_kbps':20000}})
        self.assertEqual(list(defaults['presets']),['performance','balanced','quality'])
        self.assertEqual(defaults['custom'],{'fps':[30,60],'min_bitrate_kbps':4000,'max_bitrate_kbps':40000})
    def test_competing_writers_only_one_revision(self):
        def update(value):
            try:return HostPreferencesStore(self.path).set(expected_revision=0,changes={'quality':value})['revision']
            except PreferencesError as e:return e.code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(update,['quality','performance']))
        self.assertCountEqual(results,[1,'preferences_revision_conflict'])
    def test_quality_defaults_never_change_remote_ratio_density_or_pixel_budget(self):
        for portrait in (False,True):
            size=lambda w,h:PixelSize(h,w) if portrait else PixelSize(w,h)
            viewport=PointSize(600,800) if portrait else PointSize(800,600)
            choice=OutputChoice('verified',size(3200,2400),2.5,(size(3200,2400),size(1280,960)))
            planner=DesktopProfilePlanner((choice,),encoder=encoder())
            for preset in ('performance','balanced','quality'):
                defaults=profile_defaults({**self.store.get()['values'],'quality':preset})
                budget=QualityBudget(max_pixels=8_000_000,**defaults['quality'])
                target=planner.plan(request(portrait=portrait,viewport_points=viewport,quality=budget))
                self.assertEqual(target.stream_pixels.area,7_680_000)
                self.assertAlmostEqual(target.stream_pixels.aspect,viewport.aspect)
                self.assertEqual(target.logical_size,choice.logical_size)

class FixedIdentity:
    def verify(self,value):
        if value!='settings-fixture':raise ValueError('fixture identity unavailable')
        return 'remote-fixture'

class SettingsPolicyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=HostPreferencesStore(self.root/'prefs'/'state.json')
        self.compositor=FakeCompositor()
        self.manager=RemoteManager(hyprland=Hyprland(INSTANCE,runner=self.compositor),
            journal_dir=self.root/'remote',encoder=EncoderLimits(4096,4096,16_777_216,60,40_000,2,2),
            vnc=VncBackend(FakeWayVNC.factory()))
        self.hub=Hub(authenticator=FixedIdentity());self.service=create_service(self.hub,demo=True,
            preferences_store=self.store,remote_manager=self.manager)
        self.socket=str(self.root/'core.sock')
        self.ipc=JsonLineServer(self.hub,self.socket,local_handler=self.service.dispatch_local_async)
        await self.ipc.start()
        self.server=NetworkServer(self.service,allow_loopback_http=True);await self.server.start()
        self.url=f'http://127.0.0.1:{self.server.bound_port}';self.http=aiohttp.ClientSession()
    async def asyncTearDown(self):
        await self.http.close();await self.server.close();await self.ipc.close()
        await self.service.close_media();self.temp.cleanup()
    async def call(self,method,path,body=None):
        async with self.http.request(method,self.url+path,json=body,headers={'Authorization':'Bearer settings-fixture'}) as response:
            return response.status,await response.json()
    async def cli(self,*args):
        p=await asyncio.create_subprocess_exec(sys.executable,'-c','from omodachi_core.cli import host_main; raise SystemExit(host_main())',
            '--socket',self.socket,'preferences',*args,stdout=asyncio.subprocess.PIPE)
        out,_=await p.communicate();return p.returncode,json.loads(out)
    async def save(self,**changes):
        args=['set','--revision',str(self.store.get()['revision'])]
        for name,value in changes.items():args.extend(['--'+name.replace('_','-'),str(value).lower() if type(value) is bool else value])
        code,result=await self.cli(*args);self.assertEqual(code,0,result);return result['result']
    async def start(self,**payload):
        code,value=await self.call('POST','/v1/remote/sessions',dict(profile_request(),backend='vnc',**payload))
        self.assertEqual(code,201,value);self.session=value['session']
        self.base='/v1/remote/sessions/'+self.session['id'];return self.session
    async def resize(self,width,height,**extra):
        body={'expected_revision':self.session['revision'],'viewport_points':{'width':width,'height':height},
              'orientation':'portrait' if height>width else 'landscape_left',**extra}
        code,value=await self.call('POST',self.base+'/resize',body)
        if code==200:self.session=value['session']
        return code,value

    async def test_real_cli_live_policy_snapshot_readback_and_restart_store(self):
        _,initial=await self.cli('get');self.assertEqual(initial['result']['revision'],0)
        saved=await self.save(allow_dynamic_resolution=False,quality='performance',host_audio_playback=True)
        code,value=await self.call('GET','/v1/preferences');self.assertEqual(code,200)
        self.assertEqual(value['values'],saved['values'])
        self.assertEqual(value['permission_effect'],'subsequent_output_change_requests')
        self.assertNotIn('max_pixels',json.dumps(value));self.assertNotIn('follow_orientation',json.dumps(value))
        self.assertEqual(HostPreferencesStore(self.store.path).get()['values'],saved['values'])
        code,stale=await self.cli('set','--revision','0','--quality','quality');self.assertEqual(code,1)
        self.assertEqual(stale['error'],'preferences_revision_conflict')

    async def test_false_denies_a_geometry_resize_but_not_the_session_itself(self):
        await self.save(allow_dynamic_resolution=False)
        session=await self.start()
        self.assertEqual(session['state'],'ready')
        self.assertTrue((await self.call('GET','/v1/preferences'))[1]['runtime']['remote_available'])
        before=json.dumps(self.compositor.rows,sort_keys=True)
        code,error=await self.resize(834,1194)
        self.assertEqual(code,403,error);self.assertEqual(error['error']['code'],'dynamic_resolution_policy_denied')
        self.assertEqual(json.dumps(self.compositor.rows,sort_keys=True),before)
        self.assertEqual(self.hub.state_snapshot()['remote']['session_id'],session['id'])

    async def test_an_encoder_only_change_stays_allowed_while_geometry_is_locked(self):
        session=await self.start()
        await self.save(allow_dynamic_resolution=False)
        mode=session['profile']['output_mode_pixels']
        code,value=await self.resize(1194,834,quality={'max_pixels':1_000_000,'fps':30,'bitrate_kbps':8000})
        self.assertEqual(code,200,value)
        profile=value['session']['profile']
        self.assertEqual(profile['output_mode_pixels'],mode)
        # WayVNC serves the whole framebuffer, so only the rate caps move.
        self.assertEqual(profile['stream_pixels'],mode)
        self.assertEqual((profile['fps'],profile['bitrate_kbps']),(30,8000))

    async def test_policy_is_read_at_the_resize_not_frozen_at_session_start(self):
        await self.start()
        code,value=await self.resize(834,1194);self.assertEqual(code,200,value)
        await self.save(allow_dynamic_resolution=False)
        code,error=await self.resize(1194,834);self.assertEqual(code,403,error)
        await self.save(allow_dynamic_resolution=True)
        code,value=await self.resize(1194,834);self.assertEqual(code,200,value)

    async def test_existing_resources_available_and_http_preferences_readonly(self):
        for name in ('state','catalog','herdr','capabilities'):
            self.assertEqual((await self.call('GET','/v1/'+name))[0],200)
        self.assertEqual((await self.call('POST','/v1/preferences',{'quality':'quality'}))[0],405)


if __name__=='__main__':
    unittest.main()
