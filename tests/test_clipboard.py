"""CLIP-1. The clipboard bridge: the preference, the limits, and the silence.

The tests that matter most here are the ones about what does *not* happen: no
content in the event, no content in the trace, no answer at all while the
preference is off, and our own write not coming back as news.
"""
import sys
import threading
import time
import unittest

import os

from omodachi_core.clipboard import (CLIPBOARD_MODES, ClipboardError, ClipboardService, LIMIT,
                                     WATCH_ARGV, WATCH_COMMAND)

#: A stand-in for `wl-paste --watch`: two empty lines, flushed as they are
#: written, then a long wait. A shell would buffer both newlines until it
#: exited, which would test the pipe rather than the loop.
WATCH_SCRIPT = (sys.executable, "-c",
                "import sys, time\n"
                "for _ in range(2):\n"
                "    sys.stdout.write('\\n'); sys.stdout.flush(); time.sleep(0.3)\n"
                "time.sleep(30)\n")


class FakeClipboard:
    """A `wl-copy`/`wl-paste` pair backed by one string, with a call log."""

    def __init__(self, content=b"", types=b"text/plain\n"):
        self.content, self.types, self.calls = content, types, []
        self.paste_code = 0
        self.copy_code = 0

    def __call__(self, argv, environment, *, stdin=None, cap=None, timeout=None):
        self.calls.append((tuple(argv), stdin))
        if argv[0].endswith("wl-copy"):
            if self.copy_code == 0:
                self.content = stdin
            return self.copy_code, b""
        if "--list-types" in argv:
            return (0, self.types) if self.types else (1, b"")
        if self.paste_code or not self.content:
            return (self.paste_code or 1), b""
        return 0, self.content


def service(mode="both", clipboard=None, **options):
    fake = clipboard or FakeClipboard(b"hello")
    published = []
    value = ClipboardService(mode=lambda: mode, environment=lambda: {"PATH": "/usr/bin"},
                             runner=fake, publish=lambda event, payload: published.append((event, payload)),
                             **options)
    return value, fake, published


class ThePreferenceTests(unittest.TestCase):
    def test_off_is_a_refusal_and_not_an_empty_clipboard(self):
        # A screen must be able to tell "nothing is copied" from "you may not
        # have this", so the off state answers with a code and no body.
        bridge, _, _ = service("off")
        for call in (bridge.read, lambda: bridge.write("x")):
            with self.assertRaises(ClipboardError) as caught:
                call()
            self.assertEqual(caught.exception.code, "clipboard_sync_disabled")
            self.assertEqual(caught.exception.status, 403)

    def test_host_to_device_reads_but_does_not_let_a_device_write(self):
        bridge, _, _ = service("host_to_device")
        self.assertEqual(bridge.read(), "hello")
        with self.assertRaises(ClipboardError) as caught:
            bridge.write("from the iPad")
        self.assertEqual(caught.exception.code, "clipboard_write_disabled")
        self.assertEqual(caught.exception.status, 403)

    def test_both_reads_and_writes(self):
        bridge, fake, _ = service("both")
        self.assertEqual(bridge.write("from the iPad"), {"bytes": 13, "mime": "text/plain"})
        self.assertEqual(fake.content, b"from the iPad")
        self.assertEqual(bridge.read(), "from the iPad")

    def test_an_unknown_mode_is_off(self):
        # A preference file written by a build that knows more than this one
        # must not read as permission.
        bridge, _, _ = service("everything")
        with self.assertRaises(ClipboardError):
            bridge.read()

    def test_a_preference_that_raises_is_off(self):
        def explode():
            raise RuntimeError("store unavailable")
        bridge = ClipboardService(mode=explode, environment=lambda: {}, runner=FakeClipboard(b"x"))
        with self.assertRaises(ClipboardError) as caught:
            bridge.read()
        self.assertEqual(caught.exception.code, "clipboard_sync_disabled")

    def test_the_three_modes_are_the_whole_vocabulary(self):
        self.assertEqual(CLIPBOARD_MODES, ("off", "host_to_device", "both"))


