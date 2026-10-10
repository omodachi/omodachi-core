"""LITE-1b C7: the front door - one `tailscale serve` mapping, and only ours.

Odomo needs a secure origin with a certificate a phone already
trusts, or there is no service worker and no push (LITE-0 §3). On a tailnet
with MagicDNS and HTTPS that is `tailscale serve`: Tailscale gets a Let's
Encrypt certificate for `<host>.<tailnet>.ts.net` and forwards to the daemon
on 127.0.0.1:8099. The installer adds that mapping when Tailscale is running,
and this module is the whole of how:

- read `tailscale serve status --json`;
- port 443 free: `tailscale serve --bg --yes --https=443 https+insecure://127.0.0.1:8099`;
- 443 taken by something of the user's: the same on 8443 (LITE-0 §4.5
  measured that serve accepts it);
- both taken: nothing, and the manual command is printed;
- never a path mount under someone else's port (the two would be one origin,
  and their page could read Odomo's credential and replace its worker), and
  never `tailscale serve reset`;
- every `tailscale` call has a hard timeout (the first `serve` on a tailnet
  without HTTPS waits for someone to enable it in the admin console);
- "access denied" means this user is not Tailscale's operator: the fix
  (`sudo tailscale set --operator=$USER`) is printed, never run - the
  panel's Install button has no root.

What was done is recorded in `~/.local/state/omodachi/tailscale-serve.json`:
the port, the target, the MagicDNS name, and whose mapping it is (`omodachi`
when this module made it; `user` when an identical one was already there,
which is then used but never removed). `remove` turns off a mapping only when
the record says it is ours *and* the live mapping still points exactly where
it did; anything changed since is somebody else's and stays.

The daemon reads the record (`current`) for the Host allow-list and for the
`odomo_url` it puts in the authenticated roads and claim - never on `/health`.

Standard library only: the installer imports this from the synced sources
under the system `python3 -I`, before the venv exists.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time

STATE = ".local/state/omodachi/tailscale-serve.json"
PORTS = (443, 8443)
DAEMON_PORT = 8099
TIMEOUT = 10.0
SERVE_TIMEOUT = 30.0
OPERATOR_FIX = "sudo tailscale set --operator=$USER"
_DNS = re.compile(r"[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63})*\.ts\.net")


def target(port: int = DAEMON_PORT) -> str:
    return f"https+insecure://127.0.0.1:{int(port)}"


def manual_command(port: int, daemon_port: int = DAEMON_PORT) -> str:
    return f"tailscale serve --bg --https={port} {target(daemon_port)}"


def odomo_url(dns_name: str, port: int) -> str:
    return f"https://{dns_name}{'' if port == 443 else ':' + str(port)}/odomo/"


def origin(dns_name: str, port: int) -> str:
    return f"https://{dns_name}{'' if port == 443 else ':' + str(port)}"


class Tailscale:
    """The local `tailscale` CLI, every call bounded."""

    def __init__(self, *, runner=subprocess.run, which=shutil.which):
        self.runner, self.which = runner, which

    def binary(self):
        return self.which("tailscale")

    def run(self, *arguments, timeout=TIMEOUT):
        binary = self.binary()
        if binary is None:
            return 127, "", "tailscale is not installed"
        try:
            result = self.runner([binary, *arguments], capture_output=True, text=True, timeout=timeout,
                                 stdin=subprocess.DEVNULL, check=False)
        except subprocess.TimeoutExpired as error:
            out = error.stdout.decode() if isinstance(error.stdout, bytes) else (error.stdout or "")
            return 124, out, "timed out"
        except OSError as error:
            return 126, "", str(error)
        return result.returncode, result.stdout or "", result.stderr or ""

    def json(self, *arguments):
        code, out, err = self.run(*arguments, "--json")
        if code != 0:
            return None, (out + "\n" + err)
        try:
            value = json.loads(out or "{}")
        except ValueError:
            return None, out
        return (value if isinstance(value, dict) else None), ""


def denied(text: str) -> bool:
    text = (text or "").lower()
    return "access denied" in text or "permission denied" in text or "--operator" in text


def dns_name(status: dict | None):
    value = ((status or {}).get("Self") or {}).get("DNSName")
    if not isinstance(value, str):
        return None
    value = value.strip().rstrip(".").lower()
    return value if _DNS.fullmatch(value) and len(value) <= 253 else None


def mapping(serve: dict | None, name: str, port: int):
    """What serve does on `port`: None (free), the `/` proxy target, or "other"."""
    serve = serve or {}
    tcp = serve.get("TCP") or {}
    web = (serve.get("Web") or {}).get(f"{name}:{port}") or {}
    handler = ((web.get("Handlers") or {}).get("/") or {})
    proxy = handler.get("Proxy") if isinstance(handler, dict) else None
    if str(port) not in tcp and not web:
        return None
    if isinstance(proxy, str) and (tcp.get(str(port)) or {}).get("HTTPS") is True:
        return proxy
    return "other"


# --- the record ---------------------------------------------------------------
def state_path(home: Path) -> Path:
    return Path(home) / STATE


def read_state(home: Path):
    path = state_path(home)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return None
            raw = stream.read(4097)
        value = json.loads(raw) if len(raw) <= 4096 else None
    except (OSError, ValueError):
        return None
    if (not isinstance(value, dict) or value.get("port") not in PORTS
            or not isinstance(value.get("target"), str) or not re.fullmatch(
                r"https\+insecure://127\.0\.0\.1:[0-9]{1,5}", value["target"])
            or not isinstance(value.get("dns_name"), str) or not _DNS.fullmatch(value["dns_name"])
            or value.get("owner") not in ("omodachi", "user")):
        return None
    return value


def write_state(home: Path, record: dict) -> None:
    path = state_path(home)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tailscale-serve-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(record, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def clear_state(home: Path) -> None:
    try:
        state_path(home).unlink()
    except FileNotFoundError:
        pass


class Current:
    """The daemon's view of the record: re-read only when the file changes."""

    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home is not None else Path.home()
        self._stamp = object()
        self._value = None

    def __call__(self):
        try:
            info = state_path(self.home).stat()
            stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
        except OSError:
            stamp = None
        if stamp != self._stamp:
            self._stamp, self._value = stamp, (read_state(self.home) if stamp else None)
        return self._value

    def url(self):
        record = self()
        return odomo_url(record["dns_name"], record["port"]) if record else None

    def origin(self):
        record = self()
        return origin(record["dns_name"], record["port"]) if record else None


