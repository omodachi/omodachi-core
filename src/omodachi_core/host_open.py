"""LINKS-1: `POST /v1/host/open` - something an agent showed, opened on the computer.

Leo (2026-10-04): the phone never views the computer's files. A web address
opens on the phone; a *file* opens on the computer, with the desktop's own
default handler, and the phone jumps to Remote to look at it. So this is not a
file-read interface: no byte of the file ever leaves the host. What it guards
is the other risk - that "open what the agent mentioned" becomes "make the
computer launch whatever a credential names".

What has to be true, all of it, before anything is opened:

1. **Mentioned** (ATTACH-2 §4.2 rule 1). The text the client sends must appear
   in the source it names: an assistant message or a tool row's own `paths`
   (fileChange / imageView) for the Agent, the pane's *visible* screen right now
   for Herdr (`pane.read source=visible`, which never scrolls the desktop pane;
   a path the terminal wrapped is matched with the line breaks taken out).
   A web address is held to the same rule.
2. **Inside a root**: after `~` and relative paths are resolved (against the
   thread's folder or the pane's) and symbolic links followed, the real path is
   in `$HOME`, in that thread / pane folder, or in `/tmp` - and outside `$HOME`
   only the user's own files.
3. **Not private** - checked before anything else is looked at: `~/.ssh`,
   `~/.gnupg`, `~/.codex`, Omodachi's own config and state (except the uploads
   the phone sent), key and credential files, `.env`.
4. **A regular file or a folder** - never a device, socket or FIFO.
5. **Never run**: a file with an exec bit, an ELF binary, a `.desktop` entry,
   an AppImage and the other launchable kinds are refused, so the default
   handler is never asked to execute something.

It then opens with the desktop's default handler (`xdg-open`, as the user, in
the graphical session's own environment). When a Remote session is running,
its own output is focused first, so the new window lands on the screen the
phone is showing (extend mode) rather than on the laptop panel nobody is
looking at. A `:12` / `#L12` suffix opens the editor at that line when the
default handler for that file is Omarchy's terminal editor (`nvim.desktop`,
through `omarchy-launch-editor`); any other handler opens the file as usual.

One journal line per request: device, role, source kind, the real path (or the
address), and the outcome. It is the user's own machine's journal, so the path
is written in full; nothing from the file is.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
from typing import Callable
from urllib.parse import unquote, urlsplit

from .service import ServiceError

MAX_TEXT = 4096
RATE_WINDOW_SECONDS = 60.0
RATE_LIMIT = 30
EXEC_SETTLE_SECONDS = 1.5

REFUSALS = {
    "open_not_mentioned": 403,
    "open_outside_roots": 403,
    "open_denied": 403,
    "open_executable": 403,
    "open_missing": 404,
    "open_source_gone": 404,
    "open_type_refused": 415,
    "open_url_refused": 400,
    "open_rate_limited": 429,
    "open_unavailable": 503,
}


def refused(code: str) -> ServiceError:
    return ServiceError(code, code, REFUSALS[code])


# ── 3. private places ──────────────────────────────────────────────────────
#: Directories under `$HOME`, as path segments. A real path inside any of them
#: is refused whatever else is true, unless it is inside one of `ALLOWED_INSIDE`.
DENIED_DIRS = (
    ".ssh", ".gnupg", ".codex", ".claude", ".config/omodachi", ".local/state/omodachi",
    ".local/share/omodachi", ".password-store", ".local/share/keyrings", ".aws", ".kube",
    ".docker", ".config/gh", ".config/herdr", ".config/chromium", ".config/google-chrome",
    ".config/BraveSoftware", ".mozilla", ".pki", ".config/rclone", ".terraform.d",
)
#: The two places Omodachi itself puts files a person may want to see: what the
#: phone sent (ATTACH-1) and the default agent's working folder.
ALLOWED_INSIDE = (".local/state/omodachi/uploads", ".local/share/omodachi/agent-workspace")
DENIED_NAMES = re.compile(
    r"(?:\.env(?:\..*)?|\.netrc|\.git-credentials|\.pgpass|\.npmrc|\.pypirc|auth\.json|credentials(?:\..*)?"
    r"|id_(?:rsa|dsa|ecdsa|ed25519)(?:_sk)?(?:\.pub)?|.*\.(?:pem|key|p12|pfx|kdbx|keystore|jks|gpg|asc))\Z",
    re.IGNORECASE)

# ── 5. never run ───────────────────────────────────────────────────────────
LAUNCHABLE_SUFFIXES = (
    ".desktop", ".appimage", ".exe", ".msi", ".bat", ".cmd", ".com", ".jar", ".run", ".bin",
    ".deb", ".rpm", ".apk", ".flatpak", ".flatpakref", ".flatpakrepo", ".snap", ".service",
)
LAUNCHABLE_TYPES = frozenset({
    "application/x-desktop", "application/x-executable", "application/x-pie-executable",
    "application/x-sharedlib", "application/vnd.appimage", "application/x-iso9660-appimage",
    "application/x-ms-dos-executable", "application/x-msdownload", "application/x-msi",
    "application/java-archive", "application/x-java-archive",
    "application/vnd.flatpak", "application/vnd.flatpak.ref", "application/vnd.snap",
})
#: The handler Omarchy ships for text: a terminal editor, which takes `+N`.
LINE_EDITORS = frozenset({"nvim.desktop", "vim.desktop", "nano.desktop", "micro.desktop", "helix.desktop"})

#: Omarchy's own "open this in the default editor" (`omarchy-launch-editor`):
#: a terminal editor in the default terminal, Omarchy styling, any args passed.
EDITOR_LAUNCHER = "/usr/bin/omarchy-launch-editor"

LINE_SUFFIX = re.compile(r"(?::(\d{1,7})(?::\d{1,7})?|#L(\d{1,7})(?:-L?\d{1,7})?)\Z")


@dataclass(frozen=True)
class Request:
    source: dict
    text: str
    is_url: bool
    check: bool


@dataclass(frozen=True)
class Target:
    kind: str            # file | directory | url
    value: str           # the real path, or the address
    name: str
    line: int | None = None
    mime: str | None = None


def parse(payload) -> Request:
    if not isinstance(payload, dict) or set(payload) - {"source", "path", "url", "check"} \
            or "source" not in payload or ("path" in payload) == ("url" in payload):
        raise ServiceError("invalid_request", "source and exactly one of path or url")
    check = payload.get("check", False)
    if type(check) is not bool:
        raise ServiceError("invalid_request")
    source = payload["source"]
    if not isinstance(source, dict):
        raise ServiceError("invalid_request")
    kind = source.get("kind")
    if kind == "agent":
        if set(source) != {"kind", "item"} or not isinstance(source["item"], str) \
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", source["item"]):
            raise ServiceError("invalid_request", "an agent source names its item")
    elif kind == "herdr":
        if set(source) - {"kind", "pane", "session"} or not isinstance(source.get("pane"), str) \
                or not re.fullmatch(r"w[0-9]{1,9}:p[0-9]{1,9}", source["pane"]):
            raise ServiceError("invalid_request", "a herdr source names its pane")
        session = source.get("session")
        if session is not None and (not isinstance(session, str)
                                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", session)):
            raise ServiceError("invalid_request")
    else:
        raise ServiceError("invalid_request", "source kind is agent or herdr")
    is_url = "url" in payload
    text = payload["url" if is_url else "path"]
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT \
            or any(ch in text for ch in "\0\n\r"):
        raise ServiceError("invalid_request", "the path or address as it appeared")
    return Request(dict(source), text, is_url, check)


# ── 1. mentioned ───────────────────────────────────────────────────────────
def mentioned(text: str, haystack: str) -> bool:
    """`text` appears in `haystack` as it is, or with the screen's line breaks
    taken out (a long path the terminal wrapped is still one path)."""
    if not text or not haystack:
        return False
    if text in haystack:
        return True
    joined = "".join(line.rstrip("\r") for line in haystack.split("\n"))
    return text in joined


# ── 2. where it is ─────────────────────────────────────────────────────────
def split_line(text: str) -> tuple[str, int | None]:
    match = LINE_SUFFIX.search(text)
    if not match or match.start() == 0:
        return text, None
    number = int(match.group(1) or match.group(2))
    return text[:match.start()], number if number > 0 else None


def _candidates(text: str, home: Path, cwd: Path | None) -> list[tuple[Path, int | None]]:
    """Every reading of `text` as a path, most literal first: as typed, with a
    `:line` suffix taken off, percent-decoded (a markdown link target)."""
    readings = []
    raw = text.strip()
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1]
    if raw.lower().startswith("file://"):
        parts = urlsplit(raw)
        if parts.netloc not in ("", "localhost"):
            return []
        raw = unquote(parts.path)
    body, line = split_line(raw)
    for value, number in ((raw, None), (body, line)):
        for spelled in dict.fromkeys((value, unquote(value))):
            if not spelled:
                continue
            if spelled == "~" or spelled.startswith("~/"):
                path = home / spelled[2:] if spelled != "~" else home
            elif spelled.startswith("/"):
                path = Path(spelled)
            elif cwd is not None:
                path = cwd / spelled
            else:
                continue
            readings.append((path, number))
    seen, unique = set(), []
    for path, number in readings:
        if (str(path), number) not in seen:
            seen.add((str(path), number))
            unique.append((path, number))
    return unique


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def private(real: Path, home: Path) -> bool:
    """Rule 3, on the real path."""
    if DENIED_NAMES.fullmatch(real.name):
        return True
    if not _inside(real, home):
        return False
    relative = real.relative_to(home)
    if any(_inside(relative, Path(allowed)) for allowed in ALLOWED_INSIDE):
        return False
    return any(_inside(relative, Path(denied)) for denied in DENIED_DIRS)


def in_roots(real: Path, st: os.stat_result, *, home: Path, cwd: Path | None, uid: int) -> bool:
    """Rule 2. `$HOME`; the source's own folder; `/tmp`. Outside `$HOME` only
    files that belong to this user."""
    if _inside(real, home):
        return True
    roots = [Path("/tmp")]
    if cwd is not None:
        try:
            roots.append(cwd.resolve(strict=True))
        except (OSError, RuntimeError):
            pass
    return any(_inside(real, root) for root in roots) and st.st_uid == uid


def launchable(real: Path, st: os.stat_result, mime: str | None) -> bool:
    """Rule 5. Anything the default handler might run instead of show."""
    if stat.S_ISDIR(st.st_mode):
        return False
    if st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        return True
    if real.name.lower().endswith(LAUNCHABLE_SUFFIXES):
        return True
    if mime in LAUNCHABLE_TYPES:
        return True
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        head = os.read(fd, 4)
    finally:
        os.close(fd)
    return head == b"\x7fELF"


def resolve_path(text: str, *, home: Path, cwd: Path | None, uid: int | None = None,
                 mime_of: Callable[[Path], str | None] = lambda path: None) -> Target:
    """Rules 2-5 for one mentioned path. The first reading that exists wins."""
    uid = os.getuid() if uid is None else uid
    home = home.resolve()
    candidates = _candidates(text, home, cwd)
    # Private first, before the disk is even asked: whether ~/.ssh/config
    # exists is itself not something to tell a phone.
    if any(private(Path(os.path.abspath(path)), home) for path, _ in candidates):
        raise refused("open_denied")
    for path, line in candidates:
        try:
            real = path.resolve(strict=True)
            st = real.stat()
        except (OSError, RuntimeError):
            continue
        # Private first: nothing about a private file is looked at further.
        if private(real, home) or private(Path(os.path.abspath(path)), home):
            raise refused("open_denied")
        if not in_roots(real, st, home=home, cwd=cwd, uid=uid):
            raise refused("open_outside_roots")
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            raise refused("open_type_refused")
        mime = None if stat.S_ISDIR(st.st_mode) else mime_of(real)
        if launchable(real, st, mime):
            raise refused("open_executable")
        kind = "directory" if stat.S_ISDIR(st.st_mode) else "file"
        return Target(kind, str(real), real.name or str(real), line if kind == "file" else None, mime)
    raise refused("open_missing")


def resolve_url(text: str) -> Target:
    parts = urlsplit(text.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise refused("open_url_refused")
    return Target("url", text.strip(), parts.hostname)


# ── opening it on the desktop ──────────────────────────────────────────────
def _bounded(argv, env, timeout=2.0) -> str:
    try:
        done = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    return done.stdout[:4096].decode("utf-8", "replace").strip()


def focus_monitor_lua(output: str) -> str:
    """`hl.dsp.focus({ monitor = … })` - the same dispatcher Omarchy binds to
    CTRL+ALT+TAB (`monitor = "+1"`), here with the output's own name."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", output):
        raise ValueError("output name")
    return ('local r=hl.dispatch(hl.dsp.focus({ monitor = ' + json.dumps(output) + ' })); '
            'if type(r)~="table" or r.ok~=true then error("omodachi_monitor_focus_failed") end')


