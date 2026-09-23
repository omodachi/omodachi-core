from pathlib import Path
import json

from omodachi_core.catalog import (
    compile_catalog,
    compile_catalog_from_jsonc,
    loads_jsonc,
    parse_script_annotations,
    validate_action_params,
)
from omodachi_core.routes import RouteDescriptor, RoutePolicy, describe_route

ROOT = Path(__file__).parents[1]
FIX = ROOT / "contracts" / "fixtures" / "catalog"


def test_jsonc_comments_trailing_commas_and_string_markers():
    obj = loads_jsonc('{"label":"https://example/x//y", /* c */ "v":[1,2,],}')
    assert obj == {"label": "https://example/x//y", "v": [1, 2]}


def test_three_layer_merge_same_id_and_stable_order():
    cat = compile_catalog_from_jsonc(
        FIX / "default-omarchy-menu.jsonc",
        FIX / "user-omarchy-menu.jsonc",
        FIX / "omodachi-menu.jsonc",
    )
    ids = [e.id for e in cat.entries]
    assert ids[:5] == ["root", "apps", "trigger", "trigger.toggle.notifications", "learn.herdr-keybindings"]
    assert ids[-4:] == ["omodachi", "omodachi.desktop", "omodachi.agent", "omodachi.herdr"]
    notifications = cat.by_id("trigger.toggle.notifications")
    assert notifications.label == "Quiet notifications"
    # MenuModel normalizes every layer before merge; omitted override fields
    # therefore become their explicit defaults rather than inheriting silently.
    assert notifications.icon == ""
    assert notifications.action == ""  # omitted by override layer -> normalized default
    assert notifications.aliases == ("quiet", "notify")
    assert notifications.checked == "omarchy-state is-set quiet-notifications"
    assert cat.by_id("setup.monitors").surface == "native:display-settings"
    assert len(cat.revision) == 16


def test_catalog_keeps_provider_conditions_and_unknown_fields():
    cat = compile_catalog(
        {"a": {"label": "A", "provider": {"kind": "apps"}, "when": "true", "checked": "false", "x": {"n": 1}}}
    )
    row = cat.by_id("a").as_dict()
    assert row["provider"] == {"kind": "apps"}
    assert row["when"] == "true"
    assert row["checked"] == "false"
    assert row["x"] == {"n": 1}


def test_routes_surface_and_known_wrappers():
    assert describe_route("omodachi.desktop", None, surface="desktop").route == "desktop"
    assert describe_route("wifi", None, surface="native:wifiqr").as_dict()["native_view"] == "wifiqr"
    terminal = describe_route("learn.herdr-keybindings", "omarchy-launch-terminal herdr keybindings")
    assert terminal.route == "terminal" and not terminal.supported
    assert terminal.argv == ("omarchy-launch-terminal", "herdr", "keybindings")
    herdr = describe_route("herdr", "herdr agent attach default")
    assert herdr.route == "terminal" and not herdr.supported


def test_closed_route_policy_requires_registration_and_revision():
    policy = RoutePolicy()
    entry = {"id": "menu.x", "action": "omarchy-toggle-x"}
    ok, reason = policy.validate_invocation(entry)
    assert not ok and reason == "route adapter is not registered"
    policy.register("menu.x", RouteDescriptor("host", True, argv=("omarchy-toggle-x",), entry_id="menu.x"))
    assert policy.validate_invocation(entry)[0]
    assert policy.validate_invocation(entry, catalog_revision="old", expected_revision="new")[1] == "stale_catalog_revision"
    policy.register_native("menu.wifi", "wifiqr")
    assert policy.resolve({"id": "menu.wifi", "surface": "native:wifiqr"}).route == "native"


def test_complex_shell_is_not_promoted_to_mobile_argv():
    d = describe_route("unsafe", "omarchy-launch-terminal foo && curl https://bad")
    assert d.route == "host" and not d.supported and d.argv == ()


