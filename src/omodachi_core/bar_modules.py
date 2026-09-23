"""Read-only status for the official bar modules core can answer for.

SPEC-F2 §2 drew the host's bar with the clock, the workspaces and the focused
window only: `state` had no field behind network, volume, battery or
notifications, and a self-drawn bar must not invent one. This module supplies
the three that have a cheap, official, read-only source, and nothing else.

| Module | Role | Source | Why this one |
| --- | --- | --- | --- |
| `omarchy.audio` | `audio` | `wpctl get-volume @DEFAULT_AUDIO_SINK@` | the shell's own audio widget drives WirePlumber; `wpctl` is its read-only half |
| `omarchy.power` | `power` | `/sys/class/power_supply/*` | `capacity` and `status` on the one battery, straight from the kernel |
| `omarchy.network` | `network` | `omarchy-network-status` | the exact command `plugins/panels/network/Panel.qml` samples |

Deliberately absent:

* **notifications** — the only unread count lives in the shell's in-memory
  popup model. `~/.local/state/omarchy/notifications/history/` is *recorded
  history*, which is a different number, and the `notifications` IPC target
  exposes the Do-Not-Disturb flag but needs a live `quickshell`, which is the
  dependency PERF-1 §6.3 removed from the Remote path. The bar widget on this
  host (`jankeesvw.notification-center`) is third-party besides.
* **bluetooth, weather, display, fans, keyboard layout, updates and every
  third-party widget** — no cheap read-only source, or not first-party.

Every reader fails to `None`. Unknown is never rendered as zero.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import time

# The roles a status can be published for. A role outside this set is layout,
# not telemetry.
STATUS_ROLES = ("audio", "power", "network")

_VOLUME = re.compile(r"^Volume:\s+([0-9]+(?:\.[0-9]+)?)(\s+\[MUTED\])?\s*$")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,31}")
_BATTERY_STATES = {"charging", "discharging", "full", "not_charging", "unknown"}


def _run(argv, timeout=1.0) -> str | None:
    """One fixed argv, bounded output, no stderr. Any trouble is `None`."""
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode or len(result.stdout) > 4096:
        return None
    try:
        return result.stdout.decode("utf-8")
    except UnicodeError:
        return None


class BarModules:
    """One throttled snapshot of every module status core can read.

    The daemon asks on its two-second source refresh; the throttle is the same
    two seconds, so a burst of callers cannot turn this into a poll loop.
    """

    def __init__(self, *, runner=_run, power_supply=Path("/sys/class/power_supply"),
                 clock=time.monotonic, throttle=2.0):
        self.runner = runner
        self.power_supply = Path(power_supply)
        self.clock = clock
        self.throttle = float(throttle)
        self._cached: dict[str, dict | None] = {role: None for role in STATUS_ROLES}
        self._read_at: float | None = None

    def snapshot(self) -> dict[str, dict | None]:
        now = self.clock()
        if self._read_at is not None and now - self._read_at < self.throttle:
            return dict(self._cached)
        self._read_at = now
        readers = {"audio": self._audio, "power": self._power, "network": self._network}
        value = {}
        for role, reader in readers.items():
            # One broken reader is one null module, never a broken publish: this
            # runs on the daemon's own two-second maintenance tick.
            try:
                value[role] = reader()
            except Exception:
                value[role] = None
        self._cached = value
        return dict(self._cached)

    # --- the three readers -------------------------------------------------

    def _audio(self):
        value = self.runner(("/usr/bin/wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"))
        match = _VOLUME.match((value or "").strip())
        if match is None:
            return None
        volume = float(match.group(1))
        if not 0.0 <= volume <= 10.0:
            return None
        return {"volume": round(volume, 3), "muted": bool(match.group(2))}

    def _power(self):
        """The one real battery. A host without one publishes nothing."""
        try:
            directories = sorted(self.power_supply.iterdir())
        except OSError:
            return None
        for directory in directories:
            try:
                if (directory / "type").read_text().strip() != "Battery":
                    continue
                if (directory / "present").exists() and (directory / "present").read_text().strip() == "0":
                    continue
                percent = int((directory / "capacity").read_text().strip())
                state = (directory / "status").read_text().strip().lower().replace(" ", "_")
            except (OSError, UnicodeError, ValueError):
                continue
            if not 0 <= percent <= 100 or state not in _BATTERY_STATES:
                continue
            return {"percent": percent, "state": state, "charging": state == "charging"}
        return None

    def _network(self):
        """`omarchy-network-status` prints `kind<TAB>name<TAB>signal<TAB>freq`."""
        value = self.runner(("/usr/bin/omarchy-network-status",))
        if not value:
            return None
        fields = value.splitlines()[0].split("\t")
        kind = fields[0].strip() if fields else ""
        if not _WORD.fullmatch(kind):
            return None
        name = fields[1].strip()[:64] if len(fields) > 1 else ""
        signal = None
        if len(fields) > 2 and fields[2].strip():
            try:
                signal = int(float(fields[2]))
            except ValueError:
                signal = None
            if signal is not None and not 0 <= signal <= 100:
                signal = None
        if any(ord(c) < 32 for c in name):
            return None
        return {"kind": kind, "name": name or None, "signal": signal}
