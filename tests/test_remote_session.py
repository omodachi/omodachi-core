"""RemoteManager against the synthetic host: both modes, both backends, recovery."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from omodachi_core.official_bar_position import OfficialBarPosition
from omodachi_core.remote import RemoteError, RemoteManager
from omodachi_core.remote.session import RECONFIGURE_ATTEMPTS
from omodachi_core.remote.backends import managed_sunshine_assets, SunshineBackend, VncBackend
from omodachi_core.remote.hyprland import CommandResult, Hyprland, OWNED_NAME
from omodachi_core.remote.journal import Journal
from omodachi_core.remote.profile import EncoderLimits
from omodachi_core.remote.idle import OmarchyIdle
from tests.remote_fakes import (FakeBar, FakeCompositor, FakeIdle, FakeShell, FakeSunshine, FakeWayVNC, INSTANCE,
                                profile_request)

ENCODER = EncoderLimits(4096, 4096, 16_777_216, 60, 40_000, 2, 2)
FINGERPRINT = "a" * 64


class RemoteHarness(unittest.TestCase):
    auto_layout = False

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # One ordered record of everything the manager does to the host, so a
        # test can say *when* something happened and not only that it did.
        self.timeline = []
        self.compositor = FakeCompositor(auto_layout=self.auto_layout, log=self.timeline)
        self.sunshine = FakeSunshine()
        self.bar = FakeBar()
        self.idle = FakeIdle(log=self.timeline)
        self.shell = FakeShell(self.root)
        self.events = []
        self.now = [1000.0]
        # The asset probe reads a systemd unit; the harness answers "we did not
        # look" so no test depends on this machine having one.
        self.assets = {"present": None}
        self.manager = RemoteManager(
            hyprland=Hyprland(INSTANCE, runner=self.compositor, sleep=lambda _seconds: None),
            journal_dir=self.root / "remote",
            encoder=ENCODER,
            sunshine=SunshineBackend(self.sunshine, certificate_resolver=lambda device: FINGERPRINT,
                                     address=lambda: "192.168.1.11", assets=lambda: self.assets),
            vnc=VncBackend(FakeWayVNC.factory()),
            bar_position=OfficialBarPosition(home=self.root, read_position=self.bar.read,
                                             set_position=self.bar.set),
            idle=OmarchyIdle(runner=self.idle.run, sleep=lambda _seconds: None),
            shell=self.shell,
            monotonic=lambda: self.now[0],
            events=lambda session, reason: self.events.append((session["state"], reason)))

    def start(self, **payload):
        request = dict(profile_request(), **payload)
        return self.manager.create("ipad-a", request)

    def physical(self):
        return [row for row in self.compositor.rows if not OWNED_NAME.fullmatch(row["name"])]

    def owned(self):
        return [row for row in self.compositor.rows if OWNED_NAME.fullmatch(row["name"])]

    def journals(self):
        return sorted((self.root / "remote").glob("OMODACHI-*.json"))


class ExtendTests(RemoteHarness):
    def test_create_configures_owned_output_and_leaves_the_physical_layout(self):
        before = json.dumps(self.physical(), sort_keys=True)
        session = self.start(backend="vnc")
        self.assertEqual((session.state, session.mode, session.backend), ("ready", "extend", "vnc"))
        self.assertEqual(len(self.owned()), 1)
        owned = self.owned()[0]
        self.assertEqual((owned["width"], owned["height"]),
                         (session.profile.output_mode_pixels.width, session.profile.output_mode_pixels.height))
        self.assertEqual(owned["scale"], session.profile.output_scale)
        self.assertEqual(json.dumps(self.physical(), sort_keys=True), before)
        self.assertEqual(session.connection["backend"], "vnc")
        # The client is handed a path on the one pinned TLS connection, never a
        # port: the loopback listener does not exist outside this process.
        self.assertEqual(session.connection["transport"], "wss")
        self.assertEqual(session.connection["path"], "/v1/remote/sessions/%s/vnc" % session.id)
        self.assertNotIn("port", session.connection)
        # REMOTE-6: the VNC output carries the device's pixels, so WayVNC's two
        # coordinate spaces are two numbers and the document names both.
        self.assertEqual(session.profile.output_scale, 2.0)
        self.assertEqual(session.connection["framebuffer_pixels"],
                         session.profile.output_mode_pixels.to_dict())
        self.assertEqual(session.connection["framebuffer_pixels"],
                         session.profile.stream_pixels.to_dict())
        # Core settles WayVNC first, so the client's own ServerInit is already
        # the buffer pixels and there is no mid-stream correction to follow.
        self.assertTrue(FakeWayVNC.instances[session.id].settled)
        self.assertEqual(session.connection["initial_framebuffer_pixels"],
                         session.profile.output_mode_pixels.to_dict())
        self.assertEqual(len(self.journals()), 1)

    def test_a_prime_that_could_not_run_says_what_wayvnc_will_open_at(self):
        """REMOTE-6. The document never guesses: it names the size a client
        will meet. When WayVNC could not be settled, that is the compositor's
        logical size, and the client is on notice that a resize is coming."""
        original = FakeWayVNC.settle
        FakeWayVNC.settle = lambda self, timeout=4.0: None
        try:
            session = self.start(backend="vnc")
        finally:
            FakeWayVNC.settle = original
        self.assertEqual(session.connection["initial_framebuffer_pixels"],
                         {"width": round(session.profile.logical_size.width),
                          "height": round(session.profile.logical_size.height)})
        self.assertEqual(session.connection["framebuffer_pixels"],
                         session.profile.output_mode_pixels.to_dict())
        self.assertNotEqual(session.connection["framebuffer_pixels"],
                            session.connection["initial_framebuffer_pixels"])
        self.manager.release(session.id)

    def test_both_backends_plan_the_same_desktop(self):
        """REMOTE-6. The desktop a session gets does not depend on its backend.

        SPEC-E3 planned the VNC leg at scale 1 to avoid WayVNC's mid-stream
        resize, which halved its pixel density: the same iPad got a 2560x1920
        scale-2 desktop over Sunshine and a 1280x960 scale-1 one over VNC, and
        the second is what Leo saw as "the resolution is wrong". Now the two
        legs are planned identically and only the frame source differs.
        """
        vnc = self.start(backend="vnc")
        geometry = (vnc.profile.output_mode_pixels.to_dict(), vnc.profile.output_scale,
                    vnc.profile.logical_size.to_dict())
        self.manager.release(vnc.id)
        sunshine = self.start(backend="sunshine")
        self.assertEqual((sunshine.profile.output_mode_pixels.to_dict(), sunshine.profile.output_scale,
                          sunshine.profile.logical_size.to_dict()), geometry)
        # The host's render density is what both of them got.
        self.assertEqual(sunshine.profile.output_scale, self.manager.render_density)
        # The one thing that does still differ: WayVNC serves the whole
        # framebuffer, so its stream is the mode; Sunshine encodes to a budget.
        self.assertEqual(vnc.profile.stream_pixels.to_dict(), vnc.profile.output_mode_pixels.to_dict())
        self.assertLessEqual(sunshine.profile.stream_pixels.area, sunshine.profile.output_mode_pixels.area)
        self.manager.release(sunshine.id)

    def test_a_session_after_a_vnc_one_is_planned_from_scratch(self):
        """REMOTE-6 §3. Leo: after a VNC session, Sunshine stayed low-resolution.

        Nothing a previous session left behind may reach the next one's plan.
        The plan is a pure function of the backend, the host's density and the
        request the device sent, so this runs the sequence Leo ran — VNC, end
        it, Sunshine — and pins the second session's geometry to what a first
        Sunshine session would have got.
        """
        reference = self.start(backend="sunshine")
        wanted = (reference.profile.output_mode_pixels.to_dict(), reference.profile.output_scale,
                  reference.profile.logical_size.to_dict(), reference.profile.stream_pixels.to_dict())
        self.manager.release(reference.id)

        first = self.start(backend="vnc")
        self.manager.release(first.id)
        second = self.start(backend="sunshine")
        self.assertEqual((second.profile.output_mode_pixels.to_dict(), second.profile.output_scale,
                          second.profile.logical_size.to_dict(), second.profile.stream_pixels.to_dict()), wanted)
        self.assertNotEqual(second.output_name, first.output_name)
        self.manager.release(second.id)

    def test_an_output_the_previous_session_left_behind_does_not_shape_the_next_plan(self):
        """REMOTE-6 §3. The planner never reads the compositor, so prove it.

        A release that could not remove the owned output leaves a stale
        1280x960 headless monitor on the host. The next session must still be
        planned from its own request, and must configure its own new output.
        """
        stale = self.start(backend="vnc")
        stale_name, stale_profile = stale.output_name, stale.profile
        # Release without letting the output go: the shape a failed restore, a
        # killed daemon or a compositor that ignored the removal leaves behind.
        self.compositor.fail_removal = True
        try:
            self.manager.release(stale.id)
        except RemoteError:
            pass
        self.compositor.fail_removal = False
        self.assertIn(stale_name, [row["name"] for row in self.owned()])

        session = self.start(backend="sunshine")
        self.assertNotEqual(session.output_name, stale_name)
        self.assertEqual(session.profile.output_scale, self.manager.render_density)
        self.assertEqual(session.profile.output_mode_pixels, stale_profile.output_mode_pixels,
                         "both backends plan the same desktop, so the density is the tell")
        owned = next(row for row in self.owned() if row["name"] == session.output_name)
        self.assertEqual((owned["width"], owned["height"], owned["scale"]),
                         (session.profile.output_mode_pixels.width,
                          session.profile.output_mode_pixels.height, session.profile.output_scale))
        self.manager.release(session.id)
        self.compositor.rows = [row for row in self.compositor.rows if row["name"] != stale_name]

    def test_a_sunshine_session_keeps_the_hosts_render_density(self):
        session = self.start(backend="sunshine")
        self.assertEqual(session.profile.output_scale, 2.0)
        self.assertNotEqual(session.profile.output_mode_pixels.to_dict(),
                            {"width": round(session.profile.logical_size.width),
                             "height": round(session.profile.logical_size.height)})
        self.manager.release(session.id)

    def test_placement_positions_the_output_beside_the_physical_layout(self):
        for placement, expected in (("right", (1536, 0)), ("left", None), ("above", None), ("below", (0, 960))):
            session = self.start(backend="vnc", placement=placement)
            owned = self.owned()[0]
            width, height = session.profile.logical_size.width, session.profile.logical_size.height
            wanted = {"right": (1536, 0), "left": (-int(-(-width // 1)), 0), "above": (0, -int(-(-height // 1))),
                      "below": (0, 960)}[placement]
            self.assertEqual((owned["x"], owned["y"]), (int(wanted[0]), int(wanted[1])), placement)
            if expected is not None:
                self.assertEqual((owned["x"], owned["y"]), expected)
            self.manager.release(session.id)

    def test_resize_changes_the_mode_without_recreating_the_output(self):
        session = self.start(backend="vnc")
        name, revision = session.output_name, session.revision
        landscape = session.profile.output_mode_pixels
        session = self.manager.resize(session.id, {"expected_revision": revision,
                                                   "viewport_points": {"width": 834, "height": 1194},
                                                   "orientation": "portrait"})
        self.assertEqual(session.output_name, name)
        self.assertGreater(session.revision, revision)
        portrait = session.profile.output_mode_pixels
        self.assertLess(portrait.width, portrait.height)
        self.assertNotEqual((landscape.width, landscape.height), (portrait.width, portrait.height))
        self.assertEqual([row["name"] for row in self.owned()], [name])

    def test_stale_revision_is_refused_and_the_output_is_untouched(self):
        session = self.start(backend="vnc")
        before = json.dumps(self.compositor.rows, sort_keys=True)
        with self.assertRaises(RemoteError) as caught:
            self.manager.resize(session.id, {"expected_revision": session.revision - 1,
                                             "viewport_points": {"width": 834, "height": 1194},
                                             "orientation": "portrait"})
        self.assertEqual(caught.exception.code, "stale_revision")
        self.assertEqual(caught.exception.detail["revision"], session.revision)
        self.assertEqual(json.dumps(self.compositor.rows, sort_keys=True), before)

    def test_a_second_session_is_refused_with_the_existing_id(self):
        session = self.start(backend="vnc")
        with self.assertRaises(RemoteError) as caught:
            self.start(backend="vnc")
        self.assertEqual(caught.exception.code, "remote_session_exists")
        self.assertEqual(caught.exception.detail["session_id"], session.id)
        self.assertEqual(len(self.owned()), 1)

    def test_release_destroys_the_output_moves_windows_back_and_deletes_the_journal(self):
        session = self.start(backend="vnc")
        before = json.dumps(self.physical(), sort_keys=True)
        # A user moved an occupied workspace onto the remote screen.
        owned_id = self.owned()[0]["id"]
        next(row for row in self.compositor.workspaces if row[0] == 4)[1:] = [owned_id, 3]
        result = self.manager.release(session.id)
        self.assertEqual(result, {"released": True, "errors": []})
        self.assertEqual(self.owned(), [])
        self.assertEqual(json.dumps(self.physical(), sort_keys=True), before)
        self.assertEqual(next(row for row in self.compositor.workspaces if row[0] == 4), [4, 0, 3])
        self.assertEqual(self.journals(), [])
        self.assertFalse(FakeWayVNC.instances[session.id].running)

    def test_release_is_idempotent(self):
        session = self.start(backend="vnc")
        self.manager.release(session.id)
        self.assertEqual(self.manager.release(session.id), {"released": True, "errors": []})

    def test_heartbeat_timeout_restores_and_reports_the_reason(self):
        session = self.start(backend="vnc", ttl_seconds=30)
        self.assertIsNone(self.manager.maintain())
        self.now[0] += 31
        self.assertEqual(self.manager.maintain(), {"released": True, "errors": []})
        self.assertEqual(self.owned(), [])
        self.assertEqual(self.journals(), [])
        self.assertIn(("released", "heartbeat_timeout"), self.events)
        self.assertEqual(self.manager.state_projection()["session_id"], None)

    def test_heartbeat_extends_the_budget(self):
        session = self.start(backend="vnc", ttl_seconds=30)
        self.now[0] += 25
        self.assertEqual(self.manager.heartbeat(session.id), {"revision": session.revision, "state": "ready"})
        self.now[0] += 25
        self.assertIsNone(self.manager.maintain())

    def test_switching_backend_stops_and_releases_the_previous_one(self):
        session = self.start(backend="vnc")
        session = self.manager.switch_backend(session.id, {"expected_revision": session.revision,
                                                           "backend": "sunshine"})
        self.assertEqual(session.backend, "sunshine")
        self.assertEqual(session.connection["backend"], "sunshine")
        self.assertEqual(session.connection["host"], "192.168.1.11")
        self.assertEqual(session.connection["app_name"], "Omodachi Desktop")
        self.assertFalse(FakeWayVNC.instances[session.id].running)
        self.assertEqual(self.sunshine.output, session.output_name)

    def test_switching_backend_three_times_leaves_no_trace_of_the_previous_one(self):
        """REMOTE-6 item 3. The profile after a switch is the new backend's.

        The old shape — vnc at scale 1, sunshine at the host density — meant
        every switch also changed the desktop's geometry, so the picture jumped
        and, with `allow_dynamic_resolution` off, a switch could be refused
        outright as if it were a resize. Now both legs plan the same desktop,
        so a switch is only a change of frame source.
        """
        session = self.start(backend="vnc")
        planned = (session.profile.output_mode_pixels.to_dict(), session.profile.output_scale,
                   session.profile.logical_size.to_dict())
        owned_before = dict(self.owned()[0])
        seen = []
        for backend in ("sunshine", "vnc", "sunshine"):
            session = self.manager.switch_backend(session.id, {"expected_revision": session.revision,
                                                               "backend": backend})
            seen.append(session.backend)
            self.assertEqual(session.backend, backend)
            self.assertEqual(session.connection["backend"], backend)
            self.assertEqual((session.profile.output_mode_pixels.to_dict(), session.profile.output_scale,
                              session.profile.logical_size.to_dict()), planned)
            owned = self.owned()[0]
            self.assertEqual((owned["width"], owned["height"], owned["scale"]),
                             (owned_before["width"], owned_before["height"], owned_before["scale"]))
            if backend == "vnc":
                # Nothing of the Sunshine document survives into the VNC one.
                self.assertEqual(set(session.connection),
                                 {"backend", "transport", "path", "output_id",
                                  "initial_framebuffer_pixels", "framebuffer_pixels"})
                self.assertEqual(session.connection["framebuffer_pixels"],
                                 session.profile.output_mode_pixels.to_dict())
                self.assertEqual(FakeWayVNC.instances[session.id].pixels,
                                 session.profile.output_mode_pixels.to_dict())
                self.assertEqual(FakeWayVNC.instances[session.id].logical_size,
                                 session.profile.logical_size.to_dict())
            else:
                self.assertEqual(set(session.connection),
                                 {"backend", "host", "https_port", "app_name", "output_id",
                                  "stream_pixels", "fps"})
                self.assertFalse(FakeWayVNC.instances[session.id].running)
        self.assertEqual(seen, ["sunshine", "vnc", "sunshine"])
        self.manager.release(session.id)

    def test_a_switch_is_not_a_resize_so_a_host_that_forbids_resizing_allows_it(self):
        """REMOTE-6 item 3. `allow_dynamic_resolution: false` used to block it."""
        self.manager.allow_resize = lambda: False
        session = self.start(backend="vnc")
        session = self.manager.switch_backend(session.id, {"expected_revision": session.revision,
                                                           "backend": "sunshine"})
        self.assertEqual(session.backend, "sunshine")
        self.manager.release(session.id)

    def test_sunshine_prepare_sends_the_forks_exact_lease_and_identity(self):
        session = self.start(backend="sunshine")
        self.assertEqual(self.sunshine.lease, {"lease_id": session.id, "lease_epoch": 1,
                                               "owner_device_id": "ipad-a", "client_cert_sha256": FINGERPRINT})
        self.assertEqual(set(self.sunshine.identity),
                         {"lease_id", "lease_epoch", "transition_id", "geometry_epoch", "connection_generation"})
        self.assertEqual(self.sunshine.identity["geometry_epoch"], self.sunshine.identity["connection_generation"])

    def test_sunshine_without_a_paired_certificate_is_refused_before_any_mutation(self):
        self.manager.backends["sunshine"].certificate_resolver = lambda device: None
        before = json.dumps(self.compositor.rows, sort_keys=True)
        with self.assertRaises(RemoteError) as caught:
            self.start(backend="sunshine")
        self.assertEqual(caught.exception.code, "media_pairing_required")
        self.assertEqual(json.dumps(self.compositor.rows, sort_keys=True), before)
        self.assertEqual(self.journals(), [])

    def test_a_failed_backend_prepare_rolls_the_output_back(self):
        self.sunshine.busy = True
        before = json.dumps(self.compositor.rows, sort_keys=True)
        with self.assertRaises(RemoteError):
            self.start(backend="sunshine")
        self.assertEqual(json.dumps(self.compositor.rows, sort_keys=True), before)
        self.assertEqual(self.journals(), [])

    def test_unavailable_backend_reports_its_reason(self):
        self.manager.backends["vnc"] = VncBackend(FakeWayVNC.factory(available=False))
        with self.assertRaises(RemoteError) as caught:
            self.start(backend="vnc")
        self.assertEqual(caught.exception.code, "wayvnc_0_10_1_required")

    def test_capabilities_reports_both_backends_and_the_encoder(self):
        value = self.manager.capabilities()
        self.assertTrue(value["backends"]["sunshine"]["available"])
        self.assertTrue(value["backends"]["vnc"]["available"])
        self.assertEqual(value["modes"], ["extend", "takeover"])
        self.assertEqual(value["placement_options"], ["right", "left", "above", "below"])
        self.assertTrue(value["lock_local_input_supported"])
        self.assertEqual(value["encoder_limits"]["max_pixels"], 16_777_216)
        self.sunshine.available = False
        self.assertEqual(self.manager.capabilities()["backends"]["sunshine"],
                         {"available": False, "reason": "sunshine_desktop_unavailable"})

    def test_a_missing_fork_asset_tree_is_named_next_to_a_working_backend(self):
        """PERF-3: without assets the fork streams, on the CPU, saying nothing."""
        self.assets = {"present": False, "reason": "sunshine_assets_missing"}
        row = self.manager.capabilities()["backends"]["sunshine"]
        self.assertEqual((row["available"], row["reason"]), (True, "sunshine_assets_missing"))
        self.assets = {"present": True, "reason": None}
        self.assertIsNone(self.manager.capabilities()["backends"]["sunshine"]["reason"])
        # A unit we could not read is not a missing asset tree.
        self.assets = {"present": None, "reason": "sunshine_unit_unreadable"}
        self.assertIsNone(self.manager.capabilities()["backends"]["sunshine"]["reason"])

    def test_the_asset_probe_follows_the_units_own_execstart(self):
        root = self.root / "sunshine/4785729"
        (root / "assets/shaders/opengl").mkdir(parents=True)
        (root / "assets/shaders/opengl/Scene.frag").write_text("// shader")
        (root / "sunshine").write_text("#!/bin/sh\n")
        unit = ("{ path=" + str(root / "sunshine") + " ; argv[]=" + str(root / "sunshine")
                + " capture=wlr encoder=vaapi ; ignore_errors=no ; pid=1 }")
        result = managed_sunshine_assets(runner=lambda name: unit)
        self.assertEqual((result["present"], result["reason"]), (True, None))
        self.assertEqual(result["assets"], str(root / "assets"))
        # The same binary moved to another directory takes no assets with it.
        moved = unit.replace("/4785729/", "/fceb3ab/")
        stale = managed_sunshine_assets(runner=lambda name: moved)
        self.assertEqual((stale["present"], stale["reason"]), (False, "sunshine_assets_missing"))
        # An empty tree is as good as no tree; so is a unit that says nothing.
        (root / "assets/shaders/opengl/Scene.frag").unlink()
        self.assertFalse(managed_sunshine_assets(runner=lambda name: unit)["present"])
        blank = managed_sunshine_assets(runner=lambda name: "")
        self.assertEqual(blank, {"executable": None, "assets": None, "present": None,
                                 "reason": "sunshine_unit_unreadable"})

    def test_presented_validates_geometry_without_blocking_anything(self):
        session = self.start(backend="vnc")
        pixels = session.profile.stream_pixels.to_dict()
        scale = min(1194 / pixels["width"], 834 / pixels["height"])
        width, height = pixels["width"] * scale, pixels["height"] * scale
        good = self.manager.presented(session.id, {
            "revision": session.revision, "decoded_pixels": pixels,
            "video_rect_points": {"x": (1194 - width) / 2, "y": (834 - height) / 2, "width": width, "height": height}})
        self.assertTrue(good["accepted"])
        bad = self.manager.presented(session.id, {
            "revision": session.revision, "decoded_pixels": pixels,
            "video_rect_points": {"x": 0, "y": 0, "width": 100, "height": 40}})
        self.assertFalse(bad["accepted"])
        self.assertEqual(bad["reason"], "video_stretched")
        self.assertEqual(self.manager.get(session.id).state, "ready")

    def test_a_session_turns_the_idle_cycle_off_and_back_on(self):
        session = self.start(backend="vnc")
        self.assertFalse(self.idle.state)
        self.assertEqual(self.idle.calls, ["status", "disable"])
        record = json.loads(self.journals()[0].read_text())
        self.assertIs(record["idle_was_enabled"], True)
        self.manager.release(session.id)
        self.assertTrue(self.idle.state)
        self.assertEqual(self.idle.calls, ["status", "disable", "enable"])

    def test_an_already_idle_disabled_host_is_left_alone(self):
        self.idle.state = False
        session = self.start(backend="vnc")
        self.assertEqual(self.idle.calls, ["status"])
        self.manager.release(session.id)
        self.assertFalse(self.idle.state)
        self.assertEqual(self.idle.calls, ["status"])

    def test_a_host_without_omarchy_shell_still_runs_a_session(self):
        self.idle.available = False
        session = self.start(backend="vnc")
        self.assertIsNone(json.loads(self.journals()[0].read_text())["idle_was_enabled"])
        self.assertEqual(self.manager.release(session.id), {"released": True, "errors": []})

    def test_bar_projection_marks_the_remote_workspaces(self):
        session = self.start(backend="vnc")
        owned_id = self.owned()[0]["id"]
        next(row for row in self.compositor.workspaces if row[0] == 4)[1] = owned_id
        bar = self.manager.bar_projection()
        self.assertTrue(bar["active"])
        self.assertEqual(bar["session_id"], session.id)
        self.assertEqual(bar["orientation"], "landscape")
        self.assertTrue(next(row for row in bar["workspaces"] if row["id"] == 4)["remote"])
        self.assertFalse(next(row for row in bar["workspaces"] if row["id"] == 2)["remote"])


class DisplacedLayoutTests(RemoteHarness):
    auto_layout = True

    def test_a_relayout_of_the_physical_output_is_pinned_back(self):
        before = json.dumps(self.physical(), sort_keys=True)
        session = self.start(backend="vnc", placement="left")
        self.assertEqual(json.dumps(self.physical(), sort_keys=True), before)
        self.manager.release(session.id)
        self.assertEqual(json.dumps(self.physical(), sort_keys=True), before)


class TakeoverTests(RemoteHarness):
    def takeover(self, **payload):
        return self.start(mode="takeover", backend="vnc", **payload)

    def enter(self):
        before = json.dumps(self.physical(), sort_keys=True)
        session = self.takeover()
        owned_id = self.owned()[0]["id"]
        self.assertTrue(all(row[1] == owned_id for row in self.compositor.workspaces
                            if row[0] in {1, 2, 4}), self.compositor.workspaces)
        self.assertEqual(self.compositor.active, 2)
        self.assertFalse(self.physical()[0]["dpmsStatus"], "the screen in the room is dark")
        self.assertFalse(self.physical()[0]["disabled"], "…but still in the layout (HOST-1)")
        self.assertEqual(self.bar.position, "top")
        self.assertEqual(self.bar.calls, [])  # the fixture already sat on the long edge
        record = json.loads(self.journals()[0].read_text())
        self.assertEqual(record["takeover"]["blanked"], [{"name": "eDP-1", "method": "dpms"}])
        self.assertEqual(record["takeover"]["active"], [2, 0])
        self.assertEqual(record["takeover"]["inputs"], [])
        self.assertNotEqual(json.dumps(self.physical(), sort_keys=True), before)
        return session, before

    def test_takeover_moves_every_workspace_darkens_the_screen_and_sets_the_bar(self):
        self.enter()

    def test_release_restores_the_screen_workspaces_and_bar(self):
        session, before = self.enter()
        self.assertEqual(self.manager.release(session.id), {"released": True, "errors": []})
        self.assertEqual(json.dumps(self.physical(), sort_keys=True), before)
        self.assertEqual([row[:2] for row in self.compositor.workspaces if row[0] in {1, 2, 4}],
                         [[1, 0], [2, 0], [4, 0]])
        self.assertEqual(self.compositor.active, 2)
        self.assertEqual(self.owned(), [])
        self.assertEqual(self.journals(), [])

    def test_portrait_takeover_moves_the_bar_to_the_long_edge_and_restores_it(self):
        session = self.takeover(viewport_points={"width": 834, "height": 1194}, orientation="portrait")
        self.assertEqual(self.bar.position, "left")
        self.assertEqual(self.bar.calls, ["left"])
        self.manager.release(session.id)
        self.assertEqual(self.bar.position, "top")
        self.assertEqual(self.bar.calls, ["left", "top"])

    def test_a_user_bar_change_during_the_session_is_not_overwritten(self):
        session = self.takeover(viewport_points={"width": 834, "height": 1194}, orientation="portrait")
        self.assertEqual(self.bar.calls, ["left"])
        self.bar.position = "right"  # the user moved it themselves
        self.manager.release(session.id)
        self.assertEqual(self.bar.position, "right")
        self.assertEqual(self.bar.calls, ["left"])

    def test_lock_local_input_disables_only_physical_devices_and_restores_them(self):
        session = self.takeover(lock_local_input=True)
        self.assertEqual(sorted(self.compositor.disabled_devices),
                         ["apple-inc.-apple-internal-keyboard-/-trackpad",
                          "apple-inc.-apple-internal-keyboard-/-trackpad-1"])
        record = json.loads(self.journals()[0].read_text())
        # The fork's uinput pair and Hyprland's own virtual keyboard stay live,
        # or the remote client would lock itself out along with the user.
        for name in ("mouse-passthrough", "mouse-passthrough-(absolute)",
                     "keyboard-passthrough", "hl-virtual-keyboard-fcitx5"):
            self.assertNotIn(name, record["takeover"]["inputs"])
        self.manager.release(session.id)
        self.assertEqual(self.compositor.disabled_devices, [])

    def test_takeover_turns_the_single_window_aspect_ratio_off_and_puts_it_back(self):
        """PERF-2 §8: the host's 1:1 toggle squares the lone remote window."""
        session = self.takeover()
        self.assertEqual(self.compositor.single_window_aspect, [0.0, 0.0])
        record = json.loads(self.journals()[0].read_text())
        owned = record["takeover"]["single_window_aspect"]
        self.assertEqual(owned, {"state": "owned", "original": [1.0, 1.0], "was_set": True})
        self.manager.release(session.id)
        self.assertEqual(self.compositor.single_window_aspect, [1.0, 1.0])

    def test_an_extend_session_never_touches_the_global_aspect_ratio(self):
        session = self.start(backend="vnc")
        self.assertEqual(self.compositor.single_window_aspect, [1.0, 1.0])
        record = json.loads(self.journals()[0].read_text())
        self.assertIsNone(record["takeover"])
        self.manager.release(session.id)
        self.assertEqual(self.compositor.single_window_aspect, [1.0, 1.0])

    def test_a_host_that_never_set_it_is_left_alone_on_both_sides(self):
        self.compositor.single_window_aspect = [0.0, 0.0]
        self.compositor.single_window_aspect_set = False
        session = self.takeover()
        record = json.loads(self.journals()[0].read_text())
        self.assertEqual(record["takeover"]["single_window_aspect"]["state"], "already_off")
        self.manager.release(session.id)
        self.assertEqual((self.compositor.single_window_aspect,
                          self.compositor.single_window_aspect_set), ([0.0, 0.0], False))

    def test_a_config_reload_that_puts_the_ratio_back_is_re_applied(self):
        session = self.takeover()
        self.assertEqual(self.compositor.single_window_aspect, [0.0, 0.0])
        # `hyprctl reload` re-sources the user's toggle directory mid-session.
        self.compositor.single_window_aspect = [1.0, 1.0]
        self.assertEqual(self.manager.reconcile(), "single_window_aspect_reapplied")
        self.assertEqual(self.compositor.single_window_aspect, [0.0, 0.0])
        self.manager.release(session.id)
        self.assertEqual(self.compositor.single_window_aspect, [1.0, 1.0])

    def test_a_user_value_set_during_the_session_survives_the_restore(self):
        session = self.takeover()
        self.compositor.single_window_aspect = [16.0, 9.0]
        # A value that is not ours is the user's; the restore leaves it.
        self.assertEqual(self.manager.release(session.id), {"released": True, "errors": []})
        self.assertEqual(self.compositor.single_window_aspect, [16.0, 9.0])

    def test_a_leftover_takeover_journal_restores_the_ratio_too(self):
        self.takeover()
        self.manager.session = None
        self.compositor.single_window_aspect = [0.0, 0.0]
        self.manager.recover()
        self.assertEqual(self.compositor.single_window_aspect, [1.0, 1.0])
        self.assertEqual(self.journals(), [])

    def test_resize_keeps_the_takeover_and_updates_the_bar(self):
        session = self.takeover()
        self.assertEqual(self.bar.calls, [])
        session = self.manager.resize(session.id, {"expected_revision": session.revision,
                                                   "viewport_points": {"width": 834, "height": 1194},
                                                   "orientation": "portrait"})
        self.assertEqual(self.bar.position, "left")
        self.assertFalse(self.physical()[0]["dpmsStatus"], "a resize does not light the room back up")
        self.assertEqual(len(self.owned()), 1)
        self.manager.release(session.id)
        self.assertEqual(self.bar.position, "top")


