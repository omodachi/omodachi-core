"""OVERVIEW-1: one "Needs you" list across the Agent and every Herdr pane.

Everything that wants the person, in one projection on the state
(`state.overview`, `overview.changed`): the Agent conversation and every pane
with an agent in it, in every running Herdr session, sorted into three states -

* **needs_you** - an approval, a question, a pane whose agent Herdr reports
  `blocked`;
* **working** - a turn running, a pane Herdr reports `working`;
* **done** - finished with nobody looking since (NOTIFY-1's seen ledger).

Each row says what is asked in a few words (`ask`), and, where it could be
read reliably, the question itself (`question`, at most 120 characters):

* the Agent: the pending approval's kind (command, file changes, permissions,
  a question) - never what it asks, which stays on the chat's own stream;
* a blocked Herdr pane: the dialog on its screen, read the way HERDR-9's
  choice strip reads it (a numbered list with one row marked, or a `(y/n)`
  question) - `pick` / `yes_no` with the line above the options. A screen that
  does not read as one of those is `waiting`, and the client says "Waiting for
  you": a guess is never shown as the question;
* a finished pane: `your_move` with the question when the agent's own latest
  reply (HERDR-11's log match) ends in one, else `finished`.

Order is position, never status (Collie ADR 0063): the Agent first, then the
sessions by name and each session's panes in layout order. The client groups
by state; inside a group the order is this one. Counts are taken before the
row list is capped, so a badge never undercounts.

The projector is pure bookkeeping, thread-safe: the layout poll feeds it on a
worker, the agent's events on the loop.
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata

STATES = ("needs_you", "working", "done")
ROW_LIMIT = 64
QUESTION_LIMIT = 120
SCREEN_REREAD_SECONDS = 5.0
NAME_LIMIT = 48
AGENT_LIMIT = 32
# The default agent's own pane lives in core's owned session; it is the Agent
# row, never a second Herdr row (the alerts skip it the same way).
OWNED_SESSION = "omodachi"

AGENT_WORKING = ("thinking", "working", "tool")
APPROVAL_ASKS = {"commandExecution": "approve_command", "fileChange": "approve_files",
                 "permissions": "approve_permissions", "userInput": "answer"}

# --- reading a screen ----------------------------------------------------------

# Frame characters codex and Claude Code draw around a dialog, and the
# selection markers they put on the chosen option.
_FRAME = "│┃║|╭╮╰╯┌┐└┘─━═▌▐"
_MARKERS = "›❯>▶"
_OPTION = re.compile(r"^\s*([" + _MARKERS + r"])?\s*(\d{1,2})[.)]\s+\S")
_YES_NO = re.compile(r"[(\[]\s*[yY](?:es)?\s*/\s*[nN](?:o)?\s*[)\]]\s*[:?？]?\s*$")
_REASON = re.compile(r"^(reason|原因)\s*[:：]", re.IGNORECASE)
_OPTION_REACH = 14
_QUESTION_REACH = 12


def _clean_row(row: str) -> str:
    return row.strip().strip(_FRAME).strip()


def clean_text(value, limit: int = QUESTION_LIMIT) -> str | None:
    """One line of words: control characters out, whitespace squashed,
    markdown emphasis dropped, cut to `limit` characters."""
    if not isinstance(value, str):
        return None
    text = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in value)
    text = re.sub(r"[*`_]{1,3}", "", text)
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _is_question(row: str) -> bool:
    return row.endswith(("?", "？"))


def read_ask(screen) -> dict | None:
    """The dialog at the bottom of a pane's visible screen, or None.

    `{"ask": "pick" | "yes_no", "question": str | None}`. Only the two shapes
    HERDR-9's strip answers are read: a run of options numbered one apart
    with exactly one of them carrying the selection marker, near the bottom;
    or a `(y/n)` question on one of the last rows. A numbered list in an
    answer (no marker) is not a dialog.
    """
    if not isinstance(screen, str):
        return None
    raw = screen.replace("\r", "").split("\n")[-200:]
    rows = [_clean_row(row) for row in raw]
    filled = [index for index, row in enumerate(rows) if row]
    if not filled:
        return None
    end = filled[-1]
    # A yes-or-no question on one of the last three filled rows.
    for index in filled[-3:][::-1]:
        match = _YES_NO.search(rows[index])
        if match:
            return {"ask": "yes_no", "question": clean_text(rows[index][:match.start()].rstrip(" :"))
                    or None}
    # The lowest option row within reach of the bottom, then the run above it.
    lowest = next((index for index in range(end, max(-1, end - _OPTION_REACH), -1)
                   if _OPTION.match(rows[index])), None)
    if lowest is None:
        return None
    numbers, marked, first = [], 0, lowest
    index = lowest
    while index >= 0 and lowest - index <= 40:
        row = rows[index]
        match = _OPTION.match(row)
        if match:
            numbers.append(int(match.group(2)))
            marked += 1 if match.group(1) else 0
            first = index
        elif not row:
            # One blank between options at most (codex spaces some cards).
            if index - 1 < 0 or not _OPTION.match(rows[index - 1]):
                break
        elif not _indented(raw[index]):
            # A wrapped option label is indented under its option; anything
            # else ends the run.
            break
        index -= 1
    numbers.reverse()
    if len(numbers) < 2 or marked != 1 or numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        return None
    question = None
    for index in range(first - 1, max(-1, first - 1 - _QUESTION_REACH), -1):
        row = rows[index]
        # The nearest question above the options - not the tail of a wrapped
        # sentence (it starts in lower case), and not codex's "Reason:" line,
        # which quotes the model rather than the dialog.
        if row and _is_question(row) and not row[:1].islower() and not _REASON.match(row):
            question = clean_text(row)
            break
    return {"ask": "pick", "question": question}


def _indented(row: str) -> bool:
    """A continuation line: four spaces or more, inside the dialog's frame
    when it has one."""
    stripped = row.lstrip()
    inner = stripped[1:] if stripped[:1] and stripped[0] in _FRAME else row
    return inner.startswith("    ") and bool(inner.strip())


def reply_question(text) -> str | None:
    """The question an agent's latest reply ends on, or None."""
    if not isinstance(text, str):
        return None
    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
    if not lines:
        return None
    last = clean_text(lines[-1].lstrip("#>-* ").strip(), limit=400)
    if not last or not _is_question(last):
        return None
    # The last sentence of that line.
    sentences = [part for part in re.split(r"(?<=[.!?])\s+|(?<=[。！？])", last) if part and part.strip()]
    tail = sentences[-1].strip() if sentences else last
    return clean_text(tail if len(tail) >= 4 else last)


