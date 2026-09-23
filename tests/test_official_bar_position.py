"""Official position command ownership; no real shell/compositor operation."""
import json
from pathlib import Path
import tempfile
import unittest
from omodachi_core.remote.journal import Journal as RecoveryJournal
from omodachi_core.official_bar_position import OfficialBarPosition

class OfficialBarPositionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name)
        self.journal=RecoveryJournal(self.home/'output.json');self.journal.write({'journal_version':1,'state':'configured'})
        self.position='bottom';self.calls=[]
        def setter(value):self.calls.append(value);self.position=value
        self.controller=OfficialBarPosition(home=self.home,read_position=lambda:self.position,set_position=setter)
    def tearDown(self):self.tmp.cleanup()
    def commit(self,w,h):return self.controller.committed(self.journal,{'width':w,'height':h})
    def test_commit_changes_only_with_orientation_and_end_restores(self):
        self.assertEqual(self.calls,[])
        self.assertEqual(self.commit(1200,800)['position'],'top')
        self.commit(1200,800);self.assertEqual(self.calls,['top'])
        self.assertEqual(self.commit(800,1200)['position'],'left')
        self.assertEqual(self.controller.restore(self.journal)['position'],'bottom')
        self.assertEqual(self.calls,['top','left','bottom'])
        self.controller.restore(self.journal);self.assertEqual(len(self.calls),3)
    def test_user_change_is_not_overridden_at_rotation_or_end(self):
        self.commit(1200,800);self.position='right'
        self.assertEqual(self.commit(800,1200)['status'],'user_override')
        self.controller.restore(self.journal);self.assertEqual(self.position,'right');self.assertEqual(self.calls,['top'])
    def test_user_change_at_end_without_another_rotation_is_preserved(self):
        self.commit(1200,800);self.position='left'
        self.assertEqual(self.controller.restore(self.journal)['status'],'user_override')
        self.assertEqual(self.calls,['top'])
    def test_existing_edge_preferences_are_used_without_clone_bar(self):
        p=self.home/'.config/omarchy/omodachi-remote-bar.json';p.parent.mkdir(parents=True);p.write_text(json.dumps({'landscape':'bottom','portrait':'right'}))
        self.commit(1200,800);self.assertEqual(self.calls,[])
        self.commit(800,1200);self.assertEqual(self.calls,['right'])
        self.controller.restore(self.journal);self.assertEqual(self.position,'bottom')
    def test_interrupted_command_intent_can_restore_without_reapplying(self):
        self.journal.write({'journal_version':1,'state':'configured','official_bar_position':{'original_position':'bottom','last_owned_position':'top','pending_position':'left','state':'owned'}})
        self.position='left';self.assertEqual(self.controller.restore(self.journal)['status'],'restored')
        self.assertEqual(self.calls,['bottom'])
    def test_no_session_record_causes_no_position_change(self):
        self.journal.write({'journal_version':1,'state':'removed'})
        self.assertEqual(self.commit(1200,800)['status'],'inactive');self.assertEqual(self.calls,[])
