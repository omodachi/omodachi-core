"""The notification mirror: real files, synthetic shell, no D-Bus."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import aiohttp

from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
from omodachi_core.notifications import (NotificationMirror, NotificationService,
                                         NotificationsUnavailable, project)

SNAPSHOT = {"id": 1, "originalId": 1, "app": "Omodachi", "appIcon": "/icons/a.png",
            "summary": "omodachi g1", "body": "hello", "image": "/images/b.png", "glyph": "",
            "execArgv": "[\"omarchy-shell\",\"shell\",\"summon\",\"com.omodachi.host\",\"{}\"]",
            "urgency": 2, "expireTimeout": 30000, "timestamp": 1789711988810}


class FakeShell:
    def __init__(self, dnd=False):
        self.calls = []
        self.dnd = dnd
        self.returncode = 0
    def __call__(self, argv, **options):
        self.calls.append(tuple(argv))
        method = argv[2]
        if method == "dndState":
            out = "on" if self.dnd else "off"
        elif method == "toggleDnd":
            self.dnd = not self.dnd
            out = "on" if self.dnd else "off"
        elif method == "setDnd":
            self.dnd = argv[3] == "true"
            out = "on" if self.dnd else "off"
        else:
            out = "ok"
        return subprocess.CompletedProcess(argv, self.returncode, out, "")


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "notifications"
        (self.root / "history").mkdir(parents=True)
        self.hub = Hub()
        self.shell = FakeShell()
        self.mirror = NotificationMirror(self.hub, state_dir=self.root, runner=self.shell)

    def tearDown(self):
        self.mirror.stop()
        self.temp.cleanup()

    def write(self, name, document, *, history=False):
        directory = self.root / "history" if history else self.root
        (directory / name).write_text(json.dumps(document))

    def test_the_command_line_in_a_snapshot_is_never_kept(self):
        row = project("1789711988810-1", SNAPSHOT, active=True)
        self.assertNotIn("execArgv", row)
        self.assertTrue(row["has_action"])
        self.assertEqual(json.dumps(row).count("omarchy-shell"), 0)
        self.assertEqual(row["urgency"], "critical")
        self.assertEqual(set(row), {"id", "app", "summary", "body", "glyph", "urgency",
                                    "timestamp", "has_action", "active"})

    def test_a_new_file_is_published_once_and_moving_it_marks_it_inactive(self):
        self.write("1789711988810-1.json", SNAPSHOT)
        fresh = self.mirror.scan()
        self.assertEqual([row["id"] for row in fresh], ["1789711988810-1"])
        events = [event for event in self.hub.events_since(0) if event.type == "notification.posted"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["summary"], "omodachi g1")
        self.assertEqual(self.mirror.scan(), [])
        (self.root / "1789711988810-1.json").rename(self.root / "history/1789711988810-1.json")
        self.mirror.scan()
        listing = self.mirror.history()
        self.assertFalse(listing["notifications"][0]["active"])
        self.assertIsNone(self.mirror.newest_active())
        self.assertEqual(len([e for e in self.hub.events_since(0) if e.type == "notification.posted"]), 1)

    def test_history_paging_and_a_since_cursor(self):
        for index in range(5):
            self.write(f"178971198881{index}-{index}.json", {**SNAPSHOT, "id": index,
                                                             "timestamp": 1789711988810 + index},
                       history=True)
        self.mirror.scan()
        listing = self.mirror.history()
        self.assertEqual(len(listing["notifications"]), 5)
        self.assertEqual(listing["cursor"], "1789711988814-4")
        later = self.mirror.history(since="1789711988812-2")
        self.assertEqual([row["id"] for row in later["notifications"]],
                         ["1789711988813-3", "1789711988814-4"])
        self.assertEqual(len(self.mirror.history(limit=2)["notifications"]), 2)
        for bad in ("../etc", "nope", "1"):
            with self.assertRaises(NotificationsUnavailable):
                self.mirror.history(since=bad)

    def test_invoke_only_the_newest_popup_and_dismiss_by_summary_otherwise(self):
        self.write("1789711988810-1.json", {**SNAPSHOT, "summary": "older", "timestamp": 1789711988810})
        self.write("1789711988899-2.json", {**SNAPSHOT, "summary": "newest", "timestamp": 1789711988899})
        self.mirror.scan()
        self.assertEqual(self.mirror.newest_active()["summary"], "newest")
        with self.assertRaises(NotificationsUnavailable):
            self.mirror.act("1789711988810-1", "invoke")
        # An older popup is not the one `dismissOne` would take, so it is
        # dismissed by its own summary instead.
        self.assertEqual(self.mirror.act("1789711988810-1", "dismiss")["result"], "ok")
        self.assertEqual(self.shell.calls[-1][1:], ("notifications", "dismiss", "older"))
        self.assertEqual(self.mirror.act("1789711988899-2", "invoke"),
                         {"id": "1789711988899-2", "action": "invoke", "result": "ok"})
        self.assertEqual(self.shell.calls[-1][1:], ("notifications", "invokeLast"))
        with self.assertRaises(NotificationsUnavailable):
            self.mirror.act("9999999999999-9", "dismiss")
        with self.assertRaises(NotificationsUnavailable):
            self.mirror.act("not-an-id", "dismiss")

    def test_dismiss_uses_dismiss_one_for_the_newest(self):
        self.write("1789711988899-2.json", SNAPSHOT)
        self.mirror.scan()
        self.assertEqual(self.mirror.act("1789711988899-2", "dismiss")["result"], "ok")
        self.assertEqual(self.shell.calls[-1][1:], ("notifications", "dismissOne"))
        self.assertFalse(self.mirror.history()["notifications"][0]["active"])

    def test_dnd_reads_writes_and_toggles(self):
        self.assertEqual(self.mirror.dnd(), {"dnd": False})
        self.assertEqual(self.mirror.set_dnd(True), {"dnd": True})
        self.assertEqual(self.shell.calls[-1][1:], ("notifications", "setDnd", "true"))
        self.assertEqual(self.mirror.dnd(), {"dnd": True})
        self.assertEqual(self.mirror.set_dnd(None), {"dnd": False})
        self.assertEqual(self.shell.calls[-1][1:], ("notifications", "toggleDnd"))
        with self.assertRaises(NotificationsUnavailable):
            self.mirror.set_dnd("yes")

    # --- ARCH-1 / review item 17: DND belongs to hub state --------------------
    def test_the_first_reading_lands_in_hub_state_rather_than_being_assumed(self):
        """A client must never draw the switch from a boolean of its own."""
        self.assertIsNone(self.hub.state_snapshot()["notifications"]["dnd"])
        self.shell.dnd = True
        self.mirror.refresh_dnd(force=True)
        self.assertIs(self.hub.state_snapshot()["notifications"]["dnd"], True)

    def test_a_change_made_on_the_desktop_is_pushed(self):
        self.mirror.refresh_dnd(force=True)
        events = len([row for row in self.hub._events if row.type == "notifications.changed"])
        # The desktop toggled it; the file the shell keeps DND in moved.
        self.shell.dnd = True
        self.mirror.dnd_path.write_text('{"dnd": true}')
        self.mirror.scan()
        self.assertIs(self.hub.state_snapshot()["notifications"]["dnd"], True)
        pushed = [row for row in self.hub._events if row.type == "notifications.changed"]
        self.assertEqual(len(pushed), events + 1)
        self.assertEqual(pushed[-1].payload["notifications"], {"dnd": True})
        self.assertIsNone(pushed[-1].device_id, "DND is not device-scoped")

    def test_an_unchanged_reading_publishes_nothing(self):
        self.mirror.refresh_dnd(force=True)
        before = len(self.hub._events)
        for _ in range(3):
            self.mirror.scan()
        self.assertEqual(len(self.hub._events), before)

    def test_reading_is_skipped_while_the_file_has_not_moved(self):
        """One subprocess per change, not one every two seconds."""
        self.mirror.dnd_path.write_text('{"dnd": false}')
        self.mirror.refresh_dnd(force=True)
        calls = len([row for row in self.shell.calls if row[2] == "dndState"])
        self.mirror.scan()
        self.mirror.scan()
        self.assertEqual(len([row for row in self.shell.calls if row[2] == "dndState"]), calls)

    def test_setting_it_moves_the_state_immediately(self):
        self.mirror.refresh_dnd(force=True)
        self.assertEqual(self.mirror.set_dnd(True), {"dnd": True})
        self.assertIs(self.hub.state_snapshot()["notifications"]["dnd"], True)

    def test_a_shell_that_will_not_answer_is_left_alone_for_a_while(self):
        """An unresponsive shell must not be asked again two seconds later.

        The first version of this retried on every 2 s pass, which is a
        subprocess against a busy IPC forever — and on the maintainer's own
        desktop it helped push `omarchy-shell` into "not responding".
        """
        clock = [1000.0]
        mirror = NotificationMirror(self.hub, state_dir=self.root, runner=self.shell,
                                    clock=lambda: clock[0])
        self.shell.returncode = 1
        mirror.refresh_dnd(force=True)
        attempts = len([row for row in self.shell.calls if row[2] == "dndState"])
        for _ in range(5):
            clock[0] += 2
            mirror.scan()
        self.assertEqual(len([row for row in self.shell.calls if row[2] == "dndState"]), attempts,
                         "no second attempt inside the backoff")
        clock[0] += 31
        self.shell.returncode = 0
        self.shell.dnd = True
        mirror.scan()
        self.assertIs(self.hub.state_snapshot()["notifications"]["dnd"], True)

    def test_the_state_file_answers_even_when_the_shell_does_not(self):
        """CORE-2 §3: the file the shell saves DND into is read, not used as a wakeup for `qs`."""
        self.shell.dnd = True
        self.mirror.refresh_dnd(force=True)
        self.shell.returncode = 1
        self.mirror.dnd_path.write_text('{"version": 3, "dnd": false}')
        self.assertIs(self.mirror.refresh_dnd(), False)
        self.assertIs(self.hub.state_snapshot()["notifications"]["dnd"], False)

    def test_a_shell_that_will_not_answer_leaves_the_last_reading_alone(self):
        self.shell.dnd = True
        self.mirror.refresh_dnd(force=True)
        self.shell.returncode = 1
        self.mirror.dnd_path.write_text('{"version": 3}')     # a file that cannot say
        self.assertIs(self.mirror.refresh_dnd(), True)
        self.assertIs(self.hub.state_snapshot()["notifications"]["dnd"], True)

    def test_reading_dnd_from_the_file_spawns_nothing_and_asking_is_rate_limited(self):
        calls = []
        runner = self.shell
        def counting(argv, **kwargs):
            calls.append((argv, kwargs["env"]["LANG"], kwargs["env"]["LC_ALL"]))
            return runner(argv, **kwargs)
        now = [100.0]
        mirror = NotificationMirror(self.hub, state_dir=self.root, runner=counting, clock=lambda: now[0])
        mirror.dnd_path.write_text('{"version": 3, "dnd": true}')
        for _ in range(10):
            self.assertEqual(mirror.dnd(), {"dnd": True})
        self.assertEqual(calls, [])
        mirror.dnd_path.unlink()
        self.shell.dnd = False
        for _ in range(10):
            self.assertEqual(mirror.dnd(), {"dnd": False})
            now[0] += 2
        self.assertEqual(len(calls), 1, "a missing file asks the shell at most every 30 s")
        self.assertEqual(calls[0][1:], ("C.UTF-8", "C.UTF-8"))
        now[0] += 30
        mirror.dnd()
        self.assertEqual(len(calls), 2)

    def test_an_unreadable_shell_is_a_503_not_a_crash(self):
        self.shell.returncode = 1
        with self.assertRaises(NotificationsUnavailable):
            self.mirror.dnd()

    def test_junk_in_the_directory_is_ignored(self):
        (self.root / "not-a-notification.txt").write_text("hello")
        (self.root / "1789711988810-1.json").write_text("{ not json")
        (self.root / "1789711988811-2.json").write_text(json.dumps([1, 2, 3]))
        self.write("1789711988812-3.json", SNAPSHOT)
        self.assertEqual([row["id"] for row in self.mirror.scan()], ["1789711988812-3"])


class NotificationApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "notifications"
        (self.root / "history").mkdir(parents=True)
        (self.root / "1789711988810-1.json").write_text(json.dumps(SNAPSHOT))
        self.hub = Hub()
        self.token = self.hub.register_device("phone")
        self.service = create_service(self.hub, demo=True)
        self.shell = FakeShell(dnd=True)
        self.mirror = NotificationMirror(self.hub, state_dir=self.root, runner=self.shell)
        self.mirror.scan()
        self.service.notification_mirror = self.mirror
        self.service.notifications = NotificationService(self.mirror)
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.client = aiohttp.ClientSession()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.headers = {"Authorization": "Bearer " + self.token}

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.close()
        self.mirror.stop()
        self.temp.cleanup()

    async def test_read_act_and_dnd_over_https(self):
        async with self.client.get(self.url + "/v1/notifications", headers=self.headers) as response:
            self.assertEqual(response.status, 200)
            listing = await response.json()
        self.assertEqual(listing["notifications"][0]["id"], "1789711988810-1")
        self.assertNotIn("execArgv", json.dumps(listing))
        async with self.client.get(self.url + "/v1/notifications/dnd", headers=self.headers) as response:
            self.assertEqual((await response.json())["dnd"], True)
        async with self.client.post(self.url + "/v1/notifications/dnd", headers=self.headers,
                                    json={"enabled": False}) as response:
            self.assertEqual((await response.json())["dnd"], False)
        async with self.client.post(self.url + "/v1/notifications/1789711988810-1:dismiss",
                                    headers=self.headers, json={}) as response:
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual((await response.json())["result"], "ok")
        async with self.client.post(self.url + "/v1/notifications/9999999999999-9:dismiss",
                                    headers=self.headers, json={}) as response:
            self.assertEqual(response.status, 404)
        async with self.client.post(self.url + "/v1/notifications/x:dismiss",
                                    headers=self.headers, json={}) as response:
            self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
