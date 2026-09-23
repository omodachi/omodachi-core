"""AUTH-2: where the socket is, and what happens to the path it left behind."""
import asyncio
import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest

from omodachi_core.hub import Hub
from omodachi_core.ipc import JsonLineClient, JsonLineServer
from omodachi_core import runtime_paths


class _Dir:
    """Just enough of an `os.stat_result` for the shared-directory probe."""
    def __init__(self, uid, mode=stat.S_IFDIR | 0o700):
        self.st_uid, self.st_mode = uid, mode


def _probe(present):
    def probe(path):
        try:
            return present[str(path)]
        except KeyError:
            raise FileNotFoundError(str(path))
    return probe


class RuntimePathTests(unittest.TestCase):
    ENV = {"XDG_RUNTIME_DIR": "/run/user/1000"}
    SHARED = "/run/omodachi/1000/omodachid.sock"
    RUNTIME = "/run/user/1000/omodachi/omodachid.sock"
    LEGACY = "/home/alex/.cache/omodachi/omodachid.sock"

    def test_the_shared_directory_wins_when_the_root_step_has_made_it(self):
        # This is the only directory a `ProtectHome=yes` unit can be handed.
        probe = _probe({"/run/omodachi/1000": _Dir(1000)})
        self.assertEqual(runtime_paths.default_socket_path(self.ENV, uid=1000, probe=probe),
                         self.SHARED)

    def test_without_it_the_socket_is_in_the_runtime_directory(self):
        probe = _probe({})
        self.assertEqual(runtime_paths.default_socket_path(self.ENV, uid=1000, probe=probe),
                         self.RUNTIME)

    def test_a_shared_directory_that_is_not_ours_is_not_used(self):
        # /run/omodachi is root-owned and 0755, so nobody else can plant a
        # directory there - but if one is there, it is not ours to bind in.
        for bad in (_Dir(0), _Dir(1001), _Dir(1000, stat.S_IFREG | 0o600),
                    _Dir(1000, stat.S_IFLNK | 0o777)):
            probe = _probe({"/run/omodachi/1000": bad})
            self.assertEqual(runtime_paths.default_socket_path(self.ENV, uid=1000, probe=probe),
                             self.RUNTIME)

    def test_a_missing_or_relative_runtime_directory_falls_back_to_the_uid(self):
        # systemd would have set /run/user/<uid>; a cron job or an ssh session
        # without a logind seat has no XDG_RUNTIME_DIR at all, and a relative
        # value is not a runtime directory whatever it says.
        probe = _probe({})
        for environment in ({}, {"XDG_RUNTIME_DIR": ""}, {"XDG_RUNTIME_DIR": "   "},
                            {"XDG_RUNTIME_DIR": "run/user/1000"}):
            self.assertEqual(runtime_paths.default_socket_path(environment, uid=4242, probe=probe),
                             "/run/user/4242/omodachi/omodachid.sock")

    def test_the_legacy_path_is_the_one_auth_1_shipped(self):
        self.assertEqual(runtime_paths.legacy_socket_path("/home/alex"), self.LEGACY)

    def test_a_client_tries_the_newest_path_that_is_actually_there(self):
        def ask(present, shared_dir=True):
            probe = _probe({"/run/omodachi/1000": _Dir(1000)} if shared_dir else {})
            return runtime_paths.client_socket_path(
                self.ENV, home="/home/alex", uid=1000,
                exists=lambda path: path in present, probe=probe)

        self.assertEqual(ask({self.SHARED, self.RUNTIME, self.LEGACY}), self.SHARED)
        # The root step ran after this daemon started, so the directory is
        # there and the socket is not: the daemon is still on the old one.
        self.assertEqual(ask({self.RUNTIME, self.LEGACY}), self.RUNTIME)
        # A daemon from before AUTH-2 is still running. A client from after it
        # has to find that one rather than a path with nothing behind it.
        self.assertEqual(ask({self.LEGACY}, shared_dir=False), self.LEGACY)
        # Nothing anywhere: name where a daemon started now would bind.
        self.assertEqual(ask(set()), self.SHARED)
        self.assertEqual(ask(set(), shared_dir=False), self.RUNTIME)

    def test_only_a_socket_under_run_omodachi_gets_the_root_owned_files(self):
        self.assertTrue(runtime_paths.is_shared_socket("/run/omodachi/1000/omodachid.sock"))
        for path in (self.RUNTIME, self.LEGACY, "/run/omodachi", "relative.sock",
                     "/tmp/omodachid.sock", "/run/omodachi-other/x.sock", None):
            self.assertFalse(runtime_paths.is_shared_socket(path), path)


class CompatibilitySymlinkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.runtime = root / "run/omodachi"
        self.legacy = root / "home/.cache/omodachi/omodachid.sock"
        self.path = str(self.runtime / "omodachid.sock")

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def server(self, link=True):
        return JsonLineServer(Hub(), self.path,
                              compatibility_link=str(self.legacy) if link else None)

    async def test_the_old_path_answers_while_the_daemon_runs_and_is_gone_after(self):
        server = self.server()
        await server.start()
        try:
            self.assertTrue(os.path.islink(self.legacy))
            self.assertEqual(os.readlink(self.legacy), self.path)
            # The point of the link: an old caller holding the old path talks
            # to the same daemon.
            self.assertTrue((await JsonLineClient(str(self.legacy)).request("health"))["ok"])
        finally:
            await server.close()
        # A symlink left behind after shutdown is a path that looks like a
        # daemon and is not one.
        self.assertFalse(os.path.lexists(self.legacy))

    async def test_the_runtime_directory_is_private_and_so_is_the_socket(self):
        server = self.server()
        await server.start()
        try:
            self.assertEqual(stat.S_IMODE(os.stat(self.runtime).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.lstat(self.path).st_mode), 0o600)
        finally:
            await server.close()

    async def test_an_existing_directory_with_a_loose_mode_is_tightened(self):
        self.runtime.mkdir(parents=True)
        os.chmod(self.runtime, 0o755)
        server = self.server()
        await server.start()
        try:
            self.assertEqual(stat.S_IMODE(os.stat(self.runtime).st_mode), 0o700)
        finally:
            await server.close()

    async def test_a_stale_symlink_from_the_last_run_is_rewritten(self):
        self.legacy.parent.mkdir(parents=True)
        self.legacy.symlink_to(Path(self.temporary.name) / "somewhere-else.sock")
        server = self.server()
        await server.start()
        try:
            self.assertEqual(os.readlink(self.legacy), self.path)
        finally:
            await server.close()

    async def test_a_dead_socket_at_the_old_path_is_replaced_but_a_file_is_not(self):
        # The pre-AUTH-2 daemon's own inode, left behind by a kill -9.
        self.legacy.parent.mkdir(parents=True)
        abandoned = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        abandoned.bind(str(self.legacy))
        abandoned.listen(1)
        abandoned.close()
        server = self.server()
        await server.start()
        try:
            self.assertTrue(os.path.islink(self.legacy))
        finally:
            await server.close()

        # Anything that is not ours stays exactly as it is, and the daemon
        # starts anyway: the compatibility path is a convenience, not the
        # feature.
        blocked = Path(self.temporary.name) / "home/.cache/omodachi/regular"
        blocked.write_text("not ours")
        server = JsonLineServer(Hub(), self.path, compatibility_link=str(blocked))
        await server.start()
        try:
            self.assertEqual(blocked.read_text(), "not ours")
            self.assertTrue((await JsonLineClient(self.path).request("health"))["ok"])
        finally:
            await server.close()
        self.assertEqual(blocked.read_text(), "not ours")

    async def test_a_live_socket_at_the_old_path_is_never_taken_over(self):
        # A pre-AUTH-2 daemon really is still running there. Stealing its path
        # would make it unreachable to everything that still holds it.
        self.legacy.parent.mkdir(parents=True)
        incumbent = JsonLineServer(Hub(), str(self.legacy))
        await incumbent.start()
        server = self.server()
        await server.start()
        try:
            self.assertFalse(os.path.islink(self.legacy))
            self.assertTrue((await JsonLineClient(str(self.legacy)).request("health"))["ok"])
            self.assertTrue((await JsonLineClient(self.path).request("health"))["ok"])
        finally:
            await server.close()
            await incumbent.close()

    async def test_closing_does_not_remove_a_link_somebody_else_replaced(self):
        server = self.server()
        await server.start()
        other = str(Path(self.temporary.name) / "other.sock")
        os.unlink(self.legacy)
        os.symlink(other, self.legacy)
        await server.close()
        self.assertEqual(os.readlink(self.legacy), other)

    async def test_no_link_is_asked_for_and_none_is_made(self):
        server = self.server(link=False)
        await server.start()
        try:
            self.assertFalse(os.path.lexists(self.legacy))
        finally:
            await server.close()


if __name__ == "__main__":
    unittest.main()