# --- the projection --------------------------------------------------------------

def _agent_name(pane: dict) -> str | None:
    agent = pane.get("agent") if isinstance(pane.get("agent"), dict) else {}
    name = agent.get("name") if agent.get("name") not in (None, "default") else agent.get("kind")
    if not isinstance(name, str):
        name = agent.get("kind") if isinstance(agent.get("kind"), str) else None
    return clean_text(name, AGENT_LIMIT) if name else None


def _status(pane: dict) -> str | None:
    agent = pane.get("agent") if isinstance(pane.get("agent"), dict) else {}
    status = agent.get("status") or pane.get("agent_status")
    return status if isinstance(status, str) else None


def _pane_name(pane: dict) -> str | None:
    title = pane.get("title")
    if isinstance(title, str) and title.strip():
        return clean_text(title, NAME_LIMIT)
    cwd = pane.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        tail = cwd.rstrip("/").rsplit("/", 1)[-1] or cwd
        return clean_text(tail, NAME_LIMIT)
    return None


def _workspace_name(workspace: dict) -> str | None:
    label = workspace.get("label")
    if isinstance(label, str) and label.strip():
        return clean_text(label, NAME_LIMIT)
    number = workspace.get("number")
    return str(number) if isinstance(number, int) else None


def session_panes(layout) -> list[dict]:
    """Every pane of a layout that has an agent, in layout order:
    `{pane_id, workspace_id, workspace, pane, agent, status, revision}`."""
    rows = []
    for workspace in (layout or {}).get("workspaces") or []:
        if not isinstance(workspace, dict):
            continue
        for tab in workspace.get("tabs") or []:
            for pane in (tab or {}).get("panes") or []:
                if not isinstance(pane, dict) or not isinstance(pane.get("id"), str):
                    continue
                status = _status(pane)
                if not isinstance(pane.get("agent"), dict) and status not in ("blocked", "working"):
                    continue
                rows.append({"pane_id": pane["id"][:128],
                             "workspace_id": workspace.get("id") if isinstance(workspace.get("id"), str) else None,
                             "workspace": _workspace_name(workspace), "pane": _pane_name(pane),
                             "agent": _agent_name(pane), "status": status, "revision": pane.get("revision")})
    return rows


