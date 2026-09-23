"""Host font discovery and the two file downloads it describes."""
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
from omodachi_core.fonts import FALLBACK_PROBES, FontsUnavailable, HostFonts, parse_fc_list
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer

FIXTURES = Path(__file__).parents[1] / "contracts/fixtures/fonts"
FIXTURE_FILES = ("FixtureMono-Regular.ttf", "FixtureMono-Bold.ttf", "omarchy.ttf",
                 "FixtureSymbols-Regular.ttf", "FixtureCJKMono-Regular.ttc",
                 "FixtureEmoji-Regular.ttf")


def fontconfig(root: Path, *, family="FixtureMono Nerd Font", probes=None, fail=False):
    """A fake fontconfig: the recorded `fc-list` listing plus `fc-fallback.json`.

    `fc-match` always answers — that is what it is for — so the fake answers the
    same way, and the `covers` flag is what `fc-list :charset=` would really say.
    """
    listing = (FIXTURES / "fc-list.txt").read_text().replace(
        "contracts/fixtures/fonts/", str(root) + "/")
    table = probes if probes is not None else json.loads(
        (FIXTURES / "fc-fallback.json").read_text())["probes"]

    def runner(argv):
        if fail:
            raise FontsUnavailable("font_probe_unavailable")
        if argv[0].endswith("omarchy-font-current"):
            return family + "\n"
        if argv[0].endswith("fc-match"):
            answer = table.get(argv[1].removeprefix("monospace:charset="))
            if answer is None:
                return f"{family}\t{root}/FixtureMono-Regular.ttf\n"
            return f"{answer['family']}\t{root / answer['file']}\n"
        if len(argv) == 3 and argv[1].startswith(":charset="):
            answer = table.get(argv[1].removeprefix(":charset="))
            if answer is None or not answer["covers"]:
                return ""
            return f"{root / answer['file']}: \n"
        return listing

    return runner


def fonts(root: Path, *, family="FixtureMono Nerd Font", probes=None, fail=False) -> HostFonts:
    return HostFonts(icon_font=root / "omarchy.ttf",
                     runner=fontconfig(root, family=family, probes=probes, fail=fail))


class FcListTests(unittest.TestCase):
    def test_both_family_aliases_of_a_nerd_font_match(self):
        text = (FIXTURES / "fc-list.txt").read_text()
        for name in ("FixtureMono Nerd Font", "FixtureMono NF", "fixturemono nf"):
            self.assertEqual(set(parse_fc_list(text, name)), {"Italic", "Regular", "Bold"})

    def test_another_family_on_the_host_is_never_picked_up(self):
        found = parse_fc_list((FIXTURES / "fc-list.txt").read_text(), "FixtureMono Nerd Font")
        self.assertNotIn("Other-Regular.ttf", str(found))
        self.assertTrue(str(found["Regular"]).endswith("FixtureMono-Regular.ttf"))


class HostFontsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in FIXTURE_FILES:
            shutil.copy(FIXTURES / name, self.root / name)
        self.addCleanup(self.temp.cleanup)

    def test_every_row_carries_a_real_digest(self):
        rows = fonts(self.root).listing()
        self.assertEqual([row["id"] for row in rows][:3], ["mono-regular", "mono-bold", "icons"])
        self.assertEqual([row["role"] for row in rows][:3], ["mono", "mono", "icons"])
        self.assertEqual(rows[0]["family"], "FixtureMono Nerd Font")
        for row in rows:
            raw = Path(row["path"]).read_bytes()
            self.assertEqual(row["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(row["bytes"], len(raw))

    def test_the_chain_is_what_fontconfig_answers_per_code_point(self):
        document = fonts(self.root).snapshot()
        chain = document["fallback_chain"]
        self.assertEqual([(link["coverage"], link["family"], link["font"]) for link in chain], [
            ("symbols", "FixtureSymbols Nerd Font", "fallback-symbols"),
            ("symbols", "FixtureMono Nerd Font", "mono-regular"),
            ("cjk", "FixtureCJK Mono", "fallback-cjk"),
            ("emoji", "FixtureEmoji", "fallback-emoji"),
        ])
        self.assertEqual(chain[0]["probes"], ["U+E0B0", "U+E615", "U+F07B", "U+F0249"])
        # U+F835 is gone: fc-match answered, fc-list did not confirm.
        self.assertNotIn("U+F835", [probe for link in chain for probe in link["probes"]])
        rows = {row["id"]: row for row in document["fonts"]}
        self.assertEqual(rows["fallback-symbols"]["role"], "fallback")
        self.assertEqual(rows["fallback-cjk"]["content_type"], "font/collection")
        # A link that lands on a file the mono role already published points at
        # that row rather than publishing the same bytes under a second id.
        self.assertEqual(sum(1 for row in document["fonts"]
                             if row["path"] == rows["mono-regular"]["path"]), 1)

    def test_fc_match_always_answering_is_not_taken_for_an_answer(self):
        """Every probe resolves to the matched family and none of them covers
        it — the shape of a host with a plain monospace and nothing else."""
        probes = {f"{code:x}": {"family": "FixtureMono Nerd Font",
                                "file": "FixtureMono-Regular.ttf", "covers": False}
                  for _, codes in FALLBACK_PROBES for code in codes}
        document = fonts(self.root, probes=probes).snapshot()
        self.assertEqual(document["fallback_chain"], [])
        self.assertEqual([row["id"] for row in document["fonts"]],
                         ["mono-regular", "mono-bold", "icons"])

    def test_two_families_in_one_coverage_both_reach_the_client(self):
        probes = {"e0b0": {"family": "FixtureSymbols Nerd Font",
                           "file": "FixtureSymbols-Regular.ttf", "covers": True},
                  "f835": {"family": "FixtureEmoji", "file": "FixtureEmoji-Regular.ttf",
                           "covers": True}}
        document = fonts(self.root, probes=probes).snapshot()
        self.assertEqual([(link["coverage"], link["font"]) for link in document["fallback_chain"]],
                         [("symbols", "fallback-symbols"), ("symbols", "fallback-symbols-2")])
        self.assertEqual(document["fallback_chain"][1]["probes"], ["U+F835"])

    def test_a_chain_row_is_downloadable_by_id_and_nothing_else_is(self):
        reader = fonts(self.root)
        self.assertEqual(reader.resolve("fallback-symbols")["family"], "FixtureSymbols Nerd Font")
        with self.assertRaises(FontsUnavailable):
            reader.resolve("fallback-latin")

    def test_the_hook_is_what_makes_the_chain_stale(self):
        calls = []
        base = fontconfig(self.root)

        def counting(argv):
            if argv[0].endswith("fc-match"):
                calls.append(argv[1])
            return base(argv)

        reader = HostFonts(icon_font=self.root / "omarchy.ttf", runner=counting)
        reader.snapshot()
        first = len(calls)
        self.assertEqual(first, sum(len(codes) for _, codes in FALLBACK_PROBES))
        reader.snapshot()
        self.assertEqual(len(calls), first)
        reader.refresh()
        reader.snapshot()
        self.assertEqual(len(calls), first * 2)

    def test_the_icon_font_still_answers_when_fontconfig_does_not(self):
        rows = fonts(self.root, fail=True).listing()
        self.assertEqual([row["id"] for row in rows], ["icons"])

    def test_an_unknown_id_is_not_a_path_a_client_can_choose(self):
        with self.assertRaises(FontsUnavailable):
            fonts(self.root).resolve("../../etc/passwd")
        with self.assertRaises(FontsUnavailable):
            fonts(self.root).resolve("mono-italic")
        self.assertEqual(fonts(self.root).resolve("icons")["role"], "icons")

    def test_revision_moves_only_when_a_font_actually_changes(self):
        reader = fonts(self.root)
        self.assertEqual(reader.snapshot()["revision"], 1)
        self.assertEqual(reader.snapshot()["revision"], 1)
        (self.root / "omarchy.ttf").write_bytes(b"\x00\x01\x00\x00a newer omarchy icon font\n")
        self.assertEqual(reader.snapshot()["revision"], 2)


class FontsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in FIXTURE_FILES:
            shutil.copy(FIXTURES / name, self.root / name)
        self.hub = Hub(auth_check_interval=0.05)
        self.token = self.hub.register_device("phone-a")
        self.service = create_service(self.hub, demo=True)
        self.service.fonts = fonts(self.root)
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

    async def test_listing_then_downloading_each_file_by_its_published_digest(self):
        async with self.client.get(self.url + "/v1/fonts") as response:
            self.assertEqual(response.status, 401)
        async with self.client.get(self.url + "/v1/fonts", headers=self.headers()) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()
        self.assertEqual(payload["contract_revision"], "omodachi.v1")
        for row in payload["fonts"]:
            async with self.client.get(self.url + "/v1/fonts/" + row["id"],
                                       headers=self.headers()) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["ETag"], '"' + row["sha256"] + '"')
                body = await response.read()
            self.assertEqual(hashlib.sha256(body).hexdigest(), row["sha256"])
            self.assertEqual(len(body), row["bytes"])
            headers = self.headers() | {"If-None-Match": '"' + row["sha256"] + '"'}
            async with self.client.get(self.url + "/v1/fonts/" + row["id"], headers=headers) as response:
                self.assertEqual(response.status, 304)

    async def test_unknown_font_is_404_and_a_host_without_fonts_is_503(self):
        async with self.client.get(self.url + "/v1/fonts/mono-italic", headers=self.headers()) as response:
            self.assertEqual(response.status, 404)
            self.assertEqual((await response.json())["error"]["code"], "font_not_found")
        self.service.fonts = None
        async with self.client.get(self.url + "/v1/fonts", headers=self.headers()) as response:
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())["error"]["code"], "fonts_unavailable")

    async def test_font_change_publishes_one_event(self):
        async with self.client.ws_connect(self.url + "/v1/events", headers=self.headers()) as ws:
            await ws.receive_json()
            self.assertTrue(self.service.notify_fonts_changed()["published"])
            (self.root / "omarchy.ttf").write_bytes(b"\x00\x01\x00\x00a newer omarchy icon font\n")
            result = self.service.notify_fonts_changed()
            seen = []
            while len(seen) < 2:
                message = await asyncio.wait_for(ws.receive_json(), 2)
                if message.get("event", {}).get("type") == "fonts.changed":
                    seen.append(message["event"]["payload"])
            self.assertEqual(seen[-1], {"revision": result["revision"]})
            self.assertFalse(self.service.notify_fonts_changed()["published"])
