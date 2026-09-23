"""Private, atomic, fsynced session journal.

Paths are local-owner inputs, never wire fields. One file per session; its
presence is the only durable record that a host mutation is outstanding.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile

from .errors import RemoteError

LIMIT = 262144


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)

    def write(self, value: dict) -> None:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(data) > LIMIT:
            raise RemoteError("journal_too_large")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise RemoteError("journal_unsafe")
        if self.path.exists():
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise RemoteError("journal_unsafe")
        fd, name = tempfile.mkstemp(prefix=".remote-session-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise RemoteError("journal_unsafe")
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise RemoteError("journal_too_large")
        try:
            value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(value, dict) or value.get("journal_version") != 1:
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise RemoteError("journal_invalid") from None

    def unlink(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
