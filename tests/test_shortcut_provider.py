import json
import unittest
from pathlib import Path
import aiohttp
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
from omodachi_core.shortcut_provider import ShortcutProvider, install_shortcut_provider, validate_observation

FIXTURES = Path(__file__).resolve().parent / "fixtures"
#: The host's own `omarchy-menu-keybindings` records and the compositor's own
#: `hyprctl binds -j`, both taken from omarchy (Omarchy 4.0.4, Hyprland 0.56.2)
#: on 2026-09-20. They are the two halves of the same 227/228 bindings.
HOST_RECORDS = (FIXTURES / "omarchy-binding-records.tsv").read_text()
HOST_BINDS = json.loads((FIXTURES / "omarchy-hyprctl-binds.json").read_text())

WORKSPACE_BEFORE = {"id": 1, "name": "1", "monitor": "eDP-1", "monitorID": 0, "windows": 4}
WORKSPACE_AFTER = {"id": 3, "name": "3", "monitor": "eDP-1", "monitorID": 0, "windows": 0}
WINDOW = {"address": "0x56422918caa0", "class": "kitty", "title": "alex@omarchy",
          "fullscreen": 0, "floating": False, "workspace": {"id": 1, "name": "1"}}


class FakeHost:
    """Stands in for one graphical session: hyprctl queries plus dispatch."""

    def __init__(self, records=HOST_RECORDS, *, window=WINDOW, status="0"):
        self.records = records
        self.window = window
        self.status = status
        self.dispatched = []
        self.spawned = []
        self.workspace = dict(WORKSPACE_BEFORE)

    def runner(self, argv, env):
        if argv[:3] == ("/usr/bin/hyprctl", "-j", "activeworkspace"):
            return json.dumps(self.workspace)
        if argv[:3] == ("/usr/bin/hyprctl", "-j", "activewindow"):
            return json.dumps(self.window) if self.window else "{}"
        if argv[:2] == ("/bin/bash", "-c") and argv[2].endswith("output_binding_records"):
            return self.records
        if argv[:2] == ("/bin/bash", "-c"):
            self.dispatched.append((argv[-2], argv[-1]))
            if argv[-2] == "lua" and 'workspace = "3"' in argv[-1]:
                self.workspace = dict(WORKSPACE_AFTER)
            return self.status
        raise AssertionError(f"unexpected command {argv!r}")

    def spawner(self, command, env):
        self.spawned.append(command)
        return {"pid": 4242, "exited": True, "exit_code": 0}


