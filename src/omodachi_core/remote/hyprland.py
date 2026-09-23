"""Bounded Hyprland control surface for the owned headless output.

Every mutation is followed by a readback of ``monitors all`` or the workspace
projection; nothing is reported as applied because a command returned ``ok``.
Physical outputs are read and, in takeover, disabled and restored from their
own snapshot — they are never adopted as a capture target or reconfigured to
geometry the host did not already have.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import math
import os
from pathlib import Path
import re
import selectors
import subprocess
import time
import uuid

from .errors import RemoteError

OWNED_NAME = re.compile(r"OMODACHI-[0-9a-f]{16}")
MONITOR_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
INSTANCE = re.compile(r"[A-Za-z0-9_.-]{1,200}")
DEVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.:+()/-]{0,127}")
# Sunshine and any other emulated input must stay enabled while the physical
# keyboard and pointer are locked, or the remote client loses its own input.
# The fork's uinput devices appear as `mouse-passthrough`, `mouse-passthrough-
# (absolute)` and `keyboard-passthrough`.
VIRTUAL_DEVICE = re.compile(r"sunshine|uinput|virtual|omodachi|passthrough", re.IGNORECASE)

# The fork's touch and pen devices, as Hyprland names them (lowercased, spaces
# hyphenated): inputtino creates them per client context. Their coordinates are
# normalized to the captured output (`monitor_touch_port()` in the fork), so the
# compositor is what has to place that box — `device[name]:output`, which
# Hyprland accepts for a touchscreen and a tablet.
#
# The absolute *pointer* is deliberately not here. Its coordinates are relative
# to the whole virtual desktop, and Hyprland resolves an absolute pointer
# against the bounding box of its enabled monitors: measured on 0.56.2 for
# INPUT-1, `hyprctl getoption "device:mouse-passthrough-(absolute):output"`
# answers `no such option` and setting it changes nothing. Binding it would be
# wrong even if it worked, because then the desktop-wide coordinates would be
# squeezed into one output.
OUTPUT_BOUND_DEVICES = ("touch-passthrough", "pen-passthrough")

# Only these read-only projections are executed. `hyprctl -j workspaces`
# carries last-window titles and is deliberately never consumed.
WORKSPACES_LUA = """local rows={}; for _,w in ipairs(hl.get_workspaces()) do
if w.monitor then rows[#rows+1]=string.format('[%d,%d,%d]',w.id,w.monitor.id,w.windows) end
end; return '['..table.concat(rows,',')..']' """
ACTIVE_WORKSPACE_LUA = ("""local w=hl.get_active_workspace(); local s=hl.get_active_special_workspace(); """
                        """if s or not w or not w.monitor then return 'null' end; """
                        """return string.format('[%d,%d]',w.id,w.monitor.id)""")

# The one global config option a session owns. It coerces a lone window into a
# fixed aspect ratio, which on the owned output leaves the remote client looking
# at a square in the middle of its screen. Hyprland has no per-monitor or
# per-workspace form of it (PERF-2 §8.2), so a takeover turns the global off and
# the restore puts the user's own value back.
SINGLE_WINDOW_ASPECT = "layout:single_window_aspect_ratio"

# The two globals that decide whether input wakes a screen that DPMS turned
# off. Omarchy sets both to true in `/usr/share/omarchy/default/hypr/input.lua`,
# and a takeover forwards the remote client's pointer and keys to this very
# compositor: with them on, every tap on the iPad lights the panel in the room
# until the next `reconcile()` darkens it again. Like the aspect ratio these
# have no per-monitor or per-device form, so the only honest scope is the
# session. The value is the `hl.config` path each one lives at.
DPMS_WAKE_OPTIONS = {"misc:mouse_move_enables_dpms": ("misc", "mouse_move_enables_dpms"),
                     "misc:key_press_enables_dpms": ("misc", "key_press_enables_dpms")}


def new_output_name() -> str:
    return "OMODACHI-" + uuid.uuid4().hex[:16]


def finite(value, minimum, maximum):
    if type(value) not in {int, float} or not math.isfinite(value) or not minimum <= value <= maximum:
        raise RemoteError("display_schema_invalid")
    return value


@dataclass(frozen=True)
class OutputState:
    name: str
    monitor_id: int
    width: int
    height: int
    refresh_hz: float
    scale: float
    x: int
    y: int
    transform: int
    dpms: bool
    disabled: bool

    @classmethod
    def parse(cls, row):
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not MONITOR_NAME.fullmatch(row["name"]):
            raise RemoteError("display_schema_invalid")
        bounds = {"id": (0, 10000), "width": (0, 16384), "height": (0, 16384),
                  "x": (-65536, 65536), "y": (-65536, 65536), "transform": (0, 7)}
        for key, (lower, upper) in bounds.items():
            if type(row.get(key)) is not int or not lower <= row[key] <= upper:
                raise RemoteError("display_schema_invalid")
        for key in ("dpmsStatus", "disabled"):
            if type(row.get(key)) is not bool:
                raise RemoteError("display_schema_invalid")
        # A disabled monitor reports 0x0@0 and scale 0; keep it representable so
        # the takeover snapshot can restore the configuration it had when enabled.
        return cls(row["name"], row["id"], row["width"], row["height"], finite(row.get("refreshRate"), 0, 1000),
                   finite(row.get("scale"), 0, 8), row["x"], row["y"], row["transform"],
                   row["dpmsStatus"], row["disabled"])

    @property
    def logical_width(self):
        return math.ceil(self.width / self.scale) if self.scale else 0

    @property
    def logical_height(self):
        return math.ceil(self.height / self.scale) if self.scale else 0

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise RemoteError("display_schema_invalid")
        return cls(**value)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    error: str | None = None


def guard_lua(outputs) -> str:
    """Compare the compositor's actual fields in the same evaluation as the setter."""
    lines = []
    for index, row in enumerate(outputs):
        name, variable = json.dumps(row.name), "m" + str(index)
        lines.append(
            f"local {variable}=hl.get_monitor({name}); if not {variable}"
            f" or {variable}.id~={row.monitor_id} or {variable}.width~={row.width}"
            f" or {variable}.height~={row.height} or math.abs({variable}.scale-{row.scale!r})>0.000001"
            f" or {variable}.x~={row.x} or {variable}.y~={row.y} or {variable}.transform~={row.transform}"
            f" or math.abs({variable}.refresh_rate-{row.refresh_hz!r})>0.01"
            f" or {variable}.dpms_status~={str(row.dpms).lower()} then error(\"omodachi_output_conflict\") end;")
    return "\n".join(lines)


def monitor_lua(name, *, mode_pixels=None, scale=None, position=None, refresh_hz=None,
                transform=None, disabled=None) -> str:
    """One `hl.monitor` call. No free-form client Lua ever reaches the compositor.

    `hyprctl keyword` is unusable here: Hyprland 0.56 answers it with "keyword
    can't work with non-legacy parsers. Use eval." So every output change —
    mode, scale, position, and enable/disable — goes through this binding, and
    `disabled` is the only field it accepts for turning a screen off.
    """
    if not MONITOR_NAME.fullmatch(name):
        raise RemoteError("display_schema_invalid")
    fields = ["output=" + json.dumps(name)]
    if mode_pixels is not None:
        # The profile's fps describes encoded video. Preserve the output's
        # observed refresh rather than turning a quality change into a mode.
        fields.append("mode=" + json.dumps(f"{mode_pixels.width}x{mode_pixels.height}@{refresh_hz:g}"))
    if position is not None:
        if (not isinstance(position, tuple) or len(position) != 2
                or any(type(value) is not int or not -65536 <= value <= 65536 for value in position)):
            raise RemoteError("display_schema_invalid")
        fields.append("position=" + json.dumps(f"{position[0]}x{position[1]}"))
    if scale is not None:
        fields.append("scale=" + repr(float(finite(scale, 0.25, 8))))
    if transform is not None:
        if type(transform) is not int or not 0 <= transform <= 7:
            raise RemoteError("display_schema_invalid")
        fields.append("transform=" + str(transform))
    if disabled is not None:
        fields.append("disabled=" + ("true" if disabled else "false"))
    return "hl.monitor({ " + ", ".join(fields) + " })"


def move_workspace_lua(workspace_id, source_monitor_id, destination, *, focus=False) -> str:
    return ("local w=hl.get_workspace(" + str(workspace_id) + "); if not w or not w.monitor or w.monitor.id~="
            + str(source_monitor_id) + " then error(\"omodachi_workspace_conflict\") end; "
            "local r=hl.dispatch(hl.dsp.workspace.move({workspace=w,monitor=" + json.dumps(destination) + "})); "
            "if type(r)~=\"table\" or r.ok~=true then error(\"omodachi_workspace_migration_failed\") end;"
            + (focus_workspace_lua(workspace_id) if focus else ""))


def focus_workspace_lua(workspace_id) -> str:
    return ("local f=hl.get_workspace(" + str(workspace_id) + "); if not f then error(\"omodachi_workspace_conflict\") end; "
            "local fr=hl.dispatch(hl.dsp.focus({workspace=f})); "
            "if type(fr)~=\"table\" or fr.ok~=true then error(\"omodachi_workspace_focus_failed\") end;")


class Hyprland:
    """One compositor instance, reached with a fixed hyprctl argv."""

    def __init__(self, instance_id: str, *, runner=None, timeout=2.0, max_bytes=262144, sleep=time.sleep):
        if not isinstance(instance_id, str) or not INSTANCE.fullmatch(instance_id):
            raise RemoteError("compositor_instance_invalid")
        finite(timeout, 0.01, 10)
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 1048576:
            raise RemoteError("display_schema_invalid")
        self.instance_id, self.timeout, self.max_bytes = instance_id, timeout, max_bytes
        self.runner = runner or self._local_run
        # Only the readback polls wait; injectable so the tests do not.
        self.sleep = sleep

    def argv(self, *tail):
        return ("/usr/bin/hyprctl", "--instance", self.instance_id, *tail)

    def event_socket(self):
        """The compositor's own event stream, `.socket2.sock`.

        Hyprland announces `monitoradded`, `monitorremoved` and
        `configreloaded` here. A session has to hear them: the host's own tools
        reconfigure outputs underneath it (`omarchy-hyprland-monitor-watch`
        issues `hyprctl reload`, the official Display panel calls
        `omarchy-hyprland-monitor-scaling` on whichever monitor is focused —
        which, during a session, is ours).
        """
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        return str(Path(runtime) / "hypr" / self.instance_id / ".socket2.sock")

    def _local_run(self, argv):
        process = None
        selector = selectors.DefaultSelector()
        output = bytearray()
        deadline = time.monotonic() + self.timeout
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, shell=False, start_new_session=True,
                                       env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
                                            "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"})
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return CommandResult(124, error="display_timeout")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, min(65536, self.max_bytes - len(output) + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        if len(output) > self.max_bytes:
                            return CommandResult(125, error="display_output_limit")
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
            return CommandResult(process.returncode, output.decode("utf-8"))
        except (OSError, UnicodeError, subprocess.SubprocessError):
            return CommandResult(1, error="display_command_failed")
        finally:
            selector.close()
            if process and process.poll() is None:
                try:
                    os.killpg(process.pid, 9)
                except (ProcessLookupError, PermissionError):
                    if process.poll() is None:
                        process.kill()
                process.wait(timeout=1)
            if process and process.stdout:
                process.stdout.close()

    def run(self, *tail) -> str:
        result = self.runner(self.argv(*tail))
        if (not isinstance(result, CommandResult) or type(result.returncode) is not int
                or not isinstance(result.stdout, str) or len(result.stdout.encode()) > self.max_bytes
                or result.returncode or result.error):
            raise RemoteError(getattr(result, "error", None) or "display_command_failed")
        return result.stdout

    def _mutate(self, *tail) -> None:
        if self.run(*tail).strip() != "ok":
            raise RemoteError("display_mutation_unconfirmed")

    # --- reads -------------------------------------------------------------

    def version(self) -> str:
        return self.run("version").splitlines()[0].strip()

    def monitors(self) -> list[OutputState]:
        try:
            value = json.loads(self.run("-j", "monitors", "all"))
        except (ValueError, TypeError):
            raise RemoteError("display_schema_invalid") from None
        if not isinstance(value, list) or len(value) > 64:
            raise RemoteError("display_schema_invalid")
        rows = [OutputState.parse(row) for row in value]
        if len({row.name for row in rows}) != len(rows):
            raise RemoteError("display_schema_invalid")
        return rows

    def _projection(self, lua):
        try:
            return json.loads(self.run("repl", lua))
        except (ValueError, TypeError):
            raise RemoteError("workspace_schema_invalid") from None

    def workspaces(self) -> list[dict]:
        value = self._projection(WORKSPACES_LUA)
        if not isinstance(value, list) or len(value) > 4096:
            raise RemoteError("workspace_schema_invalid")
        rows = []
        for row in value:
            if (not isinstance(row, list) or len(row) != 3 or any(type(n) is not int for n in row)
                    or not -2 ** 31 < row[0] < 2 ** 31 or not 0 <= row[1] <= 10000 or row[2] < 0):
                raise RemoteError("workspace_schema_invalid")
            rows.append({"id": row[0], "monitor_id": row[1], "windows": row[2]})
        if len({row["id"] for row in rows}) != len(rows):
            raise RemoteError("workspace_schema_invalid")
        return sorted(rows, key=lambda row: row["id"])

    def active_workspace(self):
        row = self._projection(ACTIVE_WORKSPACE_LUA)
        if row is None:
            return None
        if not isinstance(row, list) or len(row) != 2 or any(type(n) is not int for n in row):
            raise RemoteError("workspace_schema_invalid")
        return tuple(row)

    def single_window_aspect_ratio(self) -> tuple[float, float, bool]:
        """(x, y, explicitly_set) for `layout:single_window_aspect_ratio`."""
        try:
            value = json.loads(self.run("-j", "getoption", SINGLE_WINDOW_ASPECT))
        except (ValueError, TypeError):
            raise RemoteError("config_schema_invalid") from None
        vector = value.get("vec2") if isinstance(value, dict) else None
        if (not isinstance(vector, list) or len(vector) != 2
                or type(value.get("set")) is not bool):
            raise RemoteError("config_schema_invalid")
        return (finite(vector[0], 0, 1000), finite(vector[1], 0, 1000), value["set"])

    def bool_option(self, name: str) -> tuple[bool, bool]:
        """(value, explicitly_set) for one of the boolean globals a session owns.

        `hyprctl descriptions` is not usable for this: on 0.56.2 it reports the
        compiled-in default as `current` (`false` for both wake options) while
        the live value read here is the `true` the user's config set.
        """
        if name not in DPMS_WAKE_OPTIONS:
            raise RemoteError("config_option_unknown")
        try:
            value = json.loads(self.run("-j", "getoption", name))
        except (ValueError, TypeError):
            raise RemoteError("config_schema_invalid") from None
        if (not isinstance(value, dict) or type(value.get("bool")) is not bool
                or type(value.get("set")) is not bool):
            raise RemoteError("config_schema_invalid")
        return (value["bool"], value["set"])

    def input_devices(self) -> list[str]:
        """Physical keyboards and pointers only; emulated input stays untouched."""
        try:
            value = json.loads(self.run("-j", "devices"))
        except (ValueError, TypeError):
            raise RemoteError("device_schema_invalid") from None
        if not isinstance(value, dict):
            raise RemoteError("device_schema_invalid")
        names = []
        for group in ("mice", "keyboards", "touch", "tablets"):
            rows = value.get(group) or []
            if not isinstance(rows, list) or len(rows) > 256:
                raise RemoteError("device_schema_invalid")
            for row in rows:
                name = row.get("name") if isinstance(row, dict) else None
                if (isinstance(name, str) and DEVICE_NAME.fullmatch(name)
                        and not VIRTUAL_DEVICE.search(name) and name not in names):
                    names.append(name)
        return names

    # --- mutations ---------------------------------------------------------

    def eval(self, lua: str) -> None:
        self._mutate("eval", lua)

    def create_output(self, name: str) -> OutputState:
        if not OWNED_NAME.fullmatch(name):
            raise RemoteError("not_owned_headless_name")
        before = self.monitors()
        if any(row.name == name for row in before):
            raise RemoteError("owned_output_name_conflict")
        # Hyprland's outputRequest accepts a fourth argument as the headless
        # name and rejects a duplicate. There is no physical fallback.
        self._mutate("output", "create", "headless", name)
        after = self.monitors()
        owned = next((row for row in after if row.name == name), None)
        if owned is None or len(after) != len(before) + 1:
            raise RemoteError("owned_output_creation_unconfirmed")
        return owned

    def destroy_output(self, name: str) -> None:
        if not OWNED_NAME.fullmatch(name):
            raise RemoteError("not_owned_headless_name")
        self._mutate("output", "remove", name)
        if any(row.name == name for row in self.monitors()):
            raise RemoteError("owned_output_removal_unconfirmed")

    def configure_output(self, name, mode_pixels, scale, position, *, guards=(), attempts=20, rounds=4):
        """Apply mode/scale/position, then poll the readback until it settles.

        The write is issued `rounds` times rather than once. A `hyprctl reload`
        is not atomic from out here: the host's monitor watcher fires one on
        every output change, and the catch-all
        `hl.monitor({output="", mode="preferred", position="auto", scale=...})`
        in `~/.config/hypr/monitors.lua` lands on *our* output somewhere inside
        it. A single write that happens to be issued a few milliseconds before
        that is simply overwritten, and a session whose only answer to a
        readback it does not like is `profile_readback_failed` ends there
        (HOST-2 §4.2). Re-issuing, and tolerating an output that has briefly
        gone away while the compositor rebuilds it, is the whole difference
        between "the host was reconfigured" and "the session died".
        """
        others = [row for row in self.monitors() if row.name != name]
        if not OWNED_NAME.fullmatch(name):
            raise RemoteError("not_owned_headless_name")
        missing = False
        for _round in range(rounds):
            refresh = self._refresh_of(name, default=None)
            if refresh is None:
                # The output is not there this instant. It comes back by itself
                # when the reload finishes; if it does not, the caller re-creates
                # it, which is a different repair from this one.
                missing = True
                self.sleep(0.05)
                continue
            missing = False
            lua = [guard_lua(guards)] if guards else []
            lua.append(monitor_lua(name, mode_pixels=mode_pixels, scale=scale, position=position,
                                   refresh_hz=refresh, transform=0))
            self.eval("\n".join(value for value in lua if value))
            for attempt in range(attempts):
                owned = next((row for row in self.monitors() if row.name == name), None)
                if (owned is not None and (owned.width, owned.height) == (mode_pixels.width, mode_pixels.height)
                        and abs(owned.scale - scale) < 1e-6 and (owned.x, owned.y) == position):
                    return owned, others
                if attempt == attempts - 1:
                    break
                self.sleep(0.05)
        if missing:
            raise RemoteError("owned_output_unavailable")
        raise RemoteError("profile_readback_failed")

    def _refresh_of(self, name, *, default=...):
        owned = next((row for row in self.monitors() if row.name == name), None)
        if owned is None:
            if default is ...:
                raise RemoteError("owned_output_unavailable")
            return default
        return owned.refresh_hz or 60.0

    def apply_output(self, snapshot: OutputState) -> None:
        """Restore one physical output to an exact previously observed configuration."""
        if OWNED_NAME.fullmatch(snapshot.name):
            raise RemoteError("not_a_physical_output")
        self.eval(monitor_lua(snapshot.name, mode_pixels=snapshot, scale=snapshot.scale,
                              position=(snapshot.x, snapshot.y), refresh_hz=snapshot.refresh_hz,
                              transform=snapshot.transform, disabled=False))
        for attempt in range(20):
            row = next((r for r in self.monitors() if r.name == snapshot.name), None)
            if row is not None and not row.disabled and (row.width, row.height) == (snapshot.width, snapshot.height):
                return
            if attempt == 19:
                raise RemoteError("physical_output_restore_unconfirmed")
            time.sleep(0.05)

    def set_output_dpms(self, name: str, on: bool) -> None:
        """Turn a physical screen's backlight off or on, keeping the output.

        `hl.dsp.dpms` on Hyprland 0.56 takes the monitor object and **toggles
        it**: every spelling of the on/off argument the config dispatcher
        accepts (`mode`, `state`, `on`, `power`, `enabled`, a positional
        string) is ignored, and asking for the state a screen is already in
        turns it round. So the state is read first and the toggle is only
        issued when it would change something; `dpmsStatus` on `monitors all`
        is then read back until it agrees.
        """
        if OWNED_NAME.fullmatch(name) or not MONITOR_NAME.fullmatch(name):
            raise RemoteError("not_a_physical_output")
        row = next((r for r in self.monitors() if r.name == name), None)
        if row is None:
            raise RemoteError("physical_output_unavailable")
        if row.dpms is on:
            return
        self.eval("local m=hl.get_monitor(" + json.dumps(name) + "); if not m then error(\"omodachi_output_missing\") end; "
                  "local r=hl.dispatch(hl.dsp.dpms({monitor=m})); "
                  "if type(r)~=\"table\" or r.ok~=true then error(\"omodachi_dpms_failed\") end;")
        for attempt in range(20):
            row = next((r for r in self.monitors() if r.name == name), None)
            if row is not None and row.dpms is on:
                return
            if attempt == 19:
                raise RemoteError("output_dpms_unconfirmed")
            time.sleep(0.05)

    def blank_output(self, name: str) -> str:
        """Make a physical screen show nothing. Returns how it was done.

        DPMS first, because removing an output is what crashes the desktop
        shell on this host; a compositor that will not answer the dispatcher
        falls back to disabling the output, which is what every host did
        before HOST-1.
        """
        try:
            self.set_output_dpms(name, False)
            return "dpms"
        except RemoteError:
            self.disable_output(name)
            return "disabled"

    def disable_output(self, name: str) -> None:
        if OWNED_NAME.fullmatch(name) or not MONITOR_NAME.fullmatch(name):
            raise RemoteError("not_a_physical_output")
        self.eval(monitor_lua(name, disabled=True))
        for attempt in range(20):
            row = next((r for r in self.monitors() if r.name == name), None)
            if row is None or row.disabled:
                return
            if attempt == 19:
                raise RemoteError("physical_output_disable_unconfirmed")
            time.sleep(0.05)

    def set_single_window_aspect_ratio(self, x, y) -> None:
        """`hyprctl keyword` cannot reach a non-legacy parser, so this is
        `hl.config` with the two finite numbers the readback produced."""
        values = (float(finite(x, 0, 1000)), float(finite(y, 0, 1000)))
        self.eval("hl.config({ layout = { single_window_aspect_ratio = { %r, %r } } })" % values)

    def set_bool_option(self, name: str, value: bool) -> None:
        """One boolean global, through `hl.config` and read back.

        `hyprctl keyword` is unusable on this parser (see `monitor_lua`), and a
        compositor that answered `ok` has not necessarily applied anything, so
        the value is read back before this reports success.
        """
        if name not in DPMS_WAKE_OPTIONS or type(value) is not bool:
            raise RemoteError("config_option_unknown")
        section, key = DPMS_WAKE_OPTIONS[name]
        self.eval("hl.config({ %s = { %s = %s } })" % (section, key, "true" if value else "false"))
        if self.bool_option(name)[0] is not value:
            raise RemoteError("config_option_unconfirmed")

    def set_device_enabled(self, name: str, enabled: bool) -> None:
        """`hl.device` is the only binding for this; Hyprland reports no
        per-device enabled flag, so the journal is the record of what we turned
        off and the restore turns exactly those back on."""
        if not DEVICE_NAME.fullmatch(name) or VIRTUAL_DEVICE.search(name):
            raise RemoteError("input_device_invalid")
        self.eval("hl.device({ name=" + json.dumps(name) + ", enabled=" + ("true" if enabled else "false") + " })")

    def set_device_output(self, name: str, output: str | None) -> None:
        """Bind one of the fork's touch devices to the owned output, or unbind it.

        Hyprland keeps device configuration by name, so this is accepted before
        the device exists and applies when the fork creates it for a connecting
        client; it is also the only placement that survives a `hyprctl reload`
        re-sourcing the user's own input section. Only the fork's own touch and
        pen devices are addressable — a physical one is never remapped — and the
        only destination is an owned headless output. Hyprland publishes no
        per-device mapping to read back, so nothing here is reported as applied.
        """
        if name not in OUTPUT_BOUND_DEVICES:
            raise RemoteError("input_device_invalid")
        if output is None:
            target = ""
        elif OWNED_NAME.fullmatch(output):
            target = output
        else:
            raise RemoteError("not_owned_headless_name")
        self.eval("hl.device({ name=" + json.dumps(name) + ", output=" + json.dumps(target) + " })")

    def move_workspace(self, workspace_id, destination, *, focus=False, guards=()):
        """Move one whole workspace; windows are never read, closed or reparented.

        Returns False when the workspace is not there to move. An empty
        workspace is ephemeral bookkeeping in Hyprland: moving it off a monitor
        can destroy it outright, and that is a successful vacating, not a lost
        desktop. A workspace that had windows must be found where it was sent.
        """
        rows = self.workspaces()
        row = next((value for value in rows if value["id"] == workspace_id), None)
        if row is None:
            return False
        target = next((value for value in self.monitors() if value.name == destination), None)
        if target is None:
            raise RemoteError("workspace_destination_unavailable")
        if row["monitor_id"] != target.monitor_id:
            lua = [guard_lua(guards)] if guards else []
            lua.append(move_workspace_lua(workspace_id, row["monitor_id"], destination, focus=focus))
            self.eval("\n".join(value for value in lua if value))
        elif focus:
            self.eval(focus_workspace_lua(workspace_id))
        after = next((value for value in self.workspaces() if value["id"] == workspace_id), None)
        if after is None:
            if row["windows"]:
                raise RemoteError("workspace_migration_unconfirmed")
            return False
        if after["monitor_id"] != target.monitor_id:
            raise RemoteError("workspace_migration_unconfirmed")
        if focus and self.active_workspace() != (workspace_id, target.monitor_id):
            raise RemoteError("workspace_focus_unconfirmed")
        return True


def place(placement: str, physical, logical_width: int, logical_height: int) -> tuple[int, int]:
    """Position the owned output beside the physical layout, never on top of it."""
    if placement not in {"right", "left", "above", "below"}:
        raise RemoteError("placement_unsupported", 400)
    if not physical:
        return (0, 0)
    left = min(row.x for row in physical)
    top = min(row.y for row in physical)
    right = max(row.x + row.logical_width for row in physical)
    bottom = max(row.y + row.logical_height for row in physical)
    return {"right": (right, top), "left": (left - logical_width, top),
            "above": (left, top - logical_height), "below": (left, bottom)}[placement]
