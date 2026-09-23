"""Bootstrap wires exactly one Remote manager, or none at all."""
from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.remote import RemoteManager
from omodachi_core.remote.backends import SunshineBackend, VncBackend
from omodachi_core.remote.hyprland import Hyprland
from omodachi_core.remote.profile import EncoderLimits
from tests.remote_fakes import FakeCompositor, FakeSunshine, FakeWayVNC, INSTANCE


class BootstrapRemoteAssemblyTests(unittest.TestCase):
    def manager(self, root):
        return RemoteManager(hyprland=Hyprland(INSTANCE, runner=FakeCompositor()),
                             journal_dir=Path(root) / "remote",
                             encoder=EncoderLimits(4096, 4096, 16_777_216, 60, 40_000, 2, 2),
                             sunshine=SunshineBackend(FakeSunshine()), vnc=VncBackend(FakeWayVNC.factory()))

    def test_the_injected_manager_is_the_one_the_service_serves(self):
        with tempfile.TemporaryDirectory() as root:
            manager = self.manager(root)
            service = create_service(Hub(), demo=True, remote_manager=manager)
            self.assertIs(service.remote.manager, manager)

    def test_the_sunshine_backend_learns_the_paired_certificate_resolver(self):
        with tempfile.TemporaryDirectory() as root:
            manager = self.manager(root)
            service = create_service(Hub(), demo=True, remote_manager=manager)
            self.assertEqual(manager.backends["sunshine"].certificate_resolver, service.remote_certificate)

    def test_without_a_manager_remote_is_reported_unavailable(self):
        service = create_service(Hub(), demo=True)
        self.assertIsNone(service.remote.manager)
        self.assertEqual(service.remote.unavailable_reason, "remote_runtime_unavailable")

    def test_a_non_manager_is_refused(self):
        with self.assertRaisesRegex(ValueError, "remote manager invalid"):
            create_service(Hub(), demo=True, remote_manager=object())


if __name__ == "__main__":
    unittest.main()
