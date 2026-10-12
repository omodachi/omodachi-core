"""LITE-1b C6: Web Push from the host to the browsers paired with it.

This is the host's only way to reach a locked phone: no relay of Omodachi's,
no APNs key. A browser that paired as Odomo (a `web` credential)
subscribes with the browser's own push service - `web.push.apple.com` for a
Home Screen web app on iPhone, FCM for Chrome, Mozilla's autopush for Firefox
- against this host's VAPID key, and the daemon sends to that endpoint when
something on the computer needs the person.

Three parts:

`PushStore` - `~/.config/omodachi/push-subscriptions.json` (0600, with a
    `.lock`), one row per web device: its one subscription, its preferences
    and whether the person at the computer has switched push off for it.
    A revoke or a purge deletes the row (`service`), and `--purge` the file.
`kind_signals` - turns hub events into the five things worth a push:
    `agent_approval`, `agent_needs_input`, `agent_done`, `herdr_blocked`,
    `host_notification`, and the retractions that undo the first four.
`PushSender` - a hub observer. It filters by each device's preferences,
    lets the first push of a source through at once and folds whatever the
    same source says in the next 30 s into one, retracts by tag where the
    platform allows it (not Apple: a push there must always show something),
    keeps each payload under 2.7 KB, and sends on a worker thread with
    bounded retries. A 404 or 410 drops the subscription. NOTIFY-2: "quiet
    while the App is open" holds a push back only while a native client is
    in front *and* alerts that kind itself (`CoreService.native_alerts`), and
    the check is made again when a coalesced push leaves its window. It never blocks
    the event loop, and `notification.posted` - published from the
    notification mirror's own thread - reaches it through
    `call_soon_threadsafe`.

The payload is a declarative Web Push message (`{"web_push": 8030, ...}`,
WebKit, iOS 18.4+): Safari shows it without running any script, so a
Home Screen app is never penalised for a push its worker failed to show.
Every other browser hands the same JSON to the service worker, which shows
it. `navigate` is a URL on this host's own Odomo with ids only - which panel,
which pane, which notification, and (NOTIFY-2) this host's id, so Odomo can
hand the same place to the App as a link - never a credential and never more
of a question than the short title and body already say.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time
import unicodedata
import urllib.parse

KINDS = ("agent_approval", "agent_needs_input", "agent_done", "herdr_blocked", "host_notification", "herdr_done")
# NOTIFY-1: "an agent in Herdr finished and nobody has looked" is off unless
# asked for (Collie: "most people don't want a buzz for every completed task").
OFF_BY_DEFAULT = ("herdr_done",)
LANGS = ("en", "zh-Hans")
# Firefox for Android reaches FCM through a bridge that leaves ~2744 bytes
# after base64 (LITE-0 §4.2); everything Omodachi sends stays under this.
PAYLOAD_LIMIT = 2700
DEBOUNCE_SECONDS = 30.0
# NOTIFY-1: a Herdr pane that is answered at the computer within this long was
# answered by someone sitting there, and the phone is never told (Collie's
# trailing delay). Agent kinds still go at once.
HERDR_DELAY_SECONDS = 30.0
TITLE_LIMIT = 80
BODY_LIMIT = 160
# The services a browser's push endpoint may be on. Not a filter on which
# browsers work - it is what stops a subscription from turning this daemon
# into something that POSTs to an address of the caller's choosing (SSRF).
PUSH_SERVICES = ("push.apple.com", "fcm.googleapis.com", "android.googleapis.com",
                 "updates.push.services.mozilla.com", "push.services.mozilla.com",
                 "notify.windows.com")
ENDPOINT_LIMIT = 2048
MAX_DEVICES = 64
STORE_LIMIT = 262144
RETRIES = 2
# A browser on this same computer shows a push as a desktop notification, which
# the notification mirror would see and push again. Anything titled like a
# push sent in the last ten minutes is that echo, and is not pushed back.
ECHO_SECONDS = 600
# Apple: a push must always end in something on screen, so nothing is ever
# withdrawn silently there. Everywhere else a retraction closes by tag.
NO_RETRACT = ("push.apple.com",)

# NOTIFY-1: `quiet_at_desk` - hold pushes while the person is at the computer
# (`desk.py`), and send what is still waiting once they leave it or lock it.
DEFAULT_PREFS = {"kinds": {kind: kind not in OFF_BY_DEFAULT for kind in KINDS}, "snooze_until": None,
                 "quiet_when_native_connected": True, "quiet_at_desk": True, "lang": "en"}
PREF_KEYS = ("kinds", "snooze_until", "quiet_when_native_connected", "quiet_at_desk", "lang")


class PushError(ValueError):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(code)


def _clean(value, limit):
    if not isinstance(value, str):
        return ""
    text = "".join(" " if c.isspace() else c for c in value
                   if c.isspace() or unicodedata.category(c) not in {"Cc", "Cf", "Cs", "Co", "Cn"})
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def endpoint_host(endpoint: str) -> str:
    return (urllib.parse.urlsplit(endpoint).hostname or "").lower()


def _on_service(host: str, services) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in services)


def valid_endpoint(value) -> str:
    """An https push-service URL, or `PushError("invalid_push_endpoint")`."""
    if not isinstance(value, str) or not 12 <= len(value) <= ENDPOINT_LIMIT or any(
            ord(c) <= 32 or ord(c) == 127 for c in value):
        raise PushError("invalid_push_endpoint")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise PushError("invalid_push_endpoint") from None
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443)
            or not host or not re.fullmatch(r"[a-z0-9.-]+", host)
            or not _on_service(host, PUSH_SERVICES) or not parsed.path or parsed.fragment):
        raise PushError("invalid_push_endpoint")
    return value


def _key(value, length):
    from .webpush import unb64u
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}={0,2}", value):
        raise PushError("invalid_push_keys")
    try:
        raw = unb64u(value)
    except ValueError:
        raise PushError("invalid_push_keys") from None
    if len(raw) != length or (length == 65 and raw[0] != 4):
        raise PushError("invalid_push_keys")
    return value.rstrip("=")


def clean_subscription(payload: dict, now: int) -> dict:
    """The browser's `PushSubscription.toJSON()`, checked, plus our id."""
    if not isinstance(payload, dict) or set(payload) - {"endpoint", "keys", "expiration_time",
                                                        "expirationTime", "user_agent_label"}:
        raise PushError("invalid_request")
    keys = payload.get("keys")
    if not isinstance(keys, dict) or set(keys) != {"p256dh", "auth"}:
        raise PushError("invalid_push_keys")
    expiration = payload.get("expiration_time", payload.get("expirationTime"))
    if expiration is not None and (type(expiration) not in (int, float) or expiration < 0):
        raise PushError("invalid_request")
    label = payload.get("user_agent_label")
    if label is not None and not isinstance(label, str):
        raise PushError("invalid_request")
    return {"subscription_id": "sub_" + secrets.token_hex(12),
            "endpoint": valid_endpoint(payload.get("endpoint")),
            "keys": {"p256dh": _key(keys["p256dh"], 65), "auth": _key(keys["auth"], 16)},
            "expiration_time": None if expiration is None else int(expiration),
            "user_agent_label": _clean(label, 64) or None,
            "created_at": now}


