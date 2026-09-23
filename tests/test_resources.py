"""PERF-4 §0: the daemon's reading of what it is holding."""
import json
import unittest

from omodachi_core.hub import Hub
from omodachi_core.resources import ResourceMonitor, RSS_WARN_BYTES


class ResourceMonitorTests(unittest.TestCase):
    def setUp(self):
        self.hub = Hub()
        self.monitor = ResourceMonitor(self.hub)

    def test_a_snapshot_names_every_reading_and_never_raises(self):
        reading = self.monitor.snapshot()
        self.assertEqual(set(reading), {"started_at", "uptime_seconds", "rss_bytes", "open_files",
                                        "asyncio_tasks", "threads", "event_history",
                                        "event_subscribers", "event_cursor",
                                        "condition_shells_total", "condition_shells_5m"})
        # MENU-3: no condition engine on this hub, so the shell counts are
        # null rather than a zero that would claim something was counted.
        self.assertIsNone(reading["condition_shells_total"])
        # Every platform-dependent value is allowed to be unknown, and none of
        # them may be a guess: a monitor that invents a number is worse than
        # one that says it cannot see.
        for key in ("rss_bytes", "open_files", "asyncio_tasks"):
            self.assertTrue(reading[key] is None or isinstance(reading[key], int), key)
        self.assertGreaterEqual(reading["threads"], 1)
        self.assertEqual(reading["event_history"], 0)

    def test_the_retained_history_is_what_the_reading_counts(self):
        for index in range(5):
            self.hub.publish("state.changed", {"revision": index})
        self.assertEqual(self.monitor.snapshot()["event_history"], 5)
        self.assertEqual(self.monitor.snapshot()["event_cursor"], 5)

    def test_a_reading_past_its_threshold_is_named_in_the_line(self):
        healthy = {"started_at": 0.0, "uptime_seconds": 1.0, "rss_bytes": 120 * 1048576,
                   "open_files": 12, "asyncio_tasks": 6, "threads": 3,
                   "event_history": 4, "event_subscribers": 1, "event_cursor": 4}
        self.assertEqual(self.monitor.warnings(healthy), [])
        self.assertNotIn("OVER", self.monitor.line(healthy))
        # What the host was actually found at.
        sick = {**healthy, "rss_bytes": 12_000 * 1048576, "open_files": 900}
        self.assertEqual(self.monitor.warnings(sick), ["rss_bytes", "open_files"])
        self.assertIn("OVER=rss_bytes,open_files", self.monitor.line(sick))
        self.assertIn("resources rss=12000MB", self.monitor.line(sick))
        self.assertGreater(RSS_WARN_BYTES, 0)

    def test_health_carries_the_reading_and_says_so_when_it_has_none(self):
        self.assertIsNone(self.hub.dispatch("health", {})["resources"])
        self.hub.resources = self.monitor
        reading = self.hub.dispatch("health", {})["resources"]
        self.assertIsInstance(reading, dict)
        self.assertIn("rss_bytes", reading)
        json.dumps(reading)   # it has to survive the wire


if __name__ == "__main__":
    unittest.main()