class OverviewProjector:
    """What the list is made of, and the list."""

    def __init__(self, *, clock=time.monotonic):
        self._lock = threading.RLock()
        self._clock = clock
        self._sessions: dict[str, list[dict]] = {}
        # (session, pane) -> {"key": revision signature, "ask": {...} | None}
        self._asks: dict[tuple[str, str], dict] = {}
        # (session, pane) -> {"question": str | None, "first": t, "last": t}
        self._moves: dict[tuple[str, str], dict] = {}

    # --- inputs ------------------------------------------------------------------
    def set_session(self, session: str, layout) -> None:
        rows = session_panes(layout)
        with self._lock:
            self._sessions[session] = rows
            live = {row["pane_id"] for row in rows}
            for key in [key for key in self._asks if key[0] == session and key[1] not in live]:
                del self._asks[key]

    def forget_session(self, session: str) -> None:
        with self._lock:
            self._sessions.pop(session, None)
            for store in (self._asks, self._moves):
                for key in [key for key in store if key[0] == session]:
                    del store[key]

    def blocked_needing_a_read(self, *, default_pane: str | None = None) -> list[tuple[str, str]]:
        """Blocked panes whose screen has not been read at their current
        revision (and not in the last `SCREEN_REREAD_SECONDS`)."""
        with self._lock:
            out = []
            for session, rows in self._sessions.items():
                for row in rows:
                    if row["status"] != "blocked" or self._skip(session, row["pane_id"], default_pane):
                        continue
                    known = self._asks.get((session, row["pane_id"]))
                    # codex's TUI moves its pane's revision every second even
                    # while a dialog sits still (measured on the host), so a
                    # new revision is read again at most every few seconds.
                    if known is None or (known["key"] != row["revision"]
                                         and self._clock() - known["at"] >= SCREEN_REREAD_SECONDS):
                        out.append((session, row["pane_id"]))
            return out

    def note_screen(self, session: str, pane: str, screen) -> None:
        """What a blocked pane's screen said, at the revision it was read at."""
        with self._lock:
            revision = next((row["revision"] for row in self._sessions.get(session, [])
                             if row["pane_id"] == pane), None)
            self._asks[(session, pane)] = {"key": revision, "ask": read_ask(screen), "at": self._clock()}

    def unread_moves(self, unseen: list[dict], *, retry_for: float = 20.0, every: float = 2.0) -> list[tuple[str, str]]:
        """Finished panes whose latest reply has not been read yet - or was
        read before the agent wrote it (retried for `retry_for` seconds)."""
        now = self._clock()
        wanted = {(row.get("session"), row.get("pane_id")) for row in unseen or []}
        with self._lock:
            for key in [key for key in self._moves if key not in wanted]:
                del self._moves[key]
            out = []
            for key in sorted(k for k in wanted if all(isinstance(part, str) for part in k)):
                known = self._moves.get(key)
                if known is None:
                    out.append(key)
                elif known["question"] is None and now - known["first"] < retry_for and now - known["last"] >= every:
                    out.append(key)
            return out

    def note_reply(self, session: str, pane: str, text) -> None:
        now = self._clock()
        with self._lock:
            known = self._moves.get((session, pane))
            self._moves[(session, pane)] = {"question": reply_question(text),
                                            "first": known["first"] if known else now, "last": now}

    # --- the list ------------------------------------------------------------------
    @staticmethod
    def _skip(session: str, pane: str, default_pane: str | None) -> bool:
        return session == OWNED_SESSION and default_pane is not None and pane == default_pane

    @staticmethod
    def _agent(state: str, ask: str, name) -> dict:
        return {"id": "agent", "source": "agent", "state": state, "ask": ask, "question": None,
                "agent": clean_text(name, AGENT_LIMIT) if isinstance(name, str) else None,
                "session": None, "pane_id": None, "workspace_id": None, "workspace": None, "pane": None}

    @classmethod
    def agent_row(cls, agent, *, unseen: bool = False) -> dict | None:
        """The Agent conversation's row from `state.agent`, or None."""
        if not isinstance(agent, dict):
            return None
        chat = agent.get("chat") if isinstance(agent.get("chat"), dict) else {}
        status = agent.get("status")
        approval = chat.get("approval") if isinstance(chat.get("approval"), dict) else None
        waiting = (chat.get("status") or {}).get("waiting") if isinstance(chat.get("status"), dict) else None
        if status in ("waiting_on_approval", "waiting_on_user_input") or waiting in ("approval", "user_input") \
                or approval is not None:
            kind = approval.get("kind") if approval else None
            ask = APPROVAL_ASKS.get(kind) or ("answer" if status == "waiting_on_user_input"
                                              or waiting == "user_input" else "approve")
            return cls._agent("needs_you", ask, agent.get("kind"))
        activity = chat.get("activity") if isinstance(chat.get("activity"), dict) else {}
        if activity.get("phase") in AGENT_WORKING:
            return cls._agent("working", "working", agent.get("kind"))
        if unseen:
            return cls._agent("done", "failed" if activity.get("phase") == "error" else "finished", agent.get("kind"))
        return None

    def value(self, *, agent=None, unseen=None, default_pane: str | None = None,
              sessions_order: list[str] | None = None) -> dict:
        """`{rows, counts, sessions}` - see `contracts/state.schema.json#/properties/overview`."""
        unseen = unseen if isinstance(unseen, dict) else {}
        finished = {(row.get("session"), row.get("pane_id")) for row in unseen.get("herdr") or []
                    if isinstance(row, dict)}
        rows = []
        agent_row = self.agent_row(agent, unseen=unseen.get("agent") is True)
        if agent_row is not None:
            rows.append(agent_row)
        per_session = []
        with self._lock:
            names = sorted(self._sessions)
            if sessions_order:
                order = {name: index for index, name in enumerate(sessions_order)}
                names.sort(key=lambda name: (order.get(name, len(order)), name))
            for session in names:
                counts = dict.fromkeys(STATES, 0)
                for pane in self._sessions[session]:
                    if self._skip(session, pane["pane_id"], default_pane):
                        continue
                    key = (session, pane["pane_id"])
                    question = None
                    if pane["status"] == "blocked":
                        state = "needs_you"
                        read = (self._asks.get(key) or {}).get("ask")
                        ask = read["ask"] if read else "waiting"
                        question = read.get("question") if read else None
                    elif key in finished:
                        state = "done"
                        question = (self._moves.get(key) or {}).get("question")
                        ask = "your_move" if question else "finished"
                    elif pane["status"] == "working":
                        state, ask = "working", "working"
                    else:
                        continue
                    counts[state] += 1
                    rows.append({"id": f"herdr:{session}:{pane['pane_id']}", "source": "herdr", "state": state,
                                 "ask": ask, "question": question, "agent": pane["agent"], "session": session,
                                 "pane_id": pane["pane_id"], "workspace_id": pane["workspace_id"],
                                 "workspace": pane["workspace"], "pane": pane["pane"]})
                per_session.append({"name": session, **counts})
        totals = dict.fromkeys(STATES, 0)
        for row in rows:
            totals[row["state"]] += 1
        return {"rows": rows[:ROW_LIMIT], "counts": totals, "sessions": per_session[:64]}
