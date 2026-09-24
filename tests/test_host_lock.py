"""RELEASE-6: the host venv is exactly requirements/host.lock, hash-checked.

The static half runs everywhere: every pip invocation the installer makes is
hash-checked (--require-hashes) or cannot reach an index at all (--no-index),
never resolves (--no-deps), and never builds an sdist or fetches a build
backend; the lock pins every requirement to an exact version with at least one
sha256, and pyproject.toml agrees with it.

The live half builds a real venv from the lock through the installer's own
`install_venv` and compares `pip freeze --all` with the lock. It needs Linux
x86_64, CPython 3.11-3.14 and network, so it runs only when
OMODACHI_LOCK_INTEGRATION=1 (the clean VM run in RELEASE-6-report.md).
"""
import ast
import importlib.util
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("install_host_lock", ROOT / "scripts/install_host.py")
install_host = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_host)

LOCK = ROOT / "requirements/host.lock"
SOURCE = ROOT / "requirements/host.in"
_REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==(\S+)(?:\s*;\s*([^\\]+?))?\s*\\?$")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_entries(text: str | None = None) -> dict[str, dict]:
    """{name: {version, marker, hashes}} from the lock, strictly."""
    text = LOCK.read_text() if text is None else text
    entries, current = {}, None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash="):
            if current is None:
                raise AssertionError(f"host.lock:{number}: a hash with no requirement")
            digest = line.removeprefix("--hash=").rstrip(" \\")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise AssertionError(f"host.lock:{number}: not a sha256 hash: {digest}")
            current["hashes"].append(digest)
            continue
        match = _REQUIREMENT.match(line)
        if not match:
            raise AssertionError(f"host.lock:{number}: not `name==version [; marker] \\`: {raw!r}")
        name = canonical(match[1])
        if name in entries:
            raise AssertionError(f"host.lock:{number}: {name} is listed twice")
        current = entries[name] = {"version": match[2], "marker": (match[3] or "").strip(),
                                   "hashes": []}
    return entries


def applies(marker: str) -> bool:
    """The only marker the lock uses: python_version < "X.Y"."""
    if not marker:
        return True
    match = re.fullmatch(r'python_version\s*<\s*"(\d+)\.(\d+)"', marker)
    if not match:
        raise AssertionError(f"unexpected marker {marker!r}")
    return sys.version_info[:2] < (int(match[1]), int(match[2]))


class LockShapeTests(unittest.TestCase):
    def test_every_requirement_is_exact_and_carries_a_sha256(self):
        entries = lock_entries()
        self.assertGreaterEqual(len(entries), 10)
        for name, entry in entries.items():
            self.assertTrue(entry["hashes"], f"{name} has no hash")
            self.assertEqual(len(entry["hashes"]), len(set(entry["hashes"])), name)

    def test_the_lock_holds_the_build_backend_and_every_runtime_dependency(self):
        entries = lock_entries()
        for name in ("setuptools", "aiohttp", "zeroconf", "multidict", "yarl", "frozenlist",
                     "aiosignal", "attrs", "propcache", "aiohappyeyeballs", "idna", "ifaddr"):
            self.assertIn(name, entries)
        # pip is the interpreter's bundled one and is never installed from an index;
        # wheel is not needed by this setuptools; neither may sneak into the lock.
        self.assertNotIn("pip", entries)

    def test_the_lock_is_the_one_its_source_names(self):
        wanted = {}
        for line in SOURCE.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                name, _, rest = line.partition("==")
                version, _, marker = rest.partition(";")
                wanted[canonical(name)] = (version.strip(), marker.strip())
        got = {name: (entry["version"], entry["marker"]) for name, entry in lock_entries().items()}
        self.assertEqual(got, wanted)

    def test_pyproject_pins_exactly_what_the_lock_pins(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        entries = lock_entries()
        pins = list(project["build-system"]["requires"]) + list(project["project"]["dependencies"])
        self.assertTrue(pins)
        for pin in pins:
            name, operator, version = re.fullmatch(r"([A-Za-z0-9_.-]+)(==)(\S+)", pin).groups()
            self.assertEqual(entries[canonical(name)]["version"], version, pin)

    def test_a_lock_line_without_a_hash_is_rejected_by_the_parser(self):
        with self.assertRaises(AssertionError):
            lock_entries("aiohttp>=3.12\n")
        self.assertEqual(lock_entries("idna==3.20\n")["idna"]["hashes"], [])


class PipInvocationTests(unittest.TestCase):
    """Every pip call is hash-checked or offline, never resolves, never builds an sdist."""

    def commands(self):
        return install_host.pip_commands(Path("/src"), Path("/venv"))

    def test_every_pip_call_is_hash_checked_or_cannot_reach_an_index(self):
        commands = self.commands()
        self.assertEqual(len(commands), 2)
        for argv in commands:
            self.assertEqual(argv[1:3], ["-m", "pip"], argv)
            self.assertIn("--no-deps", argv)
            self.assertIn("--isolated", argv)
            self.assertIn("--disable-pip-version-check", argv)
            self.assertTrue("--require-hashes" in argv or "--no-index" in argv, argv)
            for banned in ("--upgrade", "-U", "--pre", "--index-url", "--extra-index-url",
                           "--find-links", "--user", "--target", "--prefix", "--trusted-host"):
                self.assertNotIn(banned, argv)

    def test_the_index_call_installs_only_the_lock_and_only_wheels(self):
        index = [argv for argv in self.commands() if "--no-index" not in argv]
        self.assertEqual(len(index), 1)
        argv = index[0]
        self.assertIn("--require-hashes", argv)
        self.assertIn("--only-binary=:all:", argv)
        self.assertEqual(argv[argv.index("-r") + 1], "/src/" + install_host.HOST_LOCK)
        # nothing else to install: -r is the only requirement
        self.assertEqual(argv[-2:], ["-r", "/src/" + install_host.HOST_LOCK])

    def test_core_itself_builds_with_the_locked_backend_offline(self):
        argv = self.commands()[1]
        for flag in ("--no-index", "--no-deps", "--no-build-isolation", "--check-build-dependencies"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[-1], "/src")

    def test_no_other_pip_call_exists_in_the_install_path(self):
        # Walk the code (not the comments) of every script the install runs:
        # any string mentioning pip outside PIP/pip_commands is a second pip call.
        for script in ("scripts/install_host.py", "scripts/install_wayvnc.py"):
            tree = ast.parse((ROOT / script).read_text())
            allowed = set()
            for node in ast.walk(tree):
                if (isinstance(node, ast.FunctionDef) and node.name == "pip_commands") or (
                        isinstance(node, ast.Assign)
                        and any(getattr(t, "id", None) == "PIP" for t in node.targets)):
                    allowed.update(id(child) for child in ast.walk(node))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and re.search(r"(^|[/\s])pip3?($|\s)|bin/pip|ensurepip|easy_install",
                                      node.value) and id(node) not in allowed):
                    self.fail(f"{script}:{node.lineno}: {node.value!r} looks like another pip call")

    def test_install_venv_runs_exactly_those_commands_after_a_fresh_venv(self):
        with tempfile.TemporaryDirectory() as scratch:
            source, venv = Path(scratch) / "src", Path(scratch) / "venv"
            (source / "requirements").mkdir(parents=True)
            (source / install_host.HOST_LOCK).write_text("")
            calls = []
            with mock.patch.object(install_host, "run", side_effect=lambda argv, **_: calls.append(argv)):
                install_host.install_venv(source, venv)
            self.assertEqual(calls[0], ["python3", "-m", "venv", str(venv)])
            self.assertEqual(calls[1:], install_host.pip_commands(source, venv))


class VenvConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.source = Path(self.scratch.name) / "src"
        self.venv = Path(self.scratch.name) / "venv"
        (self.source / "requirements").mkdir(parents=True)
        (self.source / install_host.HOST_LOCK).write_text("")
        (self.venv / "lib").mkdir(parents=True)
        (self.venv / "lib/old-unhashed-package").write_text("from an older installer")

    def fake(self, fail_on=None):
        def run(argv, **_):
            if argv[:3] == ["python3", "-m", "venv"]:
                Path(argv[3], "bin").mkdir(parents=True)
            if fail_on and fail_on in argv:
                raise subprocess.CalledProcessError(1, argv)
        return run

    def test_an_existing_venv_is_replaced_not_upgraded(self):
        with mock.patch.object(install_host, "run", side_effect=self.fake()):
            install_host.install_venv(self.source, self.venv)
        self.assertFalse((self.venv / "lib/old-unhashed-package").exists())
        self.assertTrue((self.venv / "bin").is_dir())
        self.assertFalse(self.venv.with_name("venv.previous").exists())

    def test_a_failed_install_puts_the_previous_venv_back(self):
        for step in ("--require-hashes", "--no-index"):
            with self.subTest(step=step), mock.patch.object(install_host, "run", side_effect=self.fake(step)):
                with self.assertRaises(subprocess.CalledProcessError):
                    install_host.install_venv(self.source, self.venv)
                self.assertEqual((self.venv / "lib/old-unhashed-package").read_text(),
                                 "from an older installer")
                self.assertFalse(self.venv.with_name("venv.previous").exists())

    def test_sources_without_a_lock_are_refused_before_anything_changes(self):
        (self.source / install_host.HOST_LOCK).unlink()
        with mock.patch.object(install_host, "run") as run, self.assertRaises(SystemExit):
            install_host.install_venv(self.source, self.venv)
        run.assert_not_called()
        self.assertTrue((self.venv / "lib/old-unhashed-package").exists())


@unittest.skipUnless(os.environ.get("OMODACHI_LOCK_INTEGRATION") == "1",
                     "set OMODACHI_LOCK_INTEGRATION=1 on a Linux x86_64 host with network")
class LiveLockTests(unittest.TestCase):
    def test_a_venv_built_from_the_lock_holds_exactly_the_lock(self):
        self.assertEqual((sys.platform, platform.machine()), ("linux", "x86_64"))
        with tempfile.TemporaryDirectory() as scratch:
            venv = Path(scratch) / "venv"
            install_host.install_venv(ROOT, venv)
            frozen = subprocess.run([str(venv / "bin/python"), "-m", "pip", "--isolated",
                                     "--disable-pip-version-check", "freeze", "--all"],
                                    capture_output=True, text=True, check=True).stdout
        got = {}
        for line in frozen.splitlines():
            # `omodachi-core @ file:///...` for the local build, `name==version` for the rest
            name, _, version = line.partition(" @ ") if " @ " in line else line.partition("==")
            got[canonical(name)] = version
        pip = got.pop("pip")
        core = got.pop("omodachi-core")
        self.assertTrue(core.startswith("file://"), core)
        want = {name: entry["version"] for name, entry in lock_entries().items()
                if applies(entry["marker"])}
        print(f"\npip freeze --all: pip=={pip} (bundled), omodachi-core=={core}, "
              f"{len(got)} locked: " + ", ".join(f"{n}=={v}" for n, v in sorted(got.items())))
        self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
