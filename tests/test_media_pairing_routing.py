"""Real loopback HTTP + Unix daemon/fork sockets with synthetic pairing only.

No real Sunshine, host display, media frames, or client trust are exercised.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

import aiohttp

from omodachi_core.auth import DeviceAuthenticator
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.ipc import JsonLineClient, JsonLineServer
from omodachi_core.media_pairing import (MediaPairingBridge, MediaPairingError, MediaPairingStore,
                                         SunshinePairingIPC)
from omodachi_core.network import NetworkServer
from omodachi_core.pairing import PairingStore
from tests.test_media_pairing import FakeFork


class MediaRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fork = FakeFork()
        self.fp = 'a' * 64
        self.rid = self.fork.add(self.fp)
        private = self.root / 'sunshine'
        private.mkdir(mode=0o700)
        self.fork_path = private / 'pairing.sock'
        async def handle(reader, writer):
            try:
                payload = json.loads(await reader.readline())
                op = payload.pop('op')
                try:
                    frame = {'ok': True, **self.fork.request(op, **payload)}
                except MediaPairingError as error:
                    # The real fork answers an error frame; it does not hang up.
                    frame = {'ok': False, 'error': {'code': error.code}}
                writer.write((json.dumps(frame) + '\n').encode())
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        self.fork_server = await asyncio.start_unix_server(handle, str(self.fork_path))
        self.fork_path.chmod(0o600)
        self.bridge = MediaPairingBridge(
            SunshinePairingIPC(self.fork_path, peer_check=lambda connection: None),
            MediaPairingStore(self.root / 'media' / 'state.json'))
        self.authority = DeviceAuthenticator.from_file(self.root / 'device.secret')
        self.hub = Hub(authenticator=self.authority)
        self.other = self.hub.register_device('other')
        self.service = create_service(self.hub, demo=True, media_pairing=self.bridge)
        self.service.pairing = PairingStore(self.authority, self.root / 'pairing.json')
        self.ipc_path = str(self.root / 'core.sock')
        self.ipc = JsonLineServer(self.hub, self.ipc_path, local_handler=self.service.dispatch_local_async)
        await self.ipc.start()
        self.network = NetworkServer(self.service, allow_loopback_http=True)
        await self.network.start()
        self.url = f'http://127.0.0.1:{self.network.bound_port}'
        self.http = aiohttp.ClientSession()
        self.local = JsonLineClient(self.ipc_path, timeout=12)

    async def asyncTearDown(self):
        await self.http.close()
        await self.network.close()
        await self.ipc.close()
        await self.service.close_media()
        self.fork_server.close()
        await self.fork_server.wait_closed()
        self.temp.cleanup()

    async def request(self, method, path, payload=None, token=None):
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        async with self.http.request(method, self.url + path, json=payload, headers=headers) as response:
            return response.status, await response.json()

    async def companion(self, remote=False, device='ipad', claim=True):
        invitation = (await self.local.request('local.pair.begin'))['result']
        code, row = await self.request('POST', '/v1/pairing/requests', {
            'invitation': invitation['invitation'], 'device_id': device, 'device_name': 'Fixture iPad'})
        self.assertEqual(code, 200)
        approved = await self.local.request('local.pair.approve', request_id=row['request_id'], remote=remote)
        self.assertTrue(approved['ok'], approved)
        if not claim:
            return row, approved
        token = await self.claim(row)
        return token, row, approved

    async def claim(self, row):
        code, claimed = await self.request('POST', '/v1/pairing/requests/' + row['request_id'] + '/claim',
            {'request_secret': row['request_secret']})
        self.assertEqual(code, 200)
        return claimed['credential']

    async def submit(self, token, **extra):
        return await self.request('POST', '/v1/media/pairing/requests', {
            'request_id': self.rid, 'client_cert_sha256': self.fp, 'pin': '7391', **extra}, token)

    async def test_remote_first_approval_claim_and_exact_final_proof(self):
        row, approved = await self.companion(remote=True, claim=False)
        self.assertTrue(approved['result']['remote']['media_authorized'])
        self.assertEqual((await self.submit(None))[0], 401)
        token = await self.claim(row)
        code, found = await self.request('POST', '/v1/media/pairing/discover',
            {'client_cert_sha256': self.fp, 'pairing_intent': True}, token)
        self.assertEqual(code, 200, found)
        self.assertEqual(found['request_id'], self.rid)
        code, attempt = await self.submit(token)
        self.assertEqual(code, 200, attempt)
        self.assertEqual(attempt['status'], 'awaiting_client_proof')
        self.assertFalse(self.bridge.media_authorized('ipad', self.fp))
        self.assertIsNone(self.bridge.paired_certificate('ipad'))
        repeats = await asyncio.gather(*(self.submit(token) for _ in range(5)))
        self.assertTrue(all(code == 200 and item['attempt_id'] == attempt['attempt_id'] for code, item in repeats))
        self.assertEqual(self.fork.approvals, 1)
        self.fork.rows[self.rid]['status'] = 'paired'
        self.service.schedule_media_maintenance()
        await self.service._media_maintenance
        self.assertTrue(self.bridge.media_authorized('ipad', self.fp))
        code, final = await self.request('GET', '/v1/media/pairing/requests/' + attempt['attempt_id'], token=token)
        self.assertEqual(code, 200, final)
        self.assertTrue(final['paired'])
        self.assertTrue(self.bridge.media_authorized('ipad', self.fp))
        self.assertEqual(self.bridge.paired_certificate('ipad'), self.fp)
        self.assertNotIn('7391', self.bridge.store.path.read_text())
        self.assertNotIn('pin', json.dumps(final))
        self.assertEqual((await self.request('GET', '/v1/media/pairing/requests/' + attempt['attempt_id'], token=self.other))[0], 404)

    async def test_ordinary_approval_and_local_exact_binding(self):
        token, row, approved = await self.companion()
        self.assertNotIn('remote', approved['result'])
        code, attempt = await self.submit(token)
        self.assertEqual(attempt['status'], 'awaiting_local_approval')
        binding = {key: attempt[key] for key in ('attempt_id', 'request_id', 'client_cert_sha256')}
        pending = await self.local.request('local.media-pairing.pending')
        self.assertEqual(pending['result']['requests'][0]['attempt_id'], attempt['attempt_id'])
        bad = await self.local.request('local.media-pairing.approve', **{**binding, 'client_cert_sha256': 'b' * 64})
        self.assertFalse(bad['ok'])
        approved = await self.local.request('local.media-pairing.approve', **binding)
        self.assertTrue(approved['ok'], approved)
        self.assertEqual(approved['result']['status'], 'awaiting_client_proof')
        self.assertEqual(self.fork.approvals, 1)
        cancelled = await self.local.request('local.media-pairing.cancel', **binding)
        self.assertEqual(cancelled['result']['status'], 'cancelled')
        self.assertEqual((await self.request('GET', '/v1/media/pairing/requests/' + attempt['attempt_id'], token=token))[1]['status'], 'cancelled')

    async def test_claim_races_explicit_remote_grant(self):
        invitation = (await self.local.request('local.pair.begin'))['result']
        _, row = await self.request('POST', '/v1/pairing/requests', {
            'invitation': invitation['invitation'], 'device_id': 'ipad', 'device_name': 'iPad'})
        entered, release = threading.Event(), threading.Event()
        original = self.bridge.grant_remote
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(3): raise AssertionError('test grant barrier timed out')
            return original(*args, **kwargs)
        self.bridge.grant_remote = delayed
        approval = asyncio.create_task(self.local.request('local.pair.approve', request_id=row['request_id'], remote=True))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            token = await self.claim(row)
            _, attempt = await self.submit(token)
            self.assertEqual(attempt['status'], 'awaiting_local_approval')
        finally:
            release.set()
        self.assertTrue((await approval)['ok'])
        repeated = await self.local.request('local.pair.approve', request_id=row['request_id'], remote=True)
        self.assertTrue(repeated['ok'], repeated)
        self.assertEqual(repeated['result']['status'], 'claimed')
        _, final = await self.request('GET', '/v1/media/pairing/requests/' + attempt['attempt_id'], token=token)
        self.assertEqual(final['status'], 'awaiting_client_proof')
        self.assertEqual(self.fork.approvals, 1)

    async def test_public_and_local_authorities_cannot_be_confused(self):
        for path in ('/v1/media/pairing/grant-remote', '/v1/media/pairing/approve', '/v1/local/media-pairing/pending'):
            self.assertEqual((await self.request('POST', path, {'local': True}, self.other))[0], 404)
        token_client = JsonLineClient(self.ipc_path, self.other)
        self.assertFalse((await token_client.request('local.media-pairing.pending'))['ok'])
        self.assertFalse((await self.local.request('local.media-pairing.pending', local=True))['ok'])
        self.assertFalse((await self.local.request('local.media-pairing.grant-remote', device_id='other',
            source_request_id='pair_' + 'f' * 32))['ok'])
        original = self.ipc._peer_uid
        self.ipc.require_same_uid = False
        self.ipc._peer_uid = lambda writer: os.getuid() + 1
        try:
            self.assertFalse((await self.local.request('local.media-pairing.pending'))['ok'])
        finally:
            self.ipc._peer_uid = original
            self.ipc.require_same_uid = True

    async def test_strict_public_fields_and_cancel(self):
        token, _, _ = await self.companion()
        self.assertEqual((await self.submit(token, device_id='other'))[0], 400)
        self.assertEqual((await self.submit(token, local=True))[0], 400)
        self.assertEqual((await self.request('POST', '/v1/media/pairing/discover',
            {'client_cert_sha256': self.fp, 'pairing_intent': 1}, token))[0], 400)
        async with self.http.post(self.url + '/v1/media/pairing/requests',
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
            data='{"request_id":"a","request_id":"b","pin":"7391"}') as response:
            self.assertEqual(response.status, 400)
            self.assertNotIn('7391', await response.text())
        _, attempt = await self.submit(token)
        async with self.http.head(self.url + '/v1/media/pairing/requests/' + attempt['attempt_id'],
            headers={'Authorization': 'Bearer ' + token}) as response:
            self.assertEqual(response.status, 405)
        _, unchanged = await self.request('GET', '/v1/media/pairing/requests/' + attempt['attempt_id'], token=token)
        self.assertEqual(unchanged['status'], 'awaiting_local_approval')
        code, result = await self.request('DELETE', '/v1/media/pairing/requests/' + attempt['attempt_id'], {}, token)
        self.assertEqual(code, 200, result)
        self.assertEqual(result['status'], 'cancelled')
        self.assertFalse(self.bridge._pins)

    async def test_maintenance_singleflight_off_eventloop_and_close_wipes(self):
        token, _, _ = await self.companion()
        _, attempt = await self.submit(token)
        self.assertTrue(self.bridge._pins)
        entered, release = threading.Event(), threading.Event()
        worker_threads = []
        def blocked():
            worker_threads.append(threading.get_ident())
            entered.set()
            release.wait(3)
        self.bridge.maintenance = blocked
        self.service.schedule_media_maintenance()
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        for _ in range(20): self.service.schedule_media_maintenance()
        self.assertEqual((await self.request('GET', '/health'))[0], 200)
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], threading.get_ident())
        closing = asyncio.create_task(self.service.close_media())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        release.set()
        await closing
        self.assertFalse(self.bridge._pins)
        self.assertFalse(self.bridge._authorizers)
        self.service.schedule_media_maintenance()
        self.assertEqual(len(worker_threads), 1)

    async def test_legacy_deny_persists_and_device_revoke_keeps_cleanup_truth(self):
        self.assertFalse(self.bridge.remote_denied('other'))
        result = await self.local.request('local.media-pairing.revoke', device_id='other')
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['result']['media_revocation_complete'])
        self.assertTrue(self.bridge.remote_denied('other'))
        restarted = MediaPairingBridge(self.bridge.ipc, self.bridge.store)
        self.assertTrue(restarted.remote_denied('other'))
        # A denied device has no paired certificate, so the Sunshine backend
        # refuses it before the manager touches the compositor.
        self.assertIsNone(self.service.remote_certificate('other'))
        result = await self.local.request('local.devices.revoke', device_id='other')
        self.assertFalse(result['result']['media']['certificate_revocation_supported'])
        self.assertFalse(result['result']['media']['media_revocation_complete'])
        self.assertEqual((await self.request('GET', '/v1/state', token=self.other))[0], 401)

    async def test_revoking_a_device_also_drops_its_authorized_keys_line(self):
        """A revoked device keeps nothing, including SPEC-F3's SSH way in."""
        import base64, tempfile
        from pathlib import Path as _Path
        from omodachi_core.ssh_keys import AuthorizedKeys, MARKER
        with tempfile.TemporaryDirectory() as home:
            keys = AuthorizedKeys(_Path(home))
            self.service.ssh_keys = keys
            blob = b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + b'\x07' * 32
            mine = 'ssh-ed25519 ' + base64.b64encode(blob).decode() + ' alex@ipad'
            others = 'ssh-ed25519 ' + base64.b64encode(blob.replace(b'\x07', b'\x09')).decode() + ' alex@mac'
            keys.path.parent.mkdir(mode=0o700, parents=True)
            keys.path.write_text(others + '\n')
            keys.authorize(mine, 'other')
            result = await self.local.request('local.devices.revoke', device_id='other')
            self.assertEqual(result['result']['ssh'], {'revoked': True, 'removed': 1, 'device': 'other'})
            self.assertEqual(keys.path.read_text(), others + '\n')
            self.assertNotIn(MARKER, keys.path.read_text())

    async def test_an_unwritable_authorized_keys_is_reported_not_swallowed(self):
        import tempfile
        from pathlib import Path as _Path
        from omodachi_core.ssh_keys import AuthorizedKeys
        with tempfile.TemporaryDirectory() as home:
            keys = AuthorizedKeys(_Path(home))
            keys.path.parent.mkdir(mode=0o700, parents=True)
            (_Path(home) / 'elsewhere').write_text('')
            keys.path.symlink_to(_Path(home) / 'elsewhere')
            self.service.ssh_keys = keys
            result = await self.local.request('local.devices.revoke', device_id='other')
            self.assertEqual(result['result']['ssh']['error'], 'authorized_keys_unsafe')
            # The credential revocation itself still happened.
            self.assertEqual((await self.request('GET', '/v1/state', token=self.other))[0], 401)

    async def test_cli_uses_same_daemon_bridge_without_a_token(self):
        token, row, _ = await self.companion()
        _, attempt = await self.submit(token)
        env = dict(os.environ)
        env.pop('OMODACHI_TOKEN', None)
        command = [sys.executable, '-c', 'from omodachi_core.cli import host_main; raise SystemExit(host_main())',
            '--socket', self.ipc_path, 'media-pairing']
        proc = await asyncio.create_subprocess_exec(*command, 'pending', env=env, stdout=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(stdout)['result']['requests'][0]['attempt_id'], attempt['attempt_id'])
        proc = await asyncio.create_subprocess_exec(*command, 'approve', attempt['attempt_id'], self.rid, self.fp,
            env=env, stdout=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        self.assertEqual(proc.returncode, 0, stdout)
        self.assertEqual(json.loads(stdout)['result']['status'], 'awaiting_client_proof')
        self.assertEqual(self.fork.approvals, 1)

    # --- PAIR-3 -----------------------------------------------------------
    # 2026-09-19: a companion paired, SSH landed, and `grants` said
    # `{companion: true, media: false, ssh: true}`. Remote then had no
    # certificate, asked for one twice, and both attempts expired unapproved.
    # Streaming was opt-in, so anything that forgot to opt in produced a device
    # that looked paired on the computer and unpaired on the iPad.

    async def _pending_row(self, device='ipad'):
        invitation = (await self.local.request('local.pair.begin'))['result']
        code, row = await self.request('POST', '/v1/pairing/requests', {
            'invitation': invitation['invitation'], 'device_id': device, 'device_name': 'Fixture iPad'})
        self.assertEqual(code, 200, row)
        return row

    async def test_one_approval_grants_streaming_with_no_flag_to_remember(self):
        row = await self._pending_row()
        # No `remote` field at all: what the panel's Approve and a bare
        # `omodachi-host pair approve <id>` now send.
        approved = await self.local.request('local.pair.approve', request_id=row['request_id'])
        self.assertTrue(approved['ok'], approved)
        self.assertTrue(approved['result']['remote']['media_authorized'])
        self.assertTrue(approved['result']['grants']['media'])
        self.assertTrue(self.bridge.authorized_devices()['ipad'] == row['request_id'])

    async def test_no_remote_is_the_only_way_to_get_a_companion_only_approval(self):
        row = await self._pending_row()
        approved = await self.local.request('local.pair.approve', request_id=row['request_id'], remote=False)
        self.assertTrue(approved['ok'], approved)
        self.assertNotIn('remote', approved['result'])
        self.assertFalse(approved['result']['grants']['media'])
        self.assertEqual(self.bridge.authorized_devices(), {})

    async def test_a_streaming_grant_that_fails_is_reported_not_swallowed(self):
        row = await self._pending_row()
        def refuse(*args, **kwargs):
            raise MediaPairingError('media_pairing_capacity', 429)
        self.bridge.grant_remote = refuse
        approved = await self.local.request('local.pair.approve', request_id=row['request_id'])
        # The approval itself still stands - the companion credential is the
        # part the person is waiting for - and the missing half says why.
        self.assertTrue(approved['ok'], approved)
        self.assertFalse(approved['result']['remote']['media_authorized'])
        self.assertEqual(approved['result']['remote']['reason'], 'media_pairing_capacity')
        self.assertFalse(approved['result']['grants']['media'])
        token = await self.claim(row)
        self.assertEqual((await self.request('GET', '/v1/state', token=token))[0], 200)

    async def test_a_claimed_approval_outlives_the_request_ttl_and_still_grants_remote(self):
        """The half-paired device's only repair must not expire in five minutes.

        `media-pairing grant-remote` names the request that approved the
        device. Pruning every row at the 300 s TTL deleted the claimed row too,
        so the repair answered `pairing_approval_required` forever - which is
        exactly what it did for Leo's iPad.
        """
        row = await self._pending_row()
        self.assertTrue((await self.local.request('local.pair.approve',
            request_id=row['request_id'], remote=False))['ok'])
        await self.claim(row)
        never_claimed = await self._pending_row(device='second-ipad')
        path = self.root / 'pairing.json'
        state = json.loads(path.read_text())
        for value in state['requests'].values():
            value['expires_at'] = int(time.time()) - 600      # ten minutes on
        path.write_text(json.dumps(state))
        granted = await self.local.request('local.media-pairing.grant-remote',
            device_id='ipad', source_request_id=row['request_id'])
        self.assertTrue(granted['ok'], granted)
        self.assertTrue(granted['result']['media_authorized'])
        # Only the approval survives. An unclaimed request still expires, and
        # it still cannot authorize a grant.
        self.assertEqual((await self.local.request('local.pair.pending'))['result']['requests'], [])
        self.assertFalse((await self.local.request('local.media-pairing.grant-remote',
            device_id='second-ipad', source_request_id=never_claimed['request_id']))['ok'])
        # A revoke takes the durable approval back with everything else.
        self.assertTrue((await self.local.request('local.devices.revoke', device_id='ipad'))['ok'])
        self.assertFalse((await self.local.request('local.media-pairing.grant-remote',
            device_id='ipad', source_request_id=row['request_id']))['ok'])

    async def test_pending_keeps_the_permanent_revocation_records_out_of_the_to_do_list(self):
        token, _, _ = await self.companion(remote=True)
        _, attempt = await self.submit(token)
        pending = (await self.local.request('local.media-pairing.pending'))['result']
        self.assertEqual([item['attempt_id'] for item in pending['requests']], [attempt['attempt_id']])
        self.assertEqual(pending['history'], [])
        self.assertTrue((await self.local.request('local.devices.revoke', device_id='ipad'))['ok'])
        pending = (await self.local.request('local.media-pairing.pending'))['result']
        self.assertEqual(pending['requests'], [])
        self.assertEqual([item['status'] for item in pending['history']], ['cancellation_pending'])
        self.assertFalse(pending['certificate_revocation_supported'])

    async def test_devices_list_shows_the_streaming_grant_beside_the_credential(self):
        row = await self._pending_row()
        self.assertTrue((await self.local.request('local.pair.approve',
            request_id=row['request_id'], remote=False))['ok'])
        await self.claim(row)
        listed = {item['device_id']: item
                  for item in (await self.local.request('local.devices.list'))['result']['devices']}
        # Companion yes, streaming no - the state that used to be invisible here.
        self.assertFalse(listed['ipad']['media_authorized'])
        self.assertEqual(listed['ipad']['source_request_id'], row['request_id'])
        self.assertFalse(listed['other']['media_authorized'])
        self.assertEqual(listed['other']['source_request_id'], '')
        self.assertTrue((await self.local.request('local.media-pairing.grant-remote',
            device_id='ipad', source_request_id=row['request_id']))['ok'])
        listed = {item['device_id']: item
                  for item in (await self.local.request('local.devices.list'))['result']['devices']}
        self.assertTrue(listed['ipad']['media_authorized'])

    async def test_devices_list_names_a_device_holding_two_ssh_keys_and_can_prune_it(self):
        """UX-4 §3. The page that shows a device's grants shows its keys too.

        Two lines for one device is what a key drift leaves behind; before this
        the only way to see it was to read `ssh list` separately and notice a
        repeated name. A device with one key is never reported and never
        pruned - which is exactly what the two iPad records on the real host
        look like, and they have to come through untouched.
        """
        import base64
        import tempfile
        from pathlib import Path as _Path
        from omodachi_core.ssh_keys import AuthorizedKeys

        def ed25519(seed):
            blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([seed]) * 32
            return "ssh-ed25519 " + base64.b64encode(blob).decode() + " synthetic"

        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        keys = AuthorizedKeys(_Path(home.name))
        self.service.ssh_keys = keys
        await self.companion(device="ipad")  # `other` is registered in setUp
        keys.authorize(ed25519(1), "ipad")
        keys.authorize(ed25519(2), "ipad")
        keys.authorize(ed25519(3), "other")
        listed = {item["device_id"]: item
                  for item in (await self.local.request("local.devices.list"))["result"]["devices"]}
        self.assertTrue(listed["ipad"]["ssh_key_duplicates"])
        self.assertEqual(len(listed["ipad"]["ssh_keys"]), 2)
        self.assertFalse(listed["other"]["ssh_key_duplicates"])
        result = (await self.local.request("local.devices.list", prune_ssh_keys=True))["result"]
        self.assertEqual(result["ssh_keys_pruned"]["removed"], 1)
        listed = {item["device_id"]: item for item in result["devices"]}
        self.assertFalse(listed["ipad"]["ssh_key_duplicates"])
        # The newest line is the one kept, and the other device is untouched.
        self.assertEqual(listed["ipad"]["ssh_keys"],
                         [row["fingerprint"] for row in keys.listing() if row["device"] == "ipad"])
        self.assertEqual(len(listed["other"]["ssh_keys"]), 1)

    async def test_a_revoke_a_capable_fork_completes_takes_the_device_off_the_page(self):
        """PLUG-4 §2.3: with real certificate revocation there is nothing left
        to chase, so the device leaves the registry instead of becoming a
        `revoked` row with no action on it."""
        self.fork.revocation = True
        token, row, _ = await self.companion(remote=True)
        _, attempt = await self.submit(token)
        self.fork.rows[self.rid]['status'] = 'paired'
        await self.request('GET', '/v1/media/pairing/requests/' + attempt['attempt_id'], None, token)
        self.assertIn(self.fp, self.fork.certs)
        revoked = (await self.local.request('local.devices.revoke', device_id='ipad'))['result']
        self.assertTrue(revoked['media']['certificate_revocation_supported'])
        self.assertEqual(revoked['media']['pending_media_revocations'], 0)
        self.assertTrue(revoked['purged']['purged'])
        self.assertNotIn(self.fp, self.fork.certs)
        listed = (await self.local.request('local.devices.list'))['result']
        self.assertNotIn('ipad', [item['device_id'] for item in listed['devices']])
        self.assertEqual(listed['revoked_hidden'], 0)
        everything = (await self.local.request('local.devices.list', all=True))['result']
        self.assertNotIn('ipad', [item['device_id'] for item in everything['devices']])
        pending = (await self.local.request('local.media-pairing.pending'))['result']
        self.assertEqual(pending['requests'], [])
        self.assertEqual(pending['history'], [])
        self.assertTrue(pending['certificate_revocation_supported'])

    async def test_devices_list_hides_revoked_rows_and_purge_removes_them(self):
        """PLUG-4 §2.2 against a fork that cannot revoke: the row survives the
        revoke, it is not in the default list, and `purge` keeps it while the
        certificate is still unresolved."""
        token, row, _ = await self.companion(remote=True)
        _, attempt = await self.submit(token)
        self.fork.rows[self.rid]['status'] = 'paired'
        await self.request('GET', '/v1/media/pairing/requests/' + attempt['attempt_id'], None, token)
        self.assertTrue((await self.local.request('local.devices.revoke', device_id='ipad'))['ok'])
        listed = (await self.local.request('local.devices.list'))['result']
        self.assertEqual([item['device_id'] for item in listed['devices']], ['other'])
        self.assertEqual(listed['revoked_hidden'], 1)
        everything = (await self.local.request('local.devices.list', all=True))['result']
        self.assertEqual(sorted(item['device_id'] for item in everything['devices']), ['ipad', 'other'])
        self.assertEqual(everything['revoked_hidden'], 0)
        held = (await self.local.request('local.devices.purge'))['result']
        self.assertEqual(held['purged'], [])
        self.assertEqual([row['reason'] for row in held['kept']], ['media_revocation_pending'])
        self.assertIn('ipad', [item['device_id'] for item in
                               (await self.local.request('local.devices.list', all=True))['result']['devices']])
        # Once the fork can revoke, the same purge finishes it.
        self.fork.revocation = True
        done = (await self.local.request('local.devices.purge'))['result']
        self.assertEqual(done['purged'], ['ipad'])
        self.assertEqual(done['kept'], [])
        self.assertNotIn(self.fp, self.fork.certs)
        everything = (await self.local.request('local.devices.list', all=True))['result']
        self.assertEqual([item['device_id'] for item in everything['devices']], ['other'])
        # An authorized device and this host's own credential are never purged.
        self.assertEqual((await self.local.request('local.devices.purge'))['result']['purged'], [])

    async def test_certificates_lists_the_unknown_ones_and_purges_only_those(self):
        """The follow-up in one call: the leftovers the fork authorized and no
        binding here claims, found and taken back without touching the device
        that is actually paired."""
        self.fork.revocation = True
        token, _, _ = await self.companion(remote=True)
        _, attempt = await self.submit(token)
        self.fork.rows[self.rid]['status'] = 'paired'
        await self.request('GET', '/v1/media/pairing/requests/' + attempt['attempt_id'], None, token)
        self.fork.certs.add('d' * 64)
        self.fork.names['d' * 64] = 'ipad-sim'
        listed = (await self.local.request('local.media-pairing.certificates'))['result']
        self.assertEqual(listed['unknown'], 1)
        self.assertEqual({row['client_cert_sha256']: row['device_id'] for row in listed['certificates']},
                         {self.fp: 'ipad', 'd' * 64: ''})
        self.assertEqual(self.fork.certs, {self.fp, 'd' * 64})
        purged = (await self.local.request('local.media-pairing.certificates', purge_unknown=True))['result']
        self.assertEqual(purged['revoked'], ['d' * 64])
        self.assertEqual(self.fork.certs, {self.fp})
        self.assertTrue(self.bridge.media_authorized('ipad', self.fp))
        self.assertFalse((await self.local.request('local.media-pairing.certificates',
                                                   purge_unknown='yes'))['ok'])

    async def test_purge_older_than_never_removes_a_device_it_cannot_date(self):
        self.fork.revocation = True
        token, row, _ = await self.companion(remote=True)
        _, attempt = await self.submit(token)
        self.assertTrue((await self.local.request('local.devices.revoke', device_id='ipad'))['ok'])
        # `other` was registered straight into the credential registry and has
        # no media permission row, so it has no age; `ipad` was revoked now.
        self.hub.auth.revoke_device('other')
        kept = (await self.local.request('local.devices.purge', older_than_days=1))['result']
        self.assertEqual(kept['purged'], [])
        self.assertEqual(sorted(row['device_id'] for row in kept['kept']), ['ipad', 'other'])
        self.assertTrue(all(row['reason'] == 'newer_than_cutoff' for row in kept['kept']))
        self.assertFalse((await self.local.request('local.devices.purge', older_than_days=-1))['ok'])

    async def test_cli_approve_defaults_to_remote_and_no_remote_opts_out(self):
        env = dict(os.environ)
        env.pop('OMODACHI_TOKEN', None)
        async def approve(row, *flags):
            proc = await asyncio.create_subprocess_exec(
                sys.executable, '-c', 'from omodachi_core.cli import host_main; raise SystemExit(host_main())',
                '--socket', self.ipc_path, 'pair', 'approve', row['request_id'], *flags,
                env=env, stdout=asyncio.subprocess.PIPE)
            stdout, _ = await proc.communicate()
            self.assertEqual(proc.returncode, 0, stdout)
            return json.loads(stdout)['result']
        bare = await approve(await self._pending_row(device='bare-ipad'))
        self.assertTrue(bare['grants']['media'])
        # `--remote` is what every report and every habit says to pass. It has
        # to keep working, as the no-op it now is.
        legacy = await approve(await self._pending_row(device='legacy-ipad'), '--remote')
        self.assertTrue(legacy['grants']['media'])
        opted_out = await approve(await self._pending_row(device='plain-ipad'), '--no-remote')
        self.assertFalse(opted_out['grants']['media'])
        self.assertNotIn('remote', opted_out)
