"""LITE-1b C7: the `tailscale serve` front door, against a fake `tailscale`.

The fake is a real executable on a temporary PATH that keeps a serve config in
a JSON file and logs every argv, so these tests see exactly which commands the
installer would run on a host - and never touch this machine's own tailscaled.
Its JSON is the shape `tailscale serve status --json` prints (ipn.ServeConfig:
`TCP{port:{HTTPS}}`, `Web{"name:port":{Handlers{"/":{Proxy}}}}`).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

from omodachi_core import odomo_serve
from omodachi_core.host_identity import HostIdentity

ROOT = Path(__file__).resolve().parents[1]
NAME = "omodachi-lab.example-tailnet.ts.net"
OURS = "https+insecure://127.0.0.1:8099"

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
state_path, log_path = os.environ["FAKE_TS_STATE"], os.environ["FAKE_TS_LOG"]
state = json.load(open(state_path))
args = sys.argv[1:]
open(log_path, "a").write(json.dumps(args) + "\n")
def save():
    json.dump(state, open(state_path, "w"))
if state.get("mode") == "denied" and args[:1] == ["serve"]:
    print("Access denied: serve config denied. Use 'sudo tailscale serve' or 'tailscale set --operator=$USER'", file=sys.stderr)
    sys.exit(1)
if args == ["status", "--json"]:
    print(json.dumps({"BackendState": state.get("backend", "Running"), "Self": {"DNSName": state["name"] + "."}}))
elif args == ["serve", "status", "--json"]:
    print(json.dumps(state["serve"]))
elif args[:1] == ["serve"] and args[-1] == "off":
    port = args[1].split("=")[1]
    state["serve"].get("TCP", {}).pop(port, None)
    state["serve"].get("Web", {}).pop(state["name"] + ":" + port, None)
    for key in ("TCP", "Web"):
        if not state["serve"].get(key):
            state["serve"].pop(key, None)
    save()
elif args[:3] == ["serve", "--bg", "--yes"]:
    if state.get("mode") == "hang":
        time.sleep(30)
    port, target = args[3].split("=")[1], args[4]
    state["serve"].setdefault("TCP", {})[port] = {"HTTPS": True}
    state["serve"].setdefault("Web", {})[state["name"] + ":" + port] = {"Handlers": {"/": {"Proxy": target}}}
    save()
    print("Available within your tailnet:\n\nhttps://" + state["name"] + ("" if port == "443" else ":" + port) + "/")
else:
    print("unexpected: " + " ".join(args), file=sys.stderr)
    sys.exit(2)
'''


def web(port, proxy, name=NAME):
    return {"TCP": {str(port): {"HTTPS": True}}, "Web": {f"{name}:{port}": {"Handlers": {"/": {"Proxy": proxy}}}}}


class FakeTailscale:
    def __init__(self, directory: Path, serve=None, *, mode=None, backend="Running"):
        self.bin = directory / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.path = self.bin / "tailscale"
        self.path.write_text(FAKE)
        self.path.chmod(0o755)
        self.state = directory / "ts-state.json"
        self.log = directory / "ts-log.jsonl"
        self.state.write_text(json.dumps({"name": NAME, "serve": serve or {}, "mode": mode, "backend": backend}))
        self.env = {"FAKE_TS_STATE": str(self.state), "FAKE_TS_LOG": str(self.log)}

    def tool(self):
        return odomo_serve.Tailscale(which=lambda name: str(self.path))

    @property
    def serve(self):
        return json.loads(self.state.read_text())["serve"]

    @property
    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []


def merged(*configs):
    out = {"TCP": {}, "Web": {}}
    for config in configs:
        out["TCP"].update(config.get("TCP", {}))
        out["Web"].update(config.get("Web", {}))
    return out


class FrontDoorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.lines = []

    def fake(self, serve=None, **options):
        fake = FakeTailscale(Path(self.temp.name), serve, **options)
        patcher = mock.patch.dict(os.environ, fake.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def ensure(self, fake):
        return odomo_serve.ensure(self.home, tailscale=fake.tool(), log=self.lines.append, clock=lambda: 1_800_000_000)

    def remove(self, fake):
        return odomo_serve.remove(self.home, tailscale=fake.tool(), log=self.lines.append)

    def assert_never_reset(self, fake):
        for argv in fake.calls:
            self.assertNotIn("reset", argv)
            self.assertNotIn("--set-path", " ".join(argv))
            self.assertNotEqual(argv[:1], ["sudo"])

    def test_443_free_takes_443_and_records_it_as_ours(self):
        fake = self.fake()
        result = self.ensure(fake)
        self.assertEqual((result["state"], result["port"], result["owner"]), ("on", 443, "omodachi"))
        self.assertEqual(result["url"], f"https://{NAME}/odomo/")
        self.assertEqual(fake.serve, web(443, OURS))
        self.assertIn(["serve", "--bg", "--yes", "--https=443", OURS], fake.calls)
        record = odomo_serve.read_state(self.home)
        self.assertEqual((record["port"], record["owner"], record["dns_name"]), (443, "omodachi", NAME))
        self.assertEqual(stat.S_IMODE(odomo_serve.state_path(self.home).stat().st_mode), 0o600)
        # Idempotent: a second Install changes nothing and keeps it ours.
        calls = len(fake.calls)
        self.assertEqual(self.ensure(fake)["owner"], "omodachi")
        self.assertFalse([argv for argv in fake.calls[calls:] if argv[:2] == ["serve", "--bg"]])
        self.assert_never_reset(fake)

    def test_443_taken_falls_back_to_8443_and_leaves_443_alone(self):
        users = web(443, "http://127.0.0.1:3000")
        fake = self.fake(users)
        result = self.ensure(fake)
        self.assertEqual((result["state"], result["port"], result["url"]), ("on", 8443, f"https://{NAME}:8443/odomo/"))
        self.assertEqual(fake.serve, merged(users, web(8443, OURS)))
        self.assert_never_reset(fake)

    def test_a_tcp_forward_on_443_counts_as_taken(self):
        fake = self.fake({"TCP": {"443": {"TCPForward": "127.0.0.1:443"}}})
        self.assertEqual(self.ensure(fake)["port"], 8443)

    def test_both_taken_does_nothing_and_says_how(self):
        users = merged(web(443, "http://127.0.0.1:3000"), web(8443, "http://127.0.0.1:4000"))
        fake = self.fake(users)
        result = self.ensure(fake)
        self.assertEqual((result["state"], result["reason"]), ("off", "ports_taken"))
        self.assertIn("tailscale serve --bg --https=10443 https+insecure://127.0.0.1:8099", result["command"])
        self.assertEqual(fake.serve, users)
        self.assertFalse([argv for argv in fake.calls if argv[:2] == ["serve", "--bg"]])
        self.assertIsNone(odomo_serve.read_state(self.home))

    def test_not_the_operator_prints_the_fix_and_runs_no_sudo(self):
        fake = self.fake(mode="denied")
        result = self.ensure(fake)
        self.assertEqual((result["state"], result["reason"], result["command"]),
                         ("off", "operator_required", "sudo tailscale set --operator=$USER"))
        self.assertTrue(any("sudo tailscale set --operator=$USER" in line for line in self.lines))
        self.assert_never_reset(fake)

    def test_tailscale_absent_or_stopped(self):
        fake = self.fake(backend="Stopped")
        self.assertEqual(self.ensure(fake)["reason"], "tailscale_not_running")
        absent = odomo_serve.Tailscale(which=lambda name: None)
        self.assertEqual(odomo_serve.ensure(self.home, tailscale=absent)["reason"], "tailscale_not_installed")

    def test_a_serve_that_hangs_is_cut_off(self):
        fake = self.fake(mode="hang")
        with mock.patch.object(odomo_serve, "SERVE_TIMEOUT", 1.0):
            result = self.ensure(fake)
        self.assertEqual((result["state"], result["reason"]), ("off", "serve_timeout"))
        self.assertIsNone(odomo_serve.read_state(self.home))

    def test_an_identical_mapping_already_there_is_used_but_never_removed(self):
        fake = self.fake(web(443, OURS))
        result = self.ensure(fake)
        self.assertEqual((result["state"], result["owner"]), ("on", "user"))
        self.assertEqual(self.remove(fake)["reason"], "not_ours")
        self.assertEqual(fake.serve, web(443, OURS))
        self.assertIsNone(odomo_serve.read_state(self.home))

    def test_remove_turns_off_only_ours_and_a_users_other_mapping_survives(self):
        users = web(443, "http://127.0.0.1:3000")
        fake = self.fake(users)
        self.ensure(fake)
        result = self.remove(fake)
        self.assertEqual(result, {"removed": True, "port": 8443})
        self.assertEqual(fake.serve, users)
        self.assertIn(["serve", "--https=8443", "off"], fake.calls)
        self.assertIsNone(odomo_serve.read_state(self.home))
        self.assert_never_reset(fake)

    def test_remove_leaves_a_mapping_somebody_changed(self):
        fake = self.fake()
        self.ensure(fake)
        state = json.loads(fake.state.read_text())
        state["serve"] = web(443, "http://127.0.0.1:5000")
        fake.state.write_text(json.dumps(state))
        result = self.remove(fake)
        self.assertEqual(result["reason"], "changed")
        self.assertEqual(fake.serve, web(443, "http://127.0.0.1:5000"))
        self.assertFalse([argv for argv in fake.calls if argv[-1:] == ["off"]])

    def test_remove_without_a_record_or_when_it_is_already_gone(self):
        fake = self.fake(web(443, OURS))
        self.assertEqual(self.remove(fake)["reason"], "no_record")
        self.assertEqual(fake.serve, web(443, OURS))
        fake2 = self.fake()
        self.ensure(fake2)
        fake2.state.write_text(json.dumps({"name": NAME, "serve": {}}))
        self.assertEqual(self.remove(fake2)["reason"], "already_gone")

    def test_status_and_the_daemon_s_view(self):
        fake = self.fake()
        self.assertEqual(odomo_serve.status(self.home, tailscale=fake.tool())["reason"], "not_enabled")
        self.ensure(fake)
        status = odomo_serve.status(self.home, tailscale=fake.tool())
        self.assertEqual((status["enabled"], status["odomo_url"]), (True, f"https://{NAME}/odomo/"))
        current = odomo_serve.Current(self.home)
        self.assertEqual((current.url(), current.origin()), (f"https://{NAME}/odomo/", f"https://{NAME}"))
        fake.state.write_text(json.dumps({"name": NAME, "serve": {}}))
        self.assertEqual(odomo_serve.status(self.home, tailscale=fake.tool())["reason"], "mapping_missing")
        odomo_serve.clear_state(self.home)
        self.assertIsNone(current.url())

    def test_a_record_that_is_not_ours_to_trust_is_ignored(self):
        path = odomo_serve.state_path(self.home)
        path.parent.mkdir(parents=True)
        for record in ({"port": 80, "target": OURS, "dns_name": NAME, "owner": "omodachi"},
                       {"port": 443, "target": "http://evil", "dns_name": NAME, "owner": "omodachi"},
                       {"port": 443, "target": OURS, "dns_name": "evil.example", "owner": "omodachi"}):
            path.write_text(json.dumps(record))
            self.assertIsNone(odomo_serve.read_state(self.home), record)
        path.unlink()
        other = self.home / "elsewhere.json"
        other.write_text(json.dumps({"port": 443, "target": OURS, "dns_name": NAME, "owner": "omodachi"}))
        path.symlink_to(other)
        self.assertIsNone(odomo_serve.read_state(self.home))


class InstallerTests(unittest.TestCase):
    """Install adds the mapping; --remove takes back only its own; the CLI says where Odomo is."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.fake = FakeTailscale(Path(self.temp.name), web(443, "http://127.0.0.1:3000"))
        spec = importlib.util.spec_from_file_location("install_host_odomo", ROOT / "scripts/install_host.py")
        self.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.installer)
        patches = [mock.patch.dict(os.environ, {**self.fake.env,
                                                "PATH": str(self.fake.bin) + os.pathsep + os.environ["PATH"]}),
                   mock.patch.object(Path, "home", return_value=self.home)]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_install_step_then_remove_step(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = self.installer.ensure_tailscale_serve(ROOT)
        self.assertEqual((result["state"], result["port"]), ("on", 8443))
        self.assertIn(f"Odomo: https://{NAME}:8443/odomo/", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            removed = self.installer._odomo_serve(ROOT).remove(self.home)
        self.assertEqual(removed, {"removed": True, "port": 8443})
        self.assertEqual(self.fake.serve, web(443, "http://127.0.0.1:3000"))

    def test_install_calls_it_last_and_remove_local_calls_remove(self):
        import ast
        tree = ast.parse((ROOT / "scripts/install_host.py").read_text())
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        install = ast.unparse(functions["install_local"])
        self.assertIn("ensure_tailscale_serve(source)", install)
        self.assertGreater(install.index("ensure_tailscale_serve(source)"), install.index("'restart'"))
        remove = ast.unparse(functions["remove_local"])
        self.assertIn("_odomo_serve(ROOT).remove(home", remove)
        # Before the sources it imports from are deleted (found on the VM).
        self.assertLess(remove.index("_odomo_serve(ROOT).remove(home"), remove.index("shutil.rmtree(source"))

    def test_purge_rules_name_the_new_files(self):
        rules = self.installer.PURGE_RULES
        config, state = rules[".config/omodachi"], rules[".local/state/omodachi"]
        import re
        for name in ("vapid.json", ".vapid-abc_1234", "push-subscriptions.json", "push-subscriptions.json.lock",
                     ".push-subscriptions-a1b2c3d4"):
            self.assertTrue(any(re.fullmatch(pattern, name) for pattern in config), name)
        for name in ("tailscale-serve.json", ".tailscale-serve-zz99_aa1"):
            self.assertTrue(any(re.fullmatch(pattern, name) for pattern in state), name)

    def test_cli_status_enable_disable(self):
        from omodachi_core import cli
        def run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.host_main(["odomo", *argv])
            return code, json.loads(out.getvalue())
        code, value = run("status")
        self.assertEqual((code, value["result"]["reason"]), (0, "not_enabled"))
        code, value = run("enable")
        self.assertEqual((code, value["result"]["url"]), (0, f"https://{NAME}:8443/odomo/"))
        code, value = run("status")
        self.assertEqual(value["result"]["odomo_url"], f"https://{NAME}:8443/odomo/")
        code, value = run("disable")
        self.assertEqual((code, value["result"]["removed"]), (0, True))
        self.assertEqual(self.fake.serve, web(443, "http://127.0.0.1:3000"))


class IdentityTests(unittest.TestCase):
    def test_odomo_url_is_in_roads_and_the_claim_never_in_health(self):
        identity = HostIdentity(host_id="0" * 32, host_name="omodachi-lab", odomo=lambda: f"https://{NAME}/odomo/")
        identity.fingerprint = lambda: "ab" * 32
        with mock.patch("omodachi_core.host_identity.host_addresses", return_value=["192.0.2.10"]):
            self.assertEqual(identity.roads()["odomo_url"], f"https://{NAME}/odomo/")
            self.assertEqual(identity.descriptor()["odomo_url"], f"https://{NAME}/odomo/")
        self.assertNotIn("odomo_url", identity.health())
        identity.odomo = lambda: None
        with mock.patch("omodachi_core.host_identity.host_addresses", return_value=["192.0.2.10"]):
            self.assertNotIn("odomo_url", identity.roads())
        identity.odomo = lambda: "http://not-https/odomo/"
        self.assertIsNone(identity.odomo_url())

    def test_odomo_object_has_the_url_and_the_push_count_only(self):
        """LITE-1d: `odomo: {url, push_devices}` on roads and the claim."""
        identity = HostIdentity(host_id="0" * 32, host_name="omodachi-lab", addresses=["192.0.2.10"],
                                odomo=lambda: f"https://{NAME}/odomo/", odomo_push=lambda: 2)
        identity.fingerprint = lambda: "ab" * 32
        for document in (identity.roads(), identity.descriptor()):
            self.assertEqual(document["odomo"], {"url": f"https://{NAME}/odomo/", "push_devices": 2})
            self.assertEqual(document["odomo_url"], f"https://{NAME}/odomo/", "LITE-1c apps read this")
        self.assertNotIn("odomo", identity.health())
        identity.odomo = lambda: None
        self.assertEqual(identity.roads()["odomo"], {"url": None, "push_devices": 2})
        identity.odomo_push = lambda: None
        self.assertNotIn("odomo", identity.roads(), "knows neither half")
        identity.odomo = lambda: f"https://{NAME}/odomo/"
        self.assertEqual(identity.roads()["odomo"], {"url": f"https://{NAME}/odomo/"})
        for broken in (lambda: -1, lambda: "3", lambda: True, lambda: 1 / 0):
            identity.odomo_push = broken
            self.assertIsNone(identity.odomo_push_devices())

    def test_the_front_door_name_is_an_allowed_host(self):
        from omodachi_core import browser_gate

        class Service:
            host_identity = None
            odomo_front_door = staticmethod(lambda: {"dns_name": NAME, "port": 8443})
        with mock.patch.object(browser_gate, "_addresses", return_value=[]), \
                mock.patch.object(browser_gate, "tailscale_dns_name", return_value=None):
            self.assertIn(NAME, browser_gate.HostNames(Service()).read())


class LockTests(unittest.TestCase):
    def test_abi3_wheels_built_for_3_11_or_earlier_are_usable(self):
        spec = importlib.util.spec_from_file_location("update_host_lock", ROOT / "scripts/update_host_lock.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(module.usable("cryptography-50.0.2-cp311-abi3-manylinux_2_28_x86_64.whl"))
        self.assertTrue(module.usable("cryptography-50.0.2-cp39-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"))
        self.assertFalse(module.usable("cryptography-50.0.2-cp315-abi3.abi3t-manylinux_2_28_x86_64.whl"))
        self.assertFalse(module.usable("cryptography-50.0.2-cp314-cp314t-manylinux_2_28_x86_64.whl"))
        self.assertFalse(module.usable("cryptography-50.0.2-cp311-abi3-manylinux_2_28_aarch64.whl"))
        self.assertTrue(module.usable("cffi-2.1.1-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"))


if __name__ == "__main__":
    unittest.main()
