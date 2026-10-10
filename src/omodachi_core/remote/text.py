"""REMOTE-INPUT-1. Text the Remote keyboard has no key for, typed on the host.

The app sends everything a US keyboard can produce as real key events down the
stream. What is left — 中文 committed by the phone's own input method, emoji,
accented letters — used to travel as Moonlight's UTF-8 text event, which the
Sunshine fork types on Linux with the GTK/IBus Ctrl+Shift+U + hex trick.
Omarchy runs fcitx5, and neither fcitx5 nor most terminals finish that
sequence, so Leo typed "y" and got "U+".

So that text is typed here instead, with `wtype` — the virtual-keyboard tool
Omarchy itself installs for Voxtype — as the desktop user, into whatever window
has the keyboard focus, which is the same window the stream's keys go to.

What this module will not do, and why:

- **No clipboard.** `wl-copy` plus a paste chord would replace what the user
  copied, put every typed word into the clipboard history, and need a
  different chord per program.
- **No shell, no echo.** `wtype` gets the text as one argv element after `--`;
  nothing is interpreted. The answer carries a count, never the text, and the
  journal line carries neither.
- **No control characters but two.** REMOTE-INPUT-2: the app's compose field
  sends a whole draft at once, and a line break in it (a pasted paragraph, or
  the "Enter after" switch) is a Return the person asked for — `wtype` types
  `\n` as the Return key and `\t` as Tab (its own remap table). Those two are
  allowed; every other control character (Escape, CR, DEL, C1) is still
  refused, not stripped, so this route cannot be used to press anything else.
- **Once per send.** A request may carry an `id` the app made for one press
  of Send. The same id again — a retry after a reply that never arrived — gets
  the first answer back and types nothing (`RemoteService.text`).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import unicodedata

from .errors import RemoteError

WTYPE = "/usr/bin/wtype"
#: One commit from a phone keyboard is a word or a phrase; a paragraph pasted
#: into the soft keyboard is still well inside this. Bigger is refused, never
#: truncated.
MAX_CHARS = 1024
MAX_BYTES = 4096
TIMEOUT = 20.0
#: Every `wtype` is a new virtual keyboard with its own keymap, and Chromium
#: (through fcitx5) drops the first key it gets from one: "你好" typed into its
#: address bar arrived as "好" on Leo's host, with or without a pause first.
#: A press of `VoidSymbol` — a key that types nothing anywhere — takes that
#: place. Verified harmless in kitty and a GTK entry.
LEADING_KEY = ("-k", "VoidSymbol")
#: REMOTE-INPUT-2. The two control characters a draft may carry: `wtype`
#: presses Return for a line break and Tab for a tab.
KEY_CHARACTERS = frozenset("\n\t")
#: What `text_input` tells a client, so a draft over the limit is shown as such
#: before Send, never refused after it.
LIMITS = {"max_chars": MAX_CHARS, "max_bytes": MAX_BYTES}


def validate_text(value) -> str:
    if not isinstance(value, str) or not value:
        raise RemoteError("invalid_request", 400)
    if len(value) > MAX_CHARS or len(value.encode("utf-8", "surrogatepass")) > MAX_BYTES:
        raise RemoteError("invalid_request", 400)
    for character in value:
        category = unicodedata.category(character)
        # Cc: C0/C1 controls (CR, Escape, DEL…), except a line break and a tab
        # (`KEY_CHARACTERS`). Cs: a lone surrogate, which is not text. Both are
        # refused, not stripped.
        if category == "Cs" or (category == "Cc" and character not in KEY_CHARACTERS):
            raise RemoteError("invalid_request", 400)
    return value


class HostText:
    """Types one piece of text into the focused host window, with `wtype`."""

    def __init__(self, *, runner=None, environment=None, which=shutil.which):
        self.runner = runner or self._run
        if environment is None:
            from ..graphical import graphical_environment
            environment = graphical_environment
        self.environment = environment
        self.which = which
        # One `wtype` at a time: two virtual keyboards typing at once would
        # interleave their keys.
        self._lock = threading.Lock()

    def executable(self) -> str | None:
        if os.access(WTYPE, os.X_OK):
            return WTYPE
        return self.which("wtype")

    def available(self) -> bool:
        return self.executable() is not None

    @staticmethod
    def _run(argv, *, env, timeout):
        try:
            return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=timeout, env=env, shell=False,
                                  start_new_session=True).returncode
        except subprocess.TimeoutExpired:
            return 124
        except (OSError, subprocess.SubprocessError):
            return 1

    def type(self, text: str) -> dict:
        text = validate_text(text)
        executable = self.executable()
        if executable is None:
            raise RemoteError("remote_text_unavailable", 503)
        env = dict(self.environment() or {})
        if not env.get("WAYLAND_DISPLAY"):
            raise RemoteError("remote_text_unavailable", 503)
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        with self._lock:
            code = self.runner((executable, *LEADING_KEY, "--", text), env=env, timeout=TIMEOUT)
        if code != 0:
            raise RemoteError("remote_text_failed", 503)
        return {"typed": True, "chars": len(text), "method": "wtype"}
