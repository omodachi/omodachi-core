"""NOTIFY-3: Omodachi's own request notifications are withdrawn once decided.

Real files in a temporary directory stand in for the Omarchy shell's
`~/.local/state/omarchy/notifications/`; a fake `omarchy-shell` records what it
was asked and can move a file into history the way the real shell does when a
toast is dismissed.
"""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from omodachi_core.auth import DeviceAuthenticator
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.notifications import (NotificationMirror, WITHDRAW_GRACE_SECONDS, project,
                                         request_marker)
from omodachi_core.pairing import PairingStore
from omodachi_core.push import Retract, Watcher

REQUEST = "pair_" + "a" * 32
OTHER_REQUEST = "pair_" + "b" * 32


def summon(payload):
    return json.dumps(["omarchy-shell", "shell", "summon", "com.omodachi.host",
                       json.dumps(payload, separators=(",", ":"))], separators=(",", ":"))


def pairing(request_id=REQUEST, name="h6-iphone", timestamp=1791102837949, ident=4):
    """What the plugin's `announcePending` leaves on disk (copied from the host)."""
    return {"id": ident, "originalId": ident, "app": "Omodachi", "appIcon": "",
            "summary": f"“{name}” via Tailscale wants to connect",
            "body": "ios-2b0aea5f · via Tailscale device mac\nApproving grants: screen",
            "image": "", "glyph": "", "execArgv": summon({"view": "devices", "request_id": request_id}),
            "urgency": 2, "expireTimeout": 30000, "timestamp": timestamp}


def ssh_request(device="ios-2b0aea5f-46fc", name="h6-ipad", timestamp=1791103408030, ident=5):
    return {"id": ident, "originalId": ident, "app": "Omodachi", "appIcon": "",
            "summary": f"“{name}” wants an SSH login",
            "body": f"{device} · SHA256:abc\nApproving adds this key", "image": "", "glyph": "",
            "execArgv": summon({"view": "devices"}), "urgency": 2, "expireTimeout": 30000,
            "timestamp": timestamp}


def name_of(document):
    return f"{document['timestamp']}-{document['id']}.json"


class Shell:
    """`omarchy-shell notifications <method>`: records, and dismisses like the shell."""

    def __init__(self, root):
        self.root = root
        self.calls = []
        self.answer = "ok"
        self.moves = True
        self.returncode = 0

    def __call__(self, argv, **options):
        self.calls.append(tuple(argv[2:]))
        if self.returncode:
            return subprocess.CompletedProcess(argv, self.returncode, "", "")
        if argv[2] == "dismiss" and self.answer == "ok" and self.moves:
            # `dismiss(summary)`: every popup whose title contains the needle
            # is archived into history.
            for path in sorted(self.root.glob("*.json")):
                document = json.loads(path.read_text())
                if argv[3] in document.get("summary", ""):
                    path.rename(self.root / "history" / path.name)
        if argv[2] == "dndState":
            return subprocess.CompletedProcess(argv, 0, "off", "")
        return subprocess.CompletedProcess(argv, 0, self.answer, "")

    def dismissals(self):
        return [call for call in self.calls if call[0] == "dismiss"]


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "notifications"
        (self.root / "history").mkdir(parents=True)
        self.hub = Hub()
        self.shell = Shell(self.root)
        self.waiting = {"pair": {REQUEST}, "ssh": set()}
        self.clock = [1000.0]
        self.journal = []
        self.mirror = NotificationMirror(self.hub, state_dir=self.root, runner=self.shell,
                                         clock=lambda: self.clock[0],
                                         pending_requests=lambda: self.waiting,
                                         journal=self.journal.append)

    def tearDown(self):
        self.mirror.stop()
        self.temp.cleanup()

    def write(self, document, *, history=False):
        path = (self.root / "history" if history else self.root) / name_of(document)
        path.write_text(json.dumps(document))
        return path

    def events(self, kind):
        return [event for event in self.hub.events_since(0) if event.type == kind]

    def listed(self):
        return [row["id"] for row in self.mirror.history()["notifications"]]


