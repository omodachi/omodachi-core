"""MENU-2. Where the bar is, on the output a session owns.

The App puts its own mark on the picture on the strength of this rectangle, so
every case where the answer would have to be guessed has to come back `None`
rather than approximately right: a bar that is not there, a logo the user moved,
a layer that is not bar-shaped. A mark in the wrong place is a square of our
colour sitting on somebody else's desktop.

Only the logo is located. The Omodachi plugin's own slot moves with every other
widget beside it, so nothing here tries to find it (A-67).
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from omodachi_core.bar import parse_bar_layout
from omodachi_core.bar_geometry import (BarGeometry, BarStyle, bar_layer, compose, logo_slot,
                                        measured_position, session_output, user_shell_tokens,
                                        _round_half_up)


def layers(output="OMODACHI-1", *, rect=(0, 0, 1280, 30), namespace="omarchy-bar", extra=()):
    x, y, width, height = rect
    surfaces = [{"address": "0x1", "x": x, "y": y, "w": width, "h": height, "alpha": 1,
                 "namespace": namespace, "pid": 1}]
    return {output: {"levels": {"0": [{"address": "0x0", "x": x, "y": y, "w": width, "h": 900,
                                       "alpha": 1, "namespace": "omarchy-background", "pid": 1}],
                                "1": [], "2": surfaces, "3": list(extra)}}}


SECTIONS = {"left": [{"id": "omarchy.menu", "role": "logo"},
                     {"id": "omarchy.workspaces", "role": "workspaces"}]}


class BarLayerTests(unittest.TestCase):
    def test_the_bar_layer_is_read_in_the_outputs_own_coordinates(self):
        """The compositor reports layer boxes in layout coordinates.

        A session's output is placed to the right of the physical screen, so
        its bar starts at x=2304 globally and at x=0 on the screen the iPad is
        actually looking at. Publishing the global number would put the logo
        two thirds of the way across the picture.
        """
        document = layers(rect=(2304, 0, 1280, 30))
        self.assertEqual(bar_layer(document, "OMODACHI-1", origin=(2304, 0)),
                         {"x": 0.0, "y": 0.0, "width": 1280.0, "height": 30.0})

    def test_a_hidden_bar_is_no_geometry_at_all(self):
        """The `omarchy-bar` layer is simply absent, which is the answer."""
        document = layers(namespace="omarchy-background")
        self.assertIsNone(bar_layer(document, "OMODACHI-1"))
        self.assertIsNone(compose(output="OMODACHI-1", logical_size={"width": 1280, "height": 894},
                                  position="top", layer=None, sections=SECTIONS))

    def test_an_output_the_document_does_not_carry_is_none(self):
        self.assertIsNone(bar_layer(layers(), "eDP-1"))

    def test_two_bars_on_one_output_are_refused(self):
        document = layers()
        document["OMODACHI-1"]["levels"]["3"] = [
            {"address": "0x2", "x": 0, "y": 870, "w": 1280, "h": 30, "alpha": 1,
             "namespace": "omarchy-bar", "pid": 2}]
        self.assertIsNone(bar_layer(document, "OMODACHI-1"))

    def test_a_malformed_document_never_produces_a_rectangle(self):
        for value in (None, [], {"OMODACHI-1": []}, {"OMODACHI-1": {"levels": []}},
                      {"OMODACHI-1": {"levels": {"0": [{"namespace": "omarchy-bar", "x": "a",
                                                        "y": 0, "w": 1, "h": 1}]}}},
                      {"OMODACHI-1": {"levels": {"0": [{"namespace": "omarchy-bar", "x": 0,
                                                        "y": 0, "w": 0, "h": 30}]}}}):
            with self.subTest(value=value):
                self.assertIsNone(bar_layer(value, "OMODACHI-1"))


class BarStyleTests(unittest.TestCase):
    """`Style.qml`'s two numbers, ported (UX-3 §3).

    The arithmetic is short and every step of it is somewhere a mark can end up
    in the wrong place, so each one is pinned rather than inferred from the
    result.
    """

    def test_the_stock_theme_gives_omarchys_own_defaults(self):
        self.assertEqual(BarStyle.from_tokens({}), BarStyle(leading_space=8, icon_slot=27))
        self.assertEqual(BarStyle.from_tokens(None), BarStyle(leading_space=8, icon_slot=27))

    def test_leos_host_is_a_fourteen_pixel_shell(self):
        """`base-size = 14` in `~/.config/omarchy/shell.toml`, and nothing else.

        The numbers this produces are checkable against the bar itself: the
        same font scale gives `size-horizontal` 26 -> 30, and 30 is what
        `hyprctl layers` reports for the `omarchy-bar` surface on that host.
        """
        style = BarStyle.from_tokens({"font": {"base-size": 14}})
        self.assertEqual(style, BarStyle(leading_space=9, icon_slot=32))

    def test_the_rounding_is_qmls_half_up_and_not_pythons_half_even(self):
        """`Math.round(30.5)` is 31 in QML. `round(30.5)` is **30** in Python.

        Every one of these numbers is a pixel offset for a mark drawn over
        somebody's screen, so the half case is pinned rather than left to
        whichever language the port happens to be written in.
        """
        self.assertEqual(_round_half_up(30.5), 31)
        self.assertEqual(round(30.5), 30, "this is the trap being avoided")
        self.assertEqual(_round_half_up(31.5), 32)
        self.assertEqual(_round_half_up(8.0), 8)

    def test_a_theme_can_switch_either_scale_off(self):
        tokens = {"font": {"base-size": 24}, "spacing": {"scale-with-font": False},
                  "bar": {"scale-with-font": "false"}}
        self.assertEqual(BarStyle.from_tokens(tokens), BarStyle(leading_space=8, icon_slot=27))

    def test_a_spacing_scale_moves_the_margin_and_not_the_slot(self):
        style = BarStyle.from_tokens({"spacing": {"scale": 2.0, "scale-with-font": False}})
        self.assertEqual(style, BarStyle(leading_space=16, icon_slot=27))

    def test_nonsense_tokens_fall_back_rather_than_raising(self):
        for tokens in ({"font": {"base-size": "huge"}}, {"font": {"base-size": 0}},
                       {"spacing": {"scale": "wide"}}, {"spacing": {"scale": -3}},
                       {"font": "not a table"}, {"bar": {"scale-with-font": "maybe"}}):
            with self.subTest(tokens=tokens):
                style = BarStyle.from_tokens(tokens)
                self.assertGreaterEqual(style.leading_space, 1)
                self.assertGreaterEqual(style.icon_slot, 1)

    def test_a_missing_user_shell_toml_is_not_an_error(self):
        directory = Path(tempfile.mkdtemp())
        try:
            self.assertEqual(user_shell_tokens(directory), {})
            (directory / ".config/omarchy").mkdir(parents=True)
            (directory / ".config/omarchy/shell.toml").write_text("[font]\nbase-size = 14\n")
            self.assertEqual(BarStyle.from_tokens(user_shell_tokens(directory)),
                             BarStyle(leading_space=9, icon_slot=32))
            (directory / ".config/omarchy/shell.toml").write_text("this is not toml {{")
            self.assertEqual(user_shell_tokens(directory), {})
        finally:
            shutil.rmtree(directory, ignore_errors=True)


class LogoSlotTests(unittest.TestCase):
    """Four bar positions, the one rectangle the App covers in each.

    UX-3 §3. These used to expect a square at the bar's origin, which is what
    MENU-2 assumed and what Leo could see was wrong: the mark sat to the left
    of the icon. `Bar.qml` insets the leading section by `Style.space(8)` and
    gives the widget `Style.bar.iconSlot` along the bar, so on Leo's host the
    old rectangle's centre was 10 px out.
    """

    #: Leo's own host: `base-size = 14`, a 30-thick top bar.
    LEO = BarStyle.from_tokens({"font": {"base-size": 14}})

    CASES = {
        "top": ((0, 0, 1280, 30), {"x": 9.0, "y": 0.0, "width": 32.0, "height": 30.0}),
        "bottom": ((0, 864, 1280, 30), {"x": 9.0, "y": 864.0, "width": 32.0, "height": 30.0}),
        "left": ((0, 0, 34, 894), {"x": 0.0, "y": 9.0, "width": 34.0, "height": 32.0}),
        "right": ((1246, 0, 34, 894), {"x": 1246.0, "y": 9.0, "width": 34.0, "height": 32.0}),
    }

    def test_each_bar_position_puts_the_logo_at_the_leading_end(self):
        for position, (rect, expected) in self.CASES.items():
            with self.subTest(position=position):
                layer = bar_layer(layers(rect=rect), "OMODACHI-1")
                self.assertEqual(logo_slot(layer, position, SECTIONS, self.LEO), expected)

    def test_the_published_geometry_agrees_with_the_slot_for_every_position(self):
        for position, (rect, expected) in self.CASES.items():
            with self.subTest(position=position):
                value = compose(output="OMODACHI-1", logical_size={"width": 1280, "height": 894},
                                position=position, layer=bar_layer(layers(rect=rect), "OMODACHI-1"),
                                sections=SECTIONS, style=self.LEO)
                self.assertEqual(value["position"], position)
                self.assertEqual(value["logo"], expected)
                self.assertNotIn("plugin", value, "the plugin's own slot is not located (A-67)")

    def test_a_shell_json_without_a_position_is_measured_not_assumed(self):
        """`bar.position` is optional; the rectangle answers it either way."""
        size = {"width": 1280, "height": 894}
        for position, rect in self.CASES.items():
            with self.subTest(position=position):
                layer = bar_layer(layers(rect=rect[0]), "OMODACHI-1")
                self.assertEqual(measured_position(layer, size), position)
                value = compose(output="OMODACHI-1", logical_size=size, position=None,
                                layer=layer, sections=SECTIONS, style=self.LEO)
                self.assertEqual(value["position"], position)
                self.assertEqual(value["logo"], rect[1])

    def test_a_bar_too_short_to_hold_the_slot_has_no_logo(self):
        """The margin plus one slot has to fit, or there is nothing to cover."""
        layer = bar_layer(layers(rect=(0, 0, 20, 30)), "OMODACHI-1")
        self.assertIsNone(logo_slot(layer, "top", SECTIONS, self.LEO))

    def test_the_mark_lands_on_the_icon_and_not_beside_it(self):
        """The regression Leo reported, as an arithmetic statement.

        On the real host the icon's centre is at 9 + 32/2 = 25. The rectangle
        MENU-2 published put it at 15 - ten logical pixels to the left, which
        on an 11-inch iPad is about the width of the icon's own margin, and is
        why the mark read as misplaced rather than merely imprecise.
        """
        layer = bar_layer(layers(rect=(0, 0, 1280, 30)), "OMODACHI-1")
        slot = logo_slot(layer, "top", SECTIONS, self.LEO)
        self.assertEqual(slot["x"] + slot["width"] / 2, 25.0)
        old = 0.0 + 30.0 / 2
        self.assertEqual(slot["x"] + slot["width"] / 2 - old, 10.0)

    def test_a_layer_that_is_not_bar_shaped_is_not_named(self):
        size = {"width": 1280, "height": 894}
        self.assertIsNone(measured_position({"x": 100.0, "y": 100.0, "width": 200.0,
                                             "height": 200.0}, size))
        self.assertIsNone(compose(output="OMODACHI-1", logical_size=size, position=None,
                                  layer={"x": 100.0, "y": 100.0, "width": 200.0, "height": 200.0},
                                  sections=SECTIONS))

    def test_a_logo_the_user_moved_or_removed_is_not_guessed(self):
        """`shell.json` decides. Nothing is intercepted where nothing is drawn."""
        layer = bar_layer(layers(), "OMODACHI-1")
        for sections in ({"left": []},
                         {"left": [{"id": "omarchy.workspaces", "role": "workspaces"}]},
                         {"left": [{"id": "com.omodachi.menu", "role": "unsupported"}]},
                         {}, None):
            with self.subTest(sections=sections):
                self.assertIsNone(logo_slot(layer, "top", sections))

    def test_the_real_host_layout_still_has_the_clone_first(self):
        """Leo's own `shell.json` opens with `com.omodachi.menu`, not the logo.

        Until the retirement step puts `omarchy.menu` back (MENU-2 §3, run with
        Leo present), the logo square is legitimately `None` on that host - and
        the App falls back to the corner handle instead of covering a
        rectangle that belongs to somebody else's widget.
        """
        document = {"version": 1, "bar": {"position": "top", "layout": {
            "left": [{"id": "com.omodachi.menu"}, {"id": "omarchy.workspaces"}],
            "center": [], "right": [{"id": "com.omodachi.host"}]}}}
        sections = parse_bar_layout(document, source_status="available")
        self.assertEqual(sections["left"][0]["role"], "unsupported")
        self.assertIsNone(logo_slot(bar_layer(layers(), "OMODACHI-1"), "top", sections))


class NoPluginSlotTests(unittest.TestCase):
    """The plugin's own bar slot is deliberately not located.

    It is one widget among a dozen third-party ones and its position moves
    whenever any of them does, so a rectangle for it would be a mark of ours
    landing on somebody else's widget. The icon keeps behaving exactly as it
    does for a person sitting at the machine, and the one thing a takeover has
    to change about it is decided inside the plugin (A-67).
    """

    def test_the_published_geometry_has_no_plugin_field_at_all(self):
        value = compose(output="OMODACHI-1", logical_size={"width": 1280, "height": 894},
                        position="top", layer=bar_layer(layers(), "OMODACHI-1"), sections=SECTIONS)
        self.assertEqual(set(value), {"output", "logical_size", "position", "bar", "logo"})

    def test_core_offers_no_way_to_report_a_widget_rectangle(self):
        """The `bar.widget` op and its transport are gone, not disabled."""
        from omodachi_core import bar_geometry, plugin_bridge
        self.assertFalse(hasattr(bar_geometry, "plugin_slot"))
        self.assertFalse(hasattr(BarGeometry, "report_widget"))
        self.assertFalse(hasattr(plugin_bridge, "read_reports"))


class LayerEventTests(unittest.IsolatedAsyncioTestCase):
    """`openlayer`/`closelayer omarchy-bar` is what "the bar moved" looks like."""

    async def test_only_the_bar_namespace_marks_the_geometry_stale(self):
        from types import SimpleNamespace
        from omodachi_core.hub import Hub
        from omodachi_core.remote.service import RemoteService
        service = RemoteService(Hub())
        seen = []
        service.core = SimpleNamespace(
            bar_geometry=SimpleNamespace(mark_dirty=lambda: seen.append("dirty"),
                                         layers=lambda: seen.append("layers")),
            refresh_bar=lambda: seen.append("refresh"))
        self.assertFalse(await service.reread_bar_geometry())
        service._bar_layer_dirty = True
        self.assertTrue(await service.reread_bar_geometry())
        self.assertEqual(seen, ["dirty", "layers", "refresh"])
        self.assertFalse(service._bar_layer_dirty)


class ReaderTests(unittest.TestCase):
    class Session:
        class Profile:
            class Mode:
                width, height = 1280, 894
            output_mode_pixels = Mode()
            output_scale = 1.0
        output_name = "OMODACHI-1"
        profile = Profile()
        position = (2304, 0)

    def reader(self, calls, *, clock=None):
        def runner(argv, env):
            calls.append(argv)
            return json.dumps(layers(rect=(2304, 0, 1280, 30)))
        return BarGeometry(runner=runner, environment=lambda: {},
                           clock=clock or (lambda: 0.0))

    def test_the_session_supplies_its_own_output_without_a_second_probe(self):
        self.assertEqual(session_output(self.Session()),
                         {"name": "OMODACHI-1", "origin": (2304.0, 0.0),
                          "logical_size": {"width": 1280, "height": 894}})
        self.assertIsNone(session_output(None))

    def test_a_snapshot_reads_the_compositor_once_inside_the_throttle(self):
        calls = []
        reader = self.reader(calls)
        for _ in range(5):
            reader.snapshot(self.Session(), "top", SECTIONS)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("/usr/bin/hyprctl", "-j", "layers"))

    def test_the_layer_events_force_the_next_read(self):
        calls = []
        reader = self.reader(calls)
        reader.snapshot(self.Session(), "top", SECTIONS)
        reader.mark_dirty()
        reader.snapshot(self.Session(), "top", SECTIONS)
        self.assertEqual(len(calls), 2)

    def test_no_session_means_no_geometry_and_no_probe(self):
        calls = []
        reader = self.reader(calls)
        self.assertIsNone(reader.snapshot(None, "top", SECTIONS))
        self.assertEqual(calls, [])

    def test_a_compositor_that_cannot_be_reached_is_null_not_an_error(self):
        def runner(argv, env):
            raise OSError("no compositor")
        reader = BarGeometry(runner=runner, environment=lambda: {}, clock=lambda: 0.0)
        self.assertIsNone(reader.snapshot(self.Session(), "top", SECTIONS))

    def test_the_geometry_is_taken_on_the_sessions_output_not_the_physical_one(self):
        document = layers(rect=(2304, 0, 1280, 30))
        document["eDP-1"] = {"levels": {"0": [{"address": "0x9", "x": 0, "y": 0, "w": 2304, "h": 30,
                                               "alpha": 1, "namespace": "omarchy-bar", "pid": 1}]}}
        reader = BarGeometry(runner=lambda argv, env: json.dumps(document),
                             environment=lambda: {}, clock=lambda: 0.0)
        value = reader.snapshot(self.Session(), "top", SECTIONS)
        self.assertEqual(value["output"], "OMODACHI-1")
        self.assertEqual(value["bar"], {"x": 0.0, "y": 0.0, "width": 1280.0, "height": 30.0})


if __name__ == "__main__":
    unittest.main()
