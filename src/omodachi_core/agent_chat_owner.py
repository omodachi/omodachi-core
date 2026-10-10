"""Manager-owned Codex process/thread lifecycle, without prompting a model.

Only a proven missing default may create a thread. Existing independent TUI
handoff is explicit; this module never stops Herdr panes or shared daemons.

The app-server is codex's own remote surface, not a wrapped stdio process:
`codex app-server --listen ws://127.0.0.1:PORT --ws-auth capability-token`.
The listener is loopback only and the capability token lives in
`~/.config/omodachi/agent/` with mode 0600, so the one client is core.
"""
from __future__ import annotations
import asyncio
from contextlib import suppress
import json
import re
import secrets
import shutil
import socket as socket_module
import sys
import time
import os
from pathlib import Path
import tempfile
from .agent_chat_provider import AgentChatError, CodexWebSocketRPC, DefaultAgentBinding, resume_in_workspace


AGENT_UNIT = "omodachi-agent.service"
# AGENT-NEW-1 (the host lost its Agent after a reboot): where a person's own
# `codex` usually lives when it is not under /usr/bin. omodachid can start
# before the session has imported its PATH into the user manager, and then it
# sees only /usr/local/bin:/usr/bin.
USER_BIN_DIRS = (".local/bin", ".local/share/mise/shims", ".cargo/bin", ".bun/bin", ".npm-global/bin",
                 ".volta/bin", ".local/share/pnpm", ".nix-profile/bin")


def codex_search_path(own_path, manager_path, home):
    """Our PATH, then the user manager's (what the login session imported -
    it may have arrived after this daemon started), then the usual per-user
    bin directories, then the system ones. Order kept, duplicates dropped."""
    parts = []
    for chunk in (own_path or "", manager_path or ""):
        parts.extend(chunk.split(":"))
    parts.extend(str(Path(home) / sub) for sub in USER_BIN_DIRS)
    parts.extend(("/usr/local/bin", "/usr/bin", "/bin"))
    seen, ordered = set(), []
    for part in parts:
        if part and part.startswith("/") and part not in seen:
            seen.add(part); ordered.append(part)
    return ":".join(ordered)
WS_HOST = "127.0.0.1"
TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
# AGENT-NEW-1: the reason a record names when the person asked for a fresh
# conversation (as opposed to the empty-thread recovery after a lost cgroup).
NEW_CONVERSATION = "user_new_conversation"