# --- doing ---------------------------------------------------------------------
def _result(state, reason=None, **extra):
    return {"state": state, "reason": reason, **extra}


def ensure(home: Path | None = None, *, tailscale: Tailscale | None = None, daemon_port=DAEMON_PORT,
           log=print, clock=time.time) -> dict:
    """Make sure Odomo has a front door. Idempotent; never changes a mapping not ours."""
    home = Path(home) if home is not None else Path.home()
    tailscale = tailscale or Tailscale()
    wanted = target(daemon_port)
    if tailscale.binary() is None:
        return _result("off", "tailscale_not_installed")
    status, _ = tailscale.json("status")
    if not status or status.get("BackendState") != "Running":
        return _result("off", "tailscale_not_running")
    name = dns_name(status)
    if name is None:
        return _result("off", "no_magicdns_name")
    serve, text = tailscale.json("serve", "status")
    if serve is None:
        if denied(text):
            log(f"tailscale serve: this user is not Tailscale's operator, so Odomo "
                f"has no address yet. Run, in a terminal:\n    {OPERATOR_FIX}\n  then "
                f"`omodachi-host odomo enable` (or press Install again).")
            return _result("off", "operator_required", command=OPERATOR_FIX)
        return _result("off", "serve_status_unreadable")
    record = read_state(home)
    for port in PORTS:
        found = mapping(serve, name, port)
        if found == wanted:
            owner = "omodachi" if record and record.get("owner") == "omodachi" and \
                record.get("port") == port and record.get("target") == wanted else "user"
            write_state(home, {"version": 1, "port": port, "target": wanted, "dns_name": name,
                               "owner": owner, "created_at": (record or {}).get("created_at") or int(clock())})
            return _result("on", None, port=port, url=odomo_url(name, port), owner=owner)
        if found is not None:
            continue
        code, out, err = tailscale.run("serve", "--bg", "--yes", f"--https={port}", wanted,
                                       timeout=SERVE_TIMEOUT)
        if denied(out + err):
            log(f"tailscale serve: this user is not Tailscale's operator. Run, in a terminal:\n"
                f"    {OPERATOR_FIX}\n  then `omodachi-host odomo enable`.")
            return _result("off", "operator_required", command=OPERATOR_FIX)
        after, _ = tailscale.json("serve", "status")
        if mapping(after, name, port) != wanted:
            hint = " ".join(line.strip() for line in (out + "\n" + err).splitlines()
                            if "https://login.tailscale.com" in line)[:300]
            log(f"tailscale serve did not take the mapping on {port} (exit {code}). If this tailnet "
                f"does not have HTTPS certificates on, turn them on in the Tailscale admin console "
                f"(DNS > HTTPS Certificates)" + (f": {hint}" if hint else "") + f"; then run "
                f"`omodachi-host odomo enable`.")
            return _result("off", "serve_timeout" if code == 124 else "serve_failed", port=port,
                           command=manual_command(port, daemon_port))
        write_state(home, {"version": 1, "port": port, "target": wanted, "dns_name": name,
                           "owner": "omodachi", "created_at": int(clock())})
        return _result("on", None, port=port, url=odomo_url(name, port), owner="omodachi", created=True)
    log("tailscale serve: ports 443 and 8443 on this computer's tailnet name already serve something "
        "else, so Odomo was not given an address. To give it one on another port, run:\n"
        f"    {manual_command(10443, daemon_port)}")
    return _result("off", "ports_taken", command=manual_command(10443, daemon_port))


