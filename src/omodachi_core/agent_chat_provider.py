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
import shlex
from typing import Callable


# The four server->client requests this adapter renders as chat approvals. The
# legacy `execCommandApproval`/`applyPatchApproval` pair is deliberately absent:
# a client written against 0.154 uses the `item/*` family.
APPROVAL_KINDS = {"item/commandExecution/requestApproval": "commandExecution",
                  "item/fileChange/requestApproval": "fileChange",
                  "item/permissions/requestApproval": "permissions",
                  "item/tool/requestUserInput": "userInput"}
DECISIONS = ("accept", "acceptForSession", "decline", "cancel")


def request_fingerprint(params) -> str:
    """AGENT-QUEUE-1 (Leo: "askuserquestion 的卡片 会循环"): what makes two server
    requests the same request - codex re-sends one still waiting, byte for
    byte (same id, item, turn, questions), to a connection that resumes the
    thread (real 0.160 frames, `ask_replay` in the capture)."""
    raw = json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def approval_fingerprint(row) -> str:
    """NOTIFY-1: 16 hex characters that change whenever what a pending
    approval shows changes - its kind, summary, details or decisions.

    A phone's alert carries it with the request id; core accepts a decision
    from the alert only while the same request still reads the same.
    """
    shown = {key: row.get(key) for key in ("request_id", "kind", "summary", "details", "decisions")}
    raw = json.dumps(shown, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
AUTH_REFRESH = "account/chatgptAuthTokens/refresh"
MAX_SUMMARY = 400
# AGENT-FIX-2: what a tool row carries to a phone. A command that prints
# megabytes used to travel whole - in every output delta and in every
# snapshot - until a frame passed the App's 1 MB limit and the stream broke.
# A row keeps the newest TOOL_DETAIL_LIMIT characters of its output and says
# how many it dropped (`detail_truncated`); output streams as increments
# (`tool.output`), at most one per item per OUTPUT_FLUSH_SECONDS; and a
# snapshot spends SNAPSHOT_DETAIL_BUDGET characters on tool output, newest
# rows first, cutting older rows to SNAPSHOT_OLD_DETAIL.
TOOL_DETAIL_LIMIT = 64 * 1024
SNAPSHOT_DETAIL_BUDGET = 512 * 1024
SNAPSHOT_OLD_DETAIL = 4 * 1024
OUTPUT_FLUSH_SECONDS = 0.1
# AGENT-COLLIE-1: a fileChange row carries the change codex made, so a phone
# can see an edit before allowing it - never more than codex put in its own
# item, and bounded: DIFF_FILES files per row, DIFF_FILE_LINES diff lines per
# file and DIFF_ROW_LINES per row, each line cut at DIFF_LINE_CHARS. A snapshot
# spends SNAPSHOT_DIFF_BUDGET characters on diffs, newest rows first; an older
# row keeps its counts and loses its lines (`diff_omitted` says how many).
DIFF_FILES = 16
DIFF_FILE_LINES = 120
DIFF_ROW_LINES = 400
DIFF_LINE_CHARS = 400
SNAPSHOT_DIFF_BUDGET = 256 * 1024
STEP_ACTIONS = 4
# AGENT-PLAN-1: codex's collaboration modes (`ModeKind` in 0.160). The phone
# switches between them; the provider owns what each one means.
MODES = ("default", "plan")
# AGENT-QUEUE-1: huge threads open fast. A snapshot carries the newest
# SNAPSHOT_ROWS rows and says whether there are `earlier` ones; a phone asks
# for those a page at a time (`history`). core reads codex's own record a page
# of HISTORY_PAGE_TURNS turns at a time (0.160 `thread/resume.initialTurnsPage`
# and `thread/turns/list`), newest first. A message longer than
# MESSAGE_TEXT_LIMIT characters travels cut, its `text_truncated` saying by how
# much (a tool row's output already does, AGENT-FIX-2).
SNAPSHOT_ROWS = 150
HISTORY_PAGE_TURNS = 10
HISTORY_LIMIT = 200
MESSAGE_TEXT_LIMIT = 64 * 1024
# AGENT-QUEUE-1: codex 0.160's own durable user-message queue (`thread/queue/*`,
# experimental). What a phone is shown of it: the first QUEUE_SHOWN messages,
# each text cut at QUEUE_TEXT characters (`text_clipped`).
QUEUE_SHOWN = 20
QUEUE_TEXT = 16000
# AGENT-QUEUE-1: codex's plan checklist (`turn/plan/updated`), bounded.
CHECKLIST_STEPS = 32
CHECKLIST_STATUSES = ("pending", "inProgress", "completed")
REASON_LIMIT = 2000


def clip_detail(row, limit=TOOL_DETAIL_LIMIT):
    """Keep the newest `limit` characters of a tool row's output, counting
    what went in `detail_truncated`. Returns the row."""
    detail = row.get("detail") or ""
    if len(detail) > limit:
        cut = len(detail) - limit
        row["detail"] = detail[cut:]
        row["detail_truncated"] = int(row.get("detail_truncated") or 0) + cut
    return row


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


def approval_view(kind, params, row=None):
    """One phone-renderable row per server request: summary plus bounded detail.
    `row` is the tool row the request is about (`itemId`), when core has it."""
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
        # AGENT-COLLIE-1: codex's request names the item, never the change; the
        # item (`item/started`, which 0.160 sends first) has it. The files and
        # the line counts ride along so an alert or an old client can say what
        # is being allowed; the diff itself stays on the row's change cards.
        changes = (row or {}).get("changes") or []
        if changes:
            detail["files"] = [change["path"][:MAX_SUMMARY] for change in changes[:8]]
            detail["added"] = sum(change["added"] for change in changes)
            detail["removed"] = sum(change["removed"] for change in changes)
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
                  "questions": [_question(row) for row in questions[:8]]}
    return summary, {key: value for key, value in detail.items() if value is not None}


def _question(row):
    """One `request_user_input` question, bounded. `options` stays the list of
    labels it always was; AGENT-COLLIE-1 adds `choices` (each label with the
    one-sentence `description` codex sends beside it) and `other` - codex's
    `isOther`, which 0.160 sets on every question the model asks: the person
    may answer in their own words instead of picking one."""
    options = [option for option in (row.get("options") or [])[:16] if isinstance(option, dict)]
    question = {"id": _text(row.get("id"), 128), "header": _text(row.get("header")),
                "question": _text(row.get("question"), 2000),
                "secret": bool(row.get("isSecret")),
                "options": [_text(option.get("label") or option.get("value")) for option in options],
                "choices": [{key: value for key, value in (
                                ("label", _text(option.get("label") or option.get("value"))),
                                ("description", _text(option.get("description")))) if value is not None}
                            for option in options],
                "other": bool(row.get("isOther"))}
    return question


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
    # The manager's current workspace. Codex keeps the cwd a thread was created
    # with; a thread made before the omadochi -> omodachi rename still names a
    # directory that no longer exists, and every command it starts fails with
    # "No such file or directory". Resume and every turn carry this one.
    cwd: str | None = None

    def __post_init__(self):
        if (not self.host_id or not self.thread_id or self.agent_id != "default"
                or self.provider != "codex" or not isinstance(self.endpoint, str)
                or not (LOOPBACK_WS.fullmatch(self.endpoint) or Path(self.endpoint).is_absolute())
                or (self.token is not None and (not isinstance(self.token, str) or not self.token))
                or (self.cwd is not None and (not isinstance(self.cwd, str) or not Path(self.cwd).is_absolute()))):
            raise AgentChatError("structured_agent_binding_required")

    def ensure_workspace(self):
        """The workspace is the user's; recreate it if it went missing."""
        if self.cwd is not None:
            os.makedirs(self.cwd, mode=0o755, exist_ok=True)

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
        # AGENT-PLAN-1: `experimentalApi` is on because codex 0.160 keeps every
        # way to choose a collaboration mode behind it - `turn/start` and
        # `thread/settings/update` refuse a `collaborationMode` ("requires
        # experimentalApi capability"), and `thread/settings/updated`, which
        # says the mode changed, only reaches connections that opted in. Core
        # still sends no `approvalPolicy` / `sandboxPolicy`: those stay Leo's,
        # in `~/.codex`. Only the fields core names are read from any answer,
        # so an experimental field it does not know changes nothing.
        result = await self.request("initialize", {
            "clientInfo": {"name": "omodachi_host", "title": "Omodachi", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True}})
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


def _paths(values, limit=32):
    """Bounded, de-duplicated path strings for a row's `paths`."""
    found = []
    for value in values:
        if isinstance(value, str) and value.strip() and len(value) <= 4096 and "\0" not in value \
                and value not in found:
            found.append(value)
        if len(found) >= limit:
            break
    return found


