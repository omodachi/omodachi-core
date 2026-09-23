"""Same-user Omarchy/Hyprland workspace adapter with a closed command surface.

The installed Omarchy 4 Workspaces.qml uses the Lua dispatcher below. Only
validated positive compositor workspace IDs reach it; no client Lua is accepted.
Window titles and compositor addresses never enter the public snapshot.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import time
from typing import Callable

from .routes import RouteDescriptor


class GraphicalUnavailable(ValueError):
    pass


SIGNATURE = re.compile(r"[A-Za-z0-9_.-]{1,200}")
WAYLAND_DISPLAY = re.compile(r"wayland-[0-9]+")


def _session(signature: str, runtime: Path, display: str) -> dict[str, str]:
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8",
            "HYPRLAND_INSTANCE_SIGNATURE": signature, "XDG_RUNTIME_DIR": str(runtime),
            "WAYLAND_DISPLAY": display}


def compositor_process(proc: Path = Path("/proc"), uid: int | None = None,
                       runtime: Path | None = None) -> tuple[Path, dict[str, str]] | None:
    """The one live same-UID Hyprland instance: its `/proc` entry and its session.

    Hyprland writes `<runtime>/hypr/<instance signature>/hyprland.lock` holding
    its PID and its Wayland display, and keeps `.socket.sock` there for as long
    as it is up. That is the compositor speaking for itself, so the answer does
    not depend on the Omarchy shell being alive: `omarchy-hyprland-monitor-watch`
    restarts the shell whenever the monitors move, and during that restart there
    is genuinely no `quickshell` to find, which used to take Remote down with it
    (`wake_state_unavailable`, "the host's Remote components are not ready yet")
    even though the compositor never went anywhere.

    PERF-5 follow-up: the `/proc` entry is returned as well, because the
    compositor's own environment is the same session environment the shell has
    and it is there whenever the session is. `catalog_providers` reads it.
    """
    uid = os.getuid() if uid is None else uid
    root = Path(runtime) if runtime is not None else Path(
        os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}")
    candidates = {}
    try:
        directories = sorted((root / "hypr").iterdir())
    except OSError:
        return None
    for directory in directories:
        try:
            if not directory.is_dir() or not SIGNATURE.fullmatch(directory.name):
                continue
            if not (directory / ".socket.sock").is_socket():
                continue
            lines = (directory / "hyprland.lock").read_text()[:256].splitlines()
            if len(lines) < 2 or not lines[0].strip().isdigit():
                continue
            pid, display = lines[0].strip(), lines[1].strip()
            if not WAYLAND_DISPLAY.fullmatch(display) or not (root / display).is_socket():
                continue
            process = proc / pid
            if process.stat().st_uid != uid or (process / "comm").read_text().strip() != "Hyprland":
                continue
        except (OSError, UnicodeError, ValueError):
            continue
        candidates[directory.name] = (process, _session(directory.name, root, display))
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def compositor_environment(proc: Path = Path("/proc"), uid: int | None = None,
                           runtime: Path | None = None) -> dict[str, str]:
    """The live same-UID compositor session, or `{}` when there is not exactly one."""
    found = compositor_process(proc, uid, runtime)
    return found[1] if found is not None else {}


def graphical_environment(proc: Path = Path("/proc"), uid: int | None = None,
                          runtime: Path | None = None) -> dict[str, str]:
    """The live same-UID graphical session: the compositor first, the shell second."""
    value = compositor_environment(proc, uid, runtime)
    if value:
        return value
    return shell_environment(proc, uid)


def shell_environment(proc: Path = Path("/proc"), uid: int | None = None) -> dict[str, str]:
    """Find one live same-UID Omarchy shell, not an SSH or matching grep process."""
    uid = os.getuid() if uid is None else uid
    candidates = {}
    try:
        processes = proc.iterdir()
        for process in processes:
            try:
                if not process.name.isdigit() or process.stat().st_uid != uid:
                    continue
                if (process / "comm").read_text().strip() != "quickshell":
                    continue
                argv = (process / "cmdline").read_bytes().split(b"\0")
                if b"/usr/share/omarchy/shell" not in argv:
                    continue
                raw = (process / "environ").read_bytes()
                if len(raw) > 131072:
                    continue
                source = dict(item.decode().split("=", 1) for item in raw.split(b"\0") if b"=" in item)
                signature = source.get("HYPRLAND_INSTANCE_SIGNATURE", "")
                runtime = source.get("XDG_RUNTIME_DIR", "")
                display = source.get("WAYLAND_DISPLAY", "")
                if (not SIGNATURE.fullmatch(signature) or runtime != f"/run/user/{uid}"
                        or not WAYLAND_DISPLAY.fullmatch(display)):
                    continue
                candidates[(signature, runtime, display)] = _session(signature, Path(runtime), display)
            except (OSError, UnicodeError, ValueError):
                continue
    except OSError:
        pass
    if len(candidates) != 1:
        raise GraphicalUnavailable("graphical_session_unavailable_or_ambiguous")
    return next(iter(candidates.values()))


def bounded_hyprctl(argv: tuple[str, ...], environment: dict[str, str]) -> str:
    """Fixed argv only; bounded memory/deadline, with no stderr/title logging."""
    process = subprocess.Popen(argv, env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    result = bytearray()
    deadline = time.monotonic() + 1.0
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GraphicalUnavailable("graphical_probe_timeout")
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, min(65536, 524289 - len(result)))
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    result.extend(chunk)
                    if len(result) > 524288:
                        raise GraphicalUnavailable("graphical_probe_output_limit")
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if process.returncode:
            raise GraphicalUnavailable("graphical_command_failed")
        return result.decode("utf-8")
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)
        if process.stdout:
            process.stdout.close()


WORKSPACE_ID = re.compile(r"[1-9][0-9]{0,8}")
OUTPUT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
#: `e+1`/`e-1` as a step. `workspace.select`'s relative form is ARCH-1's (#24,
#: `CoreService.relative_workspace`); this is the Remote-session rewrite's copy,
#: which steps the workspaces on the session's own output instead.
RELATIVE_WORKSPACE = {"e+1": 1, "e-1": -1}


def focus_workspace_on_output(workspace: str, output: str) -> str:
    """`focusworkspaceoncurrentmonitor`, spelled for a Lua config.

    Hyprland has no single Lua call for it: `hyprctl binds -j` on an Omarchy 4
    host reports every bind as `__lua`, `hyprctl dispatch <name> <arg>` no
    longer parses, and `hl.dsp.focus` silently ignores an unrecognised key
    beside a recognised `workspace`, so `current_monitor = true` and friends
    compile and then do nothing (verified on the host: focus still jumped to
    whichever monitor already owned the workspace). What the dispatcher does
    internally is move the workspace to the monitor and then focus it, and both
    halves do exist, so that is what this replays.

    The output is named rather than resolved as "current", so the result does
    not depend on where focus happens to be when a client taps: a Remote
    session's own output is the destination either way.
    """
    if not WORKSPACE_ID.fullmatch(workspace) or not OUTPUT_NAME.fullmatch(output):
        raise GraphicalUnavailable("workspace_unavailable")
    return ("local r1 = hl.dispatch(hl.dsp.workspace.move({ workspace = " + json.dumps(workspace)
            + ", monitor = " + json.dumps(output) + " })) "
            "local r2 = hl.dispatch(hl.dsp.focus({ workspace = " + json.dumps(workspace) + " })) "
            'if type(r1)~="table" or r1.ok~=true or type(r2)~="table" or r2.ok~=true then '
            'error("omodachi_workspace_focus_failed") end')


def step_workspace(current: int, existing, delta: int):
    """Hyprland's `e+1`/`e-1`: the next existing workspace, wrapping.

    `existing` is whatever collection the caller is allowed to act on: a Remote
    session steps through the workspaces on its own output.
    """
    numbers = sorted({value for value in existing if type(value) is int and value > 0})
    if type(current) is not int or current not in numbers or len(numbers) < 2:
        return None
    return numbers[(numbers.index(current) + delta) % len(numbers)]


class HyprlandWorkspaceAdapter:
    def __init__(self, service, *, runner: Callable = bounded_hyprctl,
                 environment: Callable = graphical_environment):
        self.service, self.runner, self.environment = service, runner, environment
        self.generation = 0
        for number in range(1, 11):
            entry = f"omodachi.workspace.select.{number}"
            argv = ("/usr/bin/hyprctl", "dispatch", f'hl.dsp.focus({{ workspace = "{number}" }})')
            service.policy.register(entry, RouteDescriptor("host", True, argv=argv),
                                    source_action=f"omodachi-host workspace select {number}")
            service.register_executor(entry, self.select)
            move = f"omodachi.workspace.move.{number}"
            service.policy.register(move, RouteDescriptor("host", True, argv=("hyprland.move-focused", str(number))),
                                    source_action=f"omodachi-host workspace move-focused {number}")
            service.register_executor(move, self.move, requires_target=True)

    def inspect(self) -> dict:
        try:
            env = self.environment()
            def query(name):
                return json.loads(self.runner(("/usr/bin/hyprctl", "-j", name), env))
            workspaces, window, active = query("workspaces"), query("activewindow"), query("activeworkspace")
            if not isinstance(workspaces, list) or not isinstance(window, dict) or not isinstance(active, dict):
                raise ValueError("invalid graphical response")
            counts = {}
            for row in workspaces:
                number, count = row.get("id"), row.get("windows")
                if type(number) is int and 1 <= number <= 2147483647:
                    if type(count) is not int or not 0 <= count <= 10000:
                        raise ValueError("invalid occupancy")
                    counts[number] = count
            number = active.get("id")
            if type(number) is not int:
                raise ValueError("invalid active workspace")
            focused = None
            address = window.get("address")
            if isinstance(address, str) and re.fullmatch(r"0x[0-9a-fA-F]{1,32}", address) and int(address, 16):
                # Class is an application identifier; deliberately ignore all title fields.
                app = window.get("class")
                app = app[:128] if isinstance(app, str) and not any(ord(c) < 32 for c in app) else None
                focused = {"id": address, "app_id": app, "app_name": app}
            result={"available": True, "active": number if 1 <= number <= 2147483647 else None,
                    "window_counts": counts, "focused_window": focused}
            wake=getattr(self.service,'wake_adapter',None)
            if wake is not None:
                try:result['wake']=wake.inspect(env)
                except (OSError,ValueError):pass
            return result
        except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
            return {"available": False, "active": None, "window_counts": {}, "focused_window": None}

    def publish(self, snapshot: dict) -> None:
        if "wake" in snapshot and self.service.hub.state_view("wake")["wake"]!=snapshot["wake"]:
            self.service.hub.update_state({"wake":snapshot["wake"]},event_type="wake.changed")
        status = "available" if snapshot["available"] else "unavailable"
        if (self.service.hub.state_view("host")["host"] or {}).get("graphical_state") != status:
            self.service.hub.update_state({"host": {"graphical_state": status}}, event_type="host.changed")
        # PERF-4. `set_workspace_snapshot` already invalidates and refreshes the
        # catalog when the reading actually moved. Refreshing again here meant
        # the 0.5 s probe rebuilt the whole catalog twice a second whether
        # anything had changed or not - on the asyncio thread, which is why a
        # `GET /v1/state` that reads one dict measured seconds on the host.
        self.service.set_workspace_snapshot(active=snapshot["active"],
            window_counts=snapshot["window_counts"], focused_window=snapshot["focused_window"],
            snapshot_available=snapshot["available"])

    def refresh(self) -> None:
        self.publish(self.inspect())

    def official_workspaces(self, snapshot: dict) -> set[int]:
        """The collection the official bar draws, and the only one we act on.

        `Workspaces.qml` always draws the fixed persistent rows and adds every
        other live workspace, so that set — not the compositor's list on its own
        — is what a client can see and therefore what it may select. A
        persistent row Hyprland has not materialised is still a row: it holds
        nothing, it is drawn, and SUPER+3 switches to it on the host.
        """
        numbers = set(snapshot["window_counts"])
        if snapshot["active"] is not None:
            numbers.add(snapshot["active"])
        numbers.update(getattr(self.service, "PERSISTENT_WORKSPACES", ()))
        return numbers

    def select_existing(self, number: int, *, on_output: str | None = None) -> None:
        """Focus a workspace the host already publishes.

        `on_output` is the Remote session's own output. ARCH-1 / Study 04 A-64:
        a workspace square in the bar or in Panel ① acts on the screen the user
        is looking at, and during a session that screen is ours — so the
        workspace is pulled here first, exactly the way SHORTCUT-1's rewrite
        does it for `SUPER+N`. With no session the argument is absent and this
        is the plain focus it has always been: on the physical screen, "go to
        whichever monitor already shows workspace N" is the right answer.
        """
        if type(number) is not int or not 1<=number<=2147483647:
            raise GraphicalUnavailable("workspace_unavailable")
        before=self.inspect()
        if not before["available"] or number not in self.official_workspaces(before):
            raise GraphicalUnavailable("workspace_unavailable")
        self.generation += 1
        env=self.environment()
        if on_output is not None:
            command=focus_workspace_on_output(str(number),on_output)
        else:
            # The Lua object lookup and focus share one dispatch request. An
            # existing workspace is focused as the object the compositor already
            # has; a persistent row it has not created yet is named by its number,
            # which is the one thing that selector can produce. A workspace outside
            # the published collection never reaches this line.
            command=('local w=hl.get_workspace('+str(number)+') or "'+str(number)+'"; '
                     'local r=hl.dispatch(hl.dsp.focus({workspace=w})); if type(r)~="table" or r.ok~=true then error("omodachi_workspace_focus_failed") end')
        output=self.runner(("/usr/bin/hyprctl","eval",command),env)
        if output.strip().lower()!="ok":raise GraphicalUnavailable("workspace_dispatch_failed")
        snapshot=self.inspect();self.publish(snapshot)
        if snapshot["active"]!=number:raise GraphicalUnavailable("workspace_readback_failed")

    def select(self, argv: tuple[str, ...]) -> None:
        # Legacy catalog registrations retain their command shape but dispatch
        # through the observed-workspace action, never implicit creation.
        allowed = {("/usr/bin/hyprctl", "dispatch", f'hl.dsp.focus({{ workspace = "{n}" }})'): n for n in range(1, 11)}
        if argv not in allowed:raise ValueError("invalid workspace command")
        self.select_existing(allowed[argv])

    def move(self, argv: tuple[str, ...], token: str) -> None:
        from .service import ServiceError
        if argv not in {("hyprland.move-focused", str(n)) for n in range(1, 11)}:
            raise ValueError("invalid workspace command")
        target = self.service.resolve_window_target(token)
        address = target["id"]
        if not re.fullmatch(r"0x[0-9a-fA-F]{1,32}", address) or not int(address, 16):
            raise ServiceError("stale_target", status=409)
        self.generation += 1
        snapshot = self.inspect()
        self.publish(snapshot)
        focused = snapshot["focused_window"]
        if int(argv[1]) not in self.official_workspaces(snapshot):raise GraphicalUnavailable("workspace_unavailable")
        if not snapshot["available"] or not focused or focused["id"] != address:
            raise ServiceError("stale_target", status=409)
        # Hyprland v0.56.2 LuaBindingsDispatchers.cpp:860 and
        # LuaBindingsInternal.cpp:323 support an explicit window selector.
        # Both interpolated values originate in finite/hex-validated host data.
        command = f'hl.dsp.window.move({{ workspace = "{argv[1]}", follow = false, window = "address:{address}" }})'
        env = self.environment()
        output = self.runner(("/usr/bin/hyprctl", "dispatch", command), env)
        if output.strip().lower() != "ok":
            raise GraphicalUnavailable("workspace_dispatch_failed")
        clients = json.loads(self.runner(("/usr/bin/hyprctl", "-j", "clients"), env))
        moved = next((row for row in clients if row.get("address") == address), None)
        self.refresh()
        if not moved or moved.get("workspace", {}).get("id") != int(argv[1]):
            raise GraphicalUnavailable("workspace_move_readback_failed")
