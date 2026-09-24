"""INSTALL-1 §1.3: "Remote is unavailable" has to be about now, not about boot.

The daemon builds its Remote manager once, at startup, and startup is
`WantedBy=default.target` - which on a fresh install is before the user has
ever logged into Hyprland. `build_manager()` then raises, and until this change
`self.manager` stayed None for the life of the process: every Remote request
answered `remote_runtime_unavailable` even after the desktop came up. That is
the 2026-09-20 host incident, and a clean install reaches it by definition,
because the installer starts the daemon itself.
"""
from __future__ import annotations

import asyncio
import unittest

from omodachi_core.remote.errors import RemoteError
from omodachi_core.remote.service import RemoteService


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


class Manager:
    """The smallest manager RemoteService reads at this level."""

    def __init__(self):
        self.default_ttl = 30.0

    def capabilities(self):
        return {"backends": {"sunshine": {"available": True, "reason": None},
                             "vnc": {"available": True, "reason": None}},
                "modes": ["takeover"], "placement_options": [],
                "lock_local_input_supported": True, "encoder_limits": None}

    def state_projection(self):
        return {"session_id": None, "state": "offline", "mode": None, "backend": None,
                "revision": 0}

    def current(self):
        return None


class Clock:
    """A loop-time stand-in, so the cooldown is tested without sleeping."""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now


def run(coroutine, *, clock=None):
    async def main():
        if clock is not None:
            loop = asyncio.get_running_loop()
            loop.time = clock.time
        return await coroutine()
    return asyncio.run(main())


class ManagerRetryTests(unittest.TestCase):
    def test_a_daemon_that_started_before_the_desktop_gets_remote_when_it_arrives(self):
        hub, clock = Hub(), Clock()
        attempts = []

        def factory():
            attempts.append(clock.now)
            if len(attempts) < 3:
                raise RemoteError("graphical_session_unavailable")
            return Manager()

        service = RemoteService(hub, manager=None, manager_factory=factory)

        async def exercise():
            first = await service.refresh_capabilities()
            self.assertFalse(first["backends"]["sunshine"]["available"])
            self.assertFalse(hub.state["capabilities"]["desktop"])
            self.assertEqual(first["backends"]["sunshine"]["reason"], "remote_runtime_unavailable")
            self.assertEqual(service.last_manager_error, "graphical_session_unavailable")
            # Inside the cooldown nothing is retried, however often it is asked.
            for _ in range(5):
                await service.refresh_capabilities()
            self.assertEqual(len(attempts), 1)
            clock.now += service.manager_retry_interval + 0.1
            await service.refresh_capabilities()
            self.assertEqual(len(attempts), 2)
            clock.now += service.manager_retry_interval + 0.1
            third = await service.refresh_capabilities()
            self.assertEqual(len(attempts), 3)
            self.assertTrue(third["backends"]["sunshine"]["available"])
            self.assertIsNone(service.last_manager_error)
            # The same call publishes it, which is what takes "no Remote" off
            # the panel without anybody pressing anything.
            self.assertTrue(hub.state["capabilities"]["desktop"])
            # And it stops trying once it has one.
            clock.now += 1000
            await service.refresh_capabilities()
            self.assertEqual(len(attempts), 3)
        run(lambda: exercise(), clock=clock)

    def test_the_manager_a_retry_builds_is_the_one_every_later_request_uses(self):
        hub, clock = Hub(), Clock()
        built = Manager()
        service = RemoteService(hub, manager=None, manager_factory=lambda: built)

        async def exercise():
            self.assertIs(await service.ensure_manager(), built)
            self.assertIs(service.manager, built)
            self.assertEqual(hub.state["remote"]["state"], "offline")
        run(lambda: exercise(), clock=clock)

    def test_a_manager_built_after_the_transport_is_wired_for_events_and_recovered(self):
        # REMOTE-SAFE-1 §5 finding, seen on the clean VM after a reboot: the
        # lazily built manager's sessions never reached state.remote, which
        # stayed `offline` through a live Extend session.
        hub = Hub()
        live = {"session_id": "rs_1", "state": "ready", "mode": "extend", "backend": "vnc", "revision": 2}

        class Live(Manager):
            events = None
            recovered = 0

            class hyprland:
                @staticmethod
                def event_socket():
                    return "/nonexistent/omodachi-test.sock"

            def maintain(self):
                return False

            def watch_shell(self):
                return None

            def recover(self):
                self.recovered += 1

            def state_projection(self):
                return dict(live)

        built = Live()
        service = RemoteService(hub, manager=None, manager_factory=lambda: built)

        async def exercise():
            service.refresh_capabilities = lambda: asyncio.sleep(0)
            await service.attach_transport()
            self.assertIs(await service.ensure_manager(), built)
            self.assertEqual(built.events, service._emit)
            self.assertEqual(built.recovered, 1)
            self.assertEqual(hub.state["remote"], live)
            self.assertIsNotNone(service._display_watch, "the compositor event stream is followed too")
            await service.detach_transport()
        run(lambda: exercise())

    def test_a_daemon_with_no_factory_at_all_still_answers_unavailable(self):
        # Demo mode, and every test that constructs the service by hand.
        hub = Hub()
        service = RemoteService(hub, manager=None)

        async def exercise():
            self.assertIsNone(await service.ensure_manager())
            with self.assertRaises(RemoteError) as caught:
                service._require()
            self.assertEqual(caught.exception.code, "remote_runtime_unavailable")
        run(lambda: exercise())

    def test_a_factory_that_raises_anything_is_survived_and_named(self):
        hub, clock = Hub(), Clock()

        def factory():
            raise ValueError("hyprctl is not on PATH")
        service = RemoteService(hub, manager=None, manager_factory=factory)

        async def exercise():
            self.assertIsNone(await service.ensure_manager())
            self.assertEqual(service.last_manager_error, "ValueError")
            self.assertIsNone(service.manager)
        run(lambda: exercise(), clock=clock)

    def test_a_closed_service_stops_trying(self):
        hub, clock = Hub(), Clock()
        attempts = []

        def factory():
            attempts.append(1)
            return Manager()
        service = RemoteService(hub, manager=None, manager_factory=factory)
        service._closed = True

        async def exercise():
            self.assertIsNone(await service.ensure_manager())
            self.assertEqual(attempts, [])
        run(lambda: exercise(), clock=clock)


