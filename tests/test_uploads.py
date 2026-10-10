"""ATTACH-1: files from the phone, kept on the host for an agent to read.

The accept decision (`uploads.classify`) is a table here, row by row; the
store's cap, file modes, sweep and delete run against a real directory; the
two sends - Herdr's `/text` and the Agent's `messages` - run through the real
router (Herdr against the pretend pane from `test_herdr_keys_text`) and the
real codex adapter (against a controlled JSON-RPC peer).
"""
from __future__ import annotations

import asyncio
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

import aiohttp

from omodachi_core import browser_gate, uploads
from omodachi_core.agent_chat_provider import AgentChatError, CodexDefaultAgentChat, DefaultAgentBinding, project_item
from omodachi_core.agent_chat_service import DefaultAgentChatService
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
from omodachi_core.service import ServiceError

sys.path.insert(0, str(Path(__file__).parent))
import test_agent_chat_provider  # noqa: E402
import test_herdr_keys_text  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 40
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 40
GIF = b"GIF89a" + b"\0" * 40
WEBP = b"RIFF\x24\0\0\0WEBPVP8 " + b"\0" * 40
HEIC = b"\0\0\0\x18ftypheic\0\0\0\0mif1heic" + b"\0" * 40
AVIF = b"\0\0\0\x18ftypavif\0\0\0\0mif1avif" + b"\0" * 40
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"
ZIP = b"PK\x03\x04" + b"\0" * 40


def fake_jpeg(source, target):
    Path(target).write_bytes(JPEG)
    return True