class MarkerTests(unittest.TestCase):
    def test_a_pairing_request_is_known_by_its_app_and_the_plugin_summon(self):
        self.assertEqual(request_marker(pairing()), {"kind": "pairing_request", "request_id": REQUEST})
        self.assertEqual(project("1-4", pairing(), active=True)["kind"], "pairing_request")

    def test_an_ssh_request_is_known_by_its_title_and_device(self):
        self.assertEqual(request_marker(ssh_request()), {"kind": "ssh_request", "device_id": "ios-2b0aea5f-46fc"})

    def test_nothing_that_is_not_ours_is_ever_a_marker(self):
        theirs = [
            {**pairing(), "app": "omarchy-action"},           # someone else's app, our command
            {**pairing(), "execArgv": summon({"view": "devices", "request_id": REQUEST})
                .replace("com.omodachi.host", "com.example")},  # our name, another plugin
            {**pairing(), "execArgv": ""},                     # our name, no command
            {**pairing(), "execArgv": "[\"rm\",\"-rf\",\"/\"]"},
            {**pairing(), "execArgv": summon({"view": "devices", "request_id": "pair_x"})},
            {**pairing(), "execArgv": summon({"view": "overview", "request_id": REQUEST})},
            {**pairing(), "execArgv": summon({"view": "devices", "request_id": REQUEST, "more": 1})},
            {**ssh_request(), "summary": "Theme set"},
            {**ssh_request(), "body": "../../x · SHA256"},
            "not a document",
        ]
        for document in theirs:
            self.assertIsNone(request_marker(document), document)
        row = project("1-1", {**pairing(), "app": "Slack"}, active=True)
        self.assertNotIn("kind", row)


