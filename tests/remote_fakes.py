"""Synthetic host for the Remote tests: a compositor, the fork and WayVNC.

These are the simplified descendants of the RES-01 and host-provider doubles.
They model what the real host actually does — named headless outputs, whole
workspace migration, monitor disable/restore, the fork's lease/identity echo
and WayVNC's control queries — and nothing else. No real process, frame,
certificate or Hyprland instance is involved.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from omodachi_core.remote.errors import RemoteError
from omodachi_core.remote.hyprland import (ACTIVE_WORKSPACE_LUA, CommandResult, DPMS_WAKE_OPTIONS,
                                           SINGLE_WINDOW_ASPECT, WORKSPACES_LUA)
from omodachi_core.remote.hyprland import OWNED_NAME as OWNED
from omodachi_core.remote.shell import OmarchyShell

INSTANCE = "efb50993780079460b0cbed1363e2166a2de1d9f_1789614994_877726156"


def monitor(name="eDP-1", monitor_id=0, width=3072, height=1920, scale=2.0, x=0, y=0,
            refresh=60.0, transform=0, disabled=False):
    return {"name": name, "id": monitor_id, "width": width, "height": height, "scale": scale,
            "refreshRate": refresh, "x": x, "y": y, "transform": transform,
            "dpmsStatus": True, "disabled": disabled}


class FakeCompositor:
    """One physical output plus whatever headless outputs the manager creates."""

    def __init__(self, *, auto_layout=False, drop_empty_on_move=False, log=None, dpms=True):
        self.rows = [monitor()]
        self.workspaces = [[1, 0, 0], [2, 0, 1], [4, 0, 1]]
        self.active = 2
        # The real host's names, including the fork's uinput passthrough pair.
        self.devices = {"mice": [{"name": "apple-inc.-apple-internal-keyboard-/-trackpad-1"},
                                 {"name": "mouse-passthrough"}, {"name": "mouse-passthrough-(absolute)"}],
                        "keyboards": [{"name": "apple-inc.-apple-internal-keyboard-/-trackpad"},
                                      {"name": "keyboard-passthrough"},
                                      {"name": "hl-virtual-keyboard-fcitx5"}], "touch": [], "tablets": []}
        self.disabled_devices: list[str] = []
        # A host that will not let go of an owned output: a failed restore, a
        # daemon killed mid-release, a compositor that ignored the removal.
        # REMOTE-6 §3 uses it to prove the next session is planned anyway.
        self.fail_removal = False
        # `device[name]:output`. Hyprland keeps it by name, whether or not the
        # device exists yet, and an empty value means "the whole layout again".
        self.device_outputs: dict[str, str] = {}
        # Leo's host has the Omarchy one-window-ratio toggle on (PERF-2 §8.1).
        self.single_window_aspect = [1.0, 1.0]
        self.single_window_aspect_set = True
        # Omarchy's own input config turns both wake options on (HOST-2), and
        # Hyprland then lights every screen for any input event, including the
        # events a takeover forwards from the client.
        self.wake_options = {name: True for name in DPMS_WAKE_OPTIONS}
        self.wake_options_set = {name: True for name in DPMS_WAKE_OPTIONS}
        self.calls: list[tuple] = []
        # HOST-1, as a model of the real host: this Hyprland has the DPMS
        # dispatcher, and taking a *physical* output out of the layout kills
        # the desktop shell. Removing a headless one does not - measured, 10/10
        # either way.
        self.dpms = dpms
        self.shell_crashes: list[str] = []
        self.log = log if log is not None else []
        self.auto_layout = auto_layout
        # Hyprland can destroy an empty workspace outright when it is moved off
        # a monitor; a workspace with windows always survives the move.
        self.drop_empty_on_move = drop_empty_on_move
        self.fail = None  # a substring of the command tail that should fail
        # What `mode="preferred"` resolves to on a headless output: the
        # compositor's own default, not whatever the session asked for
        # (PERF-2 §1.2 measured 1920x1080 on the real host).
        self.headless_preferred = (1920, 1080)
        # Writes to the owned output that the compositor will swallow before it
        # starts obeying them again, modelling a `hyprctl reload` still in
        # flight: the write is accepted and lost.
        self.swallow_owned_writes = 0

    # --- helpers -----------------------------------------------------------

    def row(self, name):
        return next((row for row in self.rows if row["name"] == name), None)

    def names(self):
        return [row["name"] for row in self.rows]

    def _logical(self, row):
        return round(row["width"] / row["scale"]) if row["scale"] else 0

    def wake_input(self, kind="mouse"):
        """One forwarded pointer move or key press, as Hyprland treats it.

        With the matching wake option on, a DPMS-off screen comes back; with it
        off nothing happens. Measured on 0.56.2, this is per-event and applies
        to every monitor, not only the focused one.
        """
        option = "misc:mouse_move_enables_dpms" if kind == "mouse" else "misc:key_press_enables_dpms"
        if not self.wake_options[option]:
            return []
        woken = [row["name"] for row in self.rows if not row["dpmsStatus"] and not row["disabled"]]
        for row in self.rows:
            if not row["disabled"]:
                row["dpmsStatus"] = True
        return woken

    def reload(self, *, scale=1.6):
        """`hyprctl reload`, as this host's own configuration makes it behave.

        Hyprland re-sources `~/.config/hypr/monitors.lua`, whose single
        catch-all rule - `hl.monitor({output="", mode="preferred",
        position="auto", scale=<omarchy_monitor_scale>})` - names no output and
        therefore lands on *every* one, a running session's headless output
        included. It also re-sources the Omarchy input defaults, which turn
        both DPMS wake options back on, and the toggle that puts
        `layout:single_window_aspect_ratio` back. The host's own monitor
        watcher issues this on every output change, so at least once per
        session, and the Display panel's SCALE button writes the new number
        into that file, which is what makes the next one land differently.
        """
        self.log.append(("reload", scale))
        position = 0
        for row in sorted(self.rows, key=lambda value: (value["x"], value["name"])):
            if row["disabled"]:
                continue
            if OWNED.fullmatch(row["name"]):
                row.update(width=self.headless_preferred[0], height=self.headless_preferred[1])
            row["scale"] = scale
            row.update(x=position, y=0)
            position += self._logical(row)
        for name in DPMS_WAKE_OPTIONS:
            self.wake_options[name] = True
            self.wake_options_set[name] = True
        self.single_window_aspect = [1.0, 1.0]
        self.single_window_aspect_set = True

    def _relayout(self, explicit):
        """Auto-positioned monitors are re-packed after an explicit one."""
        if not self.auto_layout:
            return
        edge = explicit["x"] + self._logical(explicit)
        for row in self.rows:
            if row is not explicit and not row["disabled"] and row["x"] < edge:
                row["x"] = edge

    # --- the hyprctl surface ----------------------------------------------

    def __call__(self, argv):
        tail = argv[3:]
        self.calls.append(tail)
        self.log.append(("hyprctl", tail))
        if self.fail and any(self.fail in str(value) for value in tail):
            return CommandResult(1, error="display_command_failed")
        if tail == ("version",):
            return CommandResult(0, "Hyprland 0.56.2 built from branch v0.56.2 at commit deadbeef clean")
        if tail == ("-j", "monitors", "all"):
            return CommandResult(0, json.dumps(self.rows))
        if tail == ("-j", "getoption", SINGLE_WINDOW_ASPECT):
            return CommandResult(0, json.dumps({"option": SINGLE_WINDOW_ASPECT,
                                                "vec2": self.single_window_aspect,
                                                "set": self.single_window_aspect_set}))
        if tail[:2] == ("-j", "getoption") and tail[2:] and tail[2] in self.wake_options:
            return CommandResult(0, json.dumps({"option": tail[2], "bool": self.wake_options[tail[2]],
                                                "set": self.wake_options_set[tail[2]]}))
        if tail == ("-j", "devices"):
            return CommandResult(0, json.dumps(self.devices))
        if tail == ("repl", WORKSPACES_LUA):
            return CommandResult(0, json.dumps(self.workspaces))
        if tail == ("repl", ACTIVE_WORKSPACE_LUA):
            row = next((w for w in self.workspaces if w[0] == self.active), None)
            return CommandResult(0, json.dumps(row[:2] if row else None))
        if tail[:3] == ("output", "create", "headless"):
            self.rows.append(monitor(tail[3], len(self.rows), 1280, 720, 1.0, x=0))
            self.workspaces.append([10 + len(self.rows), len(self.rows) - 1, 0])
            return CommandResult(0, "ok")
        if tail[:2] == ("output", "remove"):
            owned = self.row(tail[2])
            if owned is None or self.fail_removal:
                return CommandResult(1, error="display_mutation_unconfirmed")
            fallback = next((row for row in self.rows if row is not owned and not row["disabled"]), None)
            for workspace in list(self.workspaces):
                if workspace[1] == owned["id"]:
                    if workspace[2] and fallback is not None:
                        workspace[1] = fallback["id"]
                    else:
                        self.workspaces.remove(workspace)
            self.rows.remove(owned)
            return CommandResult(0, "ok")
        if tail[0] == "eval":
            return self._eval(tail[1])
        raise AssertionError(tail)

    def _eval(self, lua):
        for name, monitor_id, width, height, scale, x, y in re.findall(
                r'hl\.get_monitor\("([^"]+)"\); if not m\d+ or m\d+\.id~=(-?\d+) or m\d+\.width~=(\d+)'
                r' or m\d+\.height~=(\d+) or math\.abs\(m\d+\.scale-([0-9.]+)\)>0\.000001'
                r' or m\d+\.x~=(-?\d+) or m\d+\.y~=(-?\d+)', lua):
            row = self.row(name)
            if row is None or (row["id"], row["width"], row["height"], row["x"], row["y"]) != (
                    int(monitor_id), int(width), int(height), int(x), int(y)) or abs(row["scale"] - float(scale)) > 1e-6:
                return CommandResult(1, error="omodachi_output_conflict")
        if "hl.monitor({" in lua:
            name = re.search(r'hl\.monitor\(\{ output="([^"]+)"', lua)[1]
            row = self.row(name)
            if row is None:
                return CommandResult(1, error="display_mutation_unconfirmed")
            mode = re.search(r'mode="(\d+)x(\d+)@([0-9.]+)"', lua)
            scale = re.search(r"scale=([0-9.]+)", lua)
            position = re.search(r'position="(-?\d+)x(-?\d+)"', lua)
            transform = re.search(r"transform=(\d+)", lua)
            disabled = re.search(r"disabled=(true|false)", lua)
            if mode:
                row.update(width=int(mode[1]), height=int(mode[2]), refreshRate=float(mode[3]))
            if scale:
                row["scale"] = float(scale[1])
            if position:
                row.update(x=int(position[1]), y=int(position[2]))
            if transform:
                row["transform"] = int(transform[1])
            if disabled:
                if disabled[1] == "true" and not row["disabled"]:
                    self.shell_crashes.append(name)
                row["disabled"] = disabled[1] == "true"
            if not row["disabled"]:
                self._relayout(row)
            if OWNED.fullmatch(name) and self.swallow_owned_writes:
                # A reload landing on top of this write: accepted, then lost.
                self.swallow_owned_writes -= 1
                row.update(width=self.headless_preferred[0], height=self.headless_preferred[1],
                           scale=1.6, x=0, y=0)
        dpms = re.search(r'hl\.get_monitor\("([^"]+)"\).*hl\.dsp\.dpms\(\{monitor=m\}\)', lua, re.S)
        if dpms:
            if not self.dpms:
                return CommandResult(1, error="display_command_failed")
            row = self.row(dpms[1])
            if row is None:
                return CommandResult(1, error="display_command_failed")
            # Hyprland 0.56's dispatcher toggles; it does not take a state.
            row["dpmsStatus"] = not row["dpmsStatus"]
        if "hl.device({" in lua:
            name = re.search(r'hl\.device\(\{ name="([^"]+)"', lua)[1]
            output = re.search(r'output="([^"]*)"', lua)
            if output is not None:
                if output[1]:
                    self.device_outputs[name] = output[1]
                else:
                    self.device_outputs.pop(name, None)
            elif "enabled=false" in lua:
                self.disabled_devices.append(name)
            elif name in self.disabled_devices:
                self.disabled_devices.remove(name)
        for name, (section, key) in DPMS_WAKE_OPTIONS.items():
            wake = re.search(r"hl\.config\(\{ %s = \{ %s = (true|false) \} \}\)" % (section, key), lua)
            if wake:
                self.wake_options[name] = wake[1] == "true"
                self.wake_options_set[name] = True
        aspect = re.search(r"single_window_aspect_ratio = \{ ([0-9.]+), ([0-9.]+) \}", lua)
        if aspect:
            self.single_window_aspect = [float(aspect[1]), float(aspect[2])]
            self.single_window_aspect_set = True
        if "hl.dsp.workspace.move" in lua:
            destination = re.search(r'monitor="([^"]+)"', lua)[1]
            target = self.row(destination)
            for value in re.findall(r"local w=hl\.get_workspace\((-?\d+)\)", lua):
                for workspace in list(self.workspaces):
                    if workspace[0] == int(value):
                        if self.drop_empty_on_move and not workspace[2]:
                            self.workspaces.remove(workspace)
                        else:
                            workspace[1] = target["id"]
        for value in re.findall(r"local f=hl\.get_workspace\((-?\d+)\)", lua):
            self.active = int(value)
        return CommandResult(0, "ok")


class FakeSunshine:
    """The fork's control endpoint: lease reservation and the stop fence."""

    def __init__(self):
        self.lease = None
        self.identity = None
        self.output = None
        self.count = 0
        self.calls: list[str] = []
        self.available = True
        self.busy = False
        self.fail_stop = False
        # STREAM-1. `None` is today's fork: no `encoders` field, and a profile
        # whose codec is not H.264 is refused at `desktop.prepare`, as
        # `managed_desktop_geometry.h::valid_profile` does.
        self.encoders = None
        self.prepared_profiles: list[dict] = []

    def request(self, op, **fields):
        self.calls.append(op)
        if op == "desktop.status":
            value = {"available": self.available, "backend": "wlr", "encoder": "vaapi"}
            if self.encoders is not None:
                value["encoders"] = list(self.encoders)
            return value
        lease = fields["lease"]
        if set(lease) != {"lease_id", "lease_epoch", "owner_device_id", "client_cert_sha256"}:
            raise RemoteError("binding_mismatch")
        if op == "desktop.claim":
            if self.busy or (self.lease is not None and lease != self.lease):
                raise RemoteError("desktop_busy")
            self.lease, self.output = lease, fields["output_id"]
        if self.lease != lease:
            raise RemoteError("binding_mismatch")
        result = {"lease": self.lease, "identity": self.identity, "session_count": self.count,
                  "stopped": self.count == 0, "configured_output_id": self.output,
                  "prepared": self.identity is not None}
        if op == "desktop.prepare":
            identity = fields["identity"]
            if set(identity) != {"lease_id", "lease_epoch", "transition_id", "geometry_epoch", "connection_generation"}:
                raise RemoteError("binding_mismatch")
            if self.identity is not None and identity["geometry_epoch"] <= self.identity["geometry_epoch"]:
                raise RemoteError("stale_generation")
            if fields["profile"]["codec"] not in {"h264", *(self.encoders or ())}:
                raise RemoteError("capture_unavailable")
            self.prepared_profiles.append(fields["profile"])
            self.identity = identity
            result.update(identity=identity, prepared=True, session_count=0,
                          configured_output_id=fields["output_id"])
        if op == "desktop.stop":
            if self.fail_stop:
                return {**result, "stopped": False, "session_count": 1}
            self.count = 0
            result.update(stopped=True, session_count=0)
        if op == "desktop.release":
            result["released"] = self.count == 0
            if result["released"]:
                self.lease = self.identity = self.output = None
        return result

    def start(self):
        self.count = 1


