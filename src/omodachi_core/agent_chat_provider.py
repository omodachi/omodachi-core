"""Official Codex JSON app-server adapter for an already-owned default agent.

No terminal parsing, credential inspection, thread creation or process manager.
The existing default-agent manager must supply a verified provider thread AND
its owning app-server socket. A bare Herdr PTY or an inferred transcript path
cannot satisfy this contract. HTTP/lifecycle integration is deliberately external.
Protocol: https://developers.openai.com/codex/app-server/
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import copy
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Callable


# The four server->client requests this adapter renders as chat approvals. The
# legacy `execCommandApproval`/`applyPatchApproval` pair is deliberately absent:
# a client written against 0.154 uses the `item/*` family.
APPROVAL_KINDS = {"item/commandExecution/requestApproval": "commandExecution",
                  "item/fileChange/requestApproval": "fileChange",
                  "item/permissions/requestApproval": "permissions",
                  "item/tool/requestUserInput": "userInput"}
DECISIONS = ("accept", "acceptForSession", "decline", "cancel")
AUTH_REFRESH = "account/chatgptAuthTokens/refresh"
MAX_SUMMARY = 400


def read_codex_chatgpt_tokens(codex_home=None):
    """Read `auth.json` the Codex CLI itself maintains. Read-only, never cached.

    `account/chatgptAuthTokens/refresh` asks the client for the current ChatGPT
    credential. Core does not mint one: it re-reads the credential store the
    provider owns, so a refresh performed by `codex login` is what answers.
    """
    home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        value = json.loads((home / "auth.json").read_text())
    except (OSError, ValueError):
        raise AgentChatError("agent_auth_tokens_unavailable") from None
    tokens = value.get("tokens") if isinstance(value, dict) else None
    tokens = tokens if isinstance(tokens, dict) else {}
    access, account = tokens.get("access_token"), tokens.get("account_id")
    if not isinstance(access, str) or not access or not isinstance(account, str) or not account:
        raise AgentChatError("agent_auth_tokens_unavailable")
    answer = {"accessToken": access, "chatgptAccountId": account}
    plan = value.get("chatgpt_plan_type") if isinstance(value, dict) else None
    if isinstance(plan, str) and plan:
        answer["chatgptPlanType"] = plan
    return answer


def _text(value, limit=MAX_SUMMARY):
    return value[:limit] if isinstance(value, str) and value.strip() else None


def _scalars(value, *, limit=16):
    """Bounded projection: scalars pass, everything else is dropped.

    Approval detail is rendered on a phone, so it carries no nested provider
    structures and no unbounded strings.
    """
    if not isinstance(value, dict):
        return {}
    row = {}
    for key, item in value.items():
        if not isinstance(key, str) or len(row) >= limit:
            continue
        if type(item) in (bool, int):
            row[key] = item
        elif isinstance(item, str):
            row[key] = item[:MAX_SUMMARY]
    return row


def approval_view(kind, params):
    """One phone-renderable row per server request: summary plus bounded detail."""
    params = params if isinstance(params, dict) else {}
    base = {"item_id": _text(params.get("itemId"), 128), "turn_id": _text(params.get("turnId"), 128)}
    if kind == "commandExecution":
        summary = _text(params.get("command")) or "Run a command"
        detail = {**base, "command": _text(params.get("command"), 2000),
                  "cwd": _text(params.get("cwd"), 1024), "command_kind": _text(params.get("kind"), 64),
                  "approval_id": _text(params.get("approvalId"), 128)}
    elif kind == "fileChange":
        summary = _text(params.get("reason")) or "Apply file changes"
        detail = {**base, "reason": _text(params.get("reason"), 2000),
                  "grant_root": _text(params.get("grantRoot"), 1024)}
    elif kind == "permissions":
        summary = _text(params.get("reason")) or "Grant additional permissions"
        detail = {**base, "reason": _text(params.get("reason"), 2000),
                  "cwd": _text(params.get("cwd"), 1024),
                  "permissions": {key: _scalars(value) for key, value in
                                  (params.get("permissions") or {}).items() if isinstance(key, str)}}
    else:
        questions = [row for row in (params.get("questions") or []) if isinstance(row, dict)]
        summary = (_text(questions[0].get("header")) or _text(questions[0].get("question"))
                   if questions else None) or "The agent needs your input"
        detail = {**base, "blocking": bool(params.get("isBlocking")),
                  "questions": [{"id": _text(row.get("id"), 128), "header": _text(row.get("header")),
                                 "question": _text(row.get("question"), 2000),
                                 "secret": bool(row.get("isSecret")),
                                 "options": [_text(option.get("label") or option.get("value"))
                                             for option in (row.get("options") or [])[:16]
                                             if isinstance(option, dict)]}
                                for row in questions[:8]]}
    return summary, {key: value for key, value in detail.items() if value is not None}


class AgentChatError(Exception):
    def __init__(self, code: str, *, method=None, rpc_code=None):
        self.code = code
        self.method = method
        self.rpc_code = rpc_code
        super().__init__(code)


LOOPBACK_WS = re.compile(r"ws://127\.0\.0\.1:(?:[1-9][0-9]{2,4})/?\Z")


@dataclass(frozen=True)
class DefaultAgentBinding:
    """Where the manager-owned app-server listens, and the token to present.

    Two endpoint forms exist, both produced by the manager and never by a
    client: a loopback `ws://127.0.0.1:PORT` (the current transport, with a
    capability token) and an absolute Unix socket path (the previous one).
    """
    host_id: str
    thread_id: str
    endpoint: str
    token: str | None = None
    agent_id: str = "default"
    provider: str = "codex"

    def __post_init__(self):
        if (not self.host_id or not self.thread_id or self.agent_id != "default"
                or self.provider != "codex" or not isinstance(self.endpoint, str)
                or not (LOOPBACK_WS.fullmatch(self.endpoint) or Path(self.endpoint).is_absolute())
                or (self.token is not None and (not isinstance(self.token, str) or not self.token))):
            raise AgentChatError("structured_agent_binding_required")

    @property
    def daemon_socket(self) -> str:
        return self.endpoint

    @classmethod
    def from_ensured_agent(cls, host_id: str, agent: dict, *, endpoint: str, token: str | None = None):
        """endpoint/token come from manager-owned metadata, never App parameters.

        Herdr AgentSessionInfo is an identity hint; the loaded-thread check in
        attach() also requires that this exact ID belongs to the selected
        daemon. Path references are not guessed into IDs or read as files.
        """
        session = agent.get("agent_session") or {}
        if (agent.get("name") != "default" or agent.get("agent") != "codex"
                or session.get("agent") != "codex" or session.get("kind") != "id"
                or not isinstance(session.get("value"), str)):
            raise AgentChatError("structured_agent_binding_required")
        return cls(host_id, session["value"], endpoint, token)

    @property
    def remote_url(self) -> str:
        return self.endpoint if self.endpoint.startswith("ws://") else "unix://" + self.endpoint

    def identity(self):
        return {"hostID": self.host_id, "agentID": self.agent_id,
                "provider": self.provider, "providerSessionID": self.thread_id}


class CodexJSONRPC:
    """Bounded JSONL stdio client. Closing kills only our proxy, not its daemon."""
    def __init__(self, process, *, timeout: float = 15):
        self.process, self.timeout = process, timeout
        self.pending = {}
        self.next_id = 0
        self.ordinal = 0
        self.on_notification: Callable[[dict, int], None] = lambda message, ordinal: None
        self.on_disconnect: Callable[[], None] = lambda: None
        self.server_requests = asyncio.Queue()
        # An unhandled server request stalls the provider forever, so a handler
        # that declines is still a handler. The default one only queues.
        self.on_server_request: Callable[[dict], None] = self.server_requests.put_nowait
        self.task = asyncio.create_task(self._read())

    @classmethod
    async def spawn(cls, argv, *, env=None, timeout=15):
        process = await asyncio.create_subprocess_exec(*argv, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024)
        return cls(process, timeout=timeout)

    async def _write(self, value):
        self.process.stdin.write((json.dumps(value, separators=(",", ":")) + "\n").encode())
        await self.process.stdin.drain()

    async def request_fenced(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._write({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, self.timeout)
        except AgentChatError as error:
            error.method = method
            # Fixed method and numeric code only; never provider messages/IDs.
            if error.code == "provider_request_rejected":
                import logging
                logging.getLogger(__name__).warning("agent provider rejected method=%s rpc_code=%s", method, error.rpc_code)
            raise
        finally:
            self.pending.pop(request_id, None)

    async def request(self, method, params):
        result, _ = await self.request_fenced(method, params)
        return result

    async def initialize(self):
        result = await self.request("initialize", {
            "clientInfo": {"name": "omodachi_host", "title": "Omodachi", "version": "0.1.0"},
            "capabilities": {"experimentalApi": False}})
        await self._write({"method": "initialized", "params": {}})
        return result

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                self.ordinal += 1
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise AgentChatError("provider_protocol_invalid")
                self._dispatch(value)
        except (ValueError, OSError, asyncio.LimitOverrunError, AgentChatError):
            pass
        finally:
            self.on_disconnect()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(AgentChatError("provider_disconnected"))

    def _dispatch(self, value):
        if "method" in value:
            if "id" in value:
                # Approval/input requests stay explicit. Integration must
                # render/respond; this adapter never silently approves.
                self.on_server_request(value)
            else:
                self.on_notification(value, self.ordinal)
        elif value.get("id") in self.pending:
            future = self.pending[value["id"]]
            if not future.done():
                if "error" in value:
                    error = value.get("error") or {}
                    message = error.get("message", "")
                    code = "provider_request_rejected"
                    # Classify two documented fresh-thread states without
                    # retaining or exposing the provider's ID-bearing text.
                    if error.get("code") == -32600 and "is not materialized yet" in message and "includeTurns" in message:
                        code = "provider_thread_not_materialized"
                    elif error.get("code") == -32600 and message.startswith("no rollout found for thread id "):
                        code = "provider_thread_rollout_missing"
                    elif error.get("code") == -32600 and message.startswith("thread not loaded: "):
                        code = "provider_thread_not_loaded"
                    elif error.get("code") == -32601 and message == "list_turns is not supported yet":
                        # 0.154's answer for a thread that exists in memory but
                        # has no rollout yet. Same state as the documented
                        # missing-rollout error, reported as a missing method.
                        code = "provider_thread_rollout_missing"
                    future.set_exception(AgentChatError(code, rpc_code=error.get("code") if type(error.get("code")) is int else None))
                else:
                    future.set_result((value.get("result", {}), self.ordinal))

    async def respond(self, request_id, result):
        """Only the authenticated Host integration answers an actual request."""
        await self._write({"id": request_id, "result": result})

    async def respond_error(self, request_id, code, message):
        """Declining is an answer. A server request left unanswered hangs a turn."""
        await self._write({"id": request_id, "error": {"code": code, "message": message}})

    async def close(self):
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        if self.process.stdin:
            self.process.stdin.close()
        if self.process.returncode is None:
            self.process.terminate()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.process.wait(), 2)
            if self.process.returncode is None:
                self.process.kill()
                await self.process.wait()


class CodexWebSocketRPC(CodexJSONRPC):
    """The official transport in every listen mode is a WebSocket upgrade.

    `--listen unix://PATH` and `--listen ws://IP:PORT` differ only in the
    connector and, for the loopback TCP listener, the capability token this
    client presents as an ordinary bearer credential. No proxy subprocess and
    no daemon restart in either case.
    """
    @classmethod
    async def connect(cls, url, *, token=None, connector=None, timeout=15):
        import aiohttp
        # No total timeout: this connection is the whole session. A bounded
        # total is what makes a long-lived agent chat die after an hour.
        session = aiohttp.ClientSession(connector=connector,
                                        timeout=aiohttp.ClientTimeout(total=None, connect=timeout,
                                                                      sock_connect=timeout))
        headers = {"Authorization": "Bearer " + token} if token else {}
        try:
            ws = await asyncio.wait_for(session.ws_connect(url, headers=headers, heartbeat=30,
                                                           max_msg_size=4*1024*1024), timeout)
        except BaseException:
            await session.close()
            raise AgentChatError("agent_owner_socket_unavailable") from None
        value = cls.__new__(cls)
        value.session, value.ws = session, ws
        CodexJSONRPC.__init__(value, None, timeout=timeout)
        return value

    async def _write(self, value):
        await self.ws.send_json(value)

    async def _read(self):
        import aiohttp
        try:
            async for message in self.ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    value = json.loads(message.data)
                    if not isinstance(value, dict):
                        raise AgentChatError("provider_protocol_invalid")
                    self.ordinal += 1
                    self._dispatch(value)
                elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                    break
        except (ValueError, OSError, AgentChatError, aiohttp.ClientError):
            pass
        finally:
            self.on_disconnect()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(AgentChatError("provider_disconnected"))

    async def close(self):
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        await self.ws.close()
        await self.session.close()


class CodexUnixRPC(CodexWebSocketRPC):
    """`--listen unix://PATH`, kept for an owner that still holds a socket path."""
    @classmethod
    async def connect(cls, socket_path, *, timeout=15):
        import aiohttp
        return await CodexWebSocketRPC.connect.__func__(
            cls, "http://localhost/", connector=aiohttp.UnixConnector(path=str(socket_path)), timeout=timeout)


_USAGE_FIELDS = ("inputTokens", "cachedInputTokens", "cacheWriteInputTokens",
                 "outputTokens", "reasoningOutputTokens", "totalTokens")
_WINDOW_FIELDS = ("usedPercent", "resetsAt", "windowDurationMins")


def _scalar_usage(value):
    if not isinstance(value, dict):
        return None
    row = {}
    for key in ("last", "total"):
        part = value.get(key)
        if isinstance(part, dict):
            row[key] = {field: part[field] for field in _USAGE_FIELDS if type(part.get(field)) is int}
    window = value.get("modelContextWindow")
    if type(window) is int:
        row["modelContextWindow"] = window
    return row or None


def _scalar_rate_limits(value):
    if not isinstance(value, dict):
        return {}
    row = {}
    for key in ("primary", "secondary"):
        window = value.get(key)
        if isinstance(window, dict):
            row[key] = {field: window[field] for field in _WINDOW_FIELDS if type(window.get(field)) is int}
    for key in ("planType", "limitName"):
        if isinstance(value.get(key), str):
            row[key] = value[key][:64]
    credits = value.get("credits")
    if isinstance(credits, dict):
        row["credits"] = _scalars(credits)
    return row


def _tool_status(status):
    return {"inProgress": "running", "completed": "succeeded", "failed": "failed",
            "declined": "cancelled"}.get(status, "unknown")


def project_item(item, turn_id):
    """Map official item types; ignore hidden reasoning and unfamiliar types."""
    kind, ident = item.get("type"), item.get("id")
    if not isinstance(ident, str):
        return None
    base = {"id": ident, "turnID": turn_id}
    if kind == "agentMessage":
        return {"kind": "message", **base, "role": "assistant", "text": item.get("text", "")}
    if kind == "userMessage":
        text = "\n".join(x.get("text", "") for x in item.get("content", []) if x.get("type") == "text")
        return {"kind": "message", **base, "role": "user", "text": text}
    if kind == "commandExecution":
        return {"kind": "tool", **base, "name": item.get("command", "Command"),
                "status": _tool_status(item.get("status")), "detail": item.get("aggregatedOutput") or ""}
    if kind == "fileChange":
        return {"kind": "tool", **base, "name": "File changes", "status": _tool_status(item.get("status")),
                "detail": json.dumps(item.get("changes", []), ensure_ascii=False)}
    if kind == "mcpToolCall":
        return {"kind": "tool", **base, "name": item.get("tool", "Tool"),
                "status": _tool_status(item.get("status")),
                "detail": json.dumps(item.get("result") or item.get("error") or {}, ensure_ascii=False)}
    return None


class CodexDefaultAgentChat:
    def __init__(self, binding: DefaultAgentBinding, rpc: CodexJSONRPC, *,
                 delivery_journal=None, persist_delivery=lambda journal: None, initial_sequence=0,
                 auth_provider=None):
        self.binding, self.rpc = binding, rpc
        # Server->client requests waiting for a human. The provider's own
        # request id is the handle; nothing here is invented by a client.
        self.pending_approvals = {}
        self.status = {"type": "unknown", "activeFlags": []}
        self.usage = {"tokens": None, "rate_limits": None}
        self.settings = {"model": None, "effort": None}
        self.auth_provider = auth_provider or read_codex_chatgpt_tokens
        self._answers = set()
        self.rows = {}
        self.active_turn = None
        self.sequence = initial_sequence
        self.attached = False
        self._buffer = []
        self._loading = False
        self._send_lock = asyncio.Lock()
        self._requests = delivery_journal if delivery_journal is not None else {}
        self._persist_delivery = persist_delivery
        self.events = asyncio.Queue(maxsize=256)
        rpc.on_notification = self._receive
        rpc.on_server_request = self._server_request
        rpc.on_disconnect = self._disconnected

    @classmethod
    async def connect_existing(cls, binding, *, owner_rpc=None, **state_options):
        rpc = owner_rpc or await CodexWebSocketRPC.connect(binding.endpoint, token=binding.token)
        adapter = cls(binding, rpc, **state_options)
        try:
            if owner_rpc is None:
                await rpc.initialize()
            await adapter.attach()
            return adapter
        except BaseException:
            await rpc.close()
            raise

    async def attach(self):
        # Do not resume a dormant persisted thread in a different daemon while
        # an unrelated TUI is actually the running default agent.
        cursor = None
        while True:
            params = {"limit": 100}
            if cursor is not None:
                params["cursor"] = cursor
            page = await self.rpc.request("thread/loaded/list", params)
            if self.binding.thread_id in page.get("data", []):
                break
            cursor = page.get("nextCursor")
            if cursor is None:
                raise AgentChatError("default_agent_owner_not_attached")
        self._loading = True
        try:
            try:
                response, barrier = await self.rpc.request_fenced("thread/resume", {"threadId": self.binding.thread_id})
            except AgentChatError as error:
                if error.code != "provider_thread_rollout_missing":
                    raise
                # Already-loaded fresh threads have no persisted rollout until
                # first user message. Read their actual in-memory identity,
                # never start a substitute thread or invent empty history.
                response, barrier = await self._read_snapshot()
                if response.get("thread", {}).get("turns"):
                    raise AgentChatError("provider_thread_rollout_missing")
            self._restore(response.get("thread", {}))
            self.attached = True
            self._replay_after(barrier)
        finally:
            self._loading = False
        return self.snapshot_value()

    async def _read_snapshot(self):
        try:
            return await self.rpc.request_fenced("thread/read", {"threadId": self.binding.thread_id, "includeTurns": True})
        except AgentChatError as error:
            # Both spellings mean the same thing: this thread has no turns to
            # read yet. Read its identity instead of inventing empty history.
            if error.code not in ("provider_thread_not_materialized", "provider_thread_rollout_missing"):
                raise
            return await self.rpc.request_fenced("thread/read", {"threadId": self.binding.thread_id, "includeTurns": False})

    async def snapshot(self):
        if not self.attached:
            raise AgentChatError("agent_not_attached")
        self._loading = True
        try:
            response, barrier = await self._read_snapshot()
            # Existing subscribers must still receive real events that preceded
            # this read response; a snapshot does not create a fake sequence gap.
            for value, ordinal in self._buffer:
                if ordinal <= barrier:
                    self._apply(value)
            self._restore(response.get("thread", {}))
            self._replay_after(barrier)
        finally:
            self._loading = False
        return self.snapshot_value()

    def _restore(self, thread):
        if thread.get("id") != self.binding.thread_id:
            raise AgentChatError("provider_identity_changed")
        rows = {}
        active = None
        for turn in thread.get("turns", []):
            if turn.get("status") == "inProgress":
                active = turn["id"]
            for item in turn.get("items", []):
                client_id = item.get("clientId")
                if item.get("type") == "userMessage" and client_id in self._requests:
                    self._requests[client_id]["accepted"] = True
                row = project_item(item, turn["id"])
                if row:
                    rows[row["id"]] = row
        self._persist_delivery(copy.deepcopy(self._requests))
        self.rows, self.active_turn = rows, active

    def _replay_after(self, barrier):
        queued, self._buffer = self._buffer, []
        for value, ordinal in queued:
            if ordinal > barrier:
                self._apply(value)

    def snapshot_value(self):
        return {"identity": self.binding.identity(), "rows": copy.deepcopy(list(self.rows.values())),
                "activeTurn": self.active_turn, "sequence": self.sequence,
                "acceptedRequestIDs": [k for k, v in self._requests.items() if v.get("accepted")],
                "pendingApprovals": self.approvals_value(), "status": self.status_value(),
                "usage": self.usage_value()}

    def approvals_value(self):
        return [{key: copy.deepcopy(row[key]) for key in ("request_id", "kind", "summary", "details", "decisions")}
                for row in self.pending_approvals.values()]

    def status_value(self):
        flags = [flag for flag in self.status.get("activeFlags") or [] if isinstance(flag, str)]
        return {"type": self.status.get("type"), "activeFlags": flags,
                "waiting": "approval" if "waitingOnApproval" in flags
                           else "user_input" if "waitingOnUserInput" in flags else None}

    def usage_value(self):
        return {"tokens": copy.deepcopy(self.usage.get("tokens")),
                "rate_limits": copy.deepcopy(self.usage.get("rate_limits")),
                "model": self.settings.get("model"), "effort": self.settings.get("effort")}

    # --- server->client requests --------------------------------------------
    def _background(self, coroutine):
        task = asyncio.ensure_future(coroutine)
        self._answers.add(task)
        task.add_done_callback(lambda job: (self._answers.discard(job),
                                            None if job.cancelled() else job.exception()))

    def _server_request(self, value):
        """Render an approval, answer the credential refresh, decline the rest."""
        method, request_id = value.get("method"), value.get("id")
        params = value.get("params") if isinstance(value.get("params"), dict) else {}
        if method == AUTH_REFRESH:
            self._background(self._answer_auth(request_id))
            return
        kind = APPROVAL_KINDS.get(method)
        if kind is None or params.get("threadId") not in (None, self.binding.thread_id):
            self._background(self._decline_unknown(request_id, method))
            return
        summary, details = approval_view(kind, params)
        key = str(request_id)
        self.pending_approvals[key] = {"request_id": key, "kind": kind, "summary": summary,
                                       "details": details, "decisions": [] if kind == "userInput" else list(DECISIONS),
                                       "provider_id": request_id, "params": params}
        self._emit({"type": "agent.approval.requested", "request_id": key, "kind": kind,
                    "summary": summary, "details": details,
                    "decisions": [] if kind == "userInput" else list(DECISIONS)})

    async def _answer_auth(self, request_id):
        try:
            answer = await asyncio.to_thread(self.auth_provider)
        except Exception:
            with suppress(Exception):
                await self.rpc.respond_error(request_id, -32001, "chatgpt auth tokens unavailable")
            self._emit({"type": "agent.auth.refreshed", "refreshed": False})
            return
        with suppress(Exception):
            await self.rpc.respond(request_id, answer)
        # The credential itself never reaches an event, a log or a client.
        self._emit({"type": "agent.auth.refreshed", "refreshed": True})

    async def _decline_unknown(self, request_id, method):
        with suppress(Exception):
            await self.rpc.respond_error(request_id, -32601, "unsupported server request")
        self._emit({"type": "agent.request.declined", "method": method if isinstance(method, str) else None})

    async def resolve_approval(self, request_id, *, decision=None, answers=None):
        row = self.pending_approvals.get(str(request_id))
        if row is None:
            raise AgentChatError("agent_approval_unknown")
        if row["kind"] == "userInput":
            if decision is not None or not isinstance(answers, dict) or not answers:
                raise AgentChatError("agent_approval_input_required")
            payload = {}
            for key, value in list(answers.items())[:8]:
                if (not isinstance(key, str) or not key or not isinstance(value, list) or not value
                        or any(not isinstance(item, str) or len(item) > 4000 for item in value)):
                    raise AgentChatError("agent_approval_input_invalid")
                payload[key] = {"answers": list(value[:8])}
            result, applied = {"answers": payload}, "input"
        else:
            if answers is not None or decision not in DECISIONS:
                raise AgentChatError("agent_approval_decision_invalid")
            if row["kind"] == "permissions":
                # The granted profile mirrors the requested one; declining
                # grants nothing rather than inventing a narrower profile.
                requested = row["params"].get("permissions")
                granted = {key: value for key, value in (requested or {}).items()
                           if key in ("fileSystem", "network")} if decision in ("accept", "acceptForSession") else {}
                result = {"permissions": granted,
                          "scope": "session" if decision == "acceptForSession" else "turn"}
            else:
                result = {"decision": decision}
            applied = decision
        await self.rpc.respond(row["provider_id"], result)
        self.pending_approvals.pop(str(request_id), None)
        self._emit({"type": "agent.approval.resolved", "request_id": str(request_id),
                    "kind": row["kind"], "decision": applied, "source": "client"})
        return {"request_id": str(request_id), "kind": row["kind"], "decision": applied, "resolved": True}

    # --- models, steering ----------------------------------------------------
    async def models(self):
        rows, cursor, seen = [], None, set()
        while len(rows) < 200:
            page = await self.rpc.request("model/list", {} if cursor is None else {"cursor": cursor})
            for model in page.get("data", []) or []:
                if not isinstance(model, dict) or not isinstance(model.get("id"), str) or model["id"] in seen:
                    continue
                seen.add(model["id"])
                rows.append({"id": model["id"], "model": _text(model.get("model"), 128),
                             "display_name": _text(model.get("displayName"), 128),
                             "description": _text(model.get("description"), 1000),
                             "hidden": bool(model.get("hidden")), "is_default": bool(model.get("isDefault")),
                             "default_effort": _text(model.get("defaultReasoningEffort"), 64),
                             "efforts": [_text(option.get("reasoningEffort"), 64)
                                         for option in (model.get("supportedReasoningEfforts") or [])[:16]
                                         if isinstance(option, dict) and _text(option.get("reasoningEffort"), 64)]})
            cursor = page.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
        return {"models": rows, "default": next((row["id"] for row in rows if row["is_default"]), None)}

    async def steer(self, text: str, request_id: str):
        """Add to the turn in flight without interrupting it (`turn/steer`)."""
        if not self.attached or not isinstance(text, str) or not text.strip():
            raise AgentChatError("agent_not_ready")
        turn = self.active_turn
        if turn is None:
            raise AgentChatError("agent_not_working")
        await self.rpc.request("turn/steer", {"threadId": self.binding.thread_id, "expectedTurnId": turn,
                                              "clientUserMessageId": request_id,
                                              "input": [{"type": "text", "text": text}]})
        return {"accepted": True, "turnID": turn, "delivery": "steered"}

    def _receive(self, value, ordinal):
        method = value.get("method")
        params = value.get("params") if isinstance(value.get("params"), dict) else {}
        # Account-scoped notifications (rate limits) name no thread; everything
        # else must name ours or it belongs to a different conversation.
        if params.get("threadId") != self.binding.thread_id and not (
                isinstance(method, str) and method.startswith("account/") and "threadId" not in params):
            return
        if self._loading:
            self._buffer.append((value, ordinal))
        elif self.attached:
            self._apply(value)

    def _disconnected(self):
        self.attached = False
        self._emit({"type": "connectionLost"})

    def _emit(self, event):
        self.sequence += 1
        if self.events.full():
            self.events.get_nowait()  # Consumer sees a gap and must resnapshot.
        self.events.put_nowait({"identity": self.binding.identity(), "sequence": self.sequence, "event": copy.deepcopy(event)})

    def _apply(self, value):
        method, p = value.get("method"), value.get("params", {})
        if method == "thread/status/changed":
            status = p.get("status") if isinstance(p.get("status"), dict) else {}
            self.status = {"type": status.get("type"),
                           "activeFlags": [flag for flag in (status.get("activeFlags") or [])
                                           if isinstance(flag, str)][:8]}
            self._emit({"type": "agent.status.changed", "status": self.status_value()})
            return
        if method == "thread/tokenUsage/updated":
            self.usage["tokens"] = _scalar_usage(p.get("tokenUsage"))
            self._emit({"type": "agent.usage.updated", "usage": self.usage_value()})
            return
        if method == "account/rateLimits/updated":
            # Sparse rolling update: merge, never clear a value it omits.
            merged = dict(self.usage.get("rate_limits") or {})
            merged.update(_scalar_rate_limits(p.get("rateLimits")))
            self.usage["rate_limits"] = merged
            self._emit({"type": "agent.usage.updated", "usage": self.usage_value()})
            return
        if method == "thread/settings/updated":
            settings = p.get("settings") if isinstance(p.get("settings"), dict) else p
            for key, field in (("model", "model"), ("effort", "effort")):
                value_read = settings.get(field)
                if isinstance(value_read, str) and value_read:
                    self.settings[key] = value_read[:64]
            self._emit({"type": "agent.usage.updated", "usage": self.usage_value()})
            return
        if method == "serverRequest/resolved":
            key = str(p.get("requestId"))
            if self.pending_approvals.pop(key, None) is not None:
                self._emit({"type": "agent.approval.resolved", "request_id": key,
                            "decision": None, "source": "elsewhere"})
            return
        if method == "turn/started":
            self.active_turn = p["turn"]["id"]
            self._emit({"type": "turnStarted", "turnID": self.active_turn})
        elif method in {"item/started", "item/completed"}:
            row = project_item(p.get("item", {}), p.get("turnId"))
            if row:
                self.rows[row["id"]] = row
                self._emit({"type": row["kind"], "value": row})
        elif method in {"item/agentMessage/delta", "item/commandExecution/outputDelta"}:
            row = self.rows.get(p.get("itemId"))
            if row and row.get("turnID") == p.get("turnId"):
                field = "text" if method == "item/agentMessage/delta" else "detail"
                row[field] += p.get("delta", "")
                self._emit({"type": row["kind"], "value": dict(row)})
        elif method == "turn/completed":
            turn = p["turn"]
            if turn["id"] != self.active_turn:
                return
            self.active_turn = None
            if turn.get("status") == "failed":
                self._emit({"type": "turnFailed", "turnID": turn["id"], "message": "The agent turn failed."})
            else:
                self._emit({"type": "turnFinished", "turnID": turn["id"], "interrupted": turn.get("status") == "interrupted"})

    async def send(self, text: str, request_id: str, *, model=None, effort=None):
        async with self._send_lock:
            return await self._send(text, request_id, model=model, effort=effort)

    async def _send(self, text: str, request_id: str, *, model=None, effort=None):
        if not self.attached or not isinstance(text, str) or not text.strip():
            raise AgentChatError("agent_not_ready")
        fingerprint = hashlib.sha256(text.encode()).hexdigest()
        previous = self._requests.get(request_id)
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise AgentChatError("request_conflict")
            return {"accepted": previous.get("accepted", False), "delivery": "accepted" if previous.get("accepted") else "unknown"}
        if self.active_turn is not None:
            raise AgentChatError("agent_busy")
        self._requests[request_id] = {"fingerprint": fingerprint, "accepted": False}
        self._persist_delivery(copy.deepcopy(self._requests))
        # Model and reasoning effort are per-turn overrides; codex has no
        # separate setter, and `thread/settings/updated` confirms what landed.
        params = {"threadId": self.binding.thread_id, "clientUserMessageId": request_id,
                  "input": [{"type": "text", "text": text}]}
        if model is not None:
            params["model"] = model
        if effort is not None:
            params["effort"] = effort
        result = await self.rpc.request("turn/start", params)
        turn = result.get("turn", {})
        if not isinstance(turn.get("id"), str):
            raise AgentChatError("agent_delivery_unconfirmed")
        self._requests[request_id]["accepted"] = True
        self._persist_delivery(copy.deepcopy(self._requests))
        # The actual start response reserves the active turn even if its
        # turn/started notification has not arrived yet.
        if self.active_turn is None and turn.get("status") == "inProgress":
            self.active_turn = turn["id"]
        return {"accepted": True, "turnID": turn["id"], "delivery": "accepted"}

    async def cancel(self, turn_id):
        if not self.attached or turn_id != self.active_turn:
            raise AgentChatError("active_turn_changed")
        await self.rpc.request("turn/interrupt", {"threadId": self.binding.thread_id, "turnId": turn_id})
        return {"requested": True}  # Completion only comes from notification.

    async def close(self):
        self.attached = False
        for task in tuple(self._answers):
            task.cancel()
        self._answers.clear()
        await self.rpc.close()
