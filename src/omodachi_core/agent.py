"""Read-only default-agent and Herdr capability/state models.

Models accept sanitized snapshots. The optional local adapter only invokes
fixed, read-only argv with bounded output and deadlines. All operation targets
are explicit agent and pane identifiers; a focused pane is never inferred.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Mapping
import re

_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_KIND_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}")


class AgentStatus(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"
    DONE = "done"
    UNKNOWN = "unknown"


class ProbeStatus(str, Enum):
    AVAILABLE = "available"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    NOT_RUNNING = "not_running"
    UNSUPPORTED = "unsupported"



def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a non-empty explicit identifier")
    return value


@dataclass(frozen=True)
class AgentTarget:
    """Explicit target used by every agent/pane operation."""

    agent_id: str
    pane_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", _clean_id(self.agent_id, "agent_id"))
        object.__setattr__(self, "pane_id", _clean_id(self.pane_id, "pane_id"))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentTarget":
        return cls(agent_id=data.get("agent_id") or data.get("name"), pane_id=data.get("pane_id"))

    def to_dict(self) -> dict[str, str]:
        return {"agent_id": self.agent_id, "pane_id": self.pane_id}


@dataclass(frozen=True)
class AgentState:
    """Sanitized state for a single Herdr agent."""

    agent_id: str
    kind: str | None
    status: AgentStatus = AgentStatus.UNKNOWN
    pane_id: str | None = None
    pane_available: bool = False
    explicit_target: AgentTarget | None = None
    last_changed_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", _clean_id(self.agent_id, "agent_id"))
        if self.kind is not None:
            object.__setattr__(self, "kind", _clean_id(self.kind, "kind").lower())
        if self.pane_id is not None:
            object.__setattr__(self, "pane_id", _clean_id(self.pane_id, "pane_id"))
        if isinstance(self.status, str):
            object.__setattr__(self, "status", AgentStatus(self.status))
        if self.explicit_target is not None and self.explicit_target.agent_id != self.agent_id:
            raise ValueError("explicit_target.agent_id must match agent_id")
        if self.explicit_target is not None and self.explicit_target.pane_id != self.pane_id:
            raise ValueError("explicit_target.pane_id must match pane_id")
        if self.pane_available and not self.pane_id:
            raise ValueError("pane_available requires pane_id")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentState":
        target_data = data.get("target")
        target = AgentTarget.from_mapping(target_data) if isinstance(target_data, Mapping) else None
        status = data.get("status", AgentStatus.UNKNOWN.value)
        try:
            status_value = AgentStatus(status)
        except ValueError:
            status_value = AgentStatus.UNKNOWN
        pane_id = data.get("pane_id") or (target.pane_id if target else None)
        return cls(
            agent_id=data.get("agent_id") or data.get("name"),
            kind=data.get("kind"),
            status=status_value,
            pane_id=pane_id,
            pane_available=bool(data.get("pane_available", False)),
            explicit_target=target,
            last_changed_at=data.get("last_changed_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "agent_id": self.agent_id,
            "kind": self.kind,
            "status": self.status.value,
            "pane_id": self.pane_id,
            "pane_available": self.pane_available,
        }
        if self.explicit_target:
            result["target"] = self.explicit_target.to_dict()
        if self.last_changed_at:
            result["last_changed_at"] = self.last_changed_at
        return result


@dataclass(frozen=True)
class DefaultAgentCapabilities:
    """Result of non-mutating default-agent capability detection."""

    omarchy_default_agent: str | None
    omarchy_probe: ProbeStatus
    herdr_supported_kinds: frozenset[str] = field(default_factory=frozenset)
    herdr_probe: ProbeStatus = ProbeStatus.NOT_RUNNING
    default_agent_exists: bool = False
    pane_id: str | None = None
    pane_available: bool = False
    agent_status: AgentStatus = AgentStatus.UNKNOWN
    default_agent_probe: ProbeStatus = ProbeStatus.UNREADABLE
    pane_probe: ProbeStatus = ProbeStatus.UNREADABLE
    configured_kind: str | None = None
    actual_kind: str | None = None
    kind_mismatch: bool = False
    agent_id: str = "default"
    checked_at: str = field(default_factory=_utc_now)
    diagnostics: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.omarchy_default_agent is not None:
            object.__setattr__(self, "omarchy_default_agent", _clean_id(self.omarchy_default_agent, "omarchy_default_agent").lower())
        object.__setattr__(self, "configured_kind", self.omarchy_default_agent)
        if self.actual_kind is not None:
            object.__setattr__(self, "actual_kind", _clean_id(self.actual_kind, "actual_kind").lower())
        object.__setattr__(self, "kind_mismatch", bool(self.actual_kind and self.omarchy_default_agent and self.actual_kind != self.omarchy_default_agent))
        object.__setattr__(self, "agent_id", _clean_id(self.agent_id, "agent_id"))
        object.__setattr__(self, "herdr_supported_kinds", frozenset(str(k).lower() for k in self.herdr_supported_kinds))
        if isinstance(self.pane_probe, str):
            object.__setattr__(self, "pane_probe", ProbeStatus(self.pane_probe))
        if isinstance(self.default_agent_probe, str):
            object.__setattr__(self, "default_agent_probe", ProbeStatus(self.default_agent_probe))
        if isinstance(self.omarchy_probe, str):
            object.__setattr__(self, "omarchy_probe", ProbeStatus(self.omarchy_probe))
        if isinstance(self.herdr_probe, str):
            object.__setattr__(self, "herdr_probe", ProbeStatus(self.herdr_probe))
        if isinstance(self.agent_status, str):
            try:
                object.__setattr__(self, "agent_status", AgentStatus(self.agent_status))
            except ValueError:
                object.__setattr__(self, "agent_status", AgentStatus.UNKNOWN)
        if self.pane_available and not self.pane_id:
            raise ValueError("pane_available requires pane_id")

    @property
    def configured(self) -> bool:
        return self.omarchy_probe == ProbeStatus.AVAILABLE and bool(self.omarchy_default_agent)

    @property
    def herdr_available(self) -> bool:
        return self.herdr_probe == ProbeStatus.AVAILABLE

    @property
    def kind_supported(self) -> bool:
        return bool(self.omarchy_default_agent) and self.omarchy_default_agent in self.herdr_supported_kinds

    @property
    def actual_kind_supported(self) -> bool:
        return bool(self.actual_kind) and self.actual_kind in self.herdr_supported_kinds

    @property
    def ready_to_attach(self) -> bool:
        return self.herdr_available and self.default_agent_exists and self.pane_available and self.kind_supported and self.actual_kind_supported and not self.kind_mismatch

    @property
    def ready_to_start(self) -> bool:
        return self.herdr_available and self.default_agent_probe is ProbeStatus.MISSING and (not self.default_agent_exists) and self.configured and self.kind_supported and self.pane_available

    @classmethod
    def from_snapshot(cls, data: Mapping[str, Any]) -> "DefaultAgentCapabilities":
        """Build from a sanitized probe snapshot; never executes a command."""
        supported = data.get("herdr_supported_kinds", data.get("supported_kinds", [])) or []
        return cls(
            omarchy_default_agent=data.get("omarchy_default_agent"),
            omarchy_probe=data.get("omarchy_probe", ProbeStatus.MISSING.value),
            herdr_supported_kinds=frozenset(supported),
            herdr_probe=data.get("herdr_probe", ProbeStatus.NOT_RUNNING.value),
            default_agent_exists=bool(data.get("default_agent_exists", False)),
            pane_id=data.get("pane_id"),
            pane_available=bool(data.get("pane_available", False)),
            agent_status=data.get("agent_status", AgentStatus.UNKNOWN.value),
            default_agent_probe=data.get("default_agent_probe", ProbeStatus.UNREADABLE.value),
            pane_probe=data.get("pane_probe", ProbeStatus.UNREADABLE.value),
            actual_kind=data.get("actual_kind"),
            agent_id=data.get("agent_id", "default"),
            checked_at=data.get("checked_at", _utc_now()),
            diagnostics=tuple(str(x) for x in (data.get("diagnostics") or [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "omarchy_default_agent": self.omarchy_default_agent,
            "omarchy_probe": self.omarchy_probe.value,
            "herdr_supported_kinds": sorted(self.herdr_supported_kinds),
            "herdr_probe": self.herdr_probe.value,
            "default_agent_exists": self.default_agent_exists,
            "default_agent_probe": self.default_agent_probe.value,
            "pane_probe": self.pane_probe.value,
            "agent_id": self.agent_id,
            "pane_id": self.pane_id,
            "pane_available": self.pane_available,
            "agent_status": self.agent_status.value,
            "configured_kind": self.configured_kind,
            "actual_kind": self.actual_kind,
            "kind_mismatch": self.kind_mismatch,
            "herdr_available": self.herdr_available,
            "checked_at": self.checked_at,
            "diagnostics": list(self.diagnostics),
            "configured": self.configured,
            "kind_supported": self.kind_supported,
            "ready_to_attach": self.ready_to_attach,
            "ready_to_start": self.ready_to_start,
        }


@dataclass(frozen=True)
class HerdrStatusSnapshot:
    """Device-level, redacted Herdr status suitable for /v1/state or fixtures."""

    server_installed: bool | None
    server_running: bool | None
    socket_available: bool | None
    supported_kinds: frozenset[str]
    agents: tuple[AgentState, ...] = field(default_factory=tuple)
    pane_count: int | None = None
    checked_at: str = field(default_factory=_utc_now)
    diagnostics: tuple[str, ...] = field(default_factory=tuple)

    schema_probe: ProbeStatus = ProbeStatus.UNREADABLE

    def __post_init__(self) -> None:
        for name in ("server_installed", "server_running", "socket_available"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise ValueError(f"{name} must be a boolean or null")
        if isinstance(self.schema_probe, str):
            object.__setattr__(self, "schema_probe", ProbeStatus(self.schema_probe))

    @classmethod
    def from_snapshot(cls, data: Mapping[str, Any]) -> "HerdrStatusSnapshot":
        agents = tuple(AgentState.from_mapping(x) for x in (data.get("agents") or []) if isinstance(x, Mapping))
        return cls(
            server_installed=data.get("server_installed"),
            server_running=data.get("server_running"),
            socket_available=data.get("socket_available"),
            schema_probe=data.get("schema_probe", ProbeStatus.UNREADABLE.value),
            supported_kinds=frozenset(str(k).lower() for k in (data.get("supported_kinds") or [])),
            agents=agents,
            pane_count=data.get("pane_count"),
            checked_at=data.get("checked_at", _utc_now()),
            diagnostics=tuple(str(x) for x in (data.get("diagnostics") or [])),
        )

    @property
    def default_agent(self) -> AgentState | None:
        return next((a for a in self.agents if a.agent_id == "default"), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_installed": self.server_installed,
            "server_running": self.server_running,
            "socket_available": self.socket_available,
            "schema_probe": self.schema_probe.value,
            "supported_kinds": sorted(self.supported_kinds),
            "agents": [a.to_dict() for a in self.agents],
            "pane_count": self.pane_count,
            "checked_at": self.checked_at,
            "diagnostics": list(self.diagnostics),
        }


def build_default_agent_capabilities(
    *,
    omarchy_default_agent: str | None,
    omarchy_probe: ProbeStatus | str,
    herdr_supported_kinds: Iterable[str],
    herdr_probe: ProbeStatus | str,
    default_agent: AgentState | None,
    pane_available: bool,
    pane_id: str | None,
    diagnostics: Iterable[str] = (),
) -> DefaultAgentCapabilities:
    """Aggregate independent read-only probes into the stable state model."""
    return DefaultAgentCapabilities(
        omarchy_default_agent=omarchy_default_agent,
        omarchy_probe=omarchy_probe,
        herdr_supported_kinds=frozenset(herdr_supported_kinds),
        herdr_probe=herdr_probe,
        default_agent_exists=default_agent is not None,
        default_agent_probe=ProbeStatus.AVAILABLE if default_agent else ProbeStatus.MISSING,
        pane_id=default_agent.pane_id if default_agent else pane_id,
        pane_available=pane_available,
        pane_probe=ProbeStatus.AVAILABLE if pane_available else ProbeStatus.UNREADABLE,
        agent_status=default_agent.status if default_agent else AgentStatus.UNKNOWN,
        actual_kind=default_agent.kind if default_agent else None,
        agent_id=default_agent.agent_id if default_agent else "default",
        diagnostics=tuple(diagnostics),
    )



@dataclass(frozen=True)
class ReadOnlyProbeCommands:
    """Only fixed non-mutating argv, verified against Herdr 0.8.2 help."""
    omarchy_default_agent: tuple[str, ...] = ("omarchy-default-agent",)
    herdr_help: tuple[str, ...] = ("herdr", "agent", "start", "--help")
    herdr_schema: tuple[str, ...] = ("herdr", "api", "schema", "--json")
    herdr_status: tuple[str, ...] = ("herdr", "status")
    default_agent: tuple[str, ...] = ("herdr", "agent", "get", "default")

    def all(self) -> dict[str, tuple[str, ...]]:
        return dict(self.__dict__) if self.__dict__ else {name: getattr(self, name) for name in self.__dataclass_fields__}


def probe_commands() -> ReadOnlyProbeCommands:
    return ReadOnlyProbeCommands()


@dataclass(frozen=True)
class ProbeOutput:
    returncode: int
    stdout: str = ""
    # Machine-readable failures only; stderr/exception text can contain secrets.
    error: str | None = None


class ReadOnlyAgentProbe:
    """Bounded read-only CLI adapter; injected runners support offline fixtures.

    Server status is checked before any runtime lookup. Metadata queries never
    call agent read, pane read, current/focused targets, start, or prompt. The
    local runner kills its process group at a deadline or byte limit and never
    captures stderr. The runner and limits are host-owned, not client settings.
    """

    def __init__(self, runner: Callable[[tuple[str, ...]], ProbeOutput] | None = None,
                 *, timeout_seconds: float = 5.0, max_bytes: int = 1024 * 1024,
                 session: str | None = None):
        if session is not None and session != "omodachi":
            raise ValueError("unsupported owned Herdr session")
        self.session = session
        self._validate_limits(timeout_seconds, max_bytes)
        self.timeout_seconds, self.max_bytes = timeout_seconds, max_bytes
        self.runner = runner or (lambda argv: self._local_runner(
            argv, timeout_seconds=timeout_seconds, max_bytes=max_bytes))

    @staticmethod
    def _validate_limits(timeout_seconds: float, max_bytes: int) -> None:
        import math
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise ValueError("probe timeout must be positive and at most 30 seconds")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 4 * 1024 * 1024:
            raise ValueError("probe output limit must be 1 through 4194304 bytes")

    @staticmethod
    def _allowed_argv(argv: tuple[str, ...]) -> bool:
        if not isinstance(argv, tuple) or not all(isinstance(arg, str) for arg in argv):
            return False
        if argv[:3] == ("herdr", "--session", "omodachi"):
            argv = ("herdr",) + argv[3:]
        return (argv in probe_commands().all().values() or
                (len(argv) == 4 and argv[:3] == ("herdr", "pane", "get") and
                 bool(_ID_PATTERN.fullmatch(argv[3]))))

    @staticmethod
    def _local_runner(argv: tuple[str, ...], *, timeout_seconds: float = 5.0,
                      max_bytes: int = 1024 * 1024) -> ProbeOutput:
        if not ReadOnlyAgentProbe._allowed_argv(argv):
            return ProbeOutput(126, error="invalid_command")
        return ReadOnlyAgentProbe._run_process(argv, timeout_seconds=timeout_seconds, max_bytes=max_bytes)

    @staticmethod
    def _run_process(argv: tuple[str, ...], *, timeout_seconds: float = 5.0,
                     max_bytes: int = 1024 * 1024) -> ProbeOutput:
        """Internal bounded process primitive; callers own separate read/write allowlists."""
        import json
        import os
        import selectors
        import signal
        import subprocess
        import time

        ReadOnlyAgentProbe._validate_limits(timeout_seconds, max_bytes)
        process = None
        selector = selectors.DefaultSelector()
        collected = bytearray()
        error_bytes = bytearray()
        deadline = time.monotonic() + timeout_seconds

        def terminate_group() -> None:
            if process is None:
                return
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                if process.poll() is None:
                    process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)

        try:
            process = subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    terminate_group()
                    return ProbeOutput(124, error="timeout")
                for key, _ in selector.select(remaining):
                    # Read at most the remaining budget plus one overflow byte.
                    chunk = os.read(key.fd, min(65536, max_bytes - len(collected) - len(error_bytes) + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif len(collected) + len(error_bytes) + len(chunk) > max_bytes:
                        terminate_group()
                        return ProbeOutput(125, error="output_limit")
                    else:
                        (collected if key.data == "stdout" else error_bytes).extend(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate_group()
                return ProbeOutput(124, error="timeout")
            process.wait(timeout=remaining)
            try:
                text = collected.decode("utf-8", errors="strict")
                if not text and process.returncode:
                    # Herdr machine failures are JSON on stderr. Keep only the
                    # code; never expose its message, title, or process details.
                    try:
                        code = json.loads(error_bytes)["error"]["code"]
                        if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code):
                            text = json.dumps({"error": {"code": code}})
                    except (ValueError, KeyError, TypeError):
                        pass
                return ProbeOutput(process.returncode, text)
            except UnicodeDecodeError:
                return ProbeOutput(1, error="invalid_output")
        except FileNotFoundError:
            return ProbeOutput(127, error="executable_missing")
        except subprocess.TimeoutExpired:
            terminate_group()
            return ProbeOutput(124, error="timeout")
        except OSError:
            terminate_group()
            return ProbeOutput(127, error="execution_failed")
        finally:
            selector.close()
            if process and process.stdout:
                process.stdout.close()
            if process and process.stderr:
                process.stderr.close()
            if process and process.poll() is None:
                terminate_group()

    def _run(self, argv: tuple[str, ...]) -> ProbeOutput:
        if not self._allowed_argv(argv):
            return ProbeOutput(126, error="invalid_command")
        try:
            actual = ("herdr", "--session", self.session) + argv[1:] if self.session and argv[0] == "herdr" else argv
            result = self.runner(actual)
        except Exception:
            return ProbeOutput(1, error="runner_failed")
        if not isinstance(result, ProbeOutput) or type(result.returncode) is not int or not isinstance(result.stdout, str):
            return ProbeOutput(1, error="invalid_output")
        try:
            output_size = len(result.stdout.encode("utf-8"))
        except UnicodeError:
            return ProbeOutput(1, error="invalid_output")
        if output_size > self.max_bytes:
            return ProbeOutput(125, error="output_limit")
        return result

    @staticmethod
    def _server_running(output: ProbeOutput) -> bool | None:
        if output.returncode or output.error:
            return None
        # Only the server section counts. A client/update status must not do so.
        section = re.search(r"(?m)^server:\s*\n((?:[ \t]+[^\n]*(?:\n|$))*)", output.stdout)
        if not section:
            return None
        status = re.search(r"(?m)^[ \t]+status:[ \t]*(running|not running)[ \t]*$", section[1])
        return status[1] == "running" if status else None

    @staticmethod
    def _json_object(output: ProbeOutput) -> dict[str, Any] | None:
        import json
        if output.error:
            return None
        try:
            value = json.loads(output.stdout)
            return value if isinstance(value, dict) else None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _schema_probe(output: ProbeOutput) -> ProbeStatus:
        if output.returncode or output.error:
            return ProbeStatus.UNREADABLE
        schema = ReadOnlyAgentProbe._json_object(output)
        if schema is None:
            return ProbeStatus.UNREADABLE
        if type(schema.get("protocol")) is not int or schema["protocol"] != 20 or type(schema.get("schema_version")) is not int or schema["schema_version"] != 1:
            return ProbeStatus.UNSUPPORTED
        try:
            definitions = schema["schemas"]["success_response"]["$defs"]
            agent_fields = definitions["AgentInfo"]["properties"]
            pane_fields = definitions["PaneInfo"]["properties"]
            statuses = definitions["AgentStatus"]["enum"]
            compatible = (isinstance(agent_fields, dict) and isinstance(pane_fields, dict)
                          and {"name", "agent", "agent_status", "pane_id"} <= agent_fields.keys()
                          and "pane_id" in pane_fields
                          and set(statuses) == {status.value for status in AgentStatus})
        except (KeyError, TypeError):
            compatible = False
        return ProbeStatus.AVAILABLE if compatible else ProbeStatus.UNSUPPORTED

    @staticmethod
    def _unwrap(payload: dict[str, Any], name: str) -> dict[str, Any] | None:
        result = payload.get("result", payload)
        if not isinstance(result, dict):
            return None
        if "type" in result and result["type"] != f"{name}_info":
            return None
        nested = result.get(name)
        # AgentInfo.agent is the kind string; ResponseResult.agent is an object.
        if isinstance(nested, dict):
            return nested
        return result

    @staticmethod
    def _error_code(payload: dict[str, Any] | None) -> str | None:
        if not payload:
            return None
        error = payload.get("error")
        return error.get("code") if isinstance(error, dict) and isinstance(error.get("code"), str) else None

    def inspect(self) -> tuple[DefaultAgentCapabilities, HerdrStatusSnapshot]:
        cmds = probe_commands()
        diagnostics: list[str] = []
        preference = self._run(cmds.omarchy_default_agent)
        raw_kind = preference.stdout.strip() if preference.returncode == 0 and not preference.error else None
        configured = raw_kind if raw_kind and _KIND_PATTERN.fullmatch(raw_kind) else None
        omarchy_probe = (ProbeStatus.AVAILABLE if configured else ProbeStatus.MISSING
                          if raw_kind == "" else ProbeStatus.UNREADABLE)
        if omarchy_probe is not ProbeStatus.AVAILABLE:
            diagnostics.append("omarchy_default_unset" if omarchy_probe is ProbeStatus.MISSING else "omarchy_default_probe_unreadable")

        help_output = self._run(cmds.herdr_help)
        match = re.search(r"\[possible values:\s*([^\]]+)\]", help_output.stdout)
        kinds = [kind.strip() for kind in match[1].split(",")] if match else []
        supported = frozenset(kinds) if not help_output.returncode and not help_output.error and kinds and all(_KIND_PATTERN.fullmatch(kind) for kind in kinds) else frozenset()
        status = self._run(cmds.herdr_status)
        installed = (True if (help_output.returncode == 0 and not help_output.error) or (status.returncode == 0 and not status.error) else
                     False if help_output.error == status.error == "executable_missing" else None)
        running = self._server_running(status)
        schema_probe = ProbeStatus.UNREADABLE
        socket_available: bool | None = False if running is False else None
        herdr_probe = (ProbeStatus.NOT_RUNNING if running is False else
                       ProbeStatus.MISSING if installed is False else ProbeStatus.UNREADABLE)
        agent: AgentState | None = None
        agent_probe = ProbeStatus.UNREADABLE
        pane_probe = ProbeStatus.UNREADABLE
        if not supported:
            diagnostics.append("herdr_kind_probe_unavailable")
        if running is False:
            diagnostics.append("herdr_server_not_running")
        elif running is None:
            diagnostics.append("herdr_missing" if installed is False else "herdr_server_probe_unreadable")
        else:
            schema_probe = self._schema_probe(self._run(cmds.herdr_schema))
            if schema_probe is not ProbeStatus.AVAILABLE:
                herdr_probe = schema_probe
                agent_probe = schema_probe
                diagnostics.append("herdr_schema_unsupported" if schema_probe is ProbeStatus.UNSUPPORTED else "herdr_schema_unreadable")
            else:
                response = self._run(cmds.default_agent)
                payload = self._json_object(response)
                error_code = self._error_code(payload)
                data = self._unwrap(payload, "agent") if payload else None
                if error_code == "agent_not_found":
                    socket_available = True
                    herdr_probe = ProbeStatus.AVAILABLE
                    agent_probe = ProbeStatus.MISSING
                elif response.returncode == 0 and data and data.get("name") == "default":
                    socket_available = True
                    herdr_probe = ProbeStatus.AVAILABLE
                    raw_actual_kind = data.get("agent")
                    actual_kind = raw_actual_kind if isinstance(raw_actual_kind, str) and _KIND_PATTERN.fullmatch(raw_actual_kind) else None
                    raw_status = data.get("agent_status")
                    agent_status = AgentStatus(raw_status) if isinstance(raw_status, str) and raw_status in AgentStatus._value2member_map_ else AgentStatus.UNKNOWN
                    raw_pane_id = data.get("pane_id")
                    pane_id = raw_pane_id if isinstance(raw_pane_id, str) and _ID_PATTERN.fullmatch(raw_pane_id) else None
                    # Commit confirmed agent fields before the independent pane read.
                    agent = AgentState("default", actual_kind, agent_status, pane_id, False)
                    agent_probe = ProbeStatus.AVAILABLE
                    status_known = isinstance(raw_status, str) and raw_status in AgentStatus._value2member_map_
                    if actual_kind is None or not status_known:
                        diagnostics.append("herdr_agent_metadata_partial")
                    if pane_id:
                        pane_response = self._run(("herdr", "pane", "get", pane_id))
                        pane_payload = self._json_object(pane_response)
                        pane_data = self._unwrap(pane_payload, "pane") if pane_payload else None
                        if pane_response.returncode == 0 and pane_data and pane_data.get("pane_id") == pane_id:
                            pane_probe = ProbeStatus.AVAILABLE
                            agent = replace(agent, pane_available=True, explicit_target=AgentTarget("default", pane_id))
                        elif self._error_code(pane_payload) == "pane_not_found":
                            pane_probe = ProbeStatus.MISSING
                            diagnostics.append("herdr_pane_missing")
                        else:
                            diagnostics.append("herdr_pane_probe_unreadable")
                    else:
                        diagnostics.append("herdr_pane_id_unavailable")
                else:
                    # A failed or unfamiliar agent response never means absent.
                    diagnostics.append("herdr_agent_probe_unreadable")
        capabilities = DefaultAgentCapabilities(
            omarchy_default_agent=configured, omarchy_probe=omarchy_probe,
            herdr_supported_kinds=supported, herdr_probe=herdr_probe,
            default_agent_exists=agent is not None, default_agent_probe=agent_probe,
            pane_id=agent.pane_id if agent else None,
            pane_available=agent.pane_available if agent else False, pane_probe=pane_probe,
            agent_status=agent.status if agent else AgentStatus.UNKNOWN,
            actual_kind=agent.kind if agent else None, diagnostics=tuple(diagnostics),
        )
        herdr = HerdrStatusSnapshot(installed, running, socket_available, supported,
                                   (agent,) if agent else (), checked_at=capabilities.checked_at,
                                   diagnostics=tuple(diagnostics), schema_probe=schema_probe)
        return capabilities, herdr
