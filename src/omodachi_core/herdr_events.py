"""HERDR-11 §4: Herdr's own event stream as the "something changed" signal.

Since SPEC-F1 core has learnt about a session by running `herdr api snapshot`
every two seconds, for every running session, watched or not. Herdr protocol
20 (0.8.2, probed on the host) has `events.subscribe`: one socket connection,
one request, and from then on one JSON line per event on that connection.

This module keeps one such connection per session and does nothing with an
event but say which session moved. The projection is still read from the
snapshot — the event is a poke, the snapshot is the truth (Collie's
`event-poker`), so a missed or unknown event costs at most the fallback poll,
never a wrong screen.

What is subscribed, all from the protocol-20 schema:

- the session's structure: workspaces, tabs, panes created, closed, moved,
  renamed, focused, exited, an agent detected, the layout updated;
- `pane.agent_status_changed` — which 0.8.2 takes **per pane** (`pane_id` is
  required), so the subscription is remade when the set of panes changes.

`pane.updated` and `pane.output_matched` are not subscribed: the first fires
for every redraw of a busy pane, and the second is a screen scrape.

A session whose Herdr refuses the request (an older protocol) is marked
`unsupported` and left to the poll; one whose socket fails is retried with a
growing delay and is `healthy` only while a subscription is acknowledged.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Callable

STRUCTURE = ("workspace.created", "workspace.updated", "workspace.renamed", "workspace.moved",
             "workspace.reordered", "workspace.closed", "workspace.focused",
             "tab.created", "tab.closed", "tab.focused", "tab.renamed", "tab.moved",
             "pane.created", "pane.closed", "pane.focused", "pane.moved", "pane.exited",
             "pane.agent_detected", "layout.updated")
# Herdr's own limit on a request line is 1 MiB; a session with this many panes
# is past what a phone shows anyway, and the poll still covers the rest.
MAX_STATUS_PANES = 64
MIN_PROTOCOL = 20
MAX_LINE = 1024 * 1024


def subscription(panes) -> dict:
    rows = [{"type": name} for name in STRUCTURE]
    rows += [{"type": "pane.agent_status_changed", "pane_id": pane}
             for pane in sorted(panes)[:MAX_STATUS_PANES]]
    return {"id": "omodachi-events-" + os.urandom(6).hex(), "method": "events.subscribe",
            "params": {"subscriptions": rows}}


class HerdrEventWatch:
    """One session's subscription, on a thread of its own."""

    RETRY_SECONDS = (1.0, 2.0, 5.0, 15.0, 30.0)

    def __init__(self, session: str, socket_path, on_event: Callable[[str, str], None], *,
                 connect: Callable[[str], socket.socket] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.session = session
        self.socket_path = str(socket_path)
        self.on_event = on_event
        self.connect = connect or self._connect
        self.sleep = sleep
        self._panes: frozenset[str] = frozenset()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._changed = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self.healthy = False
        self.unsupported = False
        self.events = 0
        self.failures = 0

    @staticmethod
    def _connect(path: str) -> socket.socket:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5.0)
        client.connect(path)
        return client

    def set_panes(self, panes) -> None:
        panes = frozenset(pane for pane in panes if isinstance(pane, str))
        with self._lock:
            if panes == self._panes:
                return
            self._panes = panes
        self._changed.set()
        self._drop()

    def start(self) -> "HerdrEventWatch":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=f"herdr-events-{self.session}", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._drop()
        self.healthy = False

    def _drop(self) -> None:
        with self._lock:
            client, self._socket = self._socket, None
        if client is not None:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            started = self.serve_once()
            if self._stop.is_set() or self.unsupported:
                break
            if self._changed.is_set():
                self._changed.clear()
                attempt = 0
                continue
            attempt = 0 if started else attempt + 1
            delay = self.RETRY_SECONDS[min(attempt, len(self.RETRY_SECONDS) - 1)]
            self.sleep(delay)
        self.healthy = False

    def serve_once(self) -> bool:
        """One connection: subscribe, then read events until it ends.

        Returns whether the subscription was acknowledged at all.
        """
        self._changed.clear()
        with self._lock:
            panes = self._panes
        try:
            client = self.connect(self.socket_path)
        except OSError:
            self.failures += 1
            return False
        with self._lock:
            self._socket = client
        if self._changed.is_set() or self._stop.is_set():
            # The panes moved (or the watch ended) while this was connecting.
            self._drop()
            return True
        acknowledged = False
        try:
            client.sendall((json.dumps(subscription(panes)) + "\n").encode())
            client.settimeout(None)
            buffer = bytearray()
            while not self._stop.is_set():
                try:
                    chunk = client.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) > MAX_LINE and b"\n" not in buffer:
                    break
                while b"\n" in buffer:
                    line, _, rest = bytes(buffer).partition(b"\n")
                    buffer = bytearray(rest)
                    verdict = self._line(line, acknowledged)
                    if verdict == "ack":
                        acknowledged = True
                        self.healthy = True
                    elif verdict == "refused":
                        self.unsupported = True
                        return False
        finally:
            self.healthy = False
            self._drop()
        if not acknowledged:
            self.failures += 1
        return acknowledged

    def _line(self, line: bytes, acknowledged: bool) -> str | None:
        try:
            document = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(document, dict):
            return None
        if not acknowledged:
            if document.get("error"):
                code = document["error"].get("code") if isinstance(document["error"], dict) else None
                # A Herdr that does not know the method (or a subscription
                # type) will not learn it by being asked again.
                return "refused" if code in ("unknown_method", "method_not_found", "invalid_params",
                                             "invalid_request", "unsupported") else None
            result = document.get("result")
            if isinstance(result, dict) and result.get("type") == "subscription_started":
                return "ack"
        name = document.get("event")
        if isinstance(name, str):
            self.events += 1
            try:
                self.on_event(self.session, name)
            except Exception:  # pragma: no cover - a poke must never end the watch
                pass
        return None


class HerdrEventWatches:
    """The watches core keeps: one per running session that speaks protocol 20+."""

    def __init__(self, on_event: Callable[[str, str], None], *, factory=None) -> None:
        self.on_event = on_event
        self.factory = factory or (lambda session, path, handler: HerdrEventWatch(session, path, handler).start())
        self._lock = threading.Lock()
        self._watches: dict[str, HerdrEventWatch] = {}

    def ensure(self, session: str, socket_path, layout: dict) -> HerdrEventWatch | None:
        protocol = layout.get("protocol")
        if type(protocol) is not int or protocol < MIN_PROTOCOL or not socket_path:
            return None
        panes = [pane.get("id") for workspace in layout.get("workspaces") or []
                 for tab in workspace.get("tabs") or [] for pane in tab.get("panes") or []]
        with self._lock:
            watch = self._watches.get(session)
            if watch is None or watch.socket_path != str(socket_path):
                if watch is not None:
                    watch.stop()
                watch = self._watches[session] = self.factory(session, Path(socket_path), self.on_event)
        watch.set_panes(panes)
        return watch

    def healthy(self, session: str) -> bool:
        with self._lock:
            watch = self._watches.get(session)
        return bool(watch is not None and watch.healthy and not watch.unsupported)

    def forget(self, session: str) -> None:
        with self._lock:
            watch = self._watches.pop(session, None)
        if watch is not None:
            watch.stop()

    def stop(self) -> None:
        with self._lock:
            watches, self._watches = list(self._watches.values()), {}
        for watch in watches:
            watch.stop()
