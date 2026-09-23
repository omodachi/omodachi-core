"""MENU-3. Every menu `when`/`checked`/`disabled` is answered, and none of them loops."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from omodachi_core.catalog import compile_catalog
from omodachi_core.catalog_runtime import CatalogRuntime
from omodachi_core.conditions import (ConditionEngine, PathWatcher, Triggers, classify, known, run_bash,
                                      unknown, watch_points)
from omodachi_core.hub import Hub
from omodachi_core.resources import ResourceMonitor
from omodachi_core.routes import RouteDescriptor
from omodachi_core.service import CoreService, ServiceError

HOME = Path("/home/tester")


class FakeBash:
    """Stands in for `bash -c`: a table of answers, and a record of every spawn."""

    def __init__(self, answers=None, *, delay=0.0):
        self.answers = dict(answers or {})
        self.calls: list[str] = []
        self.delay = delay
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, expression, environment):
        with self._lock:
            self.calls.append(expression)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            answer = self.answers.get(expression, 1)
            if answer == "timeout":
                return unknown("condition_timeout")
            if answer == "spawn":
                return unknown("condition_spawn_failed")
            return known(answer == 0)
        finally:
            with self._lock:
                self.active -= 1


class FakeWatcher:
    """What `PathWatcher` looks like to the engine, with the events in our hands."""

    available = True

    def __init__(self):
        self.on_change = None
        self.patterns = {}

    def watch(self, patterns):
        self.patterns = dict(patterns)

    def fire(self, path):
        keys = [key for key, paths in self.patterns.items() if path in paths]
        self.on_change(keys)
        return keys

    def close(self):
        pass


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def engine(bash=None, *, watcher=None, clock=None, **options):
    return ConditionEngine(runner=bash or FakeBash(), environment=lambda: {"HOME": str(HOME)},
                           clock=clock or Clock(), watcher=watcher, home=HOME, log=lambda *_: None, **options)


# ------------------------------------------------------------------ classify

class ClassificationTests(unittest.TestCase):
    """The host's real expressions (Omarchy 4.0.4 menu, 2026-09-23)."""

    def kind(self, expression):
        return classify(expression, HOME)

    def test_file_tests_are_watched_on_the_path_they_name(self):
        found = self.kind("[[ ! -d $HOME/.local/share/mise/installs/elixir ]]")
        self.assertEqual(found, Triggers(paths=("/home/tester/.local/share/mise/installs/elixir",)))
        self.assertEqual(self.kind("[[ -f ~/.config/hypr/input.lua ]]").paths, ("/home/tester/.config/hypr/input.lua",))
        self.assertEqual(self.kind('[[ -f "$HOME/.local/share/applications/Xbox Cloud Gaming.desktop" ]]').paths,
                         ("/home/tester/.local/share/applications/Xbox Cloud Gaming.desktop",))
        self.assertEqual(self.kind('compgen -G "$HOME/.config/omarchy/plugins/*/manifest.json"').paths,
                         ("/home/tester/.config/omarchy/plugins/*/manifest.json",))

    def test_grep_over_files_is_the_files(self):
        found = self.kind("[[ -f ~/.config/chromium-flags.conf ]] && ! grep -q oauth2-client-id ~/.config/chromium-flags.conf")
        self.assertEqual(found, Triggers(paths=("/home/tester/.config/chromium-flags.conf",)))
        found = self.kind("grep -qE '^Exec=.*(omarchy-launch-webapp|omarchy-webapp-handler)' $HOME/.local/share/applications/*.desktop")
        self.assertEqual(found.paths, ("/home/tester/.local/share/applications/*.desktop",))
        self.assertFalse(found.demand)

    def test_helpers_that_are_a_file_test_are_read_as_one(self):
        self.assertEqual(self.kind("! omarchy-toggle-enabled suspend-off").paths,
                         ("/home/tester/.local/state/omarchy/toggles/suspend-off",))
        self.assertEqual(self.kind("omarchy-sudo-docker --configured").paths, ("/etc/group",))
        self.assertIn("/var/lib/flatpak/app/com.nvidia.geforcenow", self.kind("! flatpak info com.nvidia.geforcenow").paths)
        self.assertFalse(self.kind("omarchy-theme-extras").demand)

    def test_packages_are_the_package_database(self):
        self.assertEqual(self.kind("! omarchy-pkg-present openclaw").paths, ("/var/lib/pacman/local/*",))
        found = self.kind("omarchy-cmd-present kitty")
        self.assertIn("/usr/bin/kitty", found.paths)
        self.assertIn("/home/tester/.local/bin/kitty", found.paths)

    def test_processes_and_commands_are_read_on_demand(self):
        for expression in ("pgrep -f '^gpu-screen-recorder'", "systemctl is-enabled --quiet sshd",
                           "[[ $(omarchy-network-status) == wifi* ]]",
                           '[[ "$(omarchy-default-browser)" == "chromium" ]]',
                           '[[ "$(dell-xps-touchpad-haptics get)" == "low" ]]'):
            self.assertTrue(self.kind(expression).demand, expression)

    def test_hardware_and_the_root_filesystem_do_not_move(self):
        for expression in ("omarchy-hw-laptop", "[[ $(findmnt -no FSTYPE /) == btrfs ]]",
                           "omarchy-hibernation-available"):
            self.assertEqual(self.kind(expression).kind, "static", expression)

    def test_anything_not_understood_is_demand_never_static(self):
        for expression in ("[[ -f $XDG_CONFIG_HOME/x ]]", "[[ -f relative/path ]]", "cat <(ls)",
                           "echo $(echo $(nested))", "unknown-command --flag", "[[ -f /x"):
            self.assertTrue(self.kind(expression).demand, expression)

    def test_a_mixed_expression_keeps_every_trigger(self):
        found = self.kind("omarchy-hw-dell-xps-haptic-touchpad && omarchy-cmd-present dell-xps-touchpad-haptics")
        self.assertFalse(found.demand)
        self.assertIn("/usr/bin/dell-xps-touchpad-haptics", found.paths)