class EphemeralWorkspaceTests(RemoteHarness):
    """Hyprland destroys an empty workspace that is moved off a monitor."""

    def setUp(self):
        super().setUp()
        self.compositor.drop_empty_on_move = True

    def test_a_vanished_empty_workspace_is_not_a_failed_takeover(self):
        session = self.start(mode="takeover", backend="vnc")
        self.assertEqual(session.state, "ready")
        owned = self.owned()[0]["id"]
        # Only the occupied ones had to survive the move; the empty one the
        # compositor created with the output is its own bookkeeping.
        self.assertEqual([row[0] for row in self.compositor.workspaces if row[1] == owned and row[2]], [2, 4])
        self.assertFalse(self.physical()[0]["dpmsStatus"])
        self.assertEqual(self.manager.release(session.id), {"released": True, "errors": []})
        self.assertTrue(self.physical()[0]["dpmsStatus"])
        self.assertEqual(sorted(row[:2] for row in self.compositor.workspaces if row[2]), [[2, 0], [4, 0]])
        self.assertEqual(self.owned(), [])


class AbsoluteInputMappingTests(RemoteHarness):
    """INPUT-1: the fork's touch devices belong to the captured output.

    The fork normalizes touch and pen coordinates to the output it captures and
    hands the backend 0..1; Hyprland is what places that box, by `device:output`.
    The absolute pointer is desktop-relative and is deliberately not bound.
    """

    DEVICES = ["pen-passthrough", "touch-passthrough"]

    def test_a_sunshine_session_maps_the_absolute_devices_to_its_own_output(self):
        session = self.start(backend="sunshine")
        self.assertEqual(sorted(self.compositor.device_outputs),  self.DEVICES)
        self.assertEqual(set(self.compositor.device_outputs.values()), {session.output_name})
        # Journaled before it is issued, like every other host mutation.
        record = json.loads(self.journals()[0].read_text())
        self.assertEqual(sorted(record["device_outputs"]), self.DEVICES)
        self.manager.release(session.id)
        self.assertEqual(self.compositor.device_outputs, {})

    def test_the_pointers_and_the_keyboard_are_never_mapped(self):
        """Measured on Hyprland 0.56.2: a pointer has no `output` option at all,
        and the fork's absolute coordinates are desktop-relative anyway."""
        session = self.start(backend="sunshine")
        for name in ("mouse-passthrough", "mouse-passthrough-(absolute)", "keyboard-passthrough",
                     "apple-inc.-apple-internal-keyboard-/-trackpad-1"):
            self.assertNotIn(name, self.compositor.device_outputs)
        self.manager.release(session.id)

    def test_takeover_maps_them_too(self):
        session = self.start(mode="takeover", backend="sunshine")
        self.assertEqual(sorted(self.compositor.device_outputs), self.DEVICES)
        self.assertEqual(set(self.compositor.device_outputs.values()), {session.output_name})
        self.manager.release(session.id)
        self.assertEqual(self.compositor.device_outputs, {})

    def test_a_vnc_session_maps_nothing(self):
        """WayVNC drives the compositor's own virtual pointer; there is no uinput device."""
        session = self.start(backend="vnc")
        self.assertEqual(self.compositor.device_outputs, {})
        self.assertEqual(json.loads(self.journals()[0].read_text())["device_outputs"], [])
        self.manager.release(session.id)

    def test_switching_from_vnc_to_sunshine_maps_them_then(self):
        session = self.start(backend="vnc")
        self.assertEqual(self.compositor.device_outputs, {})
        session = self.manager.switch_backend(session.id, {"expected_revision": session.revision,
                                                           "backend": "sunshine"})
        self.assertEqual(sorted(self.compositor.device_outputs), self.DEVICES)
        self.assertEqual(set(self.compositor.device_outputs.values()), {session.output_name})
        self.manager.release(session.id)
        self.assertEqual(self.compositor.device_outputs, {})

    def test_a_resize_does_not_map_the_same_device_twice(self):
        session = self.start(backend="sunshine")
        session = self.manager.resize(session.id, {"expected_revision": session.revision,
                                                   "viewport_points": {"width": 834, "height": 1194},
                                                   "orientation": "portrait"})
        mapped = json.loads(self.journals()[0].read_text())["device_outputs"]
        self.assertEqual(sorted(mapped), self.DEVICES)
        self.assertEqual(len(mapped), 2, "one entry per device, not one per reapply")
        self.manager.release(session.id)

    def test_a_leftover_journal_unbinds_them_after_a_daemon_restart(self):
        self.start(backend="sunshine")
        self.assertNotEqual(self.compositor.device_outputs, {})
        self.manager.session = None
        self.manager._record = None
        self.assertEqual(self.manager.recover()["recovered"][0]["errors"], [])
        self.assertEqual(self.compositor.device_outputs, {})

    def test_only_the_forks_devices_and_an_owned_output_are_addressable(self):
        """A physical pointer is never remapped, and never onto a physical screen."""
        hyprland = Hyprland(INSTANCE, runner=self.compositor)
        for name in ("apple-inc.-apple-internal-keyboard-/-trackpad-1",
                     "mouse-passthrough-(absolute)"):
            with self.assertRaises(RemoteError) as caught:
                hyprland.set_device_output(name, "OMODACHI-" + "c" * 16)
            self.assertEqual(caught.exception.code, "input_device_invalid")
        with self.assertRaises(RemoteError) as caught:
            hyprland.set_device_output("touch-passthrough", "eDP-1")
        self.assertEqual(caught.exception.code, "not_owned_headless_name")
        self.assertEqual(self.compositor.device_outputs, {})
        # Unbinding is the one call that needs no output.
        hyprland.set_device_output("touch-passthrough", None)
        self.assertEqual(self.compositor.device_outputs, {})

    def test_a_pre_input1_journal_has_nothing_to_unbind(self):
        session = self.start(backend="sunshine")
        journal = self.journals()[0]
        record = json.loads(journal.read_text())
        del record["device_outputs"]
        journal.write_text(json.dumps(record))
        self.manager._record = record
        self.assertEqual(self.manager.release(session.id)["errors"], [])


