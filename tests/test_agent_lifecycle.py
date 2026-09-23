from __future__ import annotations
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from omodachi_core.agent import AgentStatus, DefaultAgentCapabilities, HerdrStatusSnapshot, ProbeStatus, ProbeOutput, ReadOnlyAgentProbe
from omodachi_core.agent_lifecycle import DefaultAgentManager, ATTACH_ARGV
from omodachi_core.service import ServiceError


def capability(status=AgentStatus.IDLE, exists=True):
    return DefaultAgentCapabilities(omarchy_default_agent="codex", omarchy_probe=ProbeStatus.AVAILABLE,
        herdr_supported_kinds=frozenset({"codex", "claude"}), herdr_probe=ProbeStatus.AVAILABLE,
        default_agent_exists=exists, default_agent_probe=ProbeStatus.AVAILABLE if exists else ProbeStatus.MISSING,
        pane_id="w1:p1" if exists else None, pane_available=exists,
        pane_probe=ProbeStatus.AVAILABLE if exists else ProbeStatus.MISSING,
        agent_status=status, actual_kind="codex" if exists else None)


def prompted(**changes):
    agent = {"name":"default", "agent":"codex", "pane_id":"w1:p1", "agent_status":"working", **changes}
    return ProbeOutput(0,json.dumps({"result":{"type":"agent_prompted","agent":agent}}))


class Probe:
    def __init__(self, cap): self.cap = cap
    def inspect(self): return self.cap, HerdrStatusSnapshot(True, True, True, frozenset({"codex"}))


class AgentLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.probe = Probe(capability())
        self.calls = []
        def runner(argv):
            self.calls.append(argv)
            return prompted()
        self.manager = DefaultAgentManager(root=self.root, probe=self.probe, runner=runner)

    def tearDown(self): self.temp.cleanup()

    def request(self, text="A task; $(not shell)"):
        return {"agent_id":"default", "request_id":"request-1", "text":text}

    def test_prompt_is_one_data_argument_persisted_without_content_and_idempotent(self):
        result = self.manager.submit(self.request(), "ipad")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.calls, [("herdr","--session","omodachi","agent","prompt","default","A task; $(not shell)")])
        self.assertNotIn("A task", (self.root/"agent-requests.json").read_text())
        self.assertEqual(self.manager.submit(self.request(), "ipad"), result)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaisesRegex(ServiceError, "request_conflict"):
            self.manager.submit(self.request("different"), "ipad")

    def test_busy_blocked_unknown_and_mismatch_never_mutate(self):
        for status, code in [(AgentStatus.BLOCKED,"agent_blocked"), (AgentStatus.WORKING,"agent_busy"), (AgentStatus.UNKNOWN,"agent_state_unknown")]:
            self.probe.cap = capability(status)
            with self.assertRaisesRegex(ServiceError, code): self.manager.submit(self.request(),"ipad")
        self.probe.cap = replace(capability(), actual_kind="claude")
        with self.assertRaisesRegex(ServiceError,"agent_kind_mismatch"): self.manager.submit(self.request(),"ipad")
        self.assertEqual(self.calls, [])

    def test_blocked_agent_is_attachable_and_preserved(self):
        self.probe.cap = capability(AgentStatus.BLOCKED)
        result = self.manager.ensure()
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["ready_to_attach"])
        self.assertEqual(result["route"]["argv"], list(ATTACH_ARGV))
        self.assertEqual(self.calls, [])

    def test_explicit_owned_pane_creation_and_kind_start(self):
        self.probe.cap = capability(exists=False)
        def runner(argv):
            self.calls.append(argv)
            if argv[3:5] == ("workspace","create"):
                return ProbeOutput(0,json.dumps({"result":{"root_pane":{"pane_id":"w9:p7"}}}))
            if argv[3:6] == ("agent","start","default"):
                self.probe.cap = replace(capability(AgentStatus.BLOCKED), pane_id="w9:p7")
                return ProbeOutput(1,json.dumps({"error":{"code":"agent_not_ready"}}))
            self.fail("unexpected command")
        self.manager.runner = runner
        result=self.manager.ensure()
        self.assertEqual(result["pane_id"],"w9:p7")
        self.assertEqual(self.calls[-1],("herdr","--session","omodachi","agent","start","default","--kind","codex","--pane","w9:p7","--timeout","10000"))
        self.assertIn("--no-focus", self.calls[0])
        self.assertEqual(json.loads((self.root/"owned-herdr-pane.json").read_text())["pane_id"], "w9:p7")

    def test_revocation_rechecked_before_any_side_effect(self):
        def revoked(): raise ServiceError("permission_denied",status=401)
        with self.assertRaisesRegex(ServiceError,"permission_denied"):
            self.manager.submit(self.request(),"ipad",revoked)
        self.assertEqual(self.calls,[])

    def test_delivered_unknown_retry_does_not_send_twice(self):
        def runner(argv):
            self.calls.append(argv)
            return ProbeOutput(124,error="timeout")
        self.manager.runner=runner
        first=self.manager.submit(self.request(),"ipad")
        self.assertEqual(first["status"],"unavailable")
        self.assertEqual(self.manager.submit(self.request(),"ipad"),first)
        self.assertEqual(len(self.calls),1)

    def test_raw_mutator_allowlist_never_accepts_an_unowned_session_or_shell(self):
        for argv in [("sh","-c","true"),("herdr","--session","default","agent","prompt","default","--","hello"), ("herdr","--session","omodachi","pane","close","w1:p1")]:
            with self.assertRaisesRegex(ServiceError,"invalid_agent_operation"): self.manager._run(argv)
        self.assertFalse(ReadOnlyAgentProbe._allowed_argv(("herdr","--session","omodachi","agent","prompt","default","hello")))
        self.assertTrue(ReadOnlyAgentProbe._allowed_argv(("herdr","--session","omodachi","agent","get","default")))

    def test_prompt_literal_leading_options_unicode_and_newlines_stay_one_argument(self):
        texts=["--wait", "--timeout", "--", "--help", "$(not shell); one\n第二行"]
        for index,text in enumerate(texts):
            request=self.request(text);request["request_id"]="literal-"+str(index)
            result=self.manager.submit(request,"ipad")
            self.assertEqual(result["delivery"],"accepted")
            self.assertEqual(self.calls[-1],("herdr","--session","omodachi","agent","prompt","default",text))
        self.assertEqual(len(self.calls),len(texts))

    def test_correct_argv_passes_allowlist_and_old_separator_is_rejected(self):
        for text in ["normal task", "--wait", "--help", "--"]:
            args=("herdr","--session","omodachi","agent","prompt","default",text)
            with patch.object(ReadOnlyAgentProbe,"_run_process",return_value=prompted()) as runner:
                self.assertEqual(self.manager._run(args).returncode,0)
                runner.assert_called_once_with(args,timeout_seconds=15,max_bytes=262144)
        with self.assertRaisesRegex(ServiceError,"invalid_agent_operation"):
            self.manager._run(("herdr","--session","omodachi","agent","prompt","default","--","task"))

    def test_cli_parse_rejection_is_known_not_sent_and_diagnostic_is_not_exposed(self):
        self.manager.runner=self.manager._run
        with patch.object(ReadOnlyAgentProbe,"_run_process",return_value=ProbeOutput(2)):
            result=self.manager.submit(self.request("PRIVATE TASK"),"ipad")
        self.assertEqual(result["status"],"rejected")
        self.assertEqual(result["delivery"],"not_sent")
        self.assertEqual(result["code"],"agent_cli_rejected")
        journal=(self.root/"agent-requests.json").read_text()
        self.assertNotIn("PRIVATE TASK",journal)
        self.assertEqual(next(iter(json.loads(journal).values()))["execution"]["returncode"],2)
        with patch.object(ReadOnlyAgentProbe,"_run_process") as runner:
            self.assertEqual(self.manager.submit(self.request("PRIVATE TASK"),"ipad"),result)
            runner.assert_not_called()

    def test_exit_zero_without_exact_prompted_target_is_delivery_unknown(self):
        invalid=[ProbeOutput(0,'{}'),ProbeOutput(0,'[]'),ProbeOutput(0,'not JSON'),
                 ProbeOutput(0,json.dumps({"result":{"type":"agent_info","agent":{"name":"default"}}})),
                 prompted(name="other"),prompted(pane_id="w9:p9"),prompted(agent="claude")]
        for index,output in enumerate(invalid):
            self.manager.runner=lambda args,output=output:output
            request=self.request();request["request_id"]="bad-output-"+str(index)
            result=self.manager.submit(request,"ipad")
            self.assertEqual(result["status"],"unavailable")
            self.assertEqual(result["delivery"],"unknown")

    def test_only_safe_metadata_survives_success_response(self):
        self.manager.runner=lambda args:prompted(terminal_title="PRIVATE TITLE",text="PRIVATE TEXT",message="PRIVATE RESPONSE")
        result=self.manager.submit(self.request(),"ipad")
        self.assertEqual(result["status"],"accepted")
        self.assertEqual(set(result),{"request_id","agent_id","pane_id","status","delivery"})
        self.assertNotIn("PRIVATE",json.dumps(result))
        self.assertNotIn("PRIVATE",(self.root/"agent-requests.json").read_text())

    def test_timeout_is_unknown_and_preserves_existing_retry_guard(self):
        self.manager.runner=lambda args:ProbeOutput(124,error="timeout")
        result=self.manager.submit(self.request(),"ipad")
        self.assertEqual(result["delivery"],"unknown")
        with patch.object(self.manager,"runner") as runner:
            self.assertEqual(self.manager.submit(self.request(),"ipad"),result)
            runner.assert_not_called()

    def test_machine_errors_only_mark_proven_no_send_and_keep_unknown_failures_unknown(self):
        for index,(code,delivery,status) in enumerate([
                ("agent_not_found","not_sent","rejected"),("agent_blocked","not_sent","blocked"),
                ("server_not_running","not_sent","rejected"),("agent_prompt_stalled","unknown","unavailable"),
                ("timeout","unknown","unavailable")]):
            self.manager.runner=lambda args,code=code:ProbeOutput(1,json.dumps({"error":{"code":code,"message":"private"}}))
            request=self.request();request["request_id"]="machine-"+str(index)
            result=self.manager.submit(request,"ipad")
            self.assertEqual((result["delivery"],result["status"]),(delivery,status))
            self.assertNotIn("private",json.dumps(result))

    def test_legacy_unknown_journal_is_preserved_not_reclassified_or_resent(self):
        self.manager.runner=lambda args:ProbeOutput(124,error="timeout")
        request=self.request();result=self.manager.submit(request,"ipad")
        journal_path=self.root/"agent-requests.json";journal=json.loads(journal_path.read_text())
        row=next(iter(journal.values()));row.pop("execution");row["result"].pop("delivery")
        journal_path.write_text(json.dumps(journal))
        with patch.object(self.manager,"runner") as runner:
            reply=self.manager.submit(request,"ipad")
            self.assertEqual(reply,row["result"]);runner.assert_not_called()