class ClassifyTableTests(unittest.TestCase):
    """Bytes decide an image and a PDF; the name decides text, the bytes veto."""

    ROWS = [
        # name, bytes, text_ok, expected (kind, ext) or refusal code
        ("shot.png", PNG, False, ("image", "png")),
        ("IMG_0001.JPG", JPEG, False, ("image", "jpg")),
        ("anim.gif", GIF, False, ("image", "gif")),
        ("pic.webp", WEBP, False, ("image", "webp")),
        ("IMG_0002.HEIC", HEIC, False, ("image", "heic")),
        # the bytes win over the name, both ways
        ("notes.txt", PNG, False, ("image", "png")),
        ("photo.png", b"just words\n", True, "upload_type_refused"),
        ("paper.pdf", PDF, False, ("pdf", "pdf")),
        ("paper.bin", PDF, False, ("pdf", "pdf")),
        ("fake.pdf", b"not a pdf\n", True, "upload_type_refused"),
        ("main.py", b"print(1)\n", True, ("text", "py")),
        ("README.md", b"# t\n", True, ("text", "md")),
        ("build.log", b"\x1b[31mred\x1b[0m\n", True, ("text", "log")),
        ("data.py", b"\x00\x01", False, "upload_not_text"),
        ("drawing.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", True, "upload_svg_refused"),
        ("drawing.xml", b"<?xml version='1.0'?>\n<svg/>", True, "upload_svg_refused"),
        ("page.html", b"<!doctype html><p>hi</p>", True, ("text", "html")),
        ("bundle.zip", ZIP, False, "upload_type_refused"),
        ("photo.avif", AVIF, False, "upload_type_refused"),
        ("tool.exe", b"MZ\x90\x00", False, "upload_type_refused"),
        ("Makefile", b"all:\n", True, "upload_type_refused"),
        (".env", b"A=1\n", True, "upload_type_refused"),
    ]

    def test_the_table(self):
        for name, head, text_ok, expected in self.ROWS:
            with self.subTest(name=name):
                if isinstance(expected, tuple):
                    self.assertEqual(uploads.classify(name, head, text_ok=text_ok), expected)
                else:
                    with self.assertRaises(ServiceError) as caught:
                        uploads.classify(name, head, text_ok=text_ok)
                    self.assertEqual(caught.exception.code, expected)
                    self.assertEqual(caught.exception.status, 415)

    def test_names_are_shown_without_directories_or_control_characters(self):
        self.assertEqual(uploads.clean_name("../../etc/passwd"), "passwd")
        self.assertEqual(uploads.clean_name("C:\\Users\\x\\a.py"), "a.py")
        self.assertEqual(uploads.clean_name("bad\x00\x07name.txt"), "badname.txt")
        self.assertEqual(uploads.clean_name("..."), "file")
        self.assertEqual(uploads.clean_name(None), "file")
        long = uploads.clean_name("x" * 300 + ".py")
        self.assertLessEqual(len(long), 121)
        self.assertTrue(long.endswith(".py"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name) / "state" / "uploads"
        self.now = 1_790_000_000.0
        self.store = uploads.UploadStore(self.dir, clock=lambda: self.now, converter=fake_jpeg)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, name, data, target=None, chunk=7):
        receiving = self.store.receive()
        for at in range(0, len(data), chunk):
            receiving.write(data[at:at + chunk])
        return receiving.finish(name, device="phone-a", target=target or {})

    def test_a_kept_file_is_private_and_named_by_target_time_and_id(self):
        upload = self.put("shot.png", PNG, {"kind": "agent"})
        path = Path(upload.path)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertRegex(path.name, r"^agent-20260921T[0-9]{6}Z-[0-9a-f]{32}\.png$")
        self.assertTrue(path.name.endswith(upload.id + ".png"))
        self.assertEqual(path.read_bytes(), PNG)
        self.assertEqual(upload.document()["media_type"], "image/png")
        self.assertEqual([p.name for p in self.dir.iterdir()], [path.name], "no .part left behind")
        self.assertTrue(path.name.startswith("agent-"))
        self.assertTrue(Path(self.put("a.py", b"x=1\n", {"kind": "herdr"}).path).name.startswith("herdr-"))
        self.assertTrue(Path(self.put("b.py", b"x=1\n").path).name.startswith("file-"))

    def test_the_cap_is_enforced_while_streaming(self):
        store = uploads.UploadStore(self.dir, max_bytes=100)
        receiving = store.receive()
        receiving.write(b"a" * 100)
        with self.assertRaises(ServiceError) as caught:
            receiving.write(b"b")
        self.assertEqual((caught.exception.code, caught.exception.status), ("upload_too_large", 413))
        self.assertEqual(list(self.dir.iterdir()), [], "the partial file is gone")
        self.assertEqual(uploads.MAX_BYTES, 25 * 1024 * 1024)
        exact = store.receive()
        exact.write(b"a" * 100)
        self.assertEqual(exact.finish("full.txt").bytes, 100)

    def test_text_is_judged_on_the_whole_file(self):
        # a control byte or bad UTF-8 past the first 4 KiB still refuses it
        for tail in (b"\x00", b"\xff\xfe", b"\xe2\x82"):
            with self.subTest(tail=tail):
                with self.assertRaises(ServiceError) as caught:
                    self.put("long.md", b"# ok\n" * 2000 + tail, chunk=4096)
                self.assertEqual(caught.exception.code, "upload_not_text")
        # a multi-byte character split across two chunks is still UTF-8
        self.assertEqual(self.put("zh.md", "你好，世界\n".encode() * 3, chunk=4).kind, "text")
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith(".")], [])

    def test_heic_becomes_a_jpeg_or_is_refused(self):
        upload = self.put("IMG_0002.HEIC", HEIC)
        self.assertEqual((upload.kind, upload.name, Path(upload.path).suffix), ("image", "IMG_0002.jpg", ".jpg"))
        self.assertEqual(Path(upload.path).read_bytes(), JPEG)
        self.store.converter = lambda source, target: False
        with self.assertRaises(ServiceError) as caught:
            self.put("IMG_0003.HEIC", HEIC)
        self.assertEqual(caught.exception.code, "upload_heic_unconvertible")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.name.startswith(".")), [])

    def test_an_empty_file_is_refused(self):
        with self.assertRaises(ServiceError) as caught:
            self.store.receive().finish("empty.txt")
        self.assertEqual((caught.exception.code, caught.exception.status), ("upload_empty", 400))

    def test_delete_and_resolve(self):
        upload = self.put("a.py", b"x=1\n")
        self.assertEqual(self.store.resolve(upload.id).path, upload.path)
        # a daemon restart still finds it on disk
        again = uploads.UploadStore(self.dir)
        self.assertEqual(again.resolve(upload.id).path, upload.path)
        self.assertEqual(again.resolve(upload.id).kind, "text")
        self.assertEqual(self.store.delete(upload.id), {"id": upload.id, "removed": True})
        self.assertFalse(Path(upload.path).exists())
        with self.assertRaises(ServiceError) as caught:
            self.store.resolve(upload.id)
        self.assertEqual((caught.exception.code, caught.exception.status), ("attachment_gone", 409))
        with self.assertRaises(ServiceError) as caught:
            self.store.delete(upload.id)
        self.assertEqual((caught.exception.code, caught.exception.status), ("upload_unknown", 404))
        with self.assertRaises(ServiceError):
            self.store.resolve("../../etc/passwd")

    def test_the_sweep_takes_what_is_older_than_seven_days_and_nothing_else(self):
        old = Path(self.put("old.py", b"x=1\n").path)
        new = Path(self.put("new.py", b"x=1\n").path)
        foreign = self.dir / "keep-me.txt"
        foreign.write_text("not ours")
        part = self.dir / (".upload-" + "f" * 32 + ".part")
        part.write_bytes(b"half")
        eight_days = time.time() - 8 * 24 * 3600
        for path in (old, foreign, part):
            os.utime(path, (eight_days, eight_days))
        six_days = time.time() - 6 * 24 * 3600
        os.utime(new, (six_days, six_days))
        removed = self.store.sweep(now=time.time())
        self.assertEqual(sorted(removed), sorted([old.name, part.name]))
        self.assertTrue(new.exists())
        self.assertTrue(foreign.exists(), "a file this module did not name is never swept")
        self.assertEqual(uploads.TTL_SECONDS, 7 * 24 * 3600)


