"""MENU-4: every Omarchy menu row runs the way Omarchy's own menu runs it.

Before this, a catalog row was executable only when somebody had written a
reviewed adapter for it, and 272 of the host's 528 action rows - About, the
whole System submenu, every Install / Remove / Setup / Update / Style row - had
none. The app drew all of them `不可用`.

What Omarchy does with a row is small and fixed. `Menu.qml`'s `runAction` hands
the row's `action` string to `Util.execDetached`, which is

    Quickshell.execDetached(["bash", "-lc", command])

(`/usr/share/omarchy/shell/Commons/Util.qml`): a detached login bash, in the
shell's own session environment, with no `uwsm-app` of its own. The rows that
want a terminal or an app scope carry it in the text - `omarchy-launch-floating-
terminal-with-presentation …` does `setsid uwsm-app -- xdg-terminal-exec …`,
`trigger.share.receive` is `uwsm-app -- localsend`. So this adapter does the
same: the row's own text, `bash -lc`, the Omarchy shell's environment, and it
adds no wrapper of its own.

It differs in one place, deliberately: the process is started in a transient
systemd scope (`systemd-run --user --scope`). Omarchy's menu runs its children
inside the compositor's unit; ours would otherwise run inside `omodachid.service`
and be killed with it (`KillMode=control-group`) - an `omarchy-update` in a
floating terminal must not die because the daemon was reinstalled.

The security boundary is the one the rest of core keeps:

* Only the host's own menu source rows are run - Omarchy's default menu, the
  user's extension file and Omodachi's menu, exactly the text Omarchy itself
  runs as this user. A provider row (an app, a font, a keybinding record) is
  never one of them.
* A device names an `entry_id`. It cannot send a command, an argument or a
  parameter: the route has no parameter enums, so `params` must be empty, and
  the text that runs is re-read from the source catalog at execution time.
* The action text is never written to a log. The journal line says which row,
  which device, when, and what became of the process.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import time
from typing import Any, Callable, Mapping

from .graphical import GraphicalUnavailable, bounded_hyprctl, compositor_process
from .routes import RouteDescriptor
from .workspace_actions import WORKSPACE_LAYOUT_ENTRY

#: The fixed argv every menu-action route carries. It is a marker, like
#: `omodachi-keybinding`: the executor ignores it and reads the row again.
ROUTE_ARGV0 = "omodachi-menu-action"

#: How long a spawned row is given to fail before it is reported as running.
#: A launcher that forks returns in a few milliseconds; this window only has to
#: catch `command not found` (127). Same value, same reason, as SHORTCUT-1.
EXEC_SETTLE = 0.1

# ------------------------------------------------------------ classification
#
# Study 04 A-68. Which rows ask for a second tap. The rule is the product's,
# not a guess about each script: power and session, erasing, updating, and
# security or boot configuration. It is applied two ways - by the row's place
# in Omarchy's menu, and by what its action text says it runs - so a row a user
# adds under their own id is caught by the second.

#: `system.*` rows that end or suspend the session. `system.screensaver` is not
#: one: it is a picture, and any input ends it.
SESSION_IDS = frozenset({"system.lock", "system.suspend", "system.hibernate",
                         "system.logout", "system.reboot", "system.shutdown"})
#: Menu groups whose every row changes the machine: Remove erases, Update
#: replaces software, configuration, firmware, credentials and services.
CONFIRM_PREFIXES = ("remove.", "update.")
#: `setup.reset` is `omarchy-system-factory-reset`.
CONFIRM_IDS = frozenset({"setup.reset"})
#: Inside `update.*`, the two rows that change nothing by themselves:
#: `omarchy-menu-timezone` opens a picker on the host, and the pick is the
#: decision; `omarchy-restart-hyprsunset` restarts the user's own night-light
#: helper. Both read from the host's scripts on 2026-09-23 (report §3).
UPDATE_EXEMPT_COMMANDS = frozenset({"omarchy-menu-timezone", "omarchy-restart-hyprsunset"})

#: Commands that, wherever they appear in a row's action, change the machine.
CONFIRM_COMMANDS = frozenset({
    # power and session
    "omarchy-system-lock", "omarchy-system-logout", "omarchy-system-reboot", "omarchy-system-shutdown",
    "reboot", "poweroff", "shutdown", "halt",
    # erasing
    "omarchy-system-factory-reset", "omarchy-pkg-remove", "omarchy-webapp-remove", "omarchy-tui-remove",
    "omarchy-theme-remove", "omarchy-voxtype-remove",
    # updating, credentials
    "omarchy-update", "omarchy-update-firmware", "omarchy-update-time", "omarchy-channel-set",
    "omarchy-drive-password", "passwd",
    # security and boot configuration
    "omarchy-sudo-passwordless", "omarchy-setup-direct-boot", "omarchy-toggle-hybrid-gpu",
    # privilege, spelled out in the row itself
    "sudo", "pkexec", "doas",
})
CONFIRM_COMMAND_PREFIXES = ("omarchy-remove-", "omarchy-refresh-", "omarchy-setup-security-")
#: `systemctl` and `loginctl` verbs that end the session or power state.
POWER_VERBS = frozenset({"suspend", "hibernate", "hybrid-sleep", "suspend-then-hibernate", "reboot",
                         "poweroff", "halt", "kexec", "soft-reboot", "terminate-session",
                         "terminate-user", "kill-session", "kill-user", "lock-session", "lock-sessions"})
#: Service restarts that take the machine's audio, network or input away.
RESTART_COMMANDS = frozenset({"omarchy-restart-audio", "omarchy-restart-wifi",
                              "omarchy-restart-bluetooth", "omarchy-restart-trackpad",
                              "omarchy-restart-shell"})

#: Commands that read the terminal they were started from. Run as a row's
#: whole action with no terminal around them they would block on nothing, so
#: such a row stays grey (MENU-4 §3). On this host no row is one of these -
#: `passwd` is wrapped in the floating terminal - but a user row may be.
TTY_COMMANDS = frozenset({"passwd", "gum", "su", "read", "vi", "vim", "nvim", "nano", "htop", "btop",
                          "top", "less", "more", "man", "ssh"})
#: Wrappers that give the command after them a terminal.
TERMINAL_WRAPPERS = frozenset({"omarchy-launch-floating-terminal-with-presentation",
                               "omarchy-launch-or-focus-tui", "omarchy-launch-tui",
                               "omarchy-launch-terminal", "xdg-terminal-exec"})

REASON_EMPTY = "menu_action_empty"
REASON_NEEDS_TERMINAL = "menu_action_needs_terminal"

_SEPARATORS = re.compile(r"\|\||&&|[;|&\n()`]|\$\(|\bthen\b|\belse\b|\bdo\b")


def _commands(action: str) -> list[list[str]]:
    """The simple commands in an action, as lists of words, best effort.

    This is read, never executed: it only has to find command names, so a
    quote it cannot pair falls back to splitting on whitespace.
    """
    commands = []
    for part in _SEPARATORS.split(action):
        part = part.strip()
        if not part:
            continue
        try:
            words = shlex.split(part, posix=True)
        except ValueError:
            words = part.split()
        # `VAR=value cmd`, `exec cmd`, `setsid cmd`, `if cmd`, `! cmd`
        while words and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0])
                         or words[0] in {"exec", "setsid", "if", "elif", "while", "until", "!", "{", "nohup",
                                         "command", "builtin"}):
            words = words[1:]
        if words:
            commands.append(words)
    return commands


def _names(action: str) -> list[str]:
    """Every command name the action runs, including the one a wrapper runs."""
    names = []
    for words in _commands(action):
        names.append(os.path.basename(words[0]))
        if words[0] in TERMINAL_WRAPPERS or words[0] == "uwsm-app":
            rest = [word for word in words[1:] if not word.startswith("-")]
            for inner in rest[:1]:
                for inner_words in _commands(inner):
                    names.append(os.path.basename(inner_words[0]))
    return names


def confirm_reason(entry_id: str, action: str) -> str | None:
    """Why this row asks for a second tap (A-68), or None when it does not."""
    names = _names(action)
    first = names[0] if names else ""
    if entry_id in SESSION_IDS:
        return "session"
    if entry_id in CONFIRM_IDS:
        return "erase"
    if entry_id.startswith(CONFIRM_PREFIXES[0]):
        return "erase"
    if entry_id.startswith(CONFIRM_PREFIXES[1]):
        return None if first in UPDATE_EXEMPT_COMMANDS else "update"
    for words in _commands(action):
        # `sudo pacman -R …` is pacman removing something, said with privilege.
        while len(words) > 1 and os.path.basename(words[0]) in {"sudo", "doas", "pkexec"}:
            words = [word for word in words[1:]]
            while len(words) > 1 and words[0].startswith("-"):
                words = words[1:]
        name = os.path.basename(words[0])
        if name in {"systemctl", "loginctl"} and any(word in POWER_VERBS for word in words[1:]):
            return "session"
        if name == "pacman" and any(word.startswith("-R") or word == "--remove" for word in words[1:]):
            return "erase"
        if name == "rm" and any(word.startswith("-") and "r" in word for word in words[1:]):
            return "erase"
    for name in names:
        if name in CONFIRM_COMMANDS or name in RESTART_COMMANDS or name.startswith(CONFIRM_COMMAND_PREFIXES):
            return "system"
    return None


def classify(entry_id: str, action: Any) -> dict[str, Any]:
    """`{"runnable": bool, "reason": str|None, "confirm": bool, "confirm_reason": str|None}`."""
    if not isinstance(action, str) or not action.strip():
        return {"runnable": False, "reason": REASON_EMPTY, "confirm": False, "confirm_reason": None}
    commands = _commands(action)
    if len(commands) == 1 and os.path.basename(commands[0][0]) in TTY_COMMANDS:
        return {"runnable": False, "reason": REASON_NEEDS_TERMINAL, "confirm": False, "confirm_reason": None}
    why = confirm_reason(entry_id, action)
    return {"runnable": True, "reason": None, "confirm": why is not None, "confirm_reason": why}


# --------------------------------------------------------------- environment

#: Keys that belong to the shell's own systemd unit or to Quickshell, not to
#: the session a child should see.
_UNIT_KEYS = frozenset({"INVOCATION_ID", "JOURNAL_STREAM", "NOTIFY_SOCKET", "MANAGERPID", "MANAGERPIDFDID",
                        "SYSTEMD_EXEC_PID", "MEMORY_PRESSURE_WATCH", "MEMORY_PRESSURE_WRITE", "LISTEN_PID",
                        "LISTEN_FDS", "LISTEN_FDNAMES", "WATCHDOG_PID", "WATCHDOG_USEC", "_", "SHLVL",
                        "PWD", "OLDPWD"})


def _environ(process: Path) -> dict[str, str] | None:
    try:
        raw = (process / "environ").read_bytes()
    except OSError:
        return None
    if len(raw) > 262144:
        return None
    env = {}
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        try:
            env[key.decode()] = value.decode()
        except UnicodeDecodeError:
            continue
    return env


def _omarchy_shell(proc: Path, uid: int) -> Path | None:
    """The one live Omarchy shell of this user - the process `runAction` runs in."""
    found = []
    try:
        entries = list(proc.iterdir())
    except OSError:
        return None
    for process in entries:
        try:
            if not process.name.isdigit() or process.stat().st_uid != uid:
                continue
            if (process / "comm").read_text().strip() not in {"quickshell", "qs"}:
                continue
            argv = [arg for arg in (process / "cmdline").read_bytes().split(b"\0") if arg]
            # Launched as `quickshell -n -p /usr/share/omarchy/shell`; after it
            # relaunches itself from a signal handler, as a bare
            # `/usr/bin/quickshell` (PERF-5 follow-up). A plugin's own shell
            # names its own path and is neither.
            if not (any(arg.rstrip(b"/") == b"/usr/share/omarchy/shell" for arg in argv)
                    or argv in ([b"/usr/bin/quickshell"], [b"quickshell"])):
                continue
            found.append(process)
        except (OSError, UnicodeError):
            continue
    return found[0] if len(found) == 1 else None


def menu_environment(proc: Path = Path("/proc"), uid: int | None = None,
                     runtime: Path | None = None) -> dict[str, str]:
    """The environment Omarchy's menu gives a row: the Omarchy shell's own.

    `Quickshell.execDetached` passes the shell's environment through, so that
    is what is read - the whole of it, including the session variables Hyprland
    set with `env =` (which never appear in Hyprland's own `/proc` entry) and the
    `GUM_*` styling the installers draw with. The compositor's session keys are
    laid over it, so a shell that outlived a compositor restart cannot point a
    row at a socket that is gone. With no shell running (it restarts whenever
    the monitors move) the compositor's own environment is used instead.
    """
    uid = os.getuid() if uid is None else uid
    compositor = compositor_process(proc, uid, runtime)
    if compositor is None:
        raise GraphicalUnavailable("graphical_session_unavailable")
    compositor_path, session = compositor
    shell = _omarchy_shell(proc, uid)
    env = (_environ(shell) if shell is not None else None) or _environ(compositor_path) or {}
    env = {key: value for key, value in env.items() if key not in _UNIT_KEYS and not key.startswith("QS_")}
    for key in ("HYPRLAND_INSTANCE_SIGNATURE", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY"):
        env[key] = session[key]
    home = str(Path.home())
    env.setdefault("HOME", home)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={session['XDG_RUNTIME_DIR']}/bus")
    parts = [part for part in env.get("PATH", "").split(":") if part]
    for directory in ("/usr/share/omarchy/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        if directory not in parts:
            parts.append(directory)
    env["PATH"] = ":".join(parts)
    env.setdefault("OMARCHY_PATH", "/usr/share/omarchy")
    return env


# -------------------------------------------------------------------- spawn

def _scope_name(entry_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", entry_id)[:64] or "row"
    return f"omodachi-menu-{slug}-{secrets.token_hex(4)}"


def spawn(action: str, env: Mapping[str, str], entry_id: str, *, sleeper: Callable[[float], None] = time.sleep,
          systemd_run: str | None = None, settle: float = EXEC_SETTLE) -> dict[str, Any]:
    """`bash -lc <action>`, detached, in its own scope; what became of it in 0.1 s."""
    command = ["/bin/bash", "-lc", action]
    runner = systemd_run if systemd_run is not None else shutil.which("systemd-run", path=env.get("PATH"))
    if runner:
        command = [runner, "--user", "--scope", "--quiet", "--collect",
                   "--unit", _scope_name(entry_id), "--"] + command
    process = subprocess.Popen(command, env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True,
                               cwd=env.get("HOME") or "/")
    deadline = time.monotonic() + settle
    while time.monotonic() < deadline and process.poll() is None:
        sleeper(0.01)
    code = process.poll()
    return {"pid": process.pid, "exited": code is not None, "exit_code": code}


def _journal(entry: Mapping[str, Any]) -> None:
    """systemd captures the daemon's stdout, so this *is* the journal line."""
    try:
        print(json.dumps({"omodachi": "menu_action", **entry}, separators=(",", ":"), default=str), flush=True)
    except (OSError, ValueError):
        pass


# ------------------------------------------------------------------ adapter

class MenuActionAdapter:
    """Registers a host route for every runnable menu source row nobody else owns."""

    def __init__(self, service, *, environment: Callable[[], Mapping[str, str]] = menu_environment,
                 spawner: Callable[..., dict[str, Any]] | None = None,
                 runner: Callable[..., str] = bounded_hyprctl,
                 journal: Callable[[Mapping[str, Any]], None] = _journal,
                 clock: Callable[[], float] = time.time) -> None:
        self.service = service
        self.environment = environment
        self.spawner = spawner or (lambda action, env, entry_id: spawn(action, env, entry_id))
        self.runner = runner
        self.journal = journal
        self.clock = clock
        #: entry_id -> (descriptor as registered, action it was registered for, executor)
        self.owned: dict[str, tuple[RouteDescriptor, str, Callable]] = {}
        self.declined: dict[str, str] = {}
        self._synced = None

    # -- registration

    def _retire(self, entry_id: str) -> None:
        descriptor, _action, executor = self.owned.pop(entry_id)
        self.service.policy.unregister(entry_id, expected=descriptor)
        if self.service._executors.get(entry_id, (None,))[0] is executor:
            self.service._executors.pop(entry_id, None)
            self.service._device_executors.discard(entry_id)

    def sync(self, *, force: bool = False) -> None:
        """Bring the registrations in line with the current menu sources."""
        catalog = self.service.runtime.catalog
        if not force and catalog is self._synced:
            return
        policy = self.service.policy
        wanted: dict[str, tuple[str, dict[str, Any]]] = {}
        declined: dict[str, tuple[str, str]] = {}
        for entry in catalog.entries:
            if entry.kind != "action" or entry.provider or entry.surface or entry.target:
                continue
            # The workspace-layout toggle carries a workspace binding the
            # service insists on (WORKSPACE-LAYOUT); only its own adapter runs it.
            if entry.id == WORKSPACE_LAYOUT_ENTRY:
                continue
            action = entry.action if isinstance(entry.action, str) else ""
            verdict = classify(entry.id, action)
            if verdict["runnable"]:
                wanted[entry.id] = (action, verdict)
            else:
                declined[entry.id] = (action, verdict["reason"])
        # Registrations someone else replaced are theirs now.
        for entry_id in list(self.owned):
            if policy._adapters.get(entry_id) is not self.owned[entry_id][0]:
                _descriptor, _action, executor = self.owned.pop(entry_id)
                if self.service._executors.get(entry_id, (None,))[0] is executor:
                    self.service._executors.pop(entry_id, None)
                    self.service._device_executors.discard(entry_id)
        for entry_id in list(self.owned):
            action, verdict = wanted.get(entry_id, (None, None))
            _descriptor, owned_action, _executor = self.owned[entry_id]
            if action is None or action != owned_action or bool(verdict["confirm"]) != self.owned[entry_id][0].confirm:
                self._retire(entry_id)
        for entry_id, (action, verdict) in wanted.items():
            if entry_id in self.owned or entry_id in policy._adapters:
                continue
            descriptor = RouteDescriptor("host", True, argv=(ROUTE_ARGV0, entry_id), entry_id=entry_id,
                                         confirm=verdict["confirm"])
            policy.register(entry_id, descriptor, source_action=action)
            def executor(argv, *, device=None, owner=self, entry_id=entry_id):
                return owner.perform(entry_id, device)
            self.service.register_executor(entry_id, executor, with_device=True)
            self.owned[entry_id] = (policy._adapters[entry_id], action, executor)
        for entry_id in list(self.declined):
            if entry_id not in declined:
                policy.undecline(entry_id)
                self.declined.pop(entry_id)
        for entry_id, (action, reason) in declined.items():
            policy.decline(entry_id, reason, source_action=action)
            self.declined[entry_id] = reason
        self._synced = catalog

    # -- execution

    def _probe(self, env):
        """`activeworkspace` and `activewindow`, or nulls. Never raises."""
        from .shortcut_provider import _window_view, _workspace_view
        result = {"workspace": None, "window": None}
        for key, query in (("workspace", "activeworkspace"), ("window", "activewindow")):
            try:
                value = json.loads(self.runner(("/usr/bin/hyprctl", "-j", query), env))
            except Exception:
                continue
            result[key] = _workspace_view(value) if key == "workspace" else _window_view(value)
        return result

    def perform(self, entry_id: str, device: str | None) -> dict[str, Any]:
        """Run one row and answer with what the host did (`observed`)."""
        from .service import ServiceError
        owned = self.owned.get(entry_id)
        current = self.service.runtime.catalog.by_id(entry_id)
        # The text that runs is the source row's, read now - never the text
        # the route was registered against, and never anything the client sent.
        if owned is None or current is None or current.action != owned[1]:
            raise ServiceError("stale_target", status=409)
        action = owned[1]
        started = self.clock()
        record = {"entry_id": entry_id, "device": device, "at": round(started, 3),
                  "confirm": owned[0].confirm}
        try:
            env = dict(self.environment())
        except GraphicalUnavailable:
            self.journal({**record, "status": "failed", "code": "graphical_session_unavailable"})
            raise ServiceError("graphical_session_unavailable", status=503) from None
        before = self._probe(env)
        try:
            process = self.spawner(action, env, entry_id)
        except OSError:
            self.journal({**record, "status": "failed", "code": "execution_failed"})
            raise ServiceError("execution_failed", status=503) from None
        after = self._probe(env)
        observed = {"kind": "exec", "before": before, "process": process, "after": after,
                    "changed": after != before}
        code = process.get("exit_code") if process.get("exited") else None
        failed = code in (126, 127)
        self.journal({**record, "status": "failed" if failed else "accepted", "pid": process.get("pid"),
                      "exited": process.get("exited"), "exit_code": code,
                      "ms": round((self.clock() - started) * 1000)})
        if failed:
            raise ServiceError("executable_missing", status=503)
        return observed


def install_menu_action_adapter(service, **options) -> MenuActionAdapter:
    """Attach after every reviewed adapter, so theirs keep the rows they own."""
    adapter = MenuActionAdapter(service, **options)
    service.menu_actions = adapter
    adapter.sync(force=True)
    return adapter
