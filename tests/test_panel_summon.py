"""`panel.summon` and the view it recalls.

SPEC-C left `panel-summon` with no arguments, so a host shortcut bound to the
keybindings overlay could only ever reopen the iPad's root panel. The view now
travels with the recall.
"""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from omodachi_core.auth import DeviceAuthenticator
from omodachi_core.bootstrap import create_service
from omodachi_core.cli import host_main
from omodachi_core.hub import Hub
from omodachi_core.protocol import PANEL_VIEWS
from omodachi_core.service import ServiceError


def session(device="ipad-a"):
    return SimpleNamespace(id="rs_" + "1" * 32, device_id=device, revision=4, ttl_seconds=60.0)


class PanelSummonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.hub = Hub(authenticator=DeviceAuthenticator.from_file(root / "secret"))
        self.device = "ipad-a"
        self.hub.register_device(self.device)
        self.service = create_service(self.hub, demo=True)

    def summon(self, **params):
        return self.service.dispatch("panel.summon", params, self.device)

    def test_without_a_session_the_answer_names_the_view_and_opens_nothing(self):
        for view in PANEL_VIEWS:
            with self.subTest(view=view):
                self.assertEqual(self.summon(view=view),
                                 {"route": "local", "view": view, "opened": False})

    def test_the_default_is_still_the_root_panel(self):
        self.assertEqual(self.summon()["view"], "overview")

    def test_the_recall_to_the_owning_device_carries_the_view(self):
        live = session(self.device)
        self.service.remote.manager = SimpleNamespace(current=lambda: live)
        result = self.summon(view="keybindings")
        self.assertEqual(result["route"], "remote")
        self.assertEqual(result["view"], "keybindings")
        self.assertEqual((result["owner_device_id"], result["session_id"]), (self.device, live.id))
        event = [row for row in self.hub._events if row.type == "panel.summon"][-1]
        self.assertEqual(event.payload["view"], "keybindings")
        self.assertEqual(event.payload["session_id"], live.id)
        self.assertEqual(event.device_id, self.device)

    def test_an_unknown_view_is_refused_rather_than_silently_downgraded(self):
        self.service.remote.manager = SimpleNamespace(current=lambda: session(self.device))
        for view in ("home", "OVERVIEW", "", None, 1, ["overview"]):
            with self.subTest(view=view), self.assertRaises(ServiceError) as caught:
                self.summon(view=view)
            self.assertEqual(caught.exception.code, "invalid_request")
        self.assertEqual([row for row in self.hub._events if row.type == "panel.summon"], [])

    def test_settings_is_the_third_destination(self):
        """ARCH-1 / A-59: the Omodachi plugin's own bar widget summons it.

        The plugin used to send `overview` from a session and open its local
        QML panel from a right click, so there was no way at all to reach the
        app's own preferences from the picture. `settings` is a destination on
        the same recall, which is why it is one constant rather than a route.
        """
        self.assertIn("settings", PANEL_VIEWS)
        self.assertEqual(self.summon(view="settings"),
                         {"route": "local", "view": "settings", "opened": False})
        live = session(self.device)
        self.service.remote.manager = SimpleNamespace(current=lambda: live)
        result = self.summon(view="settings")
        self.assertEqual((result["route"], result["view"]), ("remote", "settings"))
        event = [row for row in self.hub._events if row.type == "panel.summon"][-1]
        self.assertEqual(event.payload["view"], "settings")
        self.assertEqual(event.device_id, self.device)

    def test_the_command_line_accepts_only_the_published_views(self):
        """`--view` is `choices=PANEL_VIEWS`, so the two lists cannot drift."""
        self.assertEqual(PANEL_VIEWS, ("overview", "keybindings", "settings"))
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
            host_main(["--token", "t", "panel-summon", "--view", "home"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
