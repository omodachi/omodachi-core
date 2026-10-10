"""HERDR-4: the operator's own rows for the Herdr pane view.

Three files, in Collie's own format (Collie `docs/configure.md`,
`*.toml.example`, ADR 0018), so a person who already wrote them for Collie
gets the same buttons here on day one:

- `quick-replies.toml` - `[[replies]] scope? title items[]`: one-tap replies,
  each typed into the pane and submitted verbatim.
- `keys.toml` - `[[keys]] scope? label keys[] danger?`: labelled key chords,
  sent as one ordered `pane.send_keys` batch. `danger` asks for a second tap.
- `commands.toml` - `[[commands]] scope? command description? confirm? bar?
  bar_label?`: slash commands. `bar = true` puts one on the harness segment.

Where: `~/.config/omodachi/<file>`, and when that one is absent,
`~/.config/collie/<file>`. The choice is per file.

The rules are Collie's, unchanged:

- **Replace, never merge.** On a pane the operator's rows address, those rows
  are the whole list for that surface; a pane none of them address keeps
  what ships; a file with nothing in it changes nothing.
- **The narrowest scope wins**, one row per name: exact agent name, then
  the agent's family (`claude` reaches a `claude-code` pane), then unscoped.
- **A bad row is dropped, never the file**, with a warning. A file that does
  not parse at all keeps the last good rows (none, if it never parsed), and
  the reply carries a warning saying so. Edits are read on the next request
  behind an mtime check: no restart.

Nothing here is a secret and nothing here runs: rows are labels and the
words or keys a tap sends, through the same `/text` and `/keys` routes a
person's own typing takes. Key chords are checked with the very grammar
`/keys` enforces (`herdr_bridge.key_name`), so a row that could only ever be
refused never becomes a button.
"""
from __future__ import annotations

import os
from pathlib import Path
import threading
import tomllib
from typing import Any, Callable

from .herdr_bridge import HerdrUnavailable, key_name

FILES = ("quick-replies.toml", "keys.toml", "commands.toml")
FILE_KEYS = {"quick-replies.toml": "replies", "keys.toml": "keys", "commands.toml": "commands",
             "launchers.toml": "launchers"}
# HERDR-11: what the New workspace card may start, when the operator wrote no
# `launchers.toml`: these two, each only if this host has it.
DEFAULT_LAUNCHERS = ("codex", "claude")
MAX_LAUNCHERS = 8

# The agent families a scope may name (Collie `operator-scope.ts`), and the one
# that is not an agent at all.
FAMILIES = ("claude", "codex", "pi", "opencode", "omp", "grok", "agy", "antigravity")
SHELL = "shell"

MAX_FILE_BYTES = 64 * 1024
MAX_ROWS = 64
MAX_ITEMS = 12
MAX_TEXT = 200
MAX_LABEL = 40
BAR_LABEL_MAX, BAR_LABEL_KEEP = 12, 11
MAX_DETAIL = 200

# ── what ships ────────────────────────────────────────────────────────────
# Collie's dock (`web/src/lib/quick-replies.ts`): one set for every agent, and
# a shell gets only y/n - "commit and push" typed at a bare prompt is nonsense.
SHIPPED_QUICK_AGENT = ({"id": "confirm", "items": ["yes", "no"]},
                       {"id": "common", "items": ["continue", "commit and push", "retry", "skip"]})