class ComposeTests(unittest.TestCase):
    def test_herdr_markers_become_paths_where_they_stand(self):
        self.assertEqual(uploads.compose_herdr("look at [Image #1] please", [("[Image #1]", "/u/a.png")]),
                         "look at /u/a.png please")
        # a chip whose marker was edited away goes in front, in order
        self.assertEqual(uploads.compose_herdr("fix [File #2]", [("[Image #1]", "/u/a.png"), ("[File #2]", "/u/b.py")]),
                         "/u/a.png fix /u/b.py")
        self.assertEqual(uploads.compose_herdr("", [(None, "/u/a.png"), (None, "/u/b.py")]), "/u/a.png /u/b.py")
        # a marker with nothing behind it is typed as written
        self.assertEqual(uploads.compose_herdr("see [Image #9]", [("[Image #1]", "/u/a.png")]),
                         "/u/a.png see [Image #9]")

    def test_agent_images_are_inputs_and_files_are_paths(self):
        image = uploads.Upload("1" * 32, "a.png", "image", 1, "/u/a.png", 0)
        code = uploads.Upload("2" * 32, "b.py", "text", 1, "/u/b.py", 0)
        pdf = uploads.Upload("3" * 32, "c.pdf", "pdf", 1, "/u/c.pdf", 0)
        second = uploads.Upload("4" * 32, "d.png", "image", 1, "/u/d.png", 0)
        text, images = uploads.compose_agent(
            "compare [Image #1] with [Image #3] and run [File #2]",
            [("[Image #1]", image), ("[File #2]", code), ("[Image #3]", second), (None, pdf)])
        self.assertEqual(images, ["/u/a.png", "/u/d.png"])
        self.assertEqual(text, "compare [Image #1] with [Image #2] and run /u/b.py\n\nAttached: /u/c.pdf")
        self.assertEqual(uploads.compose_agent("", [(None, image)]), ("[Image #1]", ["/u/a.png"]))
        self.assertEqual(uploads.compose_agent("", [(None, pdf)]), ("Attached: /u/c.pdf", []))

    def test_attachment_refs(self):
        good = "a" * 32
        self.assertEqual(uploads.attachment_refs([good]), [(good, None)])
        self.assertEqual(uploads.attachment_refs([{"id": good, "marker": "[File #2]"}]), [(good, "[File #2]")])
        for bad in ("x", [good, good], ["A" * 32], [{"id": good, "marker": "[Pic #1]"}],
                    [{"id": good, "extra": 1}], ["%032x" % n for n in range(11)]):
            with self.assertRaises(ServiceError, msg=bad):
                uploads.attachment_refs(bad)


