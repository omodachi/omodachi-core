"""MENU-3. A menu row's `when` / `checked` / `disabled`, answered the way Omarchy answers it.

Omarchy's own menu hands every one of these to bash (`MenuModel.js`
`guardScript`: `if { <expression>; } >/dev/null 2>&1; then …`). Core used to
know a handful of shapes - `omarchy-pkg-present X`, `[[ "$(omarchy-default-
browser)" == "x" ]]` - and published every other expression as
`condition_adapter_unavailable`, so 57 rows of Leo's menu (every Install/Remove
toolchain, Suspend, Hibernate, Stop screenrecording …) drew `不可用` in the App
while the same rows worked on the desktop. This module is the rest:

* **Evaluation** - `bash -c <expression>` in the graphical session's
  environment (the compositor's PATH, HOME, XDG_*, the Hyprland signature),
  stdin/stdout/stderr closed, its own process group, 2 s each. Exit 0 is true,
  anything else false, a timeout or a spawn failure `unknown` - which is *not*
  `unavailable`: an unknown row is drawn and can be tapped, and what the tap
  does is the host's business, exactly as on the desktop, where a guard that
  never answered leaves its row showing.

  The expressions come from the host's own menu files only (Omarchy's default,
  the user's extension file, Omodachi's), which is the text Omarchy runs in
  bash as this same user already. A provider row, a client parameter or
  anything else a device can influence is never handed to bash.

* **Not re-running them** (PERF-4 §0 was this loop running every 2 s). Each
  expression is read once and then re-read only when something says its answer
  may have moved:

  - *file* - `[[ -f/-d/-x … ]]`, `compgen -G`, `grep … <files>`, and the few
    Omarchy helpers that are a file test in disguise: the paths are pulled out
    of the expression and watched with inotify;
  - *package* - `omarchy-pkg-present`, `omarchy-cmd-present`, `pacman`: the
    package database directory (`/var/lib/pacman/local`) and the command's
    PATH entries are watched the same way;
  - *demand* - everything else (`pgrep`, `systemctl`, `omarchy-network-status`,
    the default-app getters …): re-read when a menu is opened
    (`panel.summon`, `GET /v1/catalog`) if the last reading is older than 10 s;
  - *static* - hardware probes and the root filesystem type: read once.

  With nothing happening the count of shells spawned is zero.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import fnmatch
import glob as _glob
import json
import os
from pathlib import Path
import re
import select
import shlex
import struct
import subprocess
import threading
import time
from typing import Any, Callable, Iterable, Mapping

#: One expression may take this long. Omarchy's own guard batch has no bound
#: at all; a 2 s ceiling is the spec's (MENU-3 §1).
TIMEOUT_SECONDS = 2.0
#: At most this many shells at once, at build time and on every later pass.
MAX_WORKERS = 8
#: A demand-class reading younger than this is reused when a menu opens.
DEMAND_TTL_SECONDS = 10.0
#: A burst of filesystem events (pacman extracting a package) settles before
#: the expressions it touched are read again.
SETTLE_SECONDS = 1.0

PACKAGE_DATABASE = "/var/lib/pacman/local"

#: Reason codes carried by an `unknown` reading. They are reasons, not errors:
#: the row they belong to is drawn and can be tapped.
UNKNOWN_PENDING = "condition_pending"
UNKNOWN_TIMEOUT = "condition_timeout"
UNKNOWN_SPAWN = "condition_spawn_failed"
UNKNOWN_FAILED = "condition_adapter_failed"


def known(value: bool) -> dict[str, Any]:
    return {"status": "available", "value": bool(value)}


def unknown(reason: str) -> dict[str, Any]:
    return {"status": "unknown", "value": None, "reason": reason}


# --------------------------------------------------------------- classification

@dataclass(frozen=True)
class Triggers:
    """What can change an expression's answer.

    `paths` are absolute path patterns (glob characters allowed) whose
    appearance, disappearance or rewrite re-reads it. `demand` means only a
    fresh look can tell - it is re-read when a menu opens. Neither means the
    answer is fixed for the life of the session.
    """
    paths: tuple[str, ...] = ()
    demand: bool = False

    @property
    def kind(self) -> str:
        if self.demand:
            return "demand"
        return "file" if self.paths else "static"


#: Commands whose answer does not move while the session runs.
STATIC_COMMANDS = frozenset({"findmnt", "omarchy-hibernation-available", "true", "false", ":", "uname"})
FILE_TEST_OPERATORS = frozenset({"-e", "-f", "-d", "-x", "-r", "-w", "-s", "-L", "-h", "-p", "-S", "-a"})
SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")", "{", "}", ";;"})
_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
_PLACEHOLDER = "__omodachi_substitution__"


def command_directories(home: Path) -> tuple[str, ...]:
    """Where `omarchy-cmd-present` finds a command on this host."""
    return (str(home / ".local/share/mise/shims"), str(home / ".local/bin"), "/usr/local/bin",
            "/usr/bin", "/usr/share/omarchy/bin")


def _expand(word: str, home: Path) -> str | None:
    """A path word with `~` / `$HOME` resolved; None if it needs anything else."""
    text = word
    if text == "~" or text.startswith("~/"):
        text = str(home) + text[1:]
    text = text.replace("${HOME}", str(home)).replace("$HOME", str(home))
    if "$" in text or "`" in text or not text.startswith("/"):
        return None
    return os.path.normpath(text)


def _simple_commands(expression: str) -> tuple[list[list[str]], list[str]] | None:
    """Split into simple commands; command substitutions come back separately."""
    substitutions: list[str] = []
    def lift(match: re.Match) -> str:
        substitutions.append(match.group(1) if match.group(1) is not None else match.group(2))
        return _PLACEHOLDER
    text = _SUBSTITUTION.sub(lift, expression)
    if "$(" in text or "`" in text or "<(" in text or ">(" in text:
        return None                                   # nested or process substitution
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|(){}")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token in SEPARATORS or set(token) <= set(";&|(){}"):
            commands.append([])
        else:
            commands[-1].append(token)
    return [command for command in commands if command], substitutions


def classify(expression: str, home: Path | None = None) -> Triggers:
    """What re-reads `expression`. Anything not understood is demand-class."""
    home = Path(home) if home is not None else Path.home()
    split = _simple_commands(expression)
    if split is None:
        return Triggers(demand=True)
    commands, substitutions = split
    paths: list[str] = []
    demand = False
    for inner in substitutions:
        found = classify(inner, home)
        paths.extend(found.paths)
        demand = demand or found.demand
    for words in commands:
        while words and words[0] == "!":
            words = words[1:]
        if not words:
            continue
        found = _classify_command(words, home)
        if found is None:
            demand = True
            continue
        paths.extend(found)
    unique = tuple(dict.fromkeys(paths))
    return Triggers(paths=unique, demand=demand)


def _classify_command(words: list[str], home: Path) -> list[str] | None:
    """Paths that decide this simple command, [] for a fixed answer, None = demand."""
    name, arguments = words[0], words[1:]
    if name in {"[[", "[", "test"}:
        if name != "test":
            closing = "]]" if name == "[[" else "]"
            if closing not in arguments:
                return None
            arguments = arguments[:arguments.index(closing)]
        paths: list[str] = []
        index = 0
        while index < len(arguments):
            word = arguments[index]
            if word in FILE_TEST_OPERATORS and index + 1 < len(arguments):
                path = _expand(arguments[index + 1], home)
                if path is None:
                    return None
                paths.append(path)
                index += 2
                continue
            if "$" in word and _PLACEHOLDER not in word:
                return None                           # a variable we do not know
            index += 1
        return paths
    if name == "compgen":
        if len(arguments) == 2 and arguments[0] == "-G":
            path = _expand(arguments[1], home)
            return [path] if path else None
        return None
    if name == "grep":
        files = [word for word in arguments if not word.startswith("-")][1:]
        if not files:
            return None
        paths = [_expand(word, home) for word in files]
        return None if any(path is None for path in paths) else paths
    if name == "omarchy-toggle-enabled" and len(arguments) == 1 and re.fullmatch(r"[A-Za-z0-9_.-]+", arguments[0]):
        # `[[ -f "$HOME/.local/state/omarchy/toggles/$1" ]]`, the whole script.
        return [str(home / ".local/state/omarchy/toggles" / arguments[0])]
    if name == "omarchy-theme-extras" and not arguments:
        # A theme directory that is a git clone: `~/.config/omarchy/themes/*/.git`.
        themes = home / ".config/omarchy/themes"
        return [str(themes / "*"), str(themes / "*" / ".git")]
    if name == "omarchy-sudo-docker" and arguments == ["--configured"]:
        return ["/etc/group"]                         # `id -nG "$USER" | grep -qw docker`
    if name == "flatpak" and len(arguments) == 2 and arguments[0] == "info":
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", arguments[1]):
            return None
        return ["/var/lib/flatpak/app/" + arguments[1],
                str(home / ".local/share/flatpak/app" / arguments[1])]
    if name in {"omarchy-pkg-present", "pacman"}:
        return [PACKAGE_DATABASE + "/*"]
    if name == "omarchy-cmd-present":
        if not arguments or any(not re.fullmatch(r"[A-Za-z0-9_.+-]+", word) for word in arguments):
            return None
        return [directory + "/" + word for word in arguments for directory in command_directories(home)]
    if name.startswith("omarchy-hw-") or name in STATIC_COMMANDS:
        return []
    return None


# ------------------------------------------------------------------ evaluation

def condition_environment() -> dict[str, str]:
    """The graphical session's environment, as Omarchy's menu would see it.

    The compositor's own PATH and XDG fields (`provider_environment`, which
    reads them from Hyprland's `/proc` entry), plus the session keys
    (`HYPRLAND_INSTANCE_SIGNATURE`, `XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY`). With
    no graphical session - a host still booting - HOME/USER and a fixed PATH,
    which is enough for the file and package expressions.
    """
    home = str(Path.home())
    try:
        import pwd
        user = pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        user = os.environ.get("USER", "")
    env = {"HOME": home, "USER": user, "LOGNAME": user, "SHELL": "/bin/bash", "LANG": "C.UTF-8",
           "PATH": ":".join(command_directories(Path(home))[:4] + ("/bin",))}
    try:
        from .catalog_providers import provider_environment
        env.update(provider_environment())
    except Exception:
        try:
            from .graphical import compositor_environment
            env.update(compositor_environment())
        except Exception:
            pass
    env["HOME"], env["USER"], env["LOGNAME"] = home, user, user
    parts = [part for part in env.get("PATH", "").split(":") if part]
    for directory in ("/usr/bin", "/bin", "/usr/share/omarchy/bin"):
        if directory not in parts:
            parts.append(directory)
    env["PATH"] = ":".join(parts)
    env["OMARCHY_PATH"] = "/usr/share/omarchy"
    return env


def run_bash(expression: str, environment: Mapping[str, str], *, timeout: float = TIMEOUT_SECONDS) -> dict[str, Any]:
    """`bash -c <expression>`: 0 → true, non-zero → false, no answer → unknown."""
    try:
        process = subprocess.Popen(["/bin/bash", "-c", expression], env=dict(environment), shell=False,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True,
                                   cwd=environment.get("HOME") or "/")
    except (OSError, ValueError, subprocess.SubprocessError):
        return unknown(UNKNOWN_SPAWN)
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        return unknown(UNKNOWN_TIMEOUT)
    # A finished expression's group is left alone, as Omarchy's own guard
    # batch leaves it: only one that ran out of time is killed.
    return known(code == 0)


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, 9)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


# --------------------------------------------------------------------- engine

@dataclass
class _Entry:
    triggers: Triggers
    reading: dict[str, Any] | None = None
    evaluated_at: float = -float("inf")
    due: bool = True
    not_before: float = -float("inf")
    shell: bool = False


class ConditionEngine:
    """Every condition reading the catalog holds, and when each is taken again.

    Keys are expressions (or `state:<entry id>` for a reviewed checked-state
    reader). The engine never decides a value itself: `evaluate` is handed a
    callable per key - the reviewed adapter where one exists, `bash -c`
    otherwise - and records what came back.

    Thread safety: the maintenance tick evaluates on a worker thread while the
    event loop reads; the watcher thread marks keys due. One lock guards the
    table; nothing slow happens while it is held.
    """

    def __init__(self, *, runner: Callable[[str, Mapping[str, str]], dict] | None = None,
                 environment: Callable[[], Mapping[str, str]] | None = None,
                 clock: Callable[[], float] = time.monotonic, watcher: "PathWatcher | None" = None,
                 home: Path | None = None, timeout: float = TIMEOUT_SECONDS, max_workers: int = MAX_WORKERS,
                 demand_ttl: float = DEMAND_TTL_SECONDS, settle: float = SETTLE_SECONDS,
                 log: Callable[[str, str], None] | None = None):
        self.runner = runner or (lambda expression, env: run_bash(expression, env, timeout=timeout))
        self.environment = environment or condition_environment
        self.clock = clock
        self.home = Path(home) if home is not None else Path.home()
        self.max_workers = max(1, min(MAX_WORKERS, int(max_workers)))
        self.demand_ttl = demand_ttl
        self.settle = settle
        self.log = log or _journal
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}
        # Keys some thread is reading right now: the maintenance tick and a
        # menu-open refresh can overlap, and neither should spawn the other's
        # shells a second time.
        self._inflight: set[str] = set()
        self._watches_stale = False
        self._spawns: deque[float] = deque()
        self.shells_total = 0
        self.passes = 0
        self.watcher = watcher
        if watcher is not None:
            watcher.on_change = self._paths_changed

    # -- the table
    def track(self, key: str, triggers: Triggers | None = None, *, shell: bool = False) -> None:
        """Make `key` known. A new key is due; a known one keeps its reading."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._entries[key] = _Entry(triggers or classify(key, self.home), shell=shell)
                if self._entries[key].triggers.paths:
                    self._watches_stale = True       # re-armed once, before the next reading
            elif shell and not entry.shell:
                entry.shell = True

    def retain(self, keys: Iterable[str]) -> None:
        """Forget every key not in `keys` - a menu source edit removed its rows."""
        keep = set(keys)
        with self._lock:
            gone = [key for key in self._entries if key not in keep]
            for key in gone:
                del self._entries[key]
            if gone:
                self._watches_stale = True
        self.sync_watches()

    def known_keys(self) -> set[str]:
        with self._lock:
            return set(self._entries)

    def reading(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._entries.get(key)
            return dict(entry.reading) if entry is not None and entry.reading is not None else None

    def triggers(self, key: str) -> Triggers | None:
        with self._lock:
            entry = self._entries.get(key)
            return entry.triggers if entry is not None else None

    def is_due(self, key: str) -> bool:
        """Owed a reading now: never read, or marked and past its settle time."""
        with self._lock:
            entry = self._entries.get(key)
            return (entry is None or entry.reading is None
                    or entry.due and entry.not_before <= self.clock())

    def due_keys(self, keys: Iterable[str] | None = None) -> list[str]:
        """Keys whose next reading is owed and whose settle time has passed."""
        now = self.clock()
        with self._lock:
            candidates = self._entries if keys is None else [key for key in keys if key in self._entries]
            return [key for key in candidates
                    if (self._entries[key].due or self._entries[key].reading is None)
                    and self._entries[key].not_before <= now]

    def mark_due(self, keys: Iterable[str], *, settle: float = 0.0) -> list[str]:
        now = self.clock()
        marked = []
        with self._lock:
            for key in keys:
                entry = self._entries.get(key)
                if entry is None:
                    continue
                entry.due = True
                entry.not_before = max(entry.not_before, now + settle) if settle else entry.not_before
                marked.append(key)
        return marked

    def _watched(self, entry: _Entry) -> bool:
        return bool(entry.triggers.paths) and self.watcher is not None and self.watcher.available

    def demand(self, keys: Iterable[str] | None = None) -> list[str]:
        """A menu opened: owe a reading for every demand-class key older than the TTL.

        A file-class key counts as demand-class while nothing is watching its
        paths (no inotify on this platform, or the watcher failed), so a broken
        watcher degrades to "re-read when looked at", never to "never again".
        """
        now = self.clock()
        marked = []
        with self._lock:
            for key in (self._entries if keys is None else keys):
                entry = self._entries.get(key)
                if entry is None:
                    continue
                if not (entry.triggers.demand or (entry.triggers.paths and not self._watched(entry))):
                    continue
                if now - entry.evaluated_at >= self.demand_ttl:
                    entry.due = True
                    marked.append(key)
        return marked

    def _paths_changed(self, keys: Iterable[str]) -> None:
        self.mark_due(keys, settle=self.settle)

    def sync_watches(self) -> None:
        """Re-arm the watcher on the current keys' paths, if any key came or went."""
        if self.watcher is None:
            return
        with self._lock:
            if not self._watches_stale:
                return
            self._watches_stale = False
            patterns = {key: entry.triggers.paths for key, entry in self._entries.items() if entry.triggers.paths}
        self.watcher.watch(patterns)

    # -- reading
    def evaluate(self, jobs: Mapping[str, Callable[[], Any] | None], *, reason: str = "refresh") -> set[str]:
        """Take a reading for every key in `jobs`; returns the keys whose value moved.

        A job of None is a shell expression - the key itself is handed to bash.
        A callable is a reviewed adapter: it returns a bool, or a full reading
        dict (the checked-state readers do), and an exception is an
        `unavailable` reading exactly as before MENU-3.
        """
        self.sync_watches()
        with self._lock:
            jobs = {key: job for key, job in jobs.items() if key not in self._inflight}
            self._inflight.update(jobs)
        if not jobs:
            return set()
        try:
            return self._evaluate(jobs, reason)
        finally:
            with self._lock:
                self._inflight.difference_update(jobs)

    def _evaluate(self, jobs: Mapping[str, Callable[[], Any] | None], reason: str) -> set[str]:
        started = self.clock()
        environment: Mapping[str, str] | None = None
        if any(job is None for job in jobs.values()):
            try:
                environment = self.environment()
            except Exception:
                environment = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        def run(key: str, job):
            if job is None:
                with self._lock:
                    self.shells_total += 1
                    self._spawns.append(time.monotonic())
                try:
                    return key, self.runner(key, environment)
                except Exception:
                    return key, unknown(UNKNOWN_SPAWN)
            try:
                value = job()
            except Exception:
                return key, {"status": "unavailable", "value": None, "reason": UNKNOWN_FAILED}
            if isinstance(value, dict):
                return key, dict(value)
            if type(value) is not bool:
                return key, {"status": "unavailable", "value": None, "reason": UNKNOWN_FAILED}
            return key, known(value)
        items = list(jobs.items())
        if len(items) == 1:
            results = [run(*items[0])]
        else:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(items))) as pool:
                results = list(pool.map(lambda item: run(*item), items))
        changed: set[str] = set()
        now = self.clock()
        shells = unknowns = 0
        with self._lock:
            for key, reading in results:
                entry = self._entries.get(key)
                if entry is None:
                    entry = self._entries[key] = _Entry(classify(key, self.home), shell=jobs[key] is None)
                if jobs[key] is None:
                    shells += 1
                if reading.get("status") == "unknown":
                    unknowns += 1
                if entry.reading != reading:
                    changed.add(key)
                entry.reading = reading
                entry.evaluated_at = now
                entry.due = False
                entry.not_before = -float("inf")
            self.passes += 1
        if shells:
            elapsed = (self.clock() - started) * 1000
            self.log("conditions pass={} evaluated={} shells={} unknown={} changed={} ms={:.0f} shells_5m={} shells_total={}"
                     .format(reason, len(results), shells, unknowns, len(changed), elapsed,
                             self.shells_recent(), self.shells_total), "info")
        return changed

    def shells_recent(self, window: float = 300.0) -> int:
        cutoff = time.monotonic() - window
        with self._lock:
            while self._spawns and self._spawns[0] < cutoff:
                self._spawns.popleft()
            return len(self._spawns)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            kinds: dict[str, int] = {}
            for entry in self._entries.values():
                kinds[entry.triggers.kind] = kinds.get(entry.triggers.kind, 0) + 1
            return {"expressions": len(self._entries), "kinds": kinds, "shells_total": self.shells_total,
                    "watching": bool(self.watcher is not None and self.watcher.available)}

    def close(self) -> None:
        if self.watcher is not None:
            self.watcher.close()


