#!/usr/bin/env python3
"""Manage the isolated loopback Simulator fixture daemon.

This manager never touches a user's Omarchy configuration, never prints device
credentials, and never treats an unverified PID as the managed daemon.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
METADATA = ROOT / ".runtime/simulator-backend.json"
_STARTUP_TOKEN_ENV = "OMODACHI_SIMULATOR_STARTUP_TOKEN"
_READY_TIMEOUT = 8.0
_STOP_TIMEOUT = 3.0


def read_meta(path: Path = METADATA) -> dict:
    if not path.exists():
        raise SystemExit(f"missing {path}; create a Simulator fixture first")
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid simulator metadata: {path}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"invalid simulator metadata: {path}")
    return data


def _pid(pid) -> int | None:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def pid_alive(pid) -> bool:
    value = _pid(pid)
    if value is None:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Existence without identity is not enough for a manager to signal it.
        return True
    except OSError:
        return False
    return True


def _linux_start_time(pid: int):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
        ticks = int(fields[21])
        clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        boot = next(line for line in Path("/proc/stat").read_text().splitlines()
                    if line.startswith("btime "))
        return float(boot.split()[1]) + ticks / clock_ticks
    except (OSError, StopIteration, ValueError, IndexError):
        return None


def process_start_time(pid):
    """Return a stable process creation marker on Linux and macOS."""
    value = _pid(pid)
    if value is None:
        return None
    linux = _linux_start_time(value)
    if linux is not None:
        return linux
    try:
        result = subprocess.run(["ps", "-p", str(value), "-o", "lstart="],
                                capture_output=True, text=True, timeout=1, check=False)
        marker = result.stdout.strip()
        return marker or None
    except (OSError, subprocess.SubprocessError):
        return None


def process_command(pid) -> str | None:
    value = _pid(pid)
    if value is None:
        return None
    try:
        raw = Path(f"/proc/{value}/cmdline").read_bytes()
        if raw:
            return raw.replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        pass
    try:
        result = subprocess.run(["ps", "-p", str(value), "-o", "command="],
                                capture_output=True, text=True, timeout=1, check=False)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def process_environment_marker(pid) -> str | None:
    value = _pid(pid)
    if value is None:
        return None
    try:
        raw = Path(f"/proc/{value}/environ").read_bytes()
        prefix = (_STARTUP_TOKEN_ENV + "=").encode()
        for field in raw.split(b"\0"):
            if field.startswith(prefix):
                return field[len(prefix):].decode("ascii")
    except OSError:
        pass
    try:
        result = subprocess.run(["ps", "eww", "-p", str(value), "-o", "command="],
                                capture_output=True, text=True, timeout=1, check=False)
        match = re.search(r"(?:^|\s)" + re.escape(_STARTUP_TOKEN_ENV) + r"=([A-Za-z0-9_-]+)", result.stdout)
        return match.group(1) if match else None
    except (OSError, subprocess.SubprocessError):
        return None


def process_identity(pid) -> dict[str, object] | None:
    value = _pid(pid)
    if value is None or not pid_alive(value):
        return None
    return {"pid": value, "start_time": process_start_time(value),
            "command": process_command(value), "startup_token": process_environment_marker(value)}


def managed_process(meta: dict) -> bool:
    """Require PID, process start marker, daemon command, and startup token match."""
    identity = process_identity(meta.get("pid"))
    if identity is None:
        return False
    expected_start = meta.get("pid_start_time")
    expected_token = meta.get("launch_token")
    if expected_start is None or expected_token is None:
        return False
    if str(identity.get("start_time")) != str(expected_start):
        return False
    command = identity.get("command") or ""
    if "omodachi_core.cli" not in command or "--demo" not in command:
        return False
    return identity.get("startup_token") == expected_token


def _read_secret(path: str | Path) -> str:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValueError("credential file must not be a symlink")
    token = candidate.read_text().strip()
    if not token or len(token) > 4096 or token.count(".") != 1:
        raise ValueError("invalid credential file")
    return token


def _endpoint(meta: dict, ready: dict) -> str:
    host = ready.get("host") or meta.get("endpoint", "").split("://")[-1].split(":")[0]
    port = ready.get("port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        existing = str(meta.get("endpoint", ""))
        if "://" in existing:
            return existing.rstrip("/")
        raise ValueError("daemon ready frame has no valid port")
    scheme = "https" if ready.get("tls", True) else "http"
    return f"{scheme}://{host}:{port}"


def _ssl_context(meta: dict):
    ca_file = meta.get("ca_file")
    if ca_file and Path(ca_file).exists():
        return ssl.create_default_context(cafile=ca_file)
    return ssl.create_default_context()


def _get_json(url: str, *, context=None, token: str | None = None) -> dict:
    headers = {"Authorization": "Bearer " + token} if token else {}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, context=context, timeout=3) as response:
        value = json.loads(response.read(1024 * 1024))
    if not isinstance(value, dict):
        raise ValueError("backend response was not an object")
    return value


def verify_backend(meta: dict, ready: dict, *, require_second_device: bool = False) -> dict:
    """Check health and an authenticated state snapshot before metadata commit."""
    endpoint = _endpoint(meta, ready)
    context = _ssl_context(meta) if endpoint.startswith("https://") else None
    health = _get_json(endpoint + "/health", context=context)
    if health.get("service") != "omodachid":
        raise ValueError("backend health identity mismatch")
    credentials = meta.get("credential_files", {})
    phone_path = credentials.get("iphone-simulator")
    if not phone_path:
        raise ValueError("missing simulator credential")
    phone_token = _read_secret(phone_path)
    state = _get_json(endpoint + "/v1/state", context=context, token=phone_token)
    instance_id = state.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        raise ValueError("backend state has no instance identity")
    if ready.get("instance_id") and ready["instance_id"] != instance_id:
        raise ValueError("ready/state instance identity mismatch")
    if require_second_device or credentials.get("ipad-simulator"):
        tablet_path = credentials.get("ipad-simulator")
        if not tablet_path:
            raise ValueError("missing second simulator credential")
        _get_json(endpoint + "/v1/state", context=context, token=_read_secret(tablet_path))
    return {"endpoint": endpoint, "instance_id": instance_id,
            "catalog_revision": state.get("catalog", {}).get("revision")}


def refresh_remote_metadata(meta: dict, *, require_second_device: bool = False) -> bool:
    try:
        verified = verify_backend(meta, {"host": meta.get("endpoint", "").split("://")[-1].split(":")[0],
                                         "port": int(meta.get("endpoint", "").rsplit(":", 1)[-1]),
                                         "tls": str(meta.get("endpoint", "")).startswith("https://")},
                                 require_second_device=require_second_device)
    except (OSError, ValueError, KeyError, urllib.error.URLError, json.JSONDecodeError):
        return False
    meta.update(verified)
    _write_meta(meta)
    return True


def _write_meta(meta: dict, path: Path = METADATA) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(meta, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def status(path: Path = METADATA) -> dict:
    meta = read_meta(path)
    running = managed_process(meta)
    if running:
        # A live PID is only "running" after the current endpoint answers and
        # reports the same authenticated daemon instance.
        running = refresh_remote_metadata(meta)
    result = {"running": running, "pid": meta.get("pid"), "endpoint": meta.get("endpoint"),
              "instance_id": meta.get("instance_id"), "contract_revision": meta.get("contract_revision")}
    print(json.dumps(result))
    return result


def stop(path: Path = METADATA) -> dict:
    meta = read_meta(path)
    pid = _pid(meta.get("pid"))
    if not pid or not managed_process(meta):
        result = {"stopped": True, "pid": meta.get("pid"), "signalled": False,
                  "reason": "not_managed"}
        print(json.dumps(result))
        return result
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + _STOP_TIMEOUT
    while time.monotonic() < deadline and managed_process(meta):
        time.sleep(0.05)
    if managed_process(meta):
        os.kill(pid, signal.SIGKILL)
    result = {"stopped": True, "pid": pid, "signalled": True}
    print(json.dumps(result))
    return result


def _launch_token() -> str:
    return uuid.uuid4().hex


def start(path: Path = METADATA) -> dict:
    meta = read_meta(path)
    directory = Path(meta["directory"])
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if managed_process(meta):
        result = {"running": True, "pid": meta.get("pid"), "endpoint": meta.get("endpoint"),
                  "instance_id": meta.get("instance_id")}
        print(json.dumps(result))
        return result
    launch_token = _launch_token()
    log = directory / f"daemon-{launch_token}.log"
    marker = {"startup_marker": launch_token, "started_at": time.time()}
    log.write_text(json.dumps(marker, separators=(",", ":")) + "\n")
    log.chmod(0o600)
    command = [sys.executable, "-m", "omodachi_core.cli", "--demo",
               "--socket", str(directory / "hub.sock"), "--secret-file", str(directory / "device.secret"),
               "--listen", "127.0.0.1", "--port", "58142",
               "--tls-cert", str(directory / "development-ca.pem"), "--tls-key", str(directory / "server-key.pem"),
               "--lease-ttl", "30"]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONUNBUFFERED"] = "1"
    environment[_STARTUP_TOKEN_ENV] = launch_token
    with log.open("ab") as stream:
        process = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=stream, start_new_session=True, close_fds=True)
    identity = process_identity(process.pid)
    if identity is None:
        raise SystemExit("backend process disappeared before startup verification")
    if (identity.get("startup_token") != launch_token
            or "omodachi_core.cli" not in (identity.get("command") or "")
            or "--demo" not in (identity.get("command") or "")):
        with contextlib.suppress(OSError):
            process.send_signal(signal.SIGTERM)
        raise SystemExit("backend process identity marker mismatch")
    deadline = time.monotonic() + _READY_TIMEOUT
    ready = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"backend exited before ready; inspect {log}")
        try:
            lines = log.read_text(errors="replace").splitlines()
        except OSError:
            lines = []
        for line in reversed(lines):
            if line.startswith("{\"ready\""):
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if candidate.get("ready") is True:
                    ready = candidate
                    break
        if ready:
            break
        time.sleep(0.05)
    if not ready or process.poll() is not None:
        raise SystemExit(f"backend did not become ready; inspect {log}")
    if (ready.get("startup_token") != launch_token or ready.get("pid") != process.pid):
        with contextlib.suppress(OSError):
            process.send_signal(signal.SIGTERM)
        raise SystemExit("backend startup marker or pid mismatch")
    current = dict(meta)
    current.update({"pid": process.pid, "pid_start_time": identity.get("start_time"),
                    "launch_token": launch_token, "exec_mode": "detached",
                    "log_file": str(log), "exec_session_id": None})
    # Build endpoint from this ready frame and validate health/state before commit.
    current["endpoint"] = _endpoint(current, ready)
    try:
        verified = verify_backend(current, ready)
    except Exception as exc:
        with contextlib.suppress(OSError):
            process.send_signal(signal.SIGTERM)
        raise SystemExit(f"backend startup verification failed; inspect {log}") from exc
    current.update(verified)
    current["contract_revision"] = ready.get("contract_revision", current.get("contract_revision"))
    _write_meta(current, path)
    result = {"running": True, "pid": process.pid, "endpoint": current["endpoint"],
              "instance_id": current.get("instance_id"), "contract_revision": current.get("contract_revision")}
    print(json.dumps(result))
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("start", "stop", "status"))
    args = parser.parse_args(argv)
    {"start": start, "stop": stop, "status": status}[args.command]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
