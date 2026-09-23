"""Ordinary synthetic consumer tests only. No host audio/auth/certificate tests."""
import os
import threading
import time
import unittest
from unittest.mock import patch

from omodachi_core.audio_virtual_input import VirtualMicrophoneSession,VirtualInputError,FRAME_BYTES,_Writer
from audio_virtual_input_support import FakePulse,PipeProcess

class AudioVirtualInputTests(unittest.TestCase):
    def setUp(self):
        self.pulse=FakePulse();self.session=VirtualMicrophoneSession(runner=self.pulse.runner,process_factory=self.pulse.spawn)
    def tearDown(self):
        self.pulse.fail_unload.clear();self.pulse.ack_without_removal.clear();self.pulse.fail_lists=False
        if self.session.metadata and not self.session.closed:
            try:self.session.end(generation=self.session.metadata.generation)
            except Exception:pass
        self.pulse.close()
    def begin(self):return self.session.begin(generation=7)
    def test_load_numeric_and_unload_empty_stdout_are_distinct_protocols(self):
        class Completed:
            returncode=0
            def poll(self):return 0
        def popen(argv,**kwargs):
            kwargs['stdout'].write(b'42\n' if argv[1]=='load-module' else b'');kwargs['stdout'].flush();return Completed()
        with patch('omodachi_core.audio_virtual_input.subprocess.Popen',side_effect=popen):
            self.assertEqual(VirtualMicrophoneSession._run(('pactl','load-module','module-null-sink')),42)
            self.assertIsNone(VirtualMicrophoneSession._run(('pactl','unload-module','42')))
    def test_create_verifies_module_and_node_ownership_before_writer(self):
        metadata=self.begin();self.assertTrue(metadata['ownership_verified'])
        self.assertEqual(metadata['frame_bytes'],1920);self.assertEqual(metadata['rate'],48000)
        self.assertIn('--device='+metadata['sink_name'],self.pulse.spawns[0])
        self.assertIn('--property=node.dont-fallback=true',self.pulse.spawns[0])
        self.assertEqual(self.session.verify_owned()['source_index'],metadata['source_index'])
    def test_same_generation_new_sessions_use_different_nonce_names(self):
        first=self.begin();other=VirtualMicrophoneSession(runner=self.pulse.runner,process_factory=self.pulse.spawn)
        second=other.begin(generation=7)
        self.assertNotEqual(first['sink_name'],second['sink_name'])
        other.end(generation=7)
        self.assertIn(first['sink_module_id'],self.pulse.modules)
    def test_forced_nonce_collision_refuses_existing_sink_without_load(self):
        nonce='a'*32;name='omodachi-mic-g7-'+nonce
        self.pulse.sinks[name]={'index':9,'name':name,'owner_module':9}
        self.session=VirtualMicrophoneSession(runner=self.pulse.runner,process_factory=self.pulse.spawn,nonce_factory=lambda:nonce)
        with self.assertRaisesRegex(VirtualInputError,'collision'):self.begin()
        self.assertFalse(any(c[1]=='load-module' for c in self.pulse.calls));self.assertFalse(self.pulse.spawns)
    def test_wrong_owner_node_never_starts_writer_and_owned_modules_are_cleaned(self):
        original=self.pulse.runner
        def runner(argv):
            result=original(argv)
            if tuple(argv)==('pactl','--format=json','list','sources') and result:
                result[-1]['owner_module']=999
            return result
        self.session=VirtualMicrophoneSession(runner=runner,process_factory=self.pulse.spawn)
        with self.assertRaisesRegex(VirtualInputError,'ownership'):self.begin()
        self.assertFalse(self.pulse.spawns);self.assertFalse(self.pulse.modules)
    def test_three_frames_are_actually_read_from_pipe_not_just_accepted(self):
        self.begin();frames=[bytes([value])*FRAME_BYTES for value in (17,29,43)]
        for frame in frames:self.assertTrue(self.session.accept(frame))
        received=self.pulse.processes[0].read_exact(FRAME_BYTES*3)
        self.assertEqual(received,b''.join(frames));self.assertEqual(len(received),5760)
        result=self.session.end(generation=7)
        self.assertTrue(result['input_closed']);self.assertTrue(result['cleanup_complete']);self.assertEqual(result['state'],'closed')
        self.assertEqual(self.session.stats()['written_frames'],3)
        self.assertEqual(self.session.end(generation=7),result)
    def test_bounded_backpressure_and_frame_validation(self):
        def blocked(fd,data):raise BlockingIOError()
        self.session=VirtualMicrophoneSession(runner=self.pulse.runner,process_factory=self.pulse.spawn,write=blocked)
        self.begin()
        for _ in range(3):self.assertTrue(self.session.accept(b'\0'*1920))
        started=time.monotonic();self.assertFalse(self.session.accept(b'\0'*1920));self.assertLess(time.monotonic()-started,.1)
        self.assertLessEqual(self.session.stats()['queued_frames'],3);self.assertLessEqual(self.session.stats()['pending_bytes'],5760)
        with self.assertRaises(VirtualInputError):self.session.accept(b'bad')
        result=self.session.end(generation=7);self.assertTrue(result['cleanup_complete']);self.assertEqual(self.session.stats()['queued_frames'],0)
    def test_unload_failure_retains_ids_and_retries_reverse_order(self):
        metadata=self.begin();self.pulse.fail_unload.add(metadata['remap_module_id'])
        failed=self.session.end(generation=7)
        self.assertEqual(failed['state'],'cleanup_pending');self.assertTrue(failed['input_closed']);self.assertFalse(failed['cleanup_complete'])
        self.assertIn(metadata['remap_module_id'],failed['remaining_module_ids']);self.assertIn(metadata['sink_module_id'],self.pulse.modules)
        self.assertFalse(self.session.closed);self.assertIsNotNone(self.session.metadata)
        with self.assertRaises(VirtualInputError):self.session.begin(generation=8)
        self.pulse.fail_unload.clear();closed=self.session.end(generation=7)
        self.assertTrue(closed['cleanup_complete']);self.assertEqual(closed['remaining_module_ids'],[])
        unloads=[int(c[2]) for c in self.pulse.calls if c[1]=='unload-module']
        self.assertEqual(unloads[-2:],[metadata['remap_module_id'],metadata['sink_module_id']])
    def test_empty_unload_ack_does_not_claim_module_was_removed(self):
        metadata=self.begin();self.pulse.ack_without_removal.add(metadata['remap_module_id'])
        result=self.session.end(generation=7)
        self.assertFalse(result['cleanup_complete']);self.assertIn('virtual_input_module_still_present',result['errors'])
    def test_reused_module_id_is_not_unloaded(self):
        metadata=self.begin();ident=metadata['remap_module_id']
        self.pulse.modules[ident]={'index':ident,'name':'module-remap-source','argument':'source_name=other master=foreign'}
        before=len(self.pulse.calls);result=self.session.end(generation=7)
        self.assertEqual(result['state'],'cleanup_pending')
        self.assertFalse(any(c[:2]==('pactl','unload-module') for c in self.pulse.calls[before:]))
        self.assertIn('virtual_input_cleanup_ownership_mismatch',result['errors'])
    def test_partial_begin_failure_retains_failed_cleanup_ownership(self):
        self.pulse.fail_load_kind='module-remap-source';self.pulse.fail_unload.add(101)
        with self.assertRaises(VirtualInputError):self.begin()
        self.assertFalse(self.session.closed);self.assertEqual(self.session.stats()['remaining_module_ids'],[101])
        self.pulse.fail_unload.clear();self.assertTrue(self.session.end(generation=7)['cleanup_complete'])
    def test_wrong_generation_never_cleans_current_resources(self):
        self.begin();count=len(self.pulse.calls)
        with self.assertRaises(VirtualInputError):self.session.end(generation=8)
        self.assertEqual(len(self.pulse.calls),count);self.assertTrue(self.session.stats()['active'])
    def test_writer_exit_is_failure_and_cleanup_is_explicit(self):
        self.begin();self.pulse.processes[0].returncode=1
        deadline=time.monotonic()+.3
        while not self.session.stats().get('writer_failed') and time.monotonic()<deadline:time.sleep(.005)
        self.assertTrue(self.session.stats()['writer_failed']);self.assertTrue(self.session.stats()['cleanup_pending'])
        with self.assertRaises(VirtualInputError):self.session.accept(b'\0'*1920)
        result=self.session.end(generation=7)
        self.assertTrue(result['cleanup_complete']);self.assertEqual(result['writer_failure'],'virtual_input_writer_exited')
    def test_no_write_after_successful_close_return_and_no_current_mutation_race(self):
        entered=threading.Event();release=threading.Event();writes=[]
        def delayed(fd,data):
            entered.set();release.wait(1);writes.append(bytes(data));return os.write(fd,data)
        self.session=VirtualMicrophoneSession(runner=self.pulse.runner,process_factory=self.pulse.spawn,write=delayed)
        self.begin();self.session.accept(b'\x22'*1920);self.assertTrue(entered.wait(.5))
        result=self.session.end(generation=7)
        self.assertFalse(result['input_closed']);self.assertFalse(result['cleanup_complete']);self.assertEqual(len(self.pulse.modules),2)
        release.set();time.sleep(.02)
        result=self.session.end(generation=7);self.assertTrue(result['input_closed']);self.assertTrue(result['cleanup_complete'])
        count=len(writes);time.sleep(.03);self.assertEqual(len(writes),count)
    def test_unrelated_nodes_are_preserved(self):
        self.pulse.modules[9]={'index':9,'name':'foreign','argument':'unrelated'}
        self.pulse.sinks['foreign']={'index':9,'name':'foreign','owner_module':9}
        self.begin();self.session.end(generation=7)
        self.assertIn(9,self.pulse.modules);self.assertIn('foreign',self.pulse.sinks)
        self.assertFalse(any('default' in part or 'loopback' in part for call in self.pulse.calls for part in call))

    def test_short_module_parser_preserves_blank_unrelated_ids_and_exact_arguments(self):
        raw=b'\tmodule-native-protocol-unix\t\tn/a\n536870918\tmodule-null-sink\tsink_name=owned format=s16le\tn/a\n'
        class Completed:
            returncode=0
            def poll(self):return 0
        def popen(argv,**kwargs):
            kwargs['stdout'].write(raw);kwargs['stdout'].flush();return Completed()
        with patch('omodachi_core.audio_virtual_input.subprocess.Popen',side_effect=popen):
            rows=VirtualMicrophoneSession._run(('pactl','list','short','modules'))
        self.assertEqual(rows[0]['index'],'')
        self.assertEqual(rows[1],{'index':'536870918','name':'module-null-sink','argument':'sink_name=owned format=s16le'})
    def test_real_indexless_json_shape_joins_short_ids_and_reads_pipe_then_cleans(self):
        self.pulse.module_json_has_index=False
        self.pulse.short_unindexed_rows=[{'index':'','name':'module-native-protocol-unix','argument':''}]
        self.pulse.modules[9]={'index':9,'name':'module-native-protocol-unix','argument':''}
        metadata=self.begin()
        frames=[bytes([value])*FRAME_BYTES for value in (17,29,43)]
        for frame in frames:self.assertTrue(self.session.accept(frame))
        self.assertEqual(self.pulse.processes[0].read_exact(5760),b''.join(frames))
        self.assertEqual(self.session.verify_owned()['sink_module_id'],metadata['sink_module_id'])
        result=self.session.end(generation=7)
        self.assertTrue(result['cleanup_complete']);self.assertEqual(result['remaining_module_ids'],[])
        self.assertIn(9,self.pulse.modules)
        self.assertTrue(any(c==('pactl','list','short','modules') for c in self.pulse.calls))
    def test_indexless_json_short_mismatch_never_unloads_or_discards_owned_ids(self):
        self.pulse.module_json_has_index=False;metadata=self.begin();original=self.pulse.runner
        def runner(argv):
            rows=original(argv)
            if tuple(argv)==('pactl','list','short','modules'):
                for row in rows:
                    if row['index']==str(metadata['remap_module_id']):row['argument']='source_name=foreign master=foreign'
            return rows
        self.session.runner=runner;before=len(self.pulse.calls)
        result=self.session.end(generation=7)
        self.assertFalse(result['cleanup_complete'])
        self.assertEqual(set(result['remaining_module_ids']),{metadata['remap_module_id'],metadata['sink_module_id']})
        self.assertFalse(any(c[1]=='unload-module' for c in self.pulse.calls[before:]))
        self.session.runner=original
        self.assertTrue(self.session.end(generation=7)['cleanup_complete'])
    def test_indexless_json_missing_short_id_is_unconfirmed_and_retryable(self):
        self.pulse.module_json_has_index=False;metadata=self.begin();original=self.pulse.runner
        def runner(argv):
            rows=original(argv)
            return [r for r in rows if r['index']!=str(metadata['remap_module_id'])] if tuple(argv)==('pactl','list','short','modules') else rows
        self.session.runner=runner;before=len(self.pulse.calls);result=self.session.end(generation=7)
        self.assertFalse(result['cleanup_complete']);self.assertIn('virtual_input_module_metadata_unconfirmed',result['errors'])
        self.assertIn(metadata['remap_module_id'],result['remaining_module_ids'])
        self.assertFalse(any(c[1]=='unload-module' for c in self.pulse.calls[before:]))
        self.session.runner=original
        self.assertTrue(self.session.end(generation=7)['cleanup_complete'])
    def test_indexless_json_duplicate_short_id_is_rejected(self):
        self.pulse.module_json_has_index=False;self.begin();original=self.pulse.runner
        def runner(argv):
            rows=original(argv)
            if tuple(argv)==('pactl','list','short','modules'):rows.append(dict(rows[-1]))
            return rows
        self.session.runner=runner;before=len(self.pulse.calls);result=self.session.end(generation=7)
        self.assertFalse(result['cleanup_complete'])
        self.assertFalse(any(c[1]=='unload-module' for c in self.pulse.calls[before:]))
        self.session.runner=original
    def test_indexless_json_reused_target_id_is_not_unloaded(self):
        self.pulse.module_json_has_index=False;metadata=self.begin();ident=metadata['remap_module_id']
        self.pulse.modules[ident]={'index':ident,'name':'module-remap-source','argument':'source_name=foreign master=foreign'}
        before=len(self.pulse.calls);result=self.session.end(generation=7)
        self.assertFalse(result['cleanup_complete']);self.assertIn('virtual_input_cleanup_ownership_mismatch',result['errors'])
        self.assertFalse(any(c[1]=='unload-module' for c in self.pulse.calls[before:]))
    def test_indexless_json_partial_begin_cleanup_uses_short_owned_id(self):
        self.pulse.module_json_has_index=False;self.pulse.fail_load_kind='module-remap-source'
        with self.assertRaises(VirtualInputError):self.begin()
        self.assertTrue(self.session.closed);self.assertFalse(self.pulse.modules)

    def test_recorded_linux_module_json_is_joined_with_known_short_id(self):
        import json
        from pathlib import Path
        fixture=json.loads((Path(__file__).parent/'fixtures/audio_modules_indexless.json').read_text())
        short='\tmodule-native-protocol-unix\t\tn/a\n'+''.join(str(fixture['ids_by_module_name'][row['name']])+'\t'+row['name']+'\t'+row['argument']+'\tn/a\n' for row in fixture['json_modules'])
        class Completed:
            returncode=0
            def poll(self):return 0
        def popen(argv,**kwargs):
            raw=short.encode() if tuple(argv)==('pactl','list','short','modules') else json.dumps(fixture['json_modules']).encode()
            kwargs['stdout'].write(raw);kwargs['stdout'].flush();return Completed()
        with patch('omodachi_core.audio_virtual_input.subprocess.Popen',side_effect=popen):
            session=VirtualMicrophoneSession()
            rows=session._list('modules')
            self.assertTrue(all('index' not in row for row in rows))
            for expected in fixture['json_modules']:
                ident=fixture['ids_by_module_name'][expected['name']]
                actual=session._module_row(rows,ident)
                self.assertEqual(actual,{**expected,'index':ident})

if __name__=='__main__':unittest.main()
