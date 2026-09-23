"""What the daemon is holding, so the next leak is noticed before it hurts.

PERF-4 §0. `omodachid` was found on the real host at 12.0 GB resident with
11.0 GB in swap and its event loop pinned at 99.4 % CPU, and nothing anywhere
had said so: the first symptom anyone saw was an iPad taking ten seconds to
switch a workspace. Four numbers would have said it on the first minute -
resident memory, open file descriptors, live asyncio tasks, retained events -
so the daemon now reads them for itself, writes them to the journal once a
minute, and answers with them on `/health`.

This is a reading, not a control: nothing here restarts, trims or throttles
anything. Every value degrades to `None` rather than raising, because a monitor
that can take a daemon down is worse than no monitor.
"""
from __future__ import annotations

import os
from pathlib import Path
import platform
import resource as _resource
import threading
import time

#: Above these, the minute's line is a warning rather than a note. They are
#: chosen from what a healthy daemon actually holds (about 120 MB resident, a
#: dozen descriptors, a handful of tasks), not from what a machine can bear.
RSS_WARN_BYTES = 600 * 1024 * 1024
OPEN_FILES_WARN = 256
TASKS_WARN = 64
THREADS_WARN = 32


def resident_bytes() -> int | None:
    """Resident set size of this process in bytes, or None where unknown."""
    try:
        if platform.system() == "Linux":
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
            return None
        # macOS reports the peak rather than the current size; it is still the
        # number that grows when something is retained.
        return int(_resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss)
    except (OSError, ValueError, IndexError):
        return None


def open_files() -> int | None:
    for directory in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(directory))
        except OSError:
            continue
    return None


def live_tasks() -> int | None:
    try:
        import asyncio
        return len(asyncio.all_tasks())
    except (RuntimeError, ImportError):
        return None


class ResourceMonitor:
    """The daemon's own reading of itself, for `/health` and for the journal."""

    def __init__(self, hub, *, clock=time.time, monotonic=time.monotonic):
        self.hub = hub
        self.clock = clock
        self.monotonic = monotonic
        self.started_at = clock()
        self._started = monotonic()

    def snapshot(self) -> dict:
        events = getattr(self.hub, "_events", ())
        subscribers = getattr(self.hub, "_subscribers", ())
        return {"started_at": round(self.started_at, 3),
                "uptime_seconds": round(self.monotonic() - self._started, 1),
                "rss_bytes": resident_bytes(),
                "open_files": open_files(),
                "asyncio_tasks": live_tasks(),
                "threads": threading.active_count(),
                "event_history": len(events),
                "event_subscribers": len(subscribers),
                "event_cursor": getattr(self.hub, "event_cursor", 0),
                # MENU-3: the menu's condition shells - the loop PERF-4 found
                # running every two seconds. Null on a host without the engine.
                "condition_shells_total": self._conditions("shells_total"),
                "condition_shells_5m": self._conditions("shells_recent")}

    def _conditions(self, name: str) -> int | None:
        engine = getattr(self.hub, "condition_engine", None)
        if engine is None:
            return None
        try:
            value = getattr(engine, name)
            value = value() if callable(value) else value
            return value if type(value) is int else None
        except Exception:
            return None

    def warnings(self, reading: dict | None = None) -> list[str]:
        """The names of the readings that are past their threshold."""
        reading = self.snapshot() if reading is None else reading
        over = []
        for key, limit in (("rss_bytes", RSS_WARN_BYTES), ("open_files", OPEN_FILES_WARN),
                           ("asyncio_tasks", TASKS_WARN), ("threads", THREADS_WARN)):
            value = reading.get(key)
            if isinstance(value, int) and value > limit:
                over.append(key)
        return over

    def line(self, reading: dict | None = None) -> str:
        """One journal line. `resources` is the word to grep for."""
        reading = self.snapshot() if reading is None else reading
        megabytes = "?" if reading["rss_bytes"] is None else f"{reading['rss_bytes'] / 1048576:.0f}"
        over = self.warnings(reading)
        shells = ("" if reading.get("condition_shells_5m") is None else
                  " shells_5m={} shells_total={}".format(reading["condition_shells_5m"],
                                                        reading.get("condition_shells_total")))
        return ("resources rss={}MB fds={} tasks={} threads={} events={} subscribers={} uptime={}s{}{}"
                .format(megabytes, reading["open_files"], reading["asyncio_tasks"],
                        reading["threads"], reading["event_history"],
                        reading["event_subscribers"], int(reading["uptime_seconds"]), shells,
                        " OVER=" + ",".join(over) if over else ""))
