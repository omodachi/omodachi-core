#!/usr/bin/env python3
"""The program `pam_exec.so` runs: ask the daemon, answer PAM, never block.

This file is installed verbatim as a root-owned `/usr/local/bin/omodachi-pam`.
It imports nothing from `omodachi_core` and nothing outside the standard
library, because the copy that PAM runs is a single file under `/usr/local/bin`
with no package around it. Keep it that way.

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
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys

CONFIG = "/etc/omodachi/pam.conf"
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
    """`key=value` lines from a root-owned file. Unknown keys are ignored."""
    values = {}
    with open(path, "r", encoding="utf-8", errors="strict") as stream:
        for line in stream.read(65536).splitlines():
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


def decide(reply):
    """True only for a well-formed, explicit approval."""
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        return False, None
    result = reply.get("result")
    if not isinstance(result, dict) or result.get("approved") is not True:
        return False, None
    name = result.get("device_name")
    if not isinstance(name, str) or not 1 <= len(name) <= 80 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        name = None
    return True, name


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
    except (OSError, UnicodeError):
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
        trace(config, "asking %s for %s" % (socket_path, service))
        reply = ask(socket_path, owner_uid, {
            "op": "local.auth.approve",
            "service": service,
            "user": (environment.get("PAM_USER") or "").strip() or owner,
            "requester": who,
            "tty": (environment.get("PAM_TTY") or "").strip()[:64] or None,
            "rhost": (environment.get("PAM_RHOST") or "").strip()[:64] or None,
            "timeout": timeout,
        }, timeout)
    except BaseException as error:
        # Every failure is the same failure: ask for the password.
        trace(config, "refuse: %s: %s" % (type(error).__name__, error))
        return 1

    approved, device_name = decide(reply)
    trace(config, "reply %r -> approved=%r" % (reply, approved))
    if not approved:
        return 1
    # `stdout` on the pam_exec line turns this into a PAM_TEXT_INFO the user
    # sees where the password prompt would have been.
    sys.stdout.write("Approved on {}\n".format(device_name or "your Omodachi device"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit(1)
