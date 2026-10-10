"""Local-approved companion pairing: one approval, three grants.

The security boundary is step 3, not step 1: approval is a same-UID local
decision and the network can never make it. So an invitation is not what keeps
a stranger out, and PAIR-2 stops asking for one by default - `pairing_mode`
`open` lets a request become `pending` on its own, and `invite` is the locked
mode that still demands the 43-character one-shot.

Invitations and claim secrets are random 256-bit capabilities, stored hashed.
Only the same-UID local helper can open/approve/reject invitations. The network
can request and redeem after approval, never approve itself or revoke devices.

An invitation-less request is the one thing on this surface an unauthenticated
peer can create, so it is bounded three ways: one pending request per device
(a retry replaces its predecessor instead of stacking), two per source address,
and the store-wide 64. None of them can approve anything.

A request may carry the companion's own SSH public key. It is a public key, so
carrying it costs nothing: it is validated on arrival, shown to whoever decides,
and written to `authorized_keys` by the same local Approve that issues the
device credential and the Sunshine streaming grant. That is what the one
approval means - screen, terminal and agent - and the claim says which of the
three actually landed in `grants` rather than leaving the client to guess.
"""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
import unicodedata

from .auth import DeviceAuthenticator, _private_open, _registry_lock, _DEVICE_ID
from .protocol import PAIRING_MODES, PLUGIN_DEVICE_ID
from .service import ServiceError
from .ssh_keys import SshKeyError, fingerprint, parse_public_key


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise ServiceError("pairing_invalid_secret", status=403)
    return hashlib.sha256(value.encode()).hexdigest()


# What one approval can hand over. `companion` is the device credential this
# store issues itself; `media` and `ssh` are landed by the local approver and
# recorded here so the claim can tell the client which of the three it has.
EMPTY_GRANTS = {"companion": False, "media": False, "ssh": False}

# RELEASE-9 (B4). The longest name a request may put on the approval card and
# in the notification title.
DEVICE_NAME_LIMIT = 48


def clean_device_name(value):
    """The requester's name, fit for a notification title, or None.

    The name is the one thing an unauthenticated peer chooses that a person
    reads before approving, so it may not carry anything that changes how the
    rest of the card reads: control characters, newlines, bidi overrides and
    other invisible formatting are removed, runs of whitespace become one
    space, and it is cut to DEVICE_NAME_LIMIT characters. Emoji, accents and
    the curly apostrophe in "Leo’s iPad" stay.
    """
    if not isinstance(value, str) or len(value) > 256:
        return None
    kept = "".join(" " if c.isspace() else c for c in value
                   if c.isspace() or unicodedata.category(c) not in {"Cc", "Cf", "Cs", "Co", "Cn"})
    kept = " ".join(kept.split())[:DEVICE_NAME_LIMIT].strip()
    return kept or None