class RecoveryTests(RemoteHarness):
    def test_a_leftover_journal_is_finished_on_the_next_start(self):
        session = self.start(mode="takeover", backend="vnc")
        before = json.dumps([row for row in self.compositor.rows], sort_keys=True)
        self.assertFalse(self.physical()[0]["dpmsStatus"])
        # Simulate `kill -9`: the process is gone, the journal and the host are not.
        self.manager.session = None
        self.manager._record = None
        result = self.manager.recover()
        self.assertEqual(result["recovered"][0]["errors"], [])
        self.assertEqual(result["recovered"][0]["session_id"], session.id)
        self.assertTrue(self.physical()[0]["dpmsStatus"])
        self.assertEqual(self.owned(), [])
        self.assertEqual(self.journals(), [])
        self.assertNotEqual(before, json.dumps(self.compositor.rows, sort_keys=True))
        self.assertEqual([row[:2] for row in self.compositor.workspaces if row[0] in {1, 2, 4}],
                         [[1, 0], [2, 0], [4, 0]])

    def test_recover_removes_an_owned_output_that_has_no_journal_only_when_told_to(self):
        name = "OMODACHI-" + "b" * 16
        self.compositor(("hyprctl", "--instance", INSTANCE, "output", "create", "headless", name))
        orphan_id = self.compositor.row(name)["id"]
        next(row for row in self.compositor.workspaces if row[0] == 4)[1] = orphan_id
        # CORE-2 §2: without --orphans an output nobody here journaled is reported, not destroyed.
        result = self.manager.recover()
        self.assertEqual((result["orphan_outputs"], result["unowned_outputs"]), ([], [name]))
        self.assertEqual([row["name"] for row in self.owned()], [name])
        self.assertEqual(next(row for row in self.compositor.workspaces if row[0] == 4)[1], orphan_id)
        result = self.manager.recover(orphans=True)
        self.assertEqual((result["orphan_outputs"], result["unowned_outputs"]), ([name], []))
        self.assertEqual(self.owned(), [])
        self.assertEqual(next(row for row in self.compositor.workspaces if row[0] == 4), [4, 0, 1])

    def test_recover_with_nothing_outstanding_is_a_no_op(self):
        self.assertEqual(self.manager.recover(),
                         {"recovered": [], "orphan_outputs": [], "unowned_outputs": []})
        self.assertEqual(len(self.compositor.rows), 1)

    def second_manager(self, journal_dir):
        """Another RemoteManager on the *same* compositor - a side-by-side daemon."""
        return RemoteManager(
            hyprland=Hyprland(INSTANCE, runner=self.compositor, sleep=lambda _seconds: None),
            journal_dir=journal_dir, encoder=ENCODER,
            vnc=VncBackend(FakeWayVNC.factory()),
            bar_position=OfficialBarPosition(home=self.root / "side-home", read_position=self.bar.read,
                                             set_position=self.bar.set),
            idle=OmarchyIdle(runner=self.idle.run, sleep=lambda _seconds: None),
            monotonic=lambda: self.now[0])

    def test_a_second_daemon_starting_up_leaves_the_first_ones_live_session_alone(self):
        """HOST-1 §7.3 / REMOTE-4 §3.0: this is the start-up that ended a real session."""
        for mode in ("extend", "takeover"):
            with self.subTest(mode=mode):
                session = self.start(mode=mode, backend="vnc")
                output = session.output_name
                before = json.dumps(self.compositor.rows, sort_keys=True)
                side = self.second_manager(self.root / ("side-" + mode))
                result = side.recover()          # exactly what attach_transport runs at start
                self.assertEqual(result, {"recovered": [], "orphan_outputs": [], "unowned_outputs": [output]})
                self.assertEqual(json.dumps(self.compositor.rows, sort_keys=True), before)
                self.assertEqual(self.manager.current().id, session.id)
                self.assertEqual(self.manager.current().state, "ready")
                self.assertEqual(len(self.journals()), 1, "the first daemon's journal is untouched")
                # The first daemon still owns and can end its own session cleanly.
                self.manager.release(session.id)
                self.assertEqual(self.owned(), [])
                self.assertEqual(self.journals(), [])

    def test_the_second_daemon_only_finishes_its_own_journals(self):
        mine = self.start(backend="vnc")
        side = self.second_manager(self.root / "side")
        name = "OMODACHI-" + "c" * 16
        self.compositor(("hyprctl", "--instance", INSTANCE, "output", "create", "headless", name))
        # A journal in the side daemon's own directory for an output it made
        # and never cleaned up: that one *is* its to finish.
        Journal(self.root / "side" / (name + ".json")).write({
            "journal_version": 1, "kind": "omodachi.remote.session", "instance_id": INSTANCE,
            "id": "rs_" + "c" * 32, "device_id": "side", "backend": "vnc", "mode": "extend",
            "state": "ready", "revision": 1, "created_at": 1.0, "ttl_seconds": 30.0,
            "output_name": name, "placement": "right", "lock_local_input": False,
            "request": {}, "profile": None, "position": None, "baseline": [], "takeover": None,
            "prepared": False, "device_outputs": [], "idle_was_enabled": None})
        result = side.recover()
        self.assertEqual([row["output"] for row in result["recovered"]], [name])
        self.assertEqual(result["unowned_outputs"], [mine.output_name])
        self.assertEqual([row["name"] for row in self.owned()], [mine.output_name])
        self.manager.release(mine.id)

    def test_journal_recovery_also_restores_the_idle_cycle(self):
        self.start(mode="takeover", backend="vnc")
        self.assertFalse(self.idle.state)
        self.manager.session = None
        self.manager._record = None
        self.assertEqual(self.manager.recover()["recovered"][0]["errors"], [])
        self.assertTrue(self.idle.state)

    def test_every_restore_step_runs_even_when_the_backend_is_stuck(self):
        session = self.start(mode="takeover", backend="sunshine")
        self.sunshine.fail_stop = True
        result = self.manager.release(session.id)
        self.assertEqual([row["step"] for row in result["errors"]], ["backend"])
        # The screen and the workspaces still came back.
        self.assertFalse(self.physical()[0]["disabled"])
        self.assertEqual(self.owned(), [])
        self.assertEqual([row[:2] for row in self.compositor.workspaces if row[0] in {1, 2, 4}],
                         [[1, 0], [2, 0], [4, 0]])
        self.assertEqual(len(self.journals()), 1, "a failed step keeps the journal for the next recover")

    def test_the_journal_records_the_intent_before_each_mutation(self):
        session = self.start(mode="takeover", backend="vnc")
        record = Journal(Path(session.journal_path)).read()
        self.assertEqual(record["kind"], "omodachi.remote.session")
        self.assertEqual(record["mode"], "takeover")
        self.assertEqual(record["output_name"], session.output_name)
        self.assertEqual(record["baseline"][0]["name"], "eDP-1")
        self.assertEqual(sorted(record["takeover"]["moved"]), [1, 2, 4])