def _diff_lines(kind, diff):
    """codex 0.160's `FileUpdateChange.diff`, as unified-diff lines: an
    update is already hunks (`@@ … @@`, no file header); an added or deleted
    file is its whole text, unprefixed, so every line gets its sign here."""
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if kind == "add":
        return ["+" + line for line in lines]
    if kind == "delete":
        return ["-" + line for line in lines]
    # A file header, should one ever come, names the file twice over; the row
    # already names it.
    while lines and not lines[0].startswith("@@") and lines[0].startswith(
            ("diff --git ", "index ", "--- ", "+++ ", "new file mode", "deleted file mode")):
        lines.pop(0)
    return lines


def project_change(change, budget=DIFF_FILE_LINES):
    """One changed file of a fileChange item: `{path, kind, move_path?,
    added, removed, diff, diff_omitted?}`. `added` / `removed` count the whole
    diff; `diff` keeps its first `budget` lines."""
    if not isinstance(change, dict):
        return None
    path = _paths([change.get("path")])
    if not path:
        return None
    kind_value = change.get("kind") if isinstance(change.get("kind"), dict) else {}
    kind = kind_value.get("type") if kind_value.get("type") in ("add", "delete", "update") else "update"
    move = _paths([kind_value.get("move_path")])
    diff = change.get("diff") if isinstance(change.get("diff"), str) else ""
    if move:
        # 0.160 appends the move to the diff text; it is a field here.
        for tail in ("\n\nMoved to: " + move[0], "Moved to: " + move[0]):
            if diff.endswith(tail):
                diff = diff[:-len(tail)]
                break
    lines = _diff_lines(kind, diff)
    kept = [line[:DIFF_LINE_CHARS] for line in lines[:max(0, budget)]]
    row = {"path": path[0], "kind": kind,
           "added": sum(1 for line in lines if line.startswith("+")),
           "removed": sum(1 for line in lines if line.startswith("-")),
           "diff": "\n".join(kept)}
    if move:
        row["move_path"] = move[0]
    if len(lines) > len(kept):
        row["diff_omitted"] = len(lines) - len(kept)
    return row


def project_changes(changes):
    """A fileChange item's `changes`, bounded per file and per row."""
    rows, left, omitted = [], DIFF_ROW_LINES, 0
    for change in changes if isinstance(changes, list) else []:
        if len(rows) >= DIFF_FILES:
            omitted += 1
            continue
        row = project_change(change, min(DIFF_FILE_LINES, left))
        if row is not None:
            left -= len(row["diff"].split("\n")) if row["diff"] else 0
            rows.append(row)
    return rows, omitted


_SHELLS = {"sh", "bash", "zsh", "dash", "fish"}


def inner_command(command):
    """`/bin/zsh -lc 'git status'` -> `git status`: the words the agent
    actually ran, without the shell codex wraps them in. Anything else (or
    anything shlex cannot read) comes back unchanged."""
    if not isinstance(command, str) or not command.strip():
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return command[:MAX_SUMMARY]
    if len(argv) == 3 and os.path.basename(argv[0]) in _SHELLS and argv[1] in ("-c", "-lc", "-lic", "-ic"):
        return argv[2][:MAX_SUMMARY]
    return command[:MAX_SUMMARY]


def project_actions(actions):
    """codex's best-effort reading of a command (`commandActions`): what it
    read, listed or searched. Bounded, and without codex's absolute paths
    where a name says the same."""
    rows = []
    for action in actions if isinstance(actions, list) else []:
        if not isinstance(action, dict) or len(rows) >= STEP_ACTIONS:
            continue
        kind = action.get("type")
        if kind == "read":
            row = {"type": "read", "name": _text(action.get("name"), 256)
                   or _text(os.path.basename(action.get("path") or ""), 256)}
        elif kind == "listFiles":
            row = {"type": "listFiles", "path": _text(action.get("path"), 1024)}
        elif kind == "search":
            row = {"type": "search", "query": _text(action.get("query"), 256), "path": _text(action.get("path"), 1024)}
        else:
            row = {"type": "unknown"}
        rows.append({key: value for key, value in row.items() if value is not None})
    return rows


def project_item(item, turn_id):
    """Map official item types; ignore hidden reasoning and unfamiliar types.
    A tool row's output is clipped to its newest TOOL_DETAIL_LIMIT characters."""
    row = _project_item(item, turn_id)
    if row is not None and row["kind"] == "tool":
        clip_detail(row)
    return row


def _project_item(item, turn_id):
    kind, ident = item.get("type"), item.get("id")
    if not isinstance(ident, str):
        return None
    base = {"id": ident, "turnID": turn_id}
    if kind == "agentMessage":
        return {"kind": "message", **base, "role": "assistant", "text": item.get("text", "")}
    if kind == "plan":
        # AGENT-PLAN-1: in Plan mode codex lifts the model's <proposed_plan>
        # block out of its reply into an item of its own (0.160: `ThreadItem
        # {type: plan, id: "<turn>-plan", text}`, streamed by `item/plan/delta`).
        # It travels as an assistant message marked `plan`: a client that does
        # not know the mark shows the plan as a reply, which it is.
        return {"kind": "message", **base, "role": "assistant", "text": item.get("text", "")
                if isinstance(item.get("text"), str) else "", "plan": True}
    if kind == "userMessage":
        content = [x for x in item.get("content", []) if isinstance(x, dict)]
        text = "\n".join(x.get("text", "") for x in content if x.get("type") == "text")
        row = {"kind": "message", **base, "role": "user", "text": text}
        # ATTACH-1: how many pictures rode with the words - a count, never the
        # path or the bytes.
        images = sum(1 for x in content if x.get("type") in ("localImage", "image"))
        if images:
            row["images"] = images
        return row
    if kind == "commandExecution":
        row = {"kind": "tool", **base, "name": item.get("command", "Command"),
               "status": _tool_status(item.get("status")), "detail": item.get("aggregatedOutput") or ""}
        # AGENT-COLLIE-1: a step in a few words. Optional fields on an
        # existing row kind; a client that does not know them ignores them.
        command = inner_command(item.get("command"))
        if command is not None and command != row["name"]:
            row["command"] = command
        actions = project_actions(item.get("commandActions"))
        if actions:
            row["actions"] = actions
        for field, key in (("exitCode", "exit_code"), ("durationMs", "duration_ms")):
            if type(item.get(field)) is int:
                row[key] = item[field]
        return row
    if kind == "fileChange":
        row = {"kind": "tool", **base, "name": "File changes", "status": _tool_status(item.get("status")),
               "detail": json.dumps(item.get("changes", []), ensure_ascii=False)}
        # LINKS-1: the files it touched, as paths a client can offer to open on
        # the computer (`POST /v1/host/open`). An optional field on an existing
        # row kind: a client that does not know it ignores it.
        paths = _paths(change.get("path") for change in item.get("changes") or [] if isinstance(change, dict))
        if paths:
            row["paths"] = paths
        # AGENT-COLLIE-1: the change itself, per file, bounded.
        changes, omitted = project_changes(item.get("changes"))
        if changes:
            row["changes"] = changes
        if omitted:
            row["changes_omitted"] = omitted
        return row
    if kind == "imageView":
        # LINKS-1 (ATTACH-2 §1.3): codex looked at a picture on the computer.
        # It used to be dropped; now it is one finished tool row whose path can
        # be opened there. The picture itself never travels.
        paths = _paths([item.get("path")])
        if not paths:
            return None
        return {"kind": "tool", **base, "name": "Viewed image", "status": "succeeded",
                "detail": paths[0], "paths": paths}
    if kind == "mcpToolCall":
        return {"kind": "tool", **base, "name": item.get("tool", "Tool"),
                "status": _tool_status(item.get("status")),
                "detail": json.dumps(item.get("result") or item.get("error") or {}, ensure_ascii=False)}
    return None


def blank_message(row):
    """AGENT-NEW-1 (ATTACH-2): codex brackets tool calls with commentary
    `agentMessage` items whose whole text is a space. They carry nothing and
    became empty "Agent" bubbles, so they never leave core. A message that is
    still empty because it has only just started streaming is held back too,
    and goes out with its first visible delta."""
    return (row is not None and row.get("kind") == "message" and row.get("role") == "assistant"
            and not (row.get("text") or "").strip())


def clip_text(row, limit=MESSAGE_TEXT_LIMIT):
    """AGENT-QUEUE-1: a message's first `limit` characters, the rest counted
    in `text_truncated` - a reply is read from its beginning. Returns the row."""
    text = row.get("text") or ""
    if len(text) > limit:
        row["text"] = text[:limit]
        row["text_truncated"] = int(row.get("text_truncated") or 0) + len(text) - limit
    return row


