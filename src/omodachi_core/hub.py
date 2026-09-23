"""Device-level state and events, independent of Desktop availability."""
from __future__ import annotations

import asyncio
import inspect
import copy
from dataclasses import asdict, dataclass
import time
import uuid
from typing import Any, AsyncIterator, Callable, Mapping

from .auth import DeviceAuthenticator
from .protocol import IDLE_REMOTE_BAR


@dataclass(frozen=True)
class DeviceEvent:
    seq: int
    event_id: str
    type: str
    device_id: str | None
    payload: dict[str, Any]
    ts: float


@dataclass(eq=False)
class _Subscriber:
    queue: asyncio.Queue[DeviceEvent]
    device_id: str | None
    overflowed: bool = False


class Hub:
    """All mutations run on one asyncio thread; network adapters authenticate first."""
    def __init__(self, *, authenticator=None, history_limit=1000,
                 subscriber_queue_limit=256, auth_check_interval=1.0):
        if type(history_limit) is not int or history_limit <= 0:
            raise ValueError("history_limit must be positive")
        if type(subscriber_queue_limit) is not int or subscriber_queue_limit <= 0:
            raise ValueError("subscriber_queue_limit must be positive")
        if auth_check_interval <= 0:
            raise ValueError("auth_check_interval must be positive")
        self.instance_id = uuid.uuid4().hex
        self.auth = authenticator or DeviceAuthenticator()
        # The host identity a companion pins on pairing. The owner installs it;
        # without one, health still answers and reports the fields as unknown.
        self.health_extra = None
        # PERF-4 §0: the daemon's own reading of what it is holding, answered
        # on `/health` so a leak is visible from outside the process.
        self.resources = None
        self.remote = None
        self._state = {
            "revision": 0,
            "host": {"name": None, "connected": False},
            "workspace": {"active": None},
            "focus": {"window": None},
            "agent": {"kind": None, "exists": False, "pane_available": False, "status": "unknown"},
            "herdr": {"available": False, "agent_count": 0},
            "remote": {"session_id": None, "state": "offline", "mode": None, "backend": None, "revision": 0},
            "capabilities": {"sunshine": False, "terminal": False, "desktop": False, "native": []},
            "catalog": {"revision": None, "entries": []},
            # ARCH-1 / Study 04 N-36: Do Not Disturb belongs to the host, and
            # the desktop can change it while a client is looking at it. `None`
            # is "nobody has read the shell's own state yet" - a client must not
            # draw a switch as off because it has not been told.
            "notifications": {"dnd": None},
            "remote_bar": dict(IDLE_REMOTE_BAR, workspaces=[]),
        }
        self._history_limit = history_limit
        self._queue_limit = subscriber_queue_limit
        self._auth_check_interval = auth_check_interval
        self._events: list[DeviceEvent] = []
        self._seq = 0
        self._subscribers: set[_Subscriber] = set()
        self._handlers: dict[str, Callable[[str, dict[str, Any]], Any]] = {}

    @property
    def event_cursor(self) -> int:
        return self._seq

    def connected_devices(self) -> set[str]:
        """Which paired devices have a live event subscription right now.

        AUTH-1 asks this before it publishes an approval: a device that is not
        listening cannot answer, and a PAM prompt that waits 45 s for a tablet
        in another room is exactly the failure this feature must not have. An
        overflowed subscriber is about to be told to resync, so it does not
        count as somewhere an approval can be delivered.
        """
        return {subscriber.device_id for subscriber in self._subscribers
                if subscriber.device_id and not subscriber.overflowed}

    def register_device(self, device_id, now=None):
        return self.auth.issue(device_id, now=now).token

    def authenticate(self, token, *, local=False):
        """`local` is the same-UID Unix socket (CORE-2, see `DeviceAuthenticator.verify`)."""
        if local and "local" in inspect.signature(self.auth.verify).parameters:
            return self.auth.verify(token, local=True)
        return self.auth.verify(token)

    def tick(self) -> bool:
        """Remote session expiry belongs to RemoteService; kept for idle transports."""
        return False

    def state_view(self, *keys):
        """A copy of just these top-level state keys.

        PERF-4 §0: `state_snapshot()` deep-copies the whole state, and the
        catalog inside it is 594 rows — about 580 KB. The daemon called it 630
        times a minute to read `host.catalog_stale` or `workspace.active`, and
        `copy.deepcopy` was two thirds of its CPU. Nothing internal needs the
        catalog out of the state; the clients that do read it over HTTP.
        """
        return {key: copy.deepcopy(self._state.get(key)) for key in keys}

    def state_snapshot(self, device_id=None, *, copy_state=True):
        """The whole state. `copy_state=False` is for a caller that only serialises it."""
        snapshot = copy.deepcopy(self._state) if copy_state else dict(self._state)
        snapshot["device_id"] = device_id
        snapshot["event_cursor"] = self._seq
        snapshot["instance_id"] = self.instance_id
        return snapshot

    def capabilities_snapshot(self):
        return copy.deepcopy(self._state["capabilities"])

    def herdr_snapshot(self):
        return copy.deepcopy(self._state["herdr"] | {"agent": self._state["agent"]})

    def catalog_snapshot(self):
        return copy.deepcopy(self._state["catalog"])

    def update_state(self, patch, *, event_type="state.changed", device_id=None, event_data=None,
                     event_patch=None):
        """Merge `patch` into the state and announce it.

        `event_patch` announces something smaller than what was merged. PERF-4:
        the catalog is half a megabyte of rows, the event history keeps a
        thousand events and every subscriber queue keeps up to 256 more, so
        putting the whole catalog in the payload cost about 375 KiB of live
        objects *per event* - measured, 200 events for 75 MiB - and the host
        was found at 12 GB resident with 11 GB in swap. No client reads those
        rows out of the event anyway: `catalog.changed` means "read it again".
        """
        if not isinstance(patch, dict) or "revision" in patch:
            raise ValueError("state patch must be an object without revision")
        if event_patch is not None and (not isinstance(event_patch, dict) or "revision" in event_patch):
            raise ValueError("state patch must be an object without revision")
        self._deep_merge(self._state, copy.deepcopy(patch))
        self._state["revision"] += 1
        payload = copy.deepcopy(patch if event_patch is None else event_patch)
        if event_data:
            payload.update(copy.deepcopy(event_data))
        payload["revision"] = self._state["revision"]
        return self.publish(event_type, payload, device_id=device_id)

    @staticmethod
    def _visible(event, device_id):
        # An untargeted subscriber sees broadcasts only. No implicit privileged tap.
        return event.device_id is None or event.device_id == device_id

    def _resync_event(self, device_id, *, since, reason):
        return DeviceEvent(self._seq, f"resync_{self._seq:08d}", "resync.required", device_id,
                           {"since": since, "cursor": self._seq, "reason": reason,
                            "snapshot_required": True, "instance_id": self.instance_id}, time.time())

    def publish(self, event_type, payload, *, device_id=None):
        if not isinstance(event_type, str) or not event_type or not isinstance(payload, dict):
            raise ValueError("event type and object payload required")
        if event_type == "panel.summon" and not device_id:
            raise ValueError("panel.summon requires an explicit device target")
        self._seq += 1
        event = DeviceEvent(self._seq, f"evt_{self._seq:08d}", event_type, device_id,
                            copy.deepcopy(payload), time.time())
        self._events.append(event)
        del self._events[:-self._history_limit]
        for subscriber in tuple(self._subscribers):
            if subscriber.overflowed or not self._visible(event, subscriber.device_id):
                continue
            if subscriber.queue.full():
                while not subscriber.queue.empty():
                    subscriber.queue.get_nowait()
                subscriber.overflowed = True
                subscriber.queue.put_nowait(self._resync_event(subscriber.device_id,
                                                             since=event.seq, reason="queue_overflow"))
            else:
                subscriber.queue.put_nowait(copy.deepcopy(event))
        return copy.deepcopy(event)

    def events_since(self, seq=0, limit=100, *, device_id=None, instance_id=None):
        if type(seq) is not int or seq < 0:
            raise ValueError("event cursor must be a nonnegative integer")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= max(1000, self._history_limit)):
            raise ValueError("event limit is out of range")
        if instance_id is not None and instance_id != self.instance_id:
            return [self._resync_event(device_id, since=seq, reason="daemon_restarted")]
        oldest = self._events[0].seq if self._events else self._seq + 1
        if seq < oldest - 1 or seq > self._seq:
            return [self._resync_event(device_id, since=seq, reason="history_unavailable")]
        events = [event for event in self._events if event.seq > seq and self._visible(event, device_id)]
        return copy.deepcopy(events if limit is None else events[:limit])

    async def subscribe(self, *, since=0, device_id=None, token=None, instance_id=None,
                        local=False) -> AsyncIterator[DeviceEvent]:
        """Register before yielding replay; a live token is rechecked while idle."""
        def authenticate(value):
            return self.authenticate(value, local=True) if local else self.authenticate(value)
        if token is not None:
            owner = authenticate(token)
            if device_id is not None and owner != device_id:
                raise ValueError("credential does not match subscription target")
            device_id = owner
        subscriber = _Subscriber(asyncio.Queue(maxsize=self._queue_limit), device_id)
        self._subscribers.add(subscriber)
        try:
            # No await between registration and snapshot: concurrent publication is
            # queued while replay is yielded. Replaying every retained event is vital.
            replay = self.events_since(since, limit=None, device_id=device_id, instance_id=instance_id)
            for event in replay:
                if token is not None:
                    authenticate(token)
                if subscriber.overflowed:
                    break
                yield event
                if event.type == "resync.required":
                    return
            while True:
                if token is not None:
                    authenticate(token)
                try:
                    event = await asyncio.wait_for(subscriber.queue.get(), self._auth_check_interval)
                except asyncio.TimeoutError:
                    continue
                if token is not None:
                    authenticate(token)
                yield copy.deepcopy(event)
                if event.type == "resync.required":
                    return
        finally:
            self._subscribers.discard(subscriber)

    def register_handler(self, op: str, handler: Callable[[str, dict[str, Any]], Any]) -> None:
        """Register an allowlisted service operation; it receives (device_id, params)."""
        if not isinstance(op, str) or not op or op in self._handlers or op in self._BUILTIN_OPS:
            raise ValueError("operation is empty or already registered")
        if not callable(handler):
            raise ValueError("operation handler must be callable")
        self._handlers[op] = handler

    _BUILTIN_OPS = frozenset({"health", "state", "capabilities", "herdr", "catalog", "events"})

    def dispatch(self, op: str, params: Mapping[str, Any], device_id: str | None = None) -> Any:
        """Transport-neutral dispatch. Adapter supplies its authenticated device id."""
        if not isinstance(params, Mapping):
            raise ValueError("operation parameters must be an object")
        if op == "health":
            extra = self.health_extra() if callable(self.health_extra) else {}
            reading = self.resources.snapshot() if self.resources is not None else None
            return {"service": "omodachid", "sunshine_required": False,
                    "host_id": None, "tls_fingerprint_sha256": None, **extra,
                    "resources": reading}
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("authenticated device required")
        if op == "state": return self.state_snapshot(device_id)
        if op == "capabilities": return self.capabilities_snapshot()
        if op == "herdr": return self.herdr_snapshot()
        if op == "catalog": return self.catalog_snapshot()
        if op == "events":
            events = self.events_since(params.get("since", 0), params.get("limit", 100), device_id=device_id,
                                       instance_id=params.get("instance_id"))
            return {"events": [asdict(e) for e in events], "cursor": events[-1].seq if events else self._seq,
                    "instance_id": self.instance_id}
        if op in self._handlers:
            return self._handlers[op](device_id, copy.deepcopy(dict(params)))
        raise ValueError(f"unknown op: {op}")

    @staticmethod
    def _deep_merge(dst, patch):
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(dst.get(key), dict):
                Hub._deep_merge(dst[key], value)
            else:
                dst[key] = value
