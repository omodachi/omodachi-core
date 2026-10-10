"""LINKS-1: `POST /v1/host/open`, the rows that carry openable paths, and the
shared link-detection vectors both clients test against.

Nothing here launches anything: the opener's runner and spawner are fakes
that record the argv a real host would run. Files live in a throwaway home.
"""
import asyncio
from contextlib import redirect_stdout
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from omodachi_core import host_open as H
from omodachi_core.agent_chat_provider import project_item
from omodachi_core.bootstrap import create_service
from omodachi_core.browser_gate import web_allows
from omodachi_core.herdr_bridge import HerdrUnavailable
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
from omodachi_core.service import ServiceError

FIXTURES = Path(__file__).resolve().parents[1] / "contracts" / "fixtures"
OUTPUT = "OMODACHI-0123456789abcdef"


class FakeDesktop:
    """The runner (xdg-mime, hyprctl) and the spawner of `DesktopOpener`."""

    def __init__(self, *, mime="text/plain", handler="nvim.desktop", focus_ok=True, exit_code=None):
        self.mime, self.handler, self.focus_ok, self.exit_code = mime, handler, focus_ok, exit_code
        self.ran, self.spawned = [], []

    def runner(self, argv, env):
        self.ran.append(argv)
        if argv[1:3] == ("query", "filetype"):
            return self.mime
        if argv[1:3] == ("query", "default"):
            return self.handler if argv[3] == self.mime or argv[3] == "text/plain" else ""
        if argv[1:] == ("-j", "monitors"):
            return json.dumps([{"name": "eDP-1", "focused": not self.focus_ok},
                               {"name": OUTPUT, "focused": self.focus_ok}])
        return "ok"

    def spawner(self, argv, env, cwd):
        self.spawned.append(argv)
        return self.exit_code

    def opener(self, home):
        opener = H.DesktopOpener(environment=lambda: {"PATH": "/usr/bin"}, runner=self.runner,
                                 spawner=self.spawner, home=home)
        opener.has_editor_launcher = lambda: True
        opener.default_editor = lambda: "nvim"
        return opener


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.work = self.home / "l1test"
        self.work.mkdir()
        (self.work / "report.md").write_text("# r\n")
        (self.work / "a b.txt").write_text("x\n")
        (self.home / ".ssh").mkdir()
        (self.home / ".ssh" / "config").write_text("Host *\n")
        uploads = self.home / ".local/state/omodachi/uploads"
        uploads.mkdir(parents=True)
        (uploads / "agent-x.png").write_bytes(b"\x89PNG")
        (self.home / ".config/omodachi").mkdir(parents=True)
        (self.home / ".config/omodachi/devices.json").write_text("{}")

    def tearDown(self):
        self.tmp.cleanup()

    def resolve(self, text, cwd=None, mime=None):
        return H.resolve_path(text, home=self.home, cwd=cwd or self.work, mime_of=lambda path: mime)


