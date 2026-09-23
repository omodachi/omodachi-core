"""The three bar module readers: fixed argv, bounded output, null on anything else.

Every input here is recorded host output (`wpctl get-volume`,
`omarchy-network-status`, `/sys/class/power_supply`) reproduced as fixture text
and temporary files. No command is run and no real device is read.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from omodachi_core.bar_modules import BarModules, STATUS_ROLES


class BarModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.outputs = {"wpctl": "Volume: 0.74\n", "network": "wifi\tRT-AX56U\t81\t5745.0\n"}
        self.calls = []
        self.now = [100.0]
        self.battery("BAT0", "Battery", capacity="84", status="Full", present="1")
        self.battery("ADP1", "Mains")

    def battery(self, name, kind, **fields):
        directory = self.root / name
        directory.mkdir(exist_ok=True)
        (directory / "type").write_text(kind + "\n")
        for key, value in fields.items():
            (directory / key).write_text(value + "\n")

    def runner(self, argv):
        self.calls.append(argv)
        if argv[0].endswith("wpctl"):
            return self.outputs["wpctl"]
        if argv[0].endswith("omarchy-network-status"):
            return self.outputs["network"]
        raise AssertionError(argv)

    def modules(self, **kwargs):
        return BarModules(runner=self.runner, power_supply=self.root,
                          clock=lambda: self.now[0], **kwargs)

    def test_the_three_readings_this_host_can_actually_answer_for(self):
        value = self.modules().snapshot()
        self.assertEqual(set(value), set(STATUS_ROLES))
        self.assertEqual(value["audio"], {"volume": 0.74, "muted": False})
        self.assertEqual(value["power"], {"percent": 84, "state": "full", "charging": False})
        self.assertEqual(value["network"], {"kind": "wifi", "name": "RT-AX56U", "signal": 81})
        self.assertEqual([argv[0] for argv in self.calls],
                         ["/usr/bin/wpctl", "/usr/bin/omarchy-network-status"])
        self.assertEqual(self.calls[0][1:], ("get-volume", "@DEFAULT_AUDIO_SINK@"))

    def test_the_readings_are_throttled_to_the_publish_cadence(self):
        modules = self.modules(throttle=2.0)
        modules.snapshot()
        modules.snapshot()
        modules.snapshot()
        self.assertEqual(len(self.calls), 2)  # one wpctl, one network-status
        self.now[0] += 2.0
        self.outputs["wpctl"] = "Volume: 0.10 [MUTED]\n"
        self.assertEqual(modules.snapshot()["audio"], {"volume": 0.1, "muted": True})
        self.assertEqual(len(self.calls), 4)

    def test_a_charging_and_a_missing_battery_are_different_answers(self):
        self.battery("BAT0", "Battery", capacity="31", status="Charging", present="1")
        self.assertEqual(self.modules().snapshot()["power"],
                         {"percent": 31, "state": "charging", "charging": True})
        # A desktop with only a mains supply publishes nothing, exactly like a
        # host whose sysfs cannot be read: unknown, never zero percent.
        for name in ("BAT0",):
            for path in sorted((self.root / name).iterdir()):
                path.unlink()
            (self.root / name / "type").write_text("Mains\n")
        self.assertIsNone(self.modules().snapshot()["power"])
        self.assertIsNone(BarModules(runner=self.runner, power_supply=self.root / "missing",
                                     clock=lambda: self.now[0]).snapshot()["power"])

    def test_a_battery_that_is_not_present_or_out_of_range_is_skipped(self):
        self.battery("BAT0", "Battery", capacity="84", status="Full", present="0")
        self.assertIsNone(self.modules().snapshot()["power"])
        self.battery("BAT0", "Battery", capacity="184", status="Full", present="1")
        self.assertIsNone(self.modules().snapshot()["power"])
        self.battery("BAT0", "Battery", capacity="84", status="Levitating", present="1")
        self.assertIsNone(self.modules().snapshot()["power"])

    def test_output_the_readers_do_not_recognise_is_null(self):
        for bad in ("", "Volume: abc\n", "0.74\n", "Volume: 42\n", "Muted: yes\n", None):
            with self.subTest(bad=bad):
                self.outputs["wpctl"] = bad
                self.assertIsNone(self.modules().snapshot()["audio"])
        for bad in ("", None, "\t\t\t\n", "a b;rm -rf\twhatever\n"):
            with self.subTest(bad=bad):
                self.outputs["network"] = bad
                self.assertIsNone(self.modules().snapshot()["network"])

    def test_a_wired_link_and_an_unknown_signal_still_read(self):
        self.outputs["network"] = "ethernet\t\t\t\n"
        self.assertEqual(self.modules().snapshot()["network"],
                         {"kind": "ethernet", "name": None, "signal": None})
        self.outputs["network"] = "wifi\tRT-AX56U\tnot-a-number\t5745.0\n"
        self.assertEqual(self.modules().snapshot()["network"]["signal"], None)
        self.outputs["network"] = "wifi\tRT-AX56U\t999\t5745.0\n"
        self.assertIsNone(self.modules().snapshot()["network"]["signal"])

    def test_a_reader_that_raises_does_not_take_the_others_with_it(self):
        """This runs on the daemon's own maintenance tick; it cannot throw."""
        def broken(argv):
            if argv[0].endswith("wpctl"):
                raise OSError("no such binary")
            return self.outputs["network"]
        value = BarModules(runner=broken, power_supply=self.root,
                           clock=lambda: self.now[0]).snapshot()
        self.assertIsNone(value["audio"])
        self.assertEqual(value["network"]["kind"], "wifi")
        self.assertEqual(value["power"]["percent"], 84)
        # And the real runner answers a missing binary the same way.
        from omodachi_core.bar_modules import _run
        self.assertIsNone(_run(("/nonexistent/omodachi-probe",)))


if __name__ == "__main__":
    unittest.main()
