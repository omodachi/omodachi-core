# Soaking the daemon

`omodachid` holds a catalog, an event history and a subscription per paired
device, and it does so for weeks. None of that is exercised by
`python -m unittest discover -s tests`, which finishes in a hundred seconds —
and PERF-4 found the daemon on the real host at **12.0 GB resident with 11.0 GB
in swap**, its event loop at 99.4 % CPU and TLS handshakes timing out at eight
seconds, growing **1.34 MB/s** from a fresh start. A leak is a property of an
hour, not of a call, so it needs its own instrument.

## `scripts/soak_daemon.py`

```
.venv/bin/python scripts/soak_daemon.py --minutes 5
.venv/bin/python scripts/soak_daemon.py --minutes 30 --json /tmp/soak.json --verbose
```

It assembles a real `CoreService` behind a real `NetworkServer`, gives it a
host-sized catalog (500 provider rows and fourteen conditions, one of which
flaps so the catalog really does change), runs the daemon's own maintenance
cadence — the 0.5 s workspace publish and the 2 s source poll — and drives it
the way a paired iPad does: polling `/v1/state`, `/v1/catalog` and
`/v1/capabilities` twice a second and holding an event subscription open.

Every `--interval` seconds it records resident memory, open file descriptors,
live asyncio tasks and threads. At the end it prints a `tracemalloc` top-20 diff
and a `gc.get_objects()` type histogram diff between the first and the last
sample, and exits non-zero when

- resident memory grew by more than `--rss-budget` (default 20 MiB), or
- file descriptors or asyncio tasks trend upwards (slope > 0.05 per sample), or
- the client never got a reading, which would mean the soak proved nothing.

`--json` writes the samples and both diffs for a report.

## When to run it

- **Before a release**, for at least 30 minutes. It is not in the unit suite on
  purpose: five minutes does not belong in a run that takes a hundred seconds.
- **After anything that touches the hub, the catalog runtime or the event
  stream** — those are the three places that retain.
- In CI, as an optional manual job. `--minutes 5` is enough to catch a leak of
  the size PERF-4 found (1.34 MB/s is 400 MiB in five minutes); `--minutes 30`
  is what a release wants.

## On a real host

The daemon reads itself too. `omodachi_core/resources.py` publishes four
numbers, and they come out in two places:

- the journal, once a minute:
  `journalctl --user -u omodachid | grep resources`

  ```
  resources rss=118MB fds=14 tasks=7 threads=3 events=41 subscribers=1 uptime=1860s
  ```

  The line is logged at `warning` when a reading is past its threshold
  (600 MB resident, 256 descriptors, 64 tasks, 32 threads), and it names which
  one: `OVER=rss_bytes`.

- `GET /health`, unauthenticated, as `resources` — so a check can be run from
  another machine without pairing:

  ```
  curl -sk https://<host>:8099/health | python3 -m json.tool
  ```

A healthy daemon on `omarchy` sits at about 120 MB resident with a dozen
descriptors and single-digit tasks, and those numbers do not move with uptime.
If `rss_bytes` climbs monotonically over an hour, something is being retained;
start from the event history (`event_history`, `event_subscribers`) and from
whatever payload was last added to a published event.