class HostQualityPreferenceTests(RemoteHarness):
    """`preferences.QUALITIES` is a host rate ceiling the planner has to honour."""

    def plan_with(self, quality, **payload):
        self.manager.host_quality = lambda: quality
        session = self.start(backend="sunshine", **payload)
        self.addCleanup(lambda: self.manager.release(session.id))
        return session.profile

    def test_the_performance_preference_caps_the_rates_the_client_asked_for(self):
        profile = self.plan_with({"fps": 30, "bitrate_kbps": 8000})
        # The client asked for 60 / 20000 (tests.remote_fakes.profile_request).
        self.assertEqual((profile.fps, profile.bitrate_kbps), (30, 8000))

    def test_the_pixel_budget_stays_the_clients(self):
        loose = self.plan_with({"fps": 60, "bitrate_kbps": 20000})
        self.manager.release(self.manager.session.id)
        strict = self.plan_with({"fps": 30, "bitrate_kbps": 8000})
        self.assertEqual(strict.stream_pixels.to_dict(), loose.stream_pixels.to_dict())
        self.assertEqual(strict.output_mode_pixels.to_dict(), loose.output_mode_pixels.to_dict())
        self.assertEqual(strict.logical_size.to_dict(), loose.logical_size.to_dict())

    def test_a_client_asking_for_less_than_the_host_allows_keeps_its_own_ceiling(self):
        self.manager.host_quality = lambda: {"fps": 60, "bitrate_kbps": 20000}
        session = self.start(backend="sunshine",
                             quality={"max_pixels": 4000000, "fps": 24, "bitrate_kbps": 4000})
        self.addCleanup(lambda: self.manager.release(session.id))
        self.assertEqual((session.profile.fps, session.profile.bitrate_kbps), (24, 4000))

    def test_a_resize_is_capped_too(self):
        self.manager.host_quality = lambda: {"fps": 30, "bitrate_kbps": 8000}
        session = self.start(backend="sunshine")
        session = self.manager.resize(session.id, {"expected_revision": session.revision,
                                                   "viewport_points": {"width": 834, "height": 1194},
                                                   "orientation": "portrait"})
        self.addCleanup(lambda: self.manager.release(session.id))
        self.assertEqual((session.profile.fps, session.profile.bitrate_kbps), (30, 8000))
        self.assertEqual(session.request["quality"]["fps"], 60, "the client's own request is kept verbatim")

    def test_a_preference_store_that_cannot_answer_does_not_fail_the_session(self):
        def broken():
            raise OSError("preferences_store_unavailable")
        self.manager.host_quality = broken
        session = self.start(backend="sunshine")
        self.addCleanup(lambda: self.manager.release(session.id))
        self.assertEqual((session.profile.fps, session.profile.bitrate_kbps), (60, 20000))


