"""Device credentials with an atomic, process-shared hash registry."""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
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
from typing import Iterator

from .protocol import PLUGIN_DEVICE_ID

_DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_TOKEN_HASH = re.compile(r"[0-9a-f]{64}\Z")
# The display name a device chose when it paired. It is not an identity and
# nothing authorizes on it; it exists so a second device can be told *who*
# holds the single Remote session instead of an opaque device_id.
_DEVICE_NAME_LIMIT = 80


# CORE-2 §1. A device credential lives 30 days. In its last 7 the device may
# trade it for a new one (`POST /v1/pairing/renew`); the one it traded in keeps
# working for 24 h so a reply lost on the way back does not strand the device.
DEFAULT_TTL_SECONDS = 30 * 24 * 3600
DEFAULT_RENEW_WINDOW_SECONDS = 7 * 24 * 3600
DEFAULT_GRACE_SECONDS = 24 * 3600

# Why a credential was refused, as the 401 body says it
# (`http-error.schema.json` `error.reason`). Four answers, because the app has
# a different next step for each: an expired credential is one approval away
# from working again, a revoked one was taken back on purpose.
CREDENTIAL_REASONS = ("credential_expired", "credential_revoked", "device_purged", "unknown_credential")


class CredentialError(ValueError):
    """A refused credential, with the reason the 401 body carries.

    It stays a `ValueError` whose text is the old "invalid device credential"
    so every caller that only asks "was it refused" keeps working unchanged.
    """

    def __init__(self, reason: str):
        if reason not in CREDENTIAL_REASONS:
            reason = "unknown_credential"
        self.reason = reason
        super().__init__("invalid device credential")


class CredentialRenewalRefused(ValueError):
    """`POST /v1/pairing/renew` said no to a credential that is still valid."""

    def __init__(self, code: str, *, expires_at: int | None = None, renewable_at: int | None = None):
        self.code, self.expires_at, self.renewable_at = code, expires_at, renewable_at
        super().__init__(code)


@dataclass(frozen=True)
class DeviceCredential:
    device_id: str
    token: str
    issued_at: int
    expires_at: int | None = None