SHIPPED_QUICK_SHELL = ({"id": "confirm", "items": ["y", "n"]},)
# Collie's presets (`web/src/lib/operator-keys.ts`). ^D and ^Z can end or stop
# what is running, so they ask for a second tap.
SHIPPED_KEYS = (
    {"label": "Ctrl C", "keys": ["ctrl+c"], "danger": False},  # non-copy: key cap names
    {"label": "Ctrl D", "keys": ["ctrl+d"], "danger": True},
    {"label": "Ctrl U", "keys": ["ctrl+u"], "danger": False},
    {"label": "Ctrl R", "keys": ["ctrl+r"], "danger": False},
    {"label": "Ctrl L", "keys": ["ctrl+l"], "danger": False},
    {"label": "Ctrl Z", "keys": ["ctrl+z"], "danger": True},
)
# Collie's harness bar (`web/src/lib/harness-bar.ts`): each button sends the bare
# command, and the agent's own picker takes over in the pane. No list of model
# names is kept here; that list is the agent's and it changes.
SHIPPED_HARNESS = {
    "claude": ("model", "effort", "compact", "resume"),
    "codex": ("model", "compact", "resume"),
}
# Shipped commands that end or wipe something (Collie `agent-commands.ts`
# `dangerous`). An operator's row naming one inherits the second tap; its own
# `confirm = false` cannot lift it (ADR 0018).
DANGEROUS = {
    "claude": {"/clear", "/logout", "/rewind", "/exit"},
    "codex": {"/clear", "/new", "/logout", "/archive", "/delete", "/quit", "/exit"},
}


def canonical_agent(name: str) -> str:
    """A pane's agent name folded onto its family ("claude-code" -> "claude")."""
    if not name or name in FAMILIES:
        return name
    for family in ("claude", "codex", "opencode", "omp", "grok", "agy", "antigravity"):
        if name.startswith(family):
            return family
    if name == "pi" or name.startswith(("pi-", "pi.")):
        return "pi"
    return name


def rows_for(rows: list[dict], agent: str, key_of: Callable[[dict], str]) -> list[dict]:
    """The rows that address this pane, narrowest scope first-wins, one per name.

    Empty means "nothing of yours points here": the caller keeps what ships.
    `agent` is the pane's agent name, or `shell` for a pane without one.
    """
    pane_key = (agent or "").strip().lower()
    family = canonical_agent(pane_key)
    aimed: dict[str, tuple[dict, int]] = {}
    for row in rows:
        scope = row.get("scope")
        if scope is None:
            aim = 1
        elif not pane_key:
            aim = 0
        elif scope == pane_key:
            aim = 3
        elif scope in FAMILIES and scope == family:
            aim = 2
        else:
            aim = 0
        if not aim:
            continue
        name = key_of(row)
        previous = aimed.get(name)
        if previous is not None and previous[1] > aim:
            continue
        aimed[name] = (row, aim)
    return [row for row, _ in aimed.values()]


# ── the three grammars (pure: a document in, rows and warnings out) ─────────
def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value or len(value) > limit or any(ord(c) < 0x20 or ord(c) == 0x7f for c in value):
        return None
    return value


def _scope(row: dict, name: str, warn) -> tuple[bool, str | None]:
    if "scope" not in row:
        return True, None
    scope = row["scope"]
    # Fail closed: dropping an unusable scope would WIDEN the row to every pane.
    if not isinstance(scope, str) or not scope.strip() or len(scope) > 40:
        warn(f'ignoring "{name}" - scope must be an agent name')
        return False, None
    return True, scope.strip().lower()


def _flag(row: dict, key: str, name: str, warn) -> tuple[bool, bool]:
    if key not in row:
        return True, False
    if type(row[key]) is not bool:
        warn(f'ignoring "{name}" - {key} must be true or false')
        return False, False
    return True, row[key]


def _keep(out: list[dict], at: dict, identity: str, row: dict, name: str, warn) -> None:
    """A later row with the same scope and name wins in place."""
    if identity in at:
        warn(f'"{name}" redefined - the later row wins')
        out[at[identity]] = row
        return
    if len(out) >= MAX_ROWS:
        warn(f'ignoring "{name}" - more than {MAX_ROWS} rows')
        return
    at[identity] = len(out)
    out.append(row)


def _tables(document: dict, key: str, warn) -> list:
    rows = document.get(key)
    if rows is None:
        return []
    if not isinstance(rows, list):
        warn(f"`{key}` must be an array of [[{key}]] tables - ignoring the file's rows")
        return []
    return rows


