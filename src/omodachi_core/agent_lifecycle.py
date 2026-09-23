"""Explicit, bounded mutations in the Omodachi-owned Herdr session.

This adapter is separate from the read-only status probe. It never reads pane
output, uses a focused pane, kills an agent, or accepts a client executable/path.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .agent import AgentStatus, ProbeStatus, ReadOnlyAgentProbe
from .auth import _registry_lock, _private_open
from .service import ServiceError, fields, identifier

SESSION = "omodachi"
ATTACH_ARGV = ("herdr", "--session", SESSION, "agent", "attach", "default")


def atomic_private_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".agent-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, separators=(",", ":"))
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def read_private_json(path: Path):
    fd = _private_open(path, os.O_RDONLY)
    with os.fdopen(fd, "rb") as stream:
        data = stream.read(262145)
    if len(data) > 262144: raise ServiceError("agent_state_unavailable", status=503)
    return json.loads(data)


class DefaultAgentManager:
    def __init__(self, *, root: Path | None = None, probe=None, runner=None):
        self.root = root or Path.home() / ".config/omodachi"
        self.probe = probe or ReadOnlyAgentProbe(session=SESSION)
        self.runner = runner or self._run
        self.cwd = Path.home() / ".local/share/omodachi/agent-workspace"

    def _run(self, argv):
        prefix = ("herdr", "--session", SESSION)
        allowed = argv == ("systemctl", "--user", "start", "omodachi-herdr.service")
        tail = argv[3:] if argv[:3] == prefix else ()
        if tail[:2] == ("workspace", "create"):
            allowed = tail == ("workspace", "create", "--cwd", str(self.cwd), "--label", "Omodachi", "--no-focus")
        elif tail[:3] == ("agent", "start", "default"):
            allowed = (len(tail) == 9 and tail[3] == "--kind" and bool(re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", tail[4]))
                       and tail[5] == "--pane" and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", tail[6]))
                       and tail[7:] == ("--timeout", "10000"))
        elif tail[:3] == ("agent", "prompt", "default"):
            # Herdr 0.8.2 has a handwritten positional parser: target=text[0],
            # text=args[1], options start at args[2]. It does not consume a '--'
            # separator. Text beginning '--' is still one literal positional.
            allowed = len(tail) == 4 and isinstance(tail[3], str) and "\0" not in tail[3] and len(tail[3].encode()) <= 60000
        elif tail[:2] == ("pane", "get"):
            allowed = len(tail) == 3 and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", tail[2]))
        if not allowed: raise ServiceError("invalid_agent_operation")
        output = ReadOnlyAgentProbe._run_process(tuple(argv), timeout_seconds=15, max_bytes=262144)
        if tail[:3] == ("agent", "prompt", "default") and output.returncode == 2 and not output.error:
            # Verified 0.8.2 agent_prompt returns 2 during argument validation,
            # before send_request. Plain parser stderr may echo task text, so
            # expose only a fixed code, never that diagnostic.
            return type(output)(2, json.dumps({"error": {"code": "agent_cli_rejected"}}))
        return output

    @staticmethod
    def payload(output):
        try:
            value = json.loads(output.stdout)
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError): return {}

    @staticmethod
    def error_code(output, fallback):
        payload = DefaultAgentManager.payload(output)
        error = payload.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        return code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code) else fallback

    @staticmethod
    def delivery_result(output, *, pane_id: str, kind: str) -> dict:
        """Distinguish proven no-send, acknowledged acceptance and uncertainty.

        A completed CLI process is not a task-completion event. Even exit zero
        needs the installed schema's agent_prompted result for this exact target.
        """
        payload = DefaultAgentManager.payload(output)
        result = payload.get("result")
        agent = result.get("agent") if isinstance(result, dict) else None
        if (output.returncode == 0 and output.error is None
                and isinstance(result, dict) and result.get("type") == "agent_prompted"
                and isinstance(agent, dict) and agent.get("name") == "default"
                and agent.get("pane_id") == pane_id and agent.get("agent") == kind):
            return {"status": "accepted", "delivery": "accepted"}
        code = DefaultAgentManager.error_code(output, "agent_delivery_unconfirmed")
        no_send = {"agent_cli_rejected", "agent_not_found", "agent_blocked", "server_not_running"}
        if output.error is None and output.returncode != 0 and code in no_send:
            return {"status": "blocked" if code == "agent_blocked" else "rejected",
                    "code": code, "delivery": "not_sent"}
        return {"status": "unavailable", "code": code, "delivery": "unknown"}

    def _ensure(self, authorize):
        authorize()
        cap, _ = self.probe.inspect()
        if not cap.configured: raise ServiceError("agent_kind_unset", status=409)
        if not cap.kind_supported: raise ServiceError("agent_kind_unsupported", status=409)
        if cap.herdr_probe == ProbeStatus.NOT_RUNNING:
            authorize()
            result = self.runner(("systemctl", "--user", "start", "omodachi-herdr.service"))
            if result.returncode: raise ServiceError("herdr_service_unavailable", status=503)
            cap, _ = self.probe.inspect()
        if not cap.herdr_available: raise ServiceError("herdr_unavailable", status=503)
        if cap.default_agent_exists:
            if cap.kind_mismatch: raise ServiceError("agent_kind_mismatch", status=409)
            if not cap.pane_available: raise ServiceError("agent_pane_unavailable", status=409)
            return cap
        if cap.default_agent_probe != ProbeStatus.MISSING:
            raise ServiceError("agent_state_unknown", status=409)
        pane_id = None
        owned_path = self.root / "owned-herdr-pane.json"
        try:
            owned = read_private_json(owned_path)
            if owned.get("session") == SESSION:
                candidate = identifier(owned.get("pane_id"))
                result = self.runner(("herdr", "--session", SESSION, "pane", "get", candidate))
                if not result.returncode: pane_id = candidate
        except FileNotFoundError:
            pass
        if pane_id is None:
            authorize()
            self.cwd.mkdir(parents=True, exist_ok=True)
            result = self.runner(("herdr", "--session", SESSION, "workspace", "create", "--cwd", str(self.cwd), "--label", "Omodachi", "--no-focus"))
            if result.returncode: raise ServiceError(self.error_code(result, "agent_pane_create_failed"), status=409)
            payload = self.payload(result).get("result", {})
            pane_id = identifier(payload.get("root_pane", {}).get("pane_id"))
            atomic_private_json(owned_path, {"session": SESSION, "pane_id": pane_id})
        authorize()
        result = self.runner(("herdr", "--session", SESSION, "agent", "start", "default", "--kind", cap.configured_kind,
                              "--pane", pane_id, "--timeout", "10000"))
        current, _ = self.probe.inspect()
        if current.default_agent_exists and current.pane_available:
            return current
        raise ServiceError(self.error_code(result, "agent_start_unconfirmed"), status=409)

    def ensure(self, authorize=lambda: None):
        # A structured default is the same product default. Do not start a
        # second interactive agent because Herdr has no pane for it yet.
        owner_path=self.root/'structured-default'/'owner.json'
        try:owned=read_private_json(owner_path)
        except FileNotFoundError:owned=None
        if owned is not None and owned.get('mode')!='legacy_handoff_rolled_back':
            authorize()
            if not owned.get('thread_id'):raise ServiceError('agent_thread_creation_unconfirmed',status=409)
            return {'agent_id':'default','surface':'chat','status':'unknown','ready_to_attach':True,
                'route':{'route':'native','supported':True,'native_view':'agent-chat'},
                'provider_session_id':owned['thread_id']}
        with _registry_lock(self.root / "agent-lifecycle"):
            cap = self._ensure(authorize)
            return {"agent_id": "default", "pane_id": cap.pane_id,
                    "status": cap.agent_status.value, "ready_to_attach": cap.ready_to_attach,
                    "route": {"route": "terminal", "supported": True, "argv": list(ATTACH_ARGV)}}

    def submit(self, params: dict, device: str, authorize=lambda: None):
        try:owned=read_private_json(self.root/'structured-default'/'owner.json')
        except FileNotFoundError:owned=None
        if owned is not None and owned.get('mode')!='legacy_handoff_rolled_back':
            raise ServiceError('structured_agent_endpoint_required',status=409)
        fields(params, ("request_id", "agent_id", "text"))
        request_id = identifier(params["request_id"])
        text = params["text"]
        if params["agent_id"] != "default": raise ServiceError("invalid_agent_target")
        if not isinstance(text, str) or not text.strip() or "\0" in text or len(text.encode()) > 60000:
            raise ServiceError("invalid_task")
        key = hashlib.sha256((device + "\0" + request_id).encode()).hexdigest()
        fingerprint = hashlib.sha256(text.encode()).hexdigest()
        journal_path = self.root / "agent-requests.json"
        with _registry_lock(self.root / "agent-lifecycle"):
            try: journal = read_private_json(journal_path)
            except FileNotFoundError: journal = {}
            previous = journal.get(key)
            if previous:
                if previous["fingerprint"] != fingerprint: raise ServiceError("request_conflict", status=409)
                return previous["result"]
            cap = self._ensure(authorize)
            if cap.agent_status == AgentStatus.BLOCKED: raise ServiceError("agent_blocked", status=409)
            if cap.agent_status == AgentStatus.WORKING: raise ServiceError("agent_busy", status=409)
            if cap.agent_status not in {AgentStatus.IDLE, AgentStatus.DONE}: raise ServiceError("agent_state_unknown", status=409)
            result = {"request_id": request_id, "agent_id": "default", "pane_id": cap.pane_id, "status": "unknown", "delivery": "unknown"}
            journal[key] = {"fingerprint": fingerprint, "result": result}
            while len(journal) > 256: del journal[next(iter(journal))]
            # Persist before sending. A process crash/transport loss cannot
            # re-submit the same task; a retry reports delivery as unconfirmed.
            atomic_private_json(journal_path, journal)
            authorize()
            output = self.runner(("herdr", "--session", SESSION, "agent", "prompt", "default", text))
            result = {**result, **self.delivery_result(output, pane_id=cap.pane_id, kind=cap.actual_kind)}
            journal[key]["execution"] = {"operation": "prompt", "returncode": output.returncode,
                                          "error": output.error, "delivery": result["delivery"]}
            journal[key]["result"] = result
            atomic_private_json(journal_path, journal)
            return result