def _private_open(path: Path, flags: int) -> int:
    """Open a private regular file without following a final-component symlink."""
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError("credential file must be a regular file owned by this user")
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _registry_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = _private_open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class DeviceAuthenticator:
    def __init__(self, secret: bytes | None = None, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 state_path: str | Path | None = None, *,
                 grace_seconds: int = DEFAULT_GRACE_SECONDS,
                 renew_window_seconds: int = DEFAULT_RENEW_WINDOW_SECONDS):
        if secret is not None and (not isinstance(secret, bytes) or len(secret) != 32):
            raise ValueError("credential secret must contain 32 bytes")
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise ValueError("credential TTL must be a positive integer")
        if type(grace_seconds) is not int or grace_seconds < 0:
            raise ValueError("credential grace must be a non-negative integer")
        if type(renew_window_seconds) is not int or renew_window_seconds <= 0:
            raise ValueError("credential renewal window must be a positive integer")
        self.secret = secret if secret is not None else secrets.token_bytes(32)
        self.ttl_seconds = ttl_seconds
        self.grace_seconds = grace_seconds
        self.renew_window_seconds = renew_window_seconds
        self._state_path = Path(state_path) if state_path is not None else None
        self._issued: dict[str, str] = {}
        self._revoked: set[str] = set()
        self._names: dict[str, str] = {}
        # CORE-2. When each hash was issued and, once it was traded in, when it
        # stops working. Neither is a secret: `iat` is in the token's own
        # payload. A registry written before these existed simply does not know
        # the `iat` of its older hashes yet; `verify` learns it the first time
        # the token comes back (it is in the token), so `devices list` can say
        # when a credential expires without anybody re-pairing.
        self._issued_at: dict[str, int] = {}
        self._retire_at: dict[str, int] = {}
        with self._transaction():
            pass

    @classmethod
    def from_file(cls, path: str | Path, **kwargs) -> "DeviceAuthenticator":
        secret_path = Path(path)
        registry = secret_path.with_suffix(".credentials.json")
        # The same lock protects initial secret creation and registry updates.
        with _registry_lock(registry):
            try:
                fd = _private_open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                fd = _private_open(secret_path, os.O_RDONLY)
                with os.fdopen(fd, "rb") as stream:
                    secret = stream.read(33)
            else:
                secret = secrets.token_bytes(32)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(secret)
                    stream.flush()
                    os.fsync(stream.fileno())
            if len(secret) != 32:
                raise ValueError("invalid authenticator secret")
        return cls(secret, state_path=registry, **kwargs)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self._state_path is None:
            yield
            return
        with _registry_lock(self._state_path):
            self._reload_locked()
            yield

    def _reload_locked(self) -> None:
        assert self._state_path is not None
        try:
            fd = _private_open(self._state_path, os.O_RDONLY)
        except FileNotFoundError:
            self._issued, self._revoked, self._names = {}, set(), {}
            self._issued_at, self._retire_at = {}, {}
            return
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("credential registry exceeds size limit")
        try:
            data = json.loads(raw)
            issued, revoked = data["issued"], data["revoked"]
            # A registry written before names existed is not an invalid registry.
            names = data.get("names", {})
            if (not isinstance(names, dict)
                    or any(not isinstance(k, str) or not _DEVICE_ID.fullmatch(k)
                           or not isinstance(v, str) or not 1 <= len(v) <= _DEVICE_NAME_LIMIT
                           or any(ord(c) < 32 or ord(c) == 127 for c in v)
                           for k, v in names.items())):
                raise ValueError("invalid registry fields")
            if (not isinstance(issued, dict) or not isinstance(revoked, list)
                    or any(not isinstance(k, str) or not _TOKEN_HASH.fullmatch(k)
                           or not isinstance(v, str) or not _DEVICE_ID.fullmatch(v)
                           for k, v in issued.items())
                    or any(not isinstance(v, str) or not _TOKEN_HASH.fullmatch(v) for v in revoked)):
                raise ValueError("invalid registry fields")
            # CORE-2: both optional, so a registry from before them still loads.
            issued_at, retire_at = data.get("issued_at", {}), data.get("retire_at", {})
            for stamps in (issued_at, retire_at):
                if (not isinstance(stamps, dict)
                        or any(not isinstance(k, str) or not _TOKEN_HASH.fullmatch(k)
                               or type(v) is not int or v < 0 for k, v in stamps.items())):
                    raise ValueError("invalid registry fields")
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("invalid credential registry") from exc
        self._issued, self._revoked, self._names = dict(issued), set(revoked), dict(names)
        self._issued_at = {k: v for k, v in issued_at.items() if k in self._issued}
        self._retire_at = {k: v for k, v in retire_at.items() if k in self._issued}

    def _save_locked(self) -> None:
        if self._state_path is None:
            return
        path = self._state_path
        if path.is_symlink():
            raise PermissionError("refusing credential registry symlink")
        fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({"issued": self._issued, "revoked": sorted(self._revoked),
                           "names": self._names, "issued_at": self._issued_at,
                           "retire_at": self._retire_at},
                          stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _name(value):
        if (not isinstance(value, str) or not 1 <= len(value) <= _DEVICE_NAME_LIMIT
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError("invalid device_name")
        return value

    def _mint(self, device_id: str, issued_at: int) -> tuple[str, str]:
        payload = {"v": 1, "device_id": device_id, "iat": issued_at, "nonce": secrets.token_urlsafe(12)}
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        signature = hmac.new(self.secret, body.encode(), hashlib.sha256).digest()
        token = body + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")
        return token, hashlib.sha256(token.encode()).hexdigest()

    def _expires_locked(self, digest: str, device_id: str | None = None) -> int | None:
        """When this hash stops working, or None when its `iat` is not known yet.

        The panel's own credential has no TTL expiry on the same-UID socket
        (see `verify`), so it has no expiry to report either.
        """
        if device_id == PLUGIN_DEVICE_ID:
            return self._retire_at.get(digest)
        issued_at = self._issued_at.get(digest)
        ends = [value for value in (None if issued_at is None else issued_at + self.ttl_seconds,
                                    self._retire_at.get(digest)) if value is not None]
        return min(ends) if ends else None

    def _drop_dead_locked(self, device_id: str, current: int) -> None:
        """Forget this device's credentials that can never work again.

        A device that re-pairs after its credential expired would otherwise
        carry every expired hash for ever. Revoked hashes stay: a replayed
        revoked token must keep saying "revoked".
        """
        for digest, owner in list(self._issued.items()):
            if owner != device_id or digest in self._revoked:
                continue
            ends = self._expires_locked(digest, owner)
            if ends is not None and current >= ends:
                self._issued.pop(digest, None)
                self._issued_at.pop(digest, None)
                self._retire_at.pop(digest, None)

    def issue(self, device_id: str, now: int | None = None, device_name: str | None = None) -> DeviceCredential:
        if not isinstance(device_id, str) or not _DEVICE_ID.fullmatch(device_id):
            raise ValueError("invalid device_id")
        if device_name is not None:
            device_name = self._name(device_name)
        issued_at = int(time.time() if now is None else now)
        token, digest = self._mint(device_id, issued_at)
        with self._transaction():
            self._drop_dead_locked(device_id, issued_at)
            self._issued[digest] = device_id
            self._issued_at[digest] = issued_at
            if device_name is not None:
                self._names[device_id] = device_name
            self._save_locked()
        return DeviceCredential(device_id, token, issued_at, issued_at + self.ttl_seconds)

    def device_name(self, device_id: str) -> str | None:
        """The display name this device paired under, or None."""
        if not isinstance(device_id, str):
            return None
        with self._transaction():
            return self._names.get(device_id)

    def _decode(self, token: str) -> tuple[str, int, str]:
        """(device_id, iat, hash) of a token this host signed, or CredentialError."""
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= 4096:
                raise ValueError("invalid token size")
            body, encoded_signature = token.split(".")
            signature = base64.b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4),
                                         altchars=b"-_", validate=True)
            expected = hmac.new(self.secret, body.encode("ascii"), hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("invalid signature")
            payload = json.loads(base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_", validate=True))
            device_id, issued_at = payload["device_id"], payload["iat"]
            if (payload.get("v") != 1 or not isinstance(device_id, str)
                    or not _DEVICE_ID.fullmatch(device_id) or type(issued_at) is not int):
                raise ValueError("invalid token fields")
        except (ValueError, KeyError, TypeError, UnicodeError, binascii.Error, AttributeError) as exc:
            # Not a credential this host signed: a stranger's, a truncated one,
            # or one signed by a device.secret this host no longer has.
            raise CredentialError("unknown_credential") from exc
        return device_id, issued_at, hashlib.sha256(token.encode()).hexdigest()

    def verify(self, token: str, now: int | None = None, *, local: bool = False) -> str:
        """Reload registry for each check, including checks on a live event stream.

        CORE-2 §1: a refusal says why (`CredentialError.reason`). The order is
        the order of what the app can do about it: a revoked hash is revoked
        even if it has also expired; a hash that is gone from the registry is a
        purged device unless its own `iat` already says it expired.

        `local` is the same-UID Unix socket, where the peer's uid is the real
        authority. There - and only there - the panel's own credential does not
        expire: nothing on the host could renew it, and a panel that goes dark
        on day 30 of every install is not a security property.
        """
        device_id, issued_at, token_hash = self._decode(token)
        try:
            current = int(time.time() if now is None else now)
            if current < issued_at:
                raise CredentialError("unknown_credential")
            ageless = local and device_id == PLUGIN_DEVICE_ID
            expired = not ageless and current - issued_at >= self.ttl_seconds
            with self._transaction():
                if token_hash in self._revoked:
                    raise CredentialError("credential_revoked")
                if self._issued.get(token_hash) != device_id:
                    raise CredentialError("credential_expired" if expired else "device_purged")
                if self._issued_at.get(token_hash) != issued_at:
                    self._issued_at[token_hash] = issued_at
                    self._save_locked()
                retire = self._retire_at.get(token_hash)
                if expired or (retire is not None and current >= retire):
                    raise CredentialError("credential_expired")
            return device_id
        except CredentialError:
            raise
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise CredentialError("unknown_credential") from exc

    def credential_info(self, token: str, now: int | None = None) -> dict:
        """What `GET /v1/pairing/credential` answers about the caller's own credential."""
        device_id = self.verify(token, now)
        current = int(time.time() if now is None else now)
        _, issued_at, token_hash = self._decode(token)
        with self._transaction():
            expires_at = self._expires_locked(token_hash, device_id)
            retiring = token_hash in self._retire_at
        renewable_at = None if expires_at is None else expires_at - self.renew_window_seconds
        return {"device_id": device_id, "issued_at": issued_at, "expires_at": expires_at,
                "renewable_at": renewable_at,
                "renewable": (device_id != PLUGIN_DEVICE_ID and renewable_at is not None
                              and current >= renewable_at),
                # A credential that was already traded in: it works until
                # `expires_at`, and the device should be using the new one.
                "superseded": retiring}

    def renew(self, token: str, now: int | None = None) -> DeviceCredential:
        """Trade a credential near its end for a new one; the old one gets a grace period.

        Only a credential that still verifies can be traded in, so a revoked or
        purged device (or an expired credential) never gets here - those are
        401s. Every other live credential of the device is retired too, with
        the same grace, so a device ends up holding exactly one credential.
        """
        device_id = self.verify(token, now)
        if device_id == PLUGIN_DEVICE_ID:
            raise CredentialRenewalRefused("plugin_credential")
        current = int(time.time() if now is None else now)
        _, _, token_hash = self._decode(token)
        with self._transaction():
            expires_at = self._expires_locked(token_hash, device_id)
            if expires_at is None:
                raise CredentialError("unknown_credential")
            renewable_at = expires_at - self.renew_window_seconds
            if current < renewable_at:
                raise CredentialRenewalRefused("credential_renewal_not_due", expires_at=expires_at,
                                               renewable_at=renewable_at)
            fresh, fresh_hash = self._mint(device_id, current)
            retire = current + self.grace_seconds
            for digest, owner in self._issued.items():
                if owner == device_id and digest not in self._revoked:
                    self._retire_at[digest] = min(self._retire_at.get(digest, retire), retire)
            self._drop_dead_locked(device_id, current)
            self._issued[fresh_hash] = device_id
            self._issued_at[fresh_hash] = current
            self._save_locked()
        return DeviceCredential(device_id, fresh, current, current + self.ttl_seconds)

    def revoke(self, token: str) -> None:
        if not isinstance(token, str):
            raise ValueError("token must be a string")
        with self._transaction():
            self._revoked.add(hashlib.sha256(token.encode()).hexdigest())
            self._save_locked()

    def revoke_device(self, device_id: str) -> int:
        with self._transaction():
            matching = {digest for digest, owner in self._issued.items() if owner == device_id}
            self._revoked.update(matching)
            # A revoked device keeps nothing, including the name it displayed.
            self._names.pop(device_id, None)
            self._save_locked()
            return len(matching)

    def purge_device(self, device_id: str) -> dict:
        """Forget a revoked device completely, hashes and display name.

        `revoke_device` deliberately keeps the credential hashes: a replayed
        token has to keep being refused. Purging is the inventory operation for
        a device that is already revoked, and dropping the hash is still
        fail-closed - verification requires the hash to be present in `issued`
        and mapped to the device, so a removed hash authenticates nothing.
        A device with an active credential is never purged.
        """
        if not isinstance(device_id, str) or not _DEVICE_ID.fullmatch(device_id):
            raise ValueError("invalid device_id")
        with self._transaction():
            digests = {digest for digest, owner in self._issued.items() if owner == device_id}
            if any(digest not in self._revoked for digest in digests):
                raise ValueError("device still holds an active credential")
            for digest in digests:
                self._issued.pop(digest, None)
                self._revoked.discard(digest)
                self._issued_at.pop(digest, None)
                self._retire_at.pop(digest, None)
            named = self._names.pop(device_id, None) is not None
            self._save_locked()
            return {"device_id": device_id, "removed_credentials": len(digests), "removed_name": named}

    def list_devices(self, now: int | None = None) -> list[dict]:
        """Local-administrator inventory. Never return token hashes or tokens.

        CORE-2: an expired credential is no longer counted as active - it
        authenticates nothing, and counting it is what made the host refuse to
        re-pair a device whose only credential had run out
        (`pairing_device_exists`). `expires_at` is the latest moment one of the
        device's live credentials works until, or `null` while the host has not
        yet seen a credential from before CORE-2 come back.
        """
        current = int(time.time() if now is None else now)
        with self._transaction():
            devices = {}
            for digest, device_id in self._issued.items():
                row = devices.setdefault(device_id, {"device_id": device_id, "active_credentials": 0,
                                                     "revoked_credentials": 0, "expired_credentials": 0,
                                                     "_live": [], "_dead": []})
                ends = self._expires_locked(digest, device_id)
                if digest in self._revoked:
                    row["revoked_credentials"] += 1
                elif ends is not None and current >= ends:
                    row["expired_credentials"] += 1
                    row["_dead"].append(ends)
                else:
                    row["active_credentials"] += 1
                    row["_live"].append(ends)
            result = []
            for device_id, row in sorted(devices.items()):
                live, dead = row.pop("_live"), row.pop("_dead")
                known = [value for value in live if value is not None]
                if live:
                    # One live credential whose iat is not known yet makes the
                    # answer unknown rather than an under-estimate.
                    expires_at = max(known) if len(known) == len(live) else None
                else:
                    expires_at = max(dead) if dead else None
                status = ("authorized" if row["active_credentials"]
                          else "expired" if row["expired_credentials"] else "revoked")
                result.append({**row, "expires_at": expires_at, "device_name": self._names.get(device_id),
                               "status": status,
                               # `plugin` is the host's own panel credential, not a paired
                               # companion. The Devices page draws it as a label rather
                               # than as a row with a Remove button.
                               "role": "plugin" if device_id == PLUGIN_DEVICE_ID else "companion"})
            return result