def _journal(message: str, level: str) -> None:
    print(json.dumps({"log": message, "level": level}), flush=True)


# -------------------------------------------------------------------- watcher

def watch_points(pattern: str) -> list[tuple[str, str]]:
    """(directory, name pattern) pairs whose events can change `pattern`'s answer.

    Walking down the path: an existing plain component is descended into, a
    missing one is watched for in its parent (so a path that does not exist yet
    is noticed when its first missing ancestor appears), and a glob component is
    watched in its parent *and* descended into for every current match - a new
    match has to be noticed too. The last component is always watched in its
    parent: that is where it is created, removed or rewritten.
    """
    parts = Path(pattern).parts
    if not parts or parts[0] != "/":
        return []
    points: list[tuple[str, str]] = []
    frontier = ["/"]
    for index, part in enumerate(parts[1:], start=1):
        last = index == len(parts) - 1
        following: list[str] = []
        for directory in frontier:
            if last:
                points.append((directory, part))
                continue
            if _glob.has_magic(part):
                points.append((directory, part))
                following.extend(os.path.join(directory, name) for name in _safe_listdir(directory)
                                 if fnmatch.fnmatchcase(name, part) and os.path.isdir(os.path.join(directory, name)))
            elif os.path.isdir(os.path.join(directory, part)):
                following.append(os.path.join(directory, part))
            else:
                points.append((directory, part))
        frontier = following
        if not frontier:
            break
    return points


