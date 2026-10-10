"""LITE-1b C6: a Web Push sender on `cryptography` alone.

RFC 8291 message encryption (`aes128gcm`, RFC 8188, one record) and RFC 8292
VAPID (an ES256 JWT, DER signature turned into r||s), with `urllib` doing
the POST. It is the LITE-0 spike sender (`spikes/lite0/webpush.py` on the
`lite-0` branch) made into a module: no pywebpush, py-vapid, http-ece or
requests, so the only thing it adds to the host's hash lock is
`cryptography` and its own two dependencies (LITE-0 §1.1, §5.6).

What it owns on disk is the host's VAPID key, `~/.config/omodachi/vapid.json`:
created the first time something needs it, 0600, written through a new file
and a rename, never a link. The key identifies this host to the push services
(Apple, FCM, Mozilla autopush); a browser subscribes against its public half.
The VAPID `sub` is the host's own `https://<name>.ts.net` (Apple answers
`403 BadJwtToken` to `mailto:...@localhost`, LITE-0 §4.2). The JWT is reused
for at least an hour per push service, as Apple asks.

Nothing here blocks: `send` is synchronous on purpose and the daemon calls it
from a worker thread (`push.PushSender`).
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hmac import HMAC

RECORD_SIZE = 4096
HEADER_LEN = 16 + 4 + 1 + 65  # salt, rs, idlen, keyid
# The push services cap the encrypted body at 4096 octets (RFC 8030 §7.2), which
# leaves 4096 - 86 - 16 - 1 = 3993 octets of plaintext (measured on FCM, LITE-0
# §1.1). Firefox for Android goes through an FCM bridge that leaves about 2744,
# so Omodachi's own limit is lower still (`push.PAYLOAD_LIMIT`).
MAX_PLAINTEXT = RECORD_SIZE - HEADER_LEN - 16 - 1
# Apple: do not refresh the JWT more often than once an hour. It is minted for
# twelve and reused until it has less than one left.
JWT_TTL = 12 * 3600
JWT_REUSE_MARGIN = 3600


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    if not isinstance(text, str):
        raise ValueError("not base64url")
    text = "".join(text.split())
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    h = HMAC(salt, hashes.SHA256())
    h.update(ikm)
    return h.finalize()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    # Every length used here is at most 32, one block.
    h = HMAC(prk, hashes.SHA256())
    h.update(info + b"\x01")
    return h.finalize()[:length]


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(payload: bytes, ua_public: bytes, auth_secret: bytes, *,
            as_private: ec.EllipticCurvePrivateKey | None = None, salt: bytes | None = None) -> bytes:
    """RFC 8291 §3.4 + RFC 8188: the `aes128gcm` body, header then ciphertext."""
    if len(payload) > MAX_PLAINTEXT:
        raise ValueError(f"payload of {len(payload)} bytes does not fit one push message")
    if len(auth_secret) != 16 or len(ua_public) != 65 or ua_public[0] != 4:
        raise ValueError("not a P-256 subscription key and 16-byte auth secret")
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    salt = os.urandom(16) if salt is None else salt
    as_public = _public_bytes(as_private.public_key())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = as_private.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf_expand(_hkdf_extract(auth_secret, shared), b"WebPush: info\x00" + ua_public + as_public, 32)
    prk = _hkdf_extract(salt, ikm)
    cek = _hkdf_expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf_expand(prk, b"Content-Encoding: nonce\x00", 12)
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public + ciphertext


class Vapid:
    """The host's VAPID key pair, and the JWTs it signs."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey, *, clock=time.time):
        self.key = private_key
        self._clock = clock
        self._tokens: dict[tuple[str, str], tuple[str, int]] = {}
        self._lock = threading.Lock()

    @classmethod
    def generate(cls, **kwargs) -> "Vapid":
        return cls(ec.generate_private_key(ec.SECP256R1()), **kwargs)

    @classmethod
    def from_document(cls, document: dict, **kwargs) -> "Vapid":
        raw = unb64u(document["private"])
        if len(raw) != 32:
            raise ValueError("VAPID private key is not 32 bytes")
        vapid = cls(ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1()), **kwargs)
        if document.get("public") not in (None, vapid.public_b64u):
            raise ValueError("VAPID public key does not match its private key")
        return vapid

    def document(self) -> dict:
        raw = self.key.private_numbers().private_value.to_bytes(32, "big")
        return {"version": 1, "private": b64u(raw), "public": self.public_b64u}

    @property
    def public_b64u(self) -> str:
        return b64u(_public_bytes(self.key.public_key()))

    def jwt(self, audience: str, subject: str) -> str:
        """An ES256 JWT for one push service, reused while it has an hour left."""
        now = int(self._clock())
        with self._lock:
            cached = self._tokens.get((audience, subject))
            if cached and cached[1] - now > JWT_REUSE_MARGIN:
                return cached[0]
            expires = now + JWT_TTL
            header = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
            claims = b64u(json.dumps({"aud": audience, "exp": expires, "sub": subject},
                                     separators=(",", ":")).encode())
            signing_input = f"{header}.{claims}".encode()
            r, s = decode_dss_signature(self.key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
            token = f"{header}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
            self._tokens[(audience, subject)] = (token, expires)
            return token


def _private_write(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".vapid-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def load_or_create(path: Path, **kwargs) -> Vapid:
    """The VAPID key at `path`, created (0600) when there is none yet."""
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        vapid = Vapid.generate(**kwargs)
        _private_write(path, json.dumps(vapid.document()))
        return vapid
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError(f"{path} is not a regular file of this user")
        if info.st_mode & 0o077:
            os.fchmod(stream.fileno(), 0o600)
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError(f"{path} is too large to be a VAPID key")
    return Vapid.from_document(json.loads(raw), **kwargs)


def audience(endpoint: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    return f"{parsed.scheme}://{parsed.netloc}"


def request(subscription: dict, payload: bytes, vapid: Vapid, *, subject: str, ttl: int = 3600,
            urgency: str | None = "high", topic: str | None = None) -> urllib.request.Request:
    """The POST one push is, ready for `urlopen` (built apart so tests read it)."""
    endpoint = subscription["endpoint"]
    keys = subscription["keys"]
    body = encrypt(payload, unb64u(keys["p256dh"]), unb64u(keys["auth"]))
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Encoding": "aes128gcm",
        "TTL": str(int(ttl)),
        "Authorization": f"vapid t={vapid.jwt(audience(endpoint), subject)}, k={vapid.public_b64u}",
    }
    if urgency:
        headers["Urgency"] = urgency
    if topic:
        headers["Topic"] = topic
    return urllib.request.Request(endpoint, data=body, headers=headers, method="POST")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A push service answers 201; a redirect is never followed somewhere else."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def send(subscription: dict, payload: bytes, vapid: Vapid, *, subject: str, ttl: int = 3600,
         urgency: str | None = "high", topic: str | None = None, timeout: float = 10.0,
         opener=None) -> dict:
    """One push. `{status, retry_after}`; status 0 is "never got an answer"."""
    prepared = request(subscription, payload, vapid, subject=subject, ttl=ttl, urgency=urgency, topic=topic)
    opener = opener or _OPENER
    retry_after, reason = None, None
    try:
        with opener.open(prepared, timeout=timeout) as response:
            status = response.status
            response.read(4096)
    except urllib.error.HTTPError as error:
        status = error.code
        retry_after = error.headers.get("Retry-After") if error.headers else None
        try:
            reason = refusal(error.read(4096))
        except Exception:
            pass
    except (urllib.error.URLError, OSError, ValueError):
        status = 0
    try:
        retry_after = None if retry_after is None else max(0, min(int(retry_after), 3600))
    except ValueError:
        retry_after = None
    return {"status": status, "retry_after": retry_after, "body_bytes": len(prepared.data), "reason": reason}


def refusal(raw: bytes):
    """Why a push service said no, as a short word, or None.

    Apple answers `{"reason": "BadWebPushTopic"}`; FCM and Mozilla answer
    JSON or a line of text. Only a bounded, printable reason is kept, so a log
    line never carries anything the service echoed back of the request.
    """
    text = raw.decode("utf-8", "replace").strip()
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if isinstance(value, dict):
        error = value.get("error")
        value = (value.get("reason") or value.get("message")
                 or (error.get("status") or error.get("message") if isinstance(error, dict) else error))
    elif value is None:
        value = text
    if not isinstance(value, str):
        return None
    value = "".join(c for c in value if c.isprintable())[:80]
    return value or None
