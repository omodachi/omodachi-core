from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
import unittest

from omodachi_core.agent import AgentStatus, DefaultAgentCapabilities, HerdrStatusSnapshot, ProbeStatus
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub


class StaticProbe:
    def inspect(self):
        capabilities = DefaultAgentCapabilities(
            omarchy_default_agent="codex", omarchy_probe=ProbeStatus.AVAILABLE,
            herdr_supported_kinds=frozenset({"codex"}), herdr_probe=ProbeStatus.NOT_RUNNING,
            default_agent_exists=False, default_agent_probe=ProbeStatus.MISSING,
            pane_id=None, pane_available=False, pane_probe=ProbeStatus.MISSING,
            agent_status=AgentStatus.UNKNOWN,
        )
        return capabilities, HerdrStatusSnapshot(True, False, False, frozenset({"codex"}))


class BootstrapProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = Path(__file__).parents[1] / "contracts/fixtures/catalog"
        self.omodachi_menu = Path(__file__).parents[1] / "src/omodachi_core/data/omodachi-menu.jsonc"
        self.shell = Path(__file__).parents[1] / "src/omodachi_core/data/demo-shell.json"
        self.missing_installed = Path("/tmp/omodachi-no-installed-menu-fixture")

    def test_non_demo_never_falls_back_to_copied_source_fixture(self):
        hub = Hub()
        with patch("omodachi_core.bootstrap.OMARCHY_DEFAULT_MENU", self.missing_installed):
            service = create_service(
                hub, demo=False, user_menu=Path("/tmp/omodachi-no-user-menu"),
                omodachi_menu=Path("/tmp/omodachi-no-own-menu"), shell_config=self.shell,
                agent_probe=StaticProbe(),
            )
        ids = {entry["id"] for entry in service.refresh_catalog()["entries"]}
        self.assertNotIn("learn.herdr-keybindings", ids)
        self.assertTrue(hub.state_snapshot()["host"]["catalog_stale"])
        self.assertNotIn("learn.herdr-keybindings", {entry["id"] for entry in service.refresh_catalog()["entries"]})

    def test_explicit_default_menu_fixture_is_read_and_marked_local_source(self):
        hub = Hub()
        source = self.fixtures / "omarchy-default-v4.0.3.jsonc"
        with patch("omodachi_core.bootstrap.OMARCHY_DEFAULT_MENU", self.missing_installed):
            service = create_service(
                hub, demo=False, default_menu=source,
                user_menu=Path("/tmp/omodachi-no-user-menu"),
                omodachi_menu=Path("/tmp/omodachi-no-own-menu"), shell_config=self.shell,
                agent_probe=StaticProbe(),
            )
        ids = {entry["id"] for entry in service.refresh_catalog()["entries"]}
        self.assertIn("learn.herdr-keybindings", ids)
        self.assertFalse(hub.state_snapshot()["host"].get("catalog_stale", False))

    def test_meta_uses_controls_provenance_commit_and_byte_preserved_hash(self):
        source = self.fixtures / "omarchy-default-v4.0.3.jsonc"
        meta = json.loads((self.fixtures / "omarchy-default-v4.0.3.meta.json").read_text())
        import hashlib
        self.assertEqual(meta["source_commit"], "2fbac0c8e88eca704af1650ce721a494bd11a3d0")
        self.assertEqual(meta["source_path"], "omodachi-web/docs/design-research/source-evidence/omarchy/default/omarchy/omarchy-menu.jsonc")
        self.assertEqual(meta["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(meta["bytes"], source.stat().st_size)
        self.assertTrue(meta["provenance_file"].endswith("controls-provenance.json"))


if __name__ == "__main__":
    unittest.main()
