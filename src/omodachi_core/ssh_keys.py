"""The lines Omodachi owns in `~/.ssh/authorized_keys`, and no others.

SPEC-F3 gives the companion an SSH terminal: the app generates its own key pair
and shows the public half, and somebody has to put it on the host. Doing it by
hand is the step people get wrong, and `>>` on `authorized_keys` is the step
people get wrong twice - a duplicated key, a lost trailing newline, a file that
ends up world-readable and stops working entirely.

So core owns a line format and nothing else:

    ssh-ed25519 AAAA... # omodachi:<device>

Everything after the key body is an OpenSSH comment, so the marker is legal,
greppable, and unambiguous about who wrote it. `authorize` is idempotent on the
key body, `revoke` deletes exactly the lines carrying one device's marker, and
every other line in the file - the user's own keys, their `command=` options,
their comments - is copied through byte for byte. Nothing here ever edits
sshd_config or any key that is not marked as ours.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile

MARKER = "# omodachi:"
# The key types OpenSSH 9/10 still accepts for this purpose. An unknown type is
# refused rather than written through: a line sshd cannot parse is a line that
# silently does nothing.
KEY_TYPES = frozenset({
    "ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521", "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
})
DEVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
BODY = re.compile(r"[A-Za-z0-9+/]{32,16384}={0,3}")
LIMIT = 1048576


class SshKeyError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def parse_public_key(value) -> tuple[str, str]:
    """(type, base64 body) for one single-line OpenSSH public key."""
    if not isinstance(value, str) or len(value) > 16384 or any(ord(c) < 32 for c in value.strip()):
        raise SshKeyError("invalid_public_key")
    fields = value.strip().split()
    if len(fields) < 2 or fields[0] not in KEY_TYPES or not BODY.fullmatch(fields[1]):
        raise SshKeyError("invalid_public_key")
    # A body sshd cannot decode is a line that quietly does nothing.
    if fingerprint(fields[1]) is None:
        raise SshKeyError("invalid_public_key")
    return fields[0], fields[1]


def device_name(value) -> str:
    if not isinstance(value, str) or not DEVICE.fullmatch(value):
        raise SshKeyError("invalid_device")
    return value


def fingerprint(body: str) -> str | None:
    """`SHA256:...`, the same string `ssh-keygen -lf` prints."""
    try:
        raw = base64.b64decode(body, validate=True)
    except (ValueError, TypeError):
        return None
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def duplicate_devices(rows) -> dict:
    """The devices holding more than one owned key, oldest line first.

    UX-4 §3. One device with several keys is the shape a key drift leaves
    behind, and until now nothing said it out loud: `ssh list` printed four
    rows and a reader had to notice that two of them named the same device.
    Two devices with one key each - which is what Leo's iPad legitimately looks
    like on this host - is not this, and is never reported as it.
    """
    seen: dict[str, list] = {}
    for row in rows:
        seen.setdefault(row["device"], []).append(row["fingerprint"])
    return {device: values for device, values in seen.items() if len(values) > 1}


class AuthorizedKeys:
    """One `authorized_keys` file, read and rewritten whole and atomically."""

    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home is not None else Path.home()

    @property
    def path(self) -> Path:
        return self.home / ".ssh/authorized_keys"

    # --- reading -----------------------------------------------------------

    def _read(self) -> list[str]:
        path = self.path
        if path.is_symlink():
            raise SshKeyError("authorized_keys_unsafe")
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return []
        except OSError:
            raise SshKeyError("authorized_keys_unreadable") from None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise SshKeyError("authorized_keys_unsafe")
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise SshKeyError("authorized_keys_too_large")
        try:
            return raw.decode("utf-8").splitlines()
        except UnicodeError:
            raise SshKeyError("authorized_keys_unreadable") from None

    @staticmethod
    def _owner(line: str) -> str | None:
        """The device a line is marked for, or None when it is not ours."""
        index = line.find(MARKER)
        if index < 0 or line.lstrip().startswith("#"):
            return None
        owner = line[index + len(MARKER):].strip()
        return owner if DEVICE.fullmatch(owner) else None

    @staticmethod
    def _body(line: str) -> tuple[str, str] | None:
        try:
            return parse_public_key(line)
        except SshKeyError:
            return None

    def listing(self) -> list[dict]:
        rows = []
        for line in self._read():
            owner = self._owner(line)
            body = self._body(line)
            if owner is not None and body is not None:
                rows.append({"device": owner, "type": body[0], "fingerprint": fingerprint(body[1])})
        return rows

    # --- writing -----------------------------------------------------------

    def _write(self, lines: list[str]) -> None:
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise SshKeyError("authorized_keys_unsafe")
        text = "".join(line + "\n" for line in lines)
        handle, name = tempfile.mkstemp(prefix=".authorized_keys-", dir=directory)
        try:
            with os.fdopen(handle, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(text.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def authorize(self, public_key: str, device: str) -> dict:
        """Append one owned line. Running it again changes nothing."""
        kind, body = parse_public_key(public_key)
        owner = device_name(device)
        lines = self._read()
        for line in lines:
            if self._body(line) != (kind, body):
                continue
            existing = self._owner(line)
            if existing == owner:
                return {"authorized": True, "changed": False, "device": owner,
                        "reason": "already_authorized", "fingerprint": fingerprint(body)}
            # Somebody else's line, or one the user wrote by hand, already
            # carries this key. Adding a second copy would let a revoke look
            # like it worked while the key still opens the door.
            raise SshKeyError("public_key_not_owned" if existing is None else "public_key_owned_by_other_device")
        self._write(lines + [f"{kind} {body} {MARKER}{owner}"])
        return {"authorized": True, "changed": True, "device": owner, "reason": "added",
                "fingerprint": fingerprint(body)}

    def replace(self, public_key: str, device: str) -> dict:
        """One device, one line: the key it offers now replaces what it had.

        UX-4. `authorize` appends, which is right for a pairing - the device is
        new, the line is new. It is wrong for the case this exists for: the app
        was reinstalled, the private half it now holds is a key this host has
        never seen, and the line already here opens the door for a key that no
        longer exists anywhere. Appending would leave both, and a revoke that
        took one of them back would look like it had worked.

        So this is a replacement, not an addition. Every line marked for this
        device goes, one line for the key it is offering now takes their place,
        and `replaced` names the fingerprints that were dropped so the journal
        says what stopped working rather than only what started.

        The new line is appended rather than written where the old one was, so
        that for the lines Omodachi owns, file order is the order they were
        landed in - which is what makes "the newest one" a fact rather than a
        guess (see `prune`).
        """
        kind, body = parse_public_key(public_key)
        owner = device_name(device)
        lines = self._read()
        for line in lines:
            if self._body(line) != (kind, body):
                continue
            existing = self._owner(line)
            if existing == owner:
                continue
            # This key body is on somebody else's line, or on one the user
            # wrote by hand. Taking it over would let one device's revoke
            # silently disarm another's access, or edit a line we do not own.
            raise SshKeyError("public_key_not_owned" if existing is None else "public_key_owned_by_other_device")
        mine = [line for line in lines if self._owner(line) == owner]
        dropped = [fingerprint(found[1]) for line in mine
                   if (found := self._body(line)) is not None and found != (kind, body)]
        if len(mine) == 1 and self._body(mine[0]) == (kind, body):
            return {"authorized": True, "changed": False, "device": owner,
                    "reason": "already_authorized", "fingerprint": fingerprint(body),
                    "removed": 0, "replaced": []}
        kept = [line for line in lines if self._owner(line) != owner]
        self._write(kept + [f"{kind} {body} {MARKER}{owner}"])
        return {"authorized": True, "changed": True, "device": owner,
                "reason": "replaced" if mine else "added", "fingerprint": fingerprint(body),
                "removed": len(mine), "replaced": dropped}

    def prune(self, device: str | None = None) -> dict:
        """Keep the newest owned line per device and delete the older ones.

        "Newest" is the last one in the file, and that is a property of how
        this module writes rather than an assumption about the file: `authorize`
        appends and `replace` appends, so for the lines carrying our marker,
        later in the file means landed later. Lines without our marker are
        never counted, compared or moved.

        A device with one line is not touched, so a host whose devices each
        hold one key is a no-op - which is the common case and has to stay one.
        """
        target = device_name(device) if device is not None else None
        lines = self._read()
        owned: dict[str, list[int]] = {}
        for index, line in enumerate(lines):
            owner = self._owner(line)
            if owner is None or self._body(line) is None:
                continue
            if target is not None and owner != target:
                continue
            owned.setdefault(owner, []).append(index)
        drop: set[int] = set()
        devices = {}
        for owner, indexes in sorted(owned.items()):
            if len(indexes) < 2:
                continue
            *older, newest = indexes
            drop.update(older)
            devices[owner] = {
                "kept": fingerprint(self._body(lines[newest])[1]),
                "removed": [fingerprint(self._body(lines[index])[1]) for index in older],
            }
        if drop:
            self._write([line for index, line in enumerate(lines) if index not in drop])
        return {"pruned": True, "removed": len(drop), "devices": devices}

    def revoke(self, device: str) -> dict:
        """Delete every line marked for this device, and only those."""
        owner = device_name(device)
        lines = self._read()
        kept = [line for line in lines if self._owner(line) != owner]
        removed = len(lines) - len(kept)
        if removed:
            self._write(kept)
        return {"revoked": True, "removed": removed, "device": owner}
