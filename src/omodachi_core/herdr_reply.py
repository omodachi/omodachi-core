"""HERDR-11 §2: a codex or claude pane's latest reply, from the agent's own log.

The mirror is the terminal as Herdr drew it; on a phone that is a cramped,
redrawing grid. The words the agent said are also in its own session log —
codex's rollout file (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`) and
Claude Code's project log (`~/.claude/projects/<cwd>/<session>.jsonl`) — so
core reads them from there: read-only, by byte-offset tails, never from the
PTY and never by asking the agent anything.

The hard part is not reading, it is knowing *which* log is this pane's. A log
is only ever tied to a pane through the processes running in it:

- Herdr's `pane.process_info` names the pane's foreground processes.
- **codex**: the rollout it is writing is open in that process tree (codex
  0.160 keeps it open in its `app-server-daemon` child, probed on the host).
  Exactly one rollout open in the tree is a match. When none is open, a
  single `thread-writer-locks/<thread>.lock` held open names the thread, and
  the rollout is the one with that id in its name.
- **claude**: its log is opened per write, not held. A `--resume`/
  `--session-id` on its command line names the log; otherwise its working
  folder names the project directory, and the newest log written since the
  process started is it — but only when it is the one claude process on this
  host in that folder. Two claudes in one folder are ambiguous.
- A Herdr that reports the agent's session itself (`agent_session`, kind
  `path` or `id`, protocol 21+) is believed first.

Anything else — no match, two matches, a file outside the agent's own log
folder, a log with no reply yet — is `available: false` with a reason a
client never shows: the App simply has no reply card for that pane.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Callable, Iterable

CODEX = "codex"
CLAUDE = "claude"
AGENTS = (CODEX, CLAUDE)
# Bytes read from the end of a log for a reply; if the last reply is further
# back than that (a long tool run after it), once more with the larger window.
TAIL_BYTES = 512 * 1024
MAX_TAIL_BYTES = 4 * 1024 * 1024
# What goes on the wire. A reply longer than this is cut, and says so.
MAX_TEXT = 32 * 1024
# How many processes a pane's tree may have before the walk stops: a pane is
# one agent and its tools, not a build farm.
MAX_TREE = 256
MAX_FDS = 1024
ROLLOUT = re.compile(r"rollout-[0-9T:-]+-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl\Z")
THREAD_LOCK = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.lock\Z")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


def agent_family(name: Any) -> str | None:
    """`codex`, `claude` (also `claude-code`), or None for anything else."""
    if not isinstance(name, str):
        return None
    name = name.strip().lower()
    if name.startswith(CODEX):
        return CODEX
    if name.startswith(CLAUDE):
        return CLAUDE
    return None


def claude_project(cwd: str) -> str:
    """Claude Code's project directory name for a working folder: every
    character that is not a letter or a digit becomes `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


class ProcessTable:
    """The few things read from `/proc` about the user's own processes."""

    def __init__(self, proc: Path | str = "/proc", *, uid: int | None = None) -> None:
        self.proc = Path(proc)
        self.uid = os.getuid() if uid is None else uid

    def _stat(self, pid: int) -> list[str] | None:
        try:
            raw = (self.proc / str(pid) / "stat").read_text("utf-8", "replace")
        except OSError:
            return None
        # `comm` is in parentheses and may itself contain spaces and `)`.
        close = raw.rfind(")")
        if close < 0:
            return None
        return [raw[:close + 1]] + raw[close + 2:].split()

    def parent(self, pid: int) -> int | None:
        fields = self._stat(pid)
        try:
            return int(fields[2]) if fields else None
        except (IndexError, ValueError):
            return None

    def started(self, pid: int) -> float | None:
        """Wall-clock start time, from the boot time and the clock ticks."""
        fields = self._stat(pid)
        try:
            ticks = int(fields[20])
            boot = next(int(line.split()[1]) for line in (self.proc / "stat").read_text().splitlines()
                        if line.startswith("btime "))
        except (TypeError, IndexError, ValueError, StopIteration, OSError):
            return None
        return boot + ticks / float(os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100)

    def pids(self) -> list[int]:
        try:
            names = os.listdir(self.proc)
        except OSError:
            return []
        out = []
        for name in names:
            if name.isdigit():
                try:
                    if (self.proc / name).stat().st_uid == self.uid:
                        out.append(int(name))
                except OSError:
                    continue
        return out

    def tree(self, roots: Iterable[int]) -> list[int]:
        """The roots and everything under them, at most `MAX_TREE`."""
        children: dict[int, list[int]] = {}
        for pid in self.pids():
            parent = self.parent(pid)
            if parent is not None:
                children.setdefault(parent, []).append(pid)
        seen, queue = [], [pid for pid in roots if isinstance(pid, int) and pid > 0]
        while queue and len(seen) < MAX_TREE:
            pid = queue.pop(0)
            if pid in seen:
                continue
            seen.append(pid)
            queue.extend(children.get(pid, ()))
        return seen

    def open_files(self, pid: int) -> list[str]:
        directory = self.proc / str(pid) / "fd"
        try:
            names = os.listdir(directory)[:MAX_FDS]
        except OSError:
            return []
        out = []
        for name in names:
            try:
                out.append(os.readlink(directory / name))
            except OSError:
                continue
        return out

    def cwd(self, pid: int) -> str | None:
        try:
            return os.readlink(self.proc / str(pid) / "cwd")
        except OSError:
            return None

    def cmdline(self, pid: int) -> list[str]:
        try:
            raw = (self.proc / str(pid) / "cmdline").read_bytes()
        except OSError:
            return []
        return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]

    def name(self, pid: int) -> str:
        try:
            return (self.proc / str(pid) / "comm").read_text("utf-8", "replace").strip()
        except OSError:
            return ""


def _inside(path: str | Path, root: Path) -> Path | None:
    """`path` resolved, when it is a regular file inside `root`; else None."""
    try:
        resolved = Path(os.path.realpath(path))
        resolved.relative_to(os.path.realpath(root))
        if not stat.S_ISREG(resolved.stat().st_mode):
            return None
    except (OSError, ValueError):
        return None
    return resolved


class Match:
    def __init__(self, agent: str, path: Path | None, reason: str | None, how: str | None = None):
        self.agent, self.path, self.reason, self.how = agent, path, reason, how


def match_codex(pids: list[int], table: ProcessTable, root: Path) -> Match:
    tree = table.tree(pids)
    files, threads = set(), set()
    for pid in tree:
        for target in table.open_files(pid):
            name = os.path.basename(target)
            if ROLLOUT.fullmatch(name):
                inside = _inside(target, root)
                if inside is not None:
                    files.add(inside)
            elif "thread-writer-locks" in target:
                found = THREAD_LOCK.fullmatch(name)
                if found:
                    threads.add(found.group(1))
    if len(files) == 1:
        return Match(CODEX, files.pop(), None, "open_rollout")
    if len(files) > 1:
        return Match(CODEX, None, "ambiguous")
    if len(threads) == 1:
        path = find_rollout(root, threads.pop())
        if path is not None:
            return Match(CODEX, path, None, "thread_lock")
    if len(threads) > 1:
        return Match(CODEX, None, "ambiguous")
    return Match(CODEX, None, "no_match")


def find_rollout(root: Path, thread: str, *, days: int = 62) -> Path | None:
    """The rollout with this thread id in its name: newest day folders first,
    at most `days` of them, so a lookup never walks years of logs."""
    if not UUID.fullmatch(thread):
        return None
    try:
        years = sorted((p for p in root.iterdir() if p.name.isdigit()), reverse=True)
    except OSError:
        return None
    looked = 0
    for year in years:
        for month in sorted((p for p in _dirs(year) if p.name.isdigit()), reverse=True):
            for day in sorted((p for p in _dirs(month) if p.name.isdigit()), reverse=True):
                looked += 1
                if looked > days:
                    return None
                for candidate in _files(day):
                    found = ROLLOUT.fullmatch(candidate.name)
                    if found and found.group(1) == thread:
                        return _inside(candidate, root)
    return None


def _dirs(path: Path) -> list[Path]:
    try:
        return [p for p in path.iterdir() if p.is_dir()]
    except OSError:
        return []


def _files(path: Path) -> list[Path]:
    try:
        return [p for p in path.iterdir()]
    except OSError:
        return []


def match_claude(pids: list[int], table: ProcessTable, root: Path) -> Match:
    tree = table.tree(pids)
    claude = [pid for pid in tree if table.name(pid) == CLAUDE
              or os.path.basename((table.cmdline(pid) or [""])[0]) == CLAUDE]
    if not claude:
        return Match(CLAUDE, None, "no_match")
    # The outermost claude in the pane is the one the pane runs; its own
    # children (tools, subagents) share its log.
    process = claude[0]
    for target in (path for pid in tree for path in table.open_files(pid)):
        if target.endswith(".jsonl"):
            inside = _inside(target, root)
            if inside is not None and inside.parent.parent == Path(os.path.realpath(root)):
                return Match(CLAUDE, inside, None, "open_log")
    cwd = table.cwd(process)
    if not cwd:
        return Match(CLAUDE, None, "no_match")
    project = Path(root) / claude_project(cwd)
    arguments = table.cmdline(process)
    for flag in ("--resume", "-r", "--session-id"):
        if flag in arguments:
            index = arguments.index(flag)
            if index + 1 < len(arguments) and UUID.fullmatch(arguments[index + 1]):
                named = _inside(project / (arguments[index + 1] + ".jsonl"), root)
                # After `/clear` claude writes a new log; the named one is only
                # trusted while it is the newest in the folder.
                newest = _newest(project, since=None)
                if named is not None and newest == named:
                    return Match(CLAUDE, named, None, "command_line")
    # Two claude processes in one folder write two logs there; which is whose
    # cannot be told from outside, so neither is shown.
    others = [pid for pid in table.pids() if pid not in tree and table.name(pid) == CLAUDE
              and table.cwd(pid) == cwd and table.parent(pid) not in claude]
    if others:
        return Match(CLAUDE, None, "ambiguous")
    started = table.started(process)
    newest = _newest(project, since=(started - 2.0) if started is not None else None)
    if newest is None:
        return Match(CLAUDE, None, "no_match")
    return Match(CLAUDE, newest, None, "project_folder")


def _newest(project: Path, since: float | None) -> Path | None:
    best, best_at = None, None
    for candidate in _files(project):
        if candidate.suffix != ".jsonl" or not UUID.fullmatch(candidate.stem):
            continue
        try:
            at = candidate.stat().st_mtime
        except OSError:
            continue
        if since is not None and at < since:
            continue
        if best_at is None or at > best_at:
            best, best_at = candidate, at
    return _inside(best, project) if best is not None else None


# --- reading a log ----------------------------------------------------------

def _tail_lines(path: Path, size: int, window: int) -> list[bytes]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    handle = os.open(path, flags)
    try:
        start = max(0, size - window)
        os.lseek(handle, start, os.SEEK_SET)
        chunks, left = [], size - start
        while left > 0:
            chunk = os.read(handle, min(left, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
    finally:
        os.close(handle)
    data = b"".join(chunks)
    lines = data.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]  # the first line was cut in the middle
    return lines


def _json(line: bytes) -> dict | None:
    if not line.strip():
        return None
    try:
        value = json.loads(line)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def codex_reply(lines: list[bytes]) -> dict | None:
    """The last assistant message in a rollout, newest last.

    codex 0.160 writes `{"type":"response_item","payload":{"type":"message",
    "role":"assistant","content":[{"type":"output_text","text":…}],
    "phase":"commentary"|"final_answer"?}}`; older codex wrote the same item
    without the envelope. `task_complete` after it says the turn ended.
    """
    complete = False
    for line in reversed(lines):
        item = _json(line)
        if item is None:
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else None
        if payload is not None and item.get("type") == "event_msg" and payload.get("type") == "task_complete":
            complete = True
            continue
        if payload is not None and item.get("type") == "event_msg" and payload.get("type") == "task_started":
            complete = False
            continue
        message = payload if (payload is not None and item.get("type") == "response_item") else (
            item if item.get("type") == "message" else None)
        if not message or message.get("type") != "message" or message.get("role") != "assistant":
            continue
        parts = [block.get("text") for block in message.get("content") or []
                 if isinstance(block, dict) and block.get("type") in ("output_text", "text")
                 and isinstance(block.get("text"), str)]
        text = "\n\n".join(part for part in parts if part.strip())
        if not text.strip():
            continue
        phase = message.get("phase") if message.get("phase") in ("commentary", "final_answer") else None
        return {"text": text, "at": item.get("timestamp") if isinstance(item.get("timestamp"), str) else None,
                "complete": complete, "phase": phase}
    return None


def claude_reply(lines: list[bytes]) -> dict | None:
    """The last assistant message in a Claude Code project log.

    Each content block of one message is its own line with the same
    `message.id`; text blocks are joined, `tool_use` and `thinking` are not
    words to the person. Subagent lines (`isSidechain`) are skipped.
    """
    found_id, blocks, at, stop = None, [], None, None
    for line in reversed(lines):
        item = _json(line)
        if item is None or item.get("isSidechain") is True:
            continue
        if item.get("type") != "assistant" or not isinstance(item.get("message"), dict):
            if found_id is not None and item.get("type") == "user":
                break
            continue
        message = item["message"]
        identifier = message.get("id") if isinstance(message.get("id"), str) else None
        texts = [block.get("text") for block in message.get("content") or []
                 if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
                 and block.get("text").strip()]
        if found_id is None:
            if not texts:
                continue
            found_id = identifier or "-"
            at = item.get("timestamp") if isinstance(item.get("timestamp"), str) else None
            stop = message.get("stop_reason")
            blocks = list(reversed(texts))
            continue
        if identifier is not None and identifier == found_id:
            blocks.extend(reversed(texts))
            continue
        break
    if found_id is None:
        return None
    text = "\n\n".join(reversed(blocks))
    return {"text": text, "at": at, "complete": stop == "end_turn" if stop is not None else None, "phase": None}


READERS = {CODEX: codex_reply, CLAUDE: claude_reply}


def read_reply(agent: str, path: Path) -> dict | None:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    window = TAIL_BYTES
    while True:
        try:
            lines = _tail_lines(path, size, window)
        except OSError:
            return None
        reply = READERS[agent](lines)
        if reply is not None or window >= MAX_TAIL_BYTES or window >= size:
            return reply
        window = MAX_TAIL_BYTES


def _clip(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_TEXT:
        return text, False
    return text[:MAX_TEXT].rstrip() + "\n…", True


def etag(document: dict) -> str:
    raw = json.dumps([document.get("agent"), document.get("text"), document.get("at"),
                      document.get("complete"), document.get("available")], ensure_ascii=False)
    return '"' + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20] + '"'


class AgentReplies:
    """`GET /v1/herdr/panes/{pane}/reply` for one host.

    A match is remembered for a few seconds per (session, pane, foreground
    processes); a parse per (file, size, mtime). Polling a pane that has not
    moved costs one `pane.process_info` and a `stat`.
    """

    MATCH_SECONDS = 5.0
    # codex 0.160 opens its rollout only with the first prompt (probed on the
    # host), so "nothing yet" must not be remembered for long.
    MISS_SECONDS = 1.0

    def __init__(self, *, home: Path | None = None, table: ProcessTable | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        home = Path(home) if home is not None else Path.home()
        self.roots = {CODEX: home / ".codex" / "sessions", CLAUDE: home / ".claude" / "projects"}
        self.table = table or ProcessTable()
        self.clock = clock
        self._lock = threading.Lock()
        self._matches: dict[tuple, tuple[float, Match]] = {}
        self._parsed: dict[str, tuple[tuple, dict | None]] = {}

    def unavailable(self, reason: str, agent: str | None = None) -> dict:
        return {"available": False, "reason": reason, "agent": agent, "text": None, "at": None,
                "complete": None, "phase": None, "truncated": False}

    def reply(self, *, session: str, pane: str, agent: Any, process_info: dict | None,
              agent_session: Any = None) -> dict:
        family = agent_family(agent)
        if family is None:
            return self.unavailable("not_an_agent")
        match = self._session_match(family, agent_session)
        if match is None:
            pids = []
            for row in (process_info or {}).get("foreground_processes") or []:
                if isinstance(row, dict) and type(row.get("pid")) is int:
                    pids.append(row["pid"])
            if not pids:
                return self.unavailable("no_match", family)
            key = (session, pane, family, tuple(sorted(pids)))
            now = self.clock()
            with self._lock:
                cached = self._matches.get(key)
            if cached is not None and now - cached[0] < (
                    self.MATCH_SECONDS if cached[1].path is not None else self.MISS_SECONDS) \
                    and (cached[1].path is None or cached[1].path.exists()):
                match = cached[1]
            else:
                matcher = match_codex if family == CODEX else match_claude
                match = matcher(pids, self.table, self.roots[family])
                with self._lock:
                    if len(self._matches) > 256:
                        self._matches.clear()
                    self._matches[key] = (now, match)
        if match.path is None:
            return self.unavailable(match.reason or "no_match", family)
        reply = self._read(family, match.path)
        if reply is None:
            return self.unavailable("no_reply", family)
        text, truncated = _clip(reply["text"])
        return {"available": True, "reason": None, "agent": family, "text": text, "at": reply["at"],
                "complete": reply["complete"], "phase": reply["phase"], "truncated": truncated}

    def _session_match(self, family: str, value: Any) -> Match | None:
        """Herdr's own word for the agent's session, when it has one."""
        if not isinstance(value, dict) or agent_family(value.get("agent")) != family:
            return None
        root = self.roots[family]
        if value.get("kind") == "path" and isinstance(value.get("value"), str):
            path = _inside(os.path.expanduser(value["value"]), root)
            return Match(family, path, None if path else "no_match", "herdr")
        if value.get("kind") == "id" and isinstance(value.get("value"), str) and UUID.fullmatch(value["value"]):
            if family == CODEX:
                path = find_rollout(root, value["value"])
            else:
                path = next((p for p in (_inside(project / (value["value"] + ".jsonl"), root)
                                         for project in _dirs(root)) if p is not None), None)
            return Match(family, path, None if path else "no_match", "herdr")
        return None

    def _read(self, family: str, path: Path) -> dict | None:
        try:
            info = path.stat()
        except OSError:
            return None
        stamp = (info.st_size, info.st_mtime_ns, info.st_ino)
        with self._lock:
            cached = self._parsed.get(str(path))
        if cached is not None and cached[0] == stamp:
            return cached[1]
        reply = read_reply(family, path)
        with self._lock:
            if len(self._parsed) > 64:
                self._parsed.clear()
            self._parsed[str(path)] = (stamp, reply)
        return reply