def test_script_annotations_only_validate_bounded_params():
    annotations = parse_script_annotations(
        "# omarchy:summary=Choose agent\n# omarchy:args=<kind> [--inline]\n# omarchy:hidden=false\n"
    )
    assert annotations["summary"] == "Choose agent"
    assert validate_action_params(annotations, {"kind": "codex"})[0]
    assert validate_action_params(annotations, {"kind": "codex", "inline": True})[0]
    assert not validate_action_params(annotations, {"kind": "codex", "shell": "rm -rf"})[0]

# unittest discovery is available in the minimal host environment; retain the
# standalone functions above for pytest while exposing the same checks here.
import unittest as _unittest


class CatalogRoutesTests(_unittest.TestCase):
    def test_jsonc(self):
        test_jsonc_comments_trailing_commas_and_string_markers()

    def test_merge(self):
        test_three_layer_merge_same_id_and_stable_order()

    def test_fields(self):
        test_catalog_keeps_provider_conditions_and_unknown_fields()

    def test_routes(self):
        test_routes_surface_and_known_wrappers()

    def test_complex_shell(self):
        test_complex_shell_is_not_promoted_to_mobile_argv()

    def test_annotations(self):
        test_script_annotations_only_validate_bounded_params()

    def test_policy(self):
        test_closed_route_policy_requires_registration_and_revision()


