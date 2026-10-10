"""TAIL-2: what this host looks like from a Tailscale tailnet, read, never changed.

Everything here comes from the local `tailscale` CLI (`status --json`,
`debug prefs`, `whois --json`) and from `ip -j`. Nothing is ever set: the
`--accept-routes` fix is shown to the person at the computer, not run.

TAIL-1 measured why each reading matters:

- the claim and `/health` must carry the MagicDNS name, because an iOS app can
  only reach a tailnet host by that name (ATS), never by its 100.x address;
- Tailscale SSH (`RunSSH`) answers port 22 on the tailnet itself, so
  Omodachi's key, its approval and a revoke here do not apply there;
- `--accept-routes` (Omarchy's default) can hand this computer's own LAN to a
  subnet router on the tailnet (table 52), which breaks LAN pairing;
- Tailscale's `ts-input` chain accepts everything on `tailscale0` before ufw
  sees it, so over the tailnet the tailnet ACL decides, not ufw;
- a relayed (DERP) peer moves VNC at a few MiB/s with 1-2 s of lag.

A reading is cheap but not free (two subprocesses), and `/health` answers on
the event loop, so the daemon keeps one cached reading and refreshes it on a
worker thread (`TailnetMonitor`).
"""
from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
import threading
import time

TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
TAILSCALE_INTERFACE = "tailscale0"
# The table Tailscale installs its routes in on Linux.
TAILSCALE_ROUTE_TABLE = "52"
TIMEOUT = 3.0


def _ip(value):
    try:
        return ipaddress.ip_address(str(value).split("%", 1)[0])
    except (TypeError, ValueError):
        return None


def is_tailnet_address(value) -> bool:
    """True for Tailscale's CGNAT range and its ULA prefix, nothing else."""
    address = _ip(value)
    if address is None:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address in (TAILNET_V4 if address.version == 4 else TAILNET_V6)


def address_kind(value) -> str:
    """How a peer reached us: `lan`, `tailnet`, `loopback` or `other`."""
    address = _ip(value)
    if address is None:
        return "other"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        return "loopback"
    if is_tailnet_address(address):
        return "tailnet"
    if address.is_private or address.is_link_local:
        return "lan"
    return "other"