class OwnedCodexAgent:
    def __init__(self, root: Path, cwd: Path, host_id: str, *, rpc_factory=None, process_factory=None,
                 agent_dir: Path | None = None):
        self.root, self.cwd, self.host_id = Path(root), Path(cwd), host_id
        if not self.root.is_absolute() or not self.cwd.is_absolute() or not host_id:
            raise AgentChatError("agent_owner_location_invalid")
        # Credential and endpoint live beside the metadata, in their own 0700
        # directory, because the token is what authorizes the loopback client.
        self.agent_dir = Path(agent_dir) if agent_dir is not None else self.root.parent / "agent"
        if not self.agent_dir.is_absolute():
            raise AgentChatError("agent_owner_location_invalid")
        self.token_file = self.agent_dir / "ws-token"
        self.endpoint_file = self.agent_dir / "endpoint.json"
        self.metadata = self.root / "owner.json"
        self.rpc_factory = rpc_factory or self._proxy
        self.process_factory = process_factory or self._spawn
        self.durable_unit = sys.platform.startswith("linux") and process_factory is None
        # Single owned default agent per host, so the unit name is fixed.
        self.unit = AGENT_UNIT
        self.process = None
        self.rpc = None
        self.endpoint = None
        self.token = None
        self.lock = asyncio.Lock()
        self.started_settings = None

    # --- loopback endpoint ---------------------------------------------------
    def _ensure_token(self):
        self.agent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            value = self.token_file.read_text().strip()
        except FileNotFoundError:
            value = ""
        if not TOKEN_PATTERN.fullmatch(value):
            value = secrets.token_hex(32)
            fd, name = tempfile.mkstemp(prefix="ws-token-", dir=self.agent_dir)
            try:
                with os.fdopen(fd, "w") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(value)
                    stream.flush(); os.fsync(stream.fileno())
                os.replace(name, self.token_file)
            finally:
                with suppress(FileNotFoundError): os.unlink(name)
        else:
            os.chmod(self.token_file, 0o600)
        self.token = value
        return value

    def _read_endpoint(self):
        try:
            value = json.loads(self.endpoint_file.read_text())
        except (FileNotFoundError, ValueError):
            return None
        port = value.get("port") if isinstance(value, dict) else None
        if type(port) is not int or not 1024 <= port <= 65535 or value.get("host") != WS_HOST:
            return None
        return value

    def _write_endpoint(self, port):
        self.agent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix="endpoint-", dir=self.agent_dir)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({"host": WS_HOST, "port": port, "unit": self.unit if self.durable_unit else None,
                           "token_file": str(self.token_file)}, stream)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(name, self.endpoint_file)
        finally:
            with suppress(FileNotFoundError): os.unlink(name)

    @staticmethod
    def _free_port():
        with socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM) as probe:
            probe.bind((WS_HOST, 0))
            return probe.getsockname()[1]

    def endpoint_url(self, port=None):
        if port is None:
            record = self._read_endpoint()
            port = record["port"] if record else None
        if port is None:
            raise AgentChatError("agent_owner_socket_unavailable")
        return "ws://" + WS_HOST + ":" + str(port)

    async def _unit_command(self, *args):
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.DEVNULL)
        output, _ = await process.communicate()
        return process.returncode, output.decode().strip()

    async def _unit_active(self):
        code, state = await self._unit_command("systemctl", "--user", "is-active", self.unit)
        return code == 0 or state in {"activating", "reloading", "deactivating"}

    async def _clear_dead_endpoint(self):
        """A stale endpoint file may only be dropped when our unit is not running.

        There is no PID on disk to signal and no unrelated daemon to kill: the
        only thing retired here is core's own record of where it used to listen.
        """
        if self.durable_unit and await self._unit_active():
            raise AgentChatError("agent_owner_socket_unavailable")
        with suppress(FileNotFoundError):
            self.endpoint_file.unlink()

    async def _manager_path(self):
        code, output = await self._unit_command("systemctl", "--user", "show-environment")
        if code:
            return ""
        return next((line[5:] for line in output.splitlines() if line.startswith("PATH=")), "")

    async def _spawn(self):
        token = self._ensure_token()
        if self.durable_unit:
            if await self._unit_active():
                return None
            search = codex_search_path(os.environ.get("PATH"), await self._manager_path(), Path.home())
            executable = shutil.which("codex", path=search)
            if not executable: raise AgentChatError("agent_owner_executable_missing")
            port = self._free_port()
            self._write_endpoint(port)
            code, _ = await self._unit_command("systemd-run", "--user", "--unit=" + self.unit,
                "--collect", "--property=Type=exec", "--property=Restart=no",
                "--property=WorkingDirectory=" + str(self.cwd),
                # The same search path, so a shim (mise, asdf) finds its tool.
                "--setenv=PATH=" + search,
                executable, "app-server", "--listen", self.endpoint_url(port),
                "--ws-auth", "capability-token", "--ws-token-file", str(self.token_file))
            if code: raise AgentChatError("agent_owner_unit_start_failed")
            return None  # systemd owns its process outside the core service cgroup.
        port = self._free_port()
        self._write_endpoint(port)
        return await asyncio.create_subprocess_exec("codex", "app-server", "--listen", self.endpoint_url(port),
            "--ws-auth", "capability-token", "--ws-token-file", str(self.token_file),
            cwd=self.cwd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)

    async def _proxy(self):
        return await CodexWebSocketRPC.connect(self.endpoint_url(), token=self._ensure_token(), timeout=5)

    def owned_metadata(self):
        """Read manager ownership before invoking legacy Herdr/TUI ensure."""
        return self._read()

    def _read(self):
        try:
            value = json.loads(self.metadata.read_text())
        except FileNotFoundError:
            return None
        if value.get("host_id") != self.host_id or value.get("provider") != "codex":
            raise AgentChatError("agent_owner_metadata_conflict")
        if value.get("transport") != "ws" or value.get("cwd") != str(self.cwd) or "socket" in value:
            # Transport and workspace are the manager's own record of how it
            # reaches the daemon. A rename or a move rewrites them in place; the
            # thread the user has been talking to is carried across untouched.
            value = self._migrate(value)
        return value

    def _migrate(self, value):
        carried = {key: value[key] for key in ("previous_thread_id", "recovery_reason", "previous_metadata", "mode",
                                               "new_conversation_at")
                   if key in value}
        migrated = self._record(value.get("thread_id"),
                                creation_pending=bool(value.get("creation_pending")),
                                migrated_from={"cwd": value.get("cwd"), "socket": value.get("socket"),
                                               "daemon_unit": value.get("daemon_unit")},
                                **carried)
        self._save(migrated)
        return migrated

    def _save(self, value):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix="owner-", dir=self.root)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(value, stream)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(name, self.metadata)
        finally:
            with suppress(FileNotFoundError): os.unlink(name)

    async def _connect(self):
        if self.rpc is not None:
            return
        # A listener recorded by an earlier manager may still be running;
        # connect first. We never kill an unrelated or surviving daemon.
        if self._read_endpoint() is not None:
            rpc = None
            try:
                rpc = await self.rpc_factory()
                await rpc.initialize()
                self.rpc = rpc
                self.endpoint = self.endpoint_url()
                return
            except (AgentChatError, OSError, asyncio.TimeoutError):
                if rpc: await rpc.close()
                await self._clear_dead_endpoint()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.process = await self.process_factory()
        for _ in range(250):
            if self.process is not None and self.process.returncode is not None:
                raise AgentChatError("agent_owner_start_failed")
            if self._read_endpoint() is not None:
                try:
                    rpc = await self.rpc_factory()
                except (AgentChatError, OSError, asyncio.TimeoutError):
                    await asyncio.sleep(.02)
                    continue
                try:
                    await rpc.initialize()
                    self.rpc = rpc
                    self.endpoint = self.endpoint_url()
                    return
                except BaseException:
                    await rpc.close()
                    raise
            await asyncio.sleep(.02)
        raise AgentChatError("agent_owner_start_timeout")

    def _record(self, thread_id=None, **extra):
        return {"host_id": self.host_id, "provider": "codex", "cwd": str(self.cwd),
                "transport": "ws", "endpoint_file": str(self.endpoint_file),
                "token_file": str(self.token_file), "thread_id": thread_id,
                "daemon_unit": self.unit if self.durable_unit else None, **extra}

    def _binding(self, thread):
        return DefaultAgentBinding(self.host_id, thread, self.endpoint_url(), token=self.token or self._ensure_token(),
                                   cwd=str(self.cwd))

    def _ensure_workspace(self):
        # The agent's working directory is the user's (never deleted by
        # --purge). A thread pointed at a missing directory cannot start a
        # single command, so it is recreated before any resume or start.
        self.cwd.mkdir(parents=True, exist_ok=True)

    async def ensure_new_or_owned(self, *, existing_agent: bool = False):
        """Call only from the single existing default manager/probe lifecycle."""
        async with self.lock:
            record = self._read()
            if record is not None and record.get('mode')=='legacy_handoff_rolled_back':
                raise AgentChatError('existing_thread_handoff_required')
            if record is None and existing_agent:
                raise AgentChatError("existing_thread_handoff_required")
            if record is not None and not record.get("thread_id"):
                if self._new_conversation_pending(record):
                    # The person asked for a fresh conversation and core went
                    # away between the intent and the identity. Finishing it
                    # is what they asked for; the old thread is untouched.
                    self._ensure_workspace()
                    await self._connect()
                    return await self._start_new_thread(record)
                raise AgentChatError("agent_thread_creation_unconfirmed")
            self._ensure_workspace()
            await self._connect()
            if record:
                try:
                    response = await resume_in_workspace(self.rpc, record["thread_id"], str(self.cwd))
                except AgentChatError as error:
                    if error.code != "provider_thread_rollout_missing":
                        raise
                    # A just-created thread exists in this daemon but has no
                    # on-disk rollout until its first user input.
                    cursor = None
                    while True:
                        params = {"limit": 100}
                        if cursor is not None: params["cursor"] = cursor
                        page = await self.rpc.request("thread/loaded/list", params)
                        if record["thread_id"] in page.get("data", []): break
                        cursor = page.get("nextCursor")
                        if cursor is None: raise AgentChatError("provider_thread_rollout_missing")
                    response = await self.rpc.request("thread/read", {"threadId": record["thread_id"], "includeTurns": False})
                if response.get("thread", {}).get("id") != record["thread_id"]:
                    raise AgentChatError("provider_identity_changed")
                return self._binding(record["thread_id"])
            # Persist intent before RPC. Crash/timeout must not create another
            # default thread on retry merely because its identity was lost.
            self._save(self._record(creation_pending=True))
            response = await self.rpc.request("thread/start", {"cwd": str(self.cwd)})
            thread = response.get("thread", {}).get("id")
            if not isinstance(thread, str) or not thread:
                raise AgentChatError("agent_thread_creation_unconfirmed")
            self._save(self._record(thread, creation_pending=False))
            self.started_settings = {"model": response.get("model"), "effort": response.get("reasoningEffort")}
            return self._binding(thread)

    async def ensure_result(self, *, existing_agent: bool = False):
        # Shared manager serializes ensure calls, as for its existing lifecycle.
        had_record = self._read() is not None
        binding = await self.ensure_new_or_owned(existing_agent=existing_agent)
        return {"binding": binding, "attach_argv": self.tui_argv(binding),
                "created": not had_record, "herdr_pane_registered": False}

    def prepare_existing(self, thread_id: str, pane_id: str):
        if not thread_id or not pane_id:
            raise AgentChatError("structured_agent_binding_required")
        record = self._read_endpoint()
        return {"status": "existing_thread_handoff_required", "thread_id": thread_id,
                "pane_id": pane_id, "preserves_thread": True,
                "tui_argv": ["codex", "resume", "--remote",
                             self.endpoint_url(record["port"]) if record else "ws://" + WS_HOST + ":0", thread_id]}

    async def resume_existing(self, thread_id: str, *, handoff_confirmed: bool = False):
        """B must complete explicit old-writer handoff before calling this."""
        if not handoff_confirmed or not thread_id:
            raise AgentChatError("existing_thread_handoff_required")
        async with self.lock:
            record = self._read()
            if record and record.get("thread_id") not in (None, thread_id):
                raise AgentChatError("agent_owner_metadata_conflict")
            self._ensure_workspace()
            await self._connect()
            response = await resume_in_workspace(self.rpc, thread_id, str(self.cwd))
            if response.get("thread", {}).get("id") != thread_id:
                raise AgentChatError("provider_identity_changed")
            self._save(self._record(thread_id, creation_pending=False))
            return self._binding(thread_id)

    @staticmethod
    def _new_conversation_pending(record):
        return (bool(record.get("creation_pending")) and not record.get("thread_id")
                and record.get("recovery_reason") == NEW_CONVERSATION
                and isinstance(record.get("previous_thread_id"), str) and bool(record.get("previous_thread_id")))

    async def _thread_busy(self, thread_id):
        """What codex itself says about the old thread, for a caller that has
        no attached chat to ask. `None` when it is not loaded here (nothing
        can be running on it in this daemon) or the provider does not say."""
        cursor = None
        while True:
            params = {"limit": 100}
            if cursor is not None: params["cursor"] = cursor
            page = await self.rpc.request("thread/loaded/list", params)
            if thread_id in page.get("data", []): break
            cursor = page.get("nextCursor")
            if cursor is None: return None
        try:
            value = await self.rpc.request("thread/read", {"threadId": thread_id, "includeTurns": False})
        except AgentChatError:
            return None
        status = (value.get("thread") or {}).get("status")
        if not isinstance(status, dict) or status.get("type") != "active":
            return None
        flags = [flag for flag in status.get("activeFlags") or [] if isinstance(flag, str)]
        return "agent_approval_pending" if ("waitingOnApproval" in flags or "waitingOnUserInput" in flags) \
            else "agent_turn_active"

    async def _start_new_thread(self, record):
        """thread/start for a recorded new-conversation intent, then the identity."""
        response = await self.rpc.request("thread/start", {"cwd": str(self.cwd)})
        thread = response.get("thread", {}).get("id")
        if not isinstance(thread, str) or not thread or thread == record["previous_thread_id"]:
            raise AgentChatError("agent_thread_creation_unconfirmed")
        carried = {key: record[key] for key in ("previous_thread_id", "recovery_reason", "previous_metadata",
                                                "new_conversation_at") if key in record}
        self._save(self._record(thread, creation_pending=False, **carried))
        # What the thread starts with, so the phone's model control can show
        # it before the first turn confirms anything (ThreadStartResponse).
        self.started_settings = {"model": response.get("model"), "effort": response.get("reasoningEffort")}
        return self._binding(thread)

    async def start_new(self, expected_thread_id: str):
        """AGENT-NEW-1: a fresh default conversation the person asked for.

        The old thread is not deleted, archived or touched in codex: it stays in
        `~/.codex/sessions` and can be resumed by id. Here only the manager's
        record moves, under the owner lock, in the same crash-safe order as the
        first creation: the old record is archived to its own file, the intent
        (`creation_pending`, `previous_thread_id`) is saved, and only then is
        `thread/start` called. A core that dies in between finishes the intent
        on the next ensure instead of leaving the person without a conversation.
        The caller has already refused a running turn or a pending approval it
        can see; `_thread_busy` asks codex as well, for a chat not attached.
        """
        async with self.lock:
            record = self._read()
            if record is None or record.get("mode") == "legacy_handoff_rolled_back":
                raise AgentChatError("agent_owner_metadata_conflict")
            self._ensure_workspace()
            await self._connect()
            if self._new_conversation_pending(record):
                if record["previous_thread_id"] != expected_thread_id:
                    raise AgentChatError("agent_identity_changed")
                return await self._start_new_thread(record)
            if not record.get("thread_id") or record.get("thread_id") != expected_thread_id:
                raise AgentChatError("agent_identity_changed")
            busy = await self._thread_busy(expected_thread_id)
            if busy:
                raise AgentChatError(busy)
            stamp = time.time_ns()
            archive = self.root / ("owner-before-new-conversation-" + str(stamp) + ".json")
            with archive.open("x") as stream:
                os.chmod(archive, 0o600)
                json.dump(record, stream)
                stream.flush(); os.fsync(stream.fileno())
            intent = {"previous_thread_id": expected_thread_id, "recovery_reason": NEW_CONVERSATION,
                      "previous_metadata": archive.name,
                      "new_conversation_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp / 1e9))}
            pending = self._record(creation_pending=True, **intent)
            self._save(pending)
            return await self._start_new_thread(pending)

    async def recreate_empty_lost(self, expected_thread_id: str):
        """Explicit recovery only. B verifies user confirmation + empty delivery
        journal before calling; we verify identity and authoritative absence.
        Returns a new ID transparently, never labels replacement same-session.
        """
        async with self.lock:
            record = self._read()
            if (not record or record.get("thread_id") != expected_thread_id
                    or record.get("mode") == "legacy_handoff_rolled_back"):
                raise AgentChatError("agent_owner_metadata_conflict")
            await self._connect()
            cursor = None
            while True:
                params = {"limit": 100}
                if cursor is not None: params["cursor"] = cursor
                page = await self.rpc.request("thread/loaded/list", params)
                if expected_thread_id in page.get("data", []):
                    raise AgentChatError("agent_thread_still_available")
                cursor = page.get("nextCursor")
                if cursor is None: break
            try:
                await self.rpc.request("thread/read", {"threadId": expected_thread_id, "includeTurns": False})
            except AgentChatError as error:
                if error.code == "provider_thread_not_loaded":
                    # Not loaded is not absent: persisted history may still be
                    # resumable. Only an explicit missing rollout permits the
                    # already-confirmed empty-session replacement.
                    try:
                        await self.rpc.request("thread/resume", {"threadId": expected_thread_id})
                    except AgentChatError as resume_error:
                        if resume_error.code != "provider_thread_rollout_missing": raise
                    else:
                        raise AgentChatError("agent_thread_still_available")
                elif error.code != "provider_thread_rollout_missing": raise
            else:
                raise AgentChatError("agent_thread_still_available")
            archive = self.root / ("owner-before-empty-recovery-" + str(time.time_ns()) + ".json")
            with archive.open("x") as stream:
                os.chmod(archive, 0o600)
                json.dump(record, stream)
                stream.flush(); os.fsync(stream.fileno())
            recovery = {"previous_thread_id": expected_thread_id,
                        "recovery_reason": "confirmed_empty_thread_lost_with_core_cgroup",
                        "previous_metadata": archive.name}
            self._save(self._record(creation_pending=True, **recovery))
            self._ensure_workspace()
            response = await self.rpc.request("thread/start", {"cwd": str(self.cwd)})
            thread = response.get("thread", {}).get("id")
            if not isinstance(thread, str) or not thread or thread == expected_thread_id:
                raise AgentChatError("agent_thread_creation_unconfirmed")
            self._save(self._record(thread, creation_pending=False, **recovery))
            return self._binding(thread)

    def tui_argv(self, binding):
        return ["codex", "resume", "--remote", binding.remote_url, binding.thread_id]

    async def close_proxy(self):
        if self.rpc:
            await self.rpc.close()
            self.rpc = None

    async def stop_owned(self):
        """Explicit manager shutdown only; never signal a PID from disk."""
        await self.close_proxy()
        if self.durable_unit:
            code, _ = await self._unit_command("systemctl", "--user", "stop", self.unit)
            if code: raise AgentChatError("agent_owner_unit_stop_failed")
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try: await asyncio.wait_for(self.process.wait(), 3)
            except asyncio.TimeoutError:
                self.process.kill(); await self.process.wait()
        self.process = None