class CatalogRuntimeTests(_unittest.TestCase):
    def test_unknown_conditions_are_unavailable_and_not_false(self):
        from omodachi_core.catalog_runtime import CatalogRuntime
        runtime = CatalogRuntime(compile_catalog({"a": {"when": "unknown", "checked": "unknown"}}))
        row = next(x for x in runtime.refresh()["entries"] if x["id"] == "a")
        self.assertIsNone(row["visible"])
        self.assertIsNone(row["checked_state"])
        self.assertEqual("unavailable", row["conditions"]["when"]["status"])

    def test_registered_condition_cache_and_change_callback(self):
        from omodachi_core.catalog_runtime import CatalogRuntime
        values = {"value": False, "calls": 0}
        changes = []
        runtime = CatalogRuntime(compile_catalog({"a": {"checked": "toggle"}}), on_change=changes.append)
        def adapter():
            values["calls"] += 1
            return values["value"]
        runtime.register_condition("toggle", adapter)
        first = runtime.refresh()
        runtime.refresh()
        self.assertEqual(values["calls"], 1)
        values["value"] = True
        second = runtime.refresh(invalidate=True)
        self.assertNotEqual(first["revision"], second["revision"])
        self.assertEqual(len(changes), 2)

    def test_provider_refresh_replaces_dynamic_rows_without_static_override(self):
        from omodachi_core.catalog_runtime import CatalogRuntime
        runtime = CatalogRuntime(compile_catalog({"apps": {"provider": "apps-provider"}, "apps.static": {"label": "Static"}}))
        current = [{"id": "apps.dynamic", "label": "Dynamic", "action": "unapproved-command"}]
        runtime.register_provider("apps-provider", lambda: list(current))
        first = runtime.refresh()
        self.assertIn("apps.dynamic", [row["id"] for row in first["entries"]])
        dynamic = next(row for row in first["entries"] if row["id"] == "apps.dynamic")
        self.assertFalse(RoutePolicy().resolve(dynamic).supported)
        current[:] = [{"id": "apps.static", "label": "Override attempt"}, {"id": "apps.next", "label": "Next"}]
        # PERF-4: a listing is re-read when something says it changed, the same
        # rule the condition cache above already follows.
        second = runtime.refresh(invalidate=True)
        self.assertNotIn("apps.dynamic", [row["id"] for row in second["entries"]])
        self.assertEqual(next(row for row in second["entries"] if row["id"] == "apps.static")["label"], "Static")

    def test_a_provider_is_read_once_per_window_and_again_after_invalidate(self):
        """PERF-4. Reading a listing costs a subprocess; repeats cost nothing."""
        from omodachi_core.catalog_runtime import CatalogRuntime
        clock = {"now": 100.0}
        calls = {"n": 0}
        def adapter():
            calls["n"] += 1
            return [{"id": "apps.dynamic", "label": "Dynamic"}]
        runtime = CatalogRuntime(compile_catalog({"apps": {"provider": "apps-provider"}}),
                                 cache_seconds=2.0, clock=lambda: clock["now"])
        runtime.register_provider("apps-provider", adapter)
        for _ in range(5):
            runtime.refresh()
        self.assertEqual(calls["n"], 1)
        clock["now"] += 2.5                 # the window expired
        runtime.refresh()
        self.assertEqual(calls["n"], 2)
        clock["now"] += 2.5
        runtime.refresh(invalidate=True)    # something said it changed
        self.assertEqual(calls["n"], 3)

    def test_a_volatile_condition_is_read_every_time_and_never_cached(self):
        """PERF-4 §0: an in-process reading is not worth a cache window."""
        from omodachi_core.catalog_runtime import CatalogRuntime
        calls = {"shell": 0, "field": 0}
        runtime = CatalogRuntime(compile_catalog({"a": {"when": "expensive-shell-command"},
                                                  "b": {"when": "a-field-on-this-object"}}),
                                 cache_seconds=15.0, clock=lambda: 100.0)
        def shell():
            calls["shell"] += 1
            return True
        def field():
            calls["field"] += 1
            return True
        runtime.register_condition("expensive-shell-command", shell)
        runtime.register_condition("a-field-on-this-object", field, volatile=True)
        for _ in range(4):
            runtime.refresh()
        self.assertEqual(calls["shell"], 1)
        # Four refreshes, four readings: a volatile condition is also how a
        # snapshot that is otherwise clean finds out it has to be rebuilt, so
        # it is read even on the fast path.
        self.assertEqual(calls["field"], 4)
        # Re-registering without the flag takes the volatility away again, and
        # a registration invalidates, so the next refresh is a real one.
        runtime.register_condition("a-field-on-this-object", field)
        runtime.refresh(); runtime.refresh()
        self.assertEqual(calls["field"], 5)

    def test_a_failing_provider_is_not_retried_inside_its_window(self):
        """A provider that is down must not be re-run on every refresh."""
        from omodachi_core.catalog_runtime import CatalogRuntime
        calls = {"n": 0}
        def adapter():
            calls["n"] += 1
            raise RuntimeError("provider is down")
        runtime = CatalogRuntime(compile_catalog({"apps": {"provider": "apps-provider"}}),
                                 cache_seconds=2.0, clock=lambda: 100.0)
        runtime.register_provider("apps-provider", adapter)
        for _ in range(4):
            snapshot = runtime.refresh()
        self.assertEqual(calls["n"], 1)
        row = next(entry for entry in snapshot["entries"] if entry["id"] == "apps")
        self.assertEqual(row["provider_state"], {"status": "unavailable", "reason": "provider_adapter_failed"})

    def test_registered_route_rejects_menu_action_changed(self):
        policy = RoutePolicy()
        policy.register("a", RouteDescriptor("host", True, argv=("omarchy-toggle-example",)))
        self.assertTrue(policy.resolve({"id": "a", "action": "omarchy-toggle-example"}).supported)
        self.assertEqual(policy.resolve({"id": "a", "action": "curl attacker.example"}).reason, "menu_action_changed")
        self.assertFalse(describe_route("a", "curl attacker.example").supported)

    def test_reviewed_terminal_adapter_strips_wrapper_and_pins_action(self):
        policy = RoutePolicy()
        action = "omarchy-launch-floating-terminal-with-presentation 'omarchy-dns Custom'"
        policy.register_terminal("setup.dns", action)
        descriptor = policy.prepare_invocation({"id": "setup.dns", "action": action})
        self.assertEqual(descriptor.argv, ("omarchy-dns", "Custom"))
        self.assertFalse(policy.resolve({"id": "setup.dns", "action": action + " && evil"}).supported)

    def test_parameters_are_required_finite_enum_values(self):
        policy = RoutePolicy()
        policy.register("a", RouteDescriptor("host", True, argv=("omarchy-demo", "{mode}")),
                        source_action="omarchy-demo", parameter_enums={"mode": ("low", "high")})
        entry = {"id": "a", "action": "omarchy-demo"}
        self.assertFalse(policy.validate_invocation(entry)[0])
        self.assertFalse(policy.validate_invocation(entry, params={"mode": "https://evil/path"})[0])
        self.assertEqual(policy.prepare_invocation(entry, params={"mode": "low"}).argv, ("omarchy-demo", "low"))


