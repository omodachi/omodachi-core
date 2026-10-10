"""REMOTE-INPUT-1: text the Remote keyboard has no key for, typed with `wtype`.

Leo typed "y" on the iPad and the host showed "U+": the app sent the soft
keyboard's text as Moonlight's UTF-8 event, and the Sunshine fork types that as
Ctrl+Shift+U + hex, which fcitx5 does not finish. ASCII now goes as keys; what
is left comes here.
"""
from __future__ import annotations

import asyncio
import unittest

from omodachi_core.remote.errors import RemoteError
from omodachi_core.remote.hyprland import CommandResult, Hyprland
from omodachi_core.remote.service import RemoteService
from omodachi_core.remote.text import HostText, MAX_CHARS, validate_text


class Recorder:
    def __init__(self, code=0):
        self.code, self.calls = code, []

    def __call__(self, argv, *, env, timeout):
        self.calls.append((tuple(argv), dict(env), timeout))
        return self.code


def host_text(runner, *, executable="/usr/bin/wtype", environment=None):
    typed = HostText(runner=runner,
                     environment=environment or (lambda: {"WAYLAND_DISPLAY": "wayland-1",
                                                          "XDG_RUNTIME_DIR": "/run/user/1000"}),
                     which=lambda name: executable)
    typed.executable = lambda: executable
    return typed