class PurgeRuleTests(unittest.TestCase):
    """What the store names is what `--purge` (core's and the plugin's copy,
    compared by the plugin's own test) deletes."""

    def test_the_purge_rule_names_every_file_the_store_makes(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("core_install_host", ROOT / "scripts/install_host.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        rules = installer.PURGE_RULES[".local/state/omodachi"]["uploads"]
        names = ["agent-20261003T194400Z-" + "a" * 32 + ".png", "herdr-20261003T194400Z-" + "b" * 32 + ".py",
                 "file-20261003T194400Z-" + "c" * 32 + ".pdf", ".upload-" + "d" * 32 + ".part",
                 ".upload-" + "e" * 32 + ".jpg"]
        for name in names:
            self.assertTrue(uploads.STORED.fullmatch(name) or uploads.PART.fullmatch(name), name)
            known, rule = installer._rule_for(name, rules)
            self.assertTrue(known and rule is None, name)
        self.assertFalse(installer._rule_for("keep-me.txt", rules)[0])
        self.assertEqual(len(rules), 2, "one rule for a kept file, one for a dead stream's")

    def test_the_plugin_copy_matches_when_a_plugin_checkout_is_at_hand(self):
        plugin = Path(os.environ.get("OMODACHI_PLUGIN_TREE", ""))
        script = plugin / "tools/install_host.py"
        if not os.environ.get("OMODACHI_PLUGIN_TREE") or not script.is_file():
            self.skipTest("set OMODACHI_PLUGIN_TREE to a plugin checkout")
        import importlib.util
        loaded = []
        for name, path in (("plugin_install_host", script), ("core_install_host2", ROOT / "scripts/install_host.py")):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            loaded.append(module.PURGE_RULES)
        self.assertEqual(loaded[0], loaded[1])


class UploadRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub = Hub(auth_check_interval=0.05)
        self.token = self.hub.register_device("phone-a")
        self.web = self.hub.auth.issue("odomo-web1", role="web").token
        self.service = create_service(self.hub, demo=True)
        self.service.uploads = uploads.UploadStore(Path(self.temp.name) / "uploads", converter=fake_jpeg)
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

    async def asyncTearDown(self):
        await self.client.close()
        await asyncio.wait_for(self.server.close(), 3)
        self.temp.cleanup()

    async def upload(self, data, name="shot.png", token=None, query=None, headers=None):
        params = {"name": name, **(query or {})} if name is not None else dict(query or {})
        out = io.StringIO()
        with redirect_stdout(out):
            async with self.client.post(self.url + "/v1/uploads", params=params, data=data,
                                        headers={"Authorization": "Bearer " + (token or self.token),
                                                 "Content-Type": "application/octet-stream",
                                                 **(headers or {})}) as response:
                body = await response.json()
                status = response.status
        lines = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
        return status, body, [line for line in lines if line.get("omodachi") == "upload"]

    async def test_raw_upload_answers_the_contract_and_journals_no_name(self):
        status, body, lines = await self.upload(PNG, name="secret-plan.png", query={"target": "agent"})
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body), {"contract_revision", "id", "name", "kind", "bytes", "path",
                                     "media_type", "expires_at"})
        self.assertEqual((body["kind"], body["bytes"], body["name"]), ("image", len(PNG), "secret-plan.png"))
        self.assertTrue(Path(body["path"]).is_file())
        self.assertEqual(lines, [{"omodachi": "upload", "event": "upload.saved", "device": "phone-a",
                                  "kind": "image", "bytes": len(PNG), "target": "agent"}])
        self.assertNotIn("secret", json.dumps(lines))

    async def test_multipart_upload(self):
        form = aiohttp.FormData()
        form.add_field("target", "herdr")
        form.add_field("pane", "w1:p2")
        form.add_field("file", b"print('hi')\n", filename="main.py", content_type="text/x-python")
        async with self.client.post(self.url + "/v1/uploads", data=form,
                                    headers={"Authorization": "Bearer " + self.token}) as response:
            body = await response.json()
            self.assertEqual(response.status, 200, body)
        self.assertEqual((body["kind"], body["name"]), ("text", "main.py"))
        self.assertTrue(Path(body["path"]).name.startswith("herdr-"))

    async def test_refusals_say_why_and_leave_nothing(self):
        cases = [(b"<svg/>", "x.svg", 415, "upload_svg_refused"),
                 (ZIP, "bundle.zip", 415, "upload_type_refused"),
                 (b"\x00bin", "a.py", 415, "upload_not_text"),
                 (b"", "e.txt", 400, "upload_empty")]
        for data, name, status_expected, code in cases:
            status, body, lines = await self.upload(data, name=name)
            self.assertEqual((status, body["error"]["code"]), (status_expected, code), name)
            self.assertTrue(body["error"]["message"])
            self.assertEqual(lines[-1]["event"], "upload.refused")
            self.assertEqual(lines[-1]["reason"], code)
        status, body, _ = await self.upload(PNG, name=None)
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"))
        status, body, _ = await self.upload(PNG, query={"target": "desktop"})
        self.assertEqual(status, 400)
        status, body, _ = await self.upload(PNG, query={"pane": "../x", "target": "herdr"})
        self.assertEqual(status, 400)
        status, body, _ = await self.upload(PNG, query={"other": "1"})
        self.assertEqual(status, 400)
        self.assertEqual(list((Path(self.temp.name) / "uploads").iterdir()), [])

    async def test_too_large_is_refused_while_streaming_and_by_length(self):
        self.service.uploads.max_bytes = 1000
        async def body():
            for _ in range(40):
                yield b"a" * 100
        status, payload, lines = await self.upload(body(), name="big.txt")
        self.assertEqual((status, payload["error"]["code"]), (413, "upload_too_large"))
        self.assertEqual(list((Path(self.temp.name) / "uploads").iterdir()), [])
        status, payload, _ = await self.upload(b"a" * (1000 + 65537), name="big.txt")
        self.assertEqual((status, payload["error"]["code"]), (413, "upload_too_large"))

    async def test_web_credential_uploads_and_deletes(self):
        self.assertTrue(browser_gate.web_allows("POST", "/v1/uploads"))
        self.assertTrue(browser_gate.web_allows("DELETE", "/v1/uploads/" + "a" * 32))
        self.assertFalse(browser_gate.web_allows("GET", "/v1/uploads/" + "a" * 32))
        self.assertFalse(browser_gate.web_allows("DELETE", "/v1/uploads/../x"))
        status, body, lines = await self.upload(b"# notes\n", name="notes.md", token=self.web)
        self.assertEqual(status, 200, body)
        self.assertEqual(lines[0]["device"], "odomo-web1")
        async with self.client.delete(self.url + "/v1/uploads/" + body["id"],
                                      headers={"Authorization": "Bearer " + self.web}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), {"contract_revision": "omodachi.v1",
                                                     "id": body["id"], "removed": True})
        self.assertFalse(Path(body["path"]).exists())
        async with self.client.delete(self.url + "/v1/uploads/" + body["id"],
                                      headers={"Authorization": "Bearer " + self.web}) as response:
            self.assertEqual(response.status, 404)

    async def test_needs_a_credential_and_a_store(self):
        async with self.client.post(self.url + "/v1/uploads?name=a.png", data=PNG) as response:
            self.assertEqual(response.status, 401)
        self.service.uploads = None
        status, body, _ = await self.upload(PNG)
        self.assertEqual((status, body["error"]["code"]), (503, "upload_unavailable"))