class RowLevelInvalidationTests(_unittest.TestCase):
    """PERF-5. `invoke` re-reads one row's sources, not the whole machine."""

    now = {"t": 100.0}

    def runtime(self, calls):
        from omodachi_core.catalog_runtime import CatalogRuntime
        source = compile_catalog({
            "apps": {"provider": "apps-provider"},
            "style.font": {"provider": "font-provider"},
            "hardware.webcam": {"when": "omarchy-hw-webcam"},
        })
        self.now = {"t": 100.0}
        runtime = CatalogRuntime(source, cache_seconds=15.0, clock=lambda: self.now["t"])
        def counted(name, value):
            def adapter():
                calls.append(name)
                return value
            return adapter
        runtime.register_provider("apps-provider", counted("apps", [{"id": "apps.dynamic", "label": "Dynamic"}]))
        runtime.register_provider("font-provider", counted("fonts", [{"id": "style.font.one", "label": "One"}]))
        runtime.register_condition("omarchy-hw-webcam", counted("webcam", True))
        runtime.refresh()
        return runtime

    def test_re_reading_one_row_leaves_every_other_reading_alone(self):
        calls = []
        runtime = self.runtime(calls)
        calls.clear()
        self.assertTrue(runtime.invalidate_row("apps.dynamic"))
        runtime.refresh()
        # The listing this row came out of, and nothing else: not the font
        # listing, not the webcam shell-out that has nothing to do with it.
        self.assertEqual(calls, ["apps"])

    def test_a_static_rows_own_condition_is_the_only_thing_re_read(self):
        calls = []
        runtime = self.runtime(calls)
        calls.clear()
        self.assertTrue(runtime.invalidate_row("hardware.webcam"))
        runtime.refresh()
        self.assertEqual(calls, ["webcam"])

    def test_a_provider_menu_row_re_reads_its_own_listing(self):
        calls = []
        runtime = self.runtime(calls)
        calls.clear()
        self.assertTrue(runtime.invalidate_row("style.font"))
        runtime.refresh()
        self.assertEqual(calls, ["fonts"])

    def test_an_id_the_snapshot_does_not_have_says_so_rather_than_guessing(self):
        calls = []
        runtime = self.runtime(calls)
        calls.clear()
        self.assertFalse(runtime.invalidate_row("apps.never-existed"))
        # False is the caller's signal to fall back to the whole table, so
        # nothing may have been dropped on the way to saying it.
        runtime.refresh()
        self.assertEqual(calls, [])

    def test_an_expired_window_alone_does_not_make_the_invoke_path_re_read(self):
        """PERF-5. One tap in seven used to land on the cache window's edge.

        The maintenance tick takes these readings every two seconds anyway.
        Letting the tap take them too only meant that whichever tap happened to
        be the one that found the window expired paid for a cold pass - which
        is a tap that is usually fast rather than one that is always fast.
        """
        calls = []
        runtime = self.runtime(calls)
        calls.clear()
        self.now["t"] += 60                              # well past cache_seconds
        self.assertIsNone(runtime.current_revision())
        self.assertIsNotNone(runtime.current_revision(allow_expired=True))
        runtime.refresh()                                 # the ordinary caller still re-reads
        self.assertEqual(sorted(calls), ["apps", "fonts", "webcam"])

    def test_something_that_actually_changed_is_never_waived(self):
        calls = []
        runtime = self.runtime(calls)
        calls.clear()
        self.now["t"] += 60
        runtime.invalidate()
        self.assertIsNone(runtime.current_revision(allow_expired=True),
                          "`allow_expired` waives the window, never a real change")
