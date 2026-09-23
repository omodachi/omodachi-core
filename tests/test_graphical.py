from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from omodachi_core.bootstrap import create_service
from omodachi_core.graphical import graphical_environment, GraphicalUnavailable, HyprlandWorkspaceAdapter
from omodachi_core.hub import Hub


class GraphicalTests(unittest.TestCase):
    def test_only_exact_same_user_shell_is_selected_and_ambiguity_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            proc = Path(root)
            def process(pid, comm, signature):
                p = proc / str(pid); p.mkdir()
                (p / "comm").write_text(comm + "\n")
                (p / "cmdline").write_bytes(b"quickshell\0-p\0/usr/share/omarchy/shell\0")
                (p / "environ").write_bytes((f"HYPRLAND_INSTANCE_SIGNATURE={signature}\0XDG_RUNTIME_DIR=/run/user/{os.getuid()}\0WAYLAND_DISPLAY=wayland-1\0SECRET=not-exported\0").encode())
            process(1, "bash", "wrong")
            with self.assertRaises(GraphicalUnavailable): graphical_environment(proc)
            process(2, "quickshell", "correct")
            env = graphical_environment(proc)
            self.assertEqual(env["HYPRLAND_INSTANCE_SIGNATURE"], "correct")
            self.assertNotIn("SECRET", env)
            process(3, "quickshell", "second-instance")
            with self.assertRaises(GraphicalUnavailable): graphical_environment(proc)

    def host(self, root, *, shell=True, comm="Hyprland", display="wayland-1"):
        """The compositor's runtime dir as Hyprland actually lays it out."""
        import socket
        proc, runtime = Path(root) / "proc", Path(root) / "run"
        # The real signature is `<commit>_<boot>_<random>`; a short one keeps the
        # sockets inside the platform's AF_UNIX path limit.
        signature = "efb50993_1789614994"
        instance = runtime / "hypr" / signature
        instance.mkdir(parents=True)
        for path in (instance / ".socket.sock", instance / ".socket2.sock", runtime / display):
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            self.addCleanup(listener.close)
        (instance / "hyprland.lock").write_text(f"1472\n{display}\n")
        compositor = proc / "1472"; compositor.mkdir(parents=True)
        (compositor / "comm").write_text(comm + "\n")
        if shell:
            quickshell = proc / "325275"; quickshell.mkdir()
            (quickshell / "comm").write_text("quickshell\n")
            (quickshell / "cmdline").write_bytes(b"quickshell\0-n\0-p\0/usr/share/omarchy/shell\0")
            (quickshell / "environ").write_bytes(
                (f"HYPRLAND_INSTANCE_SIGNATURE={signature}\0XDG_RUNTIME_DIR=/run/user/{os.getuid()}"
                 f"\0WAYLAND_DISPLAY={display}\0").encode())
        return proc, runtime, signature

    def test_the_compositor_answers_for_itself_while_the_shell_is_restarting(self):
        with tempfile.TemporaryDirectory() as root:
            # omarchy-hyprland-monitor-watch restarts the Omarchy shell whenever
            # the monitors move; the compositor never went anywhere.
            proc, runtime, signature = self.host(root, shell=False)
            env = graphical_environment(proc, runtime=runtime)
            self.assertEqual(env["HYPRLAND_INSTANCE_SIGNATURE"], signature)
            self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-1")
            self.assertEqual(env["XDG_RUNTIME_DIR"], str(runtime))

    def test_a_dead_compositor_pid_falls_back_to_the_shell(self):
        with tempfile.TemporaryDirectory() as root:
            proc, runtime, signature = self.host(root, comm="bash")
            env = graphical_environment(proc, runtime=runtime)
            self.assertEqual(env["HYPRLAND_INSTANCE_SIGNATURE"], signature)

    def test_no_compositor_and_no_shell_is_unavailable(self):
        with tempfile.TemporaryDirectory() as root:
            proc, runtime, _ = self.host(root, shell=False, comm="bash")
            with self.assertRaises(GraphicalUnavailable):
                graphical_environment(proc, runtime=runtime)

    def test_real_adapter_redacts_titles_and_only_dispatches_known_workspace(self):
        service = create_service(Hub(), demo=True)
        active = [2]
        calls = []
        def runner(argv, env):
            calls.append(argv)
            if argv[1] == "eval":
                active[0] = 7
                return "ok\n"
            if argv[-1] == "activeworkspace": return json.dumps({"id":active[0], "lastwindowtitle":"secret title"})
            if argv[-1] == "workspaces": return json.dumps([{"id":2,"windows":1,"lastwindowtitle":"secret title"},{"id":7,"windows":0}])
            return json.dumps({"address":"0xabcd", "class":"foot", "title":"secret title", "pid":999})
        adapter = HyprlandWorkspaceAdapter(service, runner=runner, environment=lambda: {})
        adapter.refresh()
        state = service.state("phone")
        self.assertEqual(state["host"]["graphical_state"], "available")
        self.assertEqual(state["workspace"]["active"], 2)
        self.assertEqual(state["focus"]["app_id"], "foot")
        self.assertNotIn("secret title", json.dumps(state))
        self.assertNotIn("0xabcd", json.dumps(state))
        result = service.invoke({"entry_id":"omodachi.workspace.select.7", "request_id":"one", "catalog_revision":state["catalog"]["revision"], "params":{}}, "phone")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(service.state("phone")["workspace"]["active"], 7)
        dispatch = [call for call in calls if call[1] == "eval"]
        self.assertEqual(len(dispatch),1)
        self.assertIn('hl.get_workspace(7)',dispatch[0][2])
        self.assertIn('or "7"',dispatch[0][2])
        with self.assertRaises(ValueError): adapter.select(("/bin/sh", "-c", "client input"))
        with self.assertRaises(ValueError):
            service.invoke({"entry_id":"omodachi.workspace.select.7", "request_id":"two", "catalog_revision":service.refresh_catalog()["revision"], "params":{"command":"sh"}}, "phone")

    def test_graphical_failure_marks_unknown_and_disables_actions(self):
        service = create_service(Hub(), demo=True)
        def unavailable(): raise GraphicalUnavailable("missing")
        adapter = HyprlandWorkspaceAdapter(service, environment=unavailable)
        adapter.refresh()
        state = service.state("phone")
        self.assertEqual(state["host"]["graphical_state"], "unavailable")
        self.assertIsNone(state["workspace"]["active"])
        self.assertTrue(all(row["occupied"] is None for row in state["workspace"]["items"]))
        entry = next(e for e in state["catalog"]["entries"] if e["id"] == "omodachi.workspace.select.2")
        self.assertFalse(entry["route"]["ready"])

    def test_non_demo_uses_packaged_own_layer_without_omarchy_fixture_fallback(self):
        with tempfile.TemporaryDirectory() as root, patch("pathlib.Path.home", return_value=Path(root)):
            service = create_service(Hub(), default_menu=Path(root)/"missing", user_menu=Path(root)/"missing-user")
            ids = {e["id"] for e in service.refresh_catalog()["entries"]}
            self.assertIn("omodachi.workspace.select.3", ids)
            self.assertNotIn("apps.demo", ids)
            self.assertEqual(service.hub.state_snapshot()["host"]["source"], "local")
            self.assertTrue(service.hub.state_snapshot()["host"]["catalog_stale"])

    def test_move_targets_private_address_and_stale_focus_never_dispatches(self):
        service = create_service(Hub(), demo=True)
        active_address = ["0xabcd"]
        target_workspace = [2]
        calls = []
        def runner(argv, env):
            calls.append(argv)
            if argv[1] == "dispatch":
                target_workspace[0] = 4
                return "ok"
            if argv[-1] == "clients": return json.dumps([{"address":"0xabcd", "workspace":{"id":target_workspace[0]}, "title":"private"}])
            if argv[-1] == "activeworkspace": return json.dumps({"id":2})
            if argv[-1] == "workspaces": return json.dumps([{"id":2,"windows":int(target_workspace[0]==2)},{"id":4,"windows":int(target_workspace[0]==4)}])
            return json.dumps({"address":active_address[0],"class":"foot"})
        adapter = HyprlandWorkspaceAdapter(service, runner=runner, environment=lambda: {})
        service.set_workspace_adapter(adapter)
        adapter.refresh()
        state = service.state("phone")
        request = {"entry_id":"omodachi.workspace.move.4", "request_id":"move-one", "catalog_revision":state["catalog"]["revision"], "params":{}, "state_revision":state["revision"], "target_token":state["focus"]["target_token"]}
        active_address[0] = "0x9999"
        with self.assertRaisesRegex(ValueError, "stale_target"):
            service.invoke(request, "phone")
        self.assertFalse(any(call[1] == "dispatch" for call in calls))
        active_address[0] = "0xabcd"
        adapter.refresh();state=service.state("phone")
        request.update(request_id="move-two", state_revision=state["revision"], target_token=state["focus"]["target_token"], catalog_revision=state["catalog"]["revision"])
        self.assertEqual(service.invoke(request,"phone")["status"], "accepted")
        self.assertIn(("/usr/bin/hyprctl", "dispatch", 'hl.dsp.window.move({ workspace = "4", follow = false, window = "address:0xabcd" })'), calls)

    def test_real_ids_above_ten_are_projected_and_only_existing_can_be_selected(self):
        service=create_service(Hub(),demo=True);active=[23];calls=[]
        def runner(argv,env):
            calls.append(argv)
            if argv[1]=='eval':active[0]=42;return 'ok'
            if argv[-1]=='workspaces':return json.dumps([{'id':23,'windows':2},{'id':42,'windows':0},{'id':-99,'windows':1}])
            if argv[-1]=='activeworkspace':return json.dumps({'id':active[0]})
            return '{}'
        adapter=HyprlandWorkspaceAdapter(service,runner=runner,environment=lambda:{})
        service.set_workspace_adapter(adapter);adapter.refresh()
        # The official bar draws 1-5 always and adds any other live workspace,
        # so the rows are the fixed five plus the two real ones above ten.
        state=service.state('phone');self.assertEqual([w['id'] for w in state['workspace']['items']],[1,2,3,4,5,23,42])
        self.assertEqual([w['id'] for w in state['workspace']['items'] if w['persistent']],[1,2,3,4,5])
        # A persistent row Hyprland did not list exists and holds nothing;
        # the two real ones keep their observed counts.
        self.assertEqual([w['occupied'] for w in state['workspace']['items']],[False]*5+[True,False])
        self.assertEqual(state['workspace']['active'],23)
        self.assertIsNone(next(w for w in state['workspace']['items'] if w['id']==23)['select_entry_id'])
        result=service.dispatch('workspace.select',{'workspace_id':42},'phone')
        self.assertEqual(result['workspace']['active'],42)
        count=sum(c[1]=='eval' for c in calls)
        with self.assertRaisesRegex(ValueError,'workspace_unavailable'):
            service.dispatch('workspace.select',{'workspace_id':24},'phone')
        self.assertEqual(sum(c[1]=='eval' for c in calls),count)

    def test_every_officially_drawn_workspace_is_ready_and_selectable_when_empty(self):
        """SPEC-F2 §7.4: workspace 3 is drawn, holds nothing, and must select.

        The compositor lists only the workspaces it has materialised. The
        official bar draws the fixed persistent rows regardless, so a client
        can see and tap one the compositor has never created.
        """
        service=create_service(Hub(),demo=True);active=[11];live=[11];calls=[]
        def runner(argv,env):
            calls.append(argv)
            if argv[1]=='eval':
                active[0]=3
                if 3 not in live:live.append(3)
                return 'ok'
            if argv[-1]=='workspaces':return json.dumps([{'id':n,'windows':1} for n in live])
            if argv[-1]=='activeworkspace':return json.dumps({'id':active[0]})
            return '{}'
        adapter=HyprlandWorkspaceAdapter(service,runner=runner,environment=lambda:{})
        service.set_workspace_adapter(adapter);adapter.refresh()
        state=service.state('phone')
        # 1-5 always, plus the one live workspace; 3 is drawn and holds nothing.
        self.assertEqual([w['id'] for w in state['workspace']['items']],[1,2,3,4,5,11])
        third=next(w for w in state['workspace']['items'] if w['id']==3)
        self.assertEqual((third['persistent'],third['occupied']),(True,False))
        catalog={row['id']:row for row in state['catalog']['entries']}
        for number in (1,2,3,4,5,11):
            entry=catalog.get(f'omodachi.workspace.select.{number}')
            if entry is not None:
                self.assertTrue(entry['route']['ready'],f'select.{number} is not ready')
        result=service.dispatch('workspace.select',{'workspace_id':3},'phone')
        self.assertEqual(result['workspace']['active'],3)
        dispatched=[call for call in calls if call[1]=='eval']
        self.assertEqual(len(dispatched),1)
        # The number selector is what creates the persistent row Hyprland had
        # not materialised; the object lookup is still preferred when it exists.
        self.assertIn('hl.get_workspace(3) or "3"',dispatched[0][2])

    # --- ARCH-1 / A-64 + review item 24: the host names the neighbour ---------
    def _relative_service(self, *, live=(1, 2, 3, 4, 5, 23), active=23):
        service = create_service(Hub(), demo=True)
        state = {"active": active, "selected": []}
        def runner(argv, env):
            if argv[1] == 'eval':
                # The eval command carries the number; read it back rather than
                # trusting the caller, which is the whole point of this test.
                number = int(argv[2].split('hl.get_workspace(')[1].split(')')[0])
                state["selected"].append(number)
                state["active"] = number
                return 'ok'
            if argv[-1] == 'workspaces':
                return json.dumps([{'id': n, 'windows': 1} for n in live])
            if argv[-1] == 'activeworkspace':
                return json.dumps({'id': state["active"]})
            return '{}'
        adapter = HyprlandWorkspaceAdapter(service, runner=runner, environment=lambda: {})
        service.set_workspace_adapter(adapter)
        adapter.refresh()
        return service, state

    def test_a_relative_selection_is_resolved_against_the_published_collection(self):
        """`e+1` / `e-1` exist so a client never computes its own neighbour.

        The bar's collection is "the fixed five plus whatever exists", so the
        neighbour of 5 is 23 and not 6 — a number the client's own arithmetic
        would happily have produced from a stale snapshot.
        """
        service, state = self._relative_service(active=5)
        result = service.dispatch('workspace.select', {'relative': 'e+1'}, 'phone')
        self.assertEqual(state["selected"], [23])
        self.assertEqual(result['workspace']['active'], 23)

    def test_a_relative_selection_wraps_at_both_ends(self):
        service, state = self._relative_service(active=1)
        service.dispatch('workspace.select', {'relative': 'e-1'}, 'phone')
        self.assertEqual(state["selected"], [23])
        service, state = self._relative_service(active=23)
        service.dispatch('workspace.select', {'relative': 'e+1'}, 'phone')
        self.assertEqual(state["selected"], [1])

    def test_only_the_two_relative_forms_are_accepted_and_never_both_fields(self):
        service, _ = self._relative_service()
        for value in ('e+2', '+1', 'next', '', None, 1, ['e+1']):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'invalid_request'):
                service.dispatch('workspace.select', {'relative': value}, 'phone')
        with self.assertRaisesRegex(ValueError, 'invalid_request'):
            service.dispatch('workspace.select', {'relative': 'e+1', 'workspace_id': 2}, 'phone')
        with self.assertRaisesRegex(ValueError, 'invalid_request'):
            service.dispatch('workspace.select', {}, 'phone')

    def test_a_relative_selection_with_no_reading_is_refused_rather_than_guessed(self):
        service = create_service(Hub(), demo=True)
        def runner(argv, env):
            if argv[-1] == 'workspaces':return json.dumps([])
            if argv[-1] == 'activeworkspace':return json.dumps({})
            return '{}'
        adapter = HyprlandWorkspaceAdapter(service, runner=runner, environment=lambda: {})
        service.set_workspace_adapter(adapter); adapter.refresh()
        with self.assertRaisesRegex(ValueError, 'workspace_unavailable'):
            service.dispatch('workspace.select', {'relative': 'e+1'}, 'phone')

    # --- ARCH-1 / A-64: in a session the squares act on our own screen -------
    class _Session:
        def __init__(self, output_name): self.output_name = output_name

    class _Manager:
        def __init__(self, session): self.session = session
        def current(self): return self.session

    def _session_service(self, *, output="OMODACHI-1", live=(1, 2, 3, 4, 5, 23), active=5):
        """The relative fixture, plus a Remote session that owns `output`."""
        service = create_service(Hub(), demo=True)
        state = {"active": active, "commands": []}
        def runner(argv, env):
            if argv[1] == 'eval':
                state["commands"].append(argv[2])
                if 'hl.dsp.workspace.move' in argv[2]:
                    number = int(json.loads(argv[2].split('workspace = ')[1].split(',')[0]))
                else:
                    number = int(argv[2].split('hl.get_workspace(')[1].split(')')[0])
                state["active"] = number
                return 'ok'
            if argv[-1] == 'workspaces':
                return json.dumps([{'id': n, 'windows': 1} for n in live])
            if argv[-1] == 'activeworkspace':
                return json.dumps({'id': state["active"]})
            return '{}'
        adapter = HyprlandWorkspaceAdapter(service, runner=runner, environment=lambda: {})
        service.set_workspace_adapter(adapter); adapter.refresh()
        service.remote.manager = self._Manager(self._Session(output))
        return service, state

    def test_a_square_tapped_during_a_session_pulls_the_workspace_to_our_output(self):
        """It used to be refused outright with `remote_session_required`.

        Study 04 A-64: a workspace square acts on the screen in front of the
        user, and during a session that screen is the output we made. So the
        workspace is moved here and then focused — the pair Hyprland's own
        `focusworkspaceoncurrentmonitor` performs — rather than focused where
        it already lives, which would jump the laptop panel instead.
        """
        service, state = self._session_service()
        result = service.dispatch('workspace.select', {'workspace_id': 3}, 'phone')
        self.assertEqual(result['workspace']['active'], 3)
        self.assertEqual(len(state["commands"]), 1)
        command = state["commands"][0]
        self.assertIn('hl.dsp.workspace.move({ workspace = "3", monitor = "OMODACHI-1" })', command)
        self.assertIn('hl.dsp.focus({ workspace = "3" })', command)
        self.assertNotIn('hl.get_workspace', command)

    def test_the_relative_form_goes_to_our_output_too(self):
        service, state = self._session_service(active=5)
        service.dispatch('workspace.select', {'relative': 'e+1'}, 'phone')
        # `e+1` still names the bar's neighbour (23, not 6); only where it is
        # focused changed.
        self.assertIn('workspace = "23", monitor = "OMODACHI-1"', state["commands"][0])

    def test_with_no_session_the_square_is_the_plain_focus_it_always_was(self):
        service, state = self._relative_service(active=5)
        service.dispatch('workspace.select', {'workspace_id': 3}, 'phone')
        self.assertEqual(state["selected"], [3])

    def test_a_session_with_no_named_output_is_refused_rather_than_sent_anywhere(self):
        service, state = self._session_service(output="")
        with self.assertRaisesRegex(ValueError, 'workspace_unavailable'):
            service.dispatch('workspace.select', {'workspace_id': 3}, 'phone')
        self.assertFalse(state["commands"])

    def test_a_workspace_outside_the_drawn_collection_is_still_refused(self):
        service=create_service(Hub(),demo=True);calls=[]
        def runner(argv,env):
            calls.append(argv)
            if argv[1]=='eval':return 'ok'
            if argv[-1]=='workspaces':return json.dumps([{'id':11,'windows':1}])
            if argv[-1]=='activeworkspace':return json.dumps({'id':11})
            return '{}'
        adapter=HyprlandWorkspaceAdapter(service,runner=runner,environment=lambda:{})
        service.set_workspace_adapter(adapter);adapter.refresh()
        self.assertNotIn(12,[w['id'] for w in service.state('phone')['workspace']['items']])
        with self.assertRaisesRegex(ValueError,'workspace_unavailable'):
            service.dispatch('workspace.select',{'workspace_id':12},'phone')
        self.assertFalse([call for call in calls if call[1]=='eval'])