def _safe_listdir(directory: str) -> list[str]:
    try:
        return os.listdir(directory)
    except OSError:
        return []


IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ONLYDIR = 0x01000000
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000
WATCH_MASK = (IN_ATTRIB | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE
              | IN_DELETE_SELF | IN_MOVE_SELF | IN_ONLYDIR)
_EVENT = struct.Struct("iIII")


class PathWatcher:
    """inotify on the directories `watch_points` names, one daemon thread.

    Linux only; elsewhere (and if inotify cannot be opened) `available` is
    False and the engine treats file-class expressions as demand-class.
    `on_change(keys)` is called from the watcher thread with the keys whose
    paths saw an event.
    """

    def __init__(self, *, libc=None):
        self.on_change: Callable[[Iterable[str]], None] | None = None
        self._lock = threading.Lock()
        self._patterns: dict[str, tuple[str, ...]] = {}
        self._points: dict[str, list[tuple[str, str]]] = {}     # directory -> [(name pattern, key)]
        self._wd: dict[str, int] = {}                           # directory -> wd
        self._dirs: dict[int, str] = {}
        self._fd = -1
        self._thread: threading.Thread | None = None
        self._stop_r = self._stop_w = -1
        self.events = 0
        try:
            import ctypes
            import ctypes.util
            self._libc = libc or ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
            self._fd = self._libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        except (OSError, AttributeError):
            self._fd = -1
        self.available = self._fd >= 0

    def watch(self, patterns: Mapping[str, tuple[str, ...]]) -> None:
        if not self.available:
            return
        with self._lock:
            self._patterns = {key: tuple(paths) for key, paths in patterns.items()}
            self._resync()
        if self._thread is None:
            self._stop_r, self._stop_w = os.pipe()
            self._thread = threading.Thread(target=self._run, name="omodachi-condition-watch", daemon=True)
            self._thread.start()

    def _resync(self) -> None:
        points: dict[str, list[tuple[str, str]]] = {}
        for key, paths in self._patterns.items():
            for pattern in paths:
                for directory, name in watch_points(pattern):
                    points.setdefault(directory, []).append((name, key))
        for directory in [d for d in self._wd if d not in points]:
            wd = self._wd.pop(directory)
            self._dirs.pop(wd, None)
            self._libc.inotify_rm_watch(self._fd, wd)
        for directory in points:
            if directory in self._wd:
                continue
            wd = self._libc.inotify_add_watch(self._fd, directory.encode(), WATCH_MASK)
            if wd >= 0:
                self._wd[directory] = wd
                self._dirs[wd] = directory
        self._points = points

    def _run(self) -> None:
        while True:
            try:
                ready, _, _ = select.select([self._fd, self._stop_r], [], [])
            except (OSError, ValueError):
                return
            if self._stop_r in ready:
                return
            try:
                data = os.read(self._fd, 65536)
            except BlockingIOError:
                continue
            except OSError:
                return
            self._dispatch(data)

    def _dispatch(self, data: bytes) -> None:
        keys: set[str] = set()
        structural = False
        with self._lock:
            offset = 0
            while offset + _EVENT.size <= len(data):
                wd, mask, _cookie, length = _EVENT.unpack_from(data, offset)
                name = data[offset + _EVENT.size: offset + _EVENT.size + length].split(b"\0", 1)[0]
                offset += _EVENT.size + length
                self.events += 1
                if mask & IN_Q_OVERFLOW:
                    keys.update(self._patterns)
                    structural = True
                    continue
                directory = self._dirs.get(wd)
                if directory is None:
                    continue
                if mask & (IN_DELETE_SELF | IN_MOVE_SELF | IN_IGNORED):
                    for _name, key in self._points.get(directory, ()):
                        keys.add(key)
                    structural = True
                    if mask & IN_IGNORED:
                        self._wd.pop(directory, None)
                        self._dirs.pop(wd, None)
                    continue
                text = name.decode(errors="replace")
                for pattern, key in self._points.get(directory, ()):
                    if fnmatch.fnmatchcase(text, pattern):
                        keys.add(key)
                        if mask & (IN_CREATE | IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO):
                            structural = True
            if structural:
                self._resync()
        if keys and self.on_change is not None:
            self.on_change(keys)

    def close(self) -> None:
        if self._stop_w >= 0:
            try:
                os.write(self._stop_w, b"x")
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1)
        for fd in (self._fd, self._stop_r, self._stop_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._fd = self._stop_r = self._stop_w = -1
        self.available = False


# ------------------------------------------------------ reviewed checked states

def checked_state_triggers(entry_id: str, home: Path | None = None) -> Triggers:
    """What re-reads one of `live_menu_adapter`'s reviewed checked states.

    Those readers are keyed by row id, not by an expression, so there is
    nothing to classify: the flag-file ones are the file each reads, the
    battery percentage is `shell.json`, and the rest (Do Not Disturb, night
    light, workspace layout) are asked of a running process - demand-class.
    """
    home = Path(home) if home is not None else Path.home()
    try:
        from .live_menu_adapter import FLAGS
    except ImportError:
        FLAGS = {}
    name = entry_id.removeprefix("trigger.toggle.")
    if name in FLAGS:
        return Triggers(paths=(str(home / ".local/state/omarchy" / FLAGS[name][0]),))
    if name == "battery-percentage":
        return Triggers(paths=(str(home / ".config/omarchy/shell.json"),))
    return Triggers(demand=True)