def validate_quick_replies(document: dict, warn) -> list[dict]:
    out, at = [], {}
    for raw in _tables(document, "replies", warn):
        if not isinstance(raw, dict):
            warn("ignoring a row that is not a [[replies]] table")
            continue
        title = _text(raw.get("title"), MAX_LABEL)
        if title is None:
            warn("ignoring a row with no usable title")
            continue
        items = raw.get("items")
        if not isinstance(items, list) or not items or len(items) > MAX_ITEMS:
            warn(f'ignoring "{title}" - items must be 1 to {MAX_ITEMS} strings')
            continue
        clean = [_text(item, MAX_TEXT) for item in items]
        if any(item is None for item in clean):
            # A blank button would type nothing and submit; a typo'd group
            # silently served shorter would hide the typo. The row goes.
            warn(f'ignoring "{title}" - an item is blank, too long or not a string')
            continue
        ok, scope = _scope(raw, title, warn)
        if not ok:
            continue
        row = {"title": title, "items": clean, "scope": scope}
        _keep(out, at, f"{scope or ''} {title}", row, title, warn)
    return out


def validate_keys(document: dict, warn) -> list[dict]:
    out, at = [], {}
    for raw in _tables(document, "keys", warn):
        if not isinstance(raw, dict):
            warn("ignoring a row that is not a [[keys]] table")
            continue
        label = _text(raw.get("label"), MAX_LABEL)
        if label is None:
            warn("ignoring a row with no usable label")
            continue
        chords = raw.get("keys")
        if not isinstance(chords, list) or not chords or len(chords) > 32:
            warn(f'ignoring "{label}" - keys must be 1 to 32 chords')
            continue
        try:
            # The grammar `/keys` itself enforces: a sequence with one step
            # missing is a different sequence, so one bad chord drops the row.
            names = [key_name(chord) for chord in chords]
        except HerdrUnavailable:
            warn(f'ignoring "{label}" - a chord is not a key herdr accepts')
            continue
        ok, scope = _scope(raw, label, warn)
        ok_danger, danger = _flag(raw, "danger", label, warn)
        if not ok or not ok_danger:
            continue
        row = {"label": label, "keys": names, "danger": danger, "scope": scope}
        _keep(out, at, f"{scope or ''} {label}", row, label, warn)
    return out


def validate_commands(document: dict, warn) -> list[dict]:
    out, at = [], {}
    for raw in _tables(document, "commands", warn):
        if not isinstance(raw, dict):
            warn("ignoring a row that is not a [[commands]] table")
            continue
        spec = _text(raw.get("command"), MAX_TEXT)
        command, _, hint = (spec or "").partition(" ")
        if len(command) < 2 or not command.startswith("/") or len(command) > 64:
            warn("ignoring a row whose command is not a slash command")
            continue
        ok, scope = _scope(raw, command, warn)
        ok_confirm, confirm = _flag(raw, "confirm", command, warn)
        ok_bar, bar = _flag(raw, "bar", command, warn)
        if not (ok and ok_confirm and ok_bar):
            continue
        description = _text(raw.get("description"), MAX_TEXT)
        label = None
        if bar and "bar_label" in raw:
            label = _text(raw.get("bar_label"), 200)
            if label is None:
                warn(f'"{command}" - bar_label must be a non-empty string, using the command name')
            elif len(label) > BAR_LABEL_MAX:
                warn(f'"{command}" - bar_label is longer than {BAR_LABEL_MAX} characters, shortening it')
                label = label[:BAR_LABEL_KEEP] + "…"
        row = {"command": command, "description": description, "arg_hint": hint.strip() or None,
               "confirm": confirm, "bar": bar, "bar_label": label, "scope": scope}
        _keep(out, at, f"{scope or ''} {command}", row, command, warn)
    return out