class HerdrAttachmentTests(test_herdr_keys_text.KeysTextApiTests):
    """`/text` with chips: the pane gets the paths, never the markers."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.service.uploads = uploads.UploadStore(Path(self.temp.name) / "uploads")
        receiving = self.service.uploads.receive()
        receiving.write(PNG)
        self.image = receiving.finish("shot.png", target={"kind": "herdr"})

    async def test_markers_become_paths_and_the_audit_counts_them(self):
        status, body = await self.post("/v1/herdr/panes/w1:p1/text", {
            "text": "what is in [Image #1] please", "submit": True,
            "attachments": [{"id": self.image.id, "marker": "[Image #1]"}]})
        self.assertEqual((status, body["submitted"]), (200, True), body)
        self.assertEqual(self.fake.history[-1], "> what is in " + self.image.path + " please")
        line = json.loads(self.audit_path.read_text().splitlines()[-1])
        self.assertEqual((line["attachments"], line["length"]),
                         (1, len("what is in " + self.image.path + " please")))
        self.assertNotIn(self.image.path, self.audit_path.read_text())

    async def test_a_gone_attachment_types_nothing(self):
        self.service.uploads.delete(self.image.id)
        before = list(self.fake.requests)
        status, body = await self.post("/v1/herdr/panes/w1:p1/text", {
            "text": "see [Image #1]", "submit": True, "attachments": [self.image.id]})
        self.assertEqual((status, body["error"]["code"]), (409, "attachment_gone"))
        self.assertEqual(self.fake.requests, before)


class AgentAttachmentTests(unittest.IsolatedAsyncioTestCase):
    def binding(self):
        return DefaultAgentBinding("host-owned", "thread-owned", "ws://127.0.0.1:47311", "a" * 64)

    async def test_images_are_local_image_inputs(self):
        rpc = test_agent_chat_provider.ControlledRPC()
        chat = CodexDefaultAgentChat(self.binding(), rpc)
        await chat.attach()
        result = await chat.send("what is [Image #1]", "r1", images=("/u/a.png",))
        self.assertEqual(result["images"], "native")
        method, params = rpc.calls[-1]
        self.assertEqual(method, "turn/start")
        self.assertEqual(params["input"], [{"type": "text", "text": "what is [Image #1]"},
                                           {"type": "localImage", "path": "/u/a.png"}])

    async def test_a_rejected_image_falls_back_to_its_path_and_says_so(self):
        class Rejecting(test_agent_chat_provider.ControlledRPC):
            async def request_fenced(self, method, params):
                if method == "turn/start" and any(item["type"] == "localImage" for item in params["input"]):
                    self.calls.append((method, params))
                    raise AgentChatError("provider_request_rejected")
                return await super().request_fenced(method, params)
        rpc = Rejecting()
        chat = CodexDefaultAgentChat(self.binding(), rpc)
        await chat.attach()
        result = await chat.send("what is [Image #1]", "r1", images=("/u/a.png",))
        self.assertEqual((result["accepted"], result["images"]), (True, "path"))
        self.assertEqual(rpc.calls[-1][1]["input"],
                         [{"type": "text", "text": "what is [Image #1]\n\nAttached: /u/a.png"}])

    async def test_without_images_nothing_changes(self):
        rpc = test_agent_chat_provider.ControlledRPC()
        chat = CodexDefaultAgentChat(self.binding(), rpc)
        await chat.attach()
        result = await chat.send("plain", "r1")
        self.assertNotIn("images", result)
        self.assertEqual(rpc.calls[-1][1]["input"], [{"type": "text", "text": "plain"}])

    def test_a_user_row_counts_its_pictures(self):
        row = project_item({"type": "userMessage", "id": "u1", "content": [
            {"type": "text", "text": "look"}, {"type": "localImage", "path": "/u/a.png"},
            {"type": "image", "url": "data:x"}]}, "t1")
        self.assertEqual((row["text"], row["images"]), ("look", 2))
        plain = project_item({"type": "userMessage", "id": "u2", "content": [{"type": "text", "text": "x"}]}, "t1")
        self.assertNotIn("images", plain)

    async def test_the_service_composes_and_resolves(self):
        with tempfile.TemporaryDirectory() as scratch:
            store = uploads.UploadStore(Path(scratch) / "uploads")
            def put(name, data):
                receiving = store.receive()
                receiving.write(data)
                return receiving.finish(name)
            image, pdf = put("a.png", PNG), put("c.pdf", PDF)
            sent = []

            class Chat:
                attached = True
                async def send(self, text, request_id, *, model=None, effort=None, images=()):
                    sent.append((text, images))
                    return {"accepted": True, "turnID": "t", "delivery": "accepted", "images": "native"}
            service = DefaultAgentChatService.__new__(DefaultAgentChatService)
            service.chat, service.uploads = Chat(), store
            await service.send({"request_id": "r1", "text": "read [File #2] and see [Image #1]",
                                "attachments": [{"id": image.id, "marker": "[Image #1]"},
                                                {"id": pdf.id, "marker": "[File #2]"}]}, lambda: None)
            self.assertEqual(sent[-1], (f"read {pdf.path} and see [Image #1]", (image.path,)))
            await service.send({"request_id": "r2", "text": "", "attachments": [pdf.id]}, lambda: None)
            self.assertEqual(sent[-1], (f"Attached: {pdf.path}", ()))
            store.delete(pdf.id)
            with self.assertRaises(ServiceError) as caught:
                await service.send({"request_id": "r3", "text": "x", "attachments": [pdf.id]}, lambda: None)
            self.assertEqual(caught.exception.code, "attachment_gone")
            with self.assertRaises(ServiceError) as caught:
                await service.send({"request_id": "r4", "text": "", "attachments": []}, lambda: None)
            self.assertEqual(caught.exception.code, "invalid_task")


if __name__ == "__main__":
    unittest.main()
