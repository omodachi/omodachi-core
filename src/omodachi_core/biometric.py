"""The iPad as a PAM factor: an enrolled key, a nonce, and one signature.

Nothing here stores, sees, or can produce the user's password. What it stores
is a *public* key that a paired device enrolled once, and what it verifies is a
P-256 ECDSA signature over a nonce this host just minted. The private half never
leaves the device's Secure Enclave (or, where there is no enclave - the
simulator - its keychain, and the device says which it is, because a report that
cannot tell the difference is worth nothing).

The shape of an approval:

  PAM helper -> `local.auth.approve` -> broker.request()
      mints `approval_id` + 32-byte `nonce`, publishes `auth.approval.requested`
      to every enrolled device that has a live event subscription right now,
      and waits.
  device -> `POST /v1/auth/approvals/{id}` with a signature over the nonce
      broker.resolve() verifies it against the key that device enrolled,
      burns the nonce, and wakes the waiter.
  nobody -> the wait expires
      `auth.approval.resolved` says `timeout`, the helper exits non-zero, PAM
      falls through to the password.

Five things make this safe to put in front of a password prompt:

* **Fail closed, fail fast.** Every unknown is a refusal: preference off, no
  enrolled key, no connected device, no signature, a signature that does not
  verify, an expired approval, a device answering for an approval it was not
  asked about. A refusal is indistinguishable from "this was never installed".
* **One nonce, one use.** The pending record is removed the moment it is
  decided, so the same signature can never approve a second prompt.
* **The signature binds the whole request.** host id, approval id, nonce,
  service and target user are all in the signed bytes, so a signature captured
  for `sudo` cannot be replayed at the lock screen, and a signature for this
  host cannot be replayed at another one.
* **Only a live, paired, enrolled device is asked.** Not "every device that
  ever paired" - a device with no current event subscription is not asked and
  cannot answer, which is what makes "my iPad is with me" the real factor.
* **Both ends have to say yes, and both start at no.** The host's
  `biometric_auth` preference is one switch and the device's own registration
  is the other: a key is enrolled with `enabled` reported by the device, and a
  device that reports `false` - or has no registration at all - is not in any
  audience. Turning either switch off is enough to put the password back, and
  neither is on until somebody turns it on. When the host switch is off the
  broker refuses before it has published anything at all.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
import asyncio

from .auth import _DEVICE_ID, _private_open, _registry_lock
from .service import ServiceError

# ---------------------------------------------------------------------------
# NIST P-256 (secp256r1) signature verification, verification only.
#
# Core has no crypto dependency and is not about to grow one for a single
# `verify`. This is ~40 lines of textbook arithmetic over a fixed curve; it
# handles no secrets, so there is nothing here for a timing side channel to
# leak, and every input is range-checked before it is used.
# ---------------------------------------------------------------------------
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
      0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


def _add(first, second):
    if first is None:
        return second
    if second is None:
        return first
    (x1, y1), (x2, y2) = first, second
    if x1 == x2:
        if (y1 + y2) % _P == 0:
            return None
        slope = (3 * x1 * x1 + _A) * pow(2 * y1 % _P, _P - 2, _P) % _P
    else:
        slope = (y2 - y1) * pow((x2 - x1) % _P, _P - 2, _P) % _P
    x3 = (slope * slope - x1 - x2) % _P
    return (x3, (slope * (x1 - x3) - y1) % _P)


def _multiply(scalar, point):
    result, addend = None, point
    while scalar:
        if scalar & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    return result


def _on_curve(x, y):
    return 0 <= x < _P and 0 <= y < _P and (y * y - (x * x * x + _A * x + _B)) % _P == 0


def public_point(raw: bytes):
    """A P-256 public key from either the X9.63 point iOS hands out or SPKI DER.

    `SecKeyCopyExternalRepresentation` gives `04 || X || Y`, 65 bytes. Anything
    longer is accepted only if it is a DER SubjectPublicKeyInfo that ends in
    exactly that point with the standard 26-byte prefix; there is no general
    DER parser here on purpose.
    """
    if len(raw) == 91:
        prefix = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")
        if not raw.startswith(prefix):
            raise ValueError("unsupported public key encoding")
        raw = raw[len(prefix):]
    if len(raw) != 65 or raw[0] != 0x04:
        raise ValueError("public key must be an uncompressed P-256 point")
    x, y = int.from_bytes(raw[1:33], "big"), int.from_bytes(raw[33:], "big")
    if not _on_curve(x, y) or (x, y) == (0, 0):
        raise ValueError("public key is not on the curve")
    return (x, y)


def _der_signature(raw: bytes):
    """r, s out of a DER `SEQUENCE { INTEGER r, INTEGER s }`, strictly."""
    if len(raw) < 8 or raw[0] != 0x30 or raw[1] != len(raw) - 2:
        raise ValueError("malformed signature")
    body, values = raw[2:], []
    for _ in range(2):
        if len(body) < 2 or body[0] != 0x02:
            raise ValueError("malformed signature")
        size = body[1]
        if size == 0 or size > 33 or len(body) < 2 + size:
            raise ValueError("malformed signature")
        chunk = body[2:2 + size]
        if chunk[0] & 0x80 or (chunk[0] == 0 and (len(chunk) == 1 or not chunk[1] & 0x80)):
            raise ValueError("signature integer is not minimally encoded")
        values.append(int.from_bytes(chunk, "big"))
        body = body[2 + size:]
    if body:
        raise ValueError("trailing signature bytes")
    return values[0], values[1]


def verify_signature(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """True only for a well-formed ECDSA-P256-SHA256 signature over `message`."""
    try:
        point = public_point(public_key)
        r, s = _der_signature(signature)
    except ValueError:
        return False
    if not (1 <= r < _N and 1 <= s < _N):
        return False
    digest = int.from_bytes(hashlib.sha256(message).digest(), "big")
    inverse = pow(s, _N - 2, _N)
    combined = _add(_multiply(digest * inverse % _N, _G), _multiply(r * inverse % _N, point))
    if combined is None:
        return False
    return combined[0] % _N == r


# How many other credentials a rejected signature is checked against before the
# daemon stops guessing. Each one is a single EC verification on an already
# refused approval; the cap keeps a host with a long device history from paying
# for a signature that is simply wrong.
_DIAGNOSIS_LIMIT = 8

# ---------------------------------------------------------------------------
# What gets signed
# ---------------------------------------------------------------------------
APPROVAL_CONTEXT = "omodachi-auth-approval-v1"
ENROLLMENT_CONTEXT = "omodachi-auth-enrollment-v1"
_FIELD = re.compile(r"[\x20-\x7e]{1,128}\Z")


def _field(value, name):
    value = "" if value is None else str(value)
    if not _FIELD.fullmatch(value):
        raise ServiceError("invalid_request", f"invalid {name}")
    return value


def _optional(value, name):
    """A printable field, or None. PAM leaves most of them empty most of the time.

    `PAM_TTY` is unset for a `sudo` with no controlling terminal and `PAM_RHOST`
    is unset for everything local, so treating "" as a malformed field refuses
    every ordinary prompt. Empty means absent, not invalid.
    """
    value = "" if value is None else str(value).strip()
    return _field(value, name) if value else None


def approval_message(*, host_id, approval_id, nonce, service, user, device_id) -> bytes:
    """The exact bytes a device signs. Every field that scopes the approval is in it."""
    parts = [_field(host_id, "host_id"), _field(approval_id, "approval_id"), _field(nonce, "nonce"),
             _field(service, "service"), _field(user, "user"), _field(device_id, "device_id")]
    return ("\n".join([APPROVAL_CONTEXT, *parts]) + "\n").encode("ascii")


def enrollment_message(*, host_id, device_id, challenge, public_key_b64) -> bytes:
    parts = [_field(host_id, "host_id"), _field(device_id, "device_id"), _field(challenge, "challenge")]
    return ("\n".join([ENROLLMENT_CONTEXT, *parts, public_key_b64]) + "\n").encode("ascii")


def _b64(value, *, limit=512):
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise ServiceError("invalid_request")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ServiceError("invalid_request") from None


# ---------------------------------------------------------------------------
# Enrolled keys
# ---------------------------------------------------------------------------
_LABEL_LIMIT = 64
SERVICES = ("sudo", "polkit-1", "hyprlock", "omarchy-lock-password", "su", "login")
# What each service is called on the card. The device shows the host's words,
# not a PAM service name, because "polkit-1" means nothing to the person
# holding the iPad.
SERVICE_TITLES = {"sudo": "Administrator command", "su": "Switch user",
                  "polkit-1": "System permission", "hyprlock": "Unlock the screen",
                  "omarchy-lock-password": "Unlock the screen", "login": "Sign in"}


class BiometricKeyStore:
    """Enrolled public keys, one per device, on the same atomic-write pattern
    as the credential registry next to it."""

    MAX_KEYS = 32

    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        try:
            fd = _private_open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ServiceError("biometric_store_invalid", status=503)
        try:
            data = json.loads(raw)
            keys = data["keys"]
            if not isinstance(keys, dict):
                raise ValueError
            for device_id, row in keys.items():
                if not _DEVICE_ID.fullmatch(device_id) or not isinstance(row, dict):
                    raise ValueError
                public_point(base64.b64decode(row["public_key"], validate=True))
        except (ValueError, KeyError, TypeError, binascii.Error):
            raise ServiceError("biometric_store_invalid", status=503) from None
        return keys

    def _save(self, keys):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".biometric-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({"version": 1, "keys": keys}, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get(self, device_id):
        with _registry_lock(self.path):
            return self._load().get(device_id)

    def list(self):
        with _registry_lock(self.path):
            return [{"device_id": device_id, **{k: v for k, v in row.items() if k != "public_key"}}
                    for device_id, row in sorted(self._load().items())]

    def enroll(self, device_id, *, public_key_b64, label, secure_enclave, enabled):
        if not isinstance(label, str) or not 1 <= len(label) <= _LABEL_LIMIT or any(
                ord(c) < 32 or ord(c) == 127 for c in label):
            raise ServiceError("invalid_request", "invalid key label")
        try:
            public_point(_b64(public_key_b64))
        except ValueError:
            raise ServiceError("invalid_request", "invalid public key") from None
        with _registry_lock(self.path):
            keys = self._load()
            # Re-enrolling replaces; a device has exactly one key, and a key
            # that changed is a biometric set that changed.
            if device_id not in keys and len(keys) >= self.MAX_KEYS:
                raise ServiceError("biometric_key_capacity", status=429)
            row = {"public_key": public_key_b64, "label": label, "enrolled_at": int(time.time()),
                   "secure_enclave": bool(secure_enclave), "algorithm": "ecdsa-p256-sha256",
                   # The device's half of the two switches. It is reported by
                   # the device, stored here, and read on every approval: a
                   # registered key whose owner has the switch off is not an
                   # audience, it is just a key.
                   "enabled": bool(enabled)}
            keys[device_id] = row
            self._save(keys)
            return {"device_id": device_id, **{k: v for k, v in row.items() if k != "public_key"}}

    def set_enabled(self, device_id, enabled):
        """Flip the device's own switch without making it enrol again."""
        with _registry_lock(self.path):
            keys = self._load()
            row = keys.get(device_id)
            if row is None:
                raise ServiceError("biometric_key_unknown", status=404)
            row["enabled"] = bool(enabled)
            keys[device_id] = row
            self._save(keys)
            return {"device_id": device_id, **{k: v for k, v in row.items() if k != "public_key"}}

    def revoke(self, device_id):
        with _registry_lock(self.path):
            keys = self._load()
            removed = keys.pop(device_id, None) is not None
            if removed:
                self._save(keys)
            return {"device_id": device_id, "revoked": removed}


