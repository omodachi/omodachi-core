"""Real Draft 2020-12 validation for every handed-off fixture and key rejects."""
from copy import deepcopy
from pathlib import Path
import json
import sys
import unittest

from jsonschema.exceptions import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from verify_contracts import generated_fixtures, validators, verify


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validators = validators()
        cls.fixtures = generated_fixtures()

    def rejects(self, schema, value):
        with self.assertRaises(ValidationError):
            self.validators[schema].validate(value)

    def test_all_fixtures_validate_and_match_actual_serializers(self):
        result = verify()
        self.assertGreaterEqual(result["schemas"], 15)
        self.assertGreaterEqual(result["fixtures"], 30)

    def test_real_demo_service_resources_validate(self):
        from omodachi_core.bootstrap import create_service
        from omodachi_core.hub import Hub
        service = create_service(Hub(), demo=True)
        outputs = {
            "state.schema.json": service.state("device-schema-fixture"),
            "capabilities.schema.json": service.resource(service.hub.capabilities_snapshot()),
            "catalog.schema.json": service.refresh_catalog(),
            "herdr-resource.schema.json": service.resource(service.hub.herdr_snapshot()),
        }
        for schema, value in outputs.items():
            with self.subTest(schema=schema):
                self.validators[schema].validate(value)

    def test_catalog_event_preserves_state_revision_and_catalog_revision(self):
        event = self.fixtures["event-catalog-changed.json"]
        self.assertIsInstance(event["payload"]["revision"], int)
        self.assertIsInstance(event["payload"]["catalog"]["revision"], str)
        wrong = deepcopy(event)
        wrong["payload"] = {"revision": event["payload"]["catalog"]["revision"]}
        self.rejects("events.schema.json", wrong)
        wrong = deepcopy(event)
        wrong["payload"]["revision"] = "12"
        self.rejects("events.schema.json", wrong)

    def test_generator_is_deterministic_and_contains_all_four_routes(self):
        self.assertEqual(generated_fixtures(), generated_fixtures())
        self.assertEqual({row["route"] for row in self.fixtures["route-descriptors.json"]["routes"]},
                         {"host", "terminal", "desktop", "native"})

    def test_workspace_references_and_search_share_packaged_jsonc_catalog(self):
        from omodachi_core.catalog import load_jsonc
        source = load_jsonc(ROOT / "src/omodachi_core/data/omodachi-menu.jsonc")
        catalog = self.fixtures["catalog.json"]
        by_id = {row["id"]: row for row in catalog["entries"]}
        items = self.fixtures["state.json"]["workspace"]["items"]
        self.assertEqual([row["id"] for row in items], list(range(1, 11)))
        for row in items:
            self.assertEqual(row["select_entry_id"], f"omodachi.workspace.select.{row['id']}")
            self.assertEqual(row["move_entry_id"], f"omodachi.workspace.move.{row['id']}")
            for key in ("select_entry_id", "move_entry_id"):
                self.assertIn(row[key], source)
                self.assertIn(row[key], by_id)
                self.assertEqual(by_id[row[key]]["workspace"]["id"], row["id"])
        result = self.fixtures["catalog-search-3.json"]
        self.assertEqual(result["query"], "3")
        self.assertEqual(result["revision"], catalog["revision"])
        self.assertEqual(result["entries"][0]["id"], "omodachi.workspace.select.3")
        self.assertIn("3", result["entries"][0]["aliases"])
        for row in result["entries"]:
            self.assertEqual(row, by_id[row["id"]])

    def test_workspace_move_example_uses_source_id_and_explicit_focus_token(self):
        state = self.fixtures["state.json"]
        request = self.fixtures["ipc-request-workspace-move.json"]
        target = next(row for row in state["workspace"]["items"] if row["id"] == 3)
        self.assertEqual(request["entry_id"], target["move_entry_id"])
        self.assertEqual(request["state_revision"], state["revision"])
        self.assertEqual(request["target_token"], state["focus"]["target_token"])
        self.assertEqual(request["params"], {})
        self.assertIsNone(state["focus"]["window"])
        self.assertEqual(state["focus"]["app_id"], "fixture.editor")
        # Removing an entry from the source can be represented without inventing
        # a replacement action, and unknown occupancy is not forged as empty.
        unavailable = deepcopy(state)
        unavailable["workspace"]["items"][2].update(
            select_entry_id=None, move_entry_id=None, window_count=None, occupied=None)
        self.validators["state.schema.json"].validate(unavailable)

    def test_full_default_roots_and_terminal_menu_examples_are_present(self):
        by_id = {row["id"]: row for row in self.fixtures["catalog.json"]["entries"]}
        roots = ("apps", "learn", "trigger", "style", "setup", "install", "remove", "update", "about", "system")
        for root in roots:
            self.assertEqual(by_id[root]["parent"], "root")
        for name in ("install.demo", "update.demo", "about.demo"):
            self.assertEqual(by_id[name]["route"]["route"], "terminal")
            self.assertEqual(by_id[name]["route"]["argv"][0], "printf")

    def test_bar_is_flat_semantic_data_and_never_custom_qml(self):
        bar = self.fixtures["bar.json"]
        self.assertEqual(bar["source"], "shell.json")
        self.assertNotIn("layout", bar)
        self.assertNotIn("workspaces", bar)
        roles = {row["role"] for region in ("left", "center", "right") for row in bar[region]}
        # omarchy.agents is the AI usage widget (bar.py _REVIEWED_ROLES), so
        # its role is agent_usage, not Herdr agent status.
        self.assertTrue({"workspaces", "focused_window", "clock", "system_tray", "agent_usage", "stream"} <= roles)
        unavailable = self.fixtures["bar-unavailable.json"]
        self.assertEqual(unavailable["source_status"], "unavailable")
        self.assertTrue(all(unavailable[region] == [] for region in ("left", "center", "right")))
        malicious = deepcopy(bar)
        malicious["left"][0]["exec"] = "unreviewed command"
        self.rejects("bar.schema.json", malicious)
        malicious = deepcopy(bar)
        malicious["left"][0]["id"] = "/client/chosen/path"
        self.rejects("bar.schema.json", malicious)

    def test_capabilities_rejects_old_nested_shape(self):
        self.rejects("capabilities.schema.json", {
            "contract_revision": "omodachi.v1",
            "desktop": {"stream_available": False}, "herdr": {"server_running": True},
        })

    def test_state_rejects_incomplete_agent_and_an_invented_remote_field(self):
        state = deepcopy(self.fixtures["state.json"])
        state["agent"] = {"status": "working"}
        self.rejects("state.schema.json", state)
        state = deepcopy(self.fixtures["state.json"])
        state["remote"]["lease"] = {"owner_device_id": "fake"}
        self.rejects("state.schema.json", state)
        state = deepcopy(self.fixtures["state.json"])
        state["remote"]["state"] = "streaming"
        self.rejects("state.schema.json", state)

    def test_agent_rejects_unknown_status_or_ready_with_missing_pane(self):
        agent = deepcopy(self.fixtures["default-agent.json"])
        agent["agent_status"] = "busy-ish"
        self.rejects("schemas/default-agent-capabilities.schema.json", agent)
        agent = deepcopy(self.fixtures["default-agent.json"])
        agent["pane_available"] = False
        self.rejects("schemas/default-agent-capabilities.schema.json", agent)
        agent = deepcopy(self.fixtures["default-agent.json"])
        agent["pane_id"] = None
        self.rejects("schemas/default-agent-capabilities.schema.json", agent)

    def test_ipc_requires_device_auth_and_rejects_free_execution_fields(self):
        self.rejects("ipc-request.schema.json", {"op": "state"})
        self.validators["ipc-request.schema.json"].validate({"op": "health"})
        for key in ("actor", "shell", "lua", "url", "path", "argv"):
            request = deepcopy(self.fixtures["ipc-request-actions.json"])
            request[key] = "client-provided"
            self.rejects("ipc-request.schema.json", request)
        self.rejects("ipc-request.schema.json", {
            "op": "remote.heartbeat", "token": "fixture", "lease_id": "lease-fixture"})
        self.rejects("ipc-request.schema.json", {"op": "desktop.acquire", "token": "fixture"})

    def test_ipc_response_and_http_error_are_distinct_envelopes(self):
        self.rejects("ipc-envelope.schema.json", self.fixtures["http-error-stale-target.json"])
        self.rejects("http-error.schema.json", self.fixtures["ipc-response-error.json"])
        self.validators["ipc-envelope.schema.json"].validate(self.fixtures["ipc-response-state.json"])
        self.validators["http-error.schema.json"].validate(self.fixtures["http-error-stale-target.json"])

    def test_unknown_condition_cannot_be_serialized_as_false(self):
        catalog = deepcopy(self.fixtures["catalog-layered.json"])
        row = next(row for row in catalog["entries"] if row["conditions"]["checked"]["status"] == "unavailable")
        row["conditions"]["checked"]["value"] = False
        self.rejects("catalog.schema.json", catalog)


if __name__ == "__main__":
    unittest.main()