def validate_launchers(document: dict, warn) -> list[dict]:
    """HERDR-11: `launchers.toml` - `[[launchers]] command label?` (Collie's
    own format; its `cwd` is not read: the folder is the card's). The file
    *is* the allowlist: a workspace is started with a command only when the
    string equals one of these rows exactly."""
    out, at = [], {}
    for raw in _tables(document, "launchers", warn):
        if not isinstance(raw, dict):
            warn("ignoring a row that is not a [[launchers]] table")
            continue
        command = raw.get("command")
        clean = _text(command, MAX_TEXT)
        if clean is None or clean != command or clean.startswith("-"):
            # Whitespace folded or a control character dropped would type
            # something other than what the row says; the row goes.
            warn("ignoring a row whose command is blank, too long or not plain text")
            continue
        label = clean.split(" ", 1)[0]
        if "label" in raw:
            named = _text(raw.get("label"), MAX_LABEL)
            if named is None:
                warn(f'"{clean}" - label must be a short string, using the command')
            else:
                label = named
        if len(out) >= MAX_LAUNCHERS and clean not in at:
            warn(f'ignoring "{clean}" - more than {MAX_LAUNCHERS} launchers')
            continue
        _keep(out, at, clean, {"command": clean, "label": label}, clean, warn)
    return out


VALIDATORS = {"quick-replies.toml": validate_quick_replies, "keys.toml": validate_keys,
              "commands.toml": validate_commands, "launchers.toml": validate_launchers}


