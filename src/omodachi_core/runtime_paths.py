"""Where the daemon's local socket lives. One place, standard library only.

AUTH-2 moved it. `~/.cache/omodachi/omodachid.sock` is a perfectly good place
for a socket right up until something has to reach it from inside a systemd
sandbox. `polkit-agent-helper@.service` ships with `ProtectHome=yes`, so
AUTH-1's PAM helper - which that unit starts - looked for the socket in a
`/home` that had nothing in it, refused, and every polkit prompt fell back to
the password. Correct behaviour, useless feature.

`$XDG_RUNTIME_DIR` is not the answer either, and this is the part that decides
the shape of this module. `ProtectHome=yes` empties **`/run/user` as well as
`/home` and `/root`**, and it does it with systemd's *inaccessible* mount,
under which nothing can be mounted at all: `ReadWritePaths=`, `BindPaths=` and
`BindReadOnlyPaths=` into `/run/user/<uid>/…` are all silently dropped, and
`ProtectHome=tmpfs` does not change that. There is no drop-in that puts a
`/run/user` path back. (`ProtectHome=read-only` does, by exposing the whole of
`/home` as well - which is not a trade this feature is worth.) The evidence is
in `tests/pam/sandbox/`.

So the socket lives in a directory that is under none of those three:

    /run/omodachi/<uid>/omodachid.sock     0755 root : 0700 owner : 0600 socket

`/run/omodachi` cannot be created by the user, so it is made by a
`tmpfiles.d` fragment the opt-in root step (`install_host.py --pam`) writes,
alongside the PAM entry that needs it. When that directory is not there - a
checkout, a container, a daemon someone started by hand, a host that never ran
the root step - the socket falls back to `$XDG_RUNTIME_DIR/omodachi/`, which
needs nobody's permission and is exactly as private.

`~/.cache/omodachi/omodachid.sock` stays alive for one release cycle as a
symlink the daemon maintains while it runs, so anything still holding the old
path keeps working.

This module imports nothing but the standard library on purpose: the host
installer runs under the system interpreter, outside the virtualenv that has
aiohttp in it, and it has to agree with the daemon about this path.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

SOCKET_NAME = "omodachid.sock"
# The root-owned parent. One directory per uid inside it, because a user cannot
# create anything in /run itself.
SHARED_RUNTIME_ROOT = "/run/omodachi"
SHARED_ROOT_MODE = 0o755
# The subdirectory inside `$XDG_RUNTIME_DIR` for the fallback.
RUNTIME_SUBDIR = "omodachi"
SOCKET_DIR_MODE = 0o700
SOCKET_MODE = 0o600
# The pre-AUTH-2 location, relative to the home directory.
LEGACY_SOCKET_RELATIVE = ".cache/omodachi/" + SOCKET_NAME


def _uid(uid=None) -> int:
    return os.getuid() if uid is None else int(uid)


def runtime_dir(environ=None, uid=None) -> Path:
    """`$XDG_RUNTIME_DIR`, or the path systemd would have set it to.

    A relative or empty value is not a runtime directory; falling back to
    `/run/user/<uid>` is what every other caller in this codebase does.
    """
    environ = os.environ if environ is None else environ
    value = (environ.get("XDG_RUNTIME_DIR") or "").strip()
    if value.startswith("/"):
        return Path(value)
    return Path("/run/user/%d" % _uid(uid))


def fallback_socket_dir(environ=None, uid=None) -> Path:
    return runtime_dir(environ, uid) / RUNTIME_SUBDIR


def shared_socket_dir(uid=None) -> Path:
    """`/run/omodachi/<uid>` — the one a systemd sandbox can be given."""
    return Path(SHARED_RUNTIME_ROOT) / str(_uid(uid))


def shared_socket_dir_is_usable(uid=None, probe=None) -> bool:
    """True when the root step has made our directory and we own it.

    Ownership is the whole check: `/run/omodachi` is root-owned and `0755`, so
    the per-uid directory inside it can only have been made by root, and a
    directory there belonging to somebody else is not ours to bind in.
    """
    uid = _uid(uid)
    probe = os.lstat if probe is None else probe
    try:
        info = probe(shared_socket_dir(uid))
    except (OSError, ValueError):
        return False
    if info is None:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == uid


def socket_dir(environ=None, uid=None, probe=None) -> Path:
    if shared_socket_dir_is_usable(uid, probe):
        return shared_socket_dir(uid)
    return fallback_socket_dir(environ, uid)


def default_socket_path(environ=None, uid=None, probe=None) -> str:
    """Where the daemon binds, and where a local client looks first."""
    return str(socket_dir(environ, uid, probe) / SOCKET_NAME)


def legacy_socket_path(home=None) -> str:
    """The pre-AUTH-2 path, kept as a compatibility symlink for one cycle."""
    return str((Path(home) if home is not None else Path.home()) / LEGACY_SOCKET_RELATIVE)


def client_socket_path(environ=None, home=None, uid=None, exists=os.path.exists, probe=None) -> str:
    """The socket a local client should try.

    Newest first, and only paths that are actually there. The legacy entry is
    what matters during the cycle the symlink is alive: a daemon from before
    AUTH-2 is still running and a client from after it is asking. With nothing
    anywhere, the answer is where a daemon started now would bind.
    """
    preferred = default_socket_path(environ, uid, probe)
    candidates = [preferred,
                  str(fallback_socket_dir(environ, uid) / SOCKET_NAME),
                  legacy_socket_path(home)]
    for candidate in candidates:
        if exists(candidate):
            return candidate
    return preferred


def is_shared_socket(path) -> bool:
    """True when `path` is under `/run/omodachi`, the sandbox-reachable root.

    `pam_install.py` writes the polkit drop-in only for a socket this is true
    of. For anything else there is nothing honest to write: no drop-in reaches
    into `/home` or `/run/user`, so claiming one would be a root-owned file
    that grants a permission and fixes nothing.
    """
    try:
        candidate = Path(path)
    except TypeError:
        return False
    if not candidate.is_absolute():
        return False
    root = Path(SHARED_RUNTIME_ROOT)
    return candidate.parts[:len(root.parts)] == root.parts and len(candidate.parts) > len(root.parts)