class DesktopOpener:
    """xdg-open in the graphical session, after putting focus on the output a
    Remote session is showing. Everything it runs is a fixed argv."""

    def __init__(self, *, environment: Callable[[], dict] | None = None, runner=None,
                 spawner=None, home: Path | None = None):
        self._environment = environment
        self.runner = runner or _bounded
        self.spawner = spawner or self._spawn
        self.home = home or Path.home()

    def environment(self) -> dict:
        if self._environment is not None:
            return dict(self._environment())
        from .catalog_providers import provider_environment
        return dict(provider_environment())

    def mime_of(self, path: Path, env: dict | None = None) -> str | None:
        try:
            env = env if env is not None else self.environment()
        except Exception:
            return None
        value = self.runner(("/usr/bin/xdg-mime", "query", "filetype", str(path)), env)
        return value if re.fullmatch(r"[a-z0-9.+-]+/[A-Za-z0-9.+_-]+", value or "") else None

    def handler_of(self, mime: str | None, env: dict) -> str | None:
        if not mime:
            return None
        value = self.runner(("/usr/bin/xdg-mime", "query", "default", mime), env)
        if not value and mime.startswith("text/"):
            value = self.runner(("/usr/bin/xdg-mime", "query", "default", "text/plain"), env)
        return value or None

    def place_on(self, output: str, env: dict) -> bool:
        """Focus the Remote output, and read it back."""
        try:
            lua = focus_monitor_lua(output)
        except ValueError:
            return False
        self.runner(("/usr/bin/hyprctl", "eval", lua), env)
        try:
            monitors = json.loads(self.runner(("/usr/bin/hyprctl", "-j", "monitors"), env) or "[]")
        except ValueError:
            return False
        return any(isinstance(row, dict) and row.get("name") == output and row.get("focused") is True
                   for row in monitors if isinstance(monitors, list))

    def argv_for(self, target: Target, env: dict) -> tuple[tuple[str, ...], bool]:
        """The command, and whether it opens at `target.line`."""
        if target.kind == "file" and target.line and self.has_editor_launcher():
            if self.handler_of(target.mime, env) in LINE_EDITORS:
                editor = self.default_editor()
                if editor in ("hx", "helix"):
                    return (EDITOR_LAUNCHER, f"{target.value}:{target.line}"), True
                if editor in ("nvim", "vim", "nano", "micro"):
                    return (EDITOR_LAUNCHER, f"+{target.line}", target.value), True
        return ("/usr/bin/xdg-open", target.value), False

    @staticmethod
    def has_editor_launcher() -> bool:
        return Path(EDITOR_LAUNCHER).is_file()

    def default_editor(self) -> str:
        try:
            value = (self.home / ".local/state/omarchy/defaults/editor").read_text()[:256].strip()
        except OSError:
            return "nvim"
        return value.rsplit("/", 1)[-1] or "nvim"

    @staticmethod
    def scoped(argv, env) -> list:
        """POLISH-U: what opens a file or a page runs in a transient systemd
        scope of its own, as `menu_actions.spawn` runs a menu row. Started as
        a plain child it stayed in `omodachid.service`'s cgroup, and
        `KillMode=control-group` killed it with the daemon: a core reinstall at
        19:16:26 UTC on 2026-10-04 killed the helper processes of the Chrome a
        phone had opened a page in (the browser itself had moved to its own
        scope), and the browser died on a CHECK (SIGTRAP). The same restart
        closes an editor or image viewer opened from a link."""
        runner = shutil.which("systemd-run", path=(env or {}).get("PATH"))
        if not runner:
            return list(argv)
        return [runner, "--user", "--scope", "--quiet", "--collect",
                "--unit", "omodachi-open-" + secrets.token_hex(6), "--", *argv]

    @staticmethod
    def _spawn(argv, env, cwd) -> int | None:
        """Detached, the way a launcher starts an app, in its own scope; the
        exit code if it ended within the settle time (xdg-open hands off and
        exits)."""
        process = subprocess.Popen(DesktopOpener.scoped(argv, env), env=env, cwd=cwd,
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True, close_fds=True)
        try:
            return process.wait(timeout=EXEC_SETTLE_SECONDS)
        except subprocess.TimeoutExpired:
            threading.Thread(target=process.wait, daemon=True).start()
            return None

    def open(self, target: Target, *, output: str | None) -> dict:
        try:
            env = self.environment()
        except Exception:
            raise refused("open_unavailable") from None
        where = "screen"
        if output and self.place_on(output, env):
            where = "remote"
        argv, at_line = self.argv_for(target, env)
        try:
            code = self.spawner(argv, env, str(self.home))
        except OSError:
            raise refused("open_unavailable") from None
        if code not in (None, 0):
            raise refused("open_unavailable")
        return {"where": where, "at_line": at_line}


