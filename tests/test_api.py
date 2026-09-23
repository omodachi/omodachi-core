"""Real loopback HTTP/WSS integration, with server-owned fixture adapters."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import ssl
import subprocess
import tempfile
import unittest

import aiohttp
from omodachi_core.auth import DeviceAuthenticator
from omodachi_core.bootstrap import create_service
from omodachi_core.protocol import CONTRACT_REVISION
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer
from omodachi_core.routes import RouteDescriptor


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub = Hub(auth_check_interval=0.05)
        self.a = self.hub.register_device("phone-a")
        self.b = self.hub.register_device("phone-b")
        self.service = create_service(self.hub, demo=True)
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))

    async def asyncTearDown(self):
        await self.client.close()
        await asyncio.wait_for(self.server.close(), 3)
        self.temp.cleanup()

    def headers(self, token=None):
        return {"Authorization": "Bearer " + (token or self.a)}

    async def get(self, path, token=None):
        async with self.client.get(self.url + path, headers=self.headers(token)) as response:
            return response.status, await response.json()

    async def post(self, path, data, token=None):
        async with self.client.post(self.url + path, headers=self.headers(token), json=data) as response:
            return response.status, await response.json()

    async def test_unauthed_root_explains_api_boundary(self):
        async with self.client.get(self.url + "/") as response:
            self.assertEqual(response.status, 200)
            data = await response.json()
        self.assertEqual(data["service"], "omodachid")
        self.assertEqual(data["authenticated_api"], "/v1/*")
        self.assertIn("not the Web UI", data["message"])

    async def test_authenticated_resources_without_sunshine(self):
        async with self.client.get(self.url + "/v1/state") as response:
            self.assertEqual(response.status, 401)
        for endpoint in ("state", "capabilities", "catalog", "herdr"):
            code, data = await self.get("/v1/" + endpoint)
            self.assertEqual(code, 200)
            self.assertEqual(data["contract_revision"], CONTRACT_REVISION)
        _, state = await self.get("/v1/state")
        self.assertFalse(state["capabilities"]["sunshine"])
        self.assertEqual(state["agent"]["default_agent"]["actual_kind"], "codex")
        self.assertEqual(state["agent"]["status"], "working")
        self.assertGreater(len(state["catalog"]["entries"]), 10)

    async def test_remote_is_unavailable_without_a_manager_and_gates_nothing_else(self):
        ws = await self.client.ws_connect(self.url + "/v1/events", headers=self.headers(self.b))
        first = await ws.receive_json()
        self.assertEqual(first["type"], "snapshot")
        self.assertEqual(first["state"]["remote"]["state"], "offline")
        code, value = await self.get("/v1/remote/capabilities", self.a)
        self.assertEqual(code, 200, value)
        self.assertFalse(value["backends"]["sunshine"]["available"])
        self.assertEqual(value["backends"]["vnc"]["reason"], "remote_runtime_unavailable")
        code, value = await self.post("/v1/remote/sessions", {}, self.a)
        self.assertEqual(code, 503, value)
        self.assertEqual(value["error"]["code"], "remote_runtime_unavailable")
        for endpoint in ("state", "capabilities", "catalog", "herdr"):
            self.assertEqual((await self.get("/v1/" + endpoint, self.b))[0], 200)
        await ws.close()

    async def test_ws_replay_over_100_and_restart_identity(self):
        cursor = self.hub.event_cursor
        for n in range(150):
            self.hub.publish("fixture.changed", {"n": n})
        ws = await self.client.ws_connect(self.url + f"/v1/events?since={cursor}&instance_id={self.service.instance_id}", headers=self.headers())
        self.assertEqual((await ws.receive_json())["type"], "ready")
        numbers = [(await ws.receive_json())["event"]["payload"]["n"] for _ in range(150)]
        self.assertEqual(numbers, list(range(150)))
        await ws.close()
        ws = await self.client.ws_connect(self.url + "/v1/events?since=100000&instance_id=old-process", headers=self.headers())
        self.assertEqual((await ws.receive_json())["type"], "snapshot")
        await ws.close()
        ws = await self.client.ws_connect(self.url + f"/v1/events?since=100000&instance_id={self.service.instance_id}", headers=self.headers())
        await ws.receive_json()
        self.assertEqual((await ws.receive_json())["event"]["type"], "resync.required")
        await ws.close()

    async def test_revoked_and_expired_ws_receive_no_more_payload(self):
        ws = await self.client.ws_connect(self.url + "/v1/events", headers=self.headers())
        await ws.receive_json()
        self.hub.auth.revoke(self.a)
        self.hub.publish("secret", {"value": "must-not-be-delivered"})
        result = await asyncio.wait_for(ws.receive(), 2)
        self.assertNotEqual(result.type, aiohttp.WSMsgType.TEXT)
        await ws.close()
        # Existing subscriptions also lose access when their credential is
        # revoked. Token expiry itself is covered by the auth/security suite;
        # avoiding a wall-clock sleep keeps this transport test deterministic.
        ws = await self.client.ws_connect(self.url + "/v1/events", headers=self.headers(self.b))
        await ws.receive_json()
        self.hub.auth.revoke(self.b)
        self.hub.publish("secret", {"value": "must-not-be-delivered"})
        result = await asyncio.wait_for(ws.receive(), 2)
        self.assertNotEqual(result.type, aiohttp.WSMsgType.TEXT)
        await ws.close()

    async def test_action_condition_revision_and_idempotency(self):
        _, catalog = await self.get("/v1/catalog")
        row = next(e for e in catalog["entries"] if e["id"] == "trigger.toggle.notifications")
        self.assertFalse(row["checked_state"])
        request = {"request_id": "click-1", "catalog_revision": catalog["revision"], "params": {}}
        status, result = await self.post("/v1/actions/trigger.toggle.notifications:invoke", request)
        self.assertEqual((status, result["status"]), (200, "accepted"))
        self.assertEqual((await self.post("/v1/actions/trigger.toggle.notifications:invoke", request))[1], result)
        _, changed = await self.get("/v1/catalog")
        self.assertTrue(next(e for e in changed["entries"] if e["id"] == row["id"])["checked_state"])
        # PERF-5. The revision moved (the toggle's own `checked` flipped) and
        # the row did not. A client holding the older one is not made to
        # refresh and tap again: the id is re-resolved, the action runs, and
        # the receipt carries the revision it ran against.
        status, again = await self.post("/v1/actions/trigger.toggle.notifications:invoke", {**request, "request_id": "old-revision"})
        self.assertEqual((status, again["status"]), (200, "accepted"))
        self.assertEqual(again["catalog_revision"], changed["revision"])
        self.assertNotEqual(again["catalog_revision"], request["catalog_revision"])
        self.assertEqual((await self.post("/v1/actions/trigger.toggle.notifications:invoke", {**request, "shell":"echo no"}))[0], 400)

    async def test_current_when_is_checked_before_executor(self):
        from omodachi_core.catalog import compile_catalog
        self.service.runtime.catalog = compile_catalog({"a":{"action":"fixed-command", "when":"permitted"}})
        self.service.runtime.register_condition("permitted", lambda: True)
        self.service.policy.register("a", RouteDescriptor("host", True, argv=("fixed-command",)))
        calls=[]
        self.service.register_executor("a", calls.append)
        catalog = self.service.refresh_catalog()
        self.service.runtime.register_condition("permitted", lambda: False)
        status, result = await self.post("/v1/actions/a:invoke", {"request_id":"denied","catalog_revision":catalog["revision"]})
        self.assertEqual(status,409)
        self.assertEqual(calls,[])

    async def test_tls_required_except_explicit_loopback_and_works_with_trusted_ca(self):
        blocked = NetworkServer(self.service,host="0.0.0.0",allow_loopback_http=True)
        with self.assertRaises(ValueError):
            await blocked.start()
        root=Path(self.temp.name)
        config=root/"openssl.cnf"
        config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=IP:127.0.0.1,DNS:localhost\nbasicConstraints=critical,CA:TRUE\n')
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-keyout",str(root/"key.pem"),"-out",str(root/"cert.pem"),"-config",str(config)],check=True,capture_output=True)
        tls=NetworkServer(self.service,certificate=str(root/"cert.pem"),private_key=str(root/"key.pem"))
        await tls.start()
        try:
            context=ssl.create_default_context(cafile=str(root/"cert.pem"))
            async with self.client.get(f"https://127.0.0.1:{tls.bound_port}/v1/state",headers=self.headers(),ssl=context) as response:
                self.assertEqual(response.status,200)
            ws=await self.client.ws_connect(f"wss://127.0.0.1:{tls.bound_port}/v1/events",headers=self.headers(),ssl=context)
            self.assertEqual((await ws.receive_json())["type"],"snapshot")
            await asyncio.wait_for(tls.close(),3)
            self.assertNotEqual((await ws.receive()).type,aiohttp.WSMsgType.TEXT)
        finally:
            await tls.close()

    async def test_revoke_during_slow_body_rejected_before_action(self):
        _, catalog = await self.get('/v1/catalog')
        reached=asyncio.Event()
        resume=asyncio.Event()
        original_auth=self.hub.authenticate
        def observed(token):
            owner=original_auth(token)
            reached.set()
            return owner
        self.hub.authenticate=observed
        payload=json.dumps({'request_id':'slow-body','catalog_revision':catalog['revision'],'params':{}}).encode()
        async def chunks():
            yield payload[:1]
            await resume.wait()
            yield payload[1:]
        task=asyncio.create_task(self.client.post(self.url+'/v1/actions/trigger.toggle.notifications:invoke',headers={**self.headers(),'Content-Type':'application/json'},data=chunks()))
        await asyncio.wait_for(reached.wait(),1)
        self.hub.auth.revoke(self.a)
        resume.set()
        async with await task as response:
            self.assertEqual(response.status,401)
        current=self.service.refresh_catalog()
        row=next(e for e in current['entries'] if e['id']=='trigger.toggle.notifications')
        self.assertFalse(row['checked_state'])

    async def test_live_http_and_wss_payloads_validate_against_contracts(self):
        import sys
        sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
        from verify_contracts import validators
        checks=validators()
        for resource_name,schema in [('state','state.schema.json'),('capabilities','capabilities.schema.json'),('catalog','catalog.schema.json'),('herdr','herdr-resource.schema.json')]:
            code,data=await self.get('/v1/'+resource_name)
            self.assertEqual(code,200)
            checks[schema].validate(data)
        ws=await self.client.ws_connect(self.url+'/v1/events',headers=self.headers())
        checks['wss-envelope.schema.json'].validate(await ws.receive_json())
        _,catalog=await self.get('/v1/catalog')
        await self.post('/v1/actions/trigger.toggle.notifications:invoke',{'request_id':'schema-event','catalog_revision':catalog['revision']})
        # PERF-4: the receipt goes out first and the rebuild follows it, so the
        # answer no longer waits for a catalog the client re-reads anyway.
        message=await ws.receive_json()
        self.assertEqual(message['event']['type'],'action.result')
        checks['wss-envelope.schema.json'].validate(message)
        message=await ws.receive_json()
        self.assertEqual(message['event']['type'],'catalog.changed')
        self.assertIsInstance(message['event']['payload']['revision'],int)
        self.assertIsInstance(message['event']['payload']['catalog']['revision'],str)
        checks['wss-envelope.schema.json'].validate(message)
        await ws.close()

    async def test_invalid_catalog_source_disables_previous_actions_until_repaired(self):
        import json
        from omodachi_core.bootstrap import DATA
        source=Path(self.temp.name)/'menu.jsonc'
        original=(DATA/'demo-menu.jsonc').read_text()
        source.write_text(original)
        hub=Hub()
        service=create_service(hub,demo=True,default_menu=source)
        catalog=service.refresh_catalog()
        source.write_text('{ invalid json')
        from omodachi_core.service import ServiceError
        with self.assertRaisesRegex(ServiceError,'catalog source'):
            service.dispatch('actions.invoke',{'entry_id':'trigger.toggle.notifications','request_id':'stale-source','catalog_revision':catalog['revision']},'phone-a')
        self.assertTrue(hub.state_snapshot()['host']['catalog_stale'])
        row=next(e for e in service.refresh_catalog()['entries'] if e['id']=='trigger.toggle.notifications')
        self.assertFalse(row['route']['ready'])
        self.assertFalse(row['checked_state'])
        source.write_text(original)
        service.refresh_sources()
        self.assertFalse(hub.state_snapshot()['host']['catalog_stale'])
        catalog=service.refresh_catalog()
        self.assertEqual(service.dispatch('actions.invoke',{'entry_id':'trigger.toggle.notifications','request_id':'fixed-source','catalog_revision':catalog['revision']},'phone-a')['status'],'accepted')

    async def test_herdr_unavailable_does_not_disable_other_terminal_routes(self):
        from omodachi_core.agent import DefaultAgentCapabilities,HerdrStatusSnapshot,ProbeStatus
        cap=DefaultAgentCapabilities(omarchy_default_agent='codex',omarchy_probe=ProbeStatus.AVAILABLE,herdr_probe=ProbeStatus.NOT_RUNNING)
        self.service.update_agent(cap,HerdrStatusSnapshot(True,False,False,frozenset()))
        catalog=self.service.refresh_catalog()
        rows={row['id']:row for row in catalog['entries']}
        self.assertTrue(rows['learn.demo']['route']['ready'])
        self.assertFalse(rows['omodachi.herdr']['route']['ready'])
        self.assertFalse(rows['omodachi.agent']['route']['ready'])
        self.assertTrue(self.hub.capabilities_snapshot()['terminal'])
        self.assertEqual(self.service.dispatch('actions.invoke',{'entry_id':'learn.demo','request_id':'plain-terminal','catalog_revision':catalog['revision']},'phone-a')['status'],'prepared')

    async def test_a_device_can_replace_its_own_ssh_key_and_only_its_own(self):
        """UX-4 §2. `PUT /v1/ssh/key`, over the real loopback boundary.

        The device is the one on the credential. The body carries a key and
        nothing else - there is no `device_id` field to send, which is the
        whole security property: a credential rewrites its own line or none.
        """
        import base64
        from omodachi_core.ssh_keys import AuthorizedKeys, fingerprint

        def ed25519(seed):
            blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([seed]) * 32
            return "ssh-ed25519 " + base64.b64encode(blob).decode() + " synthetic"

        home = Path(self.temp.name)
        keys = AuthorizedKeys(home)
        self.service.ssh_keys = keys
        paired, drifted = ed25519(1), ed25519(2)
        keys.authorize(paired, "phone-a")
        keys.authorize(ed25519(3), "phone-b")

        async def put(body, token=None):
            async with self.client.put(self.url + "/v1/ssh/key",
                                       headers=self.headers(token), json=body) as response:
                return response.status, await response.json()

        status, result = await put({"public_key": drifted})
        self.assertEqual(status, 200)
        self.assertEqual(result["contract_revision"], CONTRACT_REVISION)
        self.assertEqual((result["device_id"], result["reason"], result["removed"]),
                         ("phone-a", "replaced", 1))
        self.assertEqual(result["replaced"], [fingerprint(paired.split()[1])])
        self.assertEqual(sorted((row["device"], row["fingerprint"]) for row in keys.listing()),
                         sorted([("phone-a", fingerprint(drifted.split()[1])),
                                 ("phone-b", fingerprint(ed25519(3).split()[1]))]))
        # Idempotent: offering the same key again writes nothing.
        status, result = await put({"public_key": drifted})
        self.assertEqual((status, result["changed"], result["reason"]),
                         (200, False, "already_authorized"))
        # phone-b's line is untouchable from phone-a's credential, even by
        # offering phone-b's own key.
        status, result = await put({"public_key": ed25519(3)})
        self.assertEqual((status, result["error"]["code"]), (409, "public_key_owned_by_other_device"))
        # A device_id in the body is not a field this endpoint has.
        status, result = await put({"public_key": ed25519(4), "device_id": "phone-b"})
        self.assertEqual((status, result["error"]["code"]), (400, "invalid_request"))
        # And nothing at all without a credential.
        async with self.client.put(self.url + "/v1/ssh/key", json={"public_key": ed25519(5)}) as response:
            self.assertEqual(response.status, 401)
        self.assertEqual(len(keys.listing()), 2)

    async def test_a_device_can_read_the_key_this_host_holds_for_it(self):
        """UX-4 §2. `GET /v1/ssh/key` - fingerprints, for the caller only."""
        import base64
        from omodachi_core.ssh_keys import AuthorizedKeys, fingerprint

        def ed25519(seed):
            blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([seed]) * 32
            return "ssh-ed25519 " + base64.b64encode(blob).decode() + " synthetic"

        keys = AuthorizedKeys(Path(self.temp.name))
        self.service.ssh_keys = keys
        # A device the host holds nothing for: the state a reinstalled device
        # that was adopted rather than re-paired is actually in.
        status, result = await self.get("/v1/ssh/key")
        self.assertEqual((status, result["authorized"], result["fingerprint"], result["fingerprints"]),
                         (200, False, None, []))
        older, newer = ed25519(1), ed25519(2)
        keys.authorize(older, "phone-a")
        keys.authorize(newer, "phone-a")
        keys.authorize(ed25519(3), "phone-b")
        status, result = await self.get("/v1/ssh/key")
        self.assertEqual(result["device_id"], "phone-a")
        # Oldest first, and `fingerprint` is the newest of them.
        self.assertEqual(result["fingerprints"],
                         [fingerprint(older.split()[1]), fingerprint(newer.split()[1])])
        self.assertEqual(result["fingerprint"], fingerprint(newer.split()[1]))
        # phone-b's key is not in phone-a's answer.
        _, other = await self.get("/v1/ssh/key", token=self.b)
        self.assertEqual(other["fingerprints"], [fingerprint(ed25519(3).split()[1])])
        # No public key is ever returned, under any name.
        self.assertNotIn(newer.split()[1], json.dumps(result))

    async def test_the_ssh_key_answer_validates_against_its_contract(self):
        import base64
        from jsonschema import Draft202012Validator
        from omodachi_core.ssh_keys import AuthorizedKeys

        blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([7]) * 32
        line = "ssh-ed25519 " + base64.b64encode(blob).decode() + " synthetic"
        self.service.ssh_keys = AuthorizedKeys(Path(self.temp.name))
        async with self.client.put(self.url + "/v1/ssh/key", headers=self.headers(),
                                   json={"public_key": line}) as response:
            document = await response.json()
        schema = json.loads((Path(__file__).resolve().parents[1] / "contracts/ssh-key.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(document)
        status, state = await self.get("/v1/ssh/key")
        self.assertEqual(status, 200)
        read = json.loads((Path(__file__).resolve().parents[1] / "contracts/ssh-key-state.schema.json").read_text())
        Draft202012Validator.check_schema(read)
        Draft202012Validator(read).validate(state)

class ClipboardRouteTests(unittest.IsolatedAsyncioTestCase):
    """CLIP-1's two routes, over real loopback HTTP.

    The demo service has no clipboard bridge at all, which is the first case
    here: an honest 503 rather than an empty string. The rest run against a
    stand-in bridge so the route's own behaviour — the content type, the limit,
    the revoked credential — is what is under test rather than `wl-copy`.
    """

    class Bridge:
        def __init__(self, mode="both"):
            self.mode, self.content, self.written = mode, "hello", []
            self.stopped = False

        def require(self, direction):
            from omodachi_core.clipboard import ClipboardError
            if self.mode == "off":
                raise ClipboardError("clipboard_sync_disabled", 403)
            if direction == "device_to_host" and self.mode != "both":
                raise ClipboardError("clipboard_write_disabled", 403)
            return self.mode

        def stop(self):
            """The daemon stops the watcher on shutdown; this one has none."""
            self.stopped = True

        def read(self):
            self.require("host_to_device")
            return self.content

        def write(self, text):
            self.require("device_to_host")
            self.written.append(text)
            self.content = text
            return {"bytes": len(text.encode()), "mime": "text/plain"}

    async def asyncSetUp(self):
        self.hub = Hub(auth_check_interval=0.05)
        self.token = self.hub.register_device("ipad")
        self.service = create_service(self.hub, demo=True)
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))

    async def asyncTearDown(self):
        await self.client.close()
        await asyncio.wait_for(self.server.close(), 3)

    def headers(self, extra=None):
        return {"Authorization": "Bearer " + self.token, **(extra or {})}

    #: What a client that is putting text on the clipboard says it is sending.
    TEXT = {"Content-Type": "text/plain; charset=utf-8"}

    async def test_a_host_without_a_clipboard_bridge_says_so(self):
        async with self.client.get(self.url + "/v1/clipboard", headers=self.headers()) as response:
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())["error"]["code"], "clipboard_unavailable")

    async def test_the_text_is_the_body_and_the_write_answers_with_a_count(self):
        self.service.clipboard = self.Bridge()
        async with self.client.get(self.url + "/v1/clipboard", headers=self.headers()) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.content_type, "text/plain")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(await response.text(), "hello")
        async with self.client.put(self.url + "/v1/clipboard", headers=self.headers(self.TEXT),
                                   data="from the iPad".encode()) as response:
            self.assertEqual(response.status, 200)
            body = await response.json()
        self.assertEqual(body["bytes"], 13)
        self.assertNotIn("from the iPad", json.dumps(body))
        self.assertEqual(self.service.clipboard.written, ["from the iPad"])

    async def test_the_preference_refuses_each_direction_by_code(self):
        self.service.clipboard = self.Bridge("off")
        async with self.client.get(self.url + "/v1/clipboard", headers=self.headers()) as response:
            self.assertEqual(response.status, 403)
            self.assertEqual((await response.json())["error"]["code"], "clipboard_sync_disabled")
        async with self.client.put(self.url + "/v1/clipboard", headers=self.headers(self.TEXT),
                                   data=b"x") as response:
            self.assertEqual(response.status, 403)
            self.assertEqual((await response.json())["error"]["code"], "clipboard_sync_disabled")
        self.service.clipboard = self.Bridge("host_to_device")
        async with self.client.get(self.url + "/v1/clipboard", headers=self.headers()) as response:
            self.assertEqual(response.status, 200)
        async with self.client.put(self.url + "/v1/clipboard", headers=self.headers(self.TEXT),
                                   data=b"x") as response:
            self.assertEqual(response.status, 403)
            self.assertEqual((await response.json())["error"]["code"], "clipboard_write_disabled")

    async def test_an_unpaired_caller_gets_nothing(self):
        self.service.clipboard = self.Bridge()
        async with self.client.get(self.url + "/v1/clipboard") as response:
            self.assertEqual(response.status, 401)
        async with self.client.put(self.url + "/v1/clipboard", data=b"x") as response:
            self.assertEqual(response.status, 401)
        self.assertEqual(self.service.clipboard.written, [])

    async def test_a_credential_revoked_mid_request_does_not_write(self):
        self.service.clipboard = self.Bridge()
        original = self.hub.authenticate
        headers = self.headers(self.TEXT)
        # Revoke between the middleware's check and the route's own recheck:
        # the second call is the one the route makes at the mutation boundary.
        checks = {"n": 0}
        def counting(token):
            checks["n"] += 1
            if checks["n"] == 2:
                raise ValueError("revoked")
            return original(token)
        self.hub.authenticate = counting
        try:
            async with self.client.put(self.url + "/v1/clipboard", headers=headers,
                                       data=b"x") as response:
                self.assertEqual(response.status, 401)
        finally:
            self.hub.authenticate = original
        self.assertEqual(self.service.clipboard.written, [])

    async def test_more_than_the_limit_is_refused_rather_than_truncated(self):
        from omodachi_core.clipboard import LIMIT
        self.service.clipboard = self.Bridge()
        async with self.client.put(self.url + "/v1/clipboard", headers=self.headers(self.TEXT),
                                   data=b"a" * (LIMIT + 1)) as response:
            self.assertEqual(response.status, 413)
            self.assertEqual((await response.json())["error"]["code"], "clipboard_too_large")
        self.assertEqual(self.service.clipboard.written, [])

    async def test_a_body_that_is_not_utf8_text_is_refused(self):
        self.service.clipboard = self.Bridge()
        async with self.client.put(self.url + "/v1/clipboard", headers=self.headers(self.TEXT),
                                   data=b"\xff\xfe") as response:
            self.assertEqual(response.status, 415)
            self.assertEqual((await response.json())["error"]["code"], "clipboard_not_text")
        async with self.client.put(self.url + "/v1/clipboard",
                                   headers=self.headers({"Content-Type": "image/png"}),
                                   data=b"x") as response:
            self.assertEqual(response.status, 415)
        self.assertEqual(self.service.clipboard.written, [])

    async def test_a_query_string_is_not_part_of_this_route(self):
        self.service.clipboard = self.Bridge()
        async with self.client.get(self.url + "/v1/clipboard?device=1",
                                   headers=self.headers()) as response:
            self.assertEqual(response.status, 400)