def remove(home: Path | None = None, *, tailscale: Tailscale | None = None, log=print) -> dict:
    """Turn off the mapping `ensure` made - only while it is still exactly that."""
    home = Path(home) if home is not None else Path.home()
    tailscale = tailscale or Tailscale()
    record = read_state(home)
    if record is None:
        clear_state(home)
        return {"removed": False, "reason": "no_record"}
    if record["owner"] != "omodachi":
        clear_state(home)
        return {"removed": False, "reason": "not_ours", "port": record["port"]}
    if tailscale.binary() is None:
        clear_state(home)
        return {"removed": False, "reason": "tailscale_not_installed", "port": record["port"]}
    serve, text = tailscale.json("serve", "status")
    if serve is None:
        reason = "operator_required" if denied(text) else "serve_status_unreadable"
        log(f"could not read `tailscale serve status` ({reason}); the mapping on port "
            f"{record['port']} was left as it is. Turn it off with:\n    "
            f"tailscale serve --https={record['port']} off")
        return {"removed": False, "reason": reason, "port": record["port"]}
    found = mapping(serve, record["dns_name"], record["port"])
    if found is None:
        clear_state(home)
        return {"removed": False, "reason": "already_gone", "port": record["port"]}
    if found != record["target"]:
        clear_state(home)
        log(f"left the tailscale serve mapping on port {record['port']} alone: it no longer points "
            f"at Omodachi, so it is somebody else's now")
        return {"removed": False, "reason": "changed", "port": record["port"]}
    code, out, err = tailscale.run("serve", f"--https={record['port']}", "off")
    after, _ = tailscale.json("serve", "status")
    if after is None or mapping(after, record["dns_name"], record["port"]) is not None:
        log(f"`tailscale serve --https={record['port']} off` did not take (exit {code}); run it yourself")
        return {"removed": False, "reason": "serve_off_failed", "port": record["port"]}
    clear_state(home)
    return {"removed": True, "port": record["port"]}


def status(home: Path | None = None, *, tailscale: Tailscale | None = None) -> dict:
    """`omodachi-host odomo status`: the record, and whether serve still agrees."""
    home = Path(home) if home is not None else Path.home()
    tailscale = tailscale or Tailscale()
    record = read_state(home)
    value = {"enabled": False, "odomo_url": None, "port": None, "owner": None, "dns_name": None,
             "reason": None, "command": None}
    if tailscale.binary() is None:
        return {**value, "reason": "tailscale_not_installed"}
    current, _ = tailscale.json("status")
    if not current or current.get("BackendState") != "Running":
        return {**value, "reason": "tailscale_not_running"}
    if record is None:
        return {**value, "reason": "not_enabled", "command": "omodachi-host odomo enable"}
    value.update(port=record["port"], owner=record["owner"], dns_name=record["dns_name"])
    serve, text = tailscale.json("serve", "status")
    if serve is None:
        if denied(text):
            return {**value, "reason": "operator_required", "command": OPERATOR_FIX}
        return {**value, "reason": "serve_status_unreadable"}
    if mapping(serve, record["dns_name"], record["port"]) != record["target"]:
        return {**value, "reason": "mapping_missing", "command": "omodachi-host odomo enable"}
    return {**value, "enabled": True, "odomo_url": odomo_url(record["dns_name"], record["port"])}
