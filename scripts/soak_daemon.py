#!/usr/bin/env python3
"""Run the daemon against a fake host and watch what it keeps.

PERF-4 §0. `omodachid` was found on the real host at 12.0 GB resident with
11.0 GB in swap, its event-loop thread at 99.4 % CPU and TLS handshakes timing
out at eight seconds, growing 1.34 MB/s from a fresh start. Nothing in the unit
suite could have caught that: a leak is a property of an hour, not of a call.

This is the thing that can. It assembles a real `CoreService` over a real
`NetworkServer`, gives it a host-sized catalog and a condition that flaps, runs
the daemon's own maintenance cadence, and drives it the way a paired iPad does
- polling the state, re-reading the catalog, holding an event subscription
open. Every `--interval` seconds it records resident memory, open file
descriptors, live asyncio tasks and threads; at the end it prints a tracemalloc
top-20 diff and a `gc.get_objects()` type histogram diff between the first and
the last sample, and fails if memory grew past the budget or the descriptors or
tasks trended upwards.

    .venv/bin/python scripts/soak_daemon.py --minutes 5
    .venv/bin/python scripts/soak_daemon.py --minutes 1 --rss-budget 20 --json out.json

It is deliberately not part of `unittest discover`: five minutes does not
belong in a suite that runs in a hundred seconds. `docs/soak.md` says when to
run it.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import gc
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omodachi_core.bootstrap import create_service            # noqa: E402
from omodachi_core.hub import Hub                             # noqa: E402
from omodachi_core.network import NetworkServer               # noqa: E402


# ---------------------------------------------------------------- readings

def resident_bytes() -> int:
    """Resident set size of this process, in bytes, or 0 where unknown."""
    try:
        if platform.system() == "Linux":
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
            return 0
        out = subprocess.run(("/bin/ps", "-o", "rss=", "-p", str(os.getpid())),
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return int(out) * 1024 if out else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def open_descriptors() -> int:
    for directory in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(directory))
        except OSError:
            continue
    return 0


def sample(started: float) -> dict:
    try:
        tasks = len(asyncio.all_tasks())
    except RuntimeError:
        tasks = 0
    return {"seconds": round(time.monotonic() - started, 1),
            "rss_mib": round(resident_bytes() / 1048576, 1),
            "fds": open_descriptors(),
            "tasks": tasks,
            "threads": threading.active_count()}


def histogram() -> collections.Counter:
    return collections.Counter(type(value).__name__ for value in gc.get_objects())


def trend(values: list[int | float]) -> float:
    """Least-squares slope per sample. Flat is ~0; a leak is positive."""
    count = len(values)
    if count < 3:
        return 0.0
    mean_x = (count - 1) / 2
    mean_y = sum(values) / count
    spread = sum((index - mean_x) ** 2 for index in range(count))
    if not spread:
        return 0.0
    return sum((index - mean_x) * (value - mean_y) for index, value in enumerate(values)) / spread


# ---------------------------------------------------------------- the host

class FakeHost:
    """A host-sized catalog whose readings move, without touching a machine."""

    def __init__(self, rows: int = 500, conditions: int = 14):
        self.rows = rows
        self.conditions = conditions
        self.reads = 0
        self.flip = False
        self.tick = 0

    def provider(self) -> list[dict]:
        self.reads += 1
        # Row zero's visibility rides the flapping condition, so the published
        # catalog really does change - a soak whose catalog never changes never
        # publishes `catalog.changed` and would have found nothing.
        return [{"id": f"apps.row-{index:04d}", "parent": "apps", "kind": "app",
                 "label": f"Application {index:04d}", "icon": "",
                 "action": "", "target": "", "provider": "",
                 "when": "fixture.condition.0" if index == 0 else "", "checked": ""}
                for index in range(self.rows)]

    def install(self, service) -> None:
        service.runtime.register_provider("fixture.apps", self.provider)
        for index in range(self.conditions):
            # The real host's `when` expressions are shell commands; here they
            # only have to be *readings*, and one of them has to move, because
            # a catalog that never changes never publishes an event and a soak
            # that never publishes an event proves nothing.
            service.runtime.register_condition(
                f"fixture.condition.{index}",
                (lambda self=self: self.toggle()) if index == 0 else (lambda: True))

    def toggle(self) -> bool:
        self.flip = not self.flip
        return self.flip

    def publish_workspace(self, service) -> None:
        """What `HyprlandWorkspaceAdapter.publish` does, twice a second.

        This is the cadence that mattered on the real host: the compositor
        probe runs at 2 Hz, and on `main` every reading that differed threw
        away the whole runtime and rebuilt the catalog, which published a
        `catalog.changed` carrying all of its rows.
        """
        self.tick += 1
        counts = {number: (self.tick + number) % 3 for number in range(1, 11)}
        service.set_workspace_snapshot(active=1 + self.tick % 5, window_counts=counts,
                                       focused_window={"id": f"0x{self.tick:08x}",
                                                       "app_id": "fixture.terminal",
                                                       "app_name": "Terminal"})


# ---------------------------------------------------------------- the client

async def poll_client(url: str, token: str, stop: asyncio.Event, counters: dict) -> None:
    import aiohttp
    headers = {"Authorization": "Bearer " + token}
    async with aiohttp.ClientSession(headers=headers) as session:
        while not stop.is_set():
            for path in ("/v1/state", "/v1/catalog", "/v1/capabilities"):
                if stop.is_set():
                    break
                try:
                    async with session.get(url + path) as response:
                        await response.read()
                        counters["reads"] += 1
                except Exception:
                    counters["read_failures"] += 1
            await asyncio.sleep(0.5)


async def event_client(url: str, token: str, stop: asyncio.Event, counters: dict) -> None:
    import aiohttp
    headers = {"Authorization": "Bearer " + token}
    while not stop.is_set():
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.ws_connect(url + "/v1/events") as socket:
                    while not stop.is_set():
                        message = await asyncio.wait_for(socket.receive(), timeout=5)
                        if message.type is aiohttp.WSMsgType.TEXT:
                            counters["events"] += 1
                        elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
        except asyncio.TimeoutError:
            continue
        except Exception:
            counters["event_failures"] += 1
            await asyncio.sleep(0.5)


# ---------------------------------------------------------------- the soak

async def soak(options) -> int:
    tracemalloc.start(8)
    hub = Hub()
    service = create_service(hub, demo=True)
    host = FakeHost(rows=options.rows)
    host.install(service)
    service.refresh_catalog(invalidate=True)
    token = hub.register_device("soak-client")
    server = NetworkServer(service, allow_loopback_http=True)
    port = await server.start()
    url = f"http://127.0.0.1:{port}"

    stop = asyncio.Event()
    counters = {"reads": 0, "read_failures": 0, "events": 0, "event_failures": 0}
    clients = [asyncio.create_task(poll_client(url, token, stop, counters)),
               asyncio.create_task(event_client(url, token, stop, counters))]

    async def maintain():
        """The daemon's own cadence, from cli.py."""
        ticks = 0
        while not stop.is_set():
            await asyncio.sleep(0.25)
            hub.tick()
            ticks += 1
            if ticks % 2 == 0:
                host.publish_workspace(service)
            if ticks % 8 == 0:
                # `warm` exists from PERF-4 onward; this script is also run
                # against an older build for comparison, and a soak that dies
                # on an AttributeError measures nothing.
                warm = getattr(service, "warm_readings", None) or getattr(service.runtime, "warm", None)
                if warm is not None:
                    await asyncio.to_thread(warm)
                service.refresh_catalog()
    maintenance = asyncio.create_task(maintain())

    started = time.monotonic()
    deadline = started + options.minutes * 60
    # One sample and one snapshot to compare everything against, taken after
    # the first reads so that import and first-refresh allocations are not
    # counted as growth.
    await asyncio.sleep(min(5.0, options.minutes * 60 / 4))
    gc.collect()
    first_snapshot = tracemalloc.take_snapshot()
    first_histogram = histogram()
    samples = [sample(started)]
    print(f"soak: {options.minutes} min, {options.rows} catalog rows, sampling every "
          f"{options.interval}s — first {samples[0]}", flush=True)

    while time.monotonic() < deadline:
        await asyncio.sleep(options.interval)
        samples.append(sample(started))
        if options.verbose:
            print("  ", samples[-1], flush=True)

    stop.set()
    maintenance.cancel()
    for task in clients:
        task.cancel()
    await asyncio.gather(maintenance, *clients, return_exceptions=True)
    await server.close()
    await service.close_media()
    gc.collect()
    last_snapshot = tracemalloc.take_snapshot()
    last_histogram = histogram()
    samples.append(sample(started))

    rss = [row["rss_mib"] for row in samples]
    fds = [row["fds"] for row in samples]
    tasks = [row["tasks"] for row in samples]
    growth = rss[-1] - rss[0]
    report = {
        "minutes": options.minutes, "rows": options.rows, "samples": samples,
        "counters": counters, "provider_reads": host.reads,
        "rss_first_mib": rss[0], "rss_last_mib": rss[-1], "rss_growth_mib": round(growth, 1),
        "rss_slope_mib_per_sample": round(trend(rss), 4),
        "fd_first": fds[0], "fd_last": fds[-1], "fd_slope": round(trend(fds), 4),
        "task_first": tasks[0], "task_last": tasks[-1], "task_slope": round(trend(tasks), 4),
        "top_allocations": [str(stat) for stat in last_snapshot.compare_to(first_snapshot, "lineno")[:20]],
        "type_histogram_diff": [(name, count) for name, count in
                                (last_histogram - first_histogram).most_common(20)],
    }

    print("\n--- resident memory ---")
    print(f"  first {rss[0]:.1f} MiB   last {rss[-1]:.1f} MiB   growth {growth:+.1f} MiB   "
          f"slope {report['rss_slope_mib_per_sample']:+.4f} MiB/sample")
    print(f"  file descriptors {fds[0]} -> {fds[-1]} (slope {report['fd_slope']:+.4f})")
    print(f"  asyncio tasks    {tasks[0]} -> {tasks[-1]} (slope {report['task_slope']:+.4f})")
    print(f"  client reads {counters['reads']}, events {counters['events']}, "
          f"provider reads {host.reads}")
    print("\n--- tracemalloc top 20 (last vs first) ---")
    for line in report["top_allocations"]:
        print("  ", line)
    print("\n--- gc type histogram, top 20 growers ---")
    for name, count in report["type_histogram_diff"]:
        print(f"   {count:+8d}  {name}")

    if options.json:
        Path(options.json).write_text(json.dumps(report, indent=1))
        print(f"\nwrote {options.json}")

    failures = []
    if growth > options.rss_budget:
        failures.append(f"resident memory grew {growth:.1f} MiB, budget {options.rss_budget} MiB")
    if report["fd_slope"] > 0.05:
        failures.append(f"file descriptors trending up ({report['fd_slope']:+.3f} per sample)")
    if report["task_slope"] > 0.05:
        failures.append(f"asyncio tasks trending up ({report['task_slope']:+.3f} per sample)")
    if counters["reads"] < 10:
        failures.append("the client never got a reading; this soak proved nothing")
    if not failures:
        print("\nsoak: OK")
        return 0
    for line in failures:
        print(f"\nsoak: FAILED — {line}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=float, default=5.0)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--rows", type=int, default=500, help="catalog rows the fake host publishes")
    parser.add_argument("--rss-budget", type=float, default=20.0, help="MiB of growth allowed")
    parser.add_argument("--json", help="write the full report here")
    parser.add_argument("--verbose", action="store_true", help="print every sample")
    return asyncio.run(soak(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