class HostTextTests(unittest.TestCase):
    def test_the_text_is_one_argv_element_after_double_dash_and_never_echoed(self):
        runner = Recorder()
        answer = host_text(runner).type("你好 --help; rm -rf ~")
        argv, env, _ = runner.calls[0]
        self.assertEqual(argv, ("/usr/bin/wtype", "-k", "VoidSymbol", "--", "你好 --help; rm -rf ~"))
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-1")
        self.assertEqual(answer, {"typed": True, "chars": 19, "method": "wtype"})
        self.assertNotIn("你好", repr(answer))

    def test_emoji_and_accents_are_text(self):
        runner = Recorder()
        self.assertEqual(host_text(runner).type("é👍🏽")["chars"], 3)

    def test_control_characters_are_keys_and_are_refused_not_stripped(self):
        for value in ("\r", "a\r\nb", "\x1b", "\x7f", "\x85", "", None, 7, "\ud800"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(RemoteError) as caught:
                    validate_text(value)
                self.assertEqual((caught.exception.code, caught.exception.status), ("invalid_request", 400))

    def test_a_line_break_and_a_tab_are_typed_by_wtype_as_return_and_tab(self):
        # REMOTE-INPUT-2: a compose-field draft. `wtype` remaps `\n` to the
        # Return key and `\t` to Tab; nothing else is a key.
        runner = Recorder()
        answer = host_text(runner).type("ls -la\necho\tdone\n")
        self.assertEqual(runner.calls[0][0], ("/usr/bin/wtype", "-k", "VoidSymbol", "--", "ls -la\necho\tdone\n"))
        self.assertEqual(answer["chars"], 17)

    def test_too_long_is_refused_never_truncated(self):
        validate_text("x" * MAX_CHARS)
        with self.assertRaises(RemoteError):
            validate_text("x" * (MAX_CHARS + 1))
        with self.assertRaises(RemoteError):
            validate_text("你" * 1400)  # 1400 chars is over both limits

    def test_no_wtype_or_no_graphical_session_is_unavailable(self):
        with self.assertRaises(RemoteError) as caught:
            host_text(Recorder(), executable=None).type("你好")
        self.assertEqual((caught.exception.code, caught.exception.status), ("remote_text_unavailable", 503))
        with self.assertRaises(RemoteError) as caught:
            host_text(Recorder(), environment=lambda: {}).type("你好")
        self.assertEqual(caught.exception.code, "remote_text_unavailable")

    def test_a_failing_wtype_is_a_failure(self):
        with self.assertRaises(RemoteError) as caught:
            host_text(Recorder(code=1)).type("你好")
        self.assertEqual((caught.exception.code, caught.exception.status), ("remote_text_failed", 503))


class Hub:
    def __init__(self):
        self.state = {"remote": None, "capabilities": {}}

    def state_view(self, key):
        return {key: self.state.get(key)}

    def capabilities_snapshot(self):
        return dict(self.state["capabilities"])

    def update_state(self, patch, event_type=None):
        self.state.update(patch)

    def publish(self, *arguments, **keywords):
        pass


class Session:
    def __init__(self, device_id="ios-a", state="ready"):
        self.id, self.device_id, self.state = "rs_" + "0" * 32, device_id, state


class Layout:
    def __init__(self, reading=None, error=None):
        self.reading, self.error = reading, error

    def keyboard_layout(self):
        if self.error:
            raise self.error
        return self.reading


class Manager:
    def __init__(self, session=None, hyprland=None):
        self.session, self.hyprland, self.default_ttl = session, hyprland, 30.0

    def capabilities(self):
        return {"backends": {"sunshine": {"available": True, "reason": None},
                             "vnc": {"available": True, "reason": None}},
                "modes": ["takeover"], "placement_options": [],
                "lock_local_input_supported": True, "encoder_limits": None}

    def state_projection(self):
        return {"session_id": None, "state": "offline", "mode": None, "backend": None, "revision": 0}

    def current(self):
        return self.session if self.session is not None and self.session.state == "ready" else None

    def get(self, session_id):
        if self.session is None or self.session.id != session_id:
            raise RemoteError("session_not_found", 404)
        return self.session


class RemoteTextRouteTests(unittest.TestCase):
    def service(self, session, runner=None, hyprland=None):
        return RemoteService(Hub(), manager=Manager(session, hyprland),
                             host_text=host_text(runner or Recorder()))

    def test_the_owner_types_into_its_live_session(self):
        runner = Recorder()
        service = self.service(Session(), runner)
        answer = asyncio.run(service.text("ios-a", "rs_" + "0" * 32, {"text": "你好"}))
        self.assertEqual(answer, {"typed": True, "chars": 2, "method": "wtype"})
        self.assertEqual(runner.calls[0][0][-1], "你好")

    def test_another_device_a_missing_session_or_a_session_mid_transition_types_nothing(self):
        runner = Recorder()
        cases = [(Session(device_id="ios-b"), "rs_" + "0" * 32, "permission_denied"),
                 (Session(), "rs_" + "1" * 32, "session_not_found"),
                 (Session(state="resizing"), "rs_" + "0" * 32, "session_not_ready"),
                 (None, "rs_" + "0" * 32, "session_not_found")]
        for session, session_id, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(RemoteError) as caught:
                    asyncio.run(self.service(session, runner).text("ios-a", session_id, {"text": "你好"}))
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(runner.calls, [])

    def test_only_the_text_field_and_a_send_id_are_accepted(self):
        for payload in ({}, {"text": "a", "submit": True}, {"text": "a\r"}, [], {"text": ""}, {"id": "a" * 16},
                        {"text": "a", "id": "short"}, {"text": "a", "id": "has space in it"},
                        {"text": "a", "id": 12345678}, {"text": "a", "id": "x" * 65}):
            with self.subTest(payload=payload):
                with self.assertRaises(RemoteError) as caught:
                    asyncio.run(self.service(Session()).text("ios-a", "rs_" + "0" * 32, payload))
                self.assertEqual(caught.exception.code, "invalid_request")

    def test_capabilities_say_whether_text_works_and_which_keymap_keys_go_through(self):
        # REMOTE-INPUT-2: and the route's limits, and that it takes a draft.
        extra = {"max_chars": 1024, "max_bytes": 4096, "compose": True}
        service = self.service(None, hyprland=Layout({"layout": "us", "variant": ""}))
        value = asyncio.run(service.capabilities())
        self.assertEqual(value["text_input"], {"available": True, "layout": "us", "variant": None, **extra})
        service = self.service(None, hyprland=Layout({"layout": "de", "variant": "nodeadkeys"}))
        self.assertEqual(asyncio.run(service.capabilities())["text_input"],
                         {"available": True, "layout": "de", "variant": "nodeadkeys", **extra})
        service = self.service(None, hyprland=Layout(error=RemoteError("config_schema_invalid")))
        self.assertEqual(asyncio.run(service.capabilities())["text_input"],
                         {"available": True, "layout": None, "variant": None, **extra})
        service = RemoteService(Hub(), manager=Manager(None, Layout({"layout": "us", "variant": ""})),
                                host_text=host_text(Recorder(), executable=None))
        self.assertFalse(asyncio.run(service.capabilities())["text_input"]["available"])


class SlowRecorder(Recorder):
    """A `wtype` that takes a while, so a retry can arrive while it types."""

    def __init__(self, code=0, delay=0.2):
        super().__init__(code)
        self.delay = delay

    def __call__(self, argv, *, env, timeout):
        import time
        time.sleep(self.delay)
        return super().__call__(argv, env=env, timeout=timeout)


class SendOnceTests(unittest.TestCase):
    """REMOTE-INPUT-2: one press of Send is typed once, whatever the network does."""

    SID = "rs_" + "0" * 32

    def service(self, session, runner):
        return RemoteService(Hub(), manager=Manager(session), host_text=host_text(runner))

    def test_a_retry_with_the_same_id_types_nothing_and_answers_the_first_answer(self):
        runner = Recorder()
        service = self.service(Session(), runner)

        async def run():
            first = await service.text("ios-a", self.SID, {"text": "你好世界\n", "id": "send-0001"})
            again = await service.text("ios-a", self.SID, {"text": "你好世界\n", "id": "send-0001"})
            other = await service.text("ios-a", self.SID, {"text": "你好世界\n", "id": "send-0002"})
            return first, again, other

        first, again, other = asyncio.run(run())
        self.assertEqual(first, {"typed": True, "chars": 5, "method": "wtype"})
        self.assertEqual(again, {**first, "replayed": True})
        self.assertEqual(other, first)
        self.assertEqual(len(runner.calls), 2)

    def test_a_retry_while_the_first_is_still_typing_waits_for_it(self):
        runner = SlowRecorder()
        service = self.service(Session(), runner)

        async def run():
            payload = {"text": "Hello, World!", "id": "send-0003"}
            return await asyncio.gather(service.text("ios-a", self.SID, payload),
                                        service.text("ios-a", self.SID, payload))

        first, second = asyncio.run(run())
        self.assertEqual(len(runner.calls), 1)
        self.assertNotIn("replayed", first)
        self.assertTrue(second["replayed"])

    def test_a_request_that_gave_up_still_settles_its_id(self):
        # The app's request timed out (its reply lost on a reconnect); the
        # typing finishes anyway and the retry learns that it did.
        runner = SlowRecorder(delay=0.3)
        service = self.service(Session(), runner)

        async def run():
            payload = {"text": "youtube.com", "id": "send-0004"}
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(service.text("ios-a", self.SID, payload), 0.05)
            return await service.text("ios-a", self.SID, payload)

        again = asyncio.run(run())
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(again["replayed"])

    def test_the_same_id_with_other_text_is_refused(self):
        service = self.service(Session(), Recorder())

        async def run():
            await service.text("ios-a", self.SID, {"text": "a", "id": "send-0005"})
            await service.text("ios-a", self.SID, {"text": "b", "id": "send-0005"})

        with self.assertRaises(RemoteError) as caught:
            asyncio.run(run())
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_a_failed_wtype_spends_the_id_but_a_refusal_before_it_does_not(self):
        failing = Recorder(code=1)
        service = self.service(Session(), failing)

        async def failed_twice():
            for _ in range(2):
                with self.assertRaises(RemoteError) as caught:
                    await service.text("ios-a", self.SID, {"text": "a", "id": "send-0006"})
                self.assertEqual(caught.exception.code, "remote_text_failed")

        asyncio.run(failed_twice())
        self.assertEqual(len(failing.calls), 1)

        session = Session(state="resizing")
        runner = Recorder()
        service = self.service(session, runner)

        async def refused_then_typed():
            with self.assertRaises(RemoteError) as caught:
                await service.text("ios-a", self.SID, {"text": "a", "id": "send-0007"})
            self.assertEqual(caught.exception.code, "session_not_ready")
            session.state = "ready"
            return await service.text("ios-a", self.SID, {"text": "a", "id": "send-0007"})

        answer = asyncio.run(refused_then_typed())
        self.assertNotIn("replayed", answer)
        self.assertEqual(len(runner.calls), 1)

    def test_ids_are_per_device_and_session_and_another_device_cannot_type(self):
        runner = Recorder()
        service = self.service(Session(), runner)

        async def run():
            await service.text("ios-a", self.SID, {"text": "a", "id": "send-0008"})
            with self.assertRaises(RemoteError) as caught:
                await service.text("ios-b", self.SID, {"text": "a", "id": "send-0008"})
            return caught.exception.code

        self.assertEqual(asyncio.run(run()), "permission_denied")
        self.assertEqual(len(runner.calls), 1)

    def test_a_typed_send_answers_its_retry_after_the_session_ended(self):
        runner = Recorder()
        manager = Manager(Session())
        service = RemoteService(Hub(), manager=manager, host_text=host_text(runner))

        async def run():
            await service.text("ios-a", self.SID, {"text": "a", "id": "send-0009"})
            manager.session = None
            return await service.text("ios-a", self.SID, {"text": "a", "id": "send-0009"})

        self.assertTrue(asyncio.run(run())["replayed"])
        self.assertEqual(len(runner.calls), 1)

    def test_the_memory_is_bounded(self):
        from omodachi_core.remote.service import SEND_MEMORY
        service = self.service(Session(), Recorder())

        async def run():
            for index in range(SEND_MEMORY + 10):
                await service.text("ios-a", self.SID, {"text": "a", "id": f"send-{index:04d}"})

        asyncio.run(run())
        self.assertEqual(len(service._text_sends), SEND_MEMORY)


class KeyboardLayoutReadTests(unittest.TestCase):
    def test_the_layout_is_read_from_hyprland_getoption(self):
        asked = []

        def runner(argv):
            asked.append(argv[-1])
            value = {"input:kb_layout": '{"option": "input:kb_layout", "str": "us", "set": true}',
                     "input:kb_variant": '{"option": "input:kb_variant", "str": "", "set": true}'}[argv[-1]]
            return CommandResult(0, value)

        reading = Hyprland("a" * 40 + "_1_1", runner=runner).keyboard_layout()
        self.assertEqual(reading, {"layout": "us", "variant": ""})
        self.assertEqual(asked, ["input:kb_layout", "input:kb_variant"])

    def test_an_unreadable_answer_is_a_schema_error(self):
        hyprland = Hyprland("a" * 40 + "_1_1", runner=lambda argv: CommandResult(0, '{"int": 1}'))
        with self.assertRaises(RemoteError):
            hyprland.keyboard_layout()


if __name__ == "__main__":
    unittest.main()