class TheContentTests(unittest.TestCase):
    def test_an_empty_clipboard_is_an_empty_string_not_a_failure(self):
        bridge, _, _ = service("both", FakeClipboard(b"", types=b""))
        self.assertEqual(bridge.read(), "")

    def test_a_clipboard_holding_only_an_image_says_so(self):
        bridge, _, _ = service("both", FakeClipboard(b"", types=b"image/png\n"))
        with self.assertRaises(ClipboardError) as caught:
            bridge.read()
        self.assertEqual(caught.exception.code, "clipboard_not_text")

    def test_text_that_is_not_utf8_is_not_text(self):
        bridge, _, _ = service("both", FakeClipboard(b"\xff\xfe\x00"))
        with self.assertRaises(ClipboardError) as caught:
            bridge.read()
        self.assertEqual(caught.exception.code, "clipboard_not_text")

    def test_the_limit_refuses_rather_than_truncates_in_both_directions(self):
        bridge, _, _ = service("both", FakeClipboard(b"a" * (LIMIT + 1)))
        with self.assertRaises(ClipboardError) as caught:
            bridge.read()
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ("clipboard_too_large", 413))
        with self.assertRaises(ClipboardError) as caught:
            bridge.write("b" * (LIMIT + 1))
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ("clipboard_too_large", 413))

    def test_exactly_the_limit_is_allowed(self):
        bridge, _, _ = service("both", FakeClipboard(b"a" * LIMIT))
        self.assertEqual(len(bridge.read()), LIMIT)

    def test_a_multibyte_string_is_measured_in_bytes(self):
        # 64 KiB is a transport limit, so it counts what goes on the wire.
        bridge, fake, _ = service("both", FakeClipboard(b"x"))
        self.assertEqual(bridge.write("剪贴板")["bytes"], 9)
        self.assertEqual(fake.content, "剪贴板".encode())

    def test_an_empty_write_is_refused(self):
        bridge, _, _ = service("both")
        with self.assertRaises(ClipboardError) as caught:
            bridge.write("")
        self.assertEqual(caught.exception.code, "clipboard_invalid")

    def test_a_write_that_is_not_a_string_is_refused_before_any_process_runs(self):
        bridge, fake, _ = service("both")
        for value in (None, 7, b"bytes", ["a"]):
            with self.assertRaises(ClipboardError):
                bridge.write(value)
        self.assertEqual(fake.calls, [])

    def test_a_tool_that_fails_reads_as_unavailable_not_as_empty(self):
        fake = FakeClipboard(b"hello")
        fake.copy_code = 1
        bridge, _, _ = service("both", fake)
        with self.assertRaises(ClipboardError) as caught:
            bridge.write("x")
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ("clipboard_unavailable", 503))

    def test_no_graphical_session_is_unavailable(self):
        bridge = ClipboardService(mode=lambda: "both", environment=lambda: {},
                                  runner=FakeClipboard(b"hello"))
        with self.assertRaises(ClipboardError) as caught:
            bridge.read()
        self.assertEqual(caught.exception.status, 503)