class FakeWayVNC:
    """One owned instance per session ID, as ManagedWayVNC would be."""

    instances: dict[str, "FakeWayVNC"] = {}

    def __init__(self, session_id="probe", *, available=True):
        self.session_id, self._available = session_id, available
        self.running = False
        self.output = None
        self.port = 5901
        # What the backend asked WayVNC to serve. The real instance keeps both:
        # the output's buffer pixels and the compositor's logical size, which
        # are two different numbers on a scale-2 output (REMOTE-6).
        self.pixels = None
        self.logical_size = None
        self.settled = False

    @classmethod
    def factory(cls, available=True):
        cls.instances = {}
        def build(session_id):
            if session_id not in cls.instances:
                cls.instances[session_id] = cls(session_id, available=available)
            return cls.instances[session_id]
        return build

    def available(self):
        return self._available

    def start(self, output, pixels, logical_size=None):
        self.running, self.output = True, output
        self.pixels, self.logical_size = dict(pixels), dict(logical_size) if logical_size else None
        return {"backend": "vnc", "transport": "ssh-forward", "host": "127.0.0.1", "port": self.port,
                "output_id": output, "framebuffer_pixels": pixels}

    def settle(self, timeout=4.0):
        """The real instance takes WayVNC's mid-stream resize here (REMOTE-6).

        The fake has no WayVNC to settle, so it answers what a settled one
        would serve: the output's buffer pixels.
        """
        self.settled = True
        return dict(self.pixels) if self.pixels else None

    def stop(self):
        self.running = False
        return True