class ClassificationTests(unittest.TestCase):
    """SHORTCUT-1 item 1, against the host's real records."""

    def test_hyprctl_binds_on_this_host_cannot_drive_execution(self):
        # Every bind Hyprland reports on an Omarchy 4 Lua config is `__lua`
        # with a callback index, which is why the adapter reads Omarchy's
        # records instead. This fixture is the evidence, kept executable.
        self.assertEqual({row["dispatcher"] for row in HOST_BINDS}, {"__lua"})
        self.assertTrue(all(row["arg"].isdigit() for row in HOST_BINDS))
        self.assertEqual(len(HOST_BINDS), 228)

    def test_every_host_record_shape_maps_to_one_execution_kind(self):
        kinds = {}
        for line in HOST_RECORDS.splitlines():
            display, dispatcher, arg = line.split("\t", 2)
            execution = ShortcutProvider.execution(dispatcher, arg)
            kinds.setdefault(execution["kind"] if execution else None, []).append(display)
        self.assertEqual(len(HOST_RECORDS.splitlines()), 227)
        self.assertEqual(len(kinds[None]), 5, kinds[None])
        self.assertEqual(len(kinds["exec"]), 128)
        self.assertEqual(len(kinds["eval"]), 92)
        self.assertEqual(len(kinds["sendshortcut"]), 2)
        self.assertNotIn("dispatch", kinds)

    def test_execution_kind_per_dispatcher(self):
        execution = ShortcutProvider.execution
        self.assertEqual(execution("exec", "omarchy-system-lock"), {"kind": "exec", "detail": "omarchy-system-lock"})
        self.assertEqual(execution("exec", "pkill hyprpicker || hyprpicker -a"),
                         {"kind": "exec", "detail": "pkill hyprpicker || hyprpicker -a"})
        self.assertEqual(execution("lua", 'hl.dsp.focus({ workspace = "3" })'),
                         {"kind": "eval", "detail": 'hl.dsp.focus({ workspace = "3" })'})
        self.assertEqual(execution("sendshortcut", "SHIFT ALT,D,"),
                         {"kind": "sendshortcut", "detail": "SHIFT ALT,D,"})
        # A classic non-Lua host keeps its dispatcher form rather than vanishing.
        self.assertEqual(execution("workspace", "3"), {"kind": "dispatch", "detail": "workspace 3"})
        # Only a record with no binding at all stays unusable.
        self.assertIsNone(execution("", ""))
        self.assertIsNone(execution("exec", "   "))

    def test_focused_window_kinds_are_the_window_and_group_expressions(self):
        classify = ShortcutProvider.classify
        for arg in ('hl.dsp.window.close()', 'hl.dsp.window.move({ workspace = "3" })',
                    'hl.dsp.group.next()', 'hl.dsp.layout("togglesplit")'):
            self.assertEqual(classify("lua", arg), "focused_window", arg)
        for arg in ('hl.dsp.focus({ workspace = "3" })', 'hl.dsp.workspace.move({ monitor = "u" })',
                    'hl.dsp.focus({ monitor = "+1" })', 'hl.dsp.workspace.toggle_special("scratchpad")'):
            self.assertEqual(classify("lua", arg), "compositor", arg)

    def test_only_the_two_panel_rows_stay_local(self):
        classify = ShortcutProvider.classify
        self.assertEqual(classify("exec", "omarchy-menu toggle"), "panel")
        self.assertEqual(classify("exec", "omarchy-menu-keybindings"), "shortcuts")
        # Everything else that looks like a menu runs on the host.
        self.assertEqual(classify("exec", "omarchy-menu toggle system"), "exec")
        self.assertEqual(classify("exec", "omarchy-menu-tmux-keybindings"), "exec")
        self.assertEqual(classify("exec", "omarchy-menu-herdr-keybindings"), "exec")

    def test_the_receipt_waits_for_the_change_and_not_for_a_clock(self):
        """PERF-4. The settle window is a ceiling, not a cost.

        Before this, every invoke slept `DISPATCH_SETTLE` whether the host had
        already done the thing or not - and an `exec` slept `EXEC_SETTLE` on
        top, in full, precisely because the process it spawned was still alive.
        """
        from omodachi_core.shortcut_provider import DISPATCH_SETTLE
        was = {"workspace": {"id": 1, "name": "1", "monitor": "eDP-1"}, "window": None}
        now = {"workspace": {"id": 3, "name": "3", "monitor": "eDP-1"}, "window": None}
        provider = ShortcutProvider.__new__(ShortcutProvider)

        slept = []
        provider.sleeper = slept.append
        provider._probe = lambda env: now          # already moved
        self.assertEqual(provider._settle({}, was), now)
        self.assertEqual(slept, [], "a reading that has already moved is not slept on")

        slept.clear()
        provider._probe = lambda env: was if not slept else now
        self.assertEqual(provider._settle({}, was), now)
        self.assertEqual(slept, [0.02], "one poll, not a fixed window")
        self.assertLessEqual(sum(slept), DISPATCH_SETTLE)

    def test_observation_validator_refuses_anything_else(self):
        good = {"kind": "eval", "before": {"workspace": None, "window": None},
                "after": {"workspace": None, "window": None}, "changed": False}
        self.assertIs(validate_observation(good), good)
        self.assertIsNone(validate_observation({**good, "kind": "lua"}))
        self.assertIsNone(validate_observation({**good, "extra": 1}))
        self.assertIsNone(validate_observation({**good, "process": {"pid": 1}}))


