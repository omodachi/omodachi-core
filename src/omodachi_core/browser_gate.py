"""LITE-1a: the gates a browser needs that the native app never did.

Odomo is a web page served by this daemon (`odomo.py`) and reached,
in practice, through `tailscale serve` on the host's `*.ts.net` name. A browser
differs from the native app in four ways that matter here, and each one has a
gate in this module:

C2  A browser follows DNS. A page on `evil.example` can rebind its name to
    this host and talk to it from the user's own browser. So anything a
    browser can reach - `/odomo/*`, and every request that carries `Origin` -
    must name this host in `Host`, and `Origin` must be exactly
    `<scheme>://<Host>`. LITE-0 §4.5 measured that `tailscale serve` passes
    the client's `Host` through untouched, so this cannot be left to it. A
    request with no `Origin` (the native app, `curl`, the plugin) is not a
    browser and is not asked.

C3  Through `tailscale serve` every request arrives from 127.0.0.1. Only for
    a loopback peer is the last `X-Forwarded-For` hop (which serve overwrites,
    LITE-0 §4.5) the real client, and `Tailscale-User-Login` who it is. They
    are used for the pairing card and the per-source pending bound - never
    for authorization, and never from any other peer, because :8099 is
    reachable directly on the LAN and the tailnet where both are forgeable.

C4  A browser holds a `web` credential. It is a role, not a fourth grant: the
    credential is the same 30-day device credential, and `WEB_ALLOWED` is the
    list of `/v1/*` routes it may call. Everything else - Remote, media, SSH,
    voice, approval keys, the clipboard, Herdr's `control`, `pane.focus` and
    `workspace focus` - is 403. Any request that carries
    `Origin` is held to the same list whatever credential it presents.

C5  A browser cannot put `Authorization` on `new WebSocket()`. It asks
    `POST /v1/ws-ticket {path}` for a 43-character ticket that lives 30 s,
    works once, and only on that path; `boundary` trades it for the
    credential it was minted for and strips it from the query before the
    route's own strict query check runs. Tickets are never logged (the
    daemon runs with `access_log=None`) and never stored in clear.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import shutil
import socket
import subprocess
import time
import unicodedata

# ── C4: what a web credential may call ────────────────────────────────────
# (methods, full-path pattern). HEAD is checked as GET. An allow-list, not a
# deny-list: a route added later is closed to the browser until it is listed.
_ID = r"[^/]{1,256}"
WEB_ALLOWED = (
    # menu, catalog and routes (LITE-1a L7)
    ({"GET"}, r"/v1/(state|capabilities|catalog)"),
    ({"POST"}, rf"/v1/actions/{_ID}:invoke"),
    ({"POST"}, r"/v1/workspaces/-?[0-9]+/select"),
    ({"POST"}, r"/v1/workspaces/relative/e[+-]1/select"),
    # keybindings
    ({"GET"}, r"/v1/shortcuts"),
    # notifications and the event stream
    ({"GET"}, r"/v1/events"),
    ({"POST"}, r"/v1/ws-ticket"),
    ({"GET"}, r"/v1/notifications"),
    ({"GET", "POST"}, r"/v1/notifications/dnd"),
    ({"POST"}, r"/v1/notifications/[0-9]{1,16}-[0-9]{1,12}:(invoke|dismiss)"),
    # LITE-1b C6: this browser's own push subscription and what it wants pushed
    ({"GET"}, r"/v1/push/vapid-public-key"),
    ({"POST", "DELETE"}, r"/v1/push/subscriptions"),
    ({"GET", "PUT"}, r"/v1/notifications/prefs"),
    # theme, fonts, icons
    ({"GET"}, r"/v1/theme"),
    ({"GET"}, r"/v1/theme/background"),
    ({"GET"}, r"/v1/fonts"),
    ({"GET"}, rf"/v1/fonts/{_ID}"),
    ({"GET"}, r"/v1/icons/[^/]{1,1024}"),
    # this device's own credential
    ({"GET"}, r"/v1/pairing/credential"),
    ({"POST"}, r"/v1/pairing/renew"),
    # Herdr (LITE-1a reads; ODOMO-2 the writes Odomo's Herdr panel makes,
    # which are exactly the App's since HERDR-5). Watching is `observe` only:
    # a browser never gets `control` (nor its `takeover`), never `pane.focus`
    # and never `workspace focus` - the two that move the computer's screen.
    ({"GET"}, r"/v1/herdr"),
    ({"GET"}, r"/v1/herdr/layout"),
    ({"GET"}, r"/v1/herdr/sessions"),
    ({"GET"}, r"/v1/herdr/operator-rows"),
    ({"GET"}, rf"/v1/herdr/panes/{_ID}/observe"),
    ({"POST"}, r"/v1/herdr/sessions"),
    ({"POST"}, r"/v1/herdr/sessions/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}/(select|start)"),
    # HERDR-6: Odomo's "New workspace", the App's; never with focus
    ({"GET"}, r"/v1/herdr/folders"),
    # HERDR-11: favourite folders, and a pane's latest agent reply.
    ({"POST"}, r"/v1/herdr/folders/favourites"),
    ({"GET"}, rf"/v1/herdr/panes/{_ID}/reply"),
    ({"POST"}, r"/v1/herdr/sessions/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}/workspaces"),
    ({"POST"}, rf"/v1/herdr/panes/{_ID}/(split|zoom|close|keys|text)"),
    ({"POST"}, rf"/v1/herdr/workspaces/{_ID}/tabs"),
    ({"DELETE"}, rf"/v1/herdr/workspaces/{_ID}/tabs/{_ID}"),
    # the default agent's chat (LITE-1a reads; ODOMO-2 what Odomo's Agent
    # panel sends, which is what the App's sends)
    ({"POST"}, r"/v1/agent/default:ensure"),
    ({"GET"}, r"/v1/agent/default/chat"),
    ({"GET"}, r"/v1/agent/default/chat/events"),
    ({"POST"}, r"/v1/agent/default/chat/(messages|steer|interrupt|mode)"),
    # AGENT-QUEUE-1: Odomo's queue and earlier messages, the App's.
    ({"POST"}, r"/v1/agent/default/chat/queue"),
    ({"POST"}, rf"/v1/agent/default/chat/queue/{_ID}:(send|edit|delete)"),
    ({"GET"}, r"/v1/agent/default/chat/history"),
    ({"GET"}, r"/v1/agent/default/chat/approvals"),
    ({"POST"}, rf"/v1/agent/default/chat/approvals/{_ID}"),
    ({"GET"}, r"/v1/agent/default/chat/usage"),
    ({"GET"}, r"/v1/agent/default/chat/commands"),
    ({"POST"}, rf"/v1/agent/default/chat/commands/{_ID}:execute"),
    ({"POST"}, r"/v1/agent/default/chat/handoff:(prepare|confirm)"),
    # AGENT-NEW-1: Odomo's "New conversation" is the App's
    ({"POST"}, r"/v1/agent/default/chat:new"),
    ({"GET"}, r"/v1/agent/default/models"),
    # ATTACH-1: a file for the Agent or a Herdr pane, and taking it back
    # before Send. Both panels Odomo has send them; nothing reads one back.
    ({"POST"}, r"/v1/uploads"),
    ({"DELETE"}, r"/v1/uploads/[0-9a-f]{32}"),
    # LINKS-1: "Open on the computer" for a path or an address the agent
    # showed. Odomo has no Remote, so it is the only way a browser gets a
    # file the agent made in front of somebody; the same mentioned / private /
    # never-run rules as the App's, and no byte of the file comes back.
    ({"POST"}, r"/v1/host/open"),
    # LINKS-1: this host's own names and addresses, so a `localhost` link an
    # agent printed can be rewritten to the road the browser came in on. A
    # web device already received exactly these in its pairing claim.
    ({"GET"}, r"/v1/host/roads"),
)
_WEB_ALLOWED = tuple((methods, re.compile(pattern + r"\Z")) for methods, pattern in WEB_ALLOWED)


def web_allows(method: str, path: str) -> bool:
    method = "GET" if method == "HEAD" else method
    return any(method in methods and pattern.match(path) for methods, pattern in _WEB_ALLOWED)


# ── C5: WebSocket tickets ─────────────────────────────────────────────────
# The three sockets a browser could ever open. Whether *this* caller may open
# the one it names is still `web_allows` (a web credential gets no
# `control`), checked when the ticket is minted and again when it is spent.
WS_TICKET_PATHS = (
    re.compile(r"/v1/events\Z"),
    re.compile(r"/v1/agent/default/chat/events\Z"),
    re.compile(rf"/v1/herdr/panes/{_ID}/(observe|control)\Z"),
)
_TICKET = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def ticket_path(path) -> bool:
    return isinstance(path, str) and len(path) <= 512 and any(p.match(path) for p in WS_TICKET_PATHS)


class TicketRefused(ValueError):
    """The ticket is unknown, spent, expired or for another path."""


class WsTickets:
    TTL = 30.0
    LIMIT = 256
    PER_DEVICE = 16

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._rows: dict[str, tuple[str, str, str, float]] = {}

    @staticmethod
    def _key(ticket: str) -> str:
        return hashlib.sha256(ticket.encode()).hexdigest()

    def _prune(self, now):
        for key in [key for key, row in self._rows.items() if row[3] <= now]:
            del self._rows[key]

    def mint(self, device: str, token: str, path: str) -> dict:
        now = self._clock()
        self._prune(now)
        if (len(self._rows) >= self.LIMIT
                or sum(1 for row in self._rows.values() if row[0] == device) >= self.PER_DEVICE):
            from .service import ServiceError
            raise ServiceError("ws_ticket_capacity", status=429)
        ticket = secrets.token_urlsafe(32)
        self._rows[self._key(ticket)] = (device, token, path, now + self.TTL)
        return {"ticket": ticket, "path": path, "expires_in": int(self.TTL)}

    def take(self, ticket, path: str) -> tuple[str, str]:
        """Spend a ticket. It is gone after this call whatever the answer."""
        if not isinstance(ticket, str) or not _TICKET.fullmatch(ticket):
            raise TicketRefused("ws_ticket_invalid")
        row = self._rows.pop(self._key(ticket), None)
        now = self._clock()
        self._prune(now)
        if row is None or row[3] <= now or not hmac.compare_digest(row[2], path):
            raise TicketRefused("ws_ticket_invalid")
        return row[0], row[1]

    def __len__(self):
        return len(self._rows)


# ── C2: which names are this host ─────────────────────────────────────────
def tailscale_dns_name(*, timeout=3.0):
    """`Self.DNSName` from the local tailscaled, lower case, no trailing dot."""
    binary = shutil.which("tailscale")
    if binary is None:
        return None
    try:
        result = subprocess.run([binary, "status", "--json"], capture_output=True, text=True,
                                timeout=timeout, check=False)
        if result.returncode != 0:
            return None
        value = (json.loads(result.stdout or "{}").get("Self") or {}).get("DNSName")
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None
    if not isinstance(value, str):
        return None
    value = value.strip().rstrip(".").lower()
    return value if 1 <= len(value) <= 253 and value.endswith(".ts.net") else None


def split_host(value):
    """`Host` → (name, port|None), or None when it is not a plain host[:port]."""
    if not isinstance(value, str) or not 1 <= len(value) <= 300:
        return None
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        name, rest = value[1:end], value[end + 1:]
        if rest and not rest.startswith(":"):
            return None
        port = rest[1:] if rest else None
    elif value.count(":") == 1:
        name, port = value.split(":")
    elif ":" in value:
        return None
    else:
        name, port = value, None
    name = name.rstrip(".")
    if port is not None:
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            return None
        port = int(port)
    if not name or not re.fullmatch(r"[a-z0-9.:_-]+", name):
        return None
    return name, port


def _tailnet_dns_name(identity):
    """The MagicDNS name: the daemon's cached tailnet reading when it keeps
    one (TAIL-2's `TailnetMonitor`, refreshed every 10 s off the loop), else
    one `tailscale status --json` of our own (tests, a bare service)."""
    if callable(getattr(identity, "tailnet", None)):
        reading = identity.tailnet_reading()
        if isinstance(reading, dict) and reading.get("running") is True:
            value = reading.get("dns_name")
            return value if isinstance(value, str) and value else None
        return None
    return tailscale_dns_name()


class HostNames:
    """The names a browser may use to reach this host, refreshed lazily.

    The answer comes from what core already knows about itself (HostIdentity:
    the host name, `<name>.local`, its addresses) plus the tailnet's MagicDNS
    name from the local tailscaled, plus loopback. It is re-read every
    `REFRESH` seconds, and once early when a name is refused, so Tailscale
    coming up after the daemon is not a restart - but a stream of unknown
    names cannot make it run a subprocess more than once every `MISS_GAP`.
    """
    REFRESH = 60.0
    MISS_GAP = 5.0
    SERVE_PORTS = (443, 8443)

    def __init__(self, service, *, clock=time.monotonic):
        self.service, self._clock = service, clock
        self._names: frozenset[str] = frozenset()
        self._read_at = None
        self._lock = asyncio.Lock()

    def read(self) -> frozenset[str]:
        names = {"localhost", "127.0.0.1", "::1"}
        identity = getattr(self.service, "host_identity", None)
        for value in (getattr(identity, "host_name", None), _hostname()):
            if isinstance(value, str) and value:
                names.add(value.lower())
                names.add(value.lower().split(".")[0] + ".local")
        addresses = list(getattr(identity, "addresses", None) or []) or _addresses()
        names.update(str(value).lower() for value in addresses)
        tailnet = _tailnet_dns_name(identity)
        if tailnet:
            names.add(tailnet)
        # LITE-1b C7: the name the installer's `tailscale serve` mapping is on.
        front = getattr(self.service, "odomo_front_door", None)
        if callable(front):
            try:
                record = front()
            except Exception:
                record = None
            if isinstance(record, dict) and isinstance(record.get("dns_name"), str):
                names.add(record["dns_name"].lower())
        extra = getattr(self.service, "odomo_extra_host_names", None)
        if extra:
            names.update(str(value).lower() for value in extra)
        return frozenset(names)

    async def refresh(self, *, miss=False):
        async with self._lock:
            now = self._clock()
            if self._read_at is not None:
                age = now - self._read_at
                if age < (self.MISS_GAP if miss else self.REFRESH):
                    return
            self._names = await asyncio.to_thread(self.read)
            self._read_at = self._clock()

    async def allows(self, host_header, local_port) -> bool:
        parsed = split_host(host_header)
        if parsed is None:
            return False
        name, port = parsed
        if port is not None and port != local_port and port not in self.SERVE_PORTS:
            return False
        await self.refresh()
        if name in self._names:
            return True
        await self.refresh(miss=True)
        return name in self._names


def _addresses():
    from .host_identity import host_addresses
    return host_addresses()


def _hostname():
    try:
        return socket.gethostname()
    except OSError:
        return None


def local_port(request):
    try:
        value = request.transport.get_extra_info("sockname") if request.transport else None
        return int(value[1]) if value else None
    except (TypeError, ValueError, IndexError, AttributeError):
        return None


def origin_matches(request, origin) -> bool:
    host = request.headers.get("Host")
    if not isinstance(origin, str) or not isinstance(host, str):
        return False
    return hmac.compare_digest(origin.strip().lower().encode(),
                               f"{request.scheme}://{host.strip().lower()}".encode())


# ── C3: who is behind tailscale serve ─────────────────────────────────────
def _loopback(value) -> bool:
    try:
        address = ipaddress.ip_address(str(value).split("%", 1)[0])
    except (TypeError, ValueError):
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def _clean_login(value):
    if not isinstance(value, str):
        return None
    text = "".join(c for c in value if c.isprintable()
                   and unicodedata.category(c) not in {"Cf", "Co", "Cn", "Cs"}).strip()
    return text[:128] or None


def forwarded_peer(request):
    """(address to show and bucket by, {login}|None). Display only, never authorization.

    A loopback peer that says who it forwarded for is `tailscale serve` (or
    another process on this machine, which is already inside the boundary).
    Any other peer's forwarding headers are ignored: :8099 is directly
    reachable and they are free text there.
    """
    peer = request.remote
    if not _loopback(peer):
        return peer, None
    values = request.headers.getall("X-Forwarded-For", [])
    if not values:
        return peer, None
    last = values[-1].split(",")[-1].strip()
    try:
        address = str(ipaddress.ip_address(last.split("%", 1)[0]))
    except ValueError:
        return peer, None
    # What serve vouched for. The road (`via.kind`) and the peer's name are
    # not decided here: network.py reads them for this address the same way
    # as for a direct request (NEXT-1, one `via` for both clients).
    return address, {"login": _clean_login(request.headers.get("Tailscale-User-Login"))}