class FakeBar:
    """The official Omarchy bar position, as OfficialBarPosition drives it."""

    def __init__(self, position="top"):
        self.position = position
        self.calls: list[str] = []

    def read(self):
        return self.position

    def set(self, value):
        self.position = value
        self.calls.append(value)


class FakeIdle:
    """omarchy-shell idle, as OmarchyIdle drives it."""

    def __init__(self, enabled=True, *, available=True, log=None):
        self.state = enabled
        self.available = available
        self.calls: list[str] = []
        self.log = log if log is not None else []

    def run(self, method):
        if not self.available:
            raise ValueError("omarchy_idle_unavailable")
        self.calls.append(method)
        self.log.append(("idle", method))
        if method == "status":
            return json.dumps({"enabled": self.state, "screensaver": 150, "lock": 300})
        self.state = method == "enable"
        return "enabled" if self.state else "disabled"


class FakeShell(OmarchyShell):
    """Quickshell's crash directory, and `omarchy-restart-shell`.

    A crash is a new directory under the crash dir, which is exactly how the
    real thing is noticed. `restarts` records how many times the session asked
    for a repair, so "once per session" is testable.
    """

    def __init__(self, root, *, succeeds=True):
        self.dir = Path(root) / "quickshell-crashes"
        self.dir.mkdir(parents=True, exist_ok=True)
        super().__init__(crash_dir=self.dir, runner=self._restart)
        self.restarts = 0
        self.succeeds = succeeds

    def crash(self, name):
        (self.dir / name).mkdir()

    def _restart(self):
        self.restarts += 1
        return self.succeeds


def profile_request(width=1194, height=834, *, orientation=None, long_edge=1280.0):
    return {"viewport_points": {"width": width, "height": height},
            "orientation": orientation or ("landscape_left" if width >= height else "portrait"),
            "logical_long_edge": long_edge,
            "quality": {"max_pixels": 4000000, "fps": 60, "bitrate_kbps": 20000},
            "decoder": {"max_width": 4096, "max_height": 4096, "max_pixels": 16777216,
                        "max_fps": 60, "max_bitrate_kbps": 40000, "codecs": ["h264"]}}