def _run_json(argv, *, timeout=TIMEOUT, runner=subprocess.run):
    """The JSON a read-only command printed, or None for any failure."""
    try:
        result = runner(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    try:
        value = json.loads(result.stdout or "")
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def tailscale_binary(which=shutil.which):
    return which("tailscale")


def status_json(*, runner=subprocess.run, which=shutil.which):
    binary = tailscale_binary(which)
    if binary is None:
        return None
    return _run_json([binary, "status", "--json"], runner=runner)


def prefs_json(*, runner=subprocess.run, which=shutil.which):
    binary = tailscale_binary(which)
    if binary is None:
        return None
    return _run_json([binary, "debug", "prefs"], runner=runner)


def _addresses(values) -> list[str]:
    result = []
    for value in values if isinstance(values, list) else []:
        address = _ip(value)
        if address is not None and is_tailnet_address(address) and str(address) not in result:
            result.append(str(address))
    return result[:8]


def _dns_name(value):
    if not isinstance(value, str):
        return None
    name = value.strip().rstrip(".").lower()
    if not 1 <= len(name) <= 253 or not name.endswith(".ts.net"):
        return None
    if any(not part or len(part) > 63 or not all(c.isalnum() or c == "-" for c in part)
           for part in name.split(".")):
        return None
    return name


def reading(*, runner=subprocess.run, which=shutil.which) -> dict:
    """One reading of this node. `running` is BackendState == Running only.

    The fields are deliberately few. Other nodes' names and addresses are
    never collected here: what another node is called is the business of the
    one request that came from it (`whois`, `peer_path`).
    """
    installed = tailscale_binary(which) is not None
    value = {"installed": installed, "backend_state": None, "running": False,
             "dns_name": None, "ips": [], "ssh": None, "accept_routes": None,
             "netfilter_mode": None}
    if not installed:
        return value
    status = status_json(runner=runner, which=which)
    if status is None:
        return value
    state = status.get("BackendState")
    value["backend_state"] = state if isinstance(state, str) and len(state) <= 32 else None
    if value["backend_state"] != "Running":
        return value
    own = status.get("Self") if isinstance(status.get("Self"), dict) else {}
    value.update(running=True, dns_name=_dns_name(own.get("DNSName")),
                 ips=_addresses(own.get("TailscaleIPs") or status.get("TailscaleIPs")))
    prefs = prefs_json(runner=runner, which=which)
    if prefs is not None:
        value["ssh"] = prefs.get("RunSSH") is True
        value["accept_routes"] = prefs.get("RouteAll") is True
        mode = prefs.get("NetfilterMode")
        value["netfilter_mode"] = mode if isinstance(mode, int) and 0 <= mode <= 2 else None
    return value


def public(value) -> dict | None:
    """`/health.tailnet`: present only while the node is Running."""
    if not isinstance(value, dict) or value.get("running") is not True:
        return None
    return {"dns_name": value.get("dns_name"), "ips": list(value.get("ips") or [])}


def _bounded(value, limit=64):
    if not isinstance(value, str):
        return None
    text = "".join(c for c in value if c.isprintable()).strip()
    return text[:limit] or None


def whois(address, *, runner=subprocess.run, which=shutil.which) -> dict:
    """Who a tailnet peer is, for the pairing card. Never authorization.

    `peer_name` is the node's short name (`iphone-a`), `login` the owner's
    login. Both are read from this computer's own tailscaled, which is the
    only party that can vouch for a 100.x source; either can be None.
    """
    result = {"kind": address_kind(address), "peer_name": None, "login": None}
    if result["kind"] != "tailnet":
        return result
    binary = tailscale_binary(which)
    if binary is None:
        return result
    value = _run_json([binary, "whois", "--json", str(_ip(address))], runner=runner)
    if value is None:
        return result
    node = value.get("Node") if isinstance(value.get("Node"), dict) else {}
    user = value.get("UserProfile") if isinstance(value.get("UserProfile"), dict) else {}
    hostinfo = node.get("Hostinfo") if isinstance(node.get("Hostinfo"), dict) else {}
    name = node.get("ComputedName") or hostinfo.get("Hostname")
    if not name and isinstance(node.get("Name"), str):
        name = node["Name"].split(".", 1)[0]
    result["peer_name"] = _bounded(name)
    login = user.get("LoginName")
    # A tagged node has no person behind it; tailscaled says so with this login.
    result["login"] = None if login == "tagged-devices" else _bounded(login, 128)
    return result


def peer_path(status, address) -> dict:
    """{kind, relayed} for the peer at `address`, from one `status --json`.

    `relayed` is True when Tailscale has no direct path to that peer (no
    `CurAddr`) and is carrying it over a DERP relay, False on a direct path,
    None when the peer or the reading is unknown.
    """
    kind = address_kind(address)
    result = {"kind": kind, "relayed": None}
    if kind != "tailnet" or not isinstance(status, dict):
        return result
    target = _ip(address)
    peers = status.get("Peer") if isinstance(status.get("Peer"), dict) else {}
    for peer in peers.values():
        if not isinstance(peer, dict):
            continue
        if not any(_ip(value) == target for value in peer.get("TailscaleIPs") or []):
            continue
        direct = isinstance(peer.get("CurAddr"), str) and bool(peer["CurAddr"])
        relay = isinstance(peer.get("Relay"), str) and bool(peer["Relay"])
        result["relayed"] = False if direct else (True if relay else None)
        return result
    return result


def _ip_json(argv, *, runner=subprocess.run):
    try:
        result = runner(argv, capture_output=True, text=True, timeout=TIMEOUT, check=False)
        value = json.loads(result.stdout or "[]") if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


def lan_prefixes(*, runner=subprocess.run) -> list[str]:
    """This computer's own LAN networks (not loopback, not the tailnet)."""
    result = []
    for link in _ip_json(["ip", "-j", "addr"], runner=runner):
        if not isinstance(link, dict) or link.get("ifname") == TAILSCALE_INTERFACE:
            continue
        for entry in link.get("addr_info") or []:
            try:
                network = ipaddress.ip_interface(f"{entry['local']}/{entry['prefixlen']}").network
            except (KeyError, TypeError, ValueError):
                continue
            if (network.is_loopback or network.network_address.is_link_local
                    or is_tailnet_address(network.network_address)):
                continue
            if str(network) not in result:
                result.append(str(network))
    return result[:16]


def captured_lan_routes(*, runner=subprocess.run, prefixes=None) -> list[str]:
    """Routes in Tailscale's table that cover this computer's own LAN.

    TAIL-1 §2.2: with `--accept-routes`, a subnet router that advertises the
    network this computer sits on takes that network over (`ip route get` of a
    LAN neighbour answers `dev tailscale0 table 52`), so replies to a phone on
    the same Wi-Fi go out through the tailnet and LAN pairing breaks.
    """
    own = [ipaddress.ip_network(value) for value in (lan_prefixes(runner=runner)
                                                       if prefixes is None else prefixes)]
    captured = []
    for family in ("-4", "-6"):
        for route in _ip_json(["ip", "-j", family, "route", "show", "table", TAILSCALE_ROUTE_TABLE],
                              runner=runner):
            destination = route.get("dst") if isinstance(route, dict) else None
            if destination in (None, "default"):
                continue
            try:
                network = ipaddress.ip_network(destination, strict=False)
            except ValueError:
                continue
            if is_tailnet_address(network.network_address):
                continue
            if any(network.version == mine.version and network.overlaps(mine) for mine in own):
                if str(network) not in captured:
                    captured.append(str(network))
    return captured[:16]


ACCEPT_ROUTES_FIX = "tailscale set --accept-routes=false"
FIREWALL_TAILNET = ("Over Tailscale, ufw does not decide who reaches this computer: Tailscale's "
                    "ts-input rule accepts everything arriving on tailscale0 before ufw sees it, so "
                    "every listening port (8099, 22, the Sunshine ports, 47990) is open to whoever "
                    "your tailnet's access rules (ACLs) allow.")
SSH_WARNING = ("Tailscale SSH is on: port 22 over the tailnet is answered by Tailscale, not by "
               "sshd. Omodachi's SSH key, its device approval and revoking a device here do not "
               "apply to it - your tailnet's SSH rules do.")


def host_report(*, runner=subprocess.run, which=shutil.which) -> dict:
    """`omodachi-host tailnet`: the reading plus what the panel should say."""
    value = reading(runner=runner, which=which)
    captured = []
    if value["running"] and value["accept_routes"]:
        captured = captured_lan_routes(runner=runner)
    warnings = []
    if value["running"]:
        warnings.append({"id": "tailnet_firewall", "text": FIREWALL_TAILNET})
    if value["running"] and value["ssh"]:
        warnings.append({"id": "tailscale_ssh", "text": SSH_WARNING})
    if captured:
        warnings.append({"id": "accept_routes_lan", "text": (
            "Tailscale accepts a route that covers this computer's own network ("
            + ", ".join(captured) + "), so a phone on the same Wi-Fi may not be able to pair "
            "or connect here. To fix it, run: " + ACCEPT_ROUTES_FIX),
            "command": ACCEPT_ROUTES_FIX})
    return {**value, "lan_routes_captured": captured, "warnings": warnings}


class TailnetMonitor:
    """One cached reading, refreshed off the event loop."""

    def __init__(self, reader=reading, clock=time.monotonic):
        self._reader, self._clock = reader, clock
        self._lock = threading.Lock()
        self._value = None
        self._at = None

    def refresh(self) -> dict:
        try:
            value = self._reader()
        except Exception:
            value = None
        with self._lock:
            self._value, self._at = value, self._clock()
        return value

    def current(self) -> dict | None:
        with self._lock:
            return self._value