class WithdrawTests(Base):
    def test_post_then_approve_then_gone_from_the_desktop_the_list_and_history(self):
        path = self.write(pairing())
        self.mirror.scan()
        posted = self.events("notification.posted")
        self.assertEqual([event.payload["kind"] for event in posted], ["pairing_request"])
        self.assertEqual(self.listed(), ["1791102837949-4"])
        self.assertEqual(self.mirror.withdraw_stale(), [])
        self.assertEqual(self.shell.dismissals(), [], "a waiting request is left alone")

        self.waiting["pair"] = set()  # approved on the computer
        self.mirror.scan()
        updated = self.events("notification.updated")
        self.assertEqual(len(updated), 1)
        self.assertTrue(updated[0].payload["withdrawn"])
        self.assertFalse(updated[0].payload["active"])
        self.assertEqual(self.listed(), [], "withdrawn is not history: it leaves the list")
        self.assertIsNone(self.mirror.newest_active())

        self.assertEqual(self.mirror.withdraw_stale(), [("1791102837949-4", "dismissed")])
        self.assertEqual(self.shell.dismissals(),
                         [("dismiss", "“h6-iphone” via Tailscale wants to connect")])
        self.assertFalse(path.exists())
        archived = self.root / "history" / path.name
        self.assertTrue(archived.exists(), "the shell archived it")
        # The next pass removes our copy from the shell's history too.
        self.mirror.scan()
        self.assertEqual(self.mirror.withdraw_stale(), [("1791102837949-4", "history_removed")])
        self.assertFalse(archived.exists())
        self.mirror.scan()
        self.assertEqual(self.mirror._withdrawn, {})
        self.assertEqual(self.mirror.withdraw_stale(), [])
        self.assertEqual(len(self.events("notification.posted")), 1, "never posted twice")
        self.assertEqual(len(self.events("notification.updated")), 1)
        self.assertEqual([entry["event"] for entry in self.journal],
                         ["notification.withdrawn", "notification.withdraw", "notification.withdraw"])

    def test_denied_and_expired_and_replaced_are_all_decided(self):
        # A retry from the same device replaces the request: the old id is
        # simply not pending any more, like a rejection or an expiry.
        first = self.write(pairing(REQUEST, "rf2-ipad", 1791104362487, 9))
        self.mirror.scan()
        self.waiting["pair"] = set()
        self.mirror.scan()
        self.mirror.withdraw_stale()
        self.assertFalse(first.exists())
        self.assertEqual(self.listed(), [])

    def test_a_shell_that_does_not_answer_falls_back_to_removing_our_file(self):
        path = self.write(pairing())
        self.waiting["pair"] = set()
        self.shell.returncode = 1
        self.mirror.scan()
        self.assertEqual(self.mirror.withdraw_stale(), [("1791102837949-4", "file_removed")])
        self.assertFalse(path.exists())
        self.assertFalse((self.root / "history" / path.name).exists())

    def test_a_shell_without_that_toast_falls_back_too(self):
        path = self.write(pairing())
        self.waiting["pair"] = set()
        self.shell.answer = "none"
        self.mirror.scan()
        self.assertEqual(self.mirror.withdraw_stale(), [("1791102837949-4", "file_removed")])
        self.assertFalse(path.exists())

    def test_a_dismiss_that_did_not_move_the_file_is_followed_up_after_the_grace(self):
        path = self.write(pairing())
        self.waiting["pair"] = set()
        self.shell.moves = False
        self.mirror.scan()
        self.assertEqual(self.mirror.withdraw_stale(), [("1791102837949-4", "dismissed")])
        self.clock[0] += 1
        self.assertEqual(self.mirror.withdraw_stale(), [])
        self.assertTrue(path.exists())
        self.assertEqual(len(self.shell.dismissals()), 1, "asked once, not every pass")
        self.clock[0] += WITHDRAW_GRACE_SECONDS
        self.assertEqual(self.mirror.withdraw_stale(), [("1791102837949-4", "file_removed")])
        self.assertFalse(path.exists())

    def test_another_apps_toast_with_the_same_words_is_never_dismissed(self):
        ours = self.write(pairing())
        theirs = self.write({"id": 7, "app": "Slack", "summary": "Re: “h6-iphone” via Tailscale "
                             "wants to connect", "body": "", "urgency": 1, "timestamp": 1791102837999})
        self.waiting["pair"] = set()
        self.mirror.scan()
        self.assertEqual(self.mirror.withdraw_stale(), [("1791102837949-4", "file_removed")])
        self.assertEqual(self.shell.dismissals(), [], "the shell's dismiss matches by substring")
        self.assertFalse(ours.exists())
        self.assertTrue(theirs.exists())
        self.assertIn("1791102837999-7", self.listed())

    def test_the_same_device_asking_again_keeps_the_new_toast_and_takes_both_later(self):
        old = self.write(pairing(OTHER_REQUEST, "Web app · Chrome on Mac", 1791104210452, 7))
        new = self.write(pairing(REQUEST, "Web app · Chrome on Mac", 1791104322429, 8))
        self.waiting["pair"] = {REQUEST}
        self.mirror.scan()
        self.assertEqual(self.listed(), ["1791104322429-8"], "the replaced one leaves the list now")
        self.assertEqual(self.mirror.withdraw_stale(), [("1791104210452-7", "deferred")])
        self.assertEqual(self.shell.dismissals(), [], "a dismiss would take the waiting one too")
        self.assertTrue(old.exists() and new.exists())
        self.waiting["pair"] = set()
        self.mirror.scan()
        actions = self.mirror.withdraw_stale()
        self.assertEqual(sorted(actions), [("1791104210452-7", "dismissed"), ("1791104322429-8", "dismissed")])
        self.assertEqual(len(self.shell.dismissals()), 1, "one dismiss, both toasts")
        self.assertFalse(old.exists() or new.exists())

    def test_an_ssh_request_goes_when_its_key_is_decided(self):
        path = self.write(ssh_request())
        self.waiting["ssh"] = {"ios-2b0aea5f-46fc"}
        self.mirror.scan()
        self.assertEqual(self.mirror.withdraw_stale(), [])
        self.assertEqual(self.events("notification.posted")[0].payload["kind"], "ssh_request")
        self.waiting["ssh"] = set()
        self.mirror.scan()
        self.assertEqual(self.mirror.withdraw_stale(), [("1791103408030-5", "dismissed")])
        self.assertFalse(path.exists())

    def test_when_core_cannot_tell_nothing_is_withdrawn(self):
        path = self.write(pairing())
        self.waiting = None
        self.mirror.pending_requests = lambda: None
        self.mirror.scan()
        self.assertEqual(self.mirror.withdraw_stale(), [])
        self.assertTrue(path.exists())
        self.assertEqual(self.listed(), ["1791102837949-4"])
        def broken():
            raise OSError("pairing.json unreadable")
        self.mirror.pending_requests = broken
        self.mirror.scan()
        self.assertEqual(self.mirror.withdraw_stale(), [])
        self.assertTrue(path.exists())

    def test_a_mirror_without_a_source_withdraws_nothing(self):
        mirror = NotificationMirror(self.hub, state_dir=self.root, runner=self.shell)
        path = self.write(pairing())
        mirror.scan()
        self.assertEqual(mirror.withdraw_stale(), [])
        self.assertTrue(path.exists())

    def test_a_file_that_stopped_being_ours_is_not_touched(self):
        path = self.write(pairing())
        self.waiting["pair"] = set()
        self.mirror.scan()
        # Between the pass and the withdrawal the name now holds somebody
        # else's notification (the shell reuses ids after a restart).
        path.write_text(json.dumps({**pairing(), "app": "Slack", "execArgv": ""}))
        self.assertEqual(self.mirror.withdraw_stale(), [])
        self.assertTrue(path.exists())
        self.assertEqual(self.shell.dismissals(), [])