def budget_rows(rows, budget=SNAPSHOT_DETAIL_BUDGET, older=SNAPSHOT_OLD_DETAIL):
    """Copies of `rows` whose tool output, newest row first, adds up to at
    most `budget` characters; an older row past the budget keeps its newest
    `older` characters (AGENT-FIX-2: a snapshot never nears a phone's frame
    limit, however much the conversation has printed)."""
    rows = copy.deepcopy(list(rows))
    diffs = SNAPSHOT_DIFF_BUDGET
    for row in reversed(rows):
        if row["kind"] == "message":
            clip_text(row)
        if row["kind"] != "tool":
            continue
        length = len(row.get("detail") or "")
        keep = length if length <= budget else max(budget, min(length, older))
        clip_detail(row, keep)
        budget = max(0, budget - keep)
        # AGENT-COLLIE-1: the same for the change cards' diffs. An older row
        # past the budget keeps its files and counts, not its lines.
        for change in row.get("changes") or ():
            size = len(change.get("diff") or "")
            if size > diffs:
                lines = change["diff"].count("\n") + 1 if change["diff"] else 0
                change["diff"] = ""
                change["diff_omitted"] = int(change.get("diff_omitted") or 0) + lines
            diffs = max(0, diffs - size)
    return rows


# INTEGRATE-2 finding 2: a tool whose `item/completed` never came is closed
# with its turn, so the transcript never keeps a "running" row for a turn that
# is over. A turn the person stopped closes its tools as `interrupted`; any
# other end (completed, failed) as `failed` - codex did not report success.
def closed_tool_status(turn_status):
    return "interrupted" if turn_status == "interrupted" else "failed"


def merge_rows(provider, previous, active_turn):
    """AGENT-FIX-2: the provider's history joined with what core already showed.

    codex's own record (`thread/resume`, `thread/read`) holds finished items
    only: a command still running and a reply still streaming are not in it,
    and its order inside a turn is the order items *finished*, not the order
    they appeared. Replacing core's rows with it took the running tool and the
    half-written reply off every phone, and moved rows about. So: turns come
    in the provider's order; inside a turn the rows a phone has already seen
    keep their places, and a row of the turn still running that the provider
    does not have yet stays. A finished turn is the provider's to say.
    Both arguments are id -> row dicts in display order; returns one."""
    previous = previous or {}
    if not previous:
        return dict(provider)
    seen_at = {ident: index for index, ident in enumerate(previous)}
    turns, blocks = [], {}
    for row in provider.values():
        if row["turnID"] not in blocks:
            turns.append(row["turnID"])
            blocks[row["turnID"]] = []
        blocks[row["turnID"]].append(row)
    if active_turn is not None and active_turn not in blocks and any(
            row["turnID"] == active_turn for row in previous.values()):
        turns.append(active_turn)
        blocks[active_turn] = []
    merged = {}
    for turn in turns:
        rows = {row["id"]: row for row in blocks[turn]}
        if turn == active_turn:
            for ident, row in previous.items():
                if row["turnID"] == turn and ident not in rows:
                    rows[ident] = copy.deepcopy(row)
        known = sorted((ident for ident in rows if ident in seen_at), key=seen_at.get)
        for ident in known + [ident for ident in rows if ident not in seen_at]:
            merged[ident] = rows[ident]
    return merged


def project_queued(item):
    """AGENT-QUEUE-1: one of codex's queued messages (`QueuedSubmission {id,
    clientUserMessageId, input}`) as a phone shows it: `{id, request_id,
    text, images?, text_clipped?}`. None for anything else."""
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
        return None
    content = [part for part in item.get("input") or [] if isinstance(part, dict)]
    text = "\n".join(part.get("text", "") for part in content
                     if part.get("type") == "text" and isinstance(part.get("text"), str))
    row = {"id": item["id"][:128], "request_id": _text(item.get("clientUserMessageId"), 128) or "",
           "text": text[:QUEUE_TEXT]}
    images = sum(1 for part in content if part.get("type") in ("localImage", "image"))
    if images:
        row["images"] = images
    if len(text) > QUEUE_TEXT:
        row["text_clipped"] = True
    return row


def project_checklist(params):
    """AGENT-QUEUE-1: codex's `turn/plan/updated {turnId, explanation, plan:
    [{step, status}]}` - the steps the model keeps ticking (its `update_plan`
    tool) - bounded. None when it holds no step."""
    steps = []
    for entry in params.get("plan") or [] if isinstance(params, dict) else []:
        if not isinstance(entry, dict) or len(steps) >= CHECKLIST_STEPS:
            continue
        step = _text(entry.get("step"))
        if step is None:
            continue
        status = entry.get("status")
        steps.append({"step": step, "status": status if status in CHECKLIST_STATUSES else "pending"})
    if not steps:
        return None
    value = {"turnID": _text(params.get("turnId"), 128), "steps": steps}
    explanation = _text(params.get("explanation"), 1000)
    if explanation is not None:
        value["explanation"] = explanation
    return value


async def resume_paged(rpc, thread_id, cwd):
    """AGENT-QUEUE-1: `thread/resume` without the whole history - codex 0.160's
    `excludeTurns` with `initialTurnsPage` (its newest HISTORY_PAGE_TURNS
    turns, newest first; experimental). Returns `(response, barrier, older)`
    with `response.thread.turns` in display order and `older` the cursor of
    the turns before them (None: there are none, or codex sent everything).
    A codex that refuses the paged form is resumed the whole way, as before."""
    page = {"limit": HISTORY_PAGE_TURNS, "sortDirection": "desc", "itemsView": "full"}
    for with_cwd in ((True, False) if cwd is not None else (False,)):
        params = {"threadId": thread_id, "excludeTurns": True, "initialTurnsPage": page}
        if with_cwd:
            params["cwd"] = cwd
        try:
            response, barrier = await rpc.request_fenced("thread/resume", params)
        except AgentChatError as error:
            if error.code != "provider_request_rejected":
                raise
            continue
        thread = response.get("thread") if isinstance(response.get("thread"), dict) else {}
        first = response.get("initialTurnsPage")
        if isinstance(first, dict) and isinstance(first.get("data"), list):
            thread["turns"] = [turn for turn in reversed(first["data"]) if isinstance(turn, dict)]
            older = first.get("nextCursor") if isinstance(first.get("nextCursor"), str) else None
            return response, barrier, older
        if thread.get("turns") or not isinstance(response.get("turnsBackwardsCursor"), str):
            # A codex that sent everything anyway, or a thread with no turns.
            return response, barrier, None
        # `excludeTurns` taken (the thread pages: it has a backwards cursor),
        # the page not: read the page itself.
        try:
            listed = await rpc.request("thread/turns/list", {"threadId": thread_id, **page})
        except AgentChatError as error:
            if error.code != "provider_request_rejected":
                raise
            break
        thread["turns"] = [turn for turn in reversed(listed.get("data") or []) if isinstance(turn, dict)]
        older = listed.get("nextCursor") if isinstance(listed.get("nextCursor"), str) else None
        return response, barrier, older
    response, barrier = await resume_in_workspace(rpc, thread_id, cwd, fenced=True)
    return response, barrier, None


async def resume_in_workspace(rpc, thread_id, cwd, *, fenced=False):
    """thread/resume that moves the thread into the manager's workspace.

    ThreadResumeParams.cwd is part of the 0.154 protocol. Should a provider
    refuse the override (for example for a thread another client is running),
    the plain resume is still correct: turn/start carries the cwd as well.
    """
    call = rpc.request_fenced if fenced else rpc.request
    if cwd is None:
        return await call("thread/resume", {"threadId": thread_id})
    try:
        return await call("thread/resume", {"threadId": thread_id, "cwd": cwd})
    except AgentChatError as error:
        if error.code != "provider_request_rejected":
            raise
        return await call("thread/resume", {"threadId": thread_id})


