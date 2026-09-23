import json
import unittest
import os
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from omodachi_core.agent import AgentState, AgentStatus, AgentTarget, DefaultAgentCapabilities, HerdrStatusSnapshot, ProbeStatus, ProbeOutput, ReadOnlyAgentProbe, build_default_agent_capabilities, probe_commands

FIXTURES = Path(__file__).parents[1] / "contracts" / "fixtures"

def supported_schema():
    return json.dumps({"protocol":20,"schema_version":1,"schemas":{"success_response":{"$defs":{
        "AgentInfo":{"properties":{name:{} for name in ("name","agent","agent_status","pane_id")}},
        "PaneInfo":{"properties":{"pane_id":{}}},
        "AgentStatus":{"enum":[status.value for status in AgentStatus]},
    }}}})


def probe_replies(**overrides):
    data = {
        ("omarchy-default-agent",): ProbeOutput(0,"codex"),
        ("herdr","agent","start","--help"): ProbeOutput(0,"[possible values: codex, claude]"),
        ("herdr","status"): ProbeOutput(0,"server:\n  status: running\n"),
        ("herdr","api","schema","--json"): ProbeOutput(0,supported_schema()),
        ("herdr","agent","get","default"): ProbeOutput(0,json.dumps({"result":{"type":"agent_info","agent":{"name":"default","agent":"codex","agent_status":"working","pane_id":"pane-7"}}})),
        ("herdr","pane","get","pane-7"): ProbeOutput(0,json.dumps({"result":{"type":"pane_info","pane":{"pane_id":"pane-7"}}})),
    }
    return data