# ---------------------------------------------------------------------------
# The broker
# ---------------------------------------------------------------------------
class ApprovalBroker:
    """One pending approval per PAM prompt, resolved by a signature or by time."""

    MIN_TIMEOUT, MAX_TIMEOUT, DEFAULT_TIMEOUT = 5.0, 120.0, 45.0
    MAX_PENDING = 8
    CHALLENGE_TTL = 300

    def __init__(self, hub, keys: BiometricKeyStore, *, preferences=None, host_identity=None,
                 journal=None, clock=time.time):
        self.hub = hub
        self.keys = keys
        self.preferences = preferences
        self.host_identity = host_identity
        self._journal = journal if journal is not None else _print_journal
        self._clock = clock
        self._pending = {}
        self._challenges = {}

    # -- host facts -------------------------------------------------------
    @property
    def host_id(self):
        return getattr(self.host_identity, "host_id", None) or "unknown-host"

    @property
    def host_name(self):
        return getattr(self.host_identity, "host_name", None) or "this host"

    def enabled(self) -> bool:
        store = self.preferences
        if store is None:
            return False
        try:
            return bool(store.get()["values"].get("biometric_auth"))
        except (ServiceError, OSError, KeyError, TypeError):
            return False

    def _eligible(self):
        """Devices that are enrolled, still credentialed, and connected now."""
        connected = self.hub.connected_devices()
        authorized = {row["device_id"] for row in self.hub.auth.list_devices()
                      if row.get("active_credentials")}
        return [row for row in self.keys.list()
                if row.get("enabled") and row["device_id"] in connected
                and row["device_id"] in authorized]

    def status(self) -> dict:
        """Both switches, side by side. `enabled` is the host's; each key carries its own."""
        return {"enabled": self.enabled(), "host_id": self.host_id, "host_name": self.host_name,
                "keys": self.keys.list(), "eligible_devices": [row["device_id"] for row in self._eligible()],
                "pending": len(self._pending), "services": list(SERVICES)}

    # -- enrollment -------------------------------------------------------
    def challenge(self, device_id) -> str:
        value = secrets.token_urlsafe(32)
        now = self._clock()
        self._challenges = {key: row for key, row in self._challenges.items()
                            if row["expires_at"] > now}
        self._challenges[device_id] = {"challenge": value, "expires_at": now + self.CHALLENGE_TTL}
        return value

    def enroll(self, device_id, payload) -> dict:
        if not isinstance(payload, dict) or set(payload) - {"public_key", "label", "challenge",
                                                            "signature", "secure_enclave", "enabled"}:
            raise ServiceError("invalid_request")
        for key in ("public_key", "label", "challenge", "signature", "enabled"):
            if key not in payload:
                raise ServiceError("invalid_request")
        if type(payload["enabled"]) is not bool:
            raise ServiceError("invalid_request", "enabled must be reported by the device")
        row = self._challenges.get(device_id)
        if row is None or row["expires_at"] <= self._clock() or not hmac.compare_digest(
                row["challenge"], str(payload["challenge"])):
            raise ServiceError("biometric_challenge_invalid", status=409)
        # The challenge is spent whether or not the signature is good.
        self._challenges.pop(device_id, None)
        public_key = str(payload["public_key"])
        message = enrollment_message(host_id=self.host_id, device_id=device_id,
                                     challenge=str(payload["challenge"]), public_key_b64=public_key)
        if not verify_signature(_b64(public_key), message, _b64(str(payload["signature"]), limit=256)):
            raise ServiceError("biometric_signature_invalid", status=403)
        result = self.keys.enroll(device_id, public_key_b64=public_key, label=str(payload["label"]),
                                  secure_enclave=bool(payload.get("secure_enclave", False)),
                                  enabled=payload["enabled"])
        self._journal({"event": "auth.key.enrolled", "device_id": device_id,
                       "secure_enclave": result["secure_enclave"], "label": result["label"],
                       "enabled": result["enabled"]})
        return result

    def set_enabled(self, device_id, payload) -> dict:
        if not isinstance(payload, dict) or set(payload) != {"enabled"} or type(payload["enabled"]) is not bool:
            raise ServiceError("invalid_request")
        result = self.keys.set_enabled(device_id, payload["enabled"])
        self._journal({"event": "auth.key.switch", "device_id": device_id,
                       "enabled": result["enabled"]})
        return result

    def revoke(self, device_id) -> dict:
        result = self.keys.revoke(device_id)
        if result["revoked"]:
            self._journal({"event": "auth.key.revoked", "device_id": device_id})
        for approval_id, record in list(self._pending.items()):
            if device_id in record["devices"]:
                record["devices"] = [d for d in record["devices"] if d != device_id]
        return result

    # -- approvals --------------------------------------------------------
    def _description(self, service, user, requester, tty):
        title = SERVICE_TITLES.get(service, service)
        where = f" on {tty}" if tty else ""
        if service in {"sudo", "su"} and user and user != requester:
            return f"{title}{where} as {user}"
        return f"{title}{where}"

    async def request(self, payload) -> dict:
        if not isinstance(payload, dict) or set(payload) - {"service", "user", "requester", "tty",
                                                            "rhost", "timeout"}:
            raise ServiceError("invalid_request")
        service = _field(payload.get("service"), "service")
        if service not in SERVICES:
            return self._refuse(None, "unsupported_service", service=service)
        # PAM names the target user and the person driving the prompt
        # separately, and either one can be missing depending on the service.
        # Whatever is known stands in for whatever is not; with neither there
        # is nothing to put on the card, so there is no approval to raise.
        user = _optional(payload.get("user"), "user")
        requester = _optional(payload.get("requester"), "requester")
        user, requester = user or requester, requester or user
        if not user:
            return self._refuse(None, "no_user", service=service)
        tty = _optional(payload.get("tty"), "tty")
        rhost = _optional(payload.get("rhost"), "rhost")
        timeout = payload.get("timeout", self.DEFAULT_TIMEOUT)
        if type(timeout) not in (int, float) or timeout != timeout:
            timeout = self.DEFAULT_TIMEOUT
        timeout = max(self.MIN_TIMEOUT, min(self.MAX_TIMEOUT, float(timeout)))

        if not self.enabled():
            return self._refuse(None, "disabled", service=service)
        if len(self._pending) >= self.MAX_PENDING:
            return self._refuse(None, "too_many_pending", service=service)
        devices = self._eligible()
        if not devices:
            return self._refuse(None, "no_connected_device", service=service)

        approval_id = "appr_" + secrets.token_hex(16)
        nonce = secrets.token_urlsafe(32)
        now = self._clock()
        record = {"approval_id": approval_id, "nonce": nonce, "service": service, "user": user,
                  "requester": requester, "tty": tty, "rhost": rhost,
                  "devices": [row["device_id"] for row in devices],
                  "created_at": now, "expires_at": now + timeout,
                  "event": asyncio.Event(), "outcome": None, "device_id": None}
        self._pending[approval_id] = record
        payload_out = {"approval_id": approval_id, "nonce": nonce, "service": service,
                       "service_title": SERVICE_TITLES.get(service, service), "user": user,
                       "requester": requester, "tty": tty, "rhost": rhost,
                       "description": self._description(service, user, requester, tty),
                       "host_id": self.host_id, "host_name": self.host_name,
                       "requested_at": int(now), "expires_at": int(record["expires_at"]),
                       "timeout_seconds": int(timeout)}
        self._journal({"event": "auth.approval.requested", "approval_id": approval_id,
                       "service": service, "user": user, "requester": requester, "tty": tty,
                       "devices": record["devices"], "timeout_seconds": int(timeout)})
        for device_id in record["devices"]:
            # `device_id` is in the signed bytes, and it is the one field the
            # device cannot derive: it is the identity of the *credential* this
            # connection authenticated with, which a device that re-paired (or
            # was reinstalled) no longer knows locally. UX-3 §1: leaving it to
            # the device to guess is what made every real-iPad approval fail
            # `biometric_signature_invalid`. The host knows it, so the host says
            # it, per device, in the frame the device signs from.
            self.hub.publish("auth.approval.requested", {**payload_out, "device_id": device_id},
                             device_id=device_id)

        try:
            await asyncio.wait_for(record["event"].wait(), timeout)
        except asyncio.TimeoutError:
            pass
        record = self._pending.pop(approval_id, record)
        outcome = record["outcome"] or "timeout"
        approved = outcome == "approved"
        self._announce(record, outcome)
        self._journal({"event": "auth.approval.decided", "approval_id": approval_id,
                       "service": service, "user": user, "requester": requester,
                       "outcome": outcome, "device_id": record.get("device_id")})
        name = None
        if approved and record.get("device_id"):
            name = self.hub.auth.device_name(record["device_id"])
        return {"approved": approved, "approval_id": approval_id, "outcome": outcome,
                "device_id": record.get("device_id"), "device_name": name}

    def _announce(self, record, outcome):
        for device_id in record["devices"]:
            self.hub.publish("auth.approval.resolved",
                             {"approval_id": record["approval_id"], "outcome": outcome,
                              "device_id": record.get("device_id")}, device_id=device_id)

    def _refuse(self, approval_id, reason, **extra):
        self._journal({"event": "auth.approval.refused", "reason": reason, **extra})
        return {"approved": False, "approval_id": approval_id, "outcome": reason,
                "device_id": None, "device_name": None}

    def resolve(self, approval_id, device_id, payload) -> dict:
        if not isinstance(payload, dict) or set(payload) - {"decision", "signature"}:
            raise ServiceError("invalid_request")
        decision = payload.get("decision")
        if decision not in {"approve", "decline"}:
            raise ServiceError("invalid_request")
        record = self._pending.get(approval_id)
        # An unknown approval and a spent one answer identically: a device that
        # is one signature too late learns nothing about what happened.
        if record is None or record["outcome"] is not None or device_id not in record["devices"]:
            raise ServiceError("auth_approval_unknown", status=404)
        if record["expires_at"] <= self._clock():
            raise ServiceError("auth_approval_expired", status=409)
        if decision == "decline":
            record["outcome"], record["device_id"] = "declined", device_id
            record["event"].set()
            return {"approval_id": approval_id, "outcome": "declined"}
        enrolled = self.keys.get(device_id)
        if enrolled is None:
            raise ServiceError("biometric_key_unknown", status=403)
        signature = payload.get("signature")
        if not isinstance(signature, str):
            raise ServiceError("invalid_request")
        message = approval_message(host_id=self.host_id, approval_id=approval_id,
                                   nonce=record["nonce"], service=record["service"],
                                   user=record["user"], device_id=device_id)
        public_key = base64.b64decode(enrolled["public_key"], validate=True)
        raw_signature = _b64(signature, limit=256)
        if not verify_signature(public_key, message, raw_signature):
            self._journal({"event": "auth.approval.signature_rejected",
                           "approval_id": approval_id, "device_id": device_id,
                           **self._diagnose(public_key, raw_signature, record, device_id)})
            raise ServiceError("biometric_signature_invalid", status=403)
        record["outcome"], record["device_id"] = "approved", device_id
        record["event"].set()
        return {"approval_id": approval_id, "outcome": "approved"}

    def _diagnose(self, public_key, signature, record, device_id) -> dict:
        """Say *why* a signature did not verify, when the answer is knowable.

        A bare `signature_rejected` is the least useful line this daemon can
        print: it is the same line for a tampered signature, a stale key and a
        device signing under the wrong identity, and the last of those is the
        one that actually happened (UX-3 §1). `device_id` is the only field in
        the signed bytes that the device does not receive from us, so it is the
        only one it can get wrong on its own - and it is cheap to check. Every
        other credential this host has issued is tried; a hit names the identity
        the device believes it has.

        This decides nothing. The approval has already been refused by the time
        it runs, and no branch here can approve one.
        """
        try:
            others = [row["device_id"] for row in self.hub.auth.list_devices()
                      if row["device_id"] != device_id][:_DIAGNOSIS_LIMIT]
        except Exception:                                    # pragma: no cover - inventory is advisory
            return {"reason": "signature"}
        for candidate in others:
            try:
                message = approval_message(host_id=self.host_id, approval_id=record["approval_id"],
                                           nonce=record["nonce"], service=record["service"],
                                           user=record["user"], device_id=candidate)
            except ServiceError:                             # pragma: no cover - fields already validated
                continue
            if verify_signature(public_key, message, signature):
                return {"reason": "device_id_mismatch", "signed_as": candidate}
        return {"reason": "signature"}

    def pending_for(self, device_id) -> dict:
        """What a device that just reconnected is still being asked, if anything."""
        now = self._clock()
        rows = [{"approval_id": record["approval_id"], "nonce": record["nonce"],
                 "device_id": device_id,
                 "service": record["service"], "user": record["user"],
                 "requester": record["requester"], "tty": record["tty"],
                 "service_title": SERVICE_TITLES.get(record["service"], record["service"]),
                 "description": self._description(record["service"], record["user"],
                                                  record["requester"], record["tty"]),
                 "host_id": self.host_id, "host_name": self.host_name,
                 "requested_at": int(record["created_at"]), "expires_at": int(record["expires_at"]),
                 "timeout_seconds": int(record["expires_at"] - record["created_at"])}
                for record in self._pending.values()
                if device_id in record["devices"] and record["outcome"] is None
                and record["expires_at"] > now]
        return {"approvals": rows}


def _print_journal(entry):
    """systemd captures the daemon's stdout, so this *is* the journal line."""
    try:
        print(json.dumps({"omodachi": "auth", **entry}, separators=(",", ":"), default=str), flush=True)
    except (OSError, ValueError):
        pass