class WatchPointTests(unittest.TestCase):
    def test_the_first_missing_ancestor_is_watched_until_it_appears(self):
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "Games", "battlenet")
            self.assertEqual(watch_points(target), [(root, "Games")])
            os.mkdir(os.path.join(root, "Games"))
            self.assertEqual(watch_points(target), [(os.path.join(root, "Games"), "battlenet")])

    def test_a_glob_directory_is_watched_and_each_match_descended_into(self):
        with tempfile.TemporaryDirectory() as root:
            plugins = os.path.join(root, "plugins")
            os.makedirs(os.path.join(plugins, "one"))
            points = watch_points(os.path.join(plugins, "*", "manifest.json"))
            self.assertIn((plugins, "*"), points)
            self.assertIn((os.path.join(plugins, "one"), "manifest.json"), points)


# -------------------------------------------------------------------- engine

class EngineTests(unittest.TestCase):
    def test_exit_status_is_the_answer_and_no_answer_is_unknown(self):
        bash = FakeBash({"yes": 0, "no": 1, "slow": "timeout", "broken": "spawn"})
        conditions = engine(bash)
        for key in bash.answers:
            conditions.track(key, Triggers(demand=True), shell=True)
        conditions.evaluate({key: None for key in bash.answers})
        self.assertEqual(conditions.reading("yes"), {"status": "available", "value": True})
        self.assertEqual(conditions.reading("no"), {"status": "available", "value": False})
        self.assertEqual(conditions.reading("slow"), {"status": "unknown", "value": None, "reason": "condition_timeout"})
        self.assertEqual(conditions.reading("broken")["status"], "unknown")

    def test_never_more_than_eight_shells_at_once(self):
        bash = FakeBash(delay=0.02)
        conditions = engine(bash)
        keys = [f"[[ -d /x/{index} ]]" for index in range(40)]
        conditions.evaluate({key: None for key in keys})
        self.assertEqual(len(bash.calls), 40)
        self.assertLessEqual(bash.peak, 8)
        self.assertGreater(bash.peak, 1)
        self.assertEqual(conditions.shells_total, 40)

    def test_an_adapter_is_the_fast_path_and_never_a_shell(self):
        bash = FakeBash()
        conditions = engine(bash)
        conditions.evaluate({"omarchy-pkg-present x": lambda: True})
        self.assertEqual(bash.calls, [])
        self.assertEqual(conditions.reading("omarchy-pkg-present x")["value"], True)

    def test_demand_waits_ten_seconds_and_ignores_file_and_static_rows(self):
        clock = Clock()
        conditions = engine(clock=clock, watcher=FakeWatcher())
        conditions.track("pgrep x", shell=True)
        conditions.track("[[ -f /etc/x ]]", shell=True)
        conditions.track("omarchy-hw-laptop", shell=True)
        conditions.evaluate({key: None for key in conditions.known_keys()})
        clock.now += 5
        self.assertEqual(conditions.demand(), [])
        clock.now += 6
        self.assertEqual(conditions.demand(), ["pgrep x"])

    def test_without_a_watcher_a_file_row_falls_back_to_demand(self):
        clock = Clock()
        conditions = engine(clock=clock, watcher=None)
        conditions.track("[[ -f /etc/x ]]", shell=True)
        conditions.evaluate({"[[ -f /etc/x ]]": None})
        clock.now += 11
        self.assertEqual(conditions.demand(), ["[[ -f /etc/x ]]"])

    def test_a_path_event_owes_a_reading_after_the_burst_settles(self):
        clock = Clock()
        watcher = FakeWatcher()
        conditions = engine(clock=clock, watcher=watcher)
        key = "[[ -d $HOME/.local/share/mise/installs/ruby ]]"
        conditions.track(key, shell=True)
        conditions.evaluate({key: None})
        self.assertEqual(conditions.due_keys(), [])
        self.assertEqual(watcher.fire("/home/tester/.local/share/mise/installs/ruby"), [key])
        self.assertEqual(conditions.due_keys(), [], "still settling")
        clock.now += 1.5
        self.assertEqual(conditions.due_keys(), [key])