class BootstrapWiringTests(unittest.TestCase):
    def test_a_manager_built_later_arrives_wired_like_one_built_at_startup(self):
        # The host answers - the user's quality, backend and resize
        # preferences - used to be attached inline, once, to whatever manager
        # the daemon happened to have. A manager built on the retry path has to
        # get the same treatment or every session it plans ignores the user's
        # settings, silently.
        from omodachi_core.bootstrap import create_service
        from omodachi_core.hub import Hub as RealHub
        from omodachi_core.remote.session import RemoteManager
        built = []

        def factory():
            manager = _StubManager()
            built.append(manager)
            return manager

        hub = RealHub()
        service = create_service(hub, demo=True, remote_manager_factory=factory)
        made = service.remote._manager_factory()
        self.assertIs(made, built[0])
        self.assertIsNotNone(made.allow_resize)
        self.assertIsNotNone(made.host_quality)
        self.assertIsNotNone(made.host_backend)
        self.assertIsNotNone(made.backends["sunshine"].certificate_resolver)


class BootstrapBarGeometryTests(unittest.TestCase):
    def test_the_bar_is_measured_for_a_manager_built_after_startup(self):
        # REMOTE-SAFE-1 §5 finding: `refresh_bar` measured the bar only for the
        # manager the daemon had at startup - None after every reboot - so a
        # session on a lazily built manager never got a logo rectangle.
        from pathlib import Path
        from omodachi_core.bootstrap import create_service
        from omodachi_core.hub import Hub as RealHub
        service = create_service(RealHub(), demo=False, enable_live_menu=False, enable_catalog_providers=False,
                                 default_menu=Path("src/omodachi_core/data/demo-menu.jsonc"),
                                 omodachi_menu=Path("src/omodachi_core/data/omodachi-menu.jsonc"),
                                 shell_config=Path("src/omodachi_core/data/demo-shell.json"))
        self.assertIsNone(service.remote.manager)
        seen = []
        session = object()

        class Later:
            def current(self):
                return session
        service.bar_geometry.snapshot = lambda current, position, sections: seen.append(current)
        service.refresh_bar()
        self.assertEqual(seen, [], "no manager, no measurement")
        service.remote.manager = Later()
        service.refresh_bar()
        self.assertEqual(seen, [session])


class _StubManager:
    def __init__(self):
        self.allow_resize = None
        self.host_quality = None
        self.host_backend = None
        self.device_names = None
        self.backends = {"sunshine": _StubBackend()}


class _StubBackend:
    def __init__(self):
        self.certificate_resolver = None


if __name__ == "__main__":
    unittest.main()