class StartSweepTests(Base):
    def test_starting_sweeps_stale_ones_on_screen_and_in_history_and_nothing_else(self):
        """Leo's host on 2026-10-05: five of ours, long decided, beside other apps'."""
        stale = [self.write(pairing("pair_" + str(index) * 32, f"t{index}-ipad", 1791102837949 + index, index))
                 for index in range(1, 4)]
        old = [self.write(pairing("pair_" + str(index) * 32, f"h{index}-ipad", 1791100000000 + index, index),
                          history=True) for index in range(5, 7)]
        crash = self.write({"id": 11, "app": "omarchy-action", "summary": "Process crashed: voxtype",
                            "body": "", "execArgv": "[\"omarchy-launch\",\"x\"]", "urgency": 2,
                            "timestamp": 1791105193259}, history=True)
        kitty = self.write({"id": 13, "app": "kitty", "summary": "done", "body": "", "urgency": 1,
                            "timestamp": 1791122122828})
        self.waiting["pair"] = set()
        self.mirror.poll_seconds = 0.05
        self.mirror.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(path.exists() for path in stale + old):
            time.sleep(0.05)
        self.mirror.stop()
        self.assertFalse(any(path.exists() for path in stale), "taken off the desktop")
        self.assertFalse(any(path.exists() for path in old), "and out of the shell's history")
        self.assertTrue(crash.exists() and kitty.exists(), "other apps' notifications untouched")
        self.assertEqual(sorted(self.listed()), ["1791105193259-11", "1791122122828-13"])
        self.assertEqual(sorted(event.payload["app"] for event in self.events("notification.posted")),
                         ["kitty", "omarchy-action"], "a stale one is never announced to a client at all")
        self.assertEqual(len(self.shell.dismissals()), 3)