class RealBashTests(unittest.TestCase):
    def test_bash_true_false_and_a_timeout(self):
        env = {"HOME": tempfile.gettempdir(), "PATH": "/usr/bin:/bin"}
        self.assertEqual(run_bash("true", env), known(True))
        self.assertEqual(run_bash("[[ -d / ]] && ! false", env), known(True))
        self.assertEqual(run_bash("exit 3", env), known(False))
        started = time.monotonic()
        self.assertEqual(run_bash("sleep 5", env, timeout=0.3), unknown("condition_timeout"))
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(run_bash('[[ "$HOME" == "' + env["HOME"] + '" ]]', env), known(True))


@unittest.skipUnless(sys.platform.startswith("linux"), "inotify")
class InotifyTests(unittest.TestCase):
    def test_a_directory_appearing_is_seen(self):
        with tempfile.TemporaryDirectory() as root:
            seen = []
            watcher = PathWatcher()
            watcher.on_change = seen.extend
            target = os.path.join(root, "installs", "ruby")
            watcher.watch({"key": (target,)})
            try:
                os.makedirs(target)
                deadline = time.monotonic() + 3
                while "key" not in seen and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertIn("key", seen)
                seen.clear()
                os.rmdir(target)                       # watched in its parent now
                deadline = time.monotonic() + 3
                while "key" not in seen and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertIn("key", seen)
            finally:
                watcher.close()


# ------------------------------------------------------------------- runtime

def menu(**rows):
    return compile_catalog({"install": {"label": "Install"}, "trigger": {"label": "Trigger"}, **rows})


