"""The theme reader over a recorded Omarchy theme tree, and its HTTP surface."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import aiohttp
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
from omodachi_core.theme import HostTheme, ThemeUnavailable, parse_shell_defaults

FIXTURES = Path(__file__).parents[1] / "contracts/fixtures/theme"
TEMPLATE = FIXTURES / "shell.toml.tpl"


def reader(root: Path) -> HostTheme:
    return HostTheme(root, shell_template=TEMPLATE)


class ThemeReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "current"
        shutil.copytree(FIXTURES, self.root, symlinks=True)
        self.addCleanup(self.temp.cleanup)

    def test_palette_comes_from_the_rendered_template_not_from_this_source(self):
        snapshot = reader(self.root).snapshot()
        self.assertEqual(snapshot["name"], "fixture-slate")
        self.assertEqual(snapshot["mode"], "dark")
        rendered = json.loads((self.root / "theme/omodachi-theme.json").read_text())
        for key, value in snapshot["colors"].items():
            self.assertEqual(value, rendered[key])
        self.assertEqual(len(snapshot["colors"]), 25)

    def test_missing_shell_tokens_are_filled_from_omarchy_own_template(self):
        shell = reader(self.root).shell()
        # The generated shell.toml leaves the whole type scale and the spacing
        # tokens commented out; the numbers still have to reach a client.
        self.assertEqual(shell["font"]["base-size"], 12)
        self.assertEqual(shell["font"]["heading"], 16)
        self.assertEqual(shell["font"]["display-large"], 28)
        self.assertEqual(shell["spacing"]["control-height"], 28)
        self.assertEqual(shell["spacing"]["row-padding-x"], 12)
        self.assertEqual(shell["spacing"]["dropdown-width"], 240)
        # A section the fixture's shell.toml never mentions still answers.
        self.assertEqual(shell["menu"]["scrim-alpha"], 0.5)
        self.assertEqual(shell["notifications"]["background-alpha"], 1.0)
        self.assertEqual(shell["bar"]["size-horizontal"], 26)
        self.assertEqual(shell["controls"]["selected-fill-alpha"], 0.18)

    def test_live_values_win_over_the_template_defaults(self):
        path = self.root / "theme/shell.toml"
        path.write_text(path.read_text().replace("size-horizontal  = 26", "size-horizontal  = 40"))
        self.assertEqual(reader(self.root).shell()["bar"]["size-horizontal"], 40)

    def test_hyprland_references_are_resolved_the_way_the_shell_resolves_them(self):
        shell = reader(self.root).shell()
        self.assertEqual(shell["popups"]["border"], shell["hyprland"]["active-border"])
        self.assertEqual(shell["menu"]["border"], shell["hyprland"]["active-border-foreground"])
        self.assertNotIn("hyprland.", json.dumps(shell))

    def test_colors_toml_answers_when_the_template_has_not_rendered_yet(self):
        (self.root / "theme/omodachi-theme.json").unlink()
        (self.root / "theme/colors.toml").write_text(
            "mode = \"light\"\n" + "\n".join(
                f'{key} = "#0000{index:02x}"' for index, key in enumerate(
                    ["accent", "selection", "muted", "background", "dark_background",
                     "darker_background", "lighter_background", "foreground", "bright_foreground",
                     "light_foreground", "dark_foreground", "red", "yellow", "orange", "green",
                     "cyan", "blue", "magenta", "brown", "bright_red", "bright_yellow",
                     "bright_green", "bright_cyan", "bright_blue", "bright_magenta"])) + "\n")
        snapshot = reader(self.root).snapshot()
        self.assertEqual(snapshot["mode"], "light")
        self.assertEqual(snapshot["colors"]["accent"], "#000000")

    def test_a_half_written_palette_is_refused_rather_than_partially_served(self):
        (self.root / "theme/omodachi-theme.json").write_text(json.dumps({"mode": "dark", "accent": "#112233"}))
        (self.root / "theme/colors.toml").write_text("mode = \"dark\"\n")
        with self.assertRaises(ThemeUnavailable):
            reader(self.root).snapshot()

    def test_background_reports_the_symlink_target_digest(self):
        snapshot = reader(self.root).snapshot()
        target = (self.root / "theme/backgrounds/fixture-wall.png").read_bytes()
        self.assertEqual(snapshot["background"]["sha256"], hashlib.sha256(target).hexdigest())
        self.assertEqual(snapshot["background"]["bytes"], len(target))
        self.assertEqual(snapshot["background"]["content_type"], "image/png")
        self.assertEqual(reader(self.root).background_path().name, "fixture-wall.png")

    def test_revision_only_moves_when_the_payload_moves(self):
        theme = reader(self.root)
        first = theme.snapshot()
        self.assertEqual(first["revision"], 1)
        self.assertEqual(theme.snapshot()["revision"], 1)
        path = self.root / "theme/omodachi-theme.json"
        value = json.loads(path.read_text())
        value["accent"] = "#ff0000"
        path.write_text(json.dumps(value))
        self.assertEqual(theme.snapshot()["revision"], 2)
        self.assertEqual(theme.snapshot()["colors"]["accent"], "#ff0000")

    def test_no_theme_at_all_is_reported_not_invented(self):
        shutil.rmtree(self.root)
        with self.assertRaises(ThemeUnavailable):
            reader(self.root).snapshot()

    def test_template_prose_never_becomes_a_design_token(self):
        defaults = parse_shell_defaults(TEMPLATE.read_text())
        for section, values in defaults.items():
            for key in values:
                self.assertRegex(key, r"^[a-z][a-z0-9-]*$", f"{section}.{key}")
        self.assertNotIn("derives", defaults.get("font", {}))
        # Colour placeholders are never defaults: the live file always has them.
        self.assertNotIn("background", defaults.get("bar", {}))


class ThemeApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "current"
        shutil.copytree(FIXTURES, self.root, symlinks=True)
        self.hub = Hub(auth_check_interval=0.05)
        self.token = self.hub.register_device("phone-a")
        self.service = create_service(self.hub, demo=True)
        self.service.theme = reader(self.root)
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))

    async def asyncTearDown(self):
        await self.client.close()
        await asyncio.wait_for(self.server.close(), 3)
        self.temp.cleanup()

    def headers(self):
        return {"Authorization": "Bearer " + self.token}

    async def test_theme_requires_a_credential_and_answers_the_whole_shape(self):
        async with self.client.get(self.url + "/v1/theme") as response:
            self.assertEqual(response.status, 401)
        async with self.client.get(self.url + "/v1/theme", headers=self.headers()) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()
        self.assertEqual(payload["contract_revision"], "omodachi.v1")
        self.assertEqual(payload["name"], "fixture-slate")
        for section in ("bar", "controls", "spacing", "font", "menu", "popups", "hyprland"):
            self.assertIn(section, payload["shell"])

    async def test_background_is_served_once_and_then_answered_with_304(self):
        async with self.client.get(self.url + "/v1/theme", headers=self.headers()) as response:
            digest = (await response.json())["background"]["sha256"]
        async with self.client.get(self.url + "/v1/theme/background", headers=self.headers()) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["ETag"], '"' + digest + '"')
            self.assertEqual(response.headers["Content-Type"], "image/png")
            body = await response.read()
        self.assertEqual(hashlib.sha256(body).hexdigest(), digest)
        headers = self.headers() | {"If-None-Match": '"' + digest + '"'}
        async with self.client.get(self.url + "/v1/theme/background", headers=headers) as response:
            self.assertEqual(response.status, 304)

    async def test_a_host_without_a_theme_reports_it_rather_than_guessing(self):
        self.service.theme = None
        async with self.client.get(self.url + "/v1/theme", headers=self.headers()) as response:
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())["error"]["code"], "theme_unavailable")

    async def test_theme_change_publishes_one_event_carrying_revision_and_name(self):
        async with self.client.ws_connect(self.url + "/v1/events", headers=self.headers()) as ws:
            await ws.receive_json()
            self.assertEqual(self.service.notify_theme_changed()["published"], True)
            path = self.root / "theme.name"
            path.write_text("fixture-dawn\n")
            result = self.service.notify_theme_changed()
            self.assertEqual(result["name"], "fixture-dawn")
            seen = []
            while len(seen) < 2:
                message = await asyncio.wait_for(ws.receive_json(), 2)
                if message.get("event", {}).get("type") == "theme.changed":
                    seen.append(message["event"]["payload"])
            self.assertEqual(seen[-1], {"revision": result["revision"], "name": "fixture-dawn"})
            # An unchanged host publishes nothing at all.
            self.assertEqual(self.service.notify_theme_changed()["published"], False)
