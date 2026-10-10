"""NOTIFY-1 part 3: done, but not yet seen.

A small record per Herdr pane and for the Agent conversation: the status it had
at the last look, when its agent last finished, and when a phone last had it
open. "Finished" is the transition, not the state: Herdr 0.9 reports a finished
agent as `idle` (0.8.2 sometimes says `done`), so `status == "done"` alone would
never fire on 0.9 and an idle shell would count as done on both. A pane is
**unseen** when its agent finished after anybody last had it open and it has
not started working again.

What counts as seen (Collie ADR 0003, adapted): a device opening the pane's
stream (`observe` or `control`), or sending it keys or text; for the Agent, a
device holding the chat's event stream, or sending it something. A pane being
watched at the moment it finishes is seen at once. Desk focus does not count -
nothing here reads the desktop - and a pane's first sighting is a baseline, never
news.

Persisted (`~/.config/omodachi/seen.json`, 0600) so a daemon restart keeps the
marks; rows of panes that are gone are dropped on the next look at their
session, and anything untouched for 30 days on load.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time

WORKING = ("working", "blocked")
FINISHED = ("idle", "done")
AGENT_WORKING = ("thinking", "working", "tool")
AGENT_FINISHED = ("done", "error")
STALE_SECONDS = 30 * 86400
LIMIT = 512
NAME_LIMIT = 32


def _pane_rows(layout) -> dict[str, tuple[str | None, str | None]]:
    """pane id -> (agent status, agent name) for every pane in a layout."""
    out = {}
    for workspace in (layout or {}).get("workspaces") or []:
        for tab in (workspace or {}).get("tabs") or []:
            for pane in (tab or {}).get("panes") or []:
                if not isinstance(pane, dict) or not isinstance(pane.get("id"), str):
                    continue
                agent = pane.get("agent") if isinstance(pane.get("agent"), dict) else {}
                status = agent.get("status") or pane.get("agent_status")
                name = agent.get("name") if agent.get("name") not in (None, "default") else agent.get("kind")
                out[pane["id"][:128]] = (status if isinstance(status, str) else None,
                                         name[:NAME_LIMIT] if isinstance(name, str) else None)
    return out


class SeenLedger:
    """Thread-safe: the layout poll runs on a worker, the streams on the loop."""

    def __init__(self, path: Path | None = None, *, clock=time.time):
        self.path = Path(path) if path is not None else None
        self._clock = clock
        self._lock = threading.RLock()
        self._rows: dict[str, dict] = {}
        self._watching: dict[str, int] = {}
        self._dirty = False
        self._load()

    # --- keys ----------------------------------------------------------------
    @staticmethod
    def herdr_key(session: str, pane: str) -> str:
        return f"herdr:{session}:{pane}"

    AGENT_KEY = "agent:default"

    # --- persistence -----------------------------------------------------------
    def _load(self):
        if self.path is None:
            return
        try:
            with open(self.path, "rb") as stream:
                raw = stream.read(262145)
            data = json.loads(raw) if len(raw) <= 262144 else {}
        except (OSError, ValueError):
            return
        rows = data.get("rows") if isinstance(data, dict) else None
        now = self._clock()
        for key, row in (rows or {}).items() if isinstance(rows, dict) else ():
            if not isinstance(key, str) or not isinstance(row, dict):
                continue
            touched = max(float(row.get("finished_at") or 0), float(row.get("seen_at") or 0),
                          float(row.get("at") or 0))
            if now - touched > STALE_SECONDS:
                continue
            self._rows[key] = {"status": row.get("status") if isinstance(row.get("status"), str) else None,
                               "name": row.get("name") if isinstance(row.get("name"), str) else None,
                               "finished_at": row.get("finished_at"), "seen_at": row.get("seen_at"),
                               "at": row.get("at")}

    def save(self):
        """Writes only when something changed since the last write."""
        if self.path is None:
            return
        with self._lock:
            if not self._dirty:
                return
            data = {"version": 1, "rows": dict(list(self._rows.items())[-LIMIT:])}
            self._dirty = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix=".seen-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    json.dump(data, stream, separators=(",", ":"))
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except OSError:
            with self._lock:
                self._dirty = True

    # --- what happened -------------------------------------------------------
    def _observe(self, key, status, name, *, working, finished) -> bool:
        """One reading of one key; True when it just finished unwatched."""
        now = self._clock()
        row = self._rows.get(key)
        if row is None:
            # The first sighting is a baseline: whatever state it is in, it
            # has been "seen" as far as this ledger is concerned.
            self._rows[key] = {"status": status, "name": name, "finished_at": None, "seen_at": None, "at": now}
            self._dirty = True
            return False
        previous = row.get("status")
        if status != previous or (name and name != row.get("name")):
            row["status"], row["at"] = status, now
            if name:
                row["name"] = name
            self._dirty = True
        if previous in working and status in finished:
            row["finished_at"] = now
            if self._watching.get(key):
                row["seen_at"] = now
                return False
            return True
        return False

    def observe_herdr(self, session: str, layout) -> list[dict]:
        """A fresh layout of one session: `[{session, pane_id, name}]` of the
        panes whose agent finished just now with nobody watching."""
        rows = _pane_rows(layout)
        finished = []
        with self._lock:
            prefix = f"herdr:{session}:"
            for key in [key for key in self._rows if key.startswith(prefix)]:
                if key[len(prefix):] not in rows:
                    del self._rows[key]
                    self._dirty = True
            for pane, (status, name) in rows.items():
                if self._observe(self.herdr_key(session, pane), status, name, working=WORKING, finished=FINISHED):
                    finished.append({"session": session, "pane_id": pane, "name": name})
        return finished

    def forget_session(self, session: str):
        with self._lock:
            prefix = f"herdr:{session}:"
            for key in [key for key in self._rows if key.startswith(prefix)]:
                del self._rows[key]
                self._dirty = True

    def observe_agent(self, phase) -> bool:
        if not isinstance(phase, str):
            return False
        with self._lock:
            return self._observe(self.AGENT_KEY, phase, None, working=AGENT_WORKING, finished=AGENT_FINISHED)

    def seen(self, key: str):
        with self._lock:
            row = self._rows.get(key)
            if row is None:
                return
            row["seen_at"] = self._clock()
            self._dirty = True

    def watch(self, key: str, delta: int):
        """A device opened (+1) or closed (-1) something that shows `key`."""
        with self._lock:
            count = max(0, self._watching.get(key, 0) + delta)
            if count:
                self._watching[key] = count
            else:
                self._watching.pop(key, None)
            if delta > 0:
                row = self._rows.get(key)
                if row is not None:
                    row["seen_at"] = self._clock()
                    self._dirty = True

    # --- what is unseen --------------------------------------------------------
    def _unseen(self, key, row) -> bool:
        finished = row.get("finished_at")
        if not finished or row.get("status") not in (AGENT_FINISHED if key == self.AGENT_KEY else FINISHED):
            return False
        return not row.get("seen_at") or row["seen_at"] < finished

    def value(self) -> dict:
        """`{agent: bool, herdr: [{session, pane_id, name}]}`, ids and names only."""
        with self._lock:
            herdr = []
            for key, row in self._rows.items():
                if key.startswith("herdr:") and self._unseen(key, row):
                    _, session, pane = key.split(":", 2)
                    herdr.append({"session": session, "pane_id": pane, "name": row.get("name")})
            agent = self.AGENT_KEY in self._rows and self._unseen(self.AGENT_KEY, self._rows[self.AGENT_KEY])
        herdr.sort(key=lambda row: (row["session"], row["pane_id"]))
        return {"agent": agent, "herdr": herdr[:64]}