class StreamPresetTests(RemoteHarness):
    """STREAM-1: the device picks its own point on the host's table."""

    HEVC_DECODER = {"max_width": 4096, "max_height": 4096, "max_pixels": 16777216,
                    "max_fps": 60, "max_bitrate_kbps": 40000, "codecs": ["h264", "hevc"]}

    def setUp(self):
        super().setUp()
        # Leo's host: `performance` is the host's own default.
        self.manager.host_quality = lambda: {"fps": 30, "bitrate_kbps": 8000}

    def open(self, **payload):
        session = self.start(backend="sunshine", **payload)
        self.addCleanup(lambda: self.manager.release(session.id))
        return session

    def rates(self, session):
        return (session.profile.fps, session.profile.bitrate_kbps)

    def test_no_preset_is_the_host_preference_exactly_as_before(self):
        session = self.open()
        self.assertEqual(self.rates(session), (30, 8000))
        self.assertEqual(session.to_dict()["quality"], {"preset": "host", "adaptive": False})

    def test_each_named_preset_overrides_the_host_default(self):
        for preset, rates in (("performance", (30, 8000)), ("balanced", (60, 12000)), ("quality", (60, 20000))):
            with self.subTest(preset=preset):
                session = self.start(backend="sunshine", quality_preset=preset)
                self.assertEqual(self.rates(session), rates)
                self.assertEqual(session.to_dict()["quality"]["preset"], preset)
                self.manager.release(session.id)

    def test_a_named_preset_is_the_hosts_table_not_what_the_client_sent(self):
        session = self.open(quality_preset="balanced",
                            quality={"max_pixels": 4000000, "fps": 24, "bitrate_kbps": 3000})
        self.assertEqual(self.rates(session), (60, 12000))

    def test_custom_is_the_devices_own_numbers_inside_the_published_range(self):
        session = self.open(quality_preset="custom",
                            quality={"max_pixels": 4000000, "fps": 30, "bitrate_kbps": 30000})
        self.assertEqual(self.rates(session), (30, 30000))

    def test_custom_outside_the_range_is_refused_before_the_host_is_touched(self):
        for quality in ({"max_pixels": 4000000, "fps": 45, "bitrate_kbps": 20000},
                        {"max_pixels": 4000000, "fps": 60, "bitrate_kbps": 3999},
                        {"max_pixels": 4000000, "fps": 60, "bitrate_kbps": 40001}):
            with self.subTest(quality=quality):
                with self.assertRaises(RemoteError) as caught:
                    self.start(backend="sunshine", quality_preset="custom", quality=quality)
                self.assertEqual((caught.exception.code, caught.exception.status), ("invalid_request", 400))
                self.assertEqual(self.owned(), [])

    def test_an_unknown_preset_or_a_non_boolean_adaptive_is_a_bad_request(self):
        for payload in ({"quality_preset": "ultra"}, {"quality_preset": 3}, {"adaptive": "yes"}):
            with self.subTest(payload=payload):
                with self.assertRaises(RemoteError) as caught:
                    self.start(backend="sunshine", **payload)
                self.assertEqual(caught.exception.code, "invalid_request")

    def test_the_encoder_ceiling_still_wins_over_a_preset(self):
        self.manager.encoder = EncoderLimits(4096, 4096, 16_777_216, 30, 10_000, 2, 2)
        session = self.open(quality_preset="quality")
        self.assertEqual(self.rates(session), (30, 10000))

    def test_the_pixel_budget_is_the_same_for_every_preset(self):
        geometry = set()
        for preset in ("host", "performance", "balanced", "quality"):
            session = self.start(backend="sunshine", quality_preset=preset)
            geometry.add(json.dumps([session.profile.stream_pixels.to_dict(),
                                     session.profile.output_mode_pixels.to_dict(),
                                     session.profile.logical_size.to_dict()]))
            self.manager.release(session.id)
        self.assertEqual(len(geometry), 1)

    def test_switching_preset_mid_session_keeps_the_session_and_re_prepares_the_stream(self):
        session = self.open(quality_preset="quality", adaptive=True)
        session_id, output, revision = session.id, session.output_name, session.revision
        prepared = len(self.sunshine.prepared_profiles)
        session = self.manager.resize(session.id, {"expected_revision": revision, "quality_preset": "balanced"})
        self.assertEqual((session.id, session.output_name), (session_id, output))
        self.assertEqual(session.state, "ready")
        self.assertGreater(session.revision, revision)
        self.assertEqual(self.rates(session), (60, 12000))
        self.assertEqual(session.to_dict()["quality"], {"preset": "balanced", "adaptive": True})
        # The fork was stopped and handed the new rates: it admits a launch
        # only at exactly the prepared fps and at most the prepared bitrate.
        self.assertEqual(len(self.sunshine.prepared_profiles), prepared + 1)
        self.assertEqual(self.sunshine.prepared_profiles[-1]["bitrate_kbps"], 12000)
        self.assertIn("desktop.stop", self.sunshine.calls)
        self.assertEqual(len(self.owned()), 1, "a rate change is never a new output")

    def test_a_rotation_keeps_the_preset_it_does_not_name(self):
        session = self.open(quality_preset="quality")
        session = self.manager.resize(session.id, {"expected_revision": session.revision,
                                                   "viewport_points": {"width": 834, "height": 1194},
                                                   "orientation": "portrait"})
        self.assertEqual(self.rates(session), (60, 20000))
        self.assertEqual(session.to_dict()["quality"]["preset"], "quality")

    def test_going_back_to_host_puts_the_host_ceiling_back(self):
        session = self.open(quality_preset="quality")
        session = self.manager.resize(session.id, {"expected_revision": session.revision, "quality_preset": "host"})
        self.assertEqual(self.rates(session), (30, 8000))

    def test_a_rate_change_is_not_a_resize_so_a_host_that_forbids_resizing_allows_it(self):
        self.manager.allow_resize = lambda: False
        session = self.open(quality_preset="quality")
        session = self.manager.resize(session.id, {"expected_revision": session.revision,
                                                   "quality_preset": "performance"})
        self.assertEqual(self.rates(session), (30, 8000))

    def test_the_journal_remembers_the_preset(self):
        session = self.open(quality_preset="custom", adaptive=False,
                            quality={"max_pixels": 4000000, "fps": 60, "bitrate_kbps": 16000})
        record = Journal(Path(session.journal_path)).read()
        self.assertEqual((record["quality_preset"], record["adaptive"]), ("custom", False))

    def test_a_host_rebuild_keeps_the_devices_preset(self):
        session = self.open(quality_preset="quality")
        self.compositor.rows.remove(self.compositor.row(session.output_name))
        self.assertEqual(self.manager.reconcile(), "host_reconfigured")
        self.assertEqual(self.manager.session.id, session.id)
        self.assertEqual(self.rates(self.manager.session), (60, 20000))

    # --- codec -----------------------------------------------------------

    def test_todays_fork_is_h264_even_for_a_hevc_client(self):
        # The fork on Leo's host says `encoder: vaapi` and nothing about codecs,
        # and refuses a non-H.264 profile at prepare (FakeSunshine does too).
        session = self.open(decoder=self.HEVC_DECODER)
        self.assertEqual(session.profile.codec, "h264")
        self.assertEqual(self.manager.capabilities()["backends"]["sunshine"]["codecs"], ["h264"])

    def test_a_fork_that_serves_hevc_gets_hevc_from_a_client_that_decodes_it(self):
        self.sunshine.encoders = ["h264", "hevc"]
        session = self.open(decoder=self.HEVC_DECODER, quality_preset="performance")
        self.assertEqual(session.profile.codec, "hevc")
        self.assertEqual(self.sunshine.prepared_profiles[-1]["codec"], "hevc")
        self.assertEqual(self.rates(session), (30, 8000), "the codec changes the picture, not the rates")
        self.assertEqual(self.manager.capabilities()["backends"]["sunshine"]["codecs"], ["h264", "hevc"])

    def test_a_client_that_only_decodes_h264_stays_on_h264(self):
        self.sunshine.encoders = ["h264", "hevc"]
        self.assertEqual(self.open().profile.codec, "h264")

    def test_a_fork_that_names_codecs_without_h264_is_not_believed(self):
        self.sunshine.encoders = ["hevc"]
        session = self.open(decoder=self.HEVC_DECODER)
        self.assertEqual(session.profile.codec, "h264")

    def test_what_the_fork_says_it_encodes_is_read_strictly(self):
        served = SunshineBackend.served_codecs
        self.assertEqual(served({"encoder": "vaapi"}), ("h264",))
        self.assertEqual(served({"encoders": ["hevc", "h264"]}), ("h264", "hevc"))
        self.assertEqual(served({"encoders": ["h264", "vp9", "hevc", 7]}), ("h264", "hevc"))
        self.assertEqual(served({"encoders": "h264,hevc"}), ("h264",))
        self.assertEqual(served(None), ("h264",))

    def test_a_fork_that_stops_answering_forgets_what_it_encoded(self):
        self.sunshine.encoders = ["h264", "hevc"]
        self.manager.capabilities()
        self.sunshine.available = False
        self.manager.capabilities()
        self.sunshine.encoders = None
        self.assertEqual(self.manager.capabilities()["backends"]["sunshine"].get("codecs"), None)
        self.assertEqual(self.manager.backends["sunshine"].codecs, ("h264",))

    def test_the_vnc_leg_does_not_encode_so_it_says_h264(self):
        self.sunshine.encoders = ["h264", "hevc"]
        session = self.start(backend="vnc", decoder=self.HEVC_DECODER)
        self.addCleanup(lambda: self.manager.release(session.id))
        self.assertEqual(session.profile.codec, "h264")

    def test_switching_to_sunshine_mid_session_negotiates_the_codec(self):
        self.sunshine.encoders = ["h264", "hevc"]
        session = self.start(backend="vnc", decoder=self.HEVC_DECODER)
        self.addCleanup(lambda: self.manager.release(session.id))
        session = self.manager.switch_backend(session.id, {"expected_revision": session.revision,
                                                           "backend": "sunshine"})
        self.assertEqual(session.profile.codec, "hevc")


