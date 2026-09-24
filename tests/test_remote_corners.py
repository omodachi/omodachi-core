"""REMOTE-SAFE-1: the device's corners, in the owned output's logical px.

The App reports how far its display corners reach into each end of the host
bar, in its own points; core converts that into the owned output's logical
pixels and publishes it on `remote_bar.bar_insets` for the Omarchy plugin.
"""
from __future__ import annotations

import json
import math
import unittest

from omodachi_core.protocol import IDLE_REMOTE_BAR
from omodachi_core.remote import RemoteError
from omodachi_core.remote.corners import EDGES, parse_occlusion, to_output_pixels
from tests.remote_fakes import profile_request
from tests.test_remote_session import RemoteHarness

# What the App sends for an iPhone 17 Pro (R = 62 pt) with Omarchy's 26 px bar
# at 874 pt / 1280 px: DisplayCorners.barOcclusion, same numbers as the iOS
# unit test `DisplayCornersTests.testIPhone17ProPortrait`.
IPHONE_17_PRO = {"top": 46.4, "bottom": 46.4, "left": 46.4, "right": 46.4}


class ParseTests(unittest.TestCase):
    def test_absent_is_not_an_error(self):
        self.assertIsNone(parse_occlusion(None))

    def test_all_four_edges_as_floats(self):
        self.assertEqual(parse_occlusion({"top": 1, "bottom": 2.5, "left": 0, "right": 256}),
                         {"top": 1.0, "bottom": 2.5, "left": 0.0, "right": 256.0})

    def test_anything_else_is_a_400(self):
        for value in ({}, {"top": 1, "bottom": 1, "left": 1}, dict(IPHONE_17_PRO, extra=1),
                      dict(IPHONE_17_PRO, top=-1), dict(IPHONE_17_PRO, top=257), dict(IPHONE_17_PRO, top=True),
                      dict(IPHONE_17_PRO, top="4"), dict(IPHONE_17_PRO, top=math.nan),
                      dict(IPHONE_17_PRO, top=math.inf), [1, 2, 3, 4], "top"):
            with self.subTest(value=value), self.assertRaises(RemoteError) as caught:
                parse_occlusion(value)
            self.assertEqual((caught.exception.code, caught.exception.status), ("invalid_request", 400))


class PixelTests(unittest.TestCase):
    def test_points_to_logical_px_rounds_up(self):
        # 874 pt tall picture of a 1280 px output: 1 pt = 1280/874 px.
        insets = to_output_pixels(IPHONE_17_PRO, {"width": 402, "height": 874}, {"width": 589, "height": 1280})
        self.assertEqual(insets, {edge: math.ceil(46.4 * 1280 / 874) for edge in EDGES})
        self.assertEqual(insets["bottom"], 68)

    def test_a_letterbox_is_already_that_far_from_the_corner(self):
        # A 1 % aspect mismatch: the picture is 20 pt short of each long end.
        insets = to_output_pixels({"top": 30, "bottom": 30, "left": 30, "right": 30},
                                  {"width": 400, "height": 840}, {"width": 400, "height": 800})
        self.assertEqual((insets["top"], insets["bottom"]), (10, 10))
        self.assertEqual((insets["left"], insets["right"]), (30, 30))

    def test_a_corner_bigger_than_the_output_cannot_eat_the_bar(self):
        insets = to_output_pixels({"top": 256, "bottom": 256, "left": 256, "right": 256},
                                  {"width": 100, "height": 200}, {"width": 100, "height": 200})
        self.assertEqual(insets, {"top": 50, "bottom": 50, "left": 25, "right": 25})

    def test_nothing_honest_to_say_is_none(self):
        self.assertIsNone(to_output_pixels(None, {"width": 1, "height": 1}, {"width": 1, "height": 1}))
        self.assertIsNone(to_output_pixels(IPHONE_17_PRO, {"width": 0, "height": 874}, {"width": 589, "height": 1280}))
        self.assertIsNone(to_output_pixels(IPHONE_17_PRO, None, {"width": 589, "height": 1280}))
        self.assertIsNone(to_output_pixels(IPHONE_17_PRO, {"width": 402, "height": 874}, {"width": "x"}))

    def test_zeros_stay_zeros(self):
        zero = {edge: 0 for edge in EDGES}
        self.assertEqual(to_output_pixels(zero, {"width": 402, "height": 874}, {"width": 589, "height": 1280}), zero)


class CliTests(unittest.TestCase):
    def test_the_operator_flag_is_the_wire_shape(self):
        import argparse
        from omodachi_core.cli import bar_occlusion
        self.assertEqual(bar_occlusion("46.4,46.4,0,12"), {"top": 46.4, "bottom": 46.4, "left": 0.0, "right": 12.0})
        for text in ("1,2,3", "a,b,c,d", ""):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                bar_occlusion(text)


