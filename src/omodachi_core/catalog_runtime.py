from __future__ import annotations

"""Host-only condition/provider adapter registry and catalog state cache.

Provider text is an opaque lookup key. Condition text (`when` / `checked` /
`disabled`) is looked up the same way first - a reviewed adapter answers it
without a subprocess - and, MENU-3, when a `ConditionEngine` is attached (every
real host; never the demo), an expression from the host's own menu files that
has no adapter is handed to bash the way Omarchy's menu hands it, with the
engine deciding when each reading is taken again. Without an engine, and for
any expression that did not come from a menu source row, an unknown adapter
stays explicitly unavailable instead of pretending false.
"""

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import time
from typing import Any, Callable, Mapping

from .catalog import Catalog, CatalogError, _normalize_row
from .conditions import ConditionEngine, UNKNOWN_PENDING, checked_state_triggers, unknown

#: The row fields that are condition expressions, with the value an empty one means.
CONDITION_FIELDS = (("when", True), ("checked", None), ("disabled", False))


class CatalogRuntime:
    def __init__(self, catalog: Catalog, *, cache_seconds: float = 2.0,
                 clock: Callable[[], float] = time.monotonic,
                 on_change: Callable[[dict[str, Any]], Any] | None = None,
                 conditions: ConditionEngine | None = None):
        self._source = catalog
        # MENU-3. When present, this decides when every non-volatile condition
        # reading is taken again (events and menu opens, never a clock), and
        # answers source expressions no adapter knows with bash.
        self.engine = conditions
        self._shell_expressions: tuple[int, frozenset[str]] | None = None
        self.cache_seconds = cache_seconds
        self.clock = clock
        self.on_change = on_change
        self.conditions: dict[str, Callable[[], bool]] = {}
        # PERF-4 §0. Not every condition costs the same. Most of the Omarchy
        # menu's are shell commands - `pacman -T`, `omarchy-default-browser`,
        # `omarchy-hw-webcam` - and those are what the window below is for. A
        # few are a host adapter reading a value this process already holds,
        # such as whether anything has focus; caching one of those buys nothing
        # and costs correctness, because it is exactly the reading that moves
        # while the user works. Those are registered volatile and read every
        # time, which is also what lets a focused window that moved stop
        # invalidating the expensive ones along with it.
        self.volatile: set[str] = set()
        self.providers: dict[str, Callable[[], list[dict[str, Any]]]] = {}
        self.checked_states: dict[str, tuple[dict[str, str], Callable[[], dict[str, Any]]]] = {}
        self._checked_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        # PERF-4. A provider is a *directory listing* - the desktop entries on
        # this machine, the fonts Omarchy ships, the keybinding records. Reading
        # one costs a subprocess (the Gio scan is half a second), and before
        # this cache every catalog refresh ran every one of them: the 0.5 s
        # workspace probe, the 2 s source poll and three refreshes per invoke
        # each paid for a full rescan, on the asyncio thread, so a trivial
        # `GET /v1/capabilities` measured 2-5 s on the live host.
        #
        # `invalidate()` still drops it, so every path that says "something
        # changed" - a source edit, a workspace that moved, the refresh an
        # invoke takes before it authorises anything - reads the listing again.
        # What this removes is only the repeat: the refreshes that nothing
        # invalidated, which is most of them.
        self._provider_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._snapshot: dict[str, Any] | None = None
        # PERF-4 §0. Rebuilding the snapshot is not free even when every
        # reading is cached: it copies 594 source rows, walks them, and takes a
        # SHA-256 over six hundred kilobytes of JSON - about 100 ms, and the
        # daemon asked for it once a second for ever. `_dirty` says whether any
        # reading has actually *moved* since the last build; the volatile
        # conditions are the ones that can move without a cache miss, so they
        # are re-read on the fast path and compared.
        self._dirty = True
        self._built_at = -float("inf")
        self._volatile_values: dict[str, Any] = {}

    @property
    def catalog(self) -> Catalog:
        return self._source

    @catalog.setter
    def catalog(self, value: Catalog) -> None:
        """Swapping the source is a change, whatever the readings say."""
        self._source = value
        self._dirty = True
        self._shell_expressions = None

    def register_condition(self, expression: str, adapter: Callable[[], bool], *,
                           volatile: bool = False) -> None:
        """`volatile` means "read it every time": an in-process reading, not a shell."""
        if not expression or not callable(adapter):
            raise ValueError("condition registration requires an expression and callable")
        self.conditions[expression] = adapter
        if self.engine is not None:
            self.engine.mark_due([expression])      # the adapter answers it from now on
        if volatile:
            self.volatile.add(expression)
        else:
            self.volatile.discard(expression)
        self.invalidate()

    def attach_conditions(self, engine: ConditionEngine | None) -> None:
        """MENU-3: hand condition freshness to `engine` (bootstrap, after the adapters).

        Attached last, so the first build finds every reviewed adapter already
        registered and spawns a shell only for the expressions none of them
        answers.
        """
        self.engine = engine
        self._dirty = True

    def register_provider(self, expression: str, adapter: Callable[[], list[dict[str, Any]]]) -> None:
        if not expression or not callable(adapter):
            raise ValueError("provider registration requires an expression and callable")
        self.providers[expression] = adapter
        self.invalidate()

    def register_checked_state(self, entry_id: str, adapter: Callable[[], dict[str, Any]], *,
                               reviewed_source: Mapping[str, str]) -> None:
        """Reviewed state projection for a source row with no checked expression.

        The source JSONC is unchanged. Exact source routing/condition fields pin
        this registration, so a user override cannot inherit another action's
        runtime state. Values use the existing checked condition contract.
        """
        required = {"action", "when", "checked", "target", "provider", "surface"}
        if (not isinstance(entry_id, str) or not entry_id or len(entry_id) > 256 or not callable(adapter)
                or set(reviewed_source) != required or any(not isinstance(v, str) for v in reviewed_source.values())
                or reviewed_source["checked"]):
            raise ValueError("checked state registration requires a pinned source without checked")
        self.checked_states[entry_id] = (dict(reviewed_source), adapter)
        self.invalidate()

    def invalidate(self, *, providers: bool = True) -> None:
        """Drop the cached readings.

        `providers=False` keeps the listings, for a caller that knows it has
        changed the *state* a condition reads and not the directories a
        provider walks - the workspace moving is the one that matters, because
        it happens all day and re-walking every desktop entry for it was most
        of PERF-4 §1.
        """
        self._cache.clear()
        self._checked_cache.clear()
        self._dirty = True
        if providers:
            self._provider_cache.clear()
        # MENU-3: with an engine, "something changed" no longer means "run
        # every condition again" - that was PERF-4's loop. A condition is read
        # again when its own trigger fires (a watched path, the package
        # database, a menu opening) or when its row is invoked; see
        # `invalidate_row`, `demand` and `after_invoke`.

    def invalidate_workspace(self) -> None:
        """The active workspace moved: only the readings that are about it.

        PERF-4 C3 dropped every condition here, which was the right size when
        a condition was one of fifteen adapters and is not when it is sixty
        shells. What a workspace switch can change is the workspace layout's
        checked state; nothing in the Omarchy menu's `when` text reads it.
        """
        if self.engine is None:
            self.invalidate(providers=False)
            return
        self.engine.mark_due([key for key in self.engine.known_keys()
                              if key.startswith("state:") and "workspace" in key])
        self._dirty = True

    def invalidate_row(self, entry_id: str) -> bool:
        """Drop only the readings the row `entry_id` is made of.

        PERF-5. `invoke` used to re-read the whole table before it authorised
        anything: every shell condition in the menu and every provider listing
        on the machine, three subprocesses and two dozen shell-outs, on the
        event loop, inside the user's tap. What that read is *for* is one row -
        "is the thing I am about to run still the thing the client was shown".
        So take that row's own readings again and leave the other 640 alone:
        its `when`/`checked`, and the listing it came out of (the desktop
        entries for an `apps.*` row, the keybinding records for a shortcut),
        because that listing is what re-registers the executor and the route
        adapter the authorisation below consults.

        False means this id is not in the snapshot at all, so there is nothing
        row-shaped to re-read and the caller has to fall back to the full
        invalidation - which is also the path that ends in a refusal.
        """
        if self._snapshot is None:
            return False
        row = next((item for item in self._snapshot["entries"] if item.get("id") == entry_id), None)
        if row is None:
            return False
        for expression in (row.get("when"), row.get("checked"), row.get("disabled")):
            if isinstance(expression, str) and expression:
                self._cache.pop(expression, None)
                if self.engine is not None:
                    # An adapter-backed reading is taken again by the refresh
                    # that follows (as before); a shell one by the next warm,
                    # off the event loop - never inside the user's tap.
                    self.engine.mark_due([expression])
        self._checked_cache.pop(entry_id, None)
        if self.engine is not None:
            self.engine.mark_due(["state:" + entry_id])
        # A provider row is re-read through the menu row that owns it; a menu
        # row that *is* a provider re-reads its own listing.
        owner = row.get("providerMenu")
        if isinstance(owner, str) and owner:
            parent = next((item for item in self._snapshot["entries"] if item.get("id") == owner), None)
            provider = (parent or {}).get("provider")
            if isinstance(provider, str) and provider:
                self._provider_cache.pop(provider, None)
        provider = row.get("provider")
        if isinstance(provider, str) and provider:
            self._provider_cache.pop(provider, None)
        self._dirty = True
        return True

    def _provider_rows(self, expression: str, adapter) -> list[dict[str, Any]]:
        """One provider's rows, at most one reading per `cache_seconds`.

        A failure is cached too, as the exception it was: a provider that is
        down must not be retried on every refresh, because retrying it is the
        subprocess this cache exists to avoid.
        """
        cached = self._provider_cache.get(expression)
        if cached is not None and self.clock() - cached[0] <= self.cache_seconds:
            if isinstance(cached[1], Exception):
                raise cached[1]
            return copy.deepcopy(cached[1])
        try:
            incoming = adapter()
        except Exception as error:
            if cached is None or not isinstance(cached[1], Exception) or str(cached[1]) != str(error):
                self._dirty = True
            self._provider_cache[expression] = (self.clock(), error)
            raise
        if cached is None or cached[1] != incoming:
            self._dirty = True
        self._provider_cache[expression] = (self.clock(), copy.deepcopy(incoming)
                                            if isinstance(incoming, list) else incoming)
        return incoming

    def _checked(self, row: Mapping[str, Any]) -> dict[str, Any]:
        registration = self.checked_states.get(row.get("id"))
        if registration is None:
            return self._condition(row.get("checked"), empty_value=None)
        expected, reader = registration
        if any((row.get(key) or "") != value for key, value in expected.items()):
            return {"status": "unavailable", "value": None, "reason": "state_adapter_source_changed"}
        if self.engine is not None:
            key = "state:" + row["id"]
            self.engine.track(key, checked_state_triggers(row["id"]))
            reading = self.engine.reading(key)
            if reading is not None and not self.engine.is_due(key):
                return reading
            if self.engine.evaluate({key: lambda: self._read_state(reader)}, reason="state"):
                self._dirty = True
            return self.engine.reading(key) or reading or {"status": "unavailable", "value": None,
                                                           "reason": "state_adapter_failed"}
        cached = self._checked_cache.get(row["id"])
        if cached and self.clock() - cached[0] <= self.cache_seconds:
            return dict(cached[1])
        result = self._read_state(reader)
        if cached is None or cached[1] != result:
            self._dirty = True
        self._checked_cache[row["id"]] = (self.clock(), result)
        return dict(result)

    @staticmethod
    def _read_state(reader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """One reviewed checked-state reading, validated; a bad one is `state_adapter_failed`."""
        try:
            result = reader()
            if (not isinstance(result, dict) or set(result) - {"status", "value", "reason"}
                    or result.get("status") not in {"available", "unavailable"}
                    or "value" not in result or result["value"] is not None and type(result["value"]) is not bool):
                raise ValueError("invalid checked state")
            reason = result.get("reason")
            if reason is not None and (not isinstance(reason, str) or not 1 <= len(reason) <= 128
                                       or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_:-" for c in reason)):
                raise ValueError("invalid state reason")
            if result["status"] == "unavailable" and (result["value"] is not None or not reason):
                raise ValueError("unknown state must remain null")
            return dict(result)
        except Exception:
            return {"status": "unavailable", "value": None, "reason": "state_adapter_failed"}

    def _condition(self, expression: Any, *, empty_value: bool | None) -> dict[str, Any]:
        if not expression:
            return {"status": "available", "value": empty_value}
        if not isinstance(expression, str):
            return {"status": "unavailable", "value": None, "reason": "condition_shape_unsupported"}
        volatile = expression in self.volatile
        if self.engine is not None and not volatile:
            return self._engine_condition(expression)
        cached = self._cache.get(expression)
        if not volatile and cached and self.clock() - cached[0] <= self.cache_seconds:
            return dict(cached[1])
        adapter = self.conditions.get(expression)
        if adapter is None:
            result = {"status": "unavailable", "value": None, "reason": "condition_adapter_unavailable"}
        else:
            try:
                value = adapter()
                if type(value) is not bool:
                    raise ValueError("condition adapter did not return bool")
                result = {"status": "available", "value": value}
            except Exception:
                # Do not leak host command text or exception contents to clients.
                result = {"status": "unavailable", "value": None, "reason": "condition_adapter_failed"}
        if volatile:
            # Remember it, so the fast path can tell whether it has moved.
            self._volatile_values[expression] = result.get("value")
        else:
            if cached is None or cached[1] != result:
                self._dirty = True
            self._cache[expression] = (self.clock(), dict(result))
        return result

    def shell_expressions(self) -> frozenset[str]:
        """Every condition expression written in a menu source row.

        MENU-3. These, and only these, may be handed to bash: they are the text
        of Omarchy's default menu, the user's own extension file and Omodachi's
        menu, which Omarchy itself runs in bash as this same user. A provider
        row (an app, a font, a keybinding record) never contributes one.
        """
        stamp = id(self._source)
        if self._shell_expressions is None or self._shell_expressions[0] != stamp:
            found = set()
            for entry in self._source.entries:
                row = entry.as_dict()
                for field, _empty in CONDITION_FIELDS:
                    value = row.get(field)
                    if isinstance(value, str) and value:
                        found.add(value)
            self._shell_expressions = (stamp, frozenset(found))
        return self._shell_expressions[1]

    def _engine_condition(self, expression: str) -> dict[str, Any]:
        """A reading from the engine; never spawns a shell on the caller's thread.

        An adapter-backed expression that is due is read here, as it always was
        (a reviewed adapter is a cached lookup or one bounded subprocess). A
        shell expression that is due keeps answering with its last reading
        until `warm()` - on a worker - takes the next one; one never read at
        all is `unknown` until then, which draws the row and lets it be tapped.
        """
        engine = self.engine
        adapter = self.conditions.get(expression)
        if adapter is None and expression not in self.shell_expressions():
            return {"status": "unavailable", "value": None, "reason": "condition_adapter_unavailable"}
        engine.track(expression, shell=adapter is None)
        reading = engine.reading(expression)
        if reading is not None and not engine.is_due(expression):
            return reading
        if adapter is not None:
            if engine.evaluate({expression: adapter}, reason="adapter"):
                self._dirty = True
            return engine.reading(expression) or reading or unknown(UNKNOWN_PENDING)
        return reading if reading is not None else unknown(UNKNOWN_PENDING)

    def _warm_engine(self, rows: list[dict[str, Any]], *, shells: bool, reason: str) -> None:
        """Take every reading the engine says is owed, concurrently (≤ 8).

        `shells=False` is the event loop's refresh: it takes owed adapter
        readings and shell expressions that have never been read (the first
        build, a source edit) - never a re-read of a shell it already has an
        answer for; that is `warm()`'s, on a worker.
        """
        engine = self.engine
        sources = self.shell_expressions()
        jobs: dict[str, Any] = {}
        for row in rows:
            for field, _empty in CONDITION_FIELDS:
                expression = row.get(field)
                if not isinstance(expression, str) or not expression or expression in self.volatile:
                    continue
                adapter = self.conditions.get(expression)
                if adapter is None and expression not in sources:
                    continue
                engine.track(expression, shell=adapter is None)
                if not engine.is_due(expression) or expression in jobs:
                    continue
                if adapter is not None:
                    jobs[expression] = adapter
                elif shells or engine.reading(expression) is None:
                    jobs[expression] = None
            registration = self.checked_states.get(row.get("id"))
            if registration is not None:
                expected, reader = registration
                if any((row.get(key) or "") != value for key, value in expected.items()):
                    continue
                key = "state:" + row["id"]
                engine.track(key, checked_state_triggers(row["id"]))
                if engine.is_due(key):
                    jobs[key] = (lambda reader=reader: self._read_state(reader))
        if not shells:
            jobs = {key: job for key, job in jobs.items()
                    if job is not None or engine.reading(key) is None}
        # Only the owed ones whose settle time has passed (a pacman burst).
        ready = set(engine.due_keys(jobs))
        jobs = {key: job for key, job in jobs.items() if key in ready}
        if jobs and engine.evaluate(jobs, reason=reason):
            self._dirty = True

    def demand(self) -> list[str]:
        """A menu was opened: owe a reading for the demand-class conditions older than 10 s."""
        if self.engine is None:
            return []
        return self.engine.demand()

    def after_invoke(self, entry_id: str) -> list[str]:
        """An action ran: what it may have changed is read again, off the loop.

        The row itself, and the demand-class conditions of the rows in the same
        top-level group - setting the default browser moves seven checked
        marks under `setup`, starting a screen recording shows `Stop` under
        `trigger`. File- and package-class readings need nothing here: their
        own watches see the change.
        """
        if self.engine is None or self._snapshot is None:
            return []
        group = entry_id.split(".", 1)[0]
        keys: list[str] = []
        for row in self._snapshot["entries"]:
            own = row.get("id") == entry_id
            if not own and str(row.get("id", "")).split(".", 1)[0] != group:
                continue
            for field, _empty in CONDITION_FIELDS:
                expression = row.get(field)
                if not isinstance(expression, str) or not expression:
                    continue
                triggers = self.engine.triggers(expression)
                if own or (triggers is not None and triggers.demand):
                    keys.append(expression)
            if own:
                keys.append("state:" + entry_id)
        return self.engine.mark_due(keys)

    def _stale(self, cache: dict, key: Any) -> bool:
        cached = cache.get(key)
        return cached is None or self.clock() - cached[0] > self.cache_seconds

    def _warm(self, rows: list[dict[str, Any]], *, shells: bool = False, reason: str = "refresh") -> None:
        """Read the cold condition adapters concurrently, before the row walk.

        PERF-4. Every `when`/`checked` in the Omarchy menu is its own shell
        command - `omarchy-hw-webcam` is 84 ms, `omarchy-default-browser`
        109 ms, `omarchy-channel-current` 129 ms - and the walk below asked for
        them one after another, so a cold pass was the *sum* of two dozen
        subprocesses, about 0.8 s, on the thread that also answers every HTTP
        request. They do not depend on each other; nothing is written here but
        the caches the walk is about to read, keyed by the expression each
        thread was given. Providers stay serial: reading one of those
        re-registers host executors, and that is not work to run twice at once.
        """
        if self.engine is not None:
            self._warm_engine(rows, shells=shells, reason=reason)
            return
        jobs: list[Callable[[], Any]] = []
        queued: set[str] = set()
        for row in rows:
            when = row.get("when")
            if (isinstance(when, str) and when in self.conditions and when not in self.volatile
                    and when not in queued and self._stale(self._cache, when)):
                queued.add(when)
                jobs.append(lambda expression=when: self._condition(expression, empty_value=True))
            if row.get("id") in self.checked_states:
                if self._stale(self._checked_cache, row["id"]):
                    jobs.append(lambda value=row: self._checked(value))
                continue
            checked = row.get("checked")
            if (isinstance(checked, str) and checked in self.conditions and checked not in self.volatile
                    and checked not in queued and self._stale(self._cache, checked)):
                queued.add(checked)
                jobs.append(lambda expression=checked: self._condition(expression, empty_value=None))
        if len(jobs) < 2:
            for job in jobs:
                job()
            return
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
            for future in [pool.submit(job) for job in jobs]:
                # A reader that raised has already cached its own "unavailable";
                # this loop only waits, it never decides.
                try: future.result()
                except Exception: pass

    def _volatile_moved(self) -> bool:
        """Re-read the volatile conditions; True when one of them has changed."""
        moved = False
        for expression in self.volatile:
            adapter = self.conditions.get(expression)
            if adapter is None:
                continue
            try:
                value = adapter()
                value = value if type(value) is bool else None
            except Exception:
                value = None
            if self._volatile_values.get(expression, "unread") != value:
                self._volatile_values[expression] = value
                moved = True
        return moved

    def current_revision(self, *, allow_expired: bool = False) -> str | None:
        """The revision of the snapshot already on the shelf, if it still holds.

        `None` means "ask `refresh`". This exists so a caller that only needs
        to know *which* catalog is current does not pay for building it again.

        PERF-5. `allow_expired` skips only the cache window, never `_dirty` and
        never the volatile readings - so "something said it changed" still
        forces a rebuild, and what is waived is the *periodic* re-reading. That
        is for `invoke`: the maintenance tick takes those readings every two
        seconds anyway, and without this one tap in seven landed on the window
        boundary and paid for a cold pass the user had not asked for. It is the
        difference between a tap that is always fast and a tap that is usually
        fast.
        """
        if self._snapshot is None or self._dirty:
            return None
        # The fast path never outlives a cache window: the whole point of the
        # window is that a reading is eventually taken again.
        if not allow_expired and self.clock() - self._built_at > self.cache_seconds:
            return None
        if self._volatile_moved():
            self._dirty = True
            return None
        return self._snapshot["revision"]

    def warm(self) -> None:
        """Take the cold condition readings now, off whatever thread calls this.

        PERF-4. The daemon's maintenance tick calls this through
        `asyncio.to_thread` before it refreshes, so the shell-outs happen on a
        worker and the refresh that follows on the event loop finds them
        already cached. It writes nothing but this object's own caches - no hub
        state, no route registration - which is what makes it safe to run
        beside the loop.
        """
        rows = [entry.as_dict() for entry in self.catalog.entries]
        if self.engine is not None:
            # A source edit that removed a row takes its reading - and its
            # inotify watch - with it.
            self.engine.retain(self._engine_keys(rows))
        self._warm(rows, shells=True, reason="warm")

    def _engine_keys(self, rows: list[dict[str, Any]]) -> set[str]:
        keys = set(self.volatile) | {"state:" + entry_id for entry_id in self.checked_states}
        for row in rows:
            for field, _empty in CONDITION_FIELDS:
                value = row.get(field)
                if isinstance(value, str) and value:
                    keys.add(value)
        return keys

    def app_icon(self, app_id: Any) -> tuple[str, str] | None:
        """What the host's own app list publishes for a window's `app_id`.

        ICON-1. The bar's focused item is a window, and a window is not a menu
        row: all the compositor gives is the Wayland `app_id`. The desktop
        database already behind the Apps submenu is the only place on the host
        that knows which `.desktop` file — and so which `Icon=` — that string
        belongs to.

        It reads the snapshot that already exists and never builds one: focus
        changes every time Leo alt-tabs, and PERF-4's whole point was that a
        window event must not re-run the menu's conditions.
        """
        if not isinstance(app_id, str) or not app_id:
            return None
        wanted = app_id.casefold()
        for row in self._snapshot.get("entries", ()) or ():
            if row.get("kind") != "app":
                continue
            if str(row.get("appId", "")).casefold() == wanted:
                return str(row.get("icon", "") or ""), str(row.get("icon_kind", "none") or "none")
        return None

    def refresh(self, *, invalidate: bool = False) -> dict[str, Any]:
        if invalidate:
            self.invalidate()
        if self.current_revision() is not None:
            return copy.deepcopy(self._snapshot)
        rows = [entry.as_dict() for entry in self.catalog.entries]
        first = self._snapshot is None or (self.engine is not None and self.engine.passes == 0)
        self._warm(rows, reason="build" if first else "refresh")
        seen = {row["id"] for row in rows}
        dynamic_rows: list[dict[str, Any]] = []
        for row in rows:
            row["conditions"] = {
                "when": self._condition(row.get("when"), empty_value=True),
                "checked": self._checked(row),
            }
            if row.get("disabled"):
                row["conditions"]["disabled"] = self._condition(row["disabled"], empty_value=False)
            row["visible"] = row["conditions"]["when"]["value"]
            row["checked_state"] = row["conditions"]["checked"]["value"]
            provider = row.get("provider")
            if not provider:
                continue
            adapter = self.providers.get(provider) if isinstance(provider, str) else None
            if adapter is None:
                row["provider_state"] = {"status": "unavailable", "reason": "provider_adapter_unavailable"}
                continue
            try:
                incoming = self._provider_rows(provider, adapter)
                if not isinstance(incoming, list) or len(incoming) > 512:
                    raise CatalogError("provider must return a bounded row list")
                batch: list[dict[str, Any]] = []
                batch_seen = set(seen)
                for raw in incoming:
                    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
                        raise CatalogError("provider row requires id")
                    if any(c.isspace() for c in raw["id"]) or len(raw["id"]) > 256:
                        raise CatalogError("provider id is invalid")
                    for field in ("label", "action", "target", "when", "checked", "surface"):
                        if field in raw and not isinstance(raw[field], str):
                            raise CatalogError("provider field shape is invalid")
                    if len(json.dumps(raw, allow_nan=False)) > 32768:
                        raise CatalogError("provider row is too large")
                    if raw["id"] in batch_seen:
                        continue  # MenuModel does not let provider rows replace static rows.
                    normalized = _normalize_row(raw["id"], raw)
                    normalized["parent"] = raw.get("parent", row["id"])
                    normalized["parent_id"] = normalized["parent"]
                    normalized["providerMenu"] = row["id"]
                    normalized["conditions"] = {
                        "when": self._condition(normalized.get("when"), empty_value=True),
                        "checked": self._condition(normalized.get("checked"), empty_value=None),
                    }
                    normalized["visible"] = normalized["conditions"]["when"]["value"]
                    normalized["checked_state"] = normalized["conditions"]["checked"]["value"]
                    batch.append(normalized)
                    batch_seen.add(raw["id"])
                seen = batch_seen
                dynamic_rows.extend(batch)
                row["provider_state"] = {"status": "available", "row_count": len(batch)}
            except Exception:
                row["provider_state"] = {"status": "unavailable", "reason": "provider_adapter_failed"}
        rows.extend(dynamic_rows)
        for order, row in enumerate(rows):
            row["order"] = order
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:16]
        snapshot = {"revision": digest, "source_revision": self.catalog.revision, "entries": rows}
        self._dirty = False
        self._built_at = self.clock()
        if self._snapshot != snapshot:
            self._snapshot = copy.deepcopy(snapshot)
            if self.on_change:
                self.on_change(copy.deepcopy(snapshot))
        return copy.deepcopy(snapshot)
