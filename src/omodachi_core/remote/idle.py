"""The host's screensaver and lock timer, for the length of a session.

A Remote session that is being watched from an iPad looks idle to the host: no
key, no pointer, no wake. Omarchy would blank and then lock the machine
underneath the stream. So a session turns the idle cycle off while it lives.

A user who had already turned it off keeps it off: the pre-session answer goes
into the journal and only a `true` is undone. `omarchy-shell` is an Omarchy
component that may not be installed, and a missing one is not an error.
"""
from __future__ import annotations

import json
import subprocess
import time

COMMAND = "/usr/bin/omarchy-shell"
# A shell that has just been restarted answers `ping` before its idle plugin
# is loaded, and HOST-1's crash fallback restarts it mid-session - so the
# restore that follows can arrive in exactly that window. Two more tries, a
# second apart, is the difference between a session that ends cleanly and one
# that ends `failed` with a journal nobody needs.
SET_ATTEMPTS = 3
SET_RETRY_SECONDS = 1.0


class OmarchyIdle:
    def __init__(self, *, runner=None, environment=None, sleep=time.sleep):
        self.runner = runner or self._run
        self.environment = environment
        self.sleep = sleep

    def _env(self):
        from ..graphical import graphical_environment
        value = (self.environment or graphical_environment)()
        return {**value, "OMARCHY_PATH": "/usr/share/omarchy"}

    def _run(self, method):
        result = subprocess.run([COMMAND, "idle", method], env=self._env(), stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=5, check=False)
        if result.returncode:
            raise ValueError("omarchy_idle_unavailable")
        return result.stdout

    def enabled(self):
        """True, False, or None when the host cannot answer."""
        try:
            value = json.loads(self.runner("status"))
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            return None
        return value.get("enabled") if isinstance(value, dict) and type(value.get("enabled")) is bool else None

    def set(self, enabled: bool) -> None:
        wanted = "enabled" if enabled else "disabled"
        for attempt in range(SET_ATTEMPTS):
            try:
                if self.runner("enable" if enabled else "disable").strip() == wanted:
                    return
                error = ValueError("omarchy_idle_unconfirmed")
            except (OSError, ValueError, TypeError, subprocess.SubprocessError) as raised:
                error = raised
            if attempt == SET_ATTEMPTS - 1:
                raise error
            self.sleep(SET_RETRY_SECONDS)
