#!/usr/bin/python3 -IB
"""The program `pam_exec.so` runs: ask the daemon, answer PAM, never block.

This file is installed verbatim as a root-owned `/usr/local/bin/omodachi-pam`.
It imports nothing from `omodachi_core` and nothing outside the standard
library, because the copy that PAM runs is a single file under `/usr/local/bin`
with no package around it. Keep it that way.

RELEASE-7b. The first line is `/usr/bin/python3 -IB`, not `env python3`: PAM
starts this as root for every prompt, so the interpreter is not looked up on a
PATH, and -I keeps PYTHONPATH, PYTHONHOME and every other PYTHON* variable,
user site-packages and `/usr/local/bin` itself off sys.path. -B: nothing is
written into a `__pycache__` beside it.

Three rules govern everything here:

1. **The only success is an explicit approval.** Every other outcome - no
   configuration, a socket that is not there, a daemon that says no, a daemon
   that says nothing, a malformed answer, an unexpected exception - exits
   non-zero, and the `auth sufficient` line then falls through to the password
   prompt that was always there. There is no path that exits 0 without having
   read `"approved": true` out of a reply on the daemon socket.
2. **It always ends.** Two independent timeouts (per-socket-operation, and a
   SIGALRM watchdog over the whole run) mean a wedged daemon costs the user the
   timeout and then the ordinary prompt, never a session they cannot get into.
3. **It is quiet.** `pam_exec ... stdout` hands whatever this prints to the PAM
   conversation, so the only thing ever printed is one line on success. A
   failure says nothing; the password prompt is the message.

The requester check is what keeps this from being a way for *another* Unix user
to borrow the owner's iPad: the approval is only ever asked for when the person
driving PAM is the owner named in the root-owned config file.

RELEASE-9 (B1). **This program verifies the approval itself.** Before, it
believed any `"approved": true` read off a socket the owner's uid owns - so any
process running as the owner could stop the daemon, bind that path and answer
yes, and sudo/polkit had no password left in front of them. Now:

* the device keys this helper accepts live in a root-owned store
  (`/etc/omodachi/pam/keys.json`, 0600), written only by the `--pam` root step
  or an explicit root enrolment - never by the daemon, never from a user file
  at approval time;
* this process mints the approval id and the 256-bit nonce, per prompt, with
  `secrets` - the daemon is handed them and only relays;
* the daemon's reply must carry the device's P-256 signature over the exact
  bytes the iPad has always signed (`omodachi-auth-approval-v1`, host id from
  the root store, approval id, nonce, service, user, device id), and this
  process verifies it against the root-stored key for that device.

The nonce lives only in this process's memory for the length of this one PAM
conversation, so the signature is bound to this prompt - this tty, this
requester, this moment - without the iPad having to sign anything new. A
process that fakes the socket has nothing to sign with; one that edits the
user's own key files edits files this program never reads.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import stat
import sys
import unicodedata

CONFIG = "/etc/omodachi/pam.conf"
# RELEASE-9. The root-owned store of device approval keys; `keys=` in the config
# may point elsewhere (tests, containers), and whatever it names has to pass
# the same ownership checks.
KEYS = "/etc/omodachi/pam/keys.json"
# The owner every file on the trust path must have. Only a unit test changes it.
TRUSTED_UID = 0
MAX_KEYS_BYTES = 262144
# The daemon protocol this helper speaks: it mints the nonce, the daemon relays.
PROTOCOL = 2
DEFAULT_TIMEOUT = 45.0
# Below the smallest timeout worth offering a human, and above the point where
# sudo has effectively hung. PAM has no cancel button.
MIN_TIMEOUT, MAX_TIMEOUT = 5.0, 120.0
CONNECT_TIMEOUT = 2.0
# The answer may legitimately take the whole approval window; the daemon is the
# thing enforcing it. This is the margin on top before we stop waiting for it.
REPLY_MARGIN = 5.0
MAX_REPLY_BYTES = 65536


def trace(config, *values):
    """Optional root-owned breadcrumb trail.

    A PAM helper that refuses silently is exactly right for a user and exactly
    useless for whoever has to find out *why* the password prompt came back.
    `debug=/var/log/omodachi-pam.log` in the root-owned config turns on one
    line per run; it is off unless an administrator asks for it, and it never
    contains anything the user typed - there is nothing to contain, because
    this program is never given the password.
    """
    path = (config or {}).get("debug")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(" ".join(str(value) for value in values) + "\n")
    except OSError:
        pass


def read_config(path):
    """`key=value` lines from a root-owned file. Unknown keys are ignored.

    RELEASE-9: read through the same checks as the key store. The config names
    the owner, the socket and the key store, so a config anybody but root could
    have written would hand all three to them.
    """
    values = {}
    for line in read_trusted(path, 65536).decode("utf-8", errors="strict").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def requester(environment, owner):
    """Who is driving this PAM stack, or None when that is not decidable.

    For `sudo` the target user is root and `PAM_RUSER` is the person who typed
    it; for a lock screen or a polkit agent the target user *is* the person.
    Anything else - an empty RUSER on a service where the target is not the
    owner either - is not decidable, and not decidable means no request.
    """
    ruser = (environment.get("PAM_RUSER") or "").strip()
    if ruser:
        return ruser
    user = (environment.get("PAM_USER") or "").strip()
    if user and user == owner:
        return user
    return None


def clamp_timeout(value, fallback=DEFAULT_TIMEOUT):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return fallback
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        return fallback
    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, seconds))


# ---------------------------------------------------------------------------
# NIST P-256 ECDSA verification, verification only (RELEASE-9: moved here from
# biometric.py so the root helper - one file, standard library only - can run
# it; biometric.py imports these same functions, so there is one verifier).
# It handles no secrets, so there is nothing for a timing side channel to leak,
# and every input is range-checked before it is used.
# ---------------------------------------------------------------------------
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
      0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)
_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")


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


def public_point(raw):
    """A P-256 public key from the X9.63 point iOS hands out, or its SPKI DER."""
    if len(raw) == 91:
        if not raw.startswith(_SPKI_PREFIX):
            raise ValueError("unsupported public key encoding")
        raw = raw[len(_SPKI_PREFIX):]
    if len(raw) != 65 or raw[0] != 0x04:
        raise ValueError("public key must be an uncompressed P-256 point")
    x, y = int.from_bytes(raw[1:33], "big"), int.from_bytes(raw[33:], "big")
    if not _on_curve(x, y) or (x, y) == (0, 0):
        raise ValueError("public key is not on the curve")
    return (x, y)


def _der_signature(raw):
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


def verify_signature(public_key, message, signature):
    """True only for a well-formed ECDSA-P256-SHA256 signature over `message`."""
    try:
        point = public_point(public_key)
        r, s = _der_signature(signature)
    except (ValueError, TypeError, IndexError):
        return False
    if not (1 <= r < _N and 1 <= s < _N):
        return False
    digest = int.from_bytes(hashlib.sha256(message).digest(), "big")
    inverse = pow(s, _N - 2, _N)
    combined = _add(_multiply(digest * inverse % _N, _G), _multiply(r * inverse % _N, point))
    if combined is None:
        return False
    return combined[0] % _N == r


# The exact bytes the iPad signs (ApprovalMessage.approval in the app). Kept
# byte-identical to biometric.approval_message; a unit test holds them together.
APPROVAL_CONTEXT = "omodachi-auth-approval-v1"
_FIELD = re.compile(r"[\x20-\x7e]{1,128}\Z")
_DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HOST_ID = re.compile(r"[0-9a-f]{32}\Z")
_APPROVAL_ID = re.compile(r"appr_[0-9a-f]{32}\Z")


def approval_message(host_id, approval_id, nonce, service, user, device_id):
    parts = [host_id, approval_id, nonce, service, user, device_id]
    for part in parts:
        if not isinstance(part, str) or not _FIELD.fullmatch(part):
            raise ValueError("unsignable field")
    return ("\n".join([APPROVAL_CONTEXT, *parts]) + "\n").encode("ascii")


def key_fingerprint(public_key):
    """`sha256:` + hex of the raw point: what the daemon matches its copy on."""
    return "sha256:" + hashlib.sha256(public_key).hexdigest()


# ---------------------------------------------------------------------------
# The trust path: files root reads to decide, and nobody else may write
# ---------------------------------------------------------------------------
def _trusted(info, what, *, directory=False):
    """Root's, and nobody else can write it.

    A directory may be world-writable only if it is root's own sticky one (the
    shape of `/tmp`): nobody but root can then rename or replace what root put
    in it, which is the property this check is for.
    """
    if info.st_uid not in (0, TRUSTED_UID):
        raise PermissionError("%s is not root-owned" % what)
    if info.st_mode & 0o022 and not (directory and info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
        raise PermissionError("%s is writable by someone other than root" % what)


def read_trusted(path, limit=MAX_KEYS_BYTES):
    """The bytes of a root-owned file no one else can write, or an error.

    Every directory from the file up to `/` has to be root's and not writable by
    group or others, and the file itself is opened without following a link and
    checked on the descriptor it was read from. A store a user could have
    replaced is not a store.
    """
    # Symlinks on the way are resolved first and the real chain is what is
    # checked; the file itself is still opened without following one.
    path = os.path.join(os.path.realpath(os.path.dirname(os.path.abspath(path))), os.path.basename(path))
    directory = os.path.dirname(path)
    while True:
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode):
            raise PermissionError("%s is not a directory" % directory)
        _trusted(info, directory, directory=True)
        if directory == "/":
            break
        directory = os.path.dirname(directory)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError("not a regular file")
        _trusted(info, path)
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError("file too large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def load_keys(path, owner):
    """`(host_id, {device_id: row})` out of the root store, or an error.

    A row is `{"public_key": bytes, "label": str}`. The store names the owner it
    was enrolled for; a store for somebody else is not this owner's.
    """
    data = json.loads(read_trusted(path).decode("utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or data.get("owner") != owner:
        raise ValueError("key store is not for this owner")
    host_id = data.get("host_id")
    if not isinstance(host_id, str) or not _HOST_ID.fullmatch(host_id):
        raise ValueError("key store has no host id")
    rows = data.get("keys")
    if not isinstance(rows, dict):
        raise ValueError("key store has no keys")
    keys = {}
    for device_id, row in rows.items():
        if not isinstance(device_id, str) or not _DEVICE_ID.fullmatch(device_id) or not isinstance(row, dict):
            raise ValueError("malformed key row")
        try:
            raw = base64.b64decode(row.get("public_key") or "", validate=True)
            public_point(raw)
        except (binascii.Error, ValueError, TypeError):
            raise ValueError("malformed public key") from None
        label = row.get("label")
        keys[device_id] = {"public_key": raw, "label": label if isinstance(label, str) else ""}
    return host_id, keys


def printable_label(value):
    """A device label fit to print into somebody's terminal, or None."""
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return None
    if any(unicodedata.category(c) in {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"} for c in value):
        return None
    return value


def open_socket(path, owner_uid):
    """Connect to the daemon's socket, refusing anything that is not one.

    `owner_uid` is the uid the root-owned config names. A socket owned by
    anybody else is not the daemon this host was configured to trust, and the
    helper would rather fall through to the password than ask it.
    """
    info = os.lstat(path)
    import stat as _stat
    if not _stat.S_ISSOCK(info.st_mode):
        raise OSError("not a socket")
    if owner_uid is not None and info.st_uid != owner_uid:
        raise OSError("socket is not owned by the configured owner")
    stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stream.settimeout(CONNECT_TIMEOUT)
    stream.connect(path)
    return stream


def ask(path, owner_uid, request, timeout):
    stream = open_socket(path, owner_uid)
    try:
        stream.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
        stream.settimeout(timeout + REPLY_MARGIN)
        chunks, total = [], 0
        while True:
            chunk = stream.recv(4096)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_REPLY_BYTES:
                raise ValueError("reply too large")
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        line = b"".join(chunks).split(b"\n", 1)[0]
        if not line:
            raise ValueError("empty reply")
        return json.loads(line)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def decide(reply, *, approval_id, nonce, host_id, service, user, keys):
    """The device that approved, or None. The daemon's word alone is never enough.

    The reply has to be an explicit approval *and* carry a signature this
    process can verify, with a key from the root store, over the nonce this
    process minted for this prompt. Everything the daemon says besides the
    signature and the device id it names is ignored.
    """
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        return None
    result = reply.get("result")
    if not isinstance(result, dict) or result.get("approved") is not True:
        return None
    if result.get("approval_id") != approval_id:
        return None
    device_id = result.get("device_id")
    if not isinstance(device_id, str) or device_id not in keys:
        return None
    signature = result.get("signature")
    if not isinstance(signature, str) or not 1 <= len(signature) <= 256:
        return None
    try:
        raw = base64.b64decode(signature, validate=True)
        message = approval_message(host_id, approval_id, nonce, service, user, device_id)
    except (binascii.Error, ValueError):
        return None
    if not verify_signature(keys[device_id]["public_key"], message, raw):
        return None
    return device_id


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    config_path, timeout_argument = CONFIG, None
    while argv:
        flag = argv.pop(0)
        if flag == "--timeout" and argv:
            timeout_argument = argv.pop(0)
        elif flag == "--config" and argv:
            config_path = argv.pop(0)
        else:
            # An unknown argument is a misconfigured PAM line. Fall through to
            # the password rather than guessing what was meant.
            return 1

    environment = os.environ
    if (environment.get("PAM_TYPE") or "auth") != "auth":
        return 1

    try:
        config = read_config(config_path)
    except (OSError, UnicodeError, ValueError):
        return 1

    owner = (config.get("owner") or "").strip()
    socket_path = (config.get("socket") or "").strip()
    trace(config, "start service=%r user=%r ruser=%r tty=%r type=%r" % (
        environment.get("PAM_SERVICE"), environment.get("PAM_USER"),
        environment.get("PAM_RUSER"), environment.get("PAM_TTY"), environment.get("PAM_TYPE")))
    if not owner or not socket_path or not socket_path.startswith("/"):
        trace(config, "refuse: configuration incomplete")
        return 1

    services = [name for name in (config.get("services") or "").split(",") if name.strip()]
    service = (environment.get("PAM_SERVICE") or "").strip()
    if not service or (services and service not in [name.strip() for name in services]):
        trace(config, "refuse: service %r is not configured" % service)
        return 1

    who = requester(environment, owner)
    if who != owner:
        trace(config, "refuse: requester %r is not the owner %r" % (who, owner))
        return 1

    timeout = clamp_timeout(timeout_argument if timeout_argument is not None else config.get("timeout"))

    # The watchdog is the promise that this program ends. It fires after both
    # the socket deadlines have had their chance, and it exits the process
    # without unwinding so no handler can swallow it. It is also disarmed on
    # the way out: `main()` is called directly by the unit tests, and an alarm
    # left armed would take the calling process down a few seconds later.
    def expire(_signum, _frame):
        os._exit(1)

    previous = None
    try:
        previous = signal.signal(signal.SIGALRM, expire)
        signal.setitimer(signal.ITIMER_REAL, timeout + REPLY_MARGIN + CONNECT_TIMEOUT + 2.0)
    except (ValueError, OSError):
        pass

    try:
        return _ask_and_decide(config, socket_path, owner, who, service, environment, timeout)
    finally:
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if previous is not None:
                signal.signal(signal.SIGALRM, previous)
        except (ValueError, OSError):
            pass


def _ask_and_decide(config, socket_path, owner, who, service, environment, timeout):
    """Everything past the point where a request is actually going to be made."""
    try:
        import pwd
        owner_uid = pwd.getpwnam(owner).pw_uid
    except (KeyError, ImportError):
        trace(config, "refuse: no such user %r" % owner)
        return 1
    try:
        host_id, keys = load_keys((config.get("keys") or KEYS).strip(), owner)
    except (OSError, ValueError, UnicodeError) as error:
        # No root-enrolled key means nobody can answer. The daemon is not asked:
        # there is nothing it could say that this process would accept.
        trace(config, "refuse: key store: %s: %s" % (type(error).__name__, error))
        return 1
    if not keys:
        trace(config, "refuse: no device key is enrolled for this host's password prompts")
        return 1
    user = (environment.get("PAM_USER") or "").strip() or owner
    approval_id = "appr_" + secrets.token_hex(16)
    nonce = secrets.token_urlsafe(32)
    try:
        trace(config, "asking %s for %s (%s)" % (socket_path, service, approval_id))
        reply = ask(socket_path, owner_uid, {
            "op": "local.auth.approve",
            "protocol": PROTOCOL,
            "approval_id": approval_id,
            "nonce": nonce,
            "host_id": host_id,
            # Which devices can answer, and the key each must answer with, so
            # the daemon asks nobody whose signature this process would refuse.
            "devices": {device_id: key_fingerprint(row["public_key"]) for device_id, row in keys.items()},
            "service": service,
            "user": user,
            "requester": who,
            "tty": (environment.get("PAM_TTY") or "").strip()[:64] or None,
            "rhost": (environment.get("PAM_RHOST") or "").strip()[:64] or None,
            "timeout": timeout,
        }, timeout)
    except BaseException as error:
        # Every failure is the same failure: ask for the password.
        trace(config, "refuse: %s: %s" % (type(error).__name__, error))
        return 1

    device_id = decide(reply, approval_id=approval_id, nonce=nonce, host_id=host_id,
                       service=service, user=user, keys=keys)
    trace(config, "reply for %s -> verified device %r" % (approval_id, device_id))
    if device_id is None:
        return 1
    # `stdout` on the pam_exec line turns this into a PAM_TEXT_INFO the user
    # sees where the password prompt would have been. The name comes from the
    # root store, not from the reply.
    sys.stdout.write("Approved on {}\n".format(printable_label(keys[device_id]["label"])
                                                or "your Omodachi device"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit(1)