class ResolveTests(Home):
    def test_spellings_of_one_file(self):
        real = str(self.work / "report.md")
        for text in (real, "~/l1test/report.md", "./report.md", "report.md", "file://" + real,
                     "<" + real + ">"):
            self.assertEqual(self.resolve(text).value, real, text)
        self.assertEqual(self.resolve(real + ":12").line, 12)
        self.assertEqual(self.resolve(real + ":12:4").line, 12)
        self.assertEqual(self.resolve(real + "#L7").line, 7)
        self.assertEqual(self.resolve(real + "#L7-L9").line, 7)
        self.assertIsNone(self.resolve(real).line)
        # A markdown target is percent-encoded.
        self.assertEqual(self.resolve(str(self.work) + "/a%20b.txt").name, "a b.txt")

    def test_a_folder_opens_and_has_no_line(self):
        target = self.resolve("~/l1test")
        self.assertEqual((target.kind, target.line), ("directory", None))

    def test_private_places_are_refused_first(self):
        for text in ("~/.ssh/config", "~/.config/omodachi/devices.json", "~/.ssh"):
            with self.assertRaises(ServiceError) as caught:
                self.resolve(text)
            self.assertEqual(caught.exception.code, "open_denied", text)
        # ... even through a link that lives somewhere ordinary.
        (self.work / "innocent").symlink_to(self.home / ".ssh" / "config")
        with self.assertRaises(ServiceError) as caught:
            self.resolve("./innocent")
        self.assertEqual(caught.exception.code, "open_denied")
        # Key-ish names anywhere.
        for name in (".env", ".env.local", "id_ed25519", "server.pem", "auth.json"):
            (self.work / name).write_text("secret")
            with self.assertRaises(ServiceError) as caught:
                self.resolve("./" + name)
            self.assertEqual(caught.exception.code, "open_denied", name)

    def test_a_private_place_is_refused_even_when_nothing_is_there(self):
        for text in ("~/.ssh/config-that-is-not-there", "~/.gnupg/x", "~/.codex/auth.json", "/tmp/.env.missing"):
            with self.assertRaises(ServiceError) as caught:
                self.resolve(text)
            self.assertEqual(caught.exception.code, "open_denied", text)

    def test_what_the_phone_sent_stays_openable(self):
        self.assertEqual(self.resolve("~/.local/state/omodachi/uploads/agent-x.png").name, "agent-x.png")

    def test_outside_the_roots(self):
        with self.assertRaises(ServiceError) as caught:
            H.resolve_path("/etc/hosts", home=self.home, cwd=self.work)
        self.assertEqual(caught.exception.code, "open_outside_roots")
        # The source's own folder is a root, for the user's own files.
        with tempfile.TemporaryDirectory() as other:
            Path(other, "x.txt").write_text("x")
            target = H.resolve_path("x.txt", home=self.home, cwd=Path(other))
            self.assertEqual(target.name, "x.txt")
            with self.assertRaises(ServiceError):
                H.resolve_path("x.txt", home=self.home, cwd=Path(other), uid=os.getuid() + 1)

    def test_never_run(self):
        script = self.work / "run.sh"
        script.write_text("#!/bin/sh\n")
        os.chmod(script, 0o755)
        (self.work / "app.desktop").write_text("[Desktop Entry]\n")
        (self.work / "tool").write_bytes(b"\x7fELF" + b"\0" * 16)
        (self.work / "Thing.AppImage").write_text("x")
        for text in ("./run.sh", "./app.desktop", "./tool", "./Thing.AppImage"):
            with self.assertRaises(ServiceError) as caught:
                self.resolve(text)
            self.assertEqual(caught.exception.code, "open_executable", text)
        (self.work / "x.bin2").write_text("x")
        with self.assertRaises(ServiceError) as caught:
            self.resolve("./x.bin2", mime="application/x-executable")
        self.assertEqual(caught.exception.code, "open_executable")
        # A script without the bit is text to read.
        (self.work / "plain.sh").write_text("echo hi\n")
        self.assertEqual(self.resolve("./plain.sh").name, "plain.sh")

    def test_only_files_and_folders(self):
        os.mkfifo(self.work / "pipe")
        with self.assertRaises(ServiceError) as caught:
            self.resolve("./pipe")
        self.assertEqual(caught.exception.code, "open_type_refused")
        with self.assertRaises(ServiceError) as caught:
            self.resolve("./nothing-here.md")
        self.assertEqual(caught.exception.code, "open_missing")

    def test_urls(self):
        self.assertEqual(H.resolve_url("http://localhost:8000/x").kind, "url")
        for text in ("javascript:alert(1)", "file:///etc/passwd", "ftp://x/y", "http://"):
            with self.assertRaises(ServiceError) as caught:
                H.resolve_url(text)
            self.assertEqual(caught.exception.code, "open_url_refused", text)


