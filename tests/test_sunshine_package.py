"""INSTALL-1: installing the managed Sunshine fork from a built archive.

The archive is a GPL-3.0 binary that arrives over the network and is unpacked
into the user's home, so most of what is worth locking here is refusal: a
checksum that does not match, a member outside the one directory, a symlink,
a manifest line that no longer describes the file beside it.

The rest is the 2026-09-20 host incident, in three parts: the unit has to be
*enabled* (not just written), it has to carry a WorkingDirectory because the
packaged binary resolves its shaders relative to one, and a machine with no
VAAPI render node has to be told it will encode in software rather than find
out from a `libx264` line in a status response.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from omodachi_core import sunshine_package as package


def build_archive(path: Path, *, sha="abc1234", manifest=True, extra=None,
                  prefix=None, source="commit=" + "a" * 40):
    prefix = prefix or f"omodachi-sunshine-{sha}-x86_64"
    files = {"sunshine": b"#!/bin/sh\nexit 0\n",
             "assets/shaders/opengl/Scene.frag": b"void main() {}\n",
             "LICENSE": b"GPL-3.0\n", "NOTICE": b"upstream\n",
             "DEPENDS": b"libva\nlibdrm\n", "BUNDLED": b"",
             "SOURCE": source.encode() + b"\n"}
    files.update(extra or {})
    if manifest:
        import hashlib
        lines = "".join(f"{hashlib.sha256(body).hexdigest()}  {name}\n"
                        for name, body in sorted(files.items()))
        files["MANIFEST.sha256"] = lines.encode()
    with tarfile.open(path, "w:gz") as archive:
        for name, body in sorted(files.items()):
            info = tarfile.TarInfo(prefix + "/" + name)
            info.size = len(body)
            info.mode = 0o755 if name == "sunshine" else 0o644
            archive.addfile(info, io.BytesIO(body))
    return path


class Recorder:
    """Stands in for subprocess.run and remembers every argv it was given."""

    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        key = " ".join(argv)
        for pattern, answer in self.answers.items():
            if pattern in key:
                return subprocess.CompletedProcess(argv, *answer)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def argv_containing(self, needle):
        return [call for call in self.calls if needle in " ".join(call)]


class ArchiveShapeTests(unittest.TestCase):
    def test_a_checksum_that_does_not_match_stops_the_install(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            archive = build_archive(home / "package.tar.gz")
            with self.assertRaises(package.SunshinePackageError) as caught:
                package.install(str(archive), home, sha256="0" * 64, runner=Recorder())
            self.assertEqual(caught.exception.code, "sunshine_package_checksum_mismatch")
            self.assertFalse((home / package.INSTALL_ROOT).exists())

    def test_a_member_outside_the_one_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            path = home / "evil.tar.gz"
            with tarfile.open(path, "w:gz") as archive:
                for name in ("omodachi-sunshine-abc1234-x86_64/sunshine", "../../etc/passwd"):
                    info = tarfile.TarInfo(name)
                    info.size = 1
                    archive.addfile(info, io.BytesIO(b"x"))
            with self.assertRaises(package.SunshinePackageError) as caught:
                package.unpack(path, home)
            self.assertEqual(caught.exception.code, "sunshine_package_shape")

    def test_a_symlink_is_refused_even_inside_the_directory(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            path = home / "link.tar.gz"
            with tarfile.open(path, "w:gz") as archive:
                info = tarfile.TarInfo("omodachi-sunshine-abc1234-x86_64/sunshine")
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
                link = tarfile.TarInfo("omodachi-sunshine-abc1234-x86_64/key")
                link.type, link.linkname = tarfile.SYMTYPE, "/home/someone/.ssh/id_ed25519"
                archive.addfile(link)
            with self.assertRaises(package.SunshinePackageError) as caught:
                package.unpack(path, home)
            self.assertEqual(caught.exception.code, "sunshine_package_unexpected_member")

    def test_a_directory_name_that_is_not_the_published_shape_is_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            archive = build_archive(home / "p.tar.gz", prefix="sunshine")
            with self.assertRaises(package.SunshinePackageError) as caught:
                package.unpack(archive, home)
            self.assertEqual(caught.exception.code, "sunshine_package_shape")

    def test_a_tampered_file_is_caught_by_the_manifest_not_by_the_archive(self):
        # The archive checksum only says the download arrived intact. The
        # manifest is what says the binary and its shaders belong together.
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            archive = build_archive(home / "p.tar.gz")
            unpacked = package.unpack(archive, home)
            directory = Path(unpacked["directory"])
            self.assertEqual(package.verify_manifest(directory), [])
            (directory / "assets/shaders/opengl/Scene.frag").write_text("tampered")
            self.assertEqual(package.verify_manifest(directory),
                             ["assets/shaders/opengl/Scene.frag"])

    def test_an_archive_with_no_manifest_is_unverifiable_and_says_so(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            archive = build_archive(home / "p.tar.gz", manifest=False)
            directory = Path(package.unpack(archive, home)["directory"])
            with self.assertRaises(package.SunshinePackageError) as caught:
                package.verify_manifest(directory)
            self.assertEqual(caught.exception.code, "sunshine_package_unverifiable")

    def test_reinstalling_the_same_build_replaces_the_directory_rather_than_merging(self):
        # A stale shader from an older tree beside a newer binary is invisible:
        # the fork logs a compile error and streams on the CPU.
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            archive = build_archive(home / "p.tar.gz")
            directory = Path(package.unpack(archive, home)["directory"])
            (directory / "assets/shaders/opengl/Stale.frag").write_text("old")
            package.unpack(archive, home)
            self.assertFalse((directory / "assets/shaders/opengl/Stale.frag").exists())
            self.assertEqual(package.verify_manifest(directory), [])


class EncoderTests(unittest.TestCase):
    def test_a_machine_with_no_render_node_gets_no_vaapi_argument(self):
        # A virtual machine with virtio-gpu is this case, and naming a VAAPI
        # adapter there does not fail - it streams on the CPU and says nothing.
        self.assertEqual(package.encoder_arguments(nodes=[]), ["capture=wlr"])

    def test_an_explicit_adapter_is_taken_without_probing_anything(self):
        def never(node):
            raise AssertionError("must not probe when the adapter is given")
        self.assertEqual(package.encoder_arguments(adapter="/dev/dri/renderD129", probe=never),
                         ["capture=wlr", "encoder=vaapi", "adapter_name=/dev/dri/renderD129"])

    def test_the_node_that_answers_for_h264_is_the_one_named(self):
        probed = []

        def probe(node):
            probed.append(str(node))
            return str(node).endswith("129")
        self.assertEqual(
            package.encoder_arguments(nodes=["/dev/dri/renderD128", "/dev/dri/renderD129"],
                                      probe=probe),
            ["capture=wlr", "encoder=vaapi", "adapter_name=/dev/dri/renderD129"])
        self.assertEqual(probed, ["/dev/dri/renderD128", "/dev/dri/renderD129"])

    def test_nodes_that_all_refuse_leave_the_choice_to_the_fork(self):
        self.assertEqual(package.encoder_arguments(nodes=["/dev/dri/renderD128"],
                                                   probe=lambda node: False),
                         ["capture=wlr"])


class UnitTests(unittest.TestCase):
    def test_the_unit_carries_the_working_directory_the_assets_need(self):
        # The packaged binary has the *relative* "assets" compiled in, so this
        # line is what makes the shaders findable at all.
        text = package.unit_text(Path("/home/u/.local/share/omodachi/sunshine/abc1234"),
                                 ["capture=wlr"])
        self.assertIn("WorkingDirectory=/home/u/.local/share/omodachi/sunshine/abc1234\n", text)
        self.assertIn("ExecStart=/home/u/.local/share/omodachi/sunshine/abc1234/sunshine capture=wlr\n", text)
        self.assertIn("Environment=SUNSHINE_MANAGED_LOCAL_PAIRING=1\n", text)
        self.assertIn("WantedBy=graphical-session.target\n", text)

    def test_a_host_with_no_sunshine_package_gets_the_whole_unit(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            runner = Recorder({"systemctl --user cat": (1, "", "not found")})
            result = package.ensure_unit(home, home / "install", arguments=["capture=wlr"],
                                         runner=runner)
            unit = home / package.UNIT_DIR / package.SUNSHINE_UNIT
            self.assertTrue(unit.is_file())
            self.assertFalse(result["packaged_unit"])
            self.assertIn("WorkingDirectory=", unit.read_text())

    def test_a_host_that_already_has_the_packaged_unit_only_gets_a_drop_in(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            runner = Recorder({"systemctl --user cat": (
                0, "# /usr/lib/systemd/user/app-dev.lizardbyte.app.Sunshine.service\n[Service]\n", "")})
            result = package.ensure_unit(home, home / "install", arguments=["capture=wlr"],
                                         runner=runner)
            self.assertTrue(result["packaged_unit"])
            dropin = home / package.UNIT_DIR / (package.SUNSHINE_UNIT + ".d") / package.DROPIN_NAME
            self.assertTrue(dropin.is_file())
            self.assertFalse((home / package.UNIT_DIR / package.SUNSHINE_UNIT).exists())
            self.assertIn("ExecStart=\n", dropin.read_text())

    def test_the_unit_is_enabled_and_not_only_written(self):
        # 2026-09-20: the binary was there, the drop-in was there, and nothing
        # had ever run `enable`. Remote worked until the machine rebooted.
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            runner = Recorder({"systemctl --user cat": (1, "", "")})
            package.ensure_unit(home, home / "install", arguments=["capture=wlr"], runner=runner)
            self.assertTrue(runner.argv_containing("enable " + package.SUNSHINE_UNIT))
            self.assertTrue(runner.argv_containing("daemon-reload"))


class DependencyTests(unittest.TestCase):
    def test_a_host_that_already_has_everything_never_reaches_for_sudo(self):
        runner = Recorder()
        result = package.install_packages(["libva", "libdrm"], runner=runner)
        self.assertEqual(result["reason"], "already_present")
        self.assertEqual(runner.argv_containing("sudo"), [])

    def test_only_the_absent_packages_are_installed(self):
        runner = Recorder({"pacman -Qq libva": (1, "", "not found")})
        result = package.install_packages(["libva", "libdrm"], runner=runner)
        self.assertEqual(result["installed"], ["libva"])
        command = runner.calls[-1]
        self.assertIn("libva", command)
        self.assertNotIn("libdrm", command)

    def test_a_refused_package_install_is_reported_with_the_command_to_run(self):
        runner = Recorder({"pacman -Qq libva": (1, "", ""),
                           "pacman -S": (1, "", "you cannot do that")})
        result = package.install_packages(["libva"], runner=runner)
        self.assertEqual(result["reason"], "package_install_failed")
        self.assertIn("libva", result["command"])
        self.assertIn("you cannot do that", result["detail"])


class RemovalTests(unittest.TestCase):
    def test_removal_takes_back_our_install_and_leaves_a_hand_built_one(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            archive = build_archive(home / "p.tar.gz")
            package.unpack(archive, home)
            handmade = home / package.INSTALL_ROOT / "deadbee"
            handmade.mkdir(parents=True)
            (handmade / "sunshine").write_text("a developer's own build")
            runner = Recorder({"systemctl --user cat": (1, "", "")})
            package.ensure_unit(home, home / package.INSTALL_ROOT / "abc1234",
                                arguments=["capture=wlr"], runner=runner)
            removed = package.remove(home, runner=runner)
            self.assertEqual(removed["installs"], ["abc1234"])
            self.assertTrue(removed["unit"])
            self.assertTrue((handmade / "sunshine").is_file())
            self.assertTrue(runner.argv_containing("disable --now"))

    def test_a_unit_somebody_else_wrote_is_not_ours_to_delete(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            unit = home / package.UNIT_DIR / package.SUNSHINE_UNIT
            unit.parent.mkdir(parents=True)
            unit.write_text("[Service]\nExecStart=/usr/bin/sunshine\n")
            removed = package.remove(home, runner=Recorder())
            self.assertFalse(removed["unit"])
            self.assertTrue(unit.is_file())


class InstallTests(unittest.TestCase):
    def test_the_whole_path_from_a_local_archive_to_an_enabled_unit(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            archive = build_archive(home / "p.tar.gz")
            runner = Recorder({"systemctl --user cat": (1, "", ""),
                               "systemctl --user is-active": (0, "active\n", "")})
            result = package.install(str(archive), home, sha256=package.digest(archive),
                                     runner=runner, adapter="/dev/dri/renderD129")
            self.assertTrue(result["checksum_pinned"])
            self.assertEqual(result["sha"], "abc1234")
            self.assertEqual(result["source"]["commit"], "a" * 40)
            self.assertEqual(result["unit"]["active"], "active")
            self.assertTrue(result["unit"]["enabled"])
            installed = Path(result["directory"])
            self.assertTrue((installed / "assets/shaders/opengl/Scene.frag").is_file())
            self.assertTrue((installed / "LICENSE").is_file())
            self.assertEqual(installed.parent.name, "sunshine")

    def test_an_archive_with_no_sha256_is_refused_before_it_is_fetched(self):
        # RELEASE-6: there is no "unpinned" install any more.
        def opener(url, timeout=None):
            raise AssertionError(f"fetched {url} with nothing to check it against")
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            for sha256 in (None, "", "not-a-sha", "a" * 63):
                with self.assertRaises(package.SunshinePackageError) as caught:
                    package.install("https://example.invalid/x.tar.zst", home, sha256=sha256,
                                    opener=opener, runner=Recorder())
                self.assertEqual(caught.exception.code, "sunshine_package_sha256_required")
            self.assertFalse((home / ".local/share/omodachi/sunshine").exists())

    def test_a_sidecar_beside_the_archive_is_never_trusted(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            archive = build_archive(home / "p.tar.gz")
            # The publisher's own .sha256 matches - and still counts for nothing.
            (home / "p.tar.gz.sha256").write_text(package.digest(archive) + "  p.tar.gz\n")
            with self.assertRaises(package.SunshinePackageError) as caught:
                package.install(str(archive), home, runner=Recorder())
            self.assertEqual(caught.exception.code, "sunshine_package_sha256_required")
            self.assertFalse(hasattr(package, "sidecar_sha256"))

    def test_an_archive_that_does_not_hash_to_the_given_sha256_is_not_unpacked(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            archive = build_archive(home / "p.tar.gz")
            with self.assertRaises(package.SunshinePackageError) as caught:
                package.install(str(archive), home, sha256="0" * 64, runner=Recorder())
            self.assertEqual(caught.exception.code, "sunshine_package_checksum_mismatch")
            self.assertFalse((home / ".local/share/omodachi/sunshine").exists())

    def test_a_path_that_does_not_exist_names_itself(self):
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaises(package.SunshinePackageError) as caught:
                package.fetch(str(Path(scratch) / "nope.tar.zst"), Path(scratch))
            self.assertEqual(caught.exception.code, "sunshine_package_missing")

    def test_a_download_that_cannot_be_reached_names_the_url(self):
        def opener(url, timeout=None):
            raise OSError("no route to host")
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaises(package.SunshinePackageError) as caught:
                package.fetch("https://example.invalid/x.tar.zst", Path(scratch), opener=opener)
            self.assertEqual(caught.exception.code, "sunshine_package_unreachable")
            self.assertIn("example.invalid", caught.exception.detail)


class PinTests(unittest.TestCase):
    """CORE-2 §4: a core release installs the fork build it pins, not `releases/latest`."""

    def test_the_default_is_the_pinned_versioned_asset_with_its_sha256(self):
        pin = package.pinned()
        self.assertNotIn("/releases/latest/", pin["url"])
        self.assertIn("/releases/download/", pin["url"])
        self.assertIn(pin["version"], pin["url"])
        self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")
        choice = package.choose(environ={})
        self.assertEqual((choice["source"], choice["spec"], choice["sha256"], choice["version"]),
                         ("pin", pin["url"], pin["sha256"], pin["version"]))

    def test_latest_is_only_ever_asked_for_by_name_and_with_a_sha256(self):
        choice = package.choose("latest", "b" * 64, environ={})
        self.assertEqual((choice["source"], choice["spec"], choice["sha256"]),
                         ("latest", package.LATEST_PACKAGE, "b" * 64))
        self.assertEqual(package.choose(environ={package.PACKAGE_ENV: "latest",
                                                 package.SHA256_ENV: "b" * 64})["source"], "latest")

    def test_a_flag_or_the_environment_overrides_the_pin_only_with_a_sha256(self):
        self.assertEqual(package.choose("/tmp/x.tar.zst", "F" * 64, environ={}),
                         {"source": "explicit", "spec": "/tmp/x.tar.zst", "sha256": "f" * 64,
                          "version": None, "satisfied_by": []})
        staged = package.choose(environ={package.PACKAGE_ENV: "http://jump/x.tar.zst",
                                         package.SHA256_ENV: "c" * 64})
        self.assertEqual((staged["source"], staged["sha256"]), ("explicit", "c" * 64))

    def test_an_override_without_a_sha256_is_refused(self):
        # RELEASE-6: flag, environment and `latest` alike; no sidecar, no fallback.
        for spec, environ in (("https://example.invalid/x.tar.zst", {}), ("/tmp/x.tar.zst", {}),
                              ("latest", {}), (None, {package.PACKAGE_ENV: "http://jump/x.tar.zst"}),
                              (None, {package.PACKAGE_ENV: "latest"})):
            with self.subTest(spec=spec, environ=environ), \
                    self.assertRaises(package.SunshinePackageError) as caught:
                package.choose(spec, environ=environ)
            self.assertEqual(caught.exception.code, "sunshine_package_sha256_required")
            self.assertIn("--sunshine-sha256", caught.exception.detail)

    def test_a_malformed_sha256_is_refused(self):
        for bad in ("abc", "g" * 64, "a" * 65):
            with self.subTest(bad=bad), self.assertRaises(package.SunshinePackageError) as caught:
                package.choose("/tmp/x.tar.zst", bad, environ={})
            self.assertEqual(caught.exception.code, "sunshine_package_sha256_invalid")

    def test_a_second_checksum_cannot_loosen_the_pin(self):
        pin = package.pinned()
        self.assertEqual(package.choose(None, pin["sha256"], environ={})["sha256"], pin["sha256"])
        with self.assertRaises(package.SunshinePackageError) as caught:
            package.choose(None, "d" * 64, environ={})
        self.assertEqual(caught.exception.code, "sunshine_package_sha256_conflict")

    def test_a_pin_that_points_at_latest_or_has_no_checksum_is_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            table = Path(scratch) / "versions.json"
            for entry in ({"version": "328d231", "url": package.LATEST_PACKAGE, "sha256": "a" * 64},
                          {"version": "328d231", "url": "https://x/releases/download/t/a.tar.zst", "sha256": ""},
                          {"version": "main", "url": "https://x/releases/download/t/a.tar.zst", "sha256": "a" * 64}):
                table.write_text(json.dumps({"sunshine_package": entry}))
                with self.assertRaises(package.SunshinePackageError) as refused:
                    package.pinned(table)
                self.assertEqual(refused.exception.code, "sunshine_package_pin_invalid")

    def show(self, home, directory, *, enabled="enabled"):
        binary = home / package.INSTALL_ROOT / directory / "sunshine"
        return Recorder({
            "show -p ExecStart": (0, "{ path=%s ; argv[]=%s capture=wlr ; ignore_errors=no }\n" % (binary, binary), ""),
            "is-enabled": (0, enabled + "\n", ""), "is-active": (0, "active\n", "")})

    def test_the_pin_is_the_first_fork_that_serves_hevc(self):
        # STREAM-1b: 328d231 accepts an HEVC profile and reports
        # desktop.status.encoders. Nothing before it is the same runtime, so
        # nothing before it may stand in for it.
        pin = package.pinned()
        self.assertEqual((pin["version"], pin["satisfied_by"]), ("328d231", []))
        self.assertEqual(pin["sha256"], "0f5a8f0b4e654bde7bac3e82fc878c888b4d5f3011a2d6416a6f067b6dc565d9")
        self.assertTrue(pin["url"].endswith(
            "/releases/download/sunshine-328d231/omodachi-sunshine-328d231-x86_64.tar.zst"))

    def test_leos_hand_written_drop_in_is_found_and_only_the_pinned_build_satisfies(self):
        # The drop-in shape Leo's host has carried since SPEC-B1: same file
        # name as the installer's, no marker. It is read like any other.
        for directory_name, satisfied in (("328d231", True), ("17c6043", False), ("e58627a", False)):
            with tempfile.TemporaryDirectory() as scratch:
                home = Path(scratch)
                directory = home / package.INSTALL_ROOT / directory_name
                directory.mkdir(parents=True)
                (directory / "sunshine").write_text("#!/bin/sh\n")
                dropin = home / package.UNIT_DIR / (package.SUNSHINE_UNIT + ".d") / package.DROPIN_NAME
                dropin.parent.mkdir(parents=True)
                dropin.write_text("[Service]\nExecStart=\nExecStart=%h/.local/share/omodachi/sunshine/"
                                  + directory_name + "/sunshine\n")
                found = package.installed_fork(home, runner=self.show(home, directory_name))
                self.assertEqual((found["version"], found["written_by_installer"], found["active"]),
                                 (directory_name, False, "active"))
                self.assertEqual(package.satisfies(found["version"], package.choose(environ={})),
                                 satisfied, directory_name)

    def test_satisfied_by_still_lets_a_listed_commit_stand_in_for_the_pin(self):
        choice = {"source": "pin", "version": "328d231", "satisfied_by": ["17c6043"]}
        self.assertTrue(package.satisfies("17c6043", choice))
        self.assertTrue(package.satisfies("17c6043" + "0" * 33, choice))
        self.assertFalse(package.satisfies("e58627a", choice))

    def test_a_release_install_reports_the_commit_in_its_source_file(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            directory = home / package.INSTALL_ROOT / "328d231"
            directory.mkdir(parents=True)
            (directory / "sunshine").write_text("#!/bin/sh\n")
            (directory / "SOURCE").write_text("name=x\ncommit=328d2313c92dc4db675a8eafe96a7e32460c2758\n")
            found = package.installed_fork(home, runner=self.show(home, "328d231"))
            self.assertEqual(found["version"], "328d2313c92dc4db675a8eafe96a7e32460c2758")
            self.assertTrue(package.satisfies(found["version"], package.choose(environ={})))

    def test_what_does_not_satisfy_the_pin(self):
        pin = package.choose(environ={})
        for version in ("a2fd635", "17c6043", "e58627a", "328d231-dirty", "", "328d2"):
            self.assertFalse(package.satisfies(version, pin), version)
        # Only the pin may be skipped for: an explicit archive is always installed.
        self.assertFalse(package.satisfies("328d231", package.choose("latest", "e" * 64, environ={})))

    def test_a_unit_that_runs_something_else_is_not_a_managed_fork(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            runner = Recorder({"show -p ExecStart": (0, "{ path=/usr/bin/sunshine ; argv[]=/usr/bin/sunshine }\n", "")})
            self.assertIsNone(package.installed_fork(home, runner=runner))
            self.assertIsNone(package.installed_fork(home, runner=self.show(home, "missing")))  # binary absent
            self.assertIsNone(package.installed_fork(home, runner=Recorder({"show": (1, "", "no unit")})))


class OwnerTests(unittest.TestCase):
    def test_every_public_url_here_hangs_off_the_one_owner_constant(self):
        self.assertEqual(package.GITHUB_OWNER, "omodachi")
        self.assertTrue(package.LATEST_PACKAGE.startswith(
            "https://github.com/" + package.GITHUB_OWNER + "/omodachi-sunshine/releases/"))
        self.assertTrue(package.pinned()["url"].startswith(package.SUNSHINE_REPOSITORY + "/releases/"))
        self.assertNotIn("github.com", package.VERSIONS.read_text())
        body = Path(package.__file__).read_text()
        self.assertNotIn("github.com/2nd1st", body)
        for line in body.splitlines():
            if "https://github.com/" in line and "GITHUB_OWNER" not in line \
                    and "SUNSHINE_REPOSITORY" not in line:
                self.fail("hard-coded owner in: " + line.strip())


if __name__ == "__main__":
    unittest.main()