class TheEventTests(unittest.TestCase):
    def test_the_change_event_carries_a_length_and_never_the_text(self):
        bridge, _, published = service("host_to_device", FakeClipboard(b"a secret token"))
        payload = bridge.changed()
        self.assertEqual(payload, {"sequence": 1, "bytes": 14, "mime": "text/plain"})
        self.assertEqual(published, [("clipboard.changed", payload)])
        self.assertNotIn("a secret token", repr(published))

    def test_the_trace_records_direction_and_length_and_never_the_text(self):
        bridge, _, _ = service("both", FakeClipboard(b"a secret token"))
        bridge.read()
        bridge.write("another secret")
        self.assertEqual([row["direction"] for row in bridge.trace],
                         ["host_to_device", "device_to_host"])
        self.assertEqual([row["bytes"] for row in bridge.trace], [14, 14])
        self.assertEqual(set().union(*(set(row) for row in bridge.trace)),
                         {"direction", "bytes", "ts"})
        self.assertNotIn("secret", repr(bridge.trace))

    def test_the_trace_is_bounded(self):
        bridge, _, _ = service("host_to_device", FakeClipboard(b"x"))
        for _ in range(200):
            bridge.read()
        self.assertEqual(len(bridge.trace), 32)

    def test_nothing_is_published_while_the_preference_is_off(self):
        bridge, _, published = service("off", FakeClipboard(b"hello"))
        self.assertIsNone(bridge.changed())
        self.assertEqual(published, [])

    def test_our_own_write_does_not_come_back_as_a_change(self):
        # `wl-copy` moves the selection, so the watcher fires for our own write.
        # Announcing it would make a device that pasted into the host see its
        # own paste arrive back as news from the host.
        bridge, _, published = service("both", FakeClipboard(b"x"))
        bridge.write("from the iPad")
        self.assertIsNone(bridge.changed())
        self.assertEqual(published, [])

    def test_a_host_copy_after_our_write_is_still_news(self):
        fake = FakeClipboard(b"x")
        bridge, _, published = service("both", fake)
        bridge.write("from the iPad")
        self.assertIsNone(bridge.changed())
        fake.content = b"copied on the desktop"
        bridge._last_event = -float("inf")
        self.assertIsNotNone(bridge.changed())
        self.assertEqual(len(published), 1)

    def test_the_throttle_coalesces_a_burst_and_the_flush_ends_it(self):
        ticks = [0.0]
        bridge, fake, published = service("host_to_device", FakeClipboard(b"one"),
                                          clock=lambda: ticks[0], throttle=0.5)
        self.assertIsNotNone(bridge.changed())
        for value in (b"two", b"three", b"four"):
            fake.content = value
            ticks[0] += 0.1
            self.assertIsNone(bridge.changed())
        self.assertEqual(len(published), 1)
        ticks[0] += 1.0
        self.assertIsNotNone(bridge.flush())
        self.assertEqual(len(published), 2)
        self.assertEqual(published[-1][1]["bytes"], 4)

    def test_flush_says_nothing_when_nothing_was_swallowed(self):
        bridge, _, published = service("host_to_device", FakeClipboard(b"one"))
        bridge.changed()
        self.assertIsNone(bridge.flush())
        self.assertEqual(len(published), 1)

    def test_an_empty_clipboard_is_not_announced(self):
        bridge, _, published = service("host_to_device", FakeClipboard(b"", types=b""))
        self.assertIsNone(bridge.changed())
        self.assertEqual(published, [])

    def test_the_sequence_counts_announcements_so_a_gap_is_visible(self):
        fake = FakeClipboard(b"one")
        bridge, _, published = service("host_to_device", fake, throttle=0)
        for value in (b"one", b"two", b"three"):
            fake.content = value
            bridge.changed()
        self.assertEqual([payload["sequence"] for _, payload in published], [1, 2, 3])


class TheWatcherTests(unittest.TestCase):
    def test_the_watcher_argv_never_asks_for_the_content(self):
        # `--watch <command>` runs the command with the clipboard on its stdin.
        # Ours reads nothing and prints one empty line, so the watcher pipe
        # carries a signal and not a copy of everything the user ever copies.
        self.assertEqual(WATCH_COMMAND,
                         ("/usr/bin/wl-paste", "--type", "text/plain", "--watch", "/bin/echo"))
        self.assertEqual(WATCH_ARGV[-len(WATCH_COMMAND):], WATCH_COMMAND)

    def test_the_watcher_dies_with_the_daemon_where_the_kernel_can_be_asked(self):
        # The host walk killed a daemon rather than shutting it down, and the
        # watcher outlived it. `stop()` cannot help there; the kernel can.
        prefix = WATCH_ARGV[:-len(WATCH_COMMAND)]
        if prefix:
            self.assertEqual(prefix, ("/usr/bin/setpriv", "--pdeathsig", "TERM"))
        else:
            self.assertFalse(os.access("/usr/bin/setpriv", os.X_OK),
                             "setpriv is here; the watcher should be asking it")

    def test_apply_starts_the_watcher_only_when_the_preference_allows_it(self):
        started = threading.Event()
        mode = ["off"]
        bridge = ClipboardService(mode=lambda: mode[0], environment=lambda: {"PATH": "/usr/bin"},
                                  runner=FakeClipboard(b"x"), watch_argv=("/bin/sleep", "30"),
                                  restart_delay=0.05)
        try:
            self.assertFalse(bridge.apply())
            self.assertIsNone(bridge._thread)
            for mode[0] in ("host_to_device", "both"):
                self.assertTrue(bridge.apply())
                self.assertTrue(bridge._thread.is_alive())
                started.set()
            mode[0] = "off"
            self.assertFalse(bridge.apply())
            self.assertIsNone(bridge._thread)
        finally:
            bridge.stop()
        self.assertTrue(started.is_set())

    def test_a_real_watcher_process_turns_one_line_into_one_event(self):
        # The loop end to end, with a stand-in for `wl-paste --watch` that
        # prints the two empty lines a real one would.
        fake = FakeClipboard(b"first")
        published = []
        bridge = ClipboardService(
            mode=lambda: "host_to_device", environment=lambda: {"PATH": "/usr/bin"}, runner=fake,
            publish=lambda event, payload: published.append(payload), throttle=0,
            restart_delay=30, watch_argv=WATCH_SCRIPT)
        bridge.start()
        try:
            deadline = time.monotonic() + 10
            while len(published) < 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(len(published), 1)
            fake.content = b"second"
            while len(published) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            bridge.stop()
        self.assertEqual([row["bytes"] for row in published], [5, 6])
        self.assertIsNone(bridge._thread)

    def test_stop_is_safe_when_nothing_was_started(self):
        bridge, _, _ = service("off")
        bridge.stop()
        bridge.stop()
        self.assertIsNone(bridge._thread)