class AgentCapabilityTests(unittest.TestCase):
    def test_default_agent_fixture_is_explicit_and_redacted(self):
        data = json.loads((FIXTURES / "default-agent.json").read_text())
        state = DefaultAgentCapabilities.from_snapshot(data)
        self.assertTrue(state.ready_to_attach)
        self.assertIs(state.agent_status, AgentStatus.WORKING)
        self.assertEqual(state.actual_kind, state.configured_kind)
        self.assertEqual(state.pane_id, "pane-fixture-01")

    def test_kind_mismatch_and_unavailable_herdr_never_ready(self):
        state = build_default_agent_capabilities(omarchy_default_agent="codex", omarchy_probe=ProbeStatus.AVAILABLE, herdr_supported_kinds=["codex"], herdr_probe=ProbeStatus.NOT_RUNNING, default_agent=AgentState("default", "claude", AgentStatus.BLOCKED, "original-pane", True), pane_available=True, pane_id="new-pane")
        self.assertTrue(state.kind_mismatch)
        self.assertFalse(state.herdr_available)
        self.assertFalse(state.ready_to_attach)
        self.assertEqual(state.pane_id, "original-pane")
        self.assertEqual(state.agent_status, AgentStatus.BLOCKED)

    def test_probe_stops_at_read_only_status_when_server_missing(self):
        calls = []
        def runner(argv):
            calls.append(argv)
            if argv == ("omarchy-default-agent",): return ProbeOutput(0, "codex\n")
            if argv == ("herdr", "agent", "start", "--help"): return ProbeOutput(0, "[possible values: codex, claude]")
            if argv == ("herdr", "status"): return ProbeOutput(0, "server:\n  status: not running")
            self.fail("no live lookup expected")
        state, herdr = ReadOnlyAgentProbe(runner).inspect()
        self.assertEqual(len(calls), 3)
        self.assertEqual(state.configured_kind, "codex")
        self.assertEqual(state.default_agent_probe, ProbeStatus.UNREADABLE)
        self.assertNotIn("openclaw", herdr.supported_kinds)
        self.assertFalse(state.ready_to_start)

    def test_probe_retains_kind_state_and_explicit_pane(self):
        calls = []
        def runner(argv):
            calls.append(argv)
            replies = {
                ("omarchy-default-agent",): "codex",
                ("herdr", "agent", "start", "--help"): "[possible values: codex, claude]",
                ("herdr", "status"): "server:\n  status: running",
                ("herdr", "api", "schema", "--json"): supported_schema(),
                ("herdr", "agent", "get", "default"): json.dumps({"name":"default","agent":"claude","agent_status":"blocked","pane_id":"pane-7","title":"PRIVATE","cwd":"PRIVATE"}),
                ("herdr", "pane", "get", "pane-7"): '{"pane_id":"pane-7","title":"PRIVATE"}',
            }
            return ProbeOutput(0, replies[argv])
        state, herdr = ReadOnlyAgentProbe(runner).inspect()
        self.assertTrue(state.kind_mismatch)
        self.assertEqual(state.agent_status, AgentStatus.BLOCKED)
        self.assertEqual(state.actual_kind, "claude")
        self.assertFalse(state.ready_to_attach)
        self.assertNotIn("PRIVATE", json.dumps(state.to_dict()) + json.dumps(herdr.to_dict()))
        self.assertIn(("herdr", "pane", "get", "pane-7"), calls)

    def test_explicit_target_rejects_missing_pane_fallback(self):
        with self.assertRaises(ValueError): AgentTarget("default", "")

    def test_pane_failure_keeps_confirmed_agent_identity_and_status(self):
        for response in (ProbeOutput(124,error="timeout"), ProbeOutput(0,"bad json"), ProbeOutput(0,'{"pane_id":"other"}'), ProbeOutput(0,'{"pane":[]}')):
            with self.subTest(response=response):
                replies=probe_replies()
                replies[("herdr","pane","get","pane-7")]=response
                state,herdr=ReadOnlyAgentProbe(lambda argv: replies[argv]).inspect()
                self.assertTrue(state.default_agent_exists)
                self.assertEqual(state.default_agent_probe,ProbeStatus.AVAILABLE)
                self.assertEqual(state.pane_probe,ProbeStatus.UNREADABLE)
                self.assertEqual((state.actual_kind,state.agent_status,state.pane_id),("codex",AgentStatus.WORKING,"pane-7"))
                self.assertFalse(state.pane_available)
                self.assertFalse(state.ready_to_attach)
                self.assertTrue(herdr.socket_available)
                self.assertEqual(herdr.agents[0].pane_id,"pane-7")

    def test_missing_pane_is_separate_from_missing_agent(self):
        replies=probe_replies()
        replies[("herdr","pane","get","pane-7")]=ProbeOutput(1,'{"error":{"code":"pane_not_found","message":"ignored"}}')
        state,_=ReadOnlyAgentProbe(lambda argv:replies[argv]).inspect()
        self.assertEqual(state.pane_probe,ProbeStatus.MISSING)
        self.assertTrue(state.default_agent_exists)
        self.assertEqual(state.actual_kind,"codex")

    def test_unsupported_schema_and_unknown_server_are_explicit(self):
        for schema in ('{"protocol":999,"schema_version":1}', '{"protocol":20,"schema_version":1,"schemas":{}}'):
            replies=probe_replies()
            replies[("herdr","api","schema","--json")]=ProbeOutput(0,schema)
            calls=[]
            def runner(argv):
                calls.append(argv)
                return replies[argv]
            state,herdr=ReadOnlyAgentProbe(runner).inspect()
            self.assertEqual(state.herdr_probe,ProbeStatus.UNSUPPORTED)
            self.assertEqual(state.default_agent_probe,ProbeStatus.UNSUPPORTED)
            self.assertEqual(herdr.schema_probe,ProbeStatus.UNSUPPORTED)
            self.assertTrue(herdr.server_running)
            self.assertIsNone(herdr.socket_available)
            self.assertNotIn(("herdr","agent","get","default"),calls)
        replies=probe_replies()
        replies[("herdr","status")]=ProbeOutput(1,"status unavailable")
        state,herdr=ReadOnlyAgentProbe(lambda argv:replies[argv]).inspect()
        self.assertIsNone(herdr.server_running)
        self.assertIsNone(herdr.socket_available)
        self.assertEqual(state.herdr_probe,ProbeStatus.UNREADABLE)
        self.assertNotEqual(state.herdr_probe,ProbeStatus.NOT_RUNNING)

    def test_running_status_is_scoped_to_server_section(self):
        for text in ("client:\n  status: running\n", "server:\n  status: disconnected\nupdate:\n  status: running\n"):
            self.assertIsNone(ReadOnlyAgentProbe._server_running(ProbeOutput(0,text)))
        self.assertFalse(ReadOnlyAgentProbe._server_running(ProbeOutput(0,"server:\n  status: not running\n")))

    def test_probe_missing_binary_and_bad_runner_are_unknown_safe(self):
        state,herdr=ReadOnlyAgentProbe(lambda argv:ProbeOutput(127,error="executable_missing")).inspect()
        self.assertIs(herdr.server_installed,False)
        self.assertIsNone(herdr.server_running)
        self.assertEqual(state.herdr_probe,ProbeStatus.MISSING)
        state,herdr=ReadOnlyAgentProbe(lambda argv:ProbeOutput(0,"x"*101),max_bytes=100).inspect()
        self.assertIsNone(herdr.server_installed)
        self.assertEqual(state.herdr_probe,ProbeStatus.UNREADABLE)

    def test_actual_agent_kind_and_status_survive_partial_agent_metadata(self):
        replies=probe_replies()
        replies[("herdr","agent","get","default")]=ProbeOutput(0,json.dumps({"name":"default","agent":"claude","agent_status":"blocked","pane_id":{"unknown":"shape"}}))
        state,herdr=ReadOnlyAgentProbe(lambda argv:replies[argv]).inspect()
        self.assertTrue(state.default_agent_exists)
        self.assertEqual(state.actual_kind,"claude")
        self.assertEqual(state.agent_status,AgentStatus.BLOCKED)
        self.assertTrue(state.kind_mismatch)
        self.assertIsNone(state.pane_id)

    def test_tri_state_snapshot_round_trip_preserves_unknown(self):
        snapshot=HerdrStatusSnapshot.from_snapshot({"server_installed":True,"server_running":None,"socket_available":None})
        self.assertIsNone(snapshot.to_dict()["server_running"])
        self.assertIsNone(snapshot.to_dict()["socket_available"])
        with self.assertRaises(ValueError): HerdrStatusSnapshot.from_snapshot({"server_running":"false"})

    def test_runner_only_allows_fixed_read_only_commands(self):
        for argv in (("sh","-c","true"),("herdr","agent","start","default"),("herdr","pane","get","--focused"),("herdr","pane","read","pane-7"),("herdr","pane","get","../user")):
            self.assertEqual(ReadOnlyAgentProbe._local_runner(argv).error,"invalid_command")
        self.assertTrue(ReadOnlyAgentProbe._allowed_argv(("herdr","pane","get","pane-7")))

    def test_local_runner_enforces_byte_limit_and_kills_process(self):
        original_popen=subprocess.Popen
        children=[]
        def tracked(*args,**kwargs):
            child=original_popen(*args,**kwargs)
            children.append(child)
            return child
        with tempfile.TemporaryDirectory() as directory:
            fake=Path(directory)/"herdr"
            fake.write_text(f"#!{sys.executable}\nimport os\nwhile True: os.write(1,b'x'*65536)\n")
            fake.chmod(0o700)
            with patch.dict(os.environ,{"PATH":directory}), patch("subprocess.Popen",side_effect=tracked):
                result=ReadOnlyAgentProbe._local_runner(("herdr","status"),max_bytes=1024,timeout_seconds=1)
        self.assertEqual(result.error,"output_limit")
        self.assertEqual(result.stdout,"")
        self.assertEqual(len(children),1)
        self.assertIsNotNone(children[0].poll())

    def test_local_runner_deadline_kills_child_when_stdout_closes_early(self):
        original_popen=subprocess.Popen
        children=[]
        def tracked(*args,**kwargs):
            child=original_popen(*args,**kwargs)
            children.append(child)
            return child
        with tempfile.TemporaryDirectory() as directory:
            fake=Path(directory)/"herdr"
            fake.write_text(f"#!{sys.executable}\nimport os,time\nos.close(1)\ntime.sleep(10)\n")
            fake.chmod(0o700)
            start=time.monotonic()
            with patch.dict(os.environ,{"PATH":directory}), patch("subprocess.Popen",side_effect=tracked):
                result=ReadOnlyAgentProbe._local_runner(("herdr","status"),max_bytes=1024,timeout_seconds=.1)
        self.assertEqual(result.error,"timeout")
        self.assertLess(time.monotonic()-start,2)
        self.assertIsNotNone(children[0].poll())