def journal(entry: dict) -> None:
    try:
        print(json.dumps({"omodachi": "host_open", **entry}, separators=(",", ":"), default=str), flush=True)
    except (OSError, ValueError):
        pass


class HostOpen:
    """The route's whole decision, minus HTTP. `source_text` and `remote_output`
    are the service's: what the named source shows, and the Remote session's
    own output when one is running."""

    def __init__(self, *, opener: DesktopOpener | None = None, home: Path | None = None,
                 clock=time.monotonic, log=journal):
        self.opener = opener or DesktopOpener()
        self.home = home or Path.home()
        self.clock, self.log = clock, log
        self._recent: dict[str, deque] = {}
        self._lock = threading.Lock()

    def limited(self, device: str) -> bool:
        now = self.clock()
        with self._lock:
            window = self._recent.setdefault(device, deque())
            while window and now - window[0] > RATE_WINDOW_SECONDS:
                window.popleft()
            if len(window) >= RATE_LIMIT:
                return True
            window.append(now)
            return False

    def run(self, request: Request, *, device: str, role: str, haystack: str | None,
            cwd: Path | None, output: str | None) -> dict:
        entry = {"event": "host.open", "device": device, "role": role, "source": request.source["kind"],
                 "check": request.check, "asked": request.text[:512]}
        try:
            if self.limited(device):
                raise refused("open_rate_limited")
            if haystack is None:
                raise refused("open_source_gone")
            if not mentioned(request.text, haystack):
                raise refused("open_not_mentioned")
            if request.is_url:
                target = resolve_url(request.text)
            else:
                target = resolve_path(request.text, home=self.home, cwd=cwd,
                                      mime_of=lambda path: self.opener.mime_of(path))
            entry.update({"kind": target.kind, "target": target.value})
            if request.check:
                result = {"opened": False, "where": None, "at_line": False}
            else:
                result = {"opened": True, **self.opener.open(target, output=output)}
        except ServiceError as error:
            entry["outcome"] = error.code
            self.log(entry)
            raise
        entry.update({"outcome": "checked" if request.check else "opened", "where": result["where"]})
        self.log(entry)
        return {"opened": result["opened"], "kind": target.kind, "name": target.name,
                "line": target.line, "where": result["where"], "at_line": result["at_line"]}