if __name__ == "__main__":
    unittest.main()


class TheServiceWiringTests(unittest.TestCase):
    """What the daemon does with the bridge: the switch, and the honest None."""

    def setUp(self):
        from omodachi_core.bootstrap import create_service
        from omodachi_core.hub import Hub
        self.hub = Hub()
        self.service = create_service(self.hub, demo=True)

    def test_a_host_without_a_bridge_refuses_rather_than_answering_empty(self):
        self.assertIsNone(self.service.clipboard)
        for call in (self.service.clipboard_read, lambda: self.service.clipboard_write("x")):
            with self.assertRaises(ClipboardError) as caught:
                call()
            self.assertEqual((caught.exception.code, caught.exception.status),
                             ("clipboard_unavailable", 503))

    def test_the_runtime_flag_says_unsupported_rather_than_off(self):
        # They are different answers: a plugin that cannot tell them apart
        # draws a switch that looks off when there is no switch at all.
        self.assertFalse(self.service.preferences_snapshot()["runtime"]["clipboard_supported"])
        self.service.clipboard = ClipboardService(mode=lambda: "off")
        self.assertTrue(self.service.preferences_snapshot()["runtime"]["clipboard_supported"])

    def test_writing_the_preference_moves_the_watcher_there_and_then(self):
        applied = []
        class Recording(ClipboardService):
            def apply(self, mode=None):
                applied.append(self._mode())
                return True
        self.service.clipboard = Recording(
            mode=lambda: self.service.preferences_store.get()["values"]["clipboard_sync"])
        revision = self.service.preferences_store.get()["revision"]
        self.service._dispatch_local("local.preferences.set",
                                     {"expected_revision": revision,
                                      "changes": {"clipboard_sync": "both"}}, lambda: True)
        self.assertEqual(applied, ["both"])

    def test_a_watcher_that_cannot_start_does_not_fail_the_write(self):
        class Broken(ClipboardService):
            def apply(self, mode=None):
                raise OSError("no session")
        self.service.clipboard = Broken(mode=lambda: "both")
        self.assertFalse(self.service.apply_clipboard_preference())


class TheRepeatTests(unittest.TestCase):
    """The same text twice in a row is not news, whoever put it there."""

    def test_the_desktop_copying_the_same_text_twice_announces_once(self):
        fake = FakeClipboard(b"one")
        bridge, _, published = service("host_to_device", fake, throttle=0)
        self.assertIsNotNone(bridge.changed())
        self.assertIsNone(bridge.changed())
        fake.content = b"two"
        self.assertIsNotNone(bridge.changed())
        fake.content = b"one"
        self.assertIsNotNone(bridge.changed())
        self.assertEqual([payload["bytes"] for _, payload in published], [3, 3, 3])
        self.assertEqual([payload["sequence"] for _, payload in published], [1, 2, 3])