class CoreTests(unittest.TestCase):
    """The real pairing store and service decide what is still waiting."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "notifications"
        (self.root / "history").mkdir(parents=True)
        self.auth = DeviceAuthenticator.from_file(base / "device.secret")
        self.service = create_service(Hub(authenticator=self.auth), demo=True)
        self.store = PairingStore(self.auth, base / "pairing.json")
        self.shell = Shell(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_pending_ids_are_only_the_ones_still_waiting(self):
        self.assertEqual(self.store.pending_request_ids(), set(), "no file yet is nothing pending")
        waiting = self.store.request(device_id="ipad", device_name="iPad")
        approved = self.store.request(device_id="phone", device_name="Phone")
        denied = self.store.request(device_id="mac", device_name="Mac")
        self.store.decide(approved["request_id"], approve=True)
        self.store.decide(denied["request_id"], approve=False)
        self.assertEqual(self.store.pending_request_ids(), {waiting["request_id"]})
        raw = json.loads(self.store.path.read_text())
        raw["requests"][waiting["request_id"]]["expires_at"] = int(time.time()) - 1
        self.store.path.write_text(json.dumps(raw))
        self.assertEqual(self.store.pending_request_ids(), set(), "expired is decided")
        self.store.path.write_text("{not json")
        self.assertIsNone(self.store.pending_request_ids(), "unreadable is 'cannot tell'")

    def test_the_service_answers_for_pairing_and_ssh(self):
        self.assertIsNone(self.service.pending_request_markers(), "no pairing store, no answer")
        self.service.pairing = self.store
        request = self.store.request(device_id="ipad", device_name="iPad")
        self.service._ssh_pending["phone"] = {"public_key": "k", "fingerprint": "f", "expires_at": time.time() + 60}
        self.service._ssh_pending["old"] = {"public_key": "k", "fingerprint": "f", "expires_at": time.time() - 1}
        self.assertEqual(self.service.pending_request_markers(),
                         {"pair": {request["request_id"]}, "ssh": {"phone"}})

    def test_approving_a_real_request_withdraws_its_notification(self):
        self.service.pairing = self.store
        hub = Hub()
        mirror = NotificationMirror(hub, state_dir=self.root, runner=self.shell,
                                    pending_requests=self.service.pending_request_markers)
        request = self.store.request(device_id="ipad", device_name="iPad")
        path = self.root / "1791102837949-4.json"
        path.write_text(json.dumps(pairing(request["request_id"], "iPad")))
        mirror.scan()
        self.assertEqual(mirror.withdraw_stale(), [])
        self.assertEqual([row["kind"] for row in mirror.history()["notifications"]], ["pairing_request"])
        self.store.decide(request["request_id"], approve=True)
        mirror.scan()
        self.assertEqual(mirror.withdraw_stale(), [("1791102837949-4", "dismissed")])
        self.assertEqual(mirror.history()["notifications"], [])
        self.assertFalse(path.exists())

    def test_revoking_the_device_withdraws_it_too(self):
        self.service.pairing = self.store
        mirror = NotificationMirror(Hub(), state_dir=self.root, runner=self.shell,
                                    pending_requests=self.service.pending_request_markers)
        request = self.store.request(device_id="ipad", device_name="iPad")
        path = self.root / "1791102837949-4.json"
        path.write_text(json.dumps(pairing(request["request_id"], "iPad")))
        mirror.scan()
        self.store.forget_device("ipad")
        mirror.scan()
        mirror.withdraw_stale()
        self.assertFalse(path.exists())


class PushTests(unittest.TestCase):
    def test_a_withdrawn_request_takes_its_push_off_the_phone(self):
        hub = Hub()
        watcher = Watcher(started_at=0)
        row = {**project("1791102837949-4", pairing(), active=False), "withdrawn": True}
        hub.publish("notification.updated", row)
        signals = watcher.signals(hub.events_since(0)[-1], hub)
        self.assertEqual(len(signals), 1)
        self.assertIsInstance(signals[0], Retract)
        self.assertEqual((signals[0].tag, signals[0].source), ("note-1791102837949-4", "note:Omodachi"))

    def test_a_toast_that_merely_left_the_screen_still_retracts_nothing(self):
        hub = Hub()
        hub.publish("notification.updated", project("1791102837949-4", pairing(), active=False))
        self.assertEqual(Watcher(started_at=0).signals(hub.events_since(0)[-1], hub), [])


if __name__ == "__main__":
    unittest.main()