class RuntimeTests(unittest.TestCase):
    def build(self, rows, *, bash=None, clock=None, watcher=None):
        self.bash = bash or FakeBash()
        self.clock = clock or Clock()
        runtime = CatalogRuntime(menu(**rows), cache_seconds=15.0, clock=self.clock)
        runtime.attach_conditions(engine(self.bash, clock=self.clock, watcher=watcher or FakeWatcher()))
        return runtime

    @staticmethod
    def row(snapshot, entry_id):
        return next(row for row in snapshot["entries"] if row["id"] == entry_id)

    def test_what_was_condition_adapter_unavailable_now_has_an_answer(self):
        runtime = self.build({"install.ruby": {"action": "x", "when": "[[ ! -d $HOME/.local/share/mise/installs/ruby ]]"},
                              "remove.ruby": {"action": "x", "when": "[[ -d $HOME/.local/share/mise/installs/ruby ]]"}},
                             bash=FakeBash({"[[ ! -d $HOME/.local/share/mise/installs/ruby ]]": 0,
                                            "[[ -d $HOME/.local/share/mise/installs/ruby ]]": 1}))
        snapshot = runtime.refresh()
        self.assertEqual(self.row(snapshot, "install.ruby")["conditions"]["when"], {"status": "available", "value": True})
        self.assertIs(self.row(snapshot, "install.ruby")["visible"], True)
        self.assertIs(self.row(snapshot, "remove.ruby")["visible"], False)
        self.assertNotIn("condition_adapter_unavailable", str(snapshot))

    def test_a_timeout_is_unknown_and_the_row_stays_drawn(self):
        runtime = self.build({"trigger.stop": {"action": "x", "when": "pgrep -f slow"}},
                             bash=FakeBash({"pgrep -f slow": "timeout"}))
        row = self.row(runtime.refresh(), "trigger.stop")
        self.assertEqual(row["conditions"]["when"]["status"], "unknown")
        self.assertIsNone(row["visible"])

    def test_the_catalog_is_evaluated_once_and_then_left_alone(self):
        """PERF-4's loop: a refresh, an invalidate, a tick - none of them re-runs a shell."""
        runtime = self.build({"install.go": {"action": "x", "when": "[[ ! -d $HOME/go ]]"},
                              "trigger.stop": {"action": "x", "when": "pgrep -f recorder"},
                              "install.zig": {"action": "x", "checked": "[[ -d $HOME/zig ]]"}})
        runtime.refresh()
        self.assertEqual(sorted(self.bash.calls), sorted(["[[ -d $HOME/zig ]]", "[[ ! -d $HOME/go ]]", "pgrep -f recorder"]))
        self.bash.calls.clear()
        for _ in range(5):
            self.clock.now += 20                        # past every cache window
            runtime.warm()
            runtime.refresh()
            runtime.refresh(invalidate=True)
        self.assertEqual(self.bash.calls, [])

    def test_a_menu_opening_re_reads_only_what_only_a_look_can_tell(self):
        runtime = self.build({"install.go": {"action": "x", "when": "[[ ! -d $HOME/go ]]"},
                              "trigger.stop": {"action": "x", "when": "pgrep -f recorder"}})
        runtime.refresh()
        self.bash.calls.clear()
        self.clock.now += 11
        self.assertEqual(runtime.demand(), ["pgrep -f recorder"])
        runtime.warm()
        self.assertEqual(self.bash.calls, ["pgrep -f recorder"])
        self.bash.calls.clear()
        self.clock.now += 3                             # inside the 10 s window
        self.assertEqual(runtime.demand(), [])
        runtime.warm()
        self.assertEqual(self.bash.calls, [])

    def test_a_watched_path_changing_re_reads_that_row_and_republishes(self):
        watcher = FakeWatcher()
        bash = FakeBash({"[[ -d $HOME/.rustup ]]": 1})
        runtime = self.build({"remove.rust": {"action": "x", "when": "[[ -d $HOME/.rustup ]]"},
                              "install.go": {"action": "x", "when": "[[ ! -d $HOME/go ]]"}},
                             bash=bash, watcher=watcher)
        first = runtime.refresh()
        self.assertIs(self.row(first, "remove.rust")["visible"], False)
        bash.calls.clear()
        bash.answers["[[ -d $HOME/.rustup ]]"] = 0     # rustup installed
        watcher.fire("/home/tester/.rustup")
        self.clock.now += 2
        runtime.warm()
        self.assertEqual(bash.calls, ["[[ -d $HOME/.rustup ]]"])
        second = runtime.refresh()
        self.assertIs(self.row(second, "remove.rust")["visible"], True)
        self.assertNotEqual(first["revision"], second["revision"])

    def test_invalidate_row_owes_its_shell_to_the_worker_not_the_tap(self):
        runtime = self.build({"trigger.stop": {"action": "x", "when": "pgrep -f recorder"}})
        runtime.refresh()
        self.bash.calls.clear()
        self.assertTrue(runtime.invalidate_row("trigger.stop"))
        runtime.refresh()                               # the invoke path, on the loop
        self.assertEqual(self.bash.calls, [])
        runtime.warm()                                  # the maintenance tick, on a worker
        self.assertEqual(self.bash.calls, ["pgrep -f recorder"])

    def test_after_invoke_owes_the_groups_demand_rows_and_nothing_else(self):
        runtime = self.build({"trigger.record": {"action": "x"},
                              "trigger.stop": {"action": "y", "when": "pgrep -f recorder"},
                              "trigger.gaps": {"action": "z", "when": "[[ -f $HOME/gaps ]]"},
                              "install.go": {"action": "x", "when": "systemctl is-enabled go"}})
        runtime.refresh()
        self.bash.calls.clear()
        runtime.after_invoke("trigger.record")
        runtime.warm()
        self.assertEqual(self.bash.calls, ["pgrep -f recorder"])

    def test_a_reviewed_adapter_answers_its_expression_without_bash(self):
        runtime = self.build({"install.kitty": {"action": "x", "when": "! omarchy-pkg-present kitty"}})
        runtime.register_condition("! omarchy-pkg-present kitty", lambda: False)
        row = self.row(runtime.refresh(), "install.kitty")
        self.assertIs(row["visible"], False)
        self.assertEqual(self.bash.calls, [])

    def test_a_provider_rows_expression_is_never_handed_to_bash(self):
        runtime = self.build({"apps": {"provider": "apps-provider"}})
        runtime.register_provider("apps-provider", lambda: [{"id": "apps.evil", "label": "x", "when": "touch /tmp/pwned"}])
        row = self.row(runtime.refresh(), "apps.evil")
        self.assertEqual(row["conditions"]["when"]["reason"], "condition_adapter_unavailable")
        self.assertEqual(self.bash.calls, [])

    def test_disabled_is_an_expression_too(self):
        runtime = self.build({"install.x": {"action": "x", "disabled": "[[ -f $HOME/lock ]]"}},
                             bash=FakeBash({"[[ -f $HOME/lock ]]": 0}))
        row = self.row(runtime.refresh(), "install.x")
        self.assertEqual(row["conditions"]["disabled"], {"status": "available", "value": True})
        self.assertIs(row["visible"], True)

    def test_a_source_edit_forgets_the_rows_it_removed(self):
        runtime = self.build({"install.go": {"action": "x", "when": "[[ ! -d $HOME/go ]]"}})
        runtime.refresh()
        runtime.catalog = menu(**{"install.zig": {"action": "x", "when": "[[ ! -d $HOME/zig ]]"}})
        runtime.warm()
        self.assertNotIn("[[ ! -d $HOME/go ]]", runtime.engine.known_keys())
        self.assertIn("[[ ! -d $HOME/zig ]]", runtime.engine.known_keys())

    def test_without_an_engine_nothing_changes(self):
        runtime = CatalogRuntime(menu(**{"install.go": {"action": "x", "when": "[[ ! -d $HOME/go ]]"}}))
        row = self.row(runtime.refresh(), "install.go")
        self.assertEqual(row["conditions"]["when"]["reason"], "condition_adapter_unavailable")