class HostReconfigurationTests(RemoteHarness):
    """The three ways `configreloaded` reaches a running session."""

    def owned_row(self, session):
        return self.compositor.row(session.output_name)

    def test_a_physical_only_change_updates_the_snapshot_and_leaves_the_backend_alone(self):
        session = self.start(backend="vnc")
        planned = json.dumps(self.owned_row(session), sort_keys=True)
        revision, running = session.revision, FakeWayVNC.instances[session.id].running
        # `hyprctl reload` re-applied the user's catch-all monitor rule.
        self.physical()[0].update(scale=1.3333334)
        self.assertEqual(self.manager.reconcile(), "baseline_updated")
        record = Journal(Path(session.journal_path)).read()
        self.assertEqual(record["baseline"][0]["scale"], 1.3333334)
        self.assertEqual(session.revision, revision, "nothing about the session changed")
        self.assertEqual(json.dumps(self.owned_row(session), sort_keys=True), planned)
        self.assertIs(FakeWayVNC.instances[session.id].running, running)
        # The restore puts back what the user last chose, not what was there before.
        self.manager.release(session.id)
        self.assertEqual(self.physical()[0]["scale"], 1.3333334)

    def test_a_scale_change_on_the_owned_output_is_put_back_not_adopted(self):
        """REMOTE-4. A mis-click in the Display panel costs the session nothing.

        `omarchy-hyprland-monitor-scaling` acts on whichever monitor is
        focused, and during a takeover that is ours. The session's output
        geometry was planned from the client's viewport and stays the
        session's: it is re-asserted, the backend is never stopped, the
        revision does not move, and the client sees no interruption at all.
        This is deliberately not PERF-2 §1.4's second row any more.
        """
        session = self.start(backend="sunshine")
        planned = json.dumps(self.owned_row(session), sort_keys=True)
        profile = session.profile.to_dict()
        revision, identity, calls = session.revision, session.id, list(self.sunshine.calls)
        self.owned_row(session).update(scale=1.3333334, x=0)
        self.events.clear()
        self.assertEqual(self.manager.reconcile(), "owned_output_repinned")
        self.assertEqual(json.dumps(self.owned_row(session), sort_keys=True), planned)
        self.assertEqual(session.profile.to_dict(), profile, "the profile is the client's, not the host's")
        self.assertEqual((session.revision, session.id), (revision, identity))
        self.assertEqual(self.sunshine.calls, calls, "the stream was never stopped")
        self.assertEqual(self.events, [])

    def test_the_catch_all_rule_cannot_bake_its_own_mode_into_the_stream(self):
        session = self.start(backend="sunshine")
        mode = (session.profile.output_mode_pixels.width, session.profile.output_mode_pixels.height)
        aspect = session.profile.stream_pixels.width / session.profile.stream_pixels.height
        # `hl.monitor({output="", mode="preferred", position="auto", scale=1.33333})`
        # applied to a headless output: 16:9 at the compositor's own default.
        self.owned_row(session).update(width=1920, height=1080, scale=1.3333334, x=0)
        self.assertEqual(self.manager.reconcile(), "owned_output_repinned")
        row = self.owned_row(session)
        self.assertEqual((row["width"], row["height"]), mode)
        self.assertAlmostEqual(session.profile.stream_pixels.width / session.profile.stream_pixels.height,
                               aspect, places=3)

    def test_a_drifted_mode_or_position_is_re_asserted_without_touching_the_backend(self):
        session = self.start(backend="vnc")
        revision = session.revision
        planned = json.dumps(self.owned_row(session), sort_keys=True)
        self.owned_row(session).update(width=1920, height=1080, x=0)
        self.assertEqual(self.manager.reconcile(), "owned_output_repinned")
        self.assertEqual(json.dumps(self.owned_row(session), sort_keys=True), planned)
        self.assertEqual(session.revision, revision)
        self.assertTrue(FakeWayVNC.instances[session.id].running)

    def test_an_owned_output_the_host_removed_is_rebuilt(self):
        session = self.start(backend="sunshine")
        planned = json.dumps(self.owned_row(session), sort_keys=True)
        self.compositor.rows.remove(self.owned_row(session))
        self.events.clear()
        self.assertEqual(self.manager.reconcile(), "host_reconfigured")
        self.assertIn(("ready", "host_reconfigured"), self.events)
        row = self.owned_row(session)
        self.assertIsNotNone(row)
        self.assertEqual((row["width"], row["height"], row["scale"], row["x"], row["y"]),
                         tuple(json.loads(planned)[key] for key in ("width", "height", "scale", "x", "y")))
        self.assertEqual(self.sunshine.calls[-1], "desktop.prepare")

    def test_a_quiet_host_reconciles_to_nothing(self):
        session = self.start(backend="vnc")
        self.assertIsNone(self.manager.reconcile())
        self.assertIsNone(self.manager.reconcile())
        self.manager.release(session.id)
        self.assertIsNone(self.manager.reconcile())

    def test_an_extend_session_puts_a_changed_physical_screen_back(self):
        # `hyprctl reload` during the session; nobody asked for this, and the
        # session never saw it, so the snapshot is still the user's own.
        session = self.start(backend="vnc")
        before = json.dumps(self.physical(), sort_keys=True)
        self.physical()[0].update(scale=1.3333334, x=512)
        self.assertEqual(self.manager.release(session.id), {"released": True, "errors": []})
        self.assertEqual(json.dumps(self.physical(), sort_keys=True), before)

    def test_a_screen_the_user_unplugged_is_not_a_failed_restore(self):
        session = self.start(backend="vnc")
        self.compositor.rows.remove(self.physical()[0])
        self.assertEqual(self.manager.release(session.id), {"released": True, "errors": []})
        self.assertEqual(self.journals(), [])

    def test_a_takeover_records_the_scale_a_disabled_screen_was_left_at(self):
        # The fallback path: a host with no DPMS dispatcher still disables the
        # output, and a disabled output reports geometry the snapshot must keep.
        self.compositor.dpms = False
        session = self.start(mode="takeover", backend="vnc")
        self.assertTrue(self.physical()[0]["disabled"])
        self.physical()[0].update(scale=1.3333334)
        self.assertEqual(self.manager.reconcile(), "baseline_updated")
        record = Journal(Path(session.journal_path)).read()
        self.assertEqual(record["baseline"][0]["scale"], 1.3333334)
        self.assertFalse(record["baseline"][0]["disabled"], "the snapshot still says it was on")
        self.manager.release(session.id)
        self.assertFalse(self.physical()[0]["disabled"])
        self.assertEqual(self.physical()[0]["scale"], 1.3333334)

    def test_an_unreadable_disabled_output_never_overwrites_a_good_snapshot(self):
        session = self.start(mode="takeover", backend="vnc")
        self.physical()[0].update(width=0, height=0, scale=0)
        self.assertIsNone(self.manager.reconcile())
        record = Journal(Path(session.journal_path)).read()
        self.assertEqual((record["baseline"][0]["width"], record["baseline"][0]["scale"]), (3072, 2.0))


class ConfigReloadTests(RemoteHarness):
    """REMOTE-4: a whole `hyprctl reload` while the session is up.

    The host issues one of these by itself on every output change, and the
    Display panel's SCALE button rewrites the file it re-reads. HOST-2 §4.2
    measured what that used to do: the session ended, inside a second, with
    `profile_readback_failed`.
    """

    def owned_row(self, session):
        return self.compositor.row(session.output_name)

    def reloaded(self, session, **payload):
        before = dict(session=session.id, revision=session.revision,
                      owned=json.dumps(self.owned_row(session), sort_keys=True))
        self.compositor.reload(**payload)
        return before

    def test_a_full_reload_leaves_the_session_exactly_where_it_was(self):
        session = self.start(mode="takeover", backend="sunshine")
        before = self.reloaded(session)
        calls = list(self.sunshine.calls)
        self.events.clear()
        self.assertEqual(self.manager.reconcile(), "owned_output_repinned")
        self.assertEqual(json.dumps(self.owned_row(session), sort_keys=True), before["owned"])
        self.assertEqual((session.id, session.revision, session.state),
                         (before["session"], before["revision"], "ready"))
        self.assertEqual(self.sunshine.calls, calls, "the stream was never stopped")
        self.assertEqual(self.events, [], "and the client was never told to do anything")

    def test_an_extend_session_survives_the_same_reload(self):
        session = self.start(backend="vnc")
        before = self.reloaded(session)
        self.assertEqual(self.manager.reconcile(), "owned_output_repinned")
        self.assertEqual(json.dumps(self.owned_row(session), sort_keys=True), before["owned"])
        self.assertEqual((session.id, session.revision, session.state),
                         (before["session"], before["revision"], "ready"))
        self.assertTrue(FakeWayVNC.instances[session.id].running)

    def test_the_reload_settles_and_the_next_pass_has_nothing_left_to_do(self):
        session = self.start(mode="takeover", backend="vnc")
        self.compositor.reload()
        self.assertEqual(self.manager.reconcile(), "owned_output_repinned")
        # The wake options and the aspect ratio the reload put back are dealt
        # with in the same pass; the pass after it is quiet.
        self.assertIsNone(self.manager.reconcile())

    def test_the_scale_the_user_chose_for_their_own_screen_is_what_they_get_back(self):
        """Leo's own case: the panel writes 1.6 into `monitors.lua` and reloads.

        The session does not fight the physical screen - it adopts the new
        value into its snapshot - so when the session ends the user's screen is
        at the scale they picked, not the one they had before they picked it.
        """
        session = self.start(mode="takeover", backend="vnc")
        self.assertEqual(self.physical()[0]["scale"], 2.0)
        self.compositor.reload(scale=1.6)
        # One pass does both: the snapshot takes the user's new number and the
        # session's own output goes back to the session's.
        self.assertEqual(self.manager.reconcile(), "owned_output_repinned")
        record = Journal(Path(session.journal_path)).read()
        self.assertEqual(record["baseline"][0]["scale"], 1.6)
        self.assertEqual(self.manager.release(session.id), {"released": True, "errors": []})
        self.assertEqual(self.physical()[0]["scale"], 1.6)

    def test_a_write_the_reload_swallows_is_issued_again(self):
        """A write into the middle of a reload is accepted and then lost."""
        session = self.start(backend="vnc")
        before = json.dumps(self.owned_row(session), sort_keys=True)
        self.compositor.reload()
        self.compositor.swallow_owned_writes = 2
        self.assertEqual(self.manager.reconcile(), "owned_output_repinned")
        self.assertEqual(json.dumps(self.owned_row(session), sort_keys=True), before)
        self.assertEqual(session.state, "ready")

    def test_a_reload_that_never_stops_is_retried_and_only_then_given_up_on(self):
        session = self.start(backend="vnc")
        revision = session.revision
        self.compositor.swallow_owned_writes = 10 ** 6
        self.compositor.reload()
        for _ in range(RECONFIGURE_ATTEMPTS - 1):
            self.assertEqual(self.manager.reconcile(), "host_reconfigure_retry")
            self.assertEqual((session.state, session.revision), ("ready", revision))
        self.assertEqual(self.manager.reconcile(), "profile_readback_failed")
        self.assertEqual(session.state, "failed")
        self.assertEqual(self.journals(), [], "and it let go of the host on the way out")

    def test_a_pass_that_lands_forgets_the_ones_that_did_not(self):
        session = self.start(backend="vnc")
        self.compositor.swallow_owned_writes = 10 ** 6
        self.compositor.reload()
        for _ in range(RECONFIGURE_ATTEMPTS - 1):
            self.assertEqual(self.manager.reconcile(), "host_reconfigure_retry")
        self.compositor.swallow_owned_writes = 0
        self.assertEqual(self.manager.reconcile(), "owned_output_repinned")
        self.compositor.swallow_owned_writes = 10 ** 6
        self.compositor.reload()
        self.assertEqual(self.manager.reconcile(), "host_reconfigure_retry")
        self.assertEqual(session.state, "ready")

    def test_an_output_that_briefly_goes_away_is_waited_for_not_declared_dead(self):
        """A reload can rebuild the output; `monitoradded` brings us back here."""
        session = self.start(backend="vnc")
        row = self.owned_row(session)
        self.compositor.rows.remove(row)
        self.events.clear()
        self.assertEqual(self.manager.reconcile(), "host_reconfigured")
        self.assertEqual([reason for _state, reason in self.events], ["host_reconfigured", "host_reconfigured"])
        self.assertIsNotNone(self.owned_row(session))
        self.assertTrue(FakeWayVNC.instances[session.id].running)

    def test_a_rebuild_that_did_not_land_leaves_a_ready_session_not_a_resizing_one(self):
        """The client is never left holding a `resizing` it will never be told ended."""
        session = self.start(backend="vnc")
        self.compositor(("hyprctl", "--instance", INSTANCE, "output", "remove", session.output_name))
        self.compositor.fail = "output"  # the re-create will not answer
        self.events.clear()
        self.assertEqual(self.manager.reconcile(), "host_reconfigure_retry")
        self.assertEqual(session.state, "ready")
        self.assertEqual([state for state, _reason in self.events][-1], "ready")
        self.compositor.fail = None
        self.assertEqual(self.manager.reconcile(), "host_reconfigured")
        self.assertEqual(session.state, "ready")

    def test_the_session_id_never_changes_whatever_the_host_did(self):
        """The one promise the client's reconnect is built on."""
        session = self.start(mode="takeover", backend="sunshine")
        identity = session.id
        self.compositor.reload()
        self.manager.reconcile()
        self.compositor(("hyprctl", "--instance", INSTANCE, "output", "remove", session.output_name))
        self.manager.reconcile()
        self.compositor.reload()
        self.manager.reconcile()
        self.assertEqual(self.manager.current().id, identity)
        self.assertEqual(self.manager.current().state, "ready")

    def test_a_host_that_only_changed_its_text_size_is_a_quiet_pass(self):
        """Changing the font touches no output, so there is nothing to answer."""
        session = self.start(mode="takeover", backend="vnc")
        self.assertIsNone(self.manager.reconcile())
        self.assertEqual(session.revision, 2)