class ShortcutSurfaceTests(unittest.IsolatedAsyncioTestCase):
    """The listing and the invoke boundary, against the host's real records."""

    async def asyncSetUp(self):
        self.hub = Hub()
        self.token = self.hub.register_device("phone")
        self.service = create_service(self.hub, demo=True)
        self.host = FakeHost()
        self.provider = install_shortcut_provider(
            self.service, runner=self.host.runner, environment=lambda: {},
            spawner=self.host.spawner, sleeper=lambda seconds: None)
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.client = aiohttp.ClientSession()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.headers = {"Authorization": "Bearer " + self.token}

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.close()

    async def listed(self):
        async with self.client.get(self.url + "/v1/shortcuts", headers=self.headers) as response:
            self.assertEqual(response.status, 200)
            return await response.json()

    def row(self, listing, display):
        return next(item for item in listing["items"] if item["shortcut_display"] == display)

    async def invoke(self, item, listing, **extra):
        body = {"request_id": extra.pop("request_id", "click-1"), "catalog_revision": listing["revision"],
                "params": {}, "execution_context": {"surface": "omarchy"}, **extra}
        async with self.client.post(self.url + "/v1/actions/" + item["action_ref"] + ":invoke",
                                    headers=self.headers, json=body) as response:
            return response.status, await response.json()

    async def test_the_whole_host_listing_is_enabled_except_the_five_bindingless_rows(self):
        listing = await self.listed()
        self.assertTrue(listing["available"])
        self.assertEqual(len(listing["items"]), 227)
        disabled = [item for item in listing["items"] if not item["enabled"]]
        self.assertEqual(len(disabled), 5)
        self.assertEqual(sorted(item["label"] for item in disabled),
                         ["Reset zoom", "Universal copy", "Universal cut", "Universal paste", "Zoom in"])
        for item in disabled:
            self.assertEqual(item["disabled_reason"], "binding_adapter_unavailable")
            self.assertIn("dispatcher", item["disabled_reason_detail"])
            self.assertIsNone(item["action_ref"])
            self.assertIsNone(item["execution"])
        for item in listing["items"]:
            if item["enabled"]:
                self.assertIn(item["execution"]["kind"], {"exec", "eval", "sendshortcut", "dispatch"})
                self.assertIsNone(item["disabled_reason_detail"])

    async def test_clip1_the_five_rows_are_the_hidden_ones_and_nothing_else_is(self):
        # CLIP-1 §1. Five rows on this host are bound to bare Lua functions, so
        # `omarchy-menu-keybindings` writes an empty dispatcher and an empty
        # argument for them. Nothing here can run those, and a list of 227 rows
        # with five permanently dead ones in it is a worse list, so they carry
        # `hidden` and a client leaves them out.
        listing = await self.listed()
        hidden = [item for item in listing["items"] if item["hidden"]]
        self.assertEqual(sorted(item["label"] for item in hidden),
                         ["Reset zoom", "Universal copy", "Universal cut", "Universal paste", "Zoom in"])
        self.assertEqual(hidden, [item for item in listing["items"] if not item["enabled"]])
        self.assertEqual(len(listing["items"]) - len(hidden), 222)
        # `hidden` is a decision about the list, not a second word for
        # "disabled": every row carries it, and a runnable row carries False.
        self.assertTrue(all(type(item["hidden"]) is bool for item in listing["items"]))
        self.assertFalse(self.row(listing, "SUPER + RETURN")["hidden"])

    async def test_clip1_a_hidden_row_is_still_refused_if_a_client_runs_it_anyway(self):
        # Hiding is a display decision. It must not become the only thing
        # standing between a dead row and an execution attempt.
        listing = await self.listed()
        item = self.row(listing, "SUPER + C")
        self.assertTrue(item["hidden"])
        self.assertIsNone(item["action_ref"])
        status, value = await self.invoke({"action_ref": item["id"]}, listing)
        self.assertEqual(status, 409, value)
        self.assertEqual(value["error"]["code"], "route_unavailable")

    async def test_rows_that_were_binding_adapter_unavailable_are_now_executable(self):
        listing = await self.listed()
        for display, kind in (("SUPER CTRL + L", "exec"), ("SUPER + C", None), ("SUPER + PRINT", "exec"),
                              ("SUPER SHIFT + 3", "eval"), ("SHIFT ALT + D", "sendshortcut")):
            item = self.row(listing, display)
            self.assertEqual(item["enabled"], kind is not None, item)
            if kind:
                self.assertEqual(item["execution"]["kind"], kind, item)

    async def test_a_workspace_row_dispatches_verbatim_and_reports_the_change(self):
        listing = await self.listed()
        item = self.row(listing, "SUPER + 3")
        self.assertTrue(item["enabled"])
        self.assertFalse(item["requires_target"])
        status, value = await self.invoke(item, listing)
        self.assertEqual(status, 200, value)
        self.assertEqual(value["status"], "accepted")
        self.assertEqual(self.host.dispatched[-1], ("lua", 'hl.dsp.focus({ workspace = "3" })'))
        observed = value["observed"]
        self.assertEqual(observed["kind"], "eval")
        self.assertEqual(observed["before"]["workspace"], {"id": 1, "name": "1", "monitor": "eDP-1"})
        self.assertEqual(observed["after"]["workspace"], {"id": 3, "name": "3", "monitor": "eDP-1"})
        self.assertTrue(observed["changed"])

    async def test_an_exec_row_runs_the_hosts_command_line_and_reports_the_process(self):
        listing = await self.listed()
        item = self.row(listing, "SUPER + RETURN")
        status, value = await self.invoke(item, listing)
        self.assertEqual(status, 200, value)
        self.assertEqual(self.host.spawned, ["omarchy-launch-terminal"])
        self.assertEqual(self.host.dispatched, [])
        self.assertEqual(value["observed"]["kind"], "exec")
        self.assertEqual(value["observed"]["process"], {"pid": 4242, "exited": True, "exit_code": 0})

    async def test_a_shell_pipeline_row_is_executed_as_a_shell_command(self):
        listing = await self.listed()
        item = self.row(listing, "SUPER + PRINT")
        status, value = await self.invoke(item, listing)
        self.assertEqual(status, 200, value)
        self.assertEqual(self.host.spawned, ["pkill hyprpicker || hyprpicker -a"])

    async def test_a_focused_window_row_needs_no_token_and_reports_the_window(self):
        listing = await self.listed()
        item = self.row(listing, "SUPER + F")
        self.assertTrue(item["requires_target"], "a client may still say so, but the row is lit")
        self.assertTrue(item["enabled"])
        status, value = await self.invoke(item, listing)
        self.assertEqual(status, 200, value)
        self.assertEqual(self.host.dispatched[-1], ("lua", 'hl.dsp.window.fullscreen({ mode = "fullscreen" })'))
        self.assertEqual(value["observed"]["before"]["window"]["address"], "0x56422918caa0")
        # A window title never reaches the wire.
        self.assertNotIn("alex@omarchy", json.dumps(value))

    async def test_a_focused_window_row_with_nothing_focused_says_so(self):
        self.host.window = None
        listing = await self.listed()
        item = self.row(listing, "SUPER + W")
        status, value = await self.invoke(item, listing)
        # Not an HTTP error: the acceptance is already recorded, and MERGE-1
        # showed a 409 here is indistinguishable from `stale_catalog_revision`
        # on the client. The receipt names the reason instead.
        self.assertEqual(status, 200, value)
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["code"], "no_focused_window")
        self.assertEqual(self.host.dispatched, [])

    async def test_a_compositor_row_with_nothing_focused_still_runs(self):
        self.host.window = None
        listing = await self.listed()
        item = self.row(listing, "SUPER + 3")
        status, value = await self.invoke(item, listing)
        self.assertEqual(status, 200, value)
        self.assertEqual(self.host.dispatched[-1][0], "lua")

    async def test_a_refused_dispatch_is_a_failure_not_an_acceptance(self):
        self.host.status = "7"
        listing = await self.listed()
        item = self.row(listing, "SUPER + 3")
        status, value = await self.invoke(item, listing)
        self.assertEqual(status, 200, value)
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["code"], "shortcut_execution_failed")
        self.assertNotIn("observed", value)

    async def test_the_two_panel_rows_stay_native_and_the_other_menus_do_not(self):
        listing = await self.listed()
        for display in ("SUPER + SPACE", "SUPER + K"):
            item = self.row(listing, display)
            self.assertTrue(item["enabled"], item)
            self.assertTrue(item["action_ref"])
        entries = {row["id"]: row for row in self.service.refresh_catalog()["entries"]}
        self.assertEqual(entries[self.row(listing, "SUPER + SPACE")["action_ref"]]["route"]["route"], "native")
        self.assertEqual(entries[self.row(listing, "SUPER + ESCAPE")["action_ref"]]["route"]["route"], "host")

    async def test_a_stale_reference_is_refused_rather_than_dispatched(self):
        """PERF-5 changed how this is *said*; nothing is dispatched either way.

        The keybinding records moved under the client, so the row it is
        holding names a binding the host no longer has. It used to arrive as
        409 `stale_catalog_revision` from the whole-table re-read every invoke
        took; the re-read is gone from the tap, and the binding provider's own
        lookup refuses it by name instead - a failed receipt carrying
        `stale_binding`, which is the narrower and truer of the two. The line
        that has to keep holding is the last one.
        """
        listing = await self.listed()
        item = self.row(listing, "SUPER + 3")
        self.host.records = HOST_RECORDS.replace("SUPER + 3 ", "SUPER + 0 ", 1)
        self.provider.loaded_at = -float("inf")
        status, value = await self.invoke(item, listing, request_id="click-2")
        self.assertEqual((status, value["status"]), (200, "failed"), value)
        self.assertEqual(value["code"], "stale_binding")
        self.assertEqual(self.host.dispatched, [])

    async def test_a_binding_whose_command_was_rewritten_is_refused_before_dispatch(self):
        """PERF-5. The other way a row stops meaning what it meant.

        The display stays `SUPER + 3`, so the row keeps its id, but the
        command behind it is now someone else's. That has to be caught before
        anything is dispatched, and it is: the id is re-resolved against the
        records read for this row, and they no longer match what the adapter
        was registered against.
        """
        listing = await self.listed()
        item = self.row(listing, "SUPER + 3")
        self.host.records = HOST_RECORDS.replace('hl.dsp.focus({ workspace = "3" })',
                                                 'hl.dsp.focus({ workspace = "9" })', 1)
        self.provider.loaded_at = -float("inf")
        status, value = await self.invoke(item, listing, request_id="click-3")
        self.assertEqual((status, value["status"]), (200, "failed"), value)
        self.assertEqual(value["code"], "stale_binding")
        self.assertEqual(self.host.dispatched, [])

    async def test_a_replayed_request_id_never_runs_the_binding_twice(self):
        listing = await self.listed()
        item = self.row(listing, "SUPER + 3")
        status, first = await self.invoke(item, listing)
        self.assertEqual(status, 200, first)
        status, second = await self.invoke(item, listing)
        self.assertEqual(status, 200, second)
        self.assertEqual(len(self.host.dispatched), 1)

    async def test_the_context_gate_is_unchanged(self):
        listing = await self.listed()
        item = self.row(listing, "SUPER + 3")
        status, value = await self.invoke(item, listing, execution_context={"surface": "herdr"})
        self.assertEqual(status, 400, value)
        status, value = await self.invoke(item, listing, request_id="click-3",
                                          execution_context={"surface": "remote", "session_id": "rs_" + "0" * 32,
                                                             "revision": 1})
        self.assertEqual(status, 409, value)
        self.assertEqual(self.host.dispatched, [])

    async def test_missing_provider_is_explicit_unavailable_not_empty_success(self):
        self.provider.reader = lambda: (_ for _ in ()).throw(ValueError("missing"))
        self.provider.loaded_at = -float("inf")
        result = await self.listed()
        self.assertFalse(result["available"])
        self.assertEqual(result["items"], [])

    async def test_existing_user_extension_keeps_new_packaged_provider(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old-installed-menu.jsonc"
            path.write_text(json.dumps({"omodachi.agent": {"label": "My Agent", "surface": "terminal"}}))
            service = create_service(Hub(), demo=True, omodachi_menu=path)
            install_shortcut_provider(service, reader=lambda: HOST_RECORDS,
                                      runner=FakeHost().runner, environment=lambda: {})
            result = service.shortcuts_snapshot()
            self.assertTrue(result["available"])
            self.assertEqual(len(result["items"]), 227)
            self.assertEqual(service.runtime.catalog.by_id("omodachi.agent").label, "My Agent")
            self.assertNotIn("learn.keybindings", path.read_text())


OWNED_OUTPUT = "OMODACHI-5e57f2a1a4e24bcc"
#: The host's two-monitor shape during an `extend` session: Hyprland keeps the
#: low-numbered persistent workspaces on the laptop panel and hands the rest to
#: the new output. Taken from omarchy on 2026-09-20 with a session open.
REMOTE_MONITORS = [
    {"name": "eDP-1", "id": 0, "focused": True, "activeWorkspace": {"id": 1, "name": "1"}},
    {"name": OWNED_OUTPUT, "id": 1, "focused": False, "activeWorkspace": {"id": 11, "name": "11"}},
]
REMOTE_WORKSPACES = ([{"id": n, "name": str(n), "monitor": "eDP-1"} for n in range(1, 6)]
                     + [{"id": n, "name": str(n), "monitor": OWNED_OUTPUT} for n in (6, 9, 11)])


class RemoteHost(FakeHost):
    """A FakeHost that also answers `monitors`/`workspaces` and `hyprctl eval`."""

    def __init__(self, **options):
        super().__init__(**options)
        self.evaluated = []

    def runner(self, argv, env):
        if argv[:3] == ("/usr/bin/hyprctl", "-j", "monitors"):
            return json.dumps(REMOTE_MONITORS)
        if argv[:3] == ("/usr/bin/hyprctl", "-j", "workspaces"):
            return json.dumps(REMOTE_WORKSPACES)
        if argv[:2] == ("/usr/bin/hyprctl", "eval"):
            self.evaluated.append(argv[2])
            return "ok"
        return super().runner(argv, env)


class RemoteWorkspaceRedirectTests(unittest.IsolatedAsyncioTestCase):
    """SHORTCUT-1 follow-up: in a session the workspace rows act on our output."""

    async def asyncSetUp(self):
        self.hub = Hub()
        self.token = self.hub.register_device("phone")
        self.service = create_service(self.hub, demo=True)
        self.host = RemoteHost()
        self.provider = install_shortcut_provider(
            self.service, runner=self.host.runner, environment=lambda: {},
            spawner=self.host.spawner, sleeper=lambda seconds: None)
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.client = aiohttp.ClientSession()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.headers = {"Authorization": "Bearer " + self.token}

    async def asyncTearDown(self):
        # The fake manager only knows how to be asked for the current session.
        self.service.remote.manager = None
        await self.client.close()
        await self.server.close()

    def open_session(self, output=OWNED_OUTPUT):
        from types import SimpleNamespace
        live = SimpleNamespace(id="rs_" + "0" * 32, device_id="phone", state="ready",
                               revision=1, ttl_seconds=300.0, output_name=output)
        self.service.remote.manager = SimpleNamespace(current=lambda: live)
        return live

    async def listed(self):
        async with self.client.get(self.url + "/v1/shortcuts", headers=self.headers) as response:
            self.assertEqual(response.status, 200)
            return await response.json()

    def row(self, listing, display):
        return next(item for item in listing["items"] if item["shortcut_display"] == display)

    async def invoke(self, item, listing, **extra):
        body = {"request_id": extra.pop("request_id", "click-1"), "catalog_revision": listing["revision"],
                "params": {}, "execution_context": {"surface": "omarchy"}, **extra}
        async with self.client.post(self.url + "/v1/actions/" + item["action_ref"] + ":invoke",
                                    headers=self.headers, json=body) as response:
            return response.status, await response.json()

    async def test_without_a_session_the_binding_is_dispatched_verbatim(self):
        listing = await self.listed()
        status, value = await self.invoke(self.row(listing, "SUPER + 3"), listing)
        self.assertEqual(status, 200, value)
        self.assertEqual(self.host.dispatched[-1], ("lua", 'hl.dsp.focus({ workspace = "3" })'))
        self.assertEqual(self.host.evaluated, [])
        self.assertNotIn("redirected_output", value["observed"])

    async def test_in_a_session_workspace_three_is_pulled_to_our_output(self):
        self.open_session()
        listing = await self.listed()
        status, value = await self.invoke(self.row(listing, "SUPER + 3"), listing)
        self.assertEqual(status, 200, value)
        # Nothing went through the host's `dispatch_binding`; the pair went to eval.
        self.assertEqual(self.host.dispatched, [])
        self.assertEqual(len(self.host.evaluated), 1)
        command = self.host.evaluated[0]
        self.assertIn('hl.dsp.workspace.move({ workspace = "3", monitor = "%s" })' % OWNED_OUTPUT, command)
        self.assertIn('hl.dsp.focus({ workspace = "3" })', command)
        self.assertIn("omodachi_workspace_focus_failed", command)
        self.assertEqual(value["observed"]["redirected_output"], OWNED_OUTPUT)

    async def test_in_a_session_next_workspace_steps_our_own_output(self):
        self.open_session()
        listing = await self.listed()
        # The owned output shows 11 and holds 6, 9 and 11, so `e+1` wraps to 6.
        status, value = await self.invoke(self.row(listing, "SUPER + TAB"), listing)
        self.assertEqual(status, 200, value)
        self.assertIn('workspace = "6"', self.host.evaluated[0])
        self.assertEqual(value["observed"]["redirected_output"], OWNED_OUTPUT)

    async def test_in_a_session_previous_workspace_steps_the_other_way(self):
        self.open_session()
        listing = await self.listed()
        status, value = await self.invoke(self.row(listing, "SUPER SHIFT + TAB"), listing)
        self.assertEqual(status, 200, value)
        self.assertIn('workspace = "9"', self.host.evaluated[0])

    async def test_move_window_to_workspace_is_never_redirected(self):
        self.open_session()
        listing = await self.listed()
        status, value = await self.invoke(self.row(listing, "SUPER SHIFT + 3"), listing)
        self.assertEqual(status, 200, value)
        self.assertEqual(self.host.evaluated, [])
        self.assertEqual(self.host.dispatched[-1], ("lua", 'hl.dsp.window.move({ workspace = "3" })'))
        self.assertNotIn("redirected_output", value["observed"])

    async def test_the_forms_this_does_not_claim_to_understand_stay_verbatim(self):
        # Only `hl.dsp.focus({ workspace = ... })` is rewritten. A monitor-relative
        # focus, the special workspace and `previous` all keep the host's own
        # expression, because none of them names a workspace to pull.
        self.open_session()
        listing = await self.listed()
        cases = (("CTRL ALT + TAB", 'hl.dsp.focus({ monitor = "+1" })'),
                 ("SUPER CTRL + TAB", 'hl.dsp.focus({ workspace = "previous" })'),
                 ("SUPER + S", 'hl.dsp.workspace.toggle_special("scratchpad")'))
        for index, (display, expected) in enumerate(cases):
            with self.subTest(display=display):
                item = self.row(listing, display)
                status, value = await self.invoke(item, listing, request_id="click-verbatim-%d" % index)
                self.assertEqual(status, 200, value)
                self.assertEqual(self.host.dispatched[-1], ("lua", expected))
                self.assertNotIn("redirected_output", value["observed"])
        self.assertEqual(self.host.evaluated, [])

    async def test_a_compositor_refusal_of_the_redirect_is_reported_either_way(self):
        # `hyprctl eval` answers a Lua `error(...)` with a non-zero exit, which
        # the bounded runner raises rather than returns. Both shapes have to
        # read as the same failure code on the wire.
        self.open_session()
        from omodachi_core.graphical import GraphicalUnavailable
        def runner(argv, env):
            if argv[:2] == ("/usr/bin/hyprctl", "eval"):
                raise GraphicalUnavailable("graphical_command_failed")
            return RemoteHost.runner(self.host, argv, env)
        self.provider.runner = runner
        listing = await self.listed()
        status, value = await self.invoke(self.row(listing, "SUPER + 3"), listing)
        self.assertEqual(status, 200, value)
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["code"], "shortcut_execution_failed")

    async def test_a_refused_redirect_is_a_failure_not_a_silent_success(self):
        self.open_session()
        self.host.status = "ignored"
        def runner(argv, env):
            if argv[:2] == ("/usr/bin/hyprctl", "eval"):
                self.host.evaluated.append(argv[2])
                return "error: omodachi_workspace_focus_failed"
            return RemoteHost.runner(self.host, argv, env)
        self.provider.runner = runner
        listing = await self.listed()
        status, value = await self.invoke(self.row(listing, "SUPER + 3"), listing)
        self.assertEqual(status, 200, value)
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["code"], "shortcut_execution_failed")
