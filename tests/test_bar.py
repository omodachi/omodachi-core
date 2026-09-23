"""Bar tests use tagged source shape and explicitly synthetic local JSON only."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from omodachi_core.bar import parse_bar_layout


class BarLayoutTests(unittest.TestCase):
    _MISSING = object()

    def config(self, position=_MISSING, **layout):
        bar = {"layout": layout}
        if position is not self._MISSING:
            bar["position"] = position
        return {"version": 1, "bar": bar}

    def test_demo_uses_source_shape_and_preserves_semantic_placement(self):
        path = Path(__file__).parents[1] / "src/omodachi_core/data/demo-shell.json"
        result = parse_bar_layout(json.loads(path.read_text()), source_status="fixture")
        self.assertEqual(result["source"], "shell.json")
        self.assertEqual(result["source_status"], "fixture")
        self.assertEqual(result["position"], "top")
        self.assertEqual([x["role"] for x in result["left"]], ["logo", "workspaces", "focused_window"])
        self.assertEqual([x["role"] for x in result["center"]], ["clock"])
        self.assertEqual([x["role"] for x in result["right"]],
                         ["system_tray", "agent_usage", "network", "audio", "power", "stream"])
        self.assertRegex(result["revision"], r"^[0-9a-f]{16}$")
        self.assertEqual(set(result), {"source", "source_status", "position", "revision", "modules",
                                       "geometry", "left", "center", "right"})
        self.assertIsNone(result["geometry"])
        # Demo reads nothing off this machine, so every status is unknown.
        self.assertEqual([row["status"] for row in result["modules"]], [None, None, None])

    def test_agent_usage_and_panel_are_distinct_semantic_widgets(self):
        value=parse_bar_layout(self.config(left=['omarchy.menu','com.omodachi.host','omarchy.agents']))
        self.assertEqual([row['role'] for row in value['left']],['logo','panel','agent_usage'])

    def test_position_enum_is_projected_and_invalid_position_fails_closed(self):
        for position in ("top", "bottom", "left", "right"):
            with self.subTest(position=position):
                result = parse_bar_layout(self.config(position=position, left=["omarchy.menu"]))
                self.assertEqual(result["source_status"], "available")
                self.assertEqual(result["position"], position)
        self.assertIsNone(parse_bar_layout(self.config(left=["omarchy.menu"]))["position"])
        for position in ("diagonal", "TOP", 1, {}, []):
            with self.subTest(position=position):
                result = parse_bar_layout(self.config(position=position, left=["omarchy.menu"]))
                self.assertEqual(result["source_status"], "unavailable")
                self.assertIsNone(result["position"])
                self.assertEqual(result["left"], [])

    def test_tagged_default_source_widgets_unknowns_stay_unsupported(self):
        # Widget IDs are from v4.0.3 config/omarchy/shell.json, not a live host.
        result = parse_bar_layout(self.config(
            left=[{"id":"omarchy.menu"},{"id":"omarchy.workspaces"}],
            center=[{"id":"omarchy.indicators"},{"id":"omarchy.clock"},{"id":"omarchy.keyboard-layout"}],
            right=[{"id":"omarchy.tray"},{"id":"omarchy.agents"},{"id":"omarchy.audio"},
                   {"id":"omarchy.bluetooth"}],
        ))
        self.assertEqual(result["source_status"], "available")
        self.assertEqual(result["center"][0], {"id":"omarchy.indicators","role":"unsupported"})
        self.assertEqual(result["center"][1]["role"], "clock")
        # Audio is reviewed now (CORE-1 §5); bluetooth still has no source.
        self.assertEqual(result["right"][2]["role"], "audio")
        self.assertEqual(result["right"][-1]["role"], "unsupported")

    def test_string_entry_and_absent_sections_match_util_normalization(self):
        result = parse_bar_layout(self.config(left=["omarchy.workspaces"]))
        self.assertEqual(result["left"], [{"id":"omarchy.workspaces","role":"workspaces"}])
        self.assertEqual(result["center"], [])
        self.assertEqual(result["right"], [])
        self.assertEqual(result["source_status"], "available")

    def test_tray_pins_to_inner_edges_and_last_tray_wins(self):
        rows = ["omarchy.tray", "omarchy.clock", "omarchy.tray", "omarchy.agents"]
        result = parse_bar_layout(self.config(left=rows, center=rows, right=rows))
        self.assertEqual([x["id"] for x in result["left"]], ["omarchy.clock","omarchy.agents","omarchy.tray"])
        self.assertEqual(result["left"], result["center"])
        self.assertEqual([x["id"] for x in result["right"]], ["omarchy.tray","omarchy.clock","omarchy.agents"])

    def test_preserves_repeated_nontray_ids_and_input_immutability(self):
        config = self.config(left=[{"id":"omarchy.clock","format":"HH:mm"},{"id":"omarchy.clock","format":"ddd"}])
        original = deepcopy(config)
        result = parse_bar_layout(config)
        self.assertEqual(len(result["left"]), 2)
        self.assertEqual(config, original)
        result["left"][0]["id"] = "changed"
        self.assertEqual(config, original)

    def test_drops_private_settings_and_hash_is_only_safe_projection(self):
        config = self.config(center=[{"id":"omarchy.clock","format":"private-title","apiKey":"secret"}])
        config["private"] = {"credential":"secret"}
        result = parse_bar_layout(config)
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("private-title", json.dumps(result))
        config["private"] = {"credential":"changed"}
        config["bar"]["layout"]["center"][0]["format"] = "changed"
        self.assertEqual(parse_bar_layout(config), result)

    def test_custom_command_qml_never_acquire_reviewed_role_or_leak_content(self):
        for extra in ({"type":"command","exec":"private-shell","onClick":"private-click"}, {"type":"qml","source":"private/path.qml"}, {"source":"private/path.qml"}, {"exec":"private-shell"}):
            with self.subTest(extra=extra):
                result = parse_bar_layout(self.config(left=[{"id":"omarchy.clock", **extra}]))
                self.assertEqual(result["left"], [{"id":"omarchy.clock","role":"unsupported"}])
                self.assertNotIn("private", json.dumps(result))
        result = parse_bar_layout(self.config(left=[{"id":"vpn","type":"command","exec":"private-shell"}]))
        self.assertEqual(result["left"], [{"id":"vpn","role":"unsupported"}])

    def test_role_input_cannot_override_reviewed_mapping(self):
        result = parse_bar_layout(self.config(left=[{"id":"unknown.widget","role":"stream"},{"id":"omarchy.clock","role":"logo"}]))
        self.assertEqual([x["role"] for x in result["left"]], ["unsupported","clock"])

    def test_revision_changes_with_public_layout(self):
        a = parse_bar_layout(self.config(left=["omarchy.workspaces"]))
        b = parse_bar_layout(self.config(right=["omarchy.workspaces"]))
        c = parse_bar_layout(self.config(left=["omarchy.workspaces"]), source_status="fixture")
        self.assertNotEqual(a["revision"], b["revision"])
        self.assertNotEqual(a["revision"], c["revision"])
        self.assertEqual(a, parse_bar_layout(self.config(left=["omarchy.workspaces"])))

    def test_malformed_and_unsupported_shapes_fail_closed(self):
        for config in (None, [], "raw json", {}, {"version":True,"bar":{"layout":{}}}, {"version":2,"bar":{"layout":{}}}, {"version":1,"bar":{}}, {"version":1,"bar":{"id":"custom.bar","layout":{}}}, self.config(left="omarchy.clock"), self.config(left=[None]), self.config(left=[{"component":"omarchy.clock"}])):
            with self.subTest(config=config):
                result = parse_bar_layout(config)
                self.assertEqual(result["source_status"], "unavailable")
                self.assertEqual([result[x] for x in ("left","center","right")], [[],[],[]])
                self.assertIsNone(result["position"])

    def test_unsafe_ids_and_oversize_sections_never_leak_or_truncate(self):
        for bad_id in ("", "../private", "https://private", "a;exec", "a\\nsecret", "a"*129, 9, {"id":"secret"}):
            with self.subTest(bad_id=bad_id):
                result = parse_bar_layout(self.config(left=[{"id":bad_id}]))
                self.assertEqual(result["source_status"], "unavailable")
                self.assertEqual(result["left"], [])
        self.assertEqual(parse_bar_layout(self.config(left=["omarchy.clock"]*64))["source_status"], "available")
        result = parse_bar_layout(self.config(left=["omarchy.clock"]*65))
        self.assertEqual(result["source_status"], "unavailable")
        self.assertEqual(result["left"], [])

    def test_unavailable_does_not_reuse_input_and_status_is_host_owned_enum(self):
        result = parse_bar_layout(self.config(left=["omarchy.clock"]), source_status="unavailable")
        self.assertEqual(result["left"], [])
        self.assertIsNone(result["position"])
        for status in ("live", True, None, {}, []):
            with self.assertRaises(ValueError): parse_bar_layout(self.config(), source_status=status)


class BarModuleStatusTests(unittest.TestCase):
    """SPEC-F2 §2 drew no network/volume/battery because state had no field."""

    LAYOUT = {"version": 1, "bar": {"layout": {
        "left": ["omarchy.menu", "omarchy.workspaces"],
        "center": ["omarchy.clock", "omarchy.weather"],
        "right": ["omarchy.network", "omarchy.audio", "omarchy.power", "omarchy.bluetooth"]}}}
    READINGS = {"audio": {"volume": 0.74, "muted": False},
                "power": {"percent": 84, "state": "full", "charging": False},
                "network": {"kind": "wifi", "name": "RT-AX56U", "signal": 81}}

    def test_only_the_modules_the_host_carries_are_published_with_their_status(self):
        result = parse_bar_layout(self.LAYOUT, statuses=self.READINGS)
        self.assertEqual([(row["id"], row["status"]) for row in result["modules"]],
                         [("omarchy.network", self.READINGS["network"]),
                          ("omarchy.audio", self.READINGS["audio"]),
                          ("omarchy.power", self.READINGS["power"])])
        # Bluetooth and weather have no cheap read-only source; they stay
        # layout-only rather than becoming an invented number.
        self.assertEqual([row["role"] for row in result["right"]][-1], "unsupported")
        self.assertNotIn("omarchy.weather", [row["id"] for row in result["modules"]])

    def test_a_reading_the_host_does_not_have_is_null_not_zero(self):
        result = parse_bar_layout(self.LAYOUT, statuses={"audio": None, "power": self.READINGS["power"]})
        rows = {row["id"]: row["status"] for row in result["modules"]}
        self.assertIsNone(rows["omarchy.audio"])
        self.assertIsNone(rows["omarchy.network"])
        self.assertEqual(rows["omarchy.power"]["percent"], 84)
        # No statuses at all is the same answer, and the revision differs from
        # the one that carried a reading.
        blank = parse_bar_layout(self.LAYOUT)
        self.assertTrue(all(row["status"] is None for row in blank["modules"]))
        self.assertNotEqual(blank["revision"], result["revision"])

    def test_a_status_that_is_not_a_small_flat_object_is_dropped(self):
        for value in ("0.74", 0.74, [], {}, {"Volume": 1}, {"v": object()}, {"v": float("inf")},
                      {"v": "x" * 65}, {"v": "\u0007"}, dict.fromkeys("abcdefghi", 1)):
            with self.subTest(value=value):
                result = parse_bar_layout(self.LAYOUT, statuses={"audio": value})
                rows = {row["id"]: row["status"] for row in result["modules"]}
                self.assertIsNone(rows["omarchy.audio"])

    def test_a_module_the_host_removed_from_its_bar_is_not_published(self):
        without = {"version": 1, "bar": {"layout": {"left": ["omarchy.workspaces"]}}}
        self.assertEqual(parse_bar_layout(without, statuses=self.READINGS)["modules"], [])
        # An unavailable source publishes no modules either.
        self.assertEqual(parse_bar_layout(self.LAYOUT, source_status="unavailable",
                                          statuses=self.READINGS)["modules"], [])

    def test_a_custom_module_wearing_an_official_id_is_not_given_a_status(self):
        document = {"version": 1, "bar": {"layout": {
            "right": [{"id": "omarchy.audio", "exec": "my-script"}]}}}
        result = parse_bar_layout(document, statuses=self.READINGS)
        self.assertEqual(result["modules"], [])
        self.assertEqual(result["right"][0]["role"], "unsupported")


if __name__ == "__main__":
    unittest.main()