class DefaultBackendAndOwnerTests(RemoteHarness):
    """SPEC-I §1.2 and §1.3: who holds the host, and what a client gets by default."""

    def test_the_host_default_backend_is_sunshine_and_is_said_out_loud(self):
        capabilities = self.manager.capabilities()
        self.assertEqual(capabilities["default_backend"], "sunshine")
        session = self.manager.create("ipad-a", profile_request())
        self.assertEqual(session.backend, "sunshine")
        self.manager.release(session.id)

    def test_an_unavailable_preferred_backend_falls_through_to_one_that_is(self):
        self.sunshine.available = False
        self.assertFalse(self.manager.capabilities()["backends"]["sunshine"]["available"])
        self.assertEqual(self.manager.capabilities()["default_backend"], "vnc")
        session = self.manager.create("ipad-a", profile_request())
        self.assertEqual(session.backend, "vnc")
        self.manager.release(session.id)

    def test_the_host_preference_wins_while_it_is_usable(self):
        self.manager.host_backend = lambda: "vnc"
        self.assertEqual(self.manager.capabilities()["default_backend"], "vnc")
        self.manager.host_backend = lambda: "moonlight"  # not a backend this host has
        self.assertEqual(self.manager.capabilities()["default_backend"], "sunshine")
        # An explicit client choice is still a client choice.
        session = self.manager.create("ipad-a", dict(profile_request(), backend="vnc"))
        self.assertEqual(session.backend, "vnc")
        self.manager.release(session.id)

    def test_the_second_device_is_told_who_is_holding_the_host(self):
        self.manager.device_names = {"ipad-a": "Leo's iPad"}.get
        session = self.manager.create("ipad-a", dict(profile_request(), backend="vnc", mode="takeover"))
        with self.assertRaises(RemoteError) as raised:
            self.manager.create("iphone-b", profile_request())
        self.assertEqual(raised.exception.code, "remote_session_exists")
        detail = raised.exception.detail
        self.assertEqual(detail["session_id"], session.id)
        self.assertEqual(detail["owner_device_id"], "ipad-a")
        self.assertEqual(detail["owner_device_name"], "Leo's iPad")
        self.assertEqual((detail["mode"], detail["backend"]), ("takeover", "vnc"))
        self.assertIsInstance(detail["started_at"], int)
        self.manager.release(session.id)

    def test_a_directory_that_cannot_answer_never_breaks_the_refusal(self):
        def broken(device_id):
            raise RuntimeError("registry unreadable")
        self.manager.device_names = broken
        session = self.manager.create("ipad-a", dict(profile_request(), backend="vnc"))
        with self.assertRaises(RemoteError) as raised:
            self.manager.create("iphone-b", profile_request())
        self.assertEqual(raised.exception.detail["owner_device_name"], "ipad-a")
        self.manager.release(session.id)


if __name__ == "__main__":
    unittest.main()



class BlankedScreenTests(RemoteHarness):
    """HOST-1: a takeover turns the screens off, it does not take them away.

    Removing an output kills Quickshell 0.3.1 every time - Qt 6.11
    dereferences a null `QPlatformScreen` in `QWaylandWindow::setGeometry` -
    and no ordering on this side avoids it. The compositor double models that:
    disabling a physical output is recorded as a crash of the shell.
    """

    def takeover(self, **payload):
        return self.start(mode="takeover", backend="vnc", **payload)

    def journal(self, session):
        return json.loads((self.root / "remote" / (session.output_name + ".json")).read_text())

    def test_the_screen_goes_dark_without_leaving_the_layout(self):
        before = json.dumps(self.physical(), sort_keys=True)
        session = self.takeover()
        self.assertEqual([row["dpmsStatus"] for row in self.physical()], [False])
        self.assertEqual([row["disabled"] for row in self.physical()], [False])
        self.assertEqual(self.compositor.shell_crashes, [])
        self.manager.release(session.id)
        self.assertEqual(json.dumps(self.physical(), sort_keys=True), before)

    def test_the_journal_names_each_screen_and_how_it_was_darkened(self):
        session = self.takeover()
        record = self.journal(session)
        self.assertEqual(record["takeover"]["blanked"], [{"name": "eDP-1", "method": "dpms"}])
        self.assertEqual(record["takeover"]["disabled"], [])

    def test_a_compositor_without_the_dispatcher_falls_back_to_the_old_way(self):
        self.compositor.dpms = False
        session = self.takeover()
        record = self.journal(session)
        self.assertEqual(record["takeover"]["blanked"], [{"name": "eDP-1", "method": "disabled"}])
        self.assertEqual(record["takeover"]["disabled"], ["eDP-1"])
        self.assertEqual([row["disabled"] for row in self.physical()], [True])
        # …and that is the path that kills the shell, which is what the
        # crash fallback exists for.
        self.assertEqual(self.compositor.shell_crashes, ["eDP-1"])
        self.manager.release(session.id)
        self.assertEqual([row["disabled"] for row in self.physical()], [False])

    def test_a_screen_the_user_had_already_turned_off_is_not_this_sessions(self):
        self.compositor.row("eDP-1")["dpmsStatus"] = False
        session = self.takeover()
        self.assertEqual(self.journal(session)["takeover"]["blanked"], [])
        self.manager.release(session.id)
        self.assertIs(self.compositor.row("eDP-1")["dpmsStatus"], False)

    def test_a_screen_that_came_back_on_during_the_session_is_turned_off_again(self):
        self.takeover()
        self.compositor.row("eDP-1")["dpmsStatus"] = True
        self.assertEqual(self.manager.reconcile(), "physical_blanking_reapplied")
        self.assertIs(self.compositor.row("eDP-1")["dpmsStatus"], False)

    def test_an_extend_session_never_darkens_anything(self):
        self.start(backend="vnc")
        self.assertEqual([row["dpmsStatus"] for row in self.physical()], [True])
        self.assertIsNone(self.manager.reconcile())

    def test_a_leftover_journal_turns_the_screen_back_on(self):
        session = self.takeover()
        record = self.journal(session)
        self.manager.session, self.manager._record = None, None
        self.assertIs(self.compositor.row("eDP-1")["dpmsStatus"], False)
        self.assertEqual(self.manager.recover()["recovered"][0]["errors"], [])
        self.assertIs(self.compositor.row("eDP-1")["dpmsStatus"], True)
        self.assertEqual(record["takeover"]["blanked"][0]["method"], "dpms")

    def test_a_journal_from_before_this_change_is_still_restored(self):
        session = self.takeover()
        path = self.root / "remote" / (session.output_name + ".json")
        record = json.loads(path.read_text())
        # What a pre-HOST-1 daemon wrote: a name list and no method.
        record["takeover"].pop("blanked")
        record["takeover"]["disabled"] = ["eDP-1"]
        self.compositor.row("eDP-1").update(disabled=True, dpmsStatus=True)
        path.write_text(json.dumps(record))
        self.manager.session, self.manager._record = None, None
        self.assertEqual(self.manager.recover()["recovered"][0]["errors"], [])
        self.assertIs(self.compositor.row("eDP-1")["disabled"], False)

    def test_a_screen_that_will_not_answer_is_reported_and_the_rest_still_runs(self):
        session = self.takeover()
        self.compositor.dpms = False
        errors = self.manager.restore(self.journal(session), "released")
        self.assertEqual([row["step"] for row in errors], ["dpms"])
        self.assertEqual([row.name for row in self.manager.hyprland.monitors()], ["eDP-1"])


