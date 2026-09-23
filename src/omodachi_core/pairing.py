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

from .auth import DeviceAuthenticator, _private_open, _registry_lock, _DEVICE_ID
from .protocol import PAIRING_MODES
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

def address(value):
    """The peer address, bounded and printable, or None. Never authorization."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not 1 <= len(value) <= 45 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    return value


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
        return value

    def pending(self):
        with self.transaction() as state:
            return {"requests": [self.public(row) for row in state["requests"].values()
                                 if row["status"] in {"pending", "approved"}]}

    def request(self, invitation=None, device_id=None, device_name=None, ssh_public_key=None,
                remote_addr=None, mode="open"):
        if mode not in PAIRING_MODES:
            raise ServiceError("invalid_request")
        if not isinstance(device_id, str) or not _DEVICE_ID.fullmatch(device_id):
            raise ServiceError("invalid_request")
        if (not isinstance(device_name, str) or not 1 <= len(device_name) <= 80
                or any(ord(c) < 32 or ord(c) == 127 for c in device_name)):
            raise ServiceError("invalid_request")
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
            if any(d["device_id"] == device_id and d["active_credentials"] for d in self.authority.list_devices()):
                raise ServiceError("pairing_device_exists", status=409)
            request_id = "pair_" + secrets.token_hex(16)
            request = {"request_id": request_id, "device_id": device_id, "device_name": device_name,
                       "status": "pending", "expires_at": expires_at, "secret_hash": digest(secret),
                       "grants": dict(EMPTY_GRANTS)}
            if source is not None:
                request["remote_addr"] = source
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
            credential = self.authority.issue(row["device_id"], device_name=row["device_name"])
            row["status"] = "claimed"
            # The row now outlives its TTL. The secret stays only as the stored
            # hash it already was, and it authorizes nothing further: a second
            # claim is refused on the status below, not on the comparison.
            row["claimed_at"] = int(time.time())
            row["grants"] = {**(row.get("grants") or EMPTY_GRANTS), "companion": True}
            return {**self.public(row), "credential": credential.token,
                    "issued_at": credential.issued_at,
                    "credential_expires_at": credential.issued_at + self.authority.ttl_seconds}