class CodexDefaultAgentChat:
    def __init__(self, binding: DefaultAgentBinding, rpc: CodexJSONRPC, *,
                 delivery_journal=None, persist_delivery=lambda journal: None, initial_sequence=0,
                 auth_provider=None, previous_rows=None, answered=None):
        self.binding, self.rpc = binding, rpc
        # AGENT-FIX-2: rows a previous connection to this same thread already
        # showed; attach() joins them with the provider's record.
        self._previous_rows = dict(previous_rows or {})
        # Output deltas waiting to go out as one `tool.output` per item.
        self._pending_output = {}
        self._output_flush = None
        # Server->client requests waiting for a human. The provider's own
        # request id is the handle; nothing here is invented by a client.
        self.pending_approvals = {}
        self.status = {"type": "unknown", "activeFlags": []}
        self.usage = {"tokens": None, "rate_limits": None}
        self.settings = {"model": None, "effort": None}
        # AGENT-PLAN-1: the thread's collaboration mode as codex reports it
        # (`default` | `plan`), or None while it has not said.
        self.mode = None
        # AGENT-PLAN-1 `/review`: codex 0.160 runs a review as the turn
        # `review/start` answers with, but announces it with `turn/started`
        # under another id. The review's turn and the ids that stand for it.
        self._review_pending = False
        self._review_turn = None
        self._turn_alias = {}
        self.auth_provider = auth_provider or read_codex_chatgpt_tokens
        self._answers = set()
        # AGENT-QUEUE-1 (the looping question card): what this core answered,
        # by request id - (request_fingerprint, result). The service keeps one
        # dict across re-attaches. codex re-sends a request it is still waiting
        # on whenever the thread is resumed; if core already answered exactly
        # that request, the answer never reached codex (a connection that went
        # down with it), so core sends it again instead of asking the person
        # again. Before this, each re-attach put the same questions back on the
        # phone, from question 1.
        self._answered = answered if answered is not None else {}
        # AGENT-QUEUE-1: codex's own queue for this thread, as last listed
        # (`[]` empty), or None when this codex has none (`thread/queue/*`
        # refused): then a phone steers while a turn runs, as before.
        self.queue = None
        self._queue_raw = {}
        self._queue_refresh = None
        self._queued_requests = {}
        # AGENT-QUEUE-1: words core steered into a turn (Send now, a decline's
        # reason) that codex has not yet taken as a user item: client id ->
        # (turn, input). codex drops steered input a turn never reached (a
        # Stop while it waits on an approval - real 0.160); those go back on
        # the queue, first in line, rather than vanish.
        self._steered = {}
        # AGENT-QUEUE-1: the newest plan checklist codex sent (`turn/plan/updated`).
        self.checklist = None
        # AGENT-QUEUE-1: where codex's record of turns older than core's rows
        # continues (`thread/turns/list` cursor), or None when core has them all.
        self._older = None
        self._history_lock = asyncio.Lock()
        self.rows = {}
        self.active_turn = None
        # MOTION-1: what the turn is doing right now, for the animated face
        # (`agent.chat.activity` in state). Tools still in progress, by item id.
        self.activity = {"phase": "idle", "turn_id": None}
        self._running_tools = set()
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
            self.binding.ensure_workspace()
            try:
                # AGENT-QUEUE-1: the newest page of the thread, not all of it.
                response, barrier, self._older = await resume_paged(self.rpc, self.binding.thread_id,
                                                                    self.binding.cwd)
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
            # ThreadResumeResponse names the model and effort the thread runs
            # with; the phone's model control shows them before a turn
            # confirms anything (thread/settings/updated).
            self.seed_settings(response.get("model"), response.get("reasoningEffort"))
            # AGENT-PLAN-1: the mode is the thread's, kept by codex across
            # restarts (its rollout); resume says which one it is in.
            self.seed_mode(response.get("collaborationMode"))
            self.attached = True
            self._replay_after(barrier)
        finally:
            self._loading = False
        # AGENT-QUEUE-1: what is waiting in codex's queue for this thread.
        await self._load_queue()
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
        """The conversation as core holds it.

        AGENT-FIX-2 (Leo: "agent 在工具调用的时候，聊天记录会消失"). This used to
        read `thread/read` and replace core's rows with the answer, on every
        App open, every event-stream connect and every ensure. codex's record
        holds finished items only, so each of those took the running command
        and the reply being written off the screen (their later deltas then
        had no row to land in), and reordered the turn by finish time. While
        attached, core has applied every notification of this thread since
        attach() read the provider's record, so its own rows are the
        conversation; nothing is re-read here."""
        if not self.attached:
            raise AgentChatError("agent_not_attached")
        return self.snapshot_value()

    def seed_settings(self, model, effort):
        """What the thread already runs with, until the provider says otherwise."""
        for key, value in (("model", model), ("effort", effort)):
            if self.settings.get(key) is None and isinstance(value, str) and value:
                self.settings[key] = value[:64]

    def seed_mode(self, value):
        mode = value.get("mode") if isinstance(value, dict) else None
        if mode in MODES:
            self.mode = mode

    def seed_checklist(self, value):
        """AGENT-QUEUE-1: the checklist core kept for this thread across its
        own restart - codex's record holds no plan updates to read back."""
        if self.checklist is None and isinstance(value, dict):
            self.checklist = project_checklist({"turnId": value.get("turnID"), "plan": value.get("steps"),
                                                "explanation": value.get("explanation")})

    # --- AGENT-QUEUE-1: codex's queue -----------------------------------------
    def queue_value(self):
        """The queue as phones show it, or None when this codex has none."""
        return copy.deepcopy(self.queue)

    async def _load_queue(self):
        """`thread/queue/list`, the first page (codex keeps at most 100).
        A codex that refuses it (older, or no experimental API) has no queue."""
        try:
            listed = await self.rpc.request("thread/queue/list", {"threadId": self.binding.thread_id,
                                                                  "limit": QUEUE_SHOWN})
        except AgentChatError as error:
            if error.code in ("provider_disconnected",):
                raise
            changed = self.queue is not None
            self.queue, self._queue_raw = None, {}
            return changed
        data = listed.get("data") if isinstance(listed, dict) else None
        if not isinstance(data, list):
            changed = self.queue is not None
            self.queue, self._queue_raw = None, {}
            return changed
        rows, raw = [], {}
        for item in data[:QUEUE_SHOWN]:
            row = project_queued(item)
            if row is not None:
                rows.append(row)
                raw[row["id"]] = item
        if listed.get("nextCursor") and rows:
            rows[-1]["more"] = True  # past the first QUEUE_SHOWN, still waiting
        changed = rows != self.queue
        self.queue, self._queue_raw = rows, raw
        return changed

    async def refresh_queue(self):
        """List again and tell the phones if it changed."""
        if await self._load_queue():
            self._emit({"type": "agent.queue.changed", "queue": self.queue_value()})

    def _schedule_queue_refresh(self):
        """`thread/queue/changed` names only the thread: read it again, once
        for a burst of changes."""
        if self._queue_refresh is not None and not self._queue_refresh.done():
            self._queue_dirty = True
            return
        self._queue_dirty = False
        async def run():
            while True:
                with suppress(AgentChatError):
                    await self.refresh_queue()
                if not getattr(self, "_queue_dirty", False):
                    return
                self._queue_dirty = False
        try:
            self._queue_refresh = asyncio.ensure_future(run())
        except RuntimeError:
            self._queue_refresh = None

    def _queue_item(self, queue_id):
        item = self._queue_raw.get(queue_id) if isinstance(queue_id, str) else None
        if item is None:
            raise AgentChatError("agent_queue_item_gone")
        return item

    async def enqueue(self, text: str, request_id: str):
        """A message to start when the running turn ends (`thread/queue/add`).
        codex keeps it (on disk, across restarts) and starts it itself; an
        approval is never in it. The same request id asks once."""
        if not self.attached or not isinstance(text, str) or not text.strip():
            raise AgentChatError("agent_not_ready")
        if self.queue is None:
            raise AgentChatError("agent_queue_unavailable")
        fingerprint = hashlib.sha256(text.encode()).hexdigest()
        previous = self._queued_requests.get(request_id)
        if previous is not None:
            if previous != fingerprint:
                raise AgentChatError("request_conflict")
            return {"accepted": True, "delivery": "queued", "queue": self.queue_value()}
        result = await self.rpc.request("thread/queue/add", {
            "threadId": self.binding.thread_id, "clientUserMessageId": request_id,
            "input": [{"type": "text", "text": text}]})
        self._queued_requests[request_id] = fingerprint
        while len(self._queued_requests) > 64:
            self._queued_requests.pop(next(iter(self._queued_requests)))
        queued = project_queued((result or {}).get("queuedSubmission"))
        await self.refresh_queue()
        reply = {"accepted": True, "delivery": "queued", "queue": self.queue_value()}
        if queued is not None:
            reply["queued"] = queued
        return reply

    async def send_queued_now(self, queue_id):
        """"Send now": into the running turn (`turn/steer`, as the composer's
        old Steer did), or - between turns - started as its own
        (`thread/queue/start`). Taken off the queue first, so codex cannot also
        start it; put back at the head if the turn ended in between."""
        async with self._send_lock:
            if not self.attached:
                raise AgentChatError("agent_not_ready")
            await self._load_queue()
            item = self._queue_item(queue_id)
            turn = self.active_turn
            if turn is None:
                try:
                    result = await self.rpc.request("thread/queue/start", {"threadId": self.binding.thread_id,
                                                                           "queuedSubmissionId": queue_id})
                except AgentChatError as error:
                    if error.code == "provider_request_rejected":
                        raise AgentChatError("agent_queue_item_gone") from None
                    raise
                started = (result.get("turn") or {}).get("id") if isinstance(result, dict) else None
                if isinstance(started, str) and self.active_turn is None:
                    self.active_turn = started
                await self.refresh_queue()
                return {"accepted": True, "delivery": "started", "turnID": started, "queue": self.queue_value()}
            removed = await self.rpc.request("thread/queue/delete", {"threadId": self.binding.thread_id,
                                                                     "queuedSubmissionId": queue_id})
            if not (removed or {}).get("deleted"):
                raise AgentChatError("agent_queue_item_gone")  # codex started it already
            try:
                await self.rpc.request("turn/steer", {"threadId": self.binding.thread_id, "expectedTurnId": turn,
                                                      "clientUserMessageId": item.get("clientUserMessageId"),
                                                      "input": item.get("input") or []})
                self._remember_steer(item.get("clientUserMessageId"), turn, item.get("input") or [])
                delivery = "steered"
            except AgentChatError:
                # The turn ended meanwhile: back on the queue, first in line;
                # codex starts it (or it waits, after a Stop, for Send now).
                again = await self.rpc.request("thread/queue/add", {
                    "threadId": self.binding.thread_id, "clientUserMessageId": item.get("clientUserMessageId"),
                    "input": item.get("input") or []})
                back = ((again or {}).get("queuedSubmission") or {}).get("id")
                order = [back] + [ident for ident in self._queue_raw if ident not in (queue_id, back)]
                if isinstance(back, str) and len(order) > 1:
                    with suppress(AgentChatError):
                        await self.rpc.request("thread/queue/reorder", {"threadId": self.binding.thread_id,
                                                                        "queuedSubmissionIds": order})
                delivery = "queued"
            await self.refresh_queue()
            return {"accepted": True, "delivery": delivery, "turnID": turn if delivery == "steered" else None,
                    "queue": self.queue_value()}

    def _remember_steer(self, client_id, turn, words):
        if self.queue is not None and isinstance(client_id, str) and client_id:
            self._steered[client_id] = (turn, copy.deepcopy(words))
            while len(self._steered) > 16:
                self._steered.pop(next(iter(self._steered)))

    async def _requeue_untaken(self, items):
        """Steered words their turn ended without: back on codex's queue,
        ahead of what was already waiting (codex keeps it after a Stop until
        Send now; after any other end it starts it)."""
        with suppress(AgentChatError):
            await self._load_queue()
            waiting = list(self._queue_raw)
            back = []
            for client_id, words in items:
                added = await self.rpc.request("thread/queue/add", {"threadId": self.binding.thread_id,
                                                                    "clientUserMessageId": client_id, "input": words})
                ident = ((added or {}).get("queuedSubmission") or {}).get("id")
                if isinstance(ident, str):
                    back.append(ident)
            if back and waiting:
                await self.rpc.request("thread/queue/reorder", {"threadId": self.binding.thread_id,
                                                                "queuedSubmissionIds": back + waiting})
            await self.refresh_queue()

    async def edit_queued(self, queue_id, text: str):
        """New words for a waiting message, in its place (`thread/queue/update`)."""
        if not self.attached or not isinstance(text, str) or not text.strip():
            raise AgentChatError("agent_not_ready")
        await self._load_queue()
        self._queue_item(queue_id)
        try:
            await self.rpc.request("thread/queue/update", {"threadId": self.binding.thread_id,
                                                           "queuedSubmissionId": queue_id,
                                                           "input": [{"type": "text", "text": text}]})
        except AgentChatError as error:
            if error.code == "provider_request_rejected":
                raise AgentChatError("agent_queue_item_gone") from None  # it started meanwhile
            raise
        await self.refresh_queue()
        return {"edited": True, "queue": self.queue_value()}

    async def delete_queued(self, queue_id):
        """Off the queue (`thread/queue/delete`). Gone already is fine too."""
        if not self.attached:
            raise AgentChatError("agent_not_ready")
        if self.queue is None:
            raise AgentChatError("agent_queue_unavailable")
        removed = await self.rpc.request("thread/queue/delete", {"threadId": self.binding.thread_id,
                                                                 "queuedSubmissionId": queue_id})
        await self.refresh_queue()
        return {"deleted": bool((removed or {}).get("deleted")), "queue": self.queue_value()}

    # --- AGENT-QUEUE-1: history a page at a time -------------------------------
    def _shown_rows(self):
        return [row for row in self.rows.values() if not blank_message(row)]

    async def _read_older(self):
        """One more page of codex's record, before core's oldest row. False
        when there is none (or codex will not say)."""
        if self._older is None:
            return False
        try:
            listed = await self.rpc.request("thread/turns/list", {
                "threadId": self.binding.thread_id, "cursor": self._older, "limit": HISTORY_PAGE_TURNS,
                "sortDirection": "desc", "itemsView": "full"})
        except AgentChatError as error:
            if error.code == "provider_disconnected":
                raise
            self._older = None
            return False
        cursor = listed.get("nextCursor") if isinstance(listed, dict) else None
        self._older = cursor if isinstance(cursor, str) and cursor else None
        older = {}
        for turn in reversed([turn for turn in (listed.get("data") or []) if isinstance(turn, dict)]):
            if not isinstance(turn.get("id"), str) or any(row["turnID"] == turn["id"] for row in self.rows.values()):
                continue
            for row in self._turn_rows(turn):
                if row["id"] not in self.rows:
                    older[row["id"]] = row
        if older:
            self.rows = {**older, **self.rows}
        return True

    async def history(self, before=None, limit=100):
        """AGENT-QUEUE-1: `GET /v1/agent/default/chat/history` - up to `limit`
        rows before the row `before` (core's own rows first, then codex's
        record a page at a time), oldest first, budgeted like a snapshot, and
        whether there are `earlier` ones still."""
        if not self.attached:
            raise AgentChatError("agent_not_attached")
        limit = max(1, min(HISTORY_LIMIT, int(limit)))
        async with self._history_lock:
            while True:
                shown = self._shown_rows()
                ids = [row["id"] for row in shown]
                if before is not None and before not in ids:
                    raise AgentChatError("agent_history_row_unknown")
                end = ids.index(before) if before is not None else len(ids)
                if end >= limit or self._older is None:
                    break
                if not await self._read_older():
                    break
            shown = self._shown_rows()
            ids = [row["id"] for row in shown]
            end = ids.index(before) if before is not None else len(ids)
            start = max(0, end - limit)
            return {"rows": budget_rows(shown[start:end]),
                    "earlier": start > 0 or self._older is not None}

    def _restore(self, thread):
        if thread.get("id") != self.binding.thread_id:
            raise AgentChatError("provider_identity_changed")
        rows = {}
        active = None
        for turn in thread.get("turns", []):
            if turn.get("status") == "inProgress":
                active = turn["id"]
            for row in self._turn_rows(turn):
                rows[row["id"]] = row
        self._persist_delivery(copy.deepcopy(self._requests))
        rows = merge_rows(rows, {**self._previous_rows, **self.rows}, active)
        self._previous_rows = {}
        self.rows, self.active_turn = rows, active
        self._running_tools = {row["id"] for row in rows.values()
                               if row["kind"] == "tool" and row["turnID"] == active and row["status"] == "running"}
        if active:
            self.activity = {"phase": "tool" if self._running_tools else "thinking", "turn_id": active}
        elif self.activity["phase"] not in ("done", "error"):
            self.activity = {"phase": "idle", "turn_id": None}

    def _turn_rows(self, turn):
        """One turn of codex's record (`thread/resume`, `thread/turns/list`)
        as rows, in the order they are drawn."""
        status = turn.get("status")
        # AGENT-PLAN-1: codex records a turn's items in the order they
        # finished, which puts the plan ahead of the words that introduce
        # it; live, it streams after them. The plan goes last, as it came.
        items = [item for item in turn.get("items", []) if isinstance(item, dict)]
        items = [item for item in items if item.get("type") != "plan"] + \
            [item for item in items if item.get("type") == "plan"]
        rows = []
        for item in items:
            client_id = item.get("clientId")
            if item.get("type") == "userMessage" and client_id in self._requests:
                self._requests[client_id]["accepted"] = True
            row = project_item(item, turn["id"])
            if row:
                if row["kind"] == "tool" and row["status"] == "running" and status in (
                        "completed", "failed", "interrupted"):
                    # History of a turn that is over: a tool it never
                    # closed is not still running.
                    row["status"] = closed_tool_status(status)
                rows.append(row)
        return rows

    def _replay_after(self, barrier):
        queued, self._buffer = self._buffer, []
        for value, ordinal in queued:
            if ordinal > barrier:
                self._apply(value)

    def snapshot_value(self):
        # Output not yet sent as an increment is in the rows already; send it
        # first, or a client would add it twice on top of this snapshot.
        self._flush_output()
        shown = self._shown_rows()
        # AGENT-QUEUE-1: the newest SNAPSHOT_ROWS rows; `earlier` says there
        # is more before them (`history`). The new fields are additions an
        # older client ignores.
        return {"identity": self.binding.identity(),
                "rows": budget_rows(shown[-SNAPSHOT_ROWS:]),
                "activeTurn": self.active_turn, "sequence": self.sequence,
                "acceptedRequestIDs": [k for k, v in self._requests.items() if v.get("accepted")],
                "pendingApprovals": self.approvals_value(), "status": self.status_value(),
                "usage": self.usage_value(),
                "earlier": len(shown) > SNAPSHOT_ROWS or self._older is not None,
                "queue": self.queue_value(), "checklist": copy.deepcopy(self.checklist)}

    def approvals_value(self):
        # NOTIFY-1: `fingerprint` - what a decision sent from an alert must
        # still match (`approval_fingerprint`).
        return [{**{key: copy.deepcopy(row[key]) for key in ("request_id", "kind", "summary", "details", "decisions")},
                 "fingerprint": approval_fingerprint(row)}
                for row in self.pending_approvals.values()]

    def approval_head(self):
        """NOTIFY-1: the oldest pending approval, ids only, for the hub state.

        `{request_id, kind, fingerprint, decidable}` - what a phone's alert
        needs to offer Allow / Deny on exactly this request, and nothing of
        what it asks (the summary stays on the chat's own stream). `decidable`
        is false for a question, which needs a typed answer or a choice.
        """
        for row in self.pending_approvals.values():
            return {"request_id": row["request_id"], "kind": row["kind"],
                    "fingerprint": approval_fingerprint(row), "decidable": bool(row["decisions"])}
        return None

    def activity_value(self):
        """MOTION-1. `idle | thinking | working | tool | done | error | stopped`
        and the turn it belongs to (`stopped` = interrupted by the user, `error`
        = failed). `done` / `error` / `stopped` stay until the next turn starts;
        whether somebody has seen them is the client's to remember. A chat
        whose provider connection is gone is `offline`."""
        if not self.attached:
            return {"phase": "offline", "turn_id": None}
        return dict(self.activity)

    def _track_activity(self, method, p):
        if method == "turn/started":
            self._running_tools = set()
            self.activity = {"phase": "thinking", "turn_id": self.active_turn}
            return
        if method == "turn/completed":
            turn = p.get("turn") or {}
            self._running_tools = set()
            # INTEGRATE-2 (Leo): a turn the user interrupted is `stopped`, a calm
            # state of its own; only a real failure is `error`.
            status = turn.get("status")
            phase = "done" if status == "completed" else "stopped" if status == "interrupted" else "error"
            self.activity = {"phase": phase, "turn_id": turn.get("id")}
            return
        if self.active_turn is None or p.get("turnId") not in (None, self.active_turn):
            return
        item = p.get("item") if isinstance(p.get("item"), dict) else {}
        kind, ident = item.get("type"), item.get("id")
        if method == "item/started":
            if kind in ("commandExecution", "fileChange", "mcpToolCall") and isinstance(ident, str):
                self._running_tools.add(ident)
            elif kind == "agentMessage" and not self._running_tools:
                self.activity = {"phase": "working", "turn_id": self.active_turn}
            elif kind == "reasoning" and not self._running_tools:
                self.activity = {"phase": "thinking", "turn_id": self.active_turn}
        elif method == "item/completed" and isinstance(ident, str) and ident in self._running_tools:
            self._running_tools.discard(ident)
            if not self._running_tools:
                self.activity = {"phase": "thinking", "turn_id": self.active_turn}
        elif method == "item/agentMessage/delta" and not self._running_tools \
                and self.activity["phase"] != "working":
            self.activity = {"phase": "working", "turn_id": self.active_turn}
        if self._running_tools:
            self.activity = {"phase": "tool", "turn_id": self.active_turn}

    def status_value(self):
        flags = [flag for flag in self.status.get("activeFlags") or [] if isinstance(flag, str)]
        return {"type": self.status.get("type"), "activeFlags": flags,
                "waiting": "approval" if "waitingOnApproval" in flags
                           else "user_input" if "waitingOnUserInput" in flags else None}

    def usage_value(self):
        return {"tokens": copy.deepcopy(self.usage.get("tokens")),
                "rate_limits": copy.deepcopy(self.usage.get("rate_limits")),
                "model": self.settings.get("model"), "effort": self.settings.get("effort"),
                # AGENT-PLAN-1: the collaboration mode, beside the model and
                # effort it is set with. An older client ignores the key.
                "mode": self.mode}

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
        earlier = self._answered.get(str(request_id))
        if earlier is not None and earlier[0] == request_fingerprint(params):
            # The same request, already answered here: deliver that answer again.
            self._background(self._answer_again(request_id, kind, earlier[1]))
            return
        summary, details = approval_view(kind, params, self.rows.get(params.get("itemId")))
        key = str(request_id)
        self.pending_approvals[key] = {"request_id": key, "kind": kind, "summary": summary,
                                       "details": details, "decisions": [] if kind == "userInput" else list(DECISIONS),
                                       "provider_id": request_id, "params": params}
        self._emit({"type": "agent.approval.requested", "request_id": key, "kind": kind,
                    "summary": summary, "details": details,
                    "decisions": [] if kind == "userInput" else list(DECISIONS),
                    "fingerprint": approval_fingerprint(self.pending_approvals[key])})

    async def _answer_again(self, request_id, kind, result):
        with suppress(Exception):
            await self.rpc.respond(request_id, result)
        self._emit({"type": "agent.approval.resolved", "request_id": str(request_id), "kind": kind,
                    "decision": "input" if kind == "userInput" else result.get("decision"), "source": "client"})

    def _remember_answer(self, row, result):
        self._answered[row["request_id"]] = (request_fingerprint(row["params"]), copy.deepcopy(result))
        while len(self._answered) > 32:
            self._answered.pop(next(iter(self._answered)))

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

    async def resolve_approval(self, request_id, *, decision=None, answers=None, fingerprint=None, reason=None):
        row = self.pending_approvals.get(str(request_id))
        if row is None:
            raise AgentChatError("agent_approval_unknown")
        # NOTIFY-1: a decision taken on an alert names what the alert was
        # about. If this request no longer reads the same, nothing is sent.
        if fingerprint is not None and fingerprint != approval_fingerprint(row):
            raise AgentChatError("agent_approval_changed")
        if reason is not None:
            # AGENT-QUEUE-1: "No, and here's why". codex's decisions carry no
            # words (0.160 `CommandExecutionApprovalDecision` and the rest),
            # so the reason goes in as the person's own message: steered into
            # the turn *before* the decline is answered - the model's next
            # request then holds the refusal and the reason together (real
            # 0.160 frames) - or, if the turn will not take it, queued for
            # when it ends. Only a plain decline: a cancel stops the turn.
            if (row["kind"] == "userInput" or decision != "decline" or answers is not None
                    or not isinstance(reason, str) or "\0" in reason or len(reason) > REASON_LIMIT):
                raise AgentChatError("agent_approval_decision_invalid")
            reason = reason.strip() or None
        if reason is not None:
            return await self._decline_with_reason(row, request_id, reason)
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
        self._remember_answer(row, result)
        self.pending_approvals.pop(str(request_id), None)
        self._emit({"type": "agent.approval.resolved", "request_id": str(request_id),
                    "kind": row["kind"], "decision": applied, "source": "client"})
        return {"request_id": str(request_id), "kind": row["kind"], "decision": applied, "resolved": True}

    async def _decline_with_reason(self, row, request_id, reason):
        """AGENT-QUEUE-1. `delivery` says where the words went: `steered`
        (into this turn), `queued` (the next message, codex's queue) or
        `unsent` (neither would take them; the phone keeps them)."""
        words = [{"type": "text", "text": reason}]
        message_id = "reason-" + hashlib.sha256((str(request_id) + reason).encode()).hexdigest()[:24]
        turn = row["params"].get("turnId") if isinstance(row["params"].get("turnId"), str) else self.active_turn
        delivery = "unsent"
        if turn is not None and turn == self.active_turn:
            with suppress(AgentChatError):
                await self.rpc.request("turn/steer", {"threadId": self.binding.thread_id, "expectedTurnId": turn,
                                                      "clientUserMessageId": message_id, "input": words})
                self._remember_steer(message_id, turn, words)
                delivery = "steered"
        answer = {"decision": "decline"} if row["kind"] != "permissions" else {"permissions": {}, "scope": "turn"}
        await self.rpc.respond(row["provider_id"], answer)
        self._remember_answer(row, answer)
        self.pending_approvals.pop(str(request_id), None)
        self._emit({"type": "agent.approval.resolved", "request_id": str(request_id),
                    "kind": row["kind"], "decision": "decline", "source": "client"})
        if delivery == "unsent" and self.queue is not None:
            with suppress(AgentChatError):
                await self.rpc.request("thread/queue/add", {"threadId": self.binding.thread_id,
                                                            "clientUserMessageId": message_id, "input": words})
                delivery = "queued"
                await self.refresh_queue()
        return {"request_id": str(request_id), "kind": row["kind"], "decision": "decline", "resolved": True,
                "delivery": delivery}

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

    def _schedule_output_flush(self):
        if self._output_flush is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._flush_output()
            return
        self._output_flush = loop.call_later(OUTPUT_FLUSH_SECONDS, self._flush_output)

    def _flush_output(self):
        """One `tool.output` per item for the output gathered since the last
        one (AGENT-FIX-2): `delta` is the new text (its newest TOOL_DETAIL_LIMIT characters
        at most) and `detail_truncated` how much of the row's output is no
        longer kept. A client appends `delta` and keeps the same newest
        TOOL_DETAIL_LIMIT characters, so it holds exactly core's `detail`."""
        if self._output_flush is not None:
            self._output_flush.cancel()
            self._output_flush = None
        pending, self._pending_output = self._pending_output, {}
        for ident, delta in pending.items():
            row = self.rows.get(ident)
            if row is None:
                continue
            # `kind` is there for clients older than this event: they read
            # every event's `value` as a row, and a value without `kind` and
            # `turnID` failed the whole frame and ended their stream. With it
            # they decode an empty tool row, find no use for the type and move
            # on; the row's output reaches them with its completion.
            value = {"kind": "tool", "id": ident, "turnID": row["turnID"], "delta": delta[-TOOL_DETAIL_LIMIT:]}
            if row.get("detail_truncated"):
                value["detail_truncated"] = row["detail_truncated"]
            self._emit({"type": "tool.output", "value": value})

    def _review_ids(self, method, p):
        """AGENT-PLAN-1: one turn for a review, whatever codex calls it. Real
        0.160 frames (`tests/fixtures/codex-0160-agent-plan1.json`, `review`):
        the review's items and `turn/completed` carry the id `review/start`
        answered with; its `turn/started` carries another. Without this the
        turn never ends for core, and every phone ignores the review's rows."""
        if self._turn_alias and p.get("turnId") in self._turn_alias:
            p["turnId"] = self._turn_alias[p["turnId"]]
        turn = p.get("turn") if isinstance(p.get("turn"), dict) else None
        if turn is not None and turn.get("id") in self._turn_alias:
            turn["id"] = self._turn_alias[turn["id"]]
        if not self._review_pending:
            return
        if method == "turn/started" and turn is not None:
            if self._review_turn is not None and turn.get("id") != self._review_turn:
                # The review's own turn is already showing: this is it again.
                self._turn_alias[turn["id"]] = self._review_turn
                turn["id"] = self._review_turn
            elif self._review_turn is None:
                self._review_turn = turn.get("id")
        elif isinstance(p.get("turnId"), str) and self.active_turn is None and self._review_turn is None:
            # A review item before any start: its turn has begun.
            self._review_turn = p["turnId"]
            self.active_turn = p["turnId"]
            self._track_activity("turn/started", {"turn": {"id": p["turnId"]}})
            self._emit({"type": "turnStarted", "turnID": p["turnId"]})
        if method == "turn/completed" and turn is not None and turn.get("id") == self._review_turn:
            self._review_pending, self._review_turn = False, None

    def _apply(self, value):
        method, p = value.get("method"), value.get("params", {})
        if method == "item/started" and isinstance(p.get("item"), dict) \
                and p["item"].get("type") == "enteredReviewMode" and not self._review_pending \
                and self.active_turn is None:
            # A review another client of this thread started (codex's TUI).
            self._review_pending, self._review_turn = True, None
        if self._review_pending or self._turn_alias:
            self._review_ids(method, p)
            if method == "turn/started" and self.active_turn is not None \
                    and (p.get("turn") or {}).get("id") == self.active_turn:
                return  # the review's turn, announced again
        if self._pending_output and method != "item/commandExecution/outputDelta":
            # Output first: an item's completion or its turn's end must never
            # arrive ahead of the words it printed.
            self._flush_output()
        if method == "thread/queue/changed":
            # AGENT-QUEUE-1: something was queued, edited, removed or started
            # (by any client of this thread); the notification names no more.
            if self.queue is not None:
                self._schedule_queue_refresh()
            return
        if method == "turn/plan/updated":
            # AGENT-QUEUE-1: the model ticked its checklist (`update_plan`).
            checklist = project_checklist(p)
            if checklist is not None and checklist != self.checklist:
                self.checklist = checklist
                self._emit({"type": "agent.checklist.updated", "checklist": copy.deepcopy(checklist)})
            return
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
            # 0.160 sends `{threadId, threadSettings: ThreadSettings}` (and only
            # to a connection with experimentalApi); `settings` and a flat body
            # are what earlier builds were read as.
            settings = next((p[key] for key in ("threadSettings", "settings") if isinstance(p.get(key), dict)), p)
            for key, field in (("model", "model"), ("effort", "effort")):
                value_read = settings.get(field)
                if isinstance(value_read, str) and value_read:
                    self.settings[key] = value_read[:64]
            self.seed_mode(settings.get("collaborationMode"))
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
            self._track_activity(method, p)
            self._emit({"type": "turnStarted", "turnID": self.active_turn})
        elif method in {"item/started", "item/completed"}:
            if self._steered and isinstance(p.get("item"), dict) and p["item"].get("type") == "userMessage":
                self._steered.pop(p["item"].get("clientId"), None)  # codex took the steered words
            self._track_activity(method, p)
            row = project_item(p.get("item", {}), p.get("turnId"))
            if row:
                if blank_message(row):
                    # Held back (streaming may fill it) or, once completed
                    # still blank, dropped: never an empty bubble.
                    if method == "item/completed":
                        self.rows.pop(row["id"], None)
                    else:
                        self.rows[row["id"]] = row
                    return
                self.rows[row["id"]] = row
                self._emit({"type": row["kind"], "value": row})
        elif method == "item/agentMessage/delta":
            self._track_activity(method, p)
            row = self.rows.get(p.get("itemId"))
            if row is None and isinstance(p.get("itemId"), str) and p.get("turnId") is not None \
                    and p.get("turnId") == self.active_turn:
                # AGENT-FIX-2: a reply that started before this connection
                # attached has no row yet; its words still belong on screen.
                row = {"kind": "message", "id": p["itemId"], "turnID": p["turnId"],
                       "role": "assistant", "text": ""}
                self.rows[row["id"]] = row
            if row and row["kind"] == "message" and row.get("turnID") == p.get("turnId"):
                row["text"] += p.get("delta", "") if isinstance(p.get("delta"), str) else ""
                if not blank_message(row):
                    self._emit({"type": row["kind"], "value": dict(row)})
        elif method == "item/plan/delta":
            # AGENT-PLAN-1: the plan as it is written. Its `item/completed`
            # carries the whole text and replaces what the deltas built.
            row = self.rows.get(p.get("itemId"))
            if row is None and isinstance(p.get("itemId"), str) and p.get("turnId") is not None \
                    and p.get("turnId") == self.active_turn:
                row = {"kind": "message", "id": p["itemId"], "turnID": p["turnId"],
                       "role": "assistant", "text": "", "plan": True}
                self.rows[row["id"]] = row
            if row and row.get("plan") and row.get("turnID") == p.get("turnId"):
                row["text"] += p.get("delta", "") if isinstance(p.get("delta"), str) else ""
                if not blank_message(row):
                    self._emit({"type": row["kind"], "value": dict(row)})
        elif method == "item/commandExecution/outputDelta":
            row = self.rows.get(p.get("itemId"))
            delta = p.get("delta")
            if row and row["kind"] == "tool" and row.get("turnID") == p.get("turnId") \
                    and isinstance(delta, str) and delta:
                # AGENT-FIX-2: an increment, not the whole output again.
                row["detail"] += delta
                clip_detail(row)
                self._pending_output[row["id"]] = self._pending_output.get(row["id"], "") + delta
                self._schedule_output_flush()
        elif method == "turn/completed":
            turn = p["turn"]
            if turn["id"] != self.active_turn:
                return
            self.active_turn = None
            self._track_activity(method, p)
            untaken = [(client, words) for client, (owner, words) in self._steered.items() if owner == turn["id"]]
            if untaken:
                for client, _ in untaken:
                    self._steered.pop(client, None)
                self._background(self._requeue_untaken(untaken))
            for row in self.rows.values():
                if row["kind"] == "tool" and row["turnID"] == turn["id"] and row["status"] == "running":
                    row["status"] = closed_tool_status(turn.get("status"))
                    self._emit({"type": "tool", "value": dict(row)})
            if turn.get("status") == "failed":
                self._emit({"type": "turnFailed", "turnID": turn["id"], "message": "The agent turn failed."})
            else:
                self._emit({"type": "turnFinished", "turnID": turn["id"], "interrupted": turn.get("status") == "interrupted"})

    async def send(self, text: str, request_id: str, *, model=None, effort=None, images=(), mode=None):
        async with self._send_lock:
            return await self._send(text, request_id, model=model, effort=effort, images=tuple(images), mode=mode)

    def collaboration_mode(self, kind, *, model=None, effort=None):
        """AGENT-PLAN-1: codex's `CollaborationMode` for `kind`. Its settings
        replace the thread's model and effort, so they are the conversation's
        own (or the turn's overrides); `developer_instructions: null` asks for
        codex's built-in instructions for the mode."""
        if kind not in MODES:
            raise AgentChatError("invalid_agent_mode")
        model = model or self.settings.get("model")
        if not model:
            # codex requires a model in the settings, and core will not guess one.
            raise AgentChatError("agent_mode_unavailable")
        return {"mode": kind, "settings": {"model": model,
                                           "reasoning_effort": effort or self.settings.get("effort"),
                                           "developer_instructions": None}}

    async def set_mode(self, kind):
        """AGENT-PLAN-1: `thread/settings/update {collaborationMode}`, codex's
        own way to change the mode for the turns that follow (what the TUI's
        `/plan` and Shift+Tab send). Not while a turn runs: the TUI refuses
        `/plan` then too. The mode is codex's to confirm; core shows it now,
        as codex took the request."""
        async with self._send_lock:
            if not self.attached:
                raise AgentChatError("agent_not_ready")
            if self.active_turn is not None:
                raise AgentChatError("agent_busy")
            value = self.collaboration_mode(kind)
            try:
                await self.rpc.request("thread/settings/update",
                                       {"threadId": self.binding.thread_id, "collaborationMode": value})
            except AgentChatError as error:
                if error.code == "provider_request_rejected":
                    # An older codex, or one that refuses the experimental API.
                    raise AgentChatError("agent_mode_unavailable") from None
                raise
            changed = self.mode != kind
            self.mode = kind
            self._emit({"type": "agent.usage.updated", "usage": self.usage_value()})
            return {"mode": kind, "changed": changed}

    async def review(self, instructions=""):
        """AGENT-PLAN-1: `/review` - codex's own review of the workspace, as a
        turn of this conversation (`review/start`, inline). No words: the
        uncommitted changes, as the TUI's first choice; words: the review the
        person asked for (`custom`), as the TUI's `/review <words>`."""
        async with self._send_lock:
            if not self.attached:
                raise AgentChatError("agent_not_ready")
            if self.active_turn is not None:
                raise AgentChatError("agent_busy")
            target = {"type": "custom", "instructions": instructions} if instructions \
                else {"type": "uncommittedChanges"}
            self._review_pending, self._review_turn = True, None
            try:
                result = await self.rpc.request("review/start", {"threadId": self.binding.thread_id, "target": target})
            except BaseException:
                self._review_pending = False
                raise
            turn = (result.get("turn") or {}).get("id") if isinstance(result, dict) else None
            if not isinstance(turn, str):
                self._review_pending = False
                raise AgentChatError("agent_delivery_unconfirmed")
            if self._review_turn is not None and self._review_turn != turn:
                self._turn_alias[self._review_turn] = turn
                if self.active_turn == self._review_turn:
                    # Phones follow the turn by its id: name the right one.
                    self.active_turn = turn
                    self._emit({"type": "turnStarted", "turnID": turn})
            self._review_turn = turn
            while len(self._turn_alias) > 16:
                self._turn_alias.pop(next(iter(self._turn_alias)))
            if self.active_turn is None:
                # The answer reserves the turn, as for a message.
                self.active_turn = turn
                self._track_activity("turn/started", {"turn": {"id": turn}})
                self._emit({"type": "turnStarted", "turnID": turn})
            return {"accepted": True, "turnID": turn, "delivery": "accepted"}

    def conversation_changes(self, limit=16):
        """AGENT-PLAN-1: `/diff` - the files this conversation changed, as
        AGENT-COLLIE-1's change cards hold them (codex's own fileChange items,
        already bounded), newest last; at most `limit`, and at most
        SNAPSHOT_DIFF_BUDGET / 4 characters of diff. No file is read: what a
        command changed outside codex's edits is not here."""
        changes = []
        for row in self.rows.values():
            if row.get("kind") == "tool":
                for change in row.get("changes") or ():
                    changes.append({**copy.deepcopy(change), "turnID": row["turnID"]})
        omitted = max(0, len(changes) - limit)
        changes = changes[omitted:]
        budget = SNAPSHOT_DIFF_BUDGET // 4
        for change in reversed(changes):
            size = len(change.get("diff") or "")
            if size > budget:
                lines = change["diff"].count("\n") + 1 if change["diff"] else 0
                change["diff"] = ""
                change["diff_omitted"] = int(change.get("diff_omitted") or 0) + lines
            budget = max(0, budget - size)
        return changes, omitted

    async def compact(self):
        """AGENT-PLAN-1: `/compact` - `thread/compact/start`, between turns."""
        async with self._send_lock:
            if not self.attached:
                raise AgentChatError("agent_not_ready")
            if self.active_turn is not None:
                raise AgentChatError("agent_busy")
            await self.rpc.request("thread/compact/start", {"threadId": self.binding.thread_id})
            return {"compacting": True}

    async def _send(self, text: str, request_id: str, *, model=None, effort=None, images=(), mode=None):
        if not self.attached or not isinstance(text, str) or not text.strip():
            raise AgentChatError("agent_not_ready")
        fingerprint = hashlib.sha256("\0".join((text, *images)).encode()).hexdigest()
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
        # ATTACH-1: an image is codex's own `localImage {path}` item (the
        # app-server's `UserInput`, read on the host from the upload's path).
        params = {"threadId": self.binding.thread_id, "clientUserMessageId": request_id,
                  "input": [{"type": "text", "text": text},
                            *({"type": "localImage", "path": path} for path in images)]}
        if self.binding.cwd is not None:
            # "Override the working directory for this turn and subsequent
            # turns" (0.154 TurnStartParams.cwd). This is what makes an
            # approved command start, whatever cwd the thread was made with.
            self.binding.ensure_workspace()
            params["cwd"] = self.binding.cwd
        if model is not None:
            params["model"] = model
        if effort is not None:
            params["effort"] = effort
        if mode is not None:
            # AGENT-PLAN-1: `/plan <words>` - the mode and the words in one
            # turn, as the TUI sends them. The mode stays for later turns.
            params["collaborationMode"] = self.collaboration_mode(mode, model=model, effort=effort)
        delivered_images = "native" if images else None
        try:
            result = await self.rpc.request("turn/start", params)
        except AgentChatError as error:
            if not images or error.code != "provider_request_rejected":
                raise
            # A provider that will not take the picture still gets the words,
            # and the path to the picture to open itself - and the reply says
            # so, so the phone can too.
            params["input"] = [{"type": "text", "text": text + "\n\nAttached: " + ", ".join(images)}]
            result = await self.rpc.request("turn/start", params)
            delivered_images = "path"
        turn = result.get("turn", {})
        if not isinstance(turn.get("id"), str):
            raise AgentChatError("agent_delivery_unconfirmed")
        self._requests[request_id]["accepted"] = True
        self._persist_delivery(copy.deepcopy(self._requests))
        # AGENT-NEW-1: the provider took the turn with these overrides, "for
        # this turn and subsequent turns"; the phone's model control shows
        # them now, not only if a thread/settings/updated ever arrives.
        if model is not None or effort is not None or mode is not None:
            for key, value in (("model", model), ("effort", effort)):
                if value is not None:
                    self.settings[key] = value[:64]
            if mode is not None:
                self.mode = mode
            self._emit({"type": "agent.usage.updated", "usage": self.usage_value()})
        # The actual start response reserves the active turn even if its
        # turn/started notification has not arrived yet.
        if self.active_turn is None and turn.get("status") == "inProgress":
            self.active_turn = turn["id"]
        reply = {"accepted": True, "turnID": turn["id"], "delivery": "accepted"}
        if delivered_images:
            reply["images"] = delivered_images
        return reply

    async def cancel(self, turn_id):
        if not self.attached or turn_id != self.active_turn:
            raise AgentChatError("active_turn_changed")
        await self.rpc.request("turn/interrupt", {"threadId": self.binding.thread_id, "turnId": turn_id})
        return {"requested": True}  # Completion only comes from notification.

    async def close(self):
        self.attached = False
        if self._output_flush is not None:
            self._output_flush.cancel()
            self._output_flush = None
        if self._queue_refresh is not None:
            self._queue_refresh.cancel()
            self._queue_refresh = None
        for task in tuple(self._answers):
            task.cancel()
        self._answers.clear()
        await self.rpc.close()