class WakeOnInputTests(RemoteHarness):
    """HOST-2: the screen in the room lit up on every tap during a takeover.

    Hyprland wakes every DPMS-off monitor for any input event while
    `misc:mouse_move_enables_dpms` / `misc:key_press_enables_dpms` are on, and
    Omarchy's own input config sets both. A takeover forwards the client's
    pointer and keys to that same compositor, so the panel flashed once per
    interaction - pointer, touchpad or keyboard - until the next reconcile
    darkened it again.
    """

    OPTIONS = ("misc:mouse_move_enables_dpms", "misc:key_press_enables_dpms")

    def takeover(self, **payload):
        return self.start(mode="takeover", backend="vnc", **payload)

    def journal(self, session):
        return json.loads((self.root / "remote" / (session.output_name + ".json")).read_text())

    def test_a_takeover_turns_both_wake_options_off_and_puts_them_back(self):
        session = self.takeover()
        self.assertEqual([self.compositor.wake_options[name] for name in self.OPTIONS], [False, False])
        self.assertEqual(self.journal(session)["takeover"]["dpms_wake"],
                         {"state": "owned", "original": {name: [True, True] for name in self.OPTIONS}})
        self.manager.release(session.id)
        self.assertEqual([self.compositor.wake_options[name] for name in self.OPTIONS], [True, True])

    def test_forwarded_input_no_longer_lights_the_screen(self):
        session = self.takeover()
        self.assertIs(self.compositor.row("eDP-1")["dpmsStatus"], False)
        for _ in range(20):
            self.assertEqual(self.compositor.wake_input("mouse"), [])
            self.assertEqual(self.compositor.wake_input("key"), [])
            self.assertIs(self.compositor.row("eDP-1")["dpmsStatus"], False)
        # …and nothing had to be put right afterwards.
        self.assertIsNone(self.manager.reconcile())
        self.manager.release(session.id)

    def test_without_the_change_the_same_input_would_light_it(self):
        """The double's wake behaviour is real, not a way of passing."""
        session = self.takeover()
        self.compositor.wake_options["misc:mouse_move_enables_dpms"] = True
        self.assertEqual(self.compositor.wake_input("mouse"), ["eDP-1"])
        self.assertIs(self.compositor.row("eDP-1")["dpmsStatus"], True)
        # Which is exactly the flash Leo saw: reconcile turns it off again.
        self.assertEqual(self.manager.reconcile(), "physical_blanking_reapplied")
        self.assertIs(self.compositor.row("eDP-1")["dpmsStatus"], False)
        self.manager.release(session.id)

    def test_the_options_are_off_before_the_screen_goes_dark(self):
        """Otherwise one forwarded event in the gap relights it."""
        self.takeover()
        wake = [index for index, (kind, tail) in enumerate(self.timeline)
                if kind == "hyprctl" and "enables_dpms = false" in str(tail)]
        dark = [index for index, (kind, tail) in enumerate(self.timeline)
                if kind == "hyprctl" and "hl.dsp.dpms" in str(tail)]
        self.assertEqual(len(wake), 2)
        self.assertTrue(dark and max(wake) < min(dark))

    def test_an_extend_session_leaves_the_wake_behaviour_alone(self):
        session = self.start(backend="vnc")
        self.assertEqual([self.compositor.wake_options[name] for name in self.OPTIONS], [True, True])
        self.assertIsNone(self.journal(session)["takeover"])
        self.manager.release(session.id)
        self.assertEqual([self.compositor.wake_options[name] for name in self.OPTIONS], [True, True])

    def test_a_host_that_already_had_them_off_is_left_alone_on_both_sides(self):
        for name in self.OPTIONS:
            self.compositor.wake_options[name] = False
            self.compositor.wake_options_set[name] = False
        session = self.takeover()
        self.assertEqual(self.journal(session)["takeover"]["dpms_wake"]["state"], "already_off")
        self.manager.release(session.id)
        self.assertEqual([self.compositor.wake_options_set[name] for name in self.OPTIONS], [False, False])

    def test_only_the_option_that_was_on_is_put_back_on(self):
        self.compositor.wake_options["misc:key_press_enables_dpms"] = False
        session = self.takeover()
        record = self.journal(session)["takeover"]["dpms_wake"]
        self.assertEqual(record["state"], "owned")
        self.assertEqual(record["original"]["misc:key_press_enables_dpms"], [False, True])
        self.manager.release(session.id)
        self.assertEqual([self.compositor.wake_options[name] for name in self.OPTIONS], [True, False])

    def test_a_config_reload_that_turns_them_back_on_is_re_applied(self):
        session = self.takeover()
        # `hyprctl reload` re-runs Omarchy's own input.lua mid-session.
        for name in self.OPTIONS:
            self.compositor.wake_options[name] = True
        self.assertEqual(self.manager.reconcile(), "dpms_wake_reapplied")
        self.assertEqual([self.compositor.wake_options[name] for name in self.OPTIONS], [False, False])
        self.manager.release(session.id)
        self.assertEqual([self.compositor.wake_options[name] for name in self.OPTIONS], [True, True])

    def test_the_reload_pass_closes_the_door_before_darkening_the_screen(self):
        session = self.takeover()
        for name in self.OPTIONS:
            self.compositor.wake_options[name] = True
        self.compositor.row("eDP-1")["dpmsStatus"] = True
        self.timeline.clear()
        # Blanking is what gets reported; the wake options are repaired first.
        self.assertEqual(self.manager.reconcile(), "physical_blanking_reapplied")
        order = [index for index, (kind, tail) in enumerate(self.timeline) if kind == "hyprctl"
                 and ("enables_dpms = false" in str(tail) or "hl.dsp.dpms" in str(tail))]
        first = self.timeline[order[0]][1]
        self.assertIn("enables_dpms = false", str(first))
        self.assertIs(self.compositor.row("eDP-1")["dpmsStatus"], False)
        self.manager.release(session.id)

    def test_a_leftover_takeover_journal_puts_them_back_too(self):
        self.takeover()
        self.manager.session, self.manager._record = None, None
        self.assertEqual(self.manager.recover()["recovered"][0]["errors"], [])
        self.assertEqual([self.compositor.wake_options[name] for name in self.OPTIONS], [True, True])
        self.assertEqual(self.journals(), [])

    def test_an_option_somebody_turned_back_on_during_the_session_is_not_written(self):
        session = self.takeover()
        self.compositor.wake_options["misc:mouse_move_enables_dpms"] = True
        self.compositor.calls.clear()
        self.assertEqual(self.manager.release(session.id), {"released": True, "errors": []})
        self.assertNotIn("hl.config({ misc = { mouse_move_enables_dpms = true } })",
                         [tail[1] for tail in self.compositor.calls if tail[0] == "eval"])
        self.assertEqual([self.compositor.wake_options[name] for name in self.OPTIONS], [True, True])

    def test_a_compositor_that_will_not_answer_costs_the_session_nothing(self):
        self.compositor.fail = "getoption"
        session = self.takeover()
        record = self.journal(session)["takeover"]["dpms_wake"]
        # `RemoteError` is a `ValueError`, so an unreadable option and an
        # unanswerable one arrive here under the same code the ratio uses.
        self.assertEqual(record, {"state": "unavailable", "code": "config_schema_invalid"})
        self.compositor.fail = None
        # Nothing was written, so there is nothing to put back and no error.
        self.assertEqual(self.manager.release(session.id), {"released": True, "errors": []})
        self.assertEqual([self.compositor.wake_options[name] for name in self.OPTIONS], [True, True])

    def test_a_refusal_to_write_is_reported_by_the_restore_not_by_create(self):
        session = self.takeover()
        self.assertEqual([self.compositor.wake_options[name] for name in self.OPTIONS], [False, False])
        self.compositor.fail = "hl.config"  # reads still work, writes do not
        errors = self.manager.restore(self.journal(session), "released")
        self.assertEqual([row["step"] for row in errors], ["dpms_wake", "single_window_aspect"])
        self.assertEqual(errors[0]["code"], "display_command_failed")

    def test_a_journal_from_before_this_change_restores_everything_else(self):
        session = self.takeover()
        path = self.root / "remote" / (session.output_name + ".json")
        record = json.loads(path.read_text())
        record["takeover"].pop("dpms_wake")  # what a pre-HOST-2 daemon wrote
        path.write_text(json.dumps(record))
        self.manager.session, self.manager._record = None, None
        self.assertEqual(self.manager.recover()["recovered"][0]["errors"], [])
        self.assertIs(self.compositor.row("eDP-1")["dpmsStatus"], True)


class ShellRepairTests(RemoteHarness):
    """HOST-1 §4: the shell crashed, and the user is looking at an iPad."""

    def takeover(self, **payload):
        return self.start(mode="takeover", backend="vnc", **payload)

    def test_a_crash_under_a_live_session_restarts_the_shell_once(self):
        session = self.takeover()
        self.shell.crash("c61r66olt")
        result = self.manager.watch_shell()
        self.assertEqual(result["crashes"], ["c61r66olt"])
        self.assertTrue(result["restarted"])
        self.assertEqual(self.shell.restarts, 1)
        self.assertIn(("ready", "shell_restarted"), self.events)
        self.assertEqual(session.revision, 2)
        record = json.loads((self.root / "remote" / (session.output_name + ".json")).read_text())
        self.assertEqual(record["shell_restarted"]["crashes"], ["c61r66olt"])

    def test_a_shell_that_keeps_crashing_is_not_restarted_again(self):
        self.takeover()
        self.shell.crash("one")
        self.manager.watch_shell()
        self.shell.crash("two")
        self.assertIsNone(self.manager.watch_shell())
        self.assertEqual(self.shell.restarts, 1)

    def test_a_quiet_shell_is_left_alone(self):
        self.takeover()
        self.assertIsNone(self.manager.watch_shell())
        self.assertEqual(self.shell.restarts, 0)

    def test_crashes_from_before_the_session_are_not_this_sessions_doing(self):
        self.shell.crash("yesterday")
        self.takeover()
        self.assertIsNone(self.manager.watch_shell())
        self.assertEqual(self.shell.restarts, 0)

    def test_with_no_session_nothing_is_restarted(self):
        self.shell.crash("c61r66olt")
        self.assertIsNone(self.manager.watch_shell())
        self.assertEqual(self.shell.restarts, 0)

    def test_a_refused_restart_is_recorded_as_one(self):
        self.shell.succeeds = False
        session = self.takeover()
        self.shell.crash("c61r66olt")
        result = self.manager.watch_shell()
        self.assertFalse(result["restarted"])
        record = json.loads((self.root / "remote" / (session.output_name + ".json")).read_text())
        self.assertIs(record["shell_restarted"]["restarted"], False)

    def test_an_extend_session_watches_too(self):
        self.start(backend="vnc")
        self.shell.crash("c61r66olt")
        self.assertIsNotNone(self.manager.watch_shell())
        self.assertEqual(self.shell.restarts, 1)


    def test_the_dispatcher_only_toggles_so_the_state_is_read_first(self):
        """Hyprland 0.56 ignores the on/off argument and turns the screen round.

        Asking for the state a screen is already in must therefore do nothing
        at all, or a restore on a screen somebody already turned back on would
        turn it off again — which is what it did before the readback went in.
        """
        session = self.takeover()
        self.assertIs(self.compositor.row("eDP-1")["dpmsStatus"], False)
        self.manager.hyprland.set_output_dpms("eDP-1", False)
        self.assertIs(self.compositor.row("eDP-1")["dpmsStatus"], False)
        self.compositor.row("eDP-1")["dpmsStatus"] = True
        self.assertEqual(self.manager.release(session.id), {"released": True, "errors": []})
        self.assertIs(self.compositor.row("eDP-1")["dpmsStatus"], True)

    def test_a_workspace_the_hosts_rules_pull_back_is_moved_again(self):
        session = self.takeover()
        owned = self.owned()[0]["id"]
        # `hyprctl reload` re-applies the user's workspace rules, which pin
        # every workspace to the panel that is now merely dark.
        for row in self.compositor.workspaces:
            if row[0] in {2, 4}:
                row[1] = 0
        self.assertEqual(self.manager.reconcile(), "workspaces_repinned")
        self.assertEqual(sorted(row[0] for row in self.compositor.workspaces if row[1] == owned and row[2]), [2, 4])
        self.manager.release(session.id)

    def test_a_workspace_this_session_never_moved_is_left_where_it_is(self):
        session = self.takeover()
        self.compositor.workspaces.append([77, 0, 1])
        self.assertIsNone(self.manager.reconcile())
        self.assertEqual([row for row in self.compositor.workspaces if row[0] == 77], [[77, 0, 1]])
        self.manager.release(session.id)


class IdleRetryTests(unittest.TestCase):
    """A shell that was just restarted is not ready for a second or two."""

    def test_the_second_try_is_the_one_that_lands(self):
        answers = ["", "", "enabled"]
        idle = OmarchyIdle(runner=lambda method: answers.pop(0), sleep=lambda _seconds: None)
        idle.set(True)
        self.assertEqual(answers, [])

    def test_a_shell_that_never_answers_is_still_an_error(self):
        idle = OmarchyIdle(runner=lambda method: (_ for _ in ()).throw(ValueError("omarchy_idle_unavailable")),
                           sleep=lambda _seconds: None)
        with self.assertRaises(ValueError):
            idle.set(True)

    def test_an_answer_first_time_costs_nothing(self):
        calls = []
        idle = OmarchyIdle(runner=lambda method: calls.append(method) or "disabled",
                           sleep=lambda _seconds: self.fail("waited for nothing"))
        idle.set(False)
        self.assertEqual(calls, ["disable"])
