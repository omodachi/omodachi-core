"""Ordinary quick-action adapter checks; filesystem/process effects are synthetic."""
from pathlib import Path
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from omodachi_core.catalog import compile_catalog
from omodachi_core.catalog_runtime import CatalogRuntime
from omodachi_core.hub import Hub
from omodachi_core.live_menu_adapter import (
    CommandResult, LiveMenuReaders, MenuReadUnavailable, QUICK_SOURCES,
    HYPR_FLAG_SETTER, HYPR_QUICK_FLAGS, HYPR_QUICK_COMMANDS, MUTATION_COMMANDS,
    HYPR_PACKAGED_BIN_LINKS,
    install_live_menu_adapters, bounded_command,
)
from omodachi_core.service import CoreService, ServiceError


class LiveQuickActionTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.home=Path(self.directory.name)
        self.calls=[];self.apply=True;self.returncode=0
        self.reader=LiveMenuReaders(home=self.home,runner=lambda *_:self.fail('Unexpected state command'),
            mutation_runner=self.mutate,graphical=lambda:{'XDG_RUNTIME_DIR':'/run/user/1000','WAYLAND_DISPLAY':'wayland-1'})
        self.service=CoreService(Hub(),runtime=CatalogRuntime(compile_catalog({key:QUICK_SOURCES[key] for key in HYPR_QUICK_FLAGS})))
        install_live_menu_adapters(self.service,readers=self.reader)
    def tearDown(self):self.directory.cleanup()
    def flag(self,entry):return self.home/'.local/state/omarchy/toggles/hypr'/(HYPR_QUICK_FLAGS[entry]+'.lua')
    def mutate(self,argv,environment):
        self.calls.append(argv)
        self.assertIn(argv,MUTATION_COMMANDS)
        self.assertEqual(argv[0],HYPR_FLAG_SETTER)
        self.assertEqual(environment['OMARCHY_PATH'],'/usr/share/omarchy')
        if self.apply:
            target=self.home/'.local/state/omarchy/toggles/hypr'/(argv[1]+'.lua')
            target.parent.mkdir(parents=True,exist_ok=True)
            if argv[2]=='on':target.write_text('-- synthetic known template\n')
            else:target.unlink(missing_ok=True)
        return CommandResult(self.returncode)
    def invoke(self,entry,request):
        snapshot=self.service.refresh_catalog(invalidate=True)
        row=next(row for row in snapshot['entries'] if row['id']==entry)
        self.assertTrue(row['route']['ready'])
        payload={'entry_id':entry,'request_id':request,'catalog_revision':snapshot['revision'],'params':{}}
        return payload,self.service.invoke(payload,'synthetic-device')

    def test_both_actions_apply_explicit_on_off_and_reverse_with_correct_polarity(self):
        for entry,flag in HYPR_QUICK_FLAGS.items():
            with self.subTest(entry=entry):
                before=self.reader.quick_state(entry)['value']
                _,result=self.invoke(entry,entry+'-one')
                self.assertEqual(result['status'],'accepted')
                self.assertEqual(self.calls[-1],(HYPR_FLAG_SETTER,flag,'on'))
                self.assertEqual(self.reader.quick_state(entry)['value'],not before)
                self.assertTrue(self.flag(entry).is_file())
                _,result=self.invoke(entry,entry+'-two')
                self.assertEqual(result['status'],'accepted')
                self.assertEqual(self.calls[-1],(HYPR_FLAG_SETTER,flag,'off'))
                self.assertEqual(self.reader.quick_state(entry)['value'],before)
                self.assertFalse(self.flag(entry).exists())

    def test_accepted_request_retry_does_not_repeat_mutation_or_reload(self):
        payload,result=self.invoke('trigger.toggle.window-gaps','same-request')
        self.assertEqual(self.service.invoke(payload,'synthetic-device'),result)
        self.assertEqual(len(self.calls),1)

    def test_reload_failure_after_flag_change_is_failed_and_never_automatically_retried(self):
        self.returncode=1
        payload,result=self.invoke('trigger.toggle.window-gaps','failed-reload')
        self.assertEqual(result['status'],'failed')
        self.assertTrue(self.flag('trigger.toggle.window-gaps').exists())
        self.assertEqual(self.service.invoke(payload,'synthetic-device'),result)
        self.assertEqual(len(self.calls),1)

    def test_successful_command_without_flag_readback_is_failed(self):
        self.apply=False
        payload,result=self.invoke('trigger.toggle.one-window-ratio','missing-readback')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.service.invoke(payload,'synthetic-device'),result)
        self.assertEqual(len(self.calls),1)

    def test_symlink_flag_or_parent_is_unavailable_and_cannot_write(self):
        entry='trigger.toggle.window-gaps';target=self.flag(entry)
        target.parent.mkdir(parents=True)
        foreign=self.home/'other';foreign.write_text('preserve')
        target.symlink_to(foreign)
        self.assertFalse(self.reader.can_execute(entry))
        with self.assertRaises(MenuReadUnavailable):self.reader.execute_quick(entry,HYPR_QUICK_COMMANDS[entry])
        self.assertEqual(self.calls,[]);self.assertEqual(foreign.read_text(),'preserve')
        target.unlink();target.parent.rmdir();target.parent.symlink_to(self.home)
        self.assertFalse(self.reader.can_execute(entry))
        self.assertEqual(self.calls,[])

    def test_missing_or_untrusted_packaged_template_is_refused(self):
        self.reader._default_mutation_runner=True
        trusted=SimpleNamespace(st_mode=stat.S_IFREG|0o755,st_uid=0)
        def asset_or_directory(path):
            return trusted if str(path) in HYPR_PACKAGED_BIN_LINKS else SimpleNamespace(st_mode=stat.S_IFDIR|0o755,st_uid=0)
        def missing(path):
            if str(path).endswith('.lua'):raise FileNotFoundError()
            return asset_or_directory(path)
        with patch('pathlib.Path.lstat',missing),patch('os.access',return_value=True):
            with self.assertRaisesRegex(MenuReadUnavailable,'asset_missing'):
                self.reader._validate_hypr_assets('trigger.toggle.window-gaps')
        for mode,uid in [(stat.S_IFLNK|0o777,0),(stat.S_IFREG|0o666,0),(stat.S_IFREG|0o644,1000)]:
            def untrusted(path,mode=mode,uid=uid):
                return SimpleNamespace(st_mode=mode,st_uid=uid) if str(path).endswith('.lua') else asset_or_directory(path)
            with patch('pathlib.Path.lstat',untrusted),patch('os.access',return_value=True):
                with self.assertRaisesRegex(MenuReadUnavailable,'asset_untrusted'):
                    self.reader._validate_hypr_assets('trigger.toggle.one-window-ratio')
        self.assertEqual(self.calls,[])

    def test_changed_source_and_arbitrary_params_never_inherit_execution(self):
        entry='trigger.toggle.window-gaps'
        rows={key:dict(QUICK_SOURCES[key]) for key in HYPR_QUICK_FLAGS}
        rows[entry]['action']='omarchy-hyprland-toggle arbitrary on'
        self.service.runtime.catalog=compile_catalog(rows)
        snapshot=self.service.refresh_catalog(invalidate=True)
        row=next(row for row in snapshot['entries'] if row['id']==entry)
        self.assertFalse(row['route']['ready'])
        with self.assertRaises((ServiceError,ValueError)):
            self.service.invoke({'entry_id':entry,'request_id':'drift','catalog_revision':snapshot['revision'],'params':{}},'synthetic-device')
        other='trigger.toggle.one-window-ratio'
        with self.assertRaises(ValueError):
            self.service.invoke({'entry_id':other,'request_id':'params','catalog_revision':snapshot['revision'],'params':{'flag':'foreign'}},'synthetic-device')
        self.assertEqual(self.calls,[])

    def test_only_two_fixed_flags_and_two_modes_reach_bounded_mutation_surface(self):
        for flag in HYPR_QUICK_FLAGS.values():
            for mode in ('on','off'):self.assertIn((HYPR_FLAG_SETTER,flag,mode),MUTATION_COMMANDS)
        for argv in [(HYPR_FLAG_SETTER,'foreign','on'),(HYPR_FLAG_SETTER,'window-no-gaps','toggle'),
                     (HYPR_FLAG_SETTER,'../outside','off'),(HYPR_FLAG_SETTER,'window-no-gaps','on','extra')]:
            with self.assertRaises(MenuReadUnavailable):bounded_command(argv,{},mutation=True)
        with self.assertRaises(MenuReadUnavailable):
            self.reader.execute_quick('trigger.toggle.window-gaps',HYPR_QUICK_COMMANDS['trigger.toggle.window-gaps']+('extra',))
        self.assertEqual(self.calls,[])


    def recorded_package_layout(self):
        import json
        raw=json.loads((Path(__file__).parent/'fixtures/quick_actions_arch_layout.json').read_text())
        return {row['path']:dict(row) for row in raw['files']}

    def package_probes(self, rows):
        modes={'file':stat.S_IFREG,'directory':stat.S_IFDIR,'symlink':stat.S_IFLNK}
        def lstat(path):
            row=rows.get(str(path))
            if row is None:raise FileNotFoundError(str(path))
            return SimpleNamespace(st_mode=modes[row['kind']]|int(row['mode'],8),st_uid=row['uid'])
        def access(path,mode):return rows.get(str(path),{}).get('executable',False)
        def readlink(path):return rows[str(path)]['link_target']
        return lstat,access,readlink

    def test_recorded_arch_root_owned_aliases_are_trusted_and_routes_ready_without_mutation(self):
        rows=self.recorded_package_layout();lstat,access,readlink=self.package_probes(rows)
        self.reader._default_mutation_runner=True
        # The platform/session context itself is separately verified by main;
        # this portable test replays real installed package metadata only.
        with patch('pathlib.Path.lstat',lstat),patch('os.access',access),patch('os.readlink',readlink), \
                patch('pathlib.Path.stat',lambda path: lstat(Path(HYPR_PACKAGED_BIN_LINKS.get(str(path),str(path))))), \
                patch('omodachi_core.catalog_providers.validate_action_context'):
            for entry in HYPR_QUICK_FLAGS:
                self.reader._validate_hypr_assets(entry)
                self.assertTrue(self.reader.can_execute(entry))
            snapshot=self.service.refresh_catalog(invalidate=True)
            targets=[row for row in snapshot['entries'] if row['id'] in HYPR_QUICK_FLAGS]
            self.assertEqual(len(targets),2)
            self.assertTrue(all(row['route']['ready'] for row in targets))
        self.assertEqual(self.calls,[])

    def test_packaged_alias_wrong_owner_destination_chain_or_parent_is_refused(self):
        original=self.recorded_package_layout()
        cases=[(HYPR_FLAG_SETTER,{'uid':1000}),
               (HYPR_FLAG_SETTER,{'link_target':'/tmp/omarchy-hyprland-toggle'}),
               (HYPR_FLAG_SETTER,{'link_target':'/usr/bin/omarchy-hyprland-window-gaps-toggle'}),
               ('/usr/bin/omarchy-hyprland-toggle',{'kind':'symlink','link_target':'/usr/bin/elsewhere'}),
               ('/usr/bin/omarchy-hyprland-toggle',{'uid':1000}),
               ('/usr/bin/omarchy-hyprland-toggle',{'mode':'0o777'}),
               ('/usr/bin',{'mode':'0o777'}),
               ('/usr/share/omarchy/bin',{'kind':'symlink','link_target':'/tmp/bin'}),
               ('/usr/share/omarchy/default/hypr/toggles/window-no-gaps.lua',{'kind':'symlink','link_target':'/usr/share/something.lua'})]
        self.reader._default_mutation_runner=True
        for path,changes in cases:
            with self.subTest(path=path,changes=changes):
                rows={name:dict(row) for name,row in original.items()};rows[path].update(changes)
                lstat,access,readlink=self.package_probes(rows)
                with patch('pathlib.Path.lstat',lstat),patch('os.access',access),patch('os.readlink',readlink):
                    with self.assertRaisesRegex(MenuReadUnavailable,'asset_untrusted'):
                        self.reader._validate_hypr_assets('trigger.toggle.window-gaps')
        self.assertEqual(self.calls,[])

    def test_missing_alias_destination_stays_unavailable_and_regular_package_layout_still_works(self):
        rows=self.recorded_package_layout();rows.pop('/usr/bin/omarchy-hyprland-toggle')
        self.reader._default_mutation_runner=True
        lstat,access,readlink=self.package_probes(rows)
        with patch('pathlib.Path.lstat',lstat),patch('os.access',access),patch('os.readlink',readlink):
            with self.assertRaisesRegex(MenuReadUnavailable,'asset_missing'):
                self.reader._validate_hypr_assets('trigger.toggle.window-gaps')
        rows=self.recorded_package_layout()
        for path in HYPR_PACKAGED_BIN_LINKS:
            rows[path].update(kind='file',mode='0o755',executable=True)
        lstat,access,readlink=self.package_probes(rows)
        with patch('pathlib.Path.lstat',lstat),patch('os.access',access),patch('os.readlink',readlink):
            for entry in HYPR_QUICK_FLAGS:self.reader._validate_hypr_assets(entry)
        self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