def _private_open(path: Path, flags: int) -> int:
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError(f"{path} is not a regular file of this user")
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


class PushStore:
    """One JSON file of web devices' subscriptions and preferences."""

    def __init__(self, path: Path, *, clock=time.time):
        self.path, self._clock = Path(path), clock
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self, *, write=True):
        with self._lock:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock = _private_open(self.path.with_name(self.path.name + ".lock"), os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    fd = _private_open(self.path, os.O_RDONLY)
                    with os.fdopen(fd, "rb") as stream:
                        raw = stream.read(STORE_LIMIT + 1)
                    if len(raw) > STORE_LIMIT:
                        raise ValueError("push state too large")
                    state = json.loads(raw)
                    if not isinstance(state, dict) or not isinstance(state.get("devices"), dict):
                        raise ValueError("push state unreadable")
                except FileNotFoundError:
                    state = {"version": 1, "devices": {}}
                yield state
                if not write:
                    return
                fd, temporary = tempfile.mkstemp(prefix=".push-subscriptions-", dir=self.path.parent)
                try:
                    with os.fdopen(fd, "w") as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        json.dump(state, stream, separators=(",", ":"))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                os.close(lock)

    @staticmethod
    def _row(state, device, *, create=False):
        row = state["devices"].get(device)
        if row is None and create:
            if len(state["devices"]) >= MAX_DEVICES:
                raise PushError("push_capacity", 429)
            row = state["devices"][device] = {"subscription": None, "prefs": json.loads(json.dumps(DEFAULT_PREFS)),
                                               "revision": 0, "host_enabled": True}
        return row

    @staticmethod
    def _prefs_view(device, row):
        prefs = json.loads(json.dumps(DEFAULT_PREFS))
        if row:
            stored = row.get("prefs") or {}
            prefs["kinds"].update({k: v for k, v in (stored.get("kinds") or {}).items()
                                   if k in KINDS and type(v) is bool})
            for key in PREF_KEYS[1:]:
                if key in stored:
                    prefs[key] = stored[key]
        return {"device_id": device, "revision": int((row or {}).get("revision", 0)), **prefs,
                "subscribed": bool(row and row.get("subscription")),
                "host_enabled": bool((row or {}).get("host_enabled", True))}

    # --- what a web device does -------------------------------------------
    def subscribe(self, device: str, payload: dict, *, check=None) -> dict:
        """`check` (SEC-RENEW-1) is the request's credential, asked again inside
        the transaction `forget` also takes: a revoke finishing between the
        route's own check and this write would otherwise leave a revoked
        browser subscribed. It raises to refuse."""
        record = clean_subscription(payload, int(self._clock()))
        with self.transaction() as state:
            if check is not None:
                check()
            row = self._row(state, device, create=True)
            row["subscription"] = record
        return record

    def unsubscribe(self, device: str) -> bool:
        with self.transaction() as state:
            row = self._row(state, device)
            if not row or not row.get("subscription"):
                return False
            row["subscription"] = None
            return True

    def prefs(self, device: str) -> dict:
        with self.transaction(write=False) as state:
            return self._prefs_view(device, self._row(state, device))

    def set_prefs(self, device: str, expected_revision, changes) -> dict:
        if type(expected_revision) is not int or not isinstance(changes, dict) or not changes or set(changes) - set(
                PREF_KEYS):
            raise PushError("invalid_request")
        if "kinds" in changes and (not isinstance(changes["kinds"], dict) or not changes["kinds"] or any(
                key not in KINDS or type(value) is not bool for key, value in changes["kinds"].items())):
            raise PushError("invalid_request")
        snooze = changes.get("snooze_until")
        if snooze is not None and (type(snooze) is not int or not 0 <= snooze <= 2 ** 40):
            raise PushError("invalid_request")
        for key in ("quiet_when_native_connected", "quiet_at_desk"):
            if key in changes and type(changes[key]) is not bool:
                raise PushError("invalid_request")
        if "lang" in changes and changes["lang"] not in LANGS:
            raise PushError("invalid_request")
        with self.transaction() as state:
            row = self._row(state, device, create=True)
            if int(row.get("revision", 0)) != expected_revision:
                raise PushError("stale_revision", 409)
            prefs = row.setdefault("prefs", json.loads(json.dumps(DEFAULT_PREFS)))
            for key, value in changes.items():
                if key == "kinds":
                    prefs.setdefault("kinds", {}).update(value)
                else:
                    prefs[key] = value
            row["revision"] = int(row.get("revision", 0)) + 1
            return self._prefs_view(device, row)

    # --- what the person at the computer does ------------------------------
    def set_host_enabled(self, device: str, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise PushError("invalid_request")
        with self.transaction() as state:
            row = self._row(state, device, create=True)
            row["host_enabled"] = enabled
            return self._prefs_view(device, row)

    def forget(self, device: str) -> bool:
        """A revoked or purged device keeps nothing here."""
        try:
            with self.transaction() as state:
                return state["devices"].pop(device, None) is not None
        except FileNotFoundError:
            return False

    def drop_endpoint(self, device: str, endpoint: str) -> bool:
        """The push service said the subscription is gone (404/410)."""
        with self.transaction() as state:
            row = self._row(state, device)
            if row and (row.get("subscription") or {}).get("endpoint") == endpoint:
                row["subscription"] = None
                return True
            return False

    def summary(self) -> dict:
        """{device: {subscribed, enabled}} for `devices list`."""
        try:
            with self.transaction(write=False) as state:
                return {device: {"subscribed": bool(row.get("subscription")),
                                 "enabled": bool(row.get("host_enabled", True))}
                        for device, row in state["devices"].items()}
        except (OSError, ValueError):
            return {}

    def live_count(self, devices=None) -> int:
        """LITE-1d: how many web devices this host can reach on a locked screen.

        A row counts when it holds a subscription and the person at the
        computer has not switched push off for it; with `devices`, only rows
        of those device ids (the web devices whose credential still works).
        A number and nothing else - no ids, no endpoints.
        """
        with self.transaction(write=False) as state:
            return sum(1 for device, row in state["devices"].items()
                       if row.get("subscription") and row.get("host_enabled", True)
                       and (devices is None or device in devices))

    def targets(self) -> list[tuple[str, dict, dict]]:
        """(device, subscription, prefs view) for every device that can be pushed to."""
        with self.transaction(write=False) as state:
            return [(device, dict(row["subscription"]), self._prefs_view(device, row))
                    for device, row in state["devices"].items()
                    if row.get("subscription") and row.get("host_enabled", True)]


# ── what is worth a push ──────────────────────────────────────────────────
COPY = {
    "en": {
        "agent_approval": ("The agent needs your approval", "Open Omodachi to answer it."),
        "agent_needs_input": ("The agent is waiting for you", "It asked for an answer on {host}."),
        "agent_done": ("The agent is done", "Its turn on {host} finished."),
        "herdr_blocked": ("{name} needs you", "A pane in Herdr on {host} is waiting for your answer."),
        "herdr_blocked_many": ("{n} agents need you", "Panes in Herdr on {host} are waiting for your answers."),
        "herdr_done": ("{name} finished", "A pane in Herdr on {host} is done and you haven't looked yet."),
        "more": "and {n} more",
        "notifications": "{app}: {n} notifications",
    },
    "zh-Hans": {
        "agent_approval": ("Agent 需要你批准", "打开 Omodachi 处理。"),
        "agent_needs_input": ("Agent 在等你回复", "它在 {host} 上等一个回答。"),
        "agent_done": ("Agent 完成了", "它在 {host} 上的这一轮结束了。"),
        "herdr_blocked": ("{name} 在等你", "{host} 上 Herdr 里有个 pane 在等你回答。"),
        "herdr_blocked_many": ("{n} 个 agent 在等你", "{host} 上 Herdr 里有几个 pane 在等你回答。"),
        "herdr_done": ("{name} 做完了", "{host} 上 Herdr 里有个 pane 做完了，你还没看。"),
        "more": "另有 {n} 条",
        "notifications": "{app}：{n} 条通知",
    },
}
_WAITING_INPUT = ("waiting_on_user_input", "blocked")
AGENT_TAG = "agent"
_ACTIVE = ("working", "waiting_on_approval", "waiting_on_user_input", "blocked")


class Signal:
    """One thing a device may be told: a kind, a source to coalesce on, a tag."""

    def __init__(self, kind, source, tag, *, panel, ids=None, name=None, title=None, body=None,
                 urgency="normal", count=1):
        self.kind, self.source, self.tag = kind, source, tag
        self.panel, self.ids = panel, dict(ids or {})
        self.name, self.title, self.body, self.urgency = name, title, body, urgency
        # NOTIFY-1: a Herdr digest counts the agents it is about.
        self.count = count


class Retract:
    def __init__(self, tag, source):
        self.tag, self.source = tag, source


class DeskChange:
    """NOTIFY-1: the desk was left (or taken): held pushes may go now."""

    def __init__(self, present):
        self.present = present


HERDR_TAG = "herdr"
DONE_TAG = "herdr-done"


def _tag(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", value)[:32]


def topic_for(tag: str) -> str:
    """The `Topic` header for a tag: 16 hex characters.

    Measured on the iPhone run: Apple answered `400 BadWebPushTopic` to
    `note-1790857529061-13` and took `agent-approval`, `herdr-w9-p1` and
    `agent-done`. It reads Topic as base64url, and a 21-character string is
    never valid base64 (length % 4 == 1). A fixed-length hex digest is valid
    base64url, within RFC 8030's 32 characters, and the same for the same tag.
    """
    return hashlib.sha256(tag.encode()).hexdigest()[:16]


class Watcher:
    """Remembers what the hub said last, so a push is about a change."""

    def __init__(self, *, started_at=None):
        self.agent_status = None
        self.blocked: dict[str, set[str]] = {}
        # NOTIFY-1: the agent's name per blocked pane, for the digest's title.
        self.names: dict[tuple[str, str], str] = {}
        # NOTIFY-1: Herdr panes whose agent finished and nobody has looked at,
        # `(session, pane) -> name`; None until the first look.
        self.unseen: dict[tuple[str, str], str] | None = None
        self.desk_present = None
        self.started_ms = int((time.time() if started_at is None else started_at) * 1000) - 30000

    def prime(self, hub):
        agent = (hub.state_view("agent")["agent"] or {})
        self.agent_status = agent.get("status")
        try:
            view = hub.state_view("unseen", "desk")
        except Exception:
            view = {}
        if isinstance(view.get("unseen"), dict):
            self.unseen = self._unseen_rows(view["unseen"])
        if isinstance(view.get("desk"), dict):
            self.desk_present = view["desk"].get("present")

    @staticmethod
    def _unseen_rows(value) -> dict:
        rows = value.get("herdr") if isinstance(value, dict) else None
        out = {}
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and isinstance(row.get("session"), str) and isinstance(row.get("pane_id"), str):
                out[(row["session"], row["pane_id"])] = _clean(row.get("name") or "", 32)
        return out

    def blocked_now(self) -> list[tuple[str, str]]:
        """Every blocked pane across the watched sessions, in a stable order."""
        return sorted((session, pane) for session, panes in self.blocked.items() for pane in panes)

    def still_waiting(self, signal) -> bool:
        """NOTIFY-1: is what a held push is about still true?"""
        if signal.kind == "agent_approval":
            return self.agent_status == "waiting_on_approval"
        if signal.kind == "agent_needs_input":
            return self.agent_status in _WAITING_INPUT
        if signal.kind == "agent_done":
            return self.agent_status == "done"
        if signal.kind == "herdr_done":
            return self.unseen is not None and (signal.ids.get("session"), signal.ids.get("pane")) in self.unseen
        return True

    def signals(self, event, hub) -> list:
        kind, payload = event.type, event.payload or {}
        out = []
        if kind == "agent.changed":
            # The status this event set, not the state when it is handled: two
            # changes published back to back must both be seen. A patch without
            # a status did not change it.
            patch = payload.get("agent") if isinstance(payload.get("agent"), dict) else {}
            previous = self.agent_status
            status = patch["status"] if "status" in patch else previous
            self.agent_status = status
            if status == previous:
                return out
            # One tag for the default agent (LITE-1b, from the iPhone run): a
            # newer state replaces the last one on screen everywhere, which is
            # the only way to take a stale "needs your approval" off an iPhone,
            # where nothing is ever withdrawn silently.
            pushed = None
            if status == "waiting_on_approval":
                pushed = Signal("agent_approval", "agent", AGENT_TAG, panel="agent",
                                ids={"ref": "approval"}, urgency="high")
            elif status in _WAITING_INPUT and previous not in _WAITING_INPUT:
                pushed = Signal("agent_needs_input", "agent", AGENT_TAG, panel="agent",
                                ids={"ref": "input"}, urgency="high")
            elif status == "done" and previous in _ACTIVE:
                pushed = Signal("agent_done", "agent", AGENT_TAG, panel="agent", ids={"ref": "done"})
            waited = previous == "waiting_on_approval" or (previous in _WAITING_INPUT and status not in _WAITING_INPUT)
            if waited and pushed is None:
                out.append(Retract(AGENT_TAG, "agent"))
            if pushed is not None:
                out.append(pushed)
        elif kind == "herdr.layout.changed":
            session = payload.get("session")
            rows = payload.get("blocked")
            if not isinstance(session, str) or not isinstance(rows, list):
                return out
            names = {row["pane_id"]: row.get("name") for row in rows
                     if isinstance(row, dict) and isinstance(row.get("pane_id"), str)}
            agent = hub.state_view("agent")["agent"] or {}
            default_pane = (agent.get("default_agent") or {}).get("pane_id")
            current = {pane for pane in names if pane != default_pane or session != "omodachi"}
            before = self.blocked.get(session)
            self.blocked[session] = current
            if before is None:
                return out  # the first look at a session is a baseline, not news
            for pane in sorted(before - current):
                out.append(Retract(_tag("herdr-" + pane), f"herdr:{session}:{pane}"))
            for pane in current:
                self.names[(session, pane)] = _clean(names.get(pane) or "", 32)
            for pane in sorted(current - before):
                name = _clean(names.get(pane) or "", 32)
                ids = {"session": session, "pane": pane}
                # HERDR-4: Odomo's card names the agent, never the raw pane id.
                # The name is already the push's own title; it rides along only
                # when it is a plain agent name.
                if AGENT_NAME.fullmatch(name):
                    ids["agent"] = name
                out.append(Signal("herdr_blocked", f"herdr:{session}:{pane}", _tag("herdr-" + pane),
                                  panel="herdr", ids=ids, name=name or "An agent", urgency="high"))
        elif kind == "unseen.changed" and isinstance(payload.get("unseen"), dict):
            # NOTIFY-1: a Herdr agent that finished with nobody looking.
            current = self._unseen_rows(payload["unseen"])
            before = self.unseen
            self.unseen = current
            if before is None:
                return out
            for key in sorted(set(before) - set(current)):
                out.append(Retract(_tag("done-" + key[1]), DONE_TAG))
            for key in sorted(set(current) - set(before)):
                session, pane = key
                name = current[key]
                ids = {"session": session, "pane": pane}
                if AGENT_NAME.fullmatch(name):
                    ids["agent"] = name
                out.append(Signal("herdr_done", DONE_TAG, _tag("done-" + pane), panel="herdr", ids=ids,
                                  name=name or "An agent"))
        elif kind == "desk.changed" and isinstance(payload.get("desk"), dict):
            present = payload["desk"].get("present")
            if present != self.desk_present:
                self.desk_present = present
                out.append(DeskChange(present))
        elif kind == "notification.posted":
            if payload.get("active") is True and int(payload.get("timestamp") or 0) >= self.started_ms:
                app = _clean(payload.get("app"), 40) or "Omarchy"
                identifier = str(payload.get("id"))
                out.append(Signal("host_notification", "note:" + app, _tag("note-" + identifier),
                                  panel="notifications", ids={"note": identifier}, name=app,
                                  title=_clean(payload.get("summary"), TITLE_LIMIT) or app,
                                  body=_clean(payload.get("body"), BODY_LIMIT),
                                  urgency="high" if payload.get("urgency") == "critical" else "normal"))
        # `notification.updated` (a toast left the desktop's screen) is never a
        # retraction: the files cannot tell a dismissal from a toast that timed
        # out after five seconds, and the person away from the computer is
        # exactly who should still have it on the phone (measured on the VM:
        # an 8 s toast took the push off Chrome with it).
        #
        # NOTIFY-3, the one exception: `withdrawn` is not a toast leaving the
        # screen, it is core knowing the request it announced was decided. A
        # phone still saying "wants to connect" would be wrong, so it goes too.
        elif kind == "notification.updated":
            identifier = payload.get("id")
            if payload.get("withdrawn") is True and isinstance(identifier, str):
                app = _clean(payload.get("app"), 40) or "Omarchy"
                out.append(Retract(_tag("note-" + identifier), "note:" + app))
        return out


HOST_ID = re.compile(r"[0-9a-f]{32}")
AGENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")


def navigate_url(origin: str, signal: Signal, host_id: str | None = None) -> str:
    """Odomo's own URL for the place a push is about, ids only.

    NOTIFY-2: `host` is this computer's host id (the one on `/health` and in the
    pairing claim), so Odomo can turn the push into the App's link
    (`/open/agent?host=`, `/open/herdr?host=&session=&pane=`) without reading
    anything else. Anything that is not a host id is left out.
    """
    ids = dict(signal.ids)
    if isinstance(host_id, str) and HOST_ID.fullmatch(host_id):
        ids["host"] = host_id
    query = urllib.parse.urlencode({"open": signal.panel, **ids})
    return origin.rstrip("/") + "/odomo/?" + query


def payload_for(signal: Signal, *, origin: str, lang: str, host: str, more: int = 0,
                host_id: str | None = None) -> bytes:
    """The declarative message, at most PAYLOAD_LIMIT bytes."""
    copy = COPY.get(lang) or COPY["en"]
    host = _clean(host, 40) or "the computer"
    if signal.kind == "host_notification":
        title = signal.title or signal.name or "Omarchy"
        body = signal.body or ""
        if more:
            title = copy["notifications"].format(app=signal.name or "Omarchy", n=more + 1)
    else:
        key = "herdr_blocked_many" if signal.kind == "herdr_blocked" and signal.count > 1 else signal.kind
        title, body = copy[key]
        title = title.format(name=signal.name or "An agent", host=host, n=signal.count)
        body = body.format(name=signal.name or "", host=host, n=signal.count)
        if more:
            body = body + " (" + copy["more"].format(n=more) + ")"
    note = {"title": _clean(title, TITLE_LIMIT) or "Omodachi", "body": _clean(body, BODY_LIMIT),
            "navigate": navigate_url(origin, signal, host_id), "tag": signal.tag,
            "lang": "zh-Hans" if lang == "zh-Hans" else "en", "dir": "ltr"}
    raw = json.dumps({"web_push": 8030, "notification": note}, ensure_ascii=False,
                     separators=(",", ":")).encode()
    while len(raw) > PAYLOAD_LIMIT and note["body"]:
        note["body"] = note["body"][: max(0, len(note["body"]) // 2 - 1)].rstrip() + "…" if len(note["body"]) > 2 else ""
        raw = json.dumps({"web_push": 8030, "notification": note}, ensure_ascii=False,
                         separators=(",", ":")).encode()
    if len(raw) > PAYLOAD_LIMIT:
        raise PushError("push_payload_too_large")
    return raw


def retract_payload(tag: str) -> bytes:
    return json.dumps({"omodachi": "retract", "tag": tag}, separators=(",", ":")).encode()


class PushSender:
    """The hub observer that turns events into pushes. One per daemon."""

    def __init__(self, hub, store: PushStore, vapid, *, origin, host_name, native_alerts,
                 host_id=None, send=None, clock=time.monotonic, debounce=DEBOUNCE_SECONDS,
                 sleep=time.sleep, workers=2, log=None, desk=None, herdr_delay=HERDR_DELAY_SECONDS):
        self.hub, self.store = hub, store
        self._vapid = vapid  # callable -> webpush.Vapid
        self._origin = origin  # callable -> "https://name[:port]" or None
        self._host_name = host_name  # callable -> str
        self._host_id = host_id or (lambda: None)  # callable -> 32 hex or None
        # NOTIFY-2: callable(kind) -> bool, "a native client is in front and
        # alerts this kind itself" (`CoreService.native_alerts`).
        self._native_alerts = native_alerts
        if send is None:
            from .webpush import send as webpush_send
            send = webpush_send
        self._send = send
        self._clock, self._sleep = clock, sleep
        self.debounce = debounce
        # NOTIFY-1: callable -> {"present": bool, ...} or None (`CoreService.desk_state`).
        self._desk = desk or (lambda: None)
        self.herdr_delay = herdr_delay
        self.watcher = Watcher()
        self._windows: dict[tuple[str, str], dict] = {}
        # NOTIFY-1: what waits for the desk to be left, per (device, source).
        self._held: dict[tuple[str, str], tuple] = {}
        # NOTIFY-1: the Herdr digest, per device - the panes its last digest
        # named, the trailing timer, and whether one waits for the desk.
        self._herdr_announced: dict[str, set] = {}
        self._herdr_timers: dict[str, object] = {}
        self._herdr_held: set[str] = set()
        self._sent_titles: deque = deque(maxlen=128)
        self._loop = None
        self._queues: dict[str, asyncio.Queue] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._workers = workers  # kept for callers; delivery is one ordered lane per device
        self.results = deque(maxlen=64)
        self._log = log if log is not None else (lambda line: print(json.dumps(line), flush=True))

    # --- wiring -------------------------------------------------------------
    def start(self, loop=None):
        self._loop = loop or asyncio.get_running_loop()
        self.watcher.prime(self.hub)
        self.hub.add_observer(self.observe)

    async def stop(self):
        self.hub.remove_observer(self.observe)
        for window in self._windows.values():
            if window.get("timer"):
                window["timer"].cancel()
        for timer in self._herdr_timers.values():
            timer.cancel()
        self._herdr_timers = {}
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks, self._queues = {}, {}

    def observe(self, event):
        """Called by `Hub.publish` on whichever thread published."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._on_event, event)
        except RuntimeError:
            pass  # the loop is shutting down

    async def drain(self):
        """Tests: wait until every queued push has been handled."""
        await asyncio.sleep(0)
        for queue in list(self._queues.values()):
            await queue.join()

    # --- deciding -------------------------------------------------------------
    def _echo(self, signal) -> bool:
        if signal.kind != "host_notification":
            return False
        now = self._clock()
        return any(title == signal.title and now - at < ECHO_SECONDS for title, at in self._sent_titles)

    def _on_event(self, event):
        try:
            signals = self.watcher.signals(event, self.hub)
        except Exception:
            return
        signals = [item for item in signals if not isinstance(item, Signal) or not self._echo(item)]
        if not signals:
            return
        try:
            targets = self.store.targets()
        except (OSError, ValueError):
            return
        native: dict[str, bool] = {}
        now = time.time()
        herdr_changed = False
        for item in signals:
            if isinstance(item, DeskChange):
                if not item.present:
                    self._release(targets)
                continue
            if isinstance(item, Retract) and item.source.startswith("herdr:"):
                herdr_changed = True  # the digest decides (`_herdr_settle`)
                continue
            if isinstance(item, Signal) and item.kind == "herdr_blocked":
                herdr_changed = True
                continue
            for device, subscription, prefs in targets:
                if isinstance(item, Retract):
                    self._cancel_pending(device, item)
                    self._held.pop((device, item.source), None)
                    if not _on_service(endpoint_host(subscription["endpoint"]), NO_RETRACT):
                        self._enqueue({"device": device, "subscription": subscription, "retract": item.tag})
                    continue
                if not self._wanted(prefs, item.kind, now):
                    continue
                if self._quiet(prefs, item.kind, native):
                    continue
                if self._at_desk(prefs):
                    # NOTIFY-1: held, newest per source, until the desk is left.
                    self._held[(device, item.source)] = (subscription, prefs, item)
                    continue
                self._admit(device, subscription, prefs, item)
        if herdr_changed:
            self._herdr_settle(targets)

    @staticmethod
    def _wanted(prefs, kind, now=None) -> bool:
        if not prefs["kinds"].get(kind, kind not in OFF_BY_DEFAULT):
            return False
        snooze = prefs.get("snooze_until")
        return not (isinstance(snooze, int) and snooze > (time.time() if now is None else now))

    def _at_desk(self, prefs) -> bool:
        """NOTIFY-1: the person is at the computer, and this device asked for quiet then."""
        if not prefs.get("quiet_at_desk", True):
            return False
        try:
            desk = self._desk()
        except Exception:
            desk = None
        return isinstance(desk, dict) and desk.get("present") is True

    def _fresh(self, device):
        """(subscription, prefs) as stored now, or None: a held or delayed push
        is judged by what the device wants when it leaves, not when it was made."""
        try:
            for target, subscription, prefs in self.store.targets():
                if target == device:
                    return subscription, prefs
        except (OSError, ValueError):
            return None
        return None

    def _release(self, targets=None):
        """NOTIFY-1: the desk was left or locked. What is still waiting goes;
        what was answered at the computer meanwhile is dropped."""
        held, self._held = self._held, {}
        for (device, _source), (_subscription, _prefs, signal) in sorted(held.items(), key=lambda row: row[0]):
            fresh = self._fresh(device)
            if fresh is None or not self.watcher.still_waiting(signal):
                continue
            subscription, prefs = fresh
            if not self._wanted(prefs, signal.kind) or self._quiet(prefs, signal.kind):
                continue
            self._admit(device, subscription, prefs, signal)
        waiting, self._herdr_held = self._herdr_held, set()
        for device in sorted(waiting):
            self._herdr_fire(device)

    # --- NOTIFY-1: the Herdr digest -------------------------------------------
    def _herdr_settle(self, targets):
        """A Herdr pane started or stopped waiting.

        New panes start (or join) the device's trailing timer; nothing is sent
        now. When every pane the last digest named has been answered, it is
        withdrawn where the platform allows it, and a digest that waited for
        the desk is let go.
        """
        current = set(self.watcher.blocked_now())
        for device, subscription, prefs in targets:
            if not current:
                timer = self._herdr_timers.pop(device, None)
                if timer is not None:
                    timer.cancel()
                self._herdr_held.discard(device)
                if self._herdr_announced.pop(device, None) and not _on_service(
                        endpoint_host(subscription["endpoint"]), NO_RETRACT):
                    self._enqueue({"device": device, "subscription": subscription, "retract": HERDR_TAG})
                continue
            announced = self._herdr_announced.setdefault(device, set())
            announced &= current
            if current - announced and device not in self._herdr_timers and device not in self._herdr_held:
                self._herdr_timers[device] = self._loop.call_later(self.herdr_delay, self._herdr_fire, device)

    def _herdr_fire(self, device):
        timer = self._herdr_timers.pop(device, None)
        if timer is not None:
            timer.cancel()
        panes = self.watcher.blocked_now()
        announced = self._herdr_announced.setdefault(device, set())
        if not set(panes) - announced:
            return  # answered at the computer inside the delay: nothing to say
        fresh = self._fresh(device)
        if fresh is None:
            return
        subscription, prefs = fresh
        if not self._wanted(prefs, "herdr_blocked") or self._quiet(prefs, "herdr_blocked"):
            return
        if self._at_desk(prefs):
            self._herdr_held.add(device)
            return
        new = [key for key in panes if key not in announced]
        session, pane = new[0]
        name = self.watcher.names.get((session, pane)) or ""
        ids = {"session": session, "pane": pane}
        if AGENT_NAME.fullmatch(name):
            ids["agent"] = name
        signal = Signal("herdr_blocked", "herdr", HERDR_TAG, panel="herdr", ids=ids,
                        name=name or "An agent", urgency="high", count=len(panes))
        self._herdr_announced[device] = set(panes)
        self._enqueue({"device": device, "subscription": subscription, "prefs": prefs, "signal": signal, "more": 0})

    def _quiet(self, prefs, kind, cache=None) -> bool:
        """NOTIFY-2: held back because the App, in front, already says it."""
        if not prefs.get("quiet_when_native_connected", True):
            return False
        if cache is not None and kind in cache:
            return cache[kind]
        try:
            value = bool(self._native_alerts(kind))
        except Exception:
            value = False
        if cache is not None:
            cache[kind] = value
        return value

    def _admit(self, device, subscription, prefs, signal):
        key = (device, signal.source)
        now = self._clock()
        window = self._windows.setdefault(key, {"sent_at": None, "pending": None, "count": 0, "timer": None})
        if window["sent_at"] is None or now - window["sent_at"] >= self.debounce:
            if window["timer"] is None:
                window["sent_at"] = now
                self._enqueue({"device": device, "subscription": subscription, "prefs": prefs,
                               "signal": signal, "more": 0})
                return
        window["pending"] = (subscription, prefs, signal)
        window["count"] += 1
        if window["timer"] is None:
            delay = max(0.0, self.debounce - (now - (window["sent_at"] or now)))
            window["timer"] = self._loop.call_later(delay, self._flush, key)

    def _flush(self, key):
        window = self._windows.get(key)
        if not window:
            return
        window["timer"] = None
        pending, count = window["pending"], window["count"]
        window["pending"], window["count"] = None, 0
        if pending is None:
            return
        subscription, prefs, signal = pending
        # NOTIFY-1 (Collie §4): a snooze, or a kind switched off, also stops
        # what was already waiting in its window - read as stored now.
        fresh = self._fresh(key[0])
        if fresh is None:
            return
        subscription, prefs = fresh
        if not self._wanted(prefs, signal.kind):
            return
        if self._quiet(prefs, signal.kind):
            # The App came to the front, alerting this kind, while the push
            # waited out its window: it has been said there already.
            return
        if self._at_desk(prefs):
            self._held[key] = (subscription, prefs, signal)
            return
        window["sent_at"] = self._clock()
        self._enqueue({"device": key[0], "subscription": subscription, "prefs": prefs, "signal": signal,
                       "more": count - 1})

    def _cancel_pending(self, device, retract):
        window = self._windows.get((device, retract.source))
        if window and window["pending"] is not None and window["pending"][2].tag == retract.tag:
            window["pending"], window["count"] = None, 0

    def _enqueue(self, job):
        # One lane per device, in order: a retraction must never overtake the
        # push it retracts (or the one that replaces it). Devices run side by side.
        device = job["device"]
        queue = self._queues.get(device)
        if queue is None:
            queue = self._queues[device] = asyncio.Queue(maxsize=64)
            self._tasks[device] = self._loop.create_task(self._worker(queue))
        try:
            queue.put_nowait(job)
        except asyncio.QueueFull:
            self._record(job, "dropped_queue_full")

    # --- sending --------------------------------------------------------------
    async def _worker(self, queue):
        while True:
            job = await queue.get()
            try:
                await asyncio.to_thread(self._deliver, job)
            except Exception as error:
                self._record(job, "error:" + type(error).__name__)
            finally:
                queue.task_done()

    def _deliver(self, job):
        origin = self._origin()
        if not origin:
            return self._record(job, "no_public_name")
        if "retract" in job:
            # NOTIFY-1 (Collie §4): a retraction lives as long as the push it
            # takes back, is as urgent, and rides the same topic, so a device
            # offline for a while still gets it (and it replaces the push
            # where the service still holds it).
            body, ttl, urgency, topic = retract_payload(job["retract"]), 3600, "high", topic_for(job["retract"])
        else:
            signal = job["signal"]
            try:
                host_id = self._host_id()
            except Exception:
                host_id = None
            body = payload_for(signal, origin=origin, lang=job["prefs"].get("lang", "en"),
                               host=self._host_name() or "", more=job.get("more", 0), host_id=host_id)
            ttl, urgency, topic = 3600, signal.urgency, topic_for(signal.tag)
        if "retract" not in job:
            title = json.loads(body)["notification"]["title"]
            self._loop.call_soon_threadsafe(self._sent_titles.append, (title, self._clock()))
        vapid = self._vapid()
        result = None
        for attempt in range(RETRIES + 1):
            result = self._send(job["subscription"], body, vapid, subject=origin, ttl=ttl,
                                urgency=urgency, topic=topic)
            status = result.get("status", 0)
            if status in (404, 410):
                self.store.drop_endpoint(job["device"], job["subscription"]["endpoint"])
                break
            if not (status == 0 or status == 429 or 500 <= status <= 599) or attempt == RETRIES:
                break
            self._sleep(min(result.get("retry_after") or 2 ** attempt, 30))
        extra = {"size": len(body), "attempts": attempt + 1}
        if result.get("reason"):
            extra["reason"] = result["reason"]
        return self._record(job, result.get("status", 0), **extra)

    def _record(self, job, status, **extra):
        line = {"omodachi": "push", "device_id": job.get("device"),
                "kind": "retract" if "retract" in job else getattr(job.get("signal"), "kind", None),
                "tag": job.get("retract") or getattr(job.get("signal"), "tag", None),
                "service": endpoint_host((job.get("subscription") or {}).get("endpoint", "")),
                "status": status, "more": job.get("more", 0), **extra}
        self.results.append(line)
        self._log(line)
        return line