class SessionTests(RemoteHarness):
    def phone(self, **payload):
        return self.start(backend="vnc", **dict(profile_request(402, 874), **payload))

    def test_capabilities_say_the_field_is_taken(self):
        self.assertIs(self.manager.capabilities()["bar_occlusion"], True)

    def test_an_extend_session_publishes_its_corners_in_output_pixels(self):
        session = self.phone(bar_occlusion_points=IPHONE_17_PRO)
        self.assertEqual(session.bar_occlusion, IPHONE_17_PRO)
        bar = self.manager.bar_projection()
        logical = bar["logical_size"]
        self.assertEqual(bar["output_name"], session.output_name)
        expected = to_output_pixels(IPHONE_17_PRO, {"width": 402, "height": 874}, logical)
        self.assertEqual(bar["bar_insets"], expected)
        self.assertTrue(60 <= bar["bar_insets"]["bottom"] <= 75, bar["bar_insets"])
        # It is journaled with the session, next to the request it came with.
        record = json.loads(self.journals()[0].read_text())
        self.assertEqual(record["bar_occlusion"], IPHONE_17_PRO)

    def test_a_client_that_said_nothing_gets_null(self):
        self.phone()
        self.assertIsNone(self.manager.bar_projection()["bar_insets"])

    def test_a_takeover_publishes_them_too(self):
        # REMOTE-SAFE-1b. A takeover moves the whole desktop onto the same
        # device-shaped output, so the bar there reaches the same corners.
        session = self.start(backend="vnc", mode="takeover", **dict(profile_request(402, 874),
                                                                    bar_occlusion_points=IPHONE_17_PRO))
        self.assertEqual(session.mode, "takeover")
        bar = self.manager.bar_projection()
        self.assertTrue(bar["active"])
        self.assertEqual(bar["output_name"], session.output_name)
        self.assertEqual(bar["bar_insets"], to_output_pixels(IPHONE_17_PRO, {"width": 402, "height": 874},
                                                             bar["logical_size"]))
        self.assertTrue(60 <= bar["bar_insets"]["top"] <= 75, bar["bar_insets"])

    def test_a_takeover_rotation_replaces_them_and_the_end_clears_them(self):
        # Leo's iPhone 15 Pro: 393x852 pt. A rotation is a resize that brings
        # the landscape corners; the plugin picks the axis from the bar's
        # position, which official_bar_position flips at the same time.
        portrait = {"top": 40.0, "bottom": 40.0, "left": 40.0, "right": 40.0}
        landscape = {"top": 8.0, "bottom": 8.0, "left": 40.0, "right": 40.0}
        session = self.start(backend="vnc", mode="takeover", **dict(profile_request(393, 852),
                                                                    bar_occlusion_points=portrait))
        before = self.manager.bar_projection()
        self.assertEqual(before["orientation"], "portrait")
        self.assertEqual(before["bar_insets"], to_output_pixels(portrait, {"width": 393, "height": 852},
                                                                before["logical_size"]))
        session = self.manager.resize(session.id, dict(profile_request(852, 393), expected_revision=session.revision,
                                                       bar_occlusion_points=landscape))
        after = self.manager.bar_projection()
        self.assertEqual(after["orientation"], "landscape")
        self.assertEqual(after["bar_insets"], to_output_pixels(landscape, {"width": 852, "height": 393},
                                                               after["logical_size"]))
        self.assertGreater(after["bar_insets"]["left"], after["bar_insets"]["top"])
        self.manager.release(session.id)
        self.assertEqual(self.manager.bar_projection(), IDLE_REMOTE_BAR)

    def test_a_takeover_client_that_said_nothing_gets_null(self):
        self.start(backend="vnc", mode="takeover", **profile_request(402, 874))
        self.assertIsNone(self.manager.bar_projection()["bar_insets"])

    def test_the_session_ending_takes_them_away(self):
        session = self.phone(bar_occlusion_points=IPHONE_17_PRO)
        self.assertIsNotNone(self.manager.bar_projection()["bar_insets"])
        self.manager.release(session.id)
        self.assertEqual(self.manager.bar_projection(), IDLE_REMOTE_BAR)
        self.assertIsNone(IDLE_REMOTE_BAR["bar_insets"])

    def test_a_malformed_value_refuses_the_create_and_touches_nothing(self):
        with self.assertRaises(RemoteError) as caught:
            self.phone(bar_occlusion_points={"top": 1})
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.owned(), [])
        self.assertEqual(self.journals(), [])

    def test_a_rotation_replaces_them_and_a_resize_without_them_keeps_them(self):
        session = self.phone(bar_occlusion_points=IPHONE_17_PRO)
        landscape = {"top": 10.0, "bottom": 10.0, "left": 46.4, "right": 46.4}
        payload = dict(profile_request(874, 402), expected_revision=session.revision,
                       bar_occlusion_points=landscape)
        session = self.manager.resize(session.id, payload)
        self.assertEqual(session.bar_occlusion, landscape)
        bar = self.manager.bar_projection()
        self.assertEqual(bar["orientation"], "landscape")
        self.assertEqual(bar["bar_insets"], to_output_pixels(landscape, {"width": 874, "height": 402},
                                                             bar["logical_size"]))
        session = self.manager.resize(session.id, dict(profile_request(402, 874),
                                                       expected_revision=session.revision))
        self.assertEqual(session.bar_occlusion, landscape)
        self.assertEqual(json.loads(self.journals()[0].read_text())["bar_occlusion"], landscape)

    def test_the_field_never_reaches_the_profile_request(self):
        session = self.phone(bar_occlusion_points=IPHONE_17_PRO)
        self.assertNotIn("bar_occlusion_points", session.request)
        self.assertNotIn("bar_occlusion", session.request)


if __name__ == "__main__":
    unittest.main()
