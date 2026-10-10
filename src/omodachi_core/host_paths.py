"""HERDR-6: a folder a device names on the host, checked before anything uses it.

A new Herdr workspace starts a shell in the folder the phone picked, and the
folder browser lists what is inside one. Both take a path from a client, so
both go through `folder()` here first:

- the path is read the way a person types it: `~`, `~/Work/x`, an absolute
  path, or one relative to the home folder;
- it is resolved (`realpath`, symlinks followed), and the *resolved* path must
  be the home folder or inside it - a link out of `$HOME` is outside it;
- the private places in a home folder are refused whatever the path says to
  reach them (`PRIVATE`): keys, credentials, browser profiles and Omodachi's
  own state. This is ATTACH-2's design denylist (`docs/specs/ATTACH-2-report.md`
  §4.2 rule 4), directories only, with its two exceptions - so LINKS-1 can
  use the same table for files;
- and only then is it asked whether it exists and is a directory, so a refusal
  never says whether something private is there.

Herdr itself does none of this: herdr 0.8.2's `workspace create --cwd` takes a
missing folder, a file or a relative path without a word and opens the shell
in `$HOME` instead (probed on the host, HERDR-6 report §1).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Directories, relative to the home folder. Refused at that place and below.
PRIVATE = (
    ".ssh", ".gnupg", ".password-store", ".aws", ".azure", ".kube", ".docker", ".pki",
    ".codex", ".claude", ".mozilla",
    ".config/omodachi", ".local/state/omodachi", ".local/share/omodachi",
    ".local/share/keyrings", ".config/gh", ".config/herdr",
    ".config/chromium", ".config/google-chrome", ".config/BraveSoftware",
)
# ATTACH-2's two exceptions: what Omodachi itself hands an agent to read.
NOT_PRIVATE = (".local/state/omodachi/uploads", ".local/share/omodachi/agent-workspace")
MAX_CHARS = 4096


class FolderRefused(ValueError):
    """`args[0]` is the reason code the boundary reports (400 / 404)."""


def home_folder(home: Path | str | None = None) -> Path:
    return Path(os.path.realpath(home if home is not None else Path.home()))


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def private(path: Path, home: Path) -> bool:
    """Whether a resolved path inside `home` is one of the private places."""
    if any(_within(path, home / allowed) for allowed in NOT_PRIVATE):
        return False
    return any(_within(path, home / place) for place in PRIVATE)


def expand(value: Any, home: Path) -> Path:
    """What a person typed, as an absolute path (not yet resolved)."""
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_CHARS:
        raise FolderRefused("invalid_folder")
    if any(ord(character) < 0x20 or ord(character) == 0x7f for character in value):
        raise FolderRefused("invalid_folder")
    text = value.strip()
    if text == "~":
        return home
    if text.startswith("~/"):
        return home / text[2:]
    if text.startswith("~"):
        # `~someone`: another person's home is not this one's.
        raise FolderRefused("folder_outside_home")
    path = Path(text)
    return path if path.is_absolute() else home / path


def folder(value: Any, *, home: Path | str | None = None) -> Path:
    """The resolved folder `value` names, or `FolderRefused` with the reason."""
    home = home_folder(home)
    real = Path(os.path.realpath(expand(value, home)))
    if not _within(real, home):
        raise FolderRefused("folder_outside_home")
    if private(real, home):
        raise FolderRefused("folder_private")
    if not real.is_dir():
        raise FolderRefused("folder_not_found")
    return real


def usable(path: Any, home: Path) -> Path | None:
    """`folder()` without the reasons: a path a listing may show, or None."""
    try:
        return folder(path, home=home)
    except (FolderRefused, OSError, ValueError):
        return None


def display(path: Path, home: Path) -> str:
    """`~` and `~/x/y` for the home folder and what is in it."""
    if path == home:
        return "~"
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)