def address(value):
    """The peer address, bounded and printable, or None. Never authorization."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not 1 <= len(value) <= 45 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    return value


# TAIL-2 P1-6 + LITE-1a C3: the road a request came by. One shape for the App
# and for a browser: behind `tailscale serve` the peer is the client serve
# forwarded for, so a browser on the tailnet is `tailnet` like the App is.
VIA_KINDS = ("lan", "tailnet", "loopback", "other")


# LITE-1a C4. What kind of client is asking, and the words the card uses for
# it. `native` is the App; `web` is Odomo in a browser, whose label
# ("Safari on iPhone") is the browser's own guess and is cleaned like a name.
CLIENT_KINDS = ("native", "web")


def clean_client(value, *, browser=False):
    """`{kind, label}` for the row. A browser is always `web`."""
    if value is not None and (not isinstance(value, dict) or set(value) - {"kind", "label"}
                              or value.get("kind") not in CLIENT_KINDS):
        raise ServiceError("invalid_request")
    value = value or {}
    kind = "web" if browser else value.get("kind", "native")
    label = clean_device_name(value.get("label")) if value.get("label") is not None else None
    if value.get("label") is not None and label is None:
        raise ServiceError("invalid_request")
    return {"kind": kind, "label": label if kind == "web" else None}


def client_label(client):
    """What the approval card calls a web client, formatted here, once."""
    if not isinstance(client, dict) or client.get("kind") != "web":
        return None
    return "Web app · " + client["label"] if client.get("label") else "Web app"


def clean_via(value):
    """`{kind, peer_name, login}` fit for the card, or None. Never authorization."""
    if not isinstance(value, dict) or value.get("kind") not in VIA_KINDS:
        return None
    result = {"kind": value["kind"]}
    for key, limit in (("peer_name", 64), ("login", 128)):
        text = value.get(key)
        if isinstance(text, str):
            text = "".join(c for c in text if c.isprintable() and
                           unicodedata.category(c) not in {"Cf", "Co", "Cn", "Cs"}).strip()[:limit]
        result[key] = text or None
    return result


class PairingStore:
    TTL = 300
    # How many devices' approvals are kept as durable grant sources. It is the
    # device registry's own order of magnitude, not the pending-request bound.
    MAX_CLAIMED = 64

    def __init__(self, authority: DeviceAuthenticator, path: Path):
        self.authority, self.path = authority, Path(path)

    @contextmanager
    def transaction(self):
        with _registry_lock(self.path):
            try:
                fd = _private_open(self.path, os.O_RDONLY)
                with os.fdopen(fd, "rb") as stream:
                    raw = stream.read(262145)
                if len(raw) > 262144:
                    raise ValueError("pairing state too large")
                state = json.loads(raw)
            except FileNotFoundError:
                state = {"invitations": {}, "requests": {}}
            now = int(time.time())
            state["invitations"] = {k: v for k, v in state["invitations"].items() if v["expires_at"] > now}
            # PAIR-3: a claimed request is not a pending request that ran out
            # of time - it is the record of which approval a device holds, and
            # `media-pairing grant-remote` is addressed by it. Pruning it at
            # the 300 s TTL deleted the only repair path for a half-paired
            # device five minutes after pairing (Leo's iPad, 2026-09-19), which
            # then answered `pairing_approval_required` forever. Claimed rows
            # survive; they carry no live secret and they are bounded below.
            state["requests"] = {k: v for k, v in state["requests"].items()
                                 if v["expires_at"] > now or v["status"] == "claimed"}
            claimed = sorted((v for v in state["requests"].values() if v["status"] == "claimed"),
                             key=lambda row: row.get("claimed_at", row["expires_at"]))
            while len(claimed) > self.MAX_CLAIMED:
                del state["requests"][claimed.pop(0)["request_id"]]
            yield state
            fd, temporary = tempfile.mkstemp(prefix=".pairing-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    json.dump(state, stream, separators=(",", ":"))
                    stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary): os.unlink(temporary)

    def begin(self):
        invitation = secrets.token_urlsafe(32)
        expires = int(time.time()) + self.TTL
        with self.transaction() as state:
            if len(state["invitations"]) >= 16:
                raise ServiceError("pairing_capacity", status=429)
            state["invitations"][digest(invitation)] = {"expires_at": expires}
        return {"invitation": invitation, "expires_at": expires, "ttl_seconds": self.TTL,
                "status": "open", "transport": "https", "approval_required": True}

    @staticmethod
    def public(row):
        value = {key: row[key] for key in ("request_id", "device_id", "device_name", "status", "expires_at")}
        # The public half of a key pair, shown to the person deciding. There is
        # nothing to redact; a fingerprint alone would make the plugin's card
        # unable to say which key it is about to authorize.
        value["ssh_public_key"] = row.get("ssh_public_key")
        value["ssh_fingerprint"] = row.get("ssh_fingerprint")
        value["grants"] = dict(row.get("grants") or EMPTY_GRANTS)
        # Where the request came from. Without an invitation this is the only
        # thing that distinguishes "the iPad I am holding" from "something else
        # on this network", so the card and `pair pending` both show it.
        value["remote_addr"] = row.get("remote_addr")
        # TAIL-2 P1-6: which road the request came by and, over the tailnet,
        # who this computer's own tailscaled says the peer is. The card shows
        # this instead of a raw 100.x address. Context, never authorization.
        value["via"] = dict(row["via"]) if isinstance(row.get("via"), dict) else None
        # TAIL-2 P0-2: the device already holds a live credential here (it
        # forgot this host, or reinstalled). Approving replaces it.
        value["already_paired"] = row.get("already_paired") is True
        # LITE-1a. Which client is asking (a row from before it is the App)
        # and the card's words for a web one.
        client = row.get("client") if isinstance(row.get("client"), dict) else {"kind": "native", "label": None}
        value["client"] = {"kind": client.get("kind", "native"), "label": client.get("label")}
        value["client_label"] = client_label(client)
        return value

    def pending(self):
        with self.transaction() as state:
            return {"requests": [self.public(row) for row in state["requests"].values()
                                 if row["status"] in {"pending", "approved"}]}

    def pending_request_ids(self):
        """NOTIFY-3. The ids still waiting for a local decision, or None.

        Read-only and lock-free on purpose: the notification watcher asks this
        every pass while one of our requests is on the desktop, and
        `transaction()` rewrites the file. The file is only ever replaced
        whole (`os.replace`), so a read sees one complete state. None means
        "cannot tell", which withdraws nothing.
        """
        try:
            fd = _private_open(self.path, os.O_RDONLY)
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(262145)
        except FileNotFoundError:
            return set()
        except OSError:
            return None
        if len(raw) > 262144:
            return None
        try:
            requests = json.loads(raw).get("requests")
        except (ValueError, AttributeError):
            return None
        if not isinstance(requests, dict):
            return None
        now = time.time()
        return {key for key, row in requests.items()
                if isinstance(row, dict) and row.get("status") == "pending"
                and isinstance(row.get("expires_at"), (int, float)) and row["expires_at"] > now}

    def request(self, invitation=None, device_id=None, device_name=None, ssh_public_key=None,
                remote_addr=None, mode="open", client=None, browser=False, via=None):
        if mode not in PAIRING_MODES:
            raise ServiceError("invalid_request")
        if not isinstance(device_id, str) or not _DEVICE_ID.fullmatch(device_id):
            raise ServiceError("invalid_request")
        # The panel's own credential is not a name the network may ask for.
        if device_id == PLUGIN_DEVICE_ID:
            raise ServiceError("invalid_request")
        device_name = clean_device_name(device_name)
        if device_name is None:
            raise ServiceError("invalid_request")
        client = clean_client(client, browser=browser)
        if client["kind"] == "web" and ssh_public_key is not None:
            # A browser cannot open an SSH session, so a key from one is a
            # request for a grant nobody can use: refused, not dropped.
            raise ServiceError("invalid_request", "a web client cannot offer an SSH key")
        key_type = key_body = None
        if ssh_public_key is not None:
            # Refuse a key sshd could not parse here, not at Approve: a request
            # that cannot deliver its terminal should never reach the person
            # who is about to be told it grants one.
            try:
                key_type, key_body = parse_public_key(ssh_public_key)
            except SshKeyError:
                raise ServiceError("invalid_request") from None
        source = address(remote_addr)
        if invitation is None and mode != "open":
            # The locked mode. It is the only path that still asks for the 43
            # characters, and the app only ever shows that field after seeing
            # this code.
            raise ServiceError("pairing_invitation_required", status=403)
        invitation_digest = digest(invitation) if invitation is not None else None
        secret = secrets.token_urlsafe(32)
        with self.transaction() as state:
            if invitation_digest is not None:
                row = state["invitations"].get(invitation_digest)
                if not row:
                    raise ServiceError("pairing_invitation_invalid_or_expired", status=403)
                expires_at = row["expires_at"]
            else:
                expires_at = int(time.time()) + self.TTL
                # One pending request per device: tapping the host again is the
                # same device saying "still me", not a second queue entry. The
                # old row's request_secret dies with it.
                for key in [k for k, v in state["requests"].items()
                            if v["device_id"] == device_id and v["status"] == "pending"]:
                    del state["requests"][key]
                if source is not None and sum(
                        1 for v in state["requests"].values()
                        if v["status"] == "pending" and v.get("remote_addr") == source) >= 2:
                    raise ServiceError("pairing_source_capacity", status=429)
            # Durable claimed rows are history, not queue depth: they must
            # never make the host refuse a new pairing.
            if sum(1 for v in state["requests"].values() if v["status"] != "claimed") >= 64:
                raise ServiceError("pairing_capacity", status=429)
            # TAIL-2 P0-2. This used to be `409 pairing_device_exists`: a
            # device that still held a credential could not ask again. TAIL-1
            # §4.4 met the cost away from home - a phone that forgot its
            # LAN-paired host could only come back over the tailnet after
            # somebody revoked it *at the computer* and then approved it again.
            # The request now waits for the same local Approve as any other;
            # the card says the device is already paired, and the claim
            # retires its older credentials, so the device ends with one.
            already = any(d["device_id"] == device_id and d["active_credentials"]
                          for d in self.authority.list_devices())
            request_id = "pair_" + secrets.token_hex(16)
            request = {"request_id": request_id, "device_id": device_id, "device_name": device_name,
                       "status": "pending", "expires_at": expires_at, "secret_hash": digest(secret),
                       "grants": dict(EMPTY_GRANTS)}
            if already:
                request["already_paired"] = True
            if source is not None:
                request["remote_addr"] = source
            request["client"] = client
            via = clean_via(via)
            if via is not None:
                request["via"] = via
            if key_body is not None:
                request["ssh_public_key"] = f"{key_type} {key_body}"
                request["ssh_fingerprint"] = fingerprint(key_body)
            state["requests"][request_id] = request
            if invitation_digest is not None:
                del state["invitations"][invitation_digest]
            return {**self.public(request), "request_secret": secret}

    def decide(self, request_id, *, approve):
        with self.transaction() as state:
            row = state["requests"].get(request_id)
            if not row:
                raise ServiceError("pairing_request_expired", status=404)
            if row["status"] != "pending":
                raise ServiceError("pairing_request_already_decided", status=409)
            row["status"] = "approved" if approve else "rejected"
            return self.public(row)

    def grant_source(self, device_id):
        """The request a device's grants can still be repaired from, or None.

        A claimed row outlives its TTL exactly so this answer exists; without
        it `media-pairing grant-remote` has nothing to name and a device that
        got the companion half and not the streaming half can never be fixed.
        """
        with self.transaction() as state:
            rows = [row for row in state["requests"].values()
                    if row["device_id"] == device_id and row["status"] in {"approved", "claimed"}]
            if not rows:
                return None
            return max(rows, key=lambda row: row.get("claimed_at", row["expires_at"]))["request_id"]

    def grants_for(self, device_id):
        """What the device's latest local Approve actually granted, or None.

        RELEASE-9 (B2): this is what `PUT /v1/ssh/key` asks before it writes a
        key - `ssh` is true only when a local Approve landed one.
        """
        with self.transaction() as state:
            rows = [row for row in state["requests"].values()
                    if row["device_id"] == device_id and row["status"] in {"approved", "claimed"}]
            if not rows:
                return None
            row = max(rows, key=lambda row: row.get("claimed_at", row["expires_at"]))
            return {"request_id": row["request_id"], **dict(row.get("grants") or EMPTY_GRANTS)}

    def offered_ssh_key(self, device_id):
        """REMOTE-PAIR-1: the SSH public key this device's latest approval carried."""
        with self.transaction() as state:
            rows = [row for row in state["requests"].values()
                    if row["device_id"] == device_id and row["status"] in {"approved", "claimed"}]
            if not rows:
                return None
            return max(rows, key=lambda row: row.get("claimed_at", row["expires_at"])).get("ssh_public_key")

    def ssh_fingerprints(self):
        """REMOTE-PAIR-1: `{device_id: (fingerprint, claimed_at)}` from each device's
        latest approval that carried a key. A display hint, never authority."""
        with self.transaction() as state:
            latest = {}
            for row in state["requests"].values():
                if row["status"] not in {"approved", "claimed"} or not row.get("ssh_fingerprint"):
                    continue
                when = row.get("claimed_at", row["expires_at"])
                if row["device_id"] not in latest or when > latest[row["device_id"]][1]:
                    latest[row["device_id"]] = (row["ssh_fingerprint"], when)
            return latest

    def forget_device(self, device_id):
        """Drop a revoked device's durable approvals; a revoke keeps nothing."""
        with self.transaction() as state:
            for key in [k for k, v in state["requests"].items() if v["device_id"] == device_id]:
                del state["requests"][key]

    def grant(self, request_id, **granted):
        """Record what the local Approve actually landed, for the claim to report."""
        if set(granted) - set(EMPTY_GRANTS):
            raise ServiceError("invalid_request")
        with self.transaction() as state:
            row = state["requests"].get(request_id)
            if not row:
                raise ServiceError("pairing_request_expired", status=404)
            grants = dict(row.get("grants") or EMPTY_GRANTS)
            grants.update({key: bool(value) for key, value in granted.items()})
            row["grants"] = grants
            return self.public(row)

    def claim(self, request_id, request_secret):
        secret_hash = digest(request_secret)
        with self.transaction() as state:
            row = state["requests"].get(request_id)
            if not row or not hmac.compare_digest(row["secret_hash"], secret_hash):
                raise ServiceError("pairing_request_invalid_or_expired", status=403)
            if row["status"] == "pending":
                return self.public(row)
            if row["status"] != "approved":
                raise ServiceError("pairing_request_unavailable", status=409)
            web = (row.get("client") or {}).get("kind") == "web"
            credential = self.authority.issue(row["device_id"], device_name=row["device_name"],
                                              role="web" if web else None,
                                              retire_others=row.get("already_paired") is True)
            row["status"] = "claimed"
            # The row now outlives its TTL. The secret stays only as the stored
            # hash it already was, and it authorizes nothing further: a second
            # claim is refused on the status below, not on the comparison.
            row["claimed_at"] = int(time.time())
            row["grants"] = {**(row.get("grants") or EMPTY_GRANTS), "companion": True}
            return {**self.public(row), "credential": credential.token,
                    "issued_at": credential.issued_at,
                    "credential_expires_at": credential.issued_at + self.authority.ttl_seconds}
