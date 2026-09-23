"""Provider boundary tests; every mutation runner is a fake, never a real app."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest

from omodachi_core.catalog import CatalogError, app_entry_id, compile_catalog
from omodachi_core.catalog_runtime import CatalogRuntime
from omodachi_core.catalog_providers import (
    CatalogProviders, FONT_CURRENT, FONT_LIST, FONT_SET,
    ProviderUnavailable, install_catalog_providers,
)
from omodachi_core.hub import Hub
from omodachi_core.service import CoreService, ServiceError


def snapshot(*names, revision="a" * 64):
    return {"schema": 1, "apps": [{"appId": name, "label": "Fixture " + name,
             "icon": "fixture-icon", "appRevision": revision} for name in names], "stats": {"gio_entries": len(names)}}


def service():
    return CoreService(Hub(), catalog=compile_catalog({
        "apps": {"provider": "apps"}, "style.font": {"provider": "fonts"}}))


class CatalogAppTypes(unittest.TestCase):
    def test_app_kind_survives_compile_and_provider_normalize(self):
        row = {"id": "apps.org.example.Test", "kind": "app", "appId": "org.example.Test", "parent": "apps", "label": "Test"}
        result = compile_catalog({"apps": {"provider": "apps"}}, [row]).by_id(row["id"]).as_dict()
        self.assertEqual(result["kind"], "app")
        self.assertEqual(result["appId"], "org.example.Test")
        runtime = CatalogRuntime(compile_catalog({"apps": {"provider": "apps"}}))
        runtime.register_provider("apps", lambda: [row])
        dynamic = next(r for r in runtime.refresh()["entries"] if r["id"] == row["id"])
        self.assertEqual(dynamic["kind"], "app")
        self.assertEqual(dynamic["parent_id"], "apps")

    def test_valid_unusual_ids_are_stable_without_colliding_with_reserved_namespace(self):
        for name in ["Fixture App", "测试应用", "a" * 200, "xdg-real-name"]:
            ident = app_entry_id(name)
            self.assertRegex(ident, r"^apps\.xdg-[0-9a-f]{64}$")
            self.assertEqual(ident, app_entry_id(name))
            self.assertLessEqual(len(ident), 128)
        self.assertEqual(app_entry_id("org.telegram.desktop"), "apps.org.telegram.desktop")
        self.assertNotEqual(app_entry_id("Fixture App"), app_entry_id(app_entry_id("Fixture App")[5:]))

    def test_invalid_ids_and_execution_fields_are_rejected(self):
        for name in ["", None, "../escape", "foo/bar", "foo\\bar", "--help", "a\ncommand", "a" * 256]:
            with self.subTest(name=name), self.assertRaises(CatalogError):
                app_entry_id(name)
        base = {"id": "apps.valid", "parent": "apps", "kind": "app", "appId": "valid"}
        for patch in [{"id": "apps.other"}, {"parent": "elsewhere"}, {"action": "evil"},
                      {"Exec": "/private/command"}, {"argv": ["evil"]}, {"target": "/private"},
                      {"surface": "host"}, {"checked": "true"}, {"privateField": "private"},
                      {"label": 123}, {"label": "bad\nlabel"}, {"appRevision": "wrong"}]:
            with self.subTest(patch=patch), self.assertRaises(CatalogError):
                compile_catalog([{**base, **patch}])


class CatalogProviderBoundary(unittest.TestCase):
    def setUp(self):
        self.service = service()
        self.current = snapshot("org.example.Test", "Fixture App")
        self.writes = []
        self.fonts = ["Fira Code", "Fira-Code", "Quoted ' Font; literal"]
        self.current_font = "Fira Code"
        def read_command(argv, env):
            if argv == (FONT_LIST,): return "\n".join(self.fonts) + "\n"
            if argv == (FONT_CURRENT,): return self.current_font + "\n"
            raise AssertionError("unexpected reader argv")
        self.provider = install_catalog_providers(self.service, app_reader=lambda: deepcopy(self.current),
            environment=lambda: {"PATH": "/usr/bin"}, runner=read_command,
            action_runner=lambda argv, env: self.writes.append(tuple(argv)))

    def catalog(self):
        return self.service.refresh_catalog(invalidate=True)

    def test_actions_require_explicit_runner_and_never_fall_back_to_reader(self):
        with self.assertRaisesRegex(ValueError, "explicit"):
            CatalogProviders(self.service, actions=True)

    def test_readonly_provider_does_not_register_actions_or_launch(self):
        result = self.catalog()
        apps = [r for r in result["entries"] if r["kind"] == "app"]
        self.assertEqual(len(apps), 2)
        self.assertTrue(all(not r["route"]["supported"] for r in apps))
        self.assertEqual(self.writes, [])
        serialized = json.dumps(apps)
        self.assertNotIn("Exec", serialized)
        self.assertNotIn("executable", serialized)
        self.assertNotIn("commandline", serialized)

    def test_scan_schema_rejects_exec_invalid_ids_and_duplicate_ids(self):
        invalid = []
        raw = snapshot("safe");raw["apps"][0]["Exec"] = "evil";invalid.append(raw)
        raw = snapshot("safe");raw["apps"][0]["appId"] = "bad/name";invalid.append(raw)
        invalid.append(snapshot("same", "same"))
        raw = snapshot("safe");raw["apps"][0]["label"] = "bad\nlabel";invalid.append(raw)
        raw = snapshot("safe");raw["apps"][0]["appRevision"] = "bad";invalid.append(raw)
        for raw in invalid:
            self.current = raw
            with self.subTest(raw=raw), self.assertRaises(ProviderUnavailable):
                self.provider.apps()
        self.assertEqual(self.provider.apps_index, {})

    def test_preparation_uses_current_server_index_not_client_command(self):
        self.provider.apps()
        ident = app_entry_id("Fixture App")
        ready = self.provider.prepare_app(ident, self.provider.apps_revision)
        self.assertEqual(ready.argv, ("/usr/bin/uwsm-app", "--", "/usr/bin/gtk-launch", "Fixture App.desktop"))
        self.assertEqual(self.writes, [])
        with self.assertRaises(ProviderUnavailable):
            self.provider.prepare_app("apps.unregistered", self.provider.apps_revision)
        with self.assertRaises(ServiceError):
            self.provider.prepare_app("apps.invalid ID", self.provider.apps_revision)

    def test_same_id_exec_fingerprint_change_rejects_stale_preparation(self):
        self.provider.apps()
        old = self.provider.apps_revision
        self.current["apps"][0]["appRevision"] = "b" * 64
        with self.assertRaisesRegex(ProviderUnavailable, "stale"):
            self.provider.prepare_app("apps.org.example.Test", old)
        self.provider.apps()
        self.assertNotEqual(old, self.provider.apps_revision)
        with self.assertRaises(ProviderUnavailable):
            self.provider.prepare_app("apps.org.example.Test", old)

    def test_unrelated_catalog_source_edit_invalidates_prepared_reference(self):
        self.provider.apps();old = self.provider.apps_revision
        self.service.runtime.catalog = compile_catalog({"apps": {"provider": "apps"},
            "style.font": {"provider": "fonts"}, "another": {"label": "Source changed"}})
        with self.assertRaisesRegex(ProviderUnavailable, "stale"):
            self.provider.prepare_app("apps.org.example.Test", old)

    def test_catalog_provider_payload_validates_existing_schema(self):
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        base = Path(__file__).resolve().parents[1] / "contracts"
        registry = Registry()
        for file in base.glob("*.schema.json"):
            resource = Resource.from_contents(json.loads(file.read_text()))
            registry = registry.with_resource(resource.id(), resource)
        schema = json.loads((base / "catalog.schema.json").read_text())
        Draft202012Validator(schema, registry=registry).validate(self.catalog())

    def test_deleted_or_new_app_invalidates_old_index_revision(self):
        self.provider.apps();old = self.provider.apps_revision
        self.current = snapshot("Another")
        with self.assertRaises(ProviderUnavailable):
            self.provider.prepare_app("apps.org.example.Test", old)

    def test_source_override_and_static_id_never_gain_dynamic_launch(self):
        self.provider.actions = True
        initial = self.catalog()
        self.assertTrue(next(r for r in initial["entries"] if r["id"] == "apps.org.example.Test")["route"]["ready"])
        self.service.runtime.catalog = compile_catalog({"apps": {"provider": "apps"},
            "apps.org.example.Test": {"label": "User static override"}})
        new = self.catalog()
        static = next(r for r in new["entries"] if r["id"] == "apps.org.example.Test")
        self.assertEqual(static["kind"], "menu")
        self.assertFalse(static["route"]["supported"])
        self.assertNotIn(static["id"], self.service._executors)
        self.service.runtime.catalog = compile_catalog({"apps": {"provider": "apps", "action": "changed"}})
        with self.assertRaisesRegex(ProviderUnavailable, "source"):
            self.provider.apps()
        self.assertFalse(self.provider.apps_index)
        self.assertFalse(self.writes)

    def test_fake_invoke_registers_fixed_launcher_and_is_idempotent(self):
        self.provider.actions = True
        catalog = self.catalog()
        params = {"entry_id": "apps.org.example.Test", "request_id": "test-request",
                  "catalog_revision": catalog["revision"], "params": {}}
        first = self.service.invoke(params, "fixture-device")
        second = self.service.invoke(params, "fixture-device")
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(self.writes, [("/usr/bin/uwsm-app", "--", "/usr/bin/gtk-launch", "org.example.Test.desktop")])

    def test_a_desktop_entry_that_changed_under_the_client_is_never_launched(self):
        """PERF-5. The property the old `stale_catalog_revision` gate protected.

        Before PERF-5 `invoke` re-read every provider listing on the machine
        before it authorised anything, and this arrived as a 409 from that
        read. It is no longer that read's job: the client's revision is a hint
        now, and what refuses the launch is the provider's own identity check
        at the moment of execution. The receipt is a failure and - the part
        that actually matters - no application is started.
        """
        self.provider.actions = True
        old = self.catalog()
        self.current["apps"][0]["appRevision"] = "c" * 64
        result = self.service.invoke({"entry_id": "apps.org.example.Test", "request_id": "stale",
            "catalog_revision": old["revision"], "params": {}}, "fixture-device")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(self.writes)
        new = self.catalog()
        with self.assertRaisesRegex(ValueError, "invalid_route_parameters"):
            self.service.invoke({"entry_id": "apps.org.example.Test", "request_id": "inject",
                "catalog_revision": new["revision"], "params": {"command": "evil"}}, "fixture-device")
        self.assertFalse(self.writes)

    def test_an_app_the_host_removed_is_refused_and_the_receipt_says_the_row_is_gone(self):
        """PERF-5. The other half: the row is not there any more at all."""
        self.provider.actions = True
        old = self.catalog()
        self.current = snapshot("Fixture App")
        self.catalog()
        with self.assertRaisesRegex(ServiceError, "stale_catalog_revision") as caught:
            self.service.invoke({"entry_id": "apps.org.example.Test", "request_id": "deleted",
                "catalog_revision": old["revision"], "params": {}}, "fixture-device")
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse(self.writes)

    def test_a_revision_that_moved_still_launches_the_row_it_named(self):
        """PERF-5's point: a catalog that moved is not a reason to refuse.

        The font list changing moves the catalog revision without touching a
        single app row, and a client holding the older revision used to be
        told to refresh and tap again. The id still resolves to the same
        desktop entry, so it runs, and the receipt carries the revision it
        actually ran against so the next tap is not optimistic about this gap.
        """
        self.provider.actions = True
        old = self.catalog()
        self.fonts = self.fonts + ["Another Font"]
        current = self.catalog()
        self.assertNotEqual(current["revision"], old["revision"])
        result = self.service.invoke({"entry_id": "apps.org.example.Test", "request_id": "optimistic",
            "catalog_revision": old["revision"], "params": {}}, "fixture-device")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["catalog_revision"], current["revision"])
        self.assertEqual(self.writes, [("/usr/bin/uwsm-app", "--", "/usr/bin/gtk-launch", "org.example.Test.desktop")])

    def test_an_invoke_whose_revision_matches_reads_no_provider_listing(self):
        """PERF-5. The latency contract, as a countable fact.

        Every provider listing here is a subprocess on the real host - the Gio
        desktop scan, `omarchy-font-list` - and `invoke` used to run all of
        them before it would let a tap through. With the revision the client
        holds still current there is nothing to re-read, so nothing is read.
        """
        self.provider.actions = True
        current = self.catalog()
        reads = []
        self.service.runtime.providers = {name: (lambda adapter=adapter, name=name:
                                                 (reads.append(name), adapter())[1])
                                          for name, adapter in self.service.runtime.providers.items()}
        at_launch = []
        launch = self.provider.action_runner
        self.provider.action_runner = lambda argv, env: (at_launch.append(list(reads)), launch(argv, env))[1]
        self.service.invoke({"entry_id": "apps.org.example.Test", "request_id": "warm",
            "catalog_revision": current["revision"], "params": {}}, "fixture-device")
        # The rebuild that follows the receipt still re-reads everything; what
        # matters is that nothing was re-read before the application started.
        self.assertEqual(at_launch, [[]])
        self.assertEqual(len(self.writes), 1)

    def test_last_moment_source_change_rejected_by_executor(self):
        self.provider.actions = True
        self.catalog()
        callback = self.service._executors["apps.org.example.Test"][0]
        argv = self.service.policy._adapters["apps.org.example.Test"].argv
        self.current["apps"][0]["appRevision"] = "d" * 64
        with self.assertRaisesRegex(ProviderUnavailable, "stale"):
            callback(argv)
        self.assertFalse(self.writes)

    def test_fonts_preserve_upstream_order_collision_and_selection_marker(self):
        rows = self.provider.fonts()
        self.assertEqual([r["id"] for r in rows], ["style.font.fira-code", "style.font.fira-code-", "style.font.quoted-font-literal"])
        self.assertEqual([r["icon"] for r in rows], ["✓", "", ""])
        plan = self.provider.prepare_font(rows[-1]["id"], self.provider.fonts_revision)
        self.assertEqual(plan.argv, (FONT_SET, self.fonts[-1]))
        self.assertFalse(self.writes)
        self.current_font = "Fira-Code"
        with self.assertRaisesRegex(ProviderUnavailable, "stale"):
            self.provider.prepare_font(rows[-1]["id"], self.provider.fonts_revision)


if __name__ == "__main__":
    unittest.main()


class ProviderEnvironmentTests(unittest.TestCase):
    """PERF-5 follow-up: where the listing's environment comes from.

    The Apps and Font listings need the session's `XDG_DATA_DIRS` and friends.
    Until now the only place they were read from was a `quickshell`/`qs`
    process whose argv contained `/usr/share/omarchy/shell` - and Quickshell
    relaunches itself from its own signal handlers, coming back as a bare
    `/usr/bin/quickshell`. On Leo's host that had already happened, so both
    providers were permanently unavailable and the Apps submenu was empty.
    """

    ALLOWED = ("XDG_DATA_DIRS=/usr/local/share:/usr/share\0XDG_DATA_HOME=/home/fixture/.local/share"
               "\0XDG_CURRENT_DESKTOP=Hyprland\0XDG_SESSION_DESKTOP=Hyprland\0DESKTOP_SESSION=omarchy"
               "\0DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\0SECRET=never-exported\0")

    def host(self, root, *, compositor_environ=True, shell_argv=None):
        """A session laid out the way Hyprland and Quickshell really lay it out."""
        import socket
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
        if compositor_environ:
            (compositor / "environ").write_bytes(self.ALLOWED.encode())
        for index, argv in enumerate(shell_argv or ()):
            shell = proc / str(325275 + index)
            shell.mkdir()
            (shell / "comm").write_text("quickshell\n")
            (shell / "cmdline").write_bytes(argv)
            (shell / "environ").write_bytes(self.ALLOWED.encode())
        return proc, runtime

    #: What `/proc/<pid>/cmdline` holds after Quickshell has relaunched itself.
    REEXECED = b"/usr/bin/quickshell\0"
    #: A second Quickshell the user has running: an Omarchy plugin's own shell.
    PLUGIN = b"quickshell\0--daemonize\0-p\0/home/fixture/.config/omarchy/plugins/panel/shell.qml\0"
    LAUNCHED = b"quickshell\0-n\0-p\0/usr/share/omarchy/shell\0"

    def test_a_relaunched_shell_no_longer_takes_the_listings_down_with_it(self):
        with tempfile.TemporaryDirectory() as root:
            proc, runtime = self.host(root, shell_argv=(self.REEXECED, self.PLUGIN))
            # The old rule: exactly one `quickshell`/`qs` whose argv carries the
            # shell path. Neither of these does, so it used to find nothing.
            from omodachi_core.catalog_providers import _shell_presentation, provider_environment
            self.assertIsNone(_shell_presentation(proc))
            env = provider_environment(proc, runtime=runtime)
            self.assertEqual(env["XDG_DATA_DIRS"].split(":")[:2], ["/usr/local/share", "/usr/share"])
            self.assertEqual(env["XDG_DATA_HOME"], "/home/fixture/.local/share")
            # The union of the three desktop names, as the official hidden
            # scanner does it - unchanged by this fix.
            self.assertEqual(env["XDG_CURRENT_DESKTOP"], "Hyprland:omarchy")
            self.assertEqual(env["HYPRLAND_INSTANCE_SIGNATURE"], "efb50993_1789614994")
            self.assertNotIn("SECRET", env)

    def test_the_shell_is_still_the_fallback_when_the_compositor_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as root:
            proc, runtime = self.host(root, compositor_environ=False, shell_argv=(self.LAUNCHED,))
            from omodachi_core.catalog_providers import provider_environment
            env = provider_environment(proc, runtime=runtime)
            self.assertEqual(env["DESKTOP_SESSION"], "omarchy")
            self.assertNotIn("SECRET", env)

    def test_neither_source_is_still_unavailable_rather_than_a_guess(self):
        with tempfile.TemporaryDirectory() as root:
            proc, runtime = self.host(root, compositor_environ=False,
                                      shell_argv=(self.REEXECED, self.PLUGIN))
            from omodachi_core.catalog_providers import provider_environment
            with self.assertRaisesRegex(ProviderUnavailable, "graphical_session_ambiguous"):
                provider_environment(proc, runtime=runtime)

    def test_two_compositors_are_ambiguous_and_never_silently_picked(self):
        with tempfile.TemporaryDirectory() as root:
            import socket
            proc, runtime = self.host(root, shell_argv=())
            second = runtime / "hypr" / "efb50993_1789614995"
            second.mkdir(parents=True)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(second / ".socket.sock"))
            self.addCleanup(listener.close)
            (second / "hyprland.lock").write_text("1473\nwayland-1\n")
            other = proc / "1473"; other.mkdir()
            (other / "comm").write_text("Hyprland\n")
            (other / "environ").write_bytes(self.ALLOWED.encode())
            from omodachi_core.catalog_providers import provider_environment
            from omodachi_core.graphical import GraphicalUnavailable
            # Two live compositors is the session lookup's own refusal, and it
            # comes first; either way nothing is guessed and no listing runs.
            with self.assertRaises(GraphicalUnavailable):
                provider_environment(proc, runtime=runtime)