class RequestTests(unittest.TestCase):
    def test_shape(self):
        ok = H.parse({"source": {"kind": "agent", "item": "item-1"}, "path": "/a"})
        self.assertFalse(ok.check)
        self.assertTrue(H.parse({"source": {"kind": "herdr", "pane": "w1:p2", "session": "work"},
                                 "url": "http://x/", "check": True}).is_url)
        for bad in ({"source": {"kind": "agent", "item": "i"}},
                    {"source": {"kind": "agent", "item": "i"}, "path": "/a", "url": "http://x/"},
                    {"source": {"kind": "agent"}, "path": "/a"},
                    {"source": {"kind": "herdr", "pane": "p1"}, "path": "/a"},
                    {"source": {"kind": "herdr", "pane": "w1:p1", "session": "../x"}, "path": "/a"},
                    {"source": {"kind": "shell"}, "path": "/a"},
                    {"source": {"kind": "agent", "item": "i"}, "path": "/a\n/b"},
                    {"source": {"kind": "agent", "item": "i"}, "path": "x" * 5000},
                    {"source": {"kind": "agent", "item": "i"}, "path": "/a", "check": "yes"},
                    {"source": {"kind": "agent", "item": "i"}, "path": "/a", "extra": 1}):
            with self.assertRaises(ServiceError, msg=bad):
                H.parse(bad)

    def test_mentioned_includes_a_wrapped_path(self):
        self.assertTrue(H.mentioned("/home/u/a/very/long/path.md", "x /home/u/a/very/lo\nng/path.md y"))
        self.assertFalse(H.mentioned("/home/u/other.md", "x /home/u/a.md"))
        self.assertFalse(H.mentioned("", "anything"))


class OpenTests(Home):
    def rules(self, desktop, **options):
        return H.HostOpen(opener=desktop.opener(self.home), home=self.home, log=self.lines.append, **options)

    def setUp(self):
        super().setUp()
        self.lines = []

    def run_open(self, rules, text, *, haystack=None, url=False, check=False, output=OUTPUT):
        request = H.Request({"kind": "agent", "item": "i"}, text, url, check)
        return rules.run(request, device="phone", role="companion",
                         haystack=text if haystack is None else haystack, cwd=self.work, output=output)

    def test_a_line_opens_the_editor_there_on_the_remote_screen(self):
        desktop = FakeDesktop()
        result = self.run_open(self.rules(desktop), str(self.work / "report.md") + ":12")
        self.assertEqual(result, {"opened": True, "kind": "file", "name": "report.md", "line": 12,
                                  "where": "remote", "at_line": True})
        self.assertEqual(desktop.spawned, [(H.EDITOR_LAUNCHER, "+12", str(self.work / "report.md"))])
        focus = [argv for argv in desktop.ran if argv[1:2] == ("eval",)]
        self.assertEqual(len(focus), 1)
        self.assertIn('monitor = "%s"' % OUTPUT, focus[0][2])
        self.assertEqual(self.lines[-1]["outcome"], "opened")
        self.assertEqual(self.lines[-1]["target"], str(self.work / "report.md"))

    def test_other_handlers_open_the_file_as_usual(self):
        desktop = FakeDesktop(mime="text/markdown", handler="omawrite.desktop")
        result = self.run_open(self.rules(desktop), str(self.work / "report.md") + ":12")
        self.assertFalse(result["at_line"])
        self.assertEqual(desktop.spawned, [("/usr/bin/xdg-open", str(self.work / "report.md"))])

    def test_helix_takes_path_colon_line(self):
        desktop = FakeDesktop()
        opener = desktop.opener(self.home)
        opener.default_editor = lambda: "hx"
        target = H.Target("file", "/x/a.py", "a.py", 3, "text/x-python")
        self.assertEqual(opener.argv_for(target, {})[0], (H.EDITOR_LAUNCHER, "/x/a.py:3"))

    def test_no_session_is_the_focused_screen(self):
        desktop = FakeDesktop()
        result = self.run_open(self.rules(desktop), str(self.work / "report.md"), output=None)
        self.assertEqual(result["where"], "screen")
        self.assertFalse([argv for argv in desktop.ran if argv[1:2] == ("eval",)])
        # A focus that does not read back says so rather than claiming Remote.
        desktop = FakeDesktop(focus_ok=False)
        self.assertEqual(self.run_open(self.rules(desktop), str(self.work / "report.md"))["where"], "screen")

    def test_check_opens_nothing(self):
        desktop = FakeDesktop()
        result = self.run_open(self.rules(desktop), "~/l1test/report.md", check=True)
        self.assertEqual((result["opened"], result["where"]), (False, None))
        self.assertEqual(desktop.spawned, [])
        self.assertEqual(self.lines[-1]["outcome"], "checked")

    def test_a_web_address(self):
        desktop = FakeDesktop()
        result = self.run_open(self.rules(desktop), "http://localhost:8000/", url=True)
        self.assertEqual((result["kind"], result["name"]), ("url", "localhost"))
        self.assertEqual(desktop.spawned, [("/usr/bin/xdg-open", "http://localhost:8000/")])

    def test_refusals_say_why_and_open_nothing(self):
        desktop = FakeDesktop()
        rules = self.rules(desktop)
        cases = (("~/l1test/report.md", "something else entirely", "open_not_mentioned"),
                 ("~/.ssh/config", None, "open_denied"))
        for text, haystack, code in cases:
            with self.assertRaises(ServiceError) as caught:
                self.run_open(rules, text, haystack=haystack)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(self.lines[-1]["outcome"], code)
            self.assertEqual(self.lines[-1]["asked"], text)
        request = H.Request({"kind": "agent", "item": "gone"}, "/x", False, False)
        with self.assertRaises(ServiceError) as caught:
            rules.run(request, device="phone", role="companion", haystack=None, cwd=None, output=None)
        self.assertEqual(caught.exception.code, "open_source_gone")
        self.assertEqual(desktop.spawned, [])

    def test_a_handler_that_fails_is_unavailable(self):
        desktop = FakeDesktop(exit_code=4)
        with self.assertRaises(ServiceError) as caught:
            self.run_open(self.rules(desktop), "~/l1test/report.md")
        self.assertEqual(caught.exception.code, "open_unavailable")

    def test_rate_limit(self):
        now = [0.0]
        rules = self.rules(FakeDesktop(), clock=lambda: now[0])
        for _ in range(H.RATE_LIMIT):
            self.run_open(rules, "~/l1test/report.md", check=True)
        with self.assertRaises(ServiceError) as caught:
            self.run_open(rules, "~/l1test/report.md", check=True)
        self.assertEqual(caught.exception.code, "open_rate_limited")
        now[0] = H.RATE_WINDOW_SECONDS + 1
        self.run_open(rules, "~/l1test/report.md", check=True)

    def test_the_focus_lua_takes_only_an_output_name(self):
        self.assertIn('"OMODACHI-1"', H.focus_monitor_lua("OMODACHI-1"))
        with self.assertRaises(ValueError):
            H.focus_monitor_lua('x"}) os.execute("id") --')


