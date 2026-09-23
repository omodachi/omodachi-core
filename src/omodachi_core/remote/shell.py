"""The Omarchy shell, for the length of a session that can break it.

Quickshell 0.3.1 dies when an output one of its own windows lives on is
removed: Qt 6.11's `QWaylandWindow::setGeometry` moves a toplevel to
`screen()->geometry().topLeft()` without checking that `screen()` is still
there, and a takeover removes exactly that output. Its crash handler reloads
the configuration inside the crashed process, so the shell answers `ping` again
while every IpcHandler is registered twice, the menu's Apps list is empty and
the polkit agent is unreachable. Only `omarchy-restart-shell` puts that right.

A session watches Quickshell's own crash directory - the one artefact it writes
for every crash, whether or not it managed to recover - and repairs the shell
once, so the user is not left to notice a half-dead desktop themselves.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

RESTART_COMMAND = "/usr/bin/omarchy-restart-shell"
CRASH_DIR = ".cache/quickshell/crashes"
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class OmarchyShell:
    def __init__(self, *, crash_dir=None, runner=None, environment=None, home=None):
        home = Path(home) if home is not None else Path.home()
        cache = os.environ.get("XDG_CACHE_HOME")
        default = (Path(cache) / "quickshell/crashes") if cache else (home / CRASH_DIR)
        self.crash_dir = Path(crash_dir) if crash_dir is not None else default
        self.runner = runner or self._run
        self.environment = environment

    def _env(self):
        from ..graphical import graphical_environment
        value = (self.environment or graphical_environment)()
        return {**value, "OMARCHY_PATH": "/usr/share/omarchy"}

    def _run(self):
        result = subprocess.run([RESTART_COMMAND], env=self._env(), stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=90, check=False)
        return result.returncode == 0

    def crashes(self) -> list[str]:
        """Quickshell's crash run ids, oldest first. A missing directory is none."""
        try:
            rows = [path.name for path in self.crash_dir.iterdir() if RUN_ID.fullmatch(path.name)]
        except OSError:
            return []
        return sorted(rows)[-256:]

    def restart(self) -> bool:
        """True when the shell was restarted. A locked session refuses, and says so."""
        try:
            return bool(self.runner())
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
