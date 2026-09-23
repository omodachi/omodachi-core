"""Authenticated companion -> managed-Sunshine pairing bridge.

The companion already holds a device credential, so this module is only the
relay that carries its four-digit PIN to the fork's existing pairing.list/
pending/approve/cancel operations and follows the attempt to paired, failed or
expired. There is no Sunshine web page, no password and no PIN to copy.

The PIN is spent once: it lives in a wiped buffer, is written to the fork
behind a durable `submitting` barrier, and is never persisted.

Since PLUG-4 the fork can revoke a certificate for real (`pairing.revoke`), so
a revoke finishes: the certificate is dropped in Sunshine and the record goes
with it. A fork without that operation is still handled the old way - the
revoke is fail-closed locally and the record stays explicitly
`revocation_pending` rather than claiming success.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import stat
import tempfile
import threading
import time
import uuid
from typing import Callable

_DEVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
# The managed fork emits its pairing request IDs in UPPERCASE
# (`5D0D7E45-3704-FC04-3814-31D0A72C8AF8`, measured against the real host), so a
# lowercase-only pattern rejected every `pairing.list` that actually had a
# pending request in it and failed the whole bridge with
# `media_pairing_invalid_binding`. The ID is carried back to the fork exactly as
# it was received; case is never normalized, only accepted.
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
REMOTE_STATUSES = frozenset({"pending", "awaiting_client_proof", "paired", "failed", "cancelled", "expired"})
LOCAL_STATUSES = REMOTE_STATUSES | {"awaiting_local_approval", "submitting", "cancellation_pending",
                                    "revocation_pending", "revoked"}
TERMINAL = frozenset({"paired", "failed", "cancelled", "expired", "revocation_pending", "revoked"})
# PAIR-3 §2.2: records of something that already finished and that the fork
# could not clean up, so they stayed on the host for good. They are history,
# never a to-do item, and a caller must be able to read the to-do list without
# wading past them. PLUG-4: a fork that can revoke empties this list instead -
# a finished revoke leaves no record at all, so `revoked` is never stored.
HISTORY_STATUSES = frozenset({"revocation_pending", "cancellation_pending"})
MAX_TTL = 120
MAX_ATTEMPTS = 256
MAX_DEVICES = 128
EMPTY = {"version": 2, "devices": {}, "attempts": {}, "bindings": {}}
DEVICE_KEYS = frozenset({"allowed", "source_request_id", "updated_at"})
ATTEMPT_KEYS = frozenset({"attempt_id", "device_id", "request_id", "client_cert_sha256",
                          "status", "created_at", "expires_at", "updated_at", "reason"})
BINDING_KEYS = frozenset({"device_id", "client_cert_sha256", "request_id", "paired_at", "status"})


class MediaPairingError(ValueError):
    def __init__(self, code="media_pairing_unavailable", status=409):
        self.code, self.status = code, status
        super().__init__(code)


def _match(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _binding(request_id, fingerprint):
    if not _match(_UUID, request_id) or not _match(_SHA, fingerprint):
        raise MediaPairingError("media_pairing_invalid_binding", 400)


def _authorize(callback):
    if not callable(callback): raise MediaPairingError("media_pairing_auth_required", 401)
    try:
        if callback() is False: raise PermissionError()
    except Exception:
        raise MediaPairingError("media_pairing_auth_revoked", 401) from None


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError()
            result[key] = value
        return result
    try:
        result = json.loads(raw, object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(result, dict): raise ValueError()
        return result
    except (ValueError, UnicodeError):
        raise MediaPairingError("media_pairing_invalid_response", 502) from None


def _metadata(value):
    if not isinstance(value, dict) or set(value) != {"request_id", "client_cert_sha256", "status", "expires_in_ms"}:
        raise MediaPairingError("media_pairing_invalid_response", 502)
    _binding(value["request_id"], value["client_cert_sha256"])
    if value["status"] not in REMOTE_STATUSES or type(value["expires_in_ms"]) is not int or not 0 <= value["expires_in_ms"] <= MAX_TTL * 1000:
        raise MediaPairingError("media_pairing_invalid_response", 502)
    return dict(value)


def _migrate(state):
    """Carry a SPEC-B2 grant file forward into the one allow flag per device.

    An explicit deny is the part that must not be lost: a device the user
    revoked stays denied. Certificate lists and epochs are dropped with the
    grant view, and in-flight attempts are not worth keeping - they expire in
    two minutes and their PIN never survived a restart anyway.
    """
    if not isinstance(state, dict) or state.get("version") != 1 or "grants" not in state:
        return state
    devices = {}
    for device, grant in (state.get("grants") or {}).items():
        if not isinstance(grant, dict): raise MediaPairingError("media_pairing_state_invalid", 503)
        devices[device] = {"allowed": grant.get("authorized") is True,
                           "source_request_id": str(grant.get("source_request_id", "")),
                           "updated_at": float(grant.get("updated_at", 0.0) or 0.0)}
    bindings = {}
    for fingerprint, row in (state.get("bindings") or {}).items():
        if not isinstance(row, dict): raise MediaPairingError("media_pairing_state_invalid", 503)
        bindings[fingerprint] = {key: row.get(key) for key in BINDING_KEYS}
    return {"version": 2, "devices": devices, "attempts": {}, "bindings": bindings}


class SunshinePairingIPC:
    """One bounded JSONL exchange with the actual private same-user fork socket."""
    def __init__(self, path: Path, *, timeout=1.8, peer_check=None):
        self.path = Path(path)
        if not self.path.is_absolute() or not 0 < timeout <= 2:
            raise MediaPairingError("media_pairing_ipc_config_invalid", 400)
        self.timeout = timeout
        if peer_check is None:
            from .remote.sunshine import SunshineDesktopIPC
            peer_check = SunshineDesktopIPC._verify_peer
        self.peer_check = peer_check

    def request(self, operation, **fields):
        required = {"pairing.list": set(), "pairing.pending": {"request_id", "client_cert_sha256"},
                    "pairing.cancel": {"request_id", "client_cert_sha256"},
                    "pairing.revoke": {"client_cert_sha256"}, "pairing.clients": set(),
                    "pairing.approve": {"request_id", "client_cert_sha256", "pin", "name"}}
        if operation not in required or set(fields) != required[operation]:
            raise MediaPairingError("media_pairing_ipc_operation_invalid", 400)
        # PLUG-4: a revoke is addressed by certificate alone. The attempt the
        # certificate came from is long gone by the time a user revokes a
        # device, and the certificate is what still authorizes a stream.
        if operation == "pairing.revoke":
            if not _match(_SHA, fields["client_cert_sha256"]):
                raise MediaPairingError("media_pairing_invalid_binding", 400)
        elif fields:
            _binding(fields["request_id"], fields["client_cert_sha256"])
        if operation == "pairing.approve":
            if not isinstance(fields["pin"], str) or not re.fullmatch(r"[0-9]{4}", fields["pin"]):
                raise MediaPairingError("media_pairing_invalid_pin", 400)
            name = fields["name"]
            if not isinstance(name, str) or len(name.encode()) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in name):
                raise MediaPairingError("media_pairing_invalid_name", 400)
        raw = bytearray((json.dumps({"op": operation, **fields}, separators=(",", ":")) + "\n").encode())
        if len(raw) > 4096: raise MediaPairingError("media_pairing_request_limit", 400)
        try:
            if os.getuid() == 0 or os.geteuid() != os.getuid():
                raise MediaPairingError("media_pairing_ipc_unsafe", 503)
            parent, endpoint = self.path.parent.lstat(), self.path.lstat()
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077
                    or not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != os.getuid() or endpoint.st_mode & 0o077):
                raise MediaPairingError("media_pairing_ipc_unsafe", 503)
            deadline = time.monotonic() + self.timeout
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout);client.connect(str(self.path))
                self.peer_check(client)
                client.sendall(raw)
                response = bytearray()
                while b"\n" not in response:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0: raise TimeoutError()
                    client.settimeout(remaining)
                    chunk = client.recv(min(4096, 32769 - len(response)))
                    if not chunk: raise MediaPairingError("media_pairing_ipc_incomplete", 503)
                    response.extend(chunk)
                    if len(response) > 32768: raise MediaPairingError("media_pairing_response_limit", 502)
                if response.count(b"\n") != 1 or not response.endswith(b"\n"):
                    raise MediaPairingError("media_pairing_invalid_response", 502)
                value = _json(response)
            if value.get("ok") is not True:
                code = value.get("error", {}).get("code") if isinstance(value.get("error"), dict) else None
                allowed = {"pairing_request_not_pending", "pairing_binding_mismatch", "pairing_request_expired",
                           "pairing_pin_already_submitted", "local_pairing_disabled", "unknown_operation"}
                raise MediaPairingError(code if code in allowed else "media_pairing_ipc_rejected", 409)
            if operation == "pairing.list":
                rows = value.get("requests")
                if not isinstance(rows, list) or len(rows) > 72: raise MediaPairingError("media_pairing_invalid_response", 502)
                clean = [_metadata(row) for row in rows]
                if len({row["request_id"] for row in clean}) != len(clean):
                    raise MediaPairingError("media_pairing_invalid_response", 502)
                # The capability answer. Absent means "this fork cannot revoke",
                # which is what every fork before PLUG-4 says by saying nothing.
                return {"requests": clean,
                        "certificate_revocation_supported": value.get("certificate_revocation_supported") is True}
            if operation == "pairing.clients":
                rows = value.get("clients")
                if (not isinstance(rows, list) or len(rows) > 64
                        or type(value.get("unreadable")) is not int or not 0 <= value["unreadable"] <= 4096
                        or type(value.get("truncated")) is not bool):
                    raise MediaPairingError("media_pairing_invalid_response", 502)
                clients = []
                for row in rows:
                    if (not isinstance(row, dict) or set(row) != {"client_cert_sha256", "name", "enabled"}
                            or not _match(_SHA, row["client_cert_sha256"]) or type(row["enabled"]) is not bool
                            or not isinstance(row["name"], str) or len(row["name"].encode()) > 80
                            or any(ord(c) < 32 or ord(c) == 127 for c in row["name"])):
                        raise MediaPairingError("media_pairing_invalid_response", 502)
                    clients.append(dict(row))
                if len({row["client_cert_sha256"] for row in clients}) != len(clients):
                    raise MediaPairingError("media_pairing_invalid_response", 502)
                return {"clients": clients, "unreadable": value["unreadable"], "truncated": value["truncated"]}
            if operation == "pairing.revoke":
                # `not_found` is as good as `revoked`: either way that
                # certificate is not authorized in the fork any more.
                if value.get("status") not in {"revoked", "not_found"}:
                    raise MediaPairingError("media_pairing_invalid_response", 502)
                return {"status": value["status"]}
            if operation == "pairing.approve":
                # An approve ACK means the PIN was accepted, never that the
                # client finished its proof; only a later paired poll means that.
                if (value.get("accepted") is not True or value.get("request_id") != fields["request_id"]
                        or value.get("status") != "awaiting_client_proof"):
                    raise MediaPairingError("media_pairing_invalid_response", 502)
                return {"accepted": True, "request_id": fields["request_id"], "status": "awaiting_client_proof"}
            item = _metadata(value.get("request"))
            if item["request_id"] != fields["request_id"] or item["client_cert_sha256"] != fields["client_cert_sha256"]:
                raise MediaPairingError("media_pairing_binding_mismatch", 409)
            return {"request": item}
        except MediaPairingError: raise
        except Exception:
            raise MediaPairingError("media_pairing_ipc_unavailable", 503) from None
        finally:
            raw[:] = b"\0" * len(raw)
            fields.pop("pin", None)


class MediaPairingStore:
    """Private same-user state. Attempts are short-lived, but the paired
    certificate and an explicit deny must survive a daemon restart, so they are
    written atomically under the same lock. PINs are never fields here."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.mutex = threading.RLock()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise MediaPairingError("media_pairing_state_unsafe", 503)

    @contextmanager
    def transaction(self):
        with self.mutex:
            fd = os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "r+") as lock:
                info = os.fstat(lock.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise MediaPairingError("media_pairing_state_unsafe", 503)
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield self._read()

    def _read(self):
        try: fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError: return deepcopy(EMPTY)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise MediaPairingError("media_pairing_state_unsafe", 503)
            raw = stream.read(1048577)
        if len(raw) > 1048576: raise MediaPairingError("media_pairing_state_limit", 503)
        state = _migrate(_json(raw))
        # A private file is still parsed defensively: an unexpected field could
        # otherwise be echoed back to a plugin or a companion.
        if (set(state) != set(EMPTY) or state["version"] != EMPTY["version"]
                or any(not isinstance(state[key], dict) for key in ("devices", "attempts", "bindings"))
                or len(state["devices"]) > MAX_DEVICES or len(state["attempts"]) > MAX_ATTEMPTS
                or len(state["bindings"]) > MAX_ATTEMPTS):
            raise MediaPairingError("media_pairing_state_invalid", 503)
        for section, keys, identity in (("devices", DEVICE_KEYS, _DEVICE),
                                        ("attempts", ATTEMPT_KEYS, _UUID),
                                        ("bindings", BINDING_KEYS, _SHA)):
            for key, row in state[section].items():
                if not _match(identity, key) or not isinstance(row, dict) or set(row) != keys:
                    raise MediaPairingError("media_pairing_state_invalid", 503)
        if (any(a["attempt_id"] != k or a["status"] not in LOCAL_STATUSES for k, a in state["attempts"].items())
                or any(b["client_cert_sha256"] != k or b["status"] not in {"paired", "revocation_pending"}
                       for k, b in state["bindings"].items())
                or any(type(d["allowed"]) is not bool for d in state["devices"].values())):
            raise MediaPairingError("media_pairing_state_invalid", 503)
        return state

    def commit(self, state):
        raw = json.dumps(state, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > 1048576: raise MediaPairingError("media_pairing_state_limit", 503)
        fd, name = tempfile.mkstemp(prefix=".media-pairing-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream: stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.chmod(name, 0o600);os.replace(name, self.path)
        finally:
            if os.path.exists(name): os.unlink(name)


class MediaPairingBridge:
    def __init__(self, ipc: SunshinePairingIPC, store: MediaPairingStore, *, clock=time.time, monotonic=time.monotonic):
        self.ipc, self.store, self.clock, self.monotonic = ipc, store, clock, monotonic
        self._pins: dict[str, tuple[bytearray, float]] = {}
        self._authorizers: dict[str, Callable] = {}
        self._maintenance_lock = threading.Lock()
        # None until the fork has answered once. It is never assumed: a caller
        # that has not seen the capability keeps its revoke unresolved.
        self._revocation_supported: bool | None = None

    def _wipe(self, ident):
        value = self._pins.pop(ident, None)
        if value: value[0][:] = b"\0" * len(value[0])
        self._authorizers.pop(ident, None)

    def _remember_pin(self, attempt, pin, authorize):
        self._wipe(attempt["attempt_id"])
        lifetime = max(0, min(MAX_TTL, attempt["expires_at"] - self.clock()))
        self._pins[attempt["attempt_id"]] = (bytearray(pin.encode("ascii")), self.monotonic() + lifetime)
        self._authorizers[attempt["attempt_id"]] = authorize

    def _request(self, attempt, operation):
        return self.ipc.request(operation, request_id=attempt["request_id"],
                                client_cert_sha256=attempt["client_cert_sha256"])["request"]

    @staticmethod
    def _allowed(state, device_id):
        return state["devices"].get(device_id, {}).get("allowed") is True

    def _view(self, state, attempt):
        binding = state["bindings"].get(attempt["client_cert_sha256"])
        allowed = self._allowed(state, attempt["device_id"])
        return {**deepcopy(attempt),
                "expires_in_ms": max(0, min(MAX_TTL * 1000, int((attempt["expires_at"] - self.clock()) * 1000))),
                "paired": attempt["status"] == "paired" and bool(binding and binding["status"] == "paired") and allowed,
                "media_authorized": allowed,
                "certificate_revocation_supported": self._revocation_supported is True}

    def _set_device(self, state, device_id, *, allowed, source_request_id=""):
        if device_id not in state["devices"] and len(state["devices"]) >= MAX_DEVICES:
            raise MediaPairingError("media_pairing_capacity", 429)
        row = state["devices"].setdefault(device_id, {"allowed": allowed, "source_request_id": "", "updated_at": 0.0})
        row.update(allowed=allowed, updated_at=self.clock())
        if source_request_id: row["source_request_id"] = source_request_id
        return row

    def grant_remote(self, device_id, *, remote_allowed, source_request_id, local_authorize):
        """Local host hook, only after an explicit local 'Approve + Remote'."""
        _authorize(local_authorize)
        if (not _match(_DEVICE, device_id) or remote_allowed is not True
                or not isinstance(source_request_id, str) or not re.fullmatch(r"pair_[0-9a-f]{32}", source_request_id)):
            raise MediaPairingError("media_permission_not_explicit", 400)
        with self.store.transaction() as state:
            self._set_device(state, device_id, allowed=True, source_request_id=source_request_id)
            self.store.commit(state)
            return {"device_id": device_id, "media_authorized": True}

    def _expire(self, state):
        for attempt in state["attempts"].values():
            ident = attempt["attempt_id"]
            pin = self._pins.get(ident)
            if attempt["status"] not in TERMINAL and (self.clock() >= attempt["expires_at"] or pin and self.monotonic() >= pin[1]):
                self._wipe(ident)
                attempt.update(status="cancellation_pending", reason="expired_awaiting_sunshine_cleanup", updated_at=self.clock())
        # Keep unresolved revocation records; bound ordinary terminal history.
        expired = sorted((a for a in state["attempts"].values() if a["status"] in {"failed", "expired", "cancelled"}),
                         key=lambda a: a["updated_at"])
        while len(state["attempts"]) >= MAX_ATTEMPTS and expired:
            state["attempts"].pop(expired.pop(0)["attempt_id"], None)

    def discover(self, device_id, client_cert_sha256, *, authorize, pairing_intent=False):
        """Find only this claimed fingerprint's unique pending request.

        The fork suspends its getservercert response until the PIN, so the
        request UUID is otherwise unavailable to the app. This read grants
        nothing and never exposes another pending request or certificate.
        """
        _authorize(authorize)
        if pairing_intent is not True: raise MediaPairingError("media_pairing_intent_required", 400)
        if not _match(_DEVICE, device_id) or not _match(_SHA, client_cert_sha256):
            raise MediaPairingError("media_pairing_invalid_binding", 400)
        with self.store.transaction() as state:
            existing = state["bindings"].get(client_cert_sha256)
            if existing and existing["device_id"] != device_id:
                raise MediaPairingError("media_pairing_binding_mismatch", 403)
            rows = self.ipc.request("pairing.list")["requests"]
            matched = [row for row in rows if row["client_cert_sha256"] == client_cert_sha256
                       and row["status"] == "pending" and row["expires_in_ms"] > 0]
            _authorize(authorize)
            if len(matched) != 1: raise MediaPairingError("media_pairing_request_not_unique", 409)
            return dict(matched[0])

    def submit(self, device_id, payload, *, authorize):
        _authorize(authorize)
        if not _match(_DEVICE, device_id) or not isinstance(payload, dict) or set(payload) != {"request_id", "client_cert_sha256", "pin"}:
            raise MediaPairingError("media_pairing_invalid_request", 400)
        request_id, fingerprint = payload["request_id"], payload["client_cert_sha256"]
        _binding(request_id, fingerprint)
        pin = payload["pin"]
        if not isinstance(pin, str) or not re.fullmatch(r"[0-9]{4}", pin): raise MediaPairingError("media_pairing_invalid_pin", 400)
        with self.store.transaction() as state:
            self._expire(state);self.store.commit(state)
            for previous in state["attempts"].values():
                if previous["request_id"] != request_id: continue
                # A repeated submission is the same attempt, never a second PIN.
                if previous["device_id"] != device_id or previous["client_cert_sha256"] != fingerprint:
                    raise MediaPairingError("media_pairing_binding_mismatch", 403)
                if previous["status"] == "awaiting_local_approval":
                    self._remember_pin(previous, pin, authorize)
                    self._poll(state, previous, authorize)
                self.store.commit(state)
                return self._view(state, previous)
            if state["bindings"].get(fingerprint): raise MediaPairingError("media_certificate_already_associated", 409)
            if len(state["attempts"]) >= MAX_ATTEMPTS or sum(a["status"] not in TERMINAL for a in state["attempts"].values()) >= 8:
                raise MediaPairingError("media_pairing_capacity", 429)
            remote = self.ipc.request("pairing.pending", request_id=request_id, client_cert_sha256=fingerprint)["request"]
            if remote["status"] != "pending" or remote["expires_in_ms"] <= 0:
                raise MediaPairingError("media_pairing_request_not_pending", 409)
            _authorize(authorize)
            allowed = self._allowed(state, device_id)
            now = self.clock();ident = str(uuid.uuid4())
            attempt = {"attempt_id": ident, "device_id": device_id, "request_id": request_id,
                       "client_cert_sha256": fingerprint, "status": "pending" if allowed else "awaiting_local_approval",
                       "created_at": now, "expires_at": now + min(MAX_TTL, remote["expires_in_ms"] / 1000),
                       "updated_at": now, "reason": "" if allowed else "remote_media_approval_required"}
            state["attempts"][ident] = attempt
            self._remember_pin(attempt, pin, authorize);self.store.commit(state)
            if allowed: self._approve(state, attempt, authorize)
            self.store.commit(state)
            return self._view(state, attempt)

    def _approve(self, state, attempt, authorize):
        ident = attempt["attempt_id"]
        _authorize(authorize)
        if not self._allowed(state, attempt["device_id"]): raise MediaPairingError("media_permission_required", 403)
        remote = self._request(attempt, "pairing.pending")
        if remote["status"] != "pending" or remote["expires_in_ms"] <= 0 or self.clock() >= attempt["expires_at"]:
            self._cancel(state, attempt, "expired");return
        _authorize(authorize)
        stored = self._pins.pop(ident, None)
        if stored is None or self.monotonic() >= stored[1]:
            attempt.update(status="awaiting_local_approval", reason="pin_resubmission_required", updated_at=self.clock())
            self._wipe(ident);return
        secret = stored[0]
        attempt.update(status="submitting", reason="", updated_at=self.clock())
        self.store.commit(state)  # Durable single-use barrier before the IPC send.
        try:
            result = self.ipc.request("pairing.approve", request_id=attempt["request_id"],
                client_cert_sha256=attempt["client_cert_sha256"], pin=secret.decode("ascii"),
                name=attempt["device_id"].encode()[:80].decode("utf-8", errors="ignore"))
            _authorize(authorize)
            if result["status"] != "awaiting_client_proof": raise MediaPairingError("media_pairing_invalid_response", 502)
            attempt.update(status="awaiting_client_proof", updated_at=self.clock())
        except MediaPairingError as error:
            if error.status == 401:
                self._cancel(state, attempt, "cancelled")
            else:
                # An interrupted IPC may already have spent the PIN. Never
                # resend; poll this exact binding or cancel and start again.
                attempt.update(status="awaiting_client_proof", reason="approval_outcome_unknown", updated_at=self.clock())
        finally:
            secret[:] = b"\0" * len(secret)
        self.store.commit(state)

    def approve_local(self, attempt_id, *, request_id, client_cert_sha256, local_authorize):
        """Plugin/Unix-local authority only; never a public companion operation."""
        _authorize(local_authorize);_binding(request_id, client_cert_sha256)
        with self.store.transaction() as state:
            self._expire(state);self.store.commit(state)
            attempt = state["attempts"].get(attempt_id)
            if not attempt or attempt["request_id"] != request_id or attempt["client_cert_sha256"] != client_cert_sha256:
                raise MediaPairingError("media_pairing_binding_mismatch", 403)
            if attempt["status"] != "awaiting_local_approval":
                self.store.commit(state);return self._view(state, attempt)
            authorize = self._authorizers.get(attempt_id)
            _authorize(authorize)
            self._set_device(state, attempt["device_id"], allowed=True)
            self.store.commit(state);self._approve(state, attempt, authorize)
            self.store.commit(state);return self._view(state, attempt)

    def _cancel(self, state, attempt, status="cancelled"):
        self._wipe(attempt["attempt_id"])
        if attempt["status"] == "paired":
            self._mark_revocation(state, attempt);return
        if attempt["status"] in {"cancelled", "expired", "failed", "revocation_pending"}: return
        try:
            if self._request(attempt, "pairing.cancel")["status"] != "cancelled": raise MediaPairingError()
            reason = ""
        except MediaPairingError:
            try:
                remote = self._request(attempt, "pairing.pending")
                if remote["status"] == "paired": self._mark_revocation(state, attempt);return
                if remote["status"] in {"cancelled", "expired", "failed"}:
                    attempt.update(status=status, reason="", updated_at=self.clock());return
            except MediaPairingError: pass
            status, reason = "cancellation_pending", "sunshine_cancel_unconfirmed"
        attempt.update(status=status, reason=reason, updated_at=self.clock())

    def _bind(self, state, attempt, status):
        fp = attempt["client_cert_sha256"]
        existing = state["bindings"].get(fp)
        if existing and existing["device_id"] != attempt["device_id"]:
            raise MediaPairingError("media_pairing_binding_mismatch", 403)
        state["bindings"][fp] = {"device_id": attempt["device_id"], "client_cert_sha256": fp,
            "request_id": attempt["request_id"],
            "paired_at": existing["paired_at"] if existing else self.clock(), "status": status}

    def _probe_revocation(self):
        """Ask the fork once whether it can revoke a certificate at all.

        A transport failure leaves the answer unknown rather than recording a
        "no": the socket being down is not the fork saying it cannot revoke.
        """
        if self._revocation_supported is True:
            return True
        try:
            self._revocation_supported = self.ipc.request("pairing.list").get("certificate_revocation_supported") is True
        except MediaPairingError:
            pass
        return self._revocation_supported is True

    def _revoke_certificate(self, fingerprint):
        """Take one certificate back in the fork. True only when it is gone.

        `not_found` counts as gone: the fork does not authorize it either way,
        which is exactly what the pending record was waiting for.
        """
        if not _match(_SHA, fingerprint):
            return False
        try:
            status = self.ipc.request("pairing.revoke", client_cert_sha256=fingerprint)["status"]
        except MediaPairingError as error:
            if error.code == "unknown_operation":
                self._revocation_supported = False
            return False
        self._revocation_supported = True
        return status in {"revoked", "not_found"}

    def _reconcile(self, state, budget=32):
        """Finish the revocations an older fork could not do.

        Every revoke before PLUG-4 left a `revocation_pending` record *and* left
        the certificate authorized in Sunshine, so a revoked device could still
        reach the GameStream port directly. Those records are not decisions to
        re-take - the user already revoked them - they are unfinished work, and
        this is where it finishes once a fork that can revoke is running.
        """
        if not self._probe_revocation():
            return False
        changed = False
        for fingerprint in [key for key, row in state["bindings"].items()
                            if row["status"] == "revocation_pending"][:budget]:
            if not self._revoke_certificate(fingerprint):
                continue
            state["bindings"].pop(fingerprint, None)
            changed = True
        # The binding is the record of the certificate; once it is gone its
        # attempt is a tombstone for something nobody has to chase any more.
        for ident in [row["attempt_id"] for row in state["attempts"].values()
                      if row["status"] == "revocation_pending"]:
            if state["attempts"][ident]["client_cert_sha256"] in state["bindings"]:
                continue
            state["attempts"].pop(ident, None)
            self._wipe(ident)
            changed = True
        return changed

    def _mark_revocation(self, state, attempt):
        """Take the certificate back, and only claim it when it is really gone."""
        fingerprint = attempt["client_cert_sha256"]
        self._wipe(attempt["attempt_id"])
        if self._revoke_certificate(fingerprint):
            # Nothing is left to chase, so nothing is left on the record: a
            # "revoked" tombstone is exactly the clutter PLUG-4 removes. The
            # status is still written onto the object a caller is holding, so
            # the answer to *this* call says what happened.
            attempt.update(status="revoked", reason="", updated_at=self.clock())
            state["bindings"].pop(fingerprint, None)
            state["attempts"].pop(attempt["attempt_id"], None)
            return
        self._bind(state, attempt, "revocation_pending")
        attempt.update(status="revocation_pending", reason="sunshine_certificate_revocation_unavailable",
                       updated_at=self.clock())

    def _poll(self, state, attempt, authorize):
        _authorize(authorize)
        if attempt["status"] in TERMINAL: return
        if attempt["status"] == "cancellation_pending":
            self._cancel(state, attempt, "expired" if attempt["reason"].startswith("expired_") else "cancelled");return
        if attempt["status"] == "awaiting_local_approval":
            # A companion claim can race the local approval. Once it arrives,
            # finish without asking the user a second time.
            if not self._allowed(state, attempt["device_id"]): return
            if attempt["attempt_id"] not in self._pins:
                attempt["reason"] = "pin_resubmission_required";return
            self._approve(state, attempt, authorize);return
        if not self._allowed(state, attempt["device_id"]):
            self._cancel(state, attempt);return
        if self.clock() >= attempt["expires_at"]:
            self._cancel(state, attempt, "expired");return
        remote = self._request(attempt, "pairing.pending")
        _authorize(authorize)
        if remote["status"] == "paired":
            if attempt["status"] not in {"submitting", "awaiting_client_proof"}:
                self._mark_revocation(state, attempt);return
            self._bind(state, attempt, "paired")
            attempt.update(status="paired", reason="", updated_at=self.clock());self._wipe(attempt["attempt_id"])
        elif remote["status"] in {"cancelled", "expired", "failed"}:
            attempt.update(status=remote["status"], reason="", updated_at=self.clock());self._wipe(attempt["attempt_id"])

    def status(self, device_id, attempt_id, *, authorize):
        _authorize(authorize)
        with self.store.transaction() as state:
            attempt = state["attempts"].get(attempt_id)
            if not attempt or attempt["device_id"] != device_id: raise MediaPairingError("media_pairing_not_found", 404)
            if attempt["status"] not in TERMINAL: self._authorizers[attempt_id] = authorize
            try: self._poll(state, attempt, authorize)
            except MediaPairingError as error:
                if error.status == 401: self._cancel(state, attempt)
                self.store.commit(state)
                raise
            self.store.commit(state)
            return self._view(state, attempt)

    def cancel(self, device_id, attempt_id, *, authorize):
        """Public cancel: the credential's own device, by attempt."""
        _authorize(authorize)
        with self.store.transaction() as state:
            attempt = state["attempts"].get(attempt_id)
            if not attempt or attempt["device_id"] != device_id: raise MediaPairingError("media_pairing_not_found", 404)
            self._cancel(state, attempt);self.store.commit(state);return self._view(state, attempt)

    def cancel_local(self, attempt_id, *, request_id, client_cert_sha256, local_authorize):
        """Local cancel: the exact binding the plugin is showing."""
        _authorize(local_authorize);_binding(request_id, client_cert_sha256)
        with self.store.transaction() as state:
            attempt = state["attempts"].get(attempt_id)
            if not attempt or attempt["request_id"] != request_id or attempt["client_cert_sha256"] != client_cert_sha256:
                raise MediaPairingError("media_pairing_binding_mismatch", 403)
            self._cancel(state, attempt);self.store.commit(state);return self._view(state, attempt)

    def pending_local(self, *, local_authorize):
        """The actionable attempts, and separately the records that just linger.

        PAIR-3 §2.2: `requests` is what a panel can act on; `history` is the
        `revocation_pending`/`cancellation_pending` residue a revoked device
        leaves behind forever. They used to share one list, and a reader that
        choked on one of them lost the whole thing.
        """
        _authorize(local_authorize)
        with self.store.transaction() as state:
            self._expire(state)
            # PLUG-4 §2.1: catch up the revocations an older fork left behind,
            # every time the list is read. A panel that shows this list is
            # exactly where those records would otherwise pile up forever.
            self._reconcile(state)
            self.store.commit(state)
            views = [self._view(state, item) for item in state["attempts"].values()
                     if item["status"] not in {"failed", "expired", "cancelled"}]
            return {"requests": [item for item in views if item["status"] not in HISTORY_STATUSES],
                    "history": [item for item in views if item["status"] in HISTORY_STATUSES],
                    "certificate_revocation_supported": self._revocation_supported is True}

    def authorized_devices(self):
        """Which devices the media store currently allows, and from which
        approval. This is what makes "companion yes, streaming no" visible in
        `devices list` instead of only on the iPad."""
        with self.store.transaction() as state:
            return {device: row.get("source_request_id") or ""
                    for device, row in state["devices"].items() if row.get("allowed") is True}

    def revoke_device(self, device_id, *, local_authorize):
        """Deny locally first, then attempt cleanup, and report what is unresolved."""
        _authorize(local_authorize)
        if not _match(_DEVICE, device_id): raise MediaPairingError("media_pairing_invalid_request", 400)
        with self.store.transaction() as state:
            row = self._set_device(state, device_id, allowed=False)
            # `_mark_revocation` may now remove the record it is given, so the
            # iteration cannot be over the live mapping.
            for attempt in list(state["attempts"].values()):
                if attempt["device_id"] != device_id: continue
                self._wipe(attempt["attempt_id"])
                if attempt["status"] == "paired": self._mark_revocation(state, attempt)
                elif attempt["status"] not in TERMINAL:
                    attempt.update(status="cancellation_pending", reason="media_grant_revoked", updated_at=self.clock())
            for fingerprint, binding in list(state["bindings"].items()):
                if binding["device_id"] != device_id: continue
                binding["status"] = "revocation_pending"
                # A binding with no attempt left (or one already marked) is
                # still a certificate the fork authorizes. Take it back too.
                if self._revoke_certificate(fingerprint): state["bindings"].pop(fingerprint, None)
            self._reconcile(state)
            pending = sum(b["device_id"] == device_id and b["status"] == "revocation_pending" for b in state["bindings"].values())
            pending += sum(a["device_id"] == device_id and a["status"] == "cancellation_pending" for a in state["attempts"].values())
            self.store.commit(state)
            return {"device_id": device_id, "media_authorized": False, "pending_media_revocations": pending,
                    "certificate_revocation_supported": self._revocation_supported is True,
                    # Complete means nothing is left unresolved. Against a fork
                    # that cannot revoke, that still needs a grant on record to
                    # be a claim about anything (PAIR-3); against one that can,
                    # the fork's own answer is the claim.
                    "media_revocation_complete": pending == 0 and (self._revocation_supported is True
                                                                   or row["source_request_id"] != "")}

    def purge_device(self, device_id, *, local_authorize):
        """Forget a device that is already denied, once nothing is unresolved.

        PLUG-4 §2.2: a revoked device used to leave a permanent deny row, a
        `revocation_pending` binding and a terminal attempt behind, on every
        spec's throwaway test device, so the Devices page grew to 33 rows of
        corpses. What a purge may remove is only what is *finished*: a binding
        whose certificate the fork has really taken back, and an attempt that
        is over. Anything still unresolved stays, and says so.
        """
        _authorize(local_authorize)
        if not _match(_DEVICE, device_id): raise MediaPairingError("media_pairing_invalid_request", 400)
        with self.store.transaction() as state:
            if self._allowed(state, device_id):
                # Purging an authorized device would silently drop a live grant.
                raise MediaPairingError("media_pairing_device_authorized", 409)
            removed_bindings = 0
            for fingerprint, binding in list(state["bindings"].items()):
                if binding["device_id"] != device_id: continue
                if not self._revoke_certificate(fingerprint): continue
                state["bindings"].pop(fingerprint, None)
                removed_bindings += 1
            # One unresolved certificate is one item, not two: the attempt that
            # names a binding still waiting on the fork is kept, but it is the
            # same unfinished revocation the binding already stands for.
            unresolved = sum(row["device_id"] == device_id for row in state["bindings"].values())
            removed_attempts = 0
            for ident in [row["attempt_id"] for row in state["attempts"].values() if row["device_id"] == device_id]:
                attempt = state["attempts"][ident]
                if attempt["status"] not in TERMINAL:
                    unresolved += 1
                    continue
                if attempt["client_cert_sha256"] in state["bindings"]:
                    continue
                state["attempts"].pop(ident, None)
                self._wipe(ident)
                removed_attempts += 1
            if unresolved == 0: state["devices"].pop(device_id, None)
            self.store.commit(state)
            return {"device_id": device_id, "purged": unresolved == 0,
                    "removed_bindings": removed_bindings, "removed_attempts": removed_attempts,
                    "pending_media_revocations": unresolved}

    def certificates(self, *, local_authorize, purge_unknown=False):
        """What the fork actually authorizes, next to what this host knows.

        PLUG-4 follow-up: three certificates were sitting in the fork's client
        store that core had no record of - older than managed pairing, so no
        `devices revoke` would ever name them, and each one still good for a
        direct stream. They were removed by hand, by reading the fork's private
        state file and hashing the PEMs. This is that operation as a command.

        `purge_unknown` revokes only the rows no binding of this host claims.
        A certificate core knows is never touched here; taking that one back is
        `devices revoke`, which also takes back the credential behind it.
        """
        _authorize(local_authorize)
        if purge_unknown is not True and purge_unknown is not False:
            raise MediaPairingError("media_pairing_invalid_request", 400)
        with self.store.transaction() as state:
            self._reconcile(state)
            known = {fingerprint: row["device_id"] for fingerprint, row in state["bindings"].items()}
            listed = self.ipc.request("pairing.clients")
            rows, revoked = [], []
            for item in listed["clients"]:
                fingerprint = item["client_cert_sha256"]
                device = known.get(fingerprint)
                row = {"client_cert_sha256": fingerprint, "name": item["name"], "enabled": item["enabled"],
                       "device_id": device or "", "known": device is not None}
                if device is None and purge_unknown:
                    row["revoked"] = self._revoke_certificate(fingerprint)
                    if row["revoked"]: revoked.append(fingerprint)
                rows.append(row)
            self.store.commit(state)
            return {"certificates": rows,
                    "unknown": sum(1 for row in rows if not row["known"]),
                    "revoked": revoked,
                    # A record whose certificate does not parse has no
                    # fingerprint, so nothing can revoke it by one; it is
                    # reported rather than quietly counted as clean.
                    "unreadable": listed["unreadable"], "truncated": listed["truncated"],
                    "certificate_revocation_supported": self._revocation_supported is True}

    def device_last_change(self, device_id):
        """When this device's media permission last changed, or None.

        It is the only timestamp the host keeps per device: the credential
        registry has none, so it is also what `devices purge --older-than`
        measures a device's age by.
        """
        if not _match(_DEVICE, device_id): return None
        with self.store.transaction() as state:
            row = state["devices"].get(device_id)
            return float(row["updated_at"]) if row else None

    def media_authorized(self, device_id, client_cert_sha256):
        """This exact certificate is paired for this device and not revoked."""
        if not _match(_DEVICE, device_id) or not _match(_SHA, client_cert_sha256): return False
        with self.store.transaction() as state:
            binding = state["bindings"].get(client_cert_sha256, {})
            return (self._allowed(state, device_id) and binding.get("device_id") == device_id
                    and binding.get("status") == "paired")

    def remote_denied(self, device_id):
        """Distinguish an explicit local revoke from never having been granted."""
        if not _match(_DEVICE, device_id): return True
        with self.store.transaction() as state:
            row = state["devices"].get(device_id)
            return row is not None and row["allowed"] is False

    def paired_certificate(self, device_id):
        """Return only a unique final association; never guess among certificates."""
        if not _match(_DEVICE, device_id): return None
        with self.store.transaction() as state:
            if not self._allowed(state, device_id): return None
            matches = [fp for fp, row in state["bindings"].items()
                       if row["device_id"] == device_id and row["status"] == "paired"]
            return matches[0] if len(matches) == 1 else None

    def maintenance(self):
        # Single-flight, at most two remote reconciliations per tick.
        if not self._maintenance_lock.acquire(blocking=False): return
        try:
            with self.store.transaction() as state:
                self._expire(state)
                # The leftovers of every revoke made against a fork that could
                # not revoke. This is the "catch up at startup" half of §2.1:
                # the service runs maintenance on its own tick, with no panel
                # and no companion involved.
                self._reconcile(state)
                self.store.commit(state)
                budget = 2
                for attempt in list(state["attempts"].values()):
                    if attempt["status"] in TERMINAL or budget <= 0: continue
                    authorizer = self._authorizers.get(attempt["attempt_id"])
                    if attempt["status"] == "cancellation_pending":
                        self._cancel(state, attempt, "expired" if attempt["reason"].startswith("expired_") else "cancelled")
                        budget -= 1
                    elif authorizer:
                        try: self._poll(state, attempt, authorizer)
                        except MediaPairingError as error:
                            if error.status == 401: self._cancel(state, attempt)
                        budget -= 1
                self.store.commit(state)
        finally: self._maintenance_lock.release()

    def close(self):
        """Clear transient secrets on daemon shutdown; never revoke user trust."""
        with self.store.mutex:
            for ident in list(self._pins): self._wipe(ident)
            self._authorizers.clear()