class WorkspaceFocusHelperTests(unittest.TestCase):
    """The two pure helpers the Remote-session workspace rewrite is built from.

    `workspace.select`'s own relative form is ARCH-1 #24
    (`CoreService.relative_workspace`); these are only the pieces
    `ShortcutProvider` needs to replay a binding on a session's own output.
    """

    def test_step_workspace_wraps_over_the_existing_collection(self):
        from omodachi_core.graphical import step_workspace
        self.assertEqual(step_workspace(2, [1, 2, 5], 1), 5)
        self.assertEqual(step_workspace(5, [1, 2, 5], 1), 1)
        self.assertEqual(step_workspace(1, [1, 2, 5], -1), 5)
        # Nothing to step to, or a current workspace outside the collection.
        self.assertIsNone(step_workspace(1, [1], 1))
        self.assertIsNone(step_workspace(9, [1, 2], 1))
        self.assertIsNone(step_workspace(None, [1, 2], 1))

    def test_focus_workspace_on_output_is_the_move_then_focus_pair(self):
        from omodachi_core.graphical import focus_workspace_on_output
        command = focus_workspace_on_output("3", "OMODACHI-0123456789abcdef")
        self.assertIn('hl.dsp.workspace.move({ workspace = "3", monitor = "OMODACHI-0123456789abcdef" })', command)
        self.assertIn('hl.dsp.focus({ workspace = "3" })', command)
        self.assertIn("omodachi_workspace_focus_failed", command)
        for workspace, output in (("0", "eDP-1"), ("e+1", "eDP-1"), ('3"', "eDP-1"),
                                  ("3", ""), ("3", 'eDP-1"; evil')):
            with self.subTest(workspace=workspace, output=output), self.assertRaises(GraphicalUnavailable):
                focus_workspace_on_output(workspace, output)