class RowTests(unittest.TestCase):
    def test_file_change_and_image_view_carry_paths(self):
        row = project_item({"type": "fileChange", "id": "c", "status": "completed",
                            "changes": [{"path": "/h/a.py", "kind": {"type": "add"}}, {"path": "/h/a.py"},
                                        {"path": "/h/b.py"}, "junk"]}, "t")
        self.assertEqual(row["paths"], ["/h/a.py", "/h/b.py"])
        view = project_item({"type": "imageView", "id": "v", "path": "/h/chart.png"}, "t")
        self.assertEqual((view["kind"], view["name"], view["status"], view["detail"], view["paths"]),
                         ("tool", "Viewed image", "succeeded", "/h/chart.png", ["/h/chart.png"]))
        self.assertIsNone(project_item({"type": "imageView", "id": "v"}, "t"))
        self.assertNotIn("paths", project_item({"type": "fileChange", "id": "c", "changes": []}, "t"))


class ServiceSourceTests(unittest.TestCase):
    def setUp(self):
        self.service = create_service(Hub(), demo=True)

    def test_agent_rows_that_count(self):
        rows = {"reply": {"kind": "message", "role": "assistant", "text": "see /a.md"},
                "ask": {"kind": "message", "role": "user", "text": "open /etc/x"},
                "cmd": {"kind": "tool", "name": "ls ~/.ssh", "detail": "id_rsa"},
                "view": {"kind": "tool", "name": "Viewed image", "detail": "/b.png", "paths": ["/b.png"]}}
        self.service.agent_chat = SimpleNamespace(chat=SimpleNamespace(rows=rows, binding=SimpleNamespace(cwd="/w")))
        self.assertEqual(self.service.host_open_agent_source("reply"), ("see /a.md", Path("/w")))
        self.assertEqual(self.service.host_open_agent_source("view")[0], "/b.png")
        for item in ("ask", "cmd", "missing"):
            self.assertEqual(self.service.host_open_agent_source(item), (None, None), item)
        self.service.agent_chat = None
        self.assertEqual(self.service.host_open_agent_source("reply"), (None, None))

    def test_herdr_reads_the_visible_screen_of_this_devices_session(self):
        class Bridge:
            session = "work"
            def read_visible(self, pane):
                if pane != "w1:p2":
                    raise HerdrUnavailable("herdr_pane_gone")
                return "Wrote ~/l1test/a.md"
            def layout(self):
                return {"workspaces": [{"tabs": [{"panes": [{"id": "w1:p2", "cwd": "/home/u/l1test"}]}]}]}
        self.service._herdr = lambda device: Bridge()
        self.assertEqual(self.service.host_open_herdr_source({"kind": "herdr", "pane": "w1:p2"}, "phone"),
                         ("Wrote ~/l1test/a.md", Path("/home/u/l1test")))
        self.assertEqual(self.service.host_open_herdr_source({"kind": "herdr", "pane": "w1:p2", "session": "other"},
                                                             "phone"), (None, None))
        self.assertEqual(self.service.host_open_herdr_source({"kind": "herdr", "pane": "w9:p9"}, "phone"),
                         (None, None))


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        (self.home / "l1test").mkdir()
        (self.home / "l1test" / "report.md").write_text("# r\n")
        self.hub = Hub()
        self.service = create_service(self.hub, demo=True)
        self.desktop = FakeDesktop()
        self.service.host_opener = H.HostOpen(opener=self.desktop.opener(self.home), home=self.home)
        text = f"Wrote [report]({self.home}/l1test/report.md) and http://localhost:8000/"
        self.service.agent_chat = SimpleNamespace(chat=SimpleNamespace(
            rows={"reply": {"kind": "message", "role": "assistant", "text": text}},
            binding=SimpleNamespace(cwd=str(self.home / "l1test"))))
        self.native = self.hub.register_device("phone")
        self.web = self.hub.auth.issue("odomo-web", role="web").token
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        self.url = f"http://127.0.0.1:{self.server.bound_port}"

    async def asyncTearDown(self):
        await self.client.close()
        await asyncio.wait_for(self.server.close(), 3)
        self.tmp.cleanup()

    async def post(self, token, body):
        out = io.StringIO()
        with redirect_stdout(out):
            async with self.client.post(self.url + "/v1/host/open", json=body,
                                        headers={"Authorization": "Bearer " + token}) as response:
                status, value = response.status, await response.json()
        lines = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
        return status, value, [line for line in lines if line.get("omodachi") == "host_open"]

    async def test_native_and_web_open_what_the_agent_mentioned(self):
        source = {"kind": "agent", "item": "reply"}
        for token, role in ((self.native, "companion"), (self.web, "web")):
            status, value, lines = await self.post(token, {"source": source,
                                                           "path": f"{self.home}/l1test/report.md"})
            self.assertEqual(status, 200, value)
            self.assertEqual((value["opened"], value["name"], value["where"]), (True, "report.md", "screen"))
            self.assertEqual(lines[-1]["role"], role)
        status, value, _ = await self.post(self.web, {"source": source, "url": "http://localhost:8000/"})
        self.assertEqual((status, value["kind"]), (200, "url"))
        self.assertTrue(web_allows("POST", "/v1/host/open"))
        self.assertFalse(web_allows("GET", "/v1/host/open"))

    async def test_refusals_on_the_wire(self):
        source = {"kind": "agent", "item": "reply"}
        status, value, lines = await self.post(self.native, {"source": source, "path": "/etc/hosts"})
        self.assertEqual((status, value["error"]["code"]), (403, "open_not_mentioned"))
        self.assertEqual(lines[-1]["outcome"], "open_not_mentioned")
        status, value, _ = await self.post(self.native, {"source": {"kind": "agent", "item": "gone"}, "path": "/x"})
        self.assertEqual((status, value["error"]["code"]), (404, "open_source_gone"))
        status, value, _ = await self.post(self.native, {"source": source})
        self.assertEqual((status, value["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(self.desktop.spawned, [])


# ── the shared vectors (contracts/fixtures/links-detect.json) ──────────────
# A reference implementation of the rules in the fixture's `notes`, so the
# vectors the two clients test against are consistent with themselves.
VECTORS = json.loads((FIXTURES / "links-detect.json").read_text())
EXTENSIONS = "|".join(sorted(VECTORS["known_extensions"], key=len, reverse=True))
BOUNDARY = r"(?:(?<=^)|(?<=[\s(\[{<\"'`=]))"
URL = r"https?://[^\s<>\"'`]+"
PATH_CHARS = r"[A-Za-z0-9._~@%+=,/:#-]"
PATH = (r"(?:(?:~|\.\.?)/(?![/\s])" + PATH_CHARS + r"+|/(?![/\s])(?=" + PATH_CHARS + r"*[/.])" + PATH_CHARS + r"+)")
BARE = r"[A-Za-z0-9_][A-Za-z0-9._-]*\.(?:" + EXTENSIONS + r")(?![A-Za-z0-9_/-])"
MIRROR = re.compile(BOUNDARY + "(?:(?P<url>" + URL + ")|(?P<path>" + PATH + ")|(?P<bare>" + BARE + "))", re.I)
PROSE = re.compile(BOUNDARY + "(?:(?P<url>" + URL + ")|(?P<path>" + PATH + "))")


def trim(text):
    pairs = {")": "(", "]": "[", "}": "{"}
    while text:
        last = text[-1]
        if last in ".,;:!?":
            text = text[:-1]
        elif last in pairs and text.count(pairs[last]) < text.count(last):
            text = text[:-1]
        else:
            break
    return text


def detect(text, pattern):
    found = []
    for match in pattern.finditer(text):
        kind = "url" if match.group("url") else "path"
        value = trim(match.group(0))
        if value:
            found.append({"kind": kind, "text": value})
    return found


def host_names(roads):
    names = {row["host"].lower() for row in roads["endpoints"]}
    tailnet = roads.get("tailnet") or {}
    if tailnet.get("dns_name"):
        names.add(tailnet["dns_name"].lower())
    names.update(ip.lower() for ip in tailnet.get("ips") or [])
    for name in list(names):
        if name.endswith(".local") or name.endswith(".ts.net"):
            names.add(name.split(".")[0])
    return names


def host_local(url, roads):
    host = (urlsplit(url).hostname or "").lower()
    if host in ("localhost", "0.0.0.0", "::1") or host.endswith(".localhost"):
        return True
    try:
        if ipaddress.ip_address(host).is_loopback:
            return True
    except ValueError:
        pass
    return host in host_names(roads)


def rewrite(url, road, roads):
    if not host_local(url, roads):
        return url
    parts = urlsplit(url)

    def is_ip(value):
        try:
            ipaddress.ip_address(value)
            return True
        except ValueError:
            return False

    def first_v4(kind):
        for row in roads["endpoints"]:
            if row["kind"] == kind and is_ip(row["host"]) and ipaddress.ip_address(row["host"]).version == 4:
                return row["host"]
        return None
    host = road["host"]
    if parts.scheme == "http" and not is_ip(host):
        host = (first_v4("tailnet") if road["kind"] == "tailnet" else first_v4("lan")) or host
    netloc = (f"[{host}]" if ":" in host else host) + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class VectorTests(unittest.TestCase):
    def test_detection(self):
        for case in VECTORS["mirror"]:
            self.assertEqual(detect(case["text"], MIRROR), case["links"], case["text"])
        for case in VECTORS["prose"]:
            self.assertEqual(detect(case["text"], PROSE), case["links"], case["text"])

    def test_host_local_and_rewrite(self):
        roads = json.loads((FIXTURES / "host-roads.json").read_text())
        for case in VECTORS["host_local"]:
            self.assertEqual(host_local(case["url"], roads), case["host_local"], case["url"])
        for case in VECTORS["rewrite"]:
            self.assertEqual(rewrite(case["url"], case["road"], roads), case["expected"], case["url"])

    def test_every_detected_path_resolves_through_the_same_suffix_rules(self):
        for case in VECTORS["mirror"]:
            for link in case["links"]:
                if link["kind"] == "path" and ":" in link["text"]:
                    body, line = H.split_line(link["text"])
                    self.assertIsNotNone(line, link)


if __name__ == "__main__":
    unittest.main()