class OperatorFile:
    """One operator file, re-read behind an mtime check.

    Failure is always a hold: a file that stops parsing keeps the last good
    rows (none if it never parsed) and says so in `warning`, once per change
    of the file rather than once per request.
    """

    def __init__(self, name: str, directories: tuple[Path, ...]):
        self.name, self.directories = name, directories
        self._seen: tuple | None = None
        self.rows: list[dict] = []
        self.origin = "shipped"
        self.warnings: list[str] = []
        self.failed: str | None = None

    def _locate(self) -> tuple[Path, str] | None:
        for directory, origin in zip(self.directories, ("omodachi", "collie")):
            path = directory / self.name
            try:
                if path.is_file():
                    return path, origin
            except OSError:
                continue
        return None

    def read(self) -> "OperatorFile":
        found = self._locate()
        if found is None:
            self._seen, self.rows, self.origin, self.warnings, self.failed = None, [], "shipped", [], None
            return self
        path, origin = found
        try:
            stat = path.stat()
            stamp = (str(path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp, stat = (str(path), None, None), None
        if stamp == self._seen:
            return self
        self._seen = stamp
        warnings: list[str] = []
        try:
            if stat is None:
                raise OSError("the file could not be read")
            if stat.st_size > MAX_FILE_BYTES:
                raise ValueError(f"the file is larger than {MAX_FILE_BYTES // 1024} KiB")
            document = tomllib.loads(path.read_text(encoding="utf-8"))
            rows = VALIDATORS[self.name](document, warnings.append)
        except (OSError, UnicodeDecodeError, ValueError, tomllib.TOMLDecodeError) as error:
            # Keep the last good rows; say why, and which file.
            self.failed = _detail(str(error))
            return self
        self.rows, self.origin, self.warnings, self.failed = rows, origin, warnings[:8], None
        return self

    def view(self) -> dict:
        warning = None
        if self.failed is not None:
            warning = {"code": "operator_file_unparsed", "detail": self.failed}
        elif self.warnings:
            warning = {"code": "operator_rows_dropped", "detail": _detail("; ".join(self.warnings))}
        return {"file": self.name, "origin": self.origin, "warning": warning}


def _bar_label(text: str) -> str:
    return text if len(text) <= BAR_LABEL_MAX else text[:BAR_LABEL_KEEP] + "\u2026"


def _detail(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_DETAIL else text[:MAX_DETAIL - 1] + "…"


class OperatorRows:
    """`GET /v1/herdr/operator-rows`: what one pane's Quick dock, key presets
    and harness segment show, after the operator's files are applied."""

    def __init__(self, config_dir: Path, collie_dir: Path | None = None):
        if collie_dir is None:
            collie_dir = Path(os.path.expanduser("~")) / ".config" / "collie"
        self._lock = threading.Lock()
        self.files = {name: OperatorFile(name, (Path(config_dir), Path(collie_dir))) for name in FILES}

    def resolve(self, agent: str | None) -> dict:
        """`agent` is the pane's agent name as Herdr reports it; None is a shell."""
        agent = (agent or "").strip().lower()
        pane_key = agent or SHELL
        family = canonical_agent(agent) if agent else SHELL
        with self._lock:
            quick_file = self.files["quick-replies.toml"].read()
            keys_file = self.files["keys.toml"].read()
            commands_file = self.files["commands.toml"].read()
            quick_rows, key_rows, command_rows = quick_file.rows, keys_file.rows, commands_file.rows
            sources = [quick_file.view(), keys_file.view(), commands_file.view()]

        aimed = rows_for(quick_rows, pane_key, lambda row: row["title"])
        if aimed:
            quick = [{"id": None, "title": row["title"], "items": row["items"], "operator": True} for row in aimed]
        else:
            shipped = SHIPPED_QUICK_SHELL if not agent else SHIPPED_QUICK_AGENT
            quick = [{"id": row["id"], "title": None, "items": list(row["items"]), "operator": False}
                     for row in shipped]

        aimed = rows_for(key_rows, pane_key, lambda row: row["label"])
        if aimed:
            keys = [{"label": row["label"], "keys": row["keys"], "danger": row["danger"], "operator": True}
                    for row in aimed]
        else:
            keys = [{**row, "keys": list(row["keys"]), "operator": False} for row in SHIPPED_KEYS]

        # Collie ADR 0043: the replacement rule applies per surface. The scope
        # ladder runs over every row first, then the bar filter on its answer,
        # so a narrower `bar = false` row takes a command off the bar.
        aimed_commands = rows_for(command_rows, pane_key, lambda row: row["command"])
        dangerous = DANGEROUS.get(family, set())
        bar_rows = [row for row in aimed_commands if row["bar"]]
        if bar_rows:
            harness = [{"id": None, "label": _bar_label(row["bar_label"] or row["command"][1:]), "command": row["command"],
                        "confirm": row["confirm"] or row["command"] in dangerous, "operator": True}
                       for row in bar_rows]
        else:
            harness = [{"id": name, "label": None, "command": "/" + name, "confirm": False, "operator": False}
                       for name in SHIPPED_HARNESS.get(family, ())]
        commands = [{"command": row["command"], "description": row["description"], "arg_hint": row["arg_hint"],
                     "confirm": row["confirm"] or row["command"] in dangerous}
                    for row in aimed_commands]
        return {"agent": agent or None, "family": family, "quick": quick, "keys": keys,
                "harness": harness, "commands": commands, "sources": sources}


class Launchers:
    """HERDR-11 §1: the commands a new workspace may start with.

    `~/.config/omodachi/launchers.toml` (else Collie's), same rules as the
    rows above: re-read behind an mtime check, a bad row dropped, a file that
    stops parsing keeps its last good rows. Without a file: `codex` and
    `claude`, each only when it is installed where a login shell would find
    it (the per-user bin folders the Agent's own lookup uses).
    """

    def __init__(self, config_dir: Path, collie_dir: Path | None = None, *, which=None, home=None,
                 clock=None) -> None:
        import shutil
        import time
        if collie_dir is None:
            collie_dir = Path(os.path.expanduser("~")) / ".config" / "collie"
        self.file = OperatorFile("launchers.toml", (Path(config_dir), Path(collie_dir)))
        self.which = which or shutil.which
        self.home = Path(home) if home is not None else Path(os.path.expanduser("~"))
        self.clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._installed: tuple[float, list[dict]] | None = None

    def _defaults(self) -> list[dict]:
        from .agent_chat_owner import codex_search_path
        now = self.clock()
        if self._installed is not None and now - self._installed[0] < 60:
            return self._installed[1]
        search = codex_search_path(os.environ.get("PATH"), "", self.home)
        rows = [{"command": name, "label": name} for name in DEFAULT_LAUNCHERS
                if self.which(name, path=search)]
        self._installed = (now, rows)
        return rows

    def rows(self) -> list[dict]:
        with self._lock:
            found = self.file.read()
            if found.origin != "shipped":
                return [dict(row) for row in found.rows]
            return [dict(row) for row in self._defaults()]

    def allowed(self, command) -> dict | None:
        if not isinstance(command, str):
            return None
        return next((row for row in self.rows() if row["command"] == command), None)