# ------------------------------------------------------------------- service

class ServiceTests(unittest.TestCase):
    def service(self, rows, answers):
        self.bash = FakeBash(answers)
        self.clock = Clock()
        runtime = CatalogRuntime(menu(**rows), cache_seconds=15.0, clock=self.clock)
        hub = Hub()
        service = CoreService(hub, runtime=runtime)
        conditions = engine(self.bash, clock=self.clock, watcher=FakeWatcher())
        runtime.attach_conditions(conditions)
        hub.condition_engine = conditions
        self.calls = []
        for entry_id in rows:
            service.policy.register(entry_id, RouteDescriptor("host", True, argv=(rows[entry_id]["action"],)))
            service.register_executor(entry_id, self.calls.append)
        return service

    def invoke(self, service, entry_id, request_id="r"):
        revision = service.refresh_catalog()["revision"]
        return service.invoke({"entry_id": entry_id, "request_id": request_id, "catalog_revision": revision}, "phone")

    def test_an_unknown_row_can_be_tapped_and_a_false_one_cannot(self):
        service = self.service({"trigger.stop": {"action": "stop", "when": "pgrep -f slow"},
                                "trigger.hidden": {"action": "hide", "when": "pgrep -f absent"}},
                               {"pgrep -f slow": "timeout", "pgrep -f absent": 1})
        self.assertEqual(self.invoke(service, "trigger.stop")["status"], "accepted")
        self.assertEqual(self.calls, [("stop",)])
        with self.assertRaises(ServiceError) as refused:
            self.invoke(service, "trigger.hidden", "r2")
        self.assertEqual(refused.exception.code if hasattr(refused.exception, "code") else str(refused.exception),
                         "route_unavailable")

    def test_disabled_true_greys_the_row_with_a_reason(self):
        service = self.service({"install.x": {"action": "x", "disabled": "[[ -f $HOME/lock ]]"}},
                               {"[[ -f $HOME/lock ]]": 0})
        row = next(row for row in service.refresh_catalog()["entries"] if row["id"] == "install.x")
        self.assertIs(row["route"]["ready"], False)
        self.assertEqual(row["route"]["readiness_reason"], "condition_disabled")

    def test_reading_the_catalog_is_a_menu_opening(self):
        service = self.service({"trigger.stop": {"action": "stop", "when": "pgrep -f recorder"}},
                               {"pgrep -f recorder": 0})
        service.refresh_catalog()
        self.bash.calls.clear()
        self.clock.now += 11
        service.search_catalog("")                      # GET /v1/catalog
        self.assertEqual(self.bash.calls, ["pgrep -f recorder"])
        self.bash.calls.clear()
        service.search_catalog("")                      # again, inside ten seconds
        self.assertEqual(self.bash.calls, [])

    def test_health_counts_the_shells(self):
        service = self.service({"trigger.stop": {"action": "stop", "when": "pgrep -f recorder"}},
                               {"pgrep -f recorder": 0})
        service.refresh_catalog()
        reading = ResourceMonitor(service.hub).snapshot()
        self.assertEqual(reading["condition_shells_total"], 1)
        self.assertEqual(reading["condition_shells_5m"], 1)
        self.assertIn("shells_5m=1", ResourceMonitor(service.hub).line(reading))


if __name__ == "__main__":
    unittest.main()
