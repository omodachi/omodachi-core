"""AGENT-NEW-1: "New conversation" for the default Agent, stuck tool rows and
blank agent messages.

Controlled fakes only: a codex that keeps threads the way 0.154 does (a fresh
thread is loaded but has no rollout until its first message), the real owner,
the real adapter, the real service and the real HTTP routes. Never a user's
codex daemon.
"""
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import aiohttp

from omodachi_core.agent import ProbeStatus
from omodachi_core.agent_chat_owner import OwnedCodexAgent, NEW_CONVERSATION
from omodachi_core.agent_chat_provider import AgentChatError, CodexDefaultAgentChat, DefaultAgentBinding
from omodachi_core.agent_chat_service import DefaultAgentChatService
from omodachi_core.agent_lifecycle import DefaultAgentManager
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub
from omodachi_core.network import NetworkServer


class FakeCodex:
    """Threads, loaded threads, rollouts and turns, as the app-server keeps them."""

    def __init__(self):
        self.calls = []
        self.threads = {'old-thread': {'materialized': True, 'status': {'type': 'idle'}, 'cwd': '/stale'}}
        self.loaded = []
        self.started = 0
        self.fail_start = None
        self.ordinal = 0
        self.on_notification = lambda *_: None
        self.on_server_request = lambda value: None
        self.on_disconnect = lambda: None
        self.closed = False

    async def initialize(self): return {}
    async def close(self): self.closed = True
    async def respond(self, request_id, result): pass
    async def respond_error(self, request_id, code, message): pass

    async def request(self, method, params):
        result, _ = await self.request_fenced(method, params)
        return result

    async def request_fenced(self, method, params):
        self.calls.append((method, dict(params)))
        self.ordinal += 1
        thread = params.get('threadId')
        if method == 'thread/loaded/list':
            return {'data': list(self.loaded), 'nextCursor': None}, self.ordinal
        if method == 'thread/resume':
            if thread not in self.threads:
                raise AgentChatError('provider_request_rejected')
            if not self.threads[thread]['materialized']:
                raise AgentChatError('provider_thread_rollout_missing')
            if thread not in self.loaded: self.loaded.append(thread)
            if 'cwd' in params: self.threads[thread]['cwd'] = params['cwd']
            return {'thread': {'id': thread, 'turns': self.turns(thread)}, 'model': 'gpt-6-sol',
                    'reasoningEffort': 'high'}, self.ordinal
        if method == 'thread/read':
            if params.get('includeTurns') and not self.threads[thread]['materialized']:
                raise AgentChatError('provider_thread_not_materialized')
            return {'thread': {'id': thread, 'turns': self.turns(thread) if params.get('includeTurns') else [],
                               'status': self.threads[thread]['status']}}, self.ordinal
        if method == 'thread/start':
            if self.fail_start:
                raise AgentChatError(self.fail_start)
            self.started += 1
            ident = 'new-thread-' + str(self.started)
            self.threads[ident] = {'materialized': False, 'status': {'type': 'idle'}, 'cwd': params['cwd']}
            self.loaded.append(ident)
            return {'thread': {'id': ident}, 'model': 'gpt-6-sol', 'reasoningEffort': 'medium',
                    'cwd': params['cwd']}, self.ordinal
        if method == 'turn/start':
            self.threads[thread]['materialized'] = True
            if 'cwd' in params: self.threads[thread]['cwd'] = params['cwd']
            return {'turn': {'id': 'turn-' + str(self.ordinal), 'status': 'inProgress'}}, self.ordinal
        if method == 'thread/queue/list':  # AGENT-QUEUE-1: a codex without the queue
            raise AgentChatError('provider_request_rejected')
        raise AssertionError(method)

    def turns(self, thread):
        if thread != 'old-thread':
            return []
        return [{'id': 'old-turn', 'status': 'completed', 'items': [
            {'type': 'userMessage', 'id': 'u1', 'content': [{'type': 'text', 'text': 'hello from before'}]},
            {'type': 'agentMessage', 'id': 'a1', 'text': 'Hi.'}]}]


class Harness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.cap = SimpleNamespace(configured=True, configured_kind='codex', default_agent_exists=False,
                                   default_agent_probe=ProbeStatus.MISSING)
        self.manager = DefaultAgentManager(root=home / '.config/omodachi',
                                           probe=SimpleNamespace(inspect=lambda: (self.cap, None)),
                                           runner=lambda _: self.fail('no TUI'))
        self.workspace = home / '.local/share/omodachi/agent-workspace'
        self.manager.cwd = self.workspace
        self.codex = FakeCodex()

        async def proxy(): return self.codex
        self.owner = OwnedCodexAgent(self.manager.root / 'structured-default', self.workspace, 'testhost',
                                     rpc_factory=proxy)
        self.owner._write_endpoint(47311)
        self.owner._save(self.owner._record('old-thread', creation_pending=False))
        self.hub = Hub()
        self.token = self.hub.register_device('phone')
        self.chat = DefaultAgentChatService(self.manager, 'testhost', owner=self.owner, hub=self.hub)
        self.service = create_service(self.hub, demo=True)
        self.service.agent_manager = self.manager
        self.service.agent_chat = self.chat
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        self.url = f'http://127.0.0.1:{self.server.bound_port}'

    async def asyncTearDown(self):
        await self.client.close()
        await self.chat.close()
        await self.server.close()
        self.tmp.cleanup()

    def record(self):
        return json.loads((self.owner.root / 'owner.json').read_text())

    async def post(self, path, body, token=None):
        out = io.StringIO()
        with redirect_stdout(out):
            async with self.client.post(self.url + path, json=body,
                                        headers={'Authorization': 'Bearer ' + (token or self.token)}) as response:
                status, value = response.status, await response.json()
        lines = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith('{')]
        return status, value, [line for line in lines if line.get('omodachi') == 'agent']

    async def ensure(self):
        status, value, _ = await self.post('/v1/agent/default:ensure', {'surface': 'chat'})
        self.assertEqual(status, 200, value)
        return value

    def new(self, expected='old-thread', confirmed=True):
        return self.post('/v1/agent/default/chat:new', {'confirmed': confirmed,
                                                         'expected_provider_session_id': expected})


class NewConversationTests(Harness):
    async def test_a_new_conversation_keeps_the_old_thread_and_starts_empty(self):
        first = await self.ensure()
        self.assertEqual(first['identity']['providerSessionID'], 'old-thread')
        self.assertEqual(len(first['snapshot']['rows']), 2)
        ws = await self.client.ws_connect(self.url + '/v1/agent/default/chat/events',
                                          headers={'Authorization': 'Bearer ' + self.token})
        self.assertEqual((await ws.receive_json())['type'], 'snapshot')
        status, value, audit = await self.new()
        self.assertEqual(status, 200, value)
        self.assertEqual((value['status'], value['previous_provider_session_id'], value['provider_session_id'],
                          value['same_thread'], value['previous_kept']),
                         ('new_conversation_started', 'old-thread', 'new-thread-1', False, True))
        self.assertEqual(value['snapshot']['rows'], [])
        self.assertEqual(value['identity']['providerSessionID'], 'new-thread-1')
        # The thread starts in the workspace; the old one is never archived,
        # deleted or otherwise touched in codex.
        start = [p for m, p in self.codex.calls if m == 'thread/start']
        self.assertEqual(start, [{'cwd': str(self.workspace)}])
        self.assertFalse(any(m in {'thread/archive', 'thread/delete', 'thread/unsubscribe'} for m, _ in self.codex.calls))
        self.assertIn('old-thread', self.codex.threads)
        # Core's record moved on and keeps where it came from.
        record = self.record()
        self.assertEqual((record['thread_id'], record['previous_thread_id'], record['recovery_reason'],
                          record['creation_pending']), ('new-thread-1', 'old-thread', NEW_CONVERSATION, False))
        archived = json.loads((self.owner.root / record['previous_metadata']).read_text())
        self.assertEqual(archived['thread_id'], 'old-thread')
        self.assertRegex(record['new_conversation_at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        # agent.changed: the face is idle, nothing waits.
        chat_state = self.hub.state_snapshot()['agent']['chat']
        self.assertEqual(chat_state['activity'], {'phase': 'idle', 'turn_id': None})
        self.assertEqual(chat_state['pending_approvals'], 0)
        # Whoever watched the old conversation is told to read again.
        self.assertEqual(await ws.receive_json(), {'type': 'resync_required'})
        await ws.close()
        # The model control knows what the fresh thread runs with.
        self.assertEqual(value['snapshot']['usage']['model'], 'gpt-6-sol')
        # One audit line, without words.
        self.assertEqual(audit, [{'omodachi': 'agent', 'event': 'agent.conversation.new', 'device': 'phone',
                                  'role': 'companion', 'previous_thread_id': 'old-thread', 'outcome': 'started',
                                  'thread_id': 'new-thread-1'}])
        # The next message goes to the new thread, in the workspace.
        status, sent, _ = await self.post('/v1/agent/default/chat/messages', {'request_id': 'r1', 'text': 'pwd'})
        self.assertEqual(status, 200, sent)
        turn = [p for m, p in self.codex.calls if m == 'turn/start'][-1]
        self.assertEqual((turn['threadId'], turn['cwd']), ('new-thread-1', str(self.workspace)))
        # And a fresh ensure (another phone) gets the same new conversation.
        again = await self.ensure()
        self.assertEqual(again['identity']['providerSessionID'], 'new-thread-1')

    async def test_refused_while_a_turn_runs_or_a_question_waits(self):
        await self.ensure()
        self.chat.chat.active_turn = 'turn-running'
        status, value, audit = await self.new()
        self.assertEqual((status, value['error']['code']), (409, 'agent_turn_active'))
        self.assertEqual(audit[0]['outcome'], 'agent_turn_active')
        self.chat.chat.active_turn = None
        self.chat.chat.pending_approvals['7'] = {'request_id': '7'}
        status, value, _ = await self.new()
        self.assertEqual((status, value['error']['code']), (409, 'agent_approval_pending'))
        self.assertFalse(any(m == 'thread/start' for m, _ in self.codex.calls))
        self.assertEqual(self.record()['thread_id'], 'old-thread')
        self.assertEqual(list(self.owner.root.glob('owner-before-new-conversation-*')), [])

    async def test_codex_s_own_status_refuses_when_no_chat_is_attached(self):
        # Core restarted mid-turn: no adapter, but the thread is active in codex.
        self.codex.loaded.append('old-thread')
        self.codex.threads['old-thread']['status'] = {'type': 'active', 'activeFlags': ['waitingOnApproval']}
        status, value, _ = await self.new()
        self.assertEqual((status, value['error']['code']), (409, 'agent_approval_pending'))
        self.codex.threads['old-thread']['status'] = {'type': 'active', 'activeFlags': []}
        status, value, _ = await self.new()
        self.assertEqual((status, value['error']['code']), (409, 'agent_turn_active'))
        self.assertEqual(self.record()['thread_id'], 'old-thread')

    async def test_a_turn_s_model_and_effort_show_once_the_provider_took_it(self):
        value = await self.ensure()
        self.assertEqual((value['snapshot']['usage']['model'], value['snapshot']['usage']['effort']), ('gpt-6-sol', 'high'))
        status, sent, _ = await self.post('/v1/agent/default/chat/messages',
                                          {'request_id': 'r1', 'text': 'ok', 'model': 'gpt-6-sol', 'effort': 'low'})
        self.assertEqual(status, 200, sent)
        usage = self.chat.chat.usage_value()
        self.assertEqual((usage['model'], usage['effort']), ('gpt-6-sol', 'low'))
        self.assertEqual(self.hub.state_snapshot()['agent']['usage']['effort'], 'low')

    async def test_a_lost_provider_connection_is_reopened_first(self):
        await self.ensure()
        self.chat.chat._disconnected()  # codex went away under the old chat
        status, value, _ = await self.new()
        self.assertEqual(status, 200, value)
        self.assertTrue(self.codex.closed, 'the dead connection was closed, a fresh one opened')
        self.assertEqual(value['provider_session_id'], 'new-thread-1')

    async def test_needs_confirmation_and_the_conversation_the_phone_is_looking_at(self):
        await self.ensure()
        status, value, _ = await self.new(confirmed=False)
        self.assertEqual((status, value['error']['code']), (409, 'new_conversation_confirmation_required'))
        status, value, _ = await self.new(expected='some-other-thread')
        self.assertEqual((status, value['error']['code']), (409, 'agent_identity_changed'))
        # Two phones press at once: one new conversation.
        status, _, _ = await self.new()
        self.assertEqual(status, 200)
        status, value, _ = await self.new()
        self.assertEqual((status, value['error']['code']), (409, 'agent_identity_changed'))
        self.assertEqual(self.codex.started, 1)

    async def test_a_web_device_may_start_one_too(self):
        web = self.hub.auth.issue('odomo-web1', role='web').token
        status, value, audit = await self.new_as(web)
        self.assertEqual(status, 200, value)
        self.assertEqual((audit[0]['device'], audit[0]['role']), ('odomo-web1', 'web'))

    def new_as(self, token):
        return self.post('/v1/agent/default/chat:new', {'confirmed': True,
                                                         'expected_provider_session_id': 'old-thread'}, token=token)


class CrashSafetyTests(Harness):
    async def test_intent_is_saved_before_the_rpc_and_finished_on_the_next_ensure(self):
        await self.ensure()
        self.codex.fail_start = 'provider_disconnected'
        status, value, audit = await self.new()
        self.assertEqual((status, value['error']['code']), (409, 'provider_disconnected'))
        record = self.record()
        self.assertEqual((record['thread_id'], record['creation_pending'], record['previous_thread_id'],
                          record['recovery_reason']), (None, True, 'old-thread', NEW_CONVERSATION))
        self.assertEqual(audit[0]['outcome'], 'provider_disconnected')
        # The daemon comes back: ensure finishes what the person asked for.
        self.codex.fail_start = None
        await self.chat.close()
        self.chat.chat = None
        again = await self.ensure()
        self.assertEqual(again['identity']['providerSessionID'], 'new-thread-1')
        self.assertEqual(self.record()['previous_thread_id'], 'old-thread')
        self.assertFalse(self.record()['creation_pending'])

    async def test_retrying_the_request_finishes_the_same_intent_once(self):
        await self.ensure()
        self.codex.fail_start = 'provider_disconnected'
        await self.new()
        self.codex.fail_start = None
        status, value, _ = await self.new()
        self.assertEqual(status, 200, value)
        self.assertEqual(value['provider_session_id'], 'new-thread-1')
        self.assertEqual(len(list(self.owner.root.glob('owner-before-new-conversation-*'))), 1)

    async def test_plain_first_creation_is_still_never_retried(self):
        # Only a new-conversation intent is finished on its own; the original
        # "uncertain creation" guard is unchanged.
        self.owner._save(self.owner._record(creation_pending=True))
        with self.assertRaisesRegex(AgentChatError, 'agent_thread_creation_unconfirmed'):
            await self.owner.ensure_new_or_owned()


class CodexAfterRebootTests(unittest.IsolatedAsyncioTestCase):
    """The host rebooted; omodachid started before the session imported its
    PATH (it saw /usr/local/bin:/usr/bin), and the person's codex is a mise
    shim. Core still finds it, through the user manager's PATH or the usual
    per-user directories, and the unit it starts gets the same PATH."""

    async def test_codex_is_found_where_the_person_has_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            shims = home / '.local/share/mise/shims'
            shims.mkdir(parents=True)
            (shims / 'codex').write_text('#!/bin/sh\n')
            (shims / 'codex').chmod(0o755)
            owner = OwnedCodexAgent(home / 'structured-default', home / 'ws', 'h',
                                    rpc_factory=None, agent_dir=home / 'agent')
            owner.durable_unit = True
            calls = []

            async def unit(*argv):
                calls.append(argv)
                if argv[:3] == ('systemctl', '--user', 'is-active'):
                    return 3, 'inactive'
                if argv[:3] == ('systemctl', '--user', 'show-environment'):
                    return 0, 'HOME=' + str(home) + '\nPATH=/usr/local/bin:/usr/bin\n'
                return 0, ''
            owner._unit_command = unit
            import os as os_module
            from unittest import mock
            with mock.patch.dict(os_module.environ, {'PATH': '/usr/local/bin:/usr/bin'}), \
                    mock.patch('omodachi_core.agent_chat_owner.Path.home', return_value=home):
                await owner._spawn()
            run = next(argv for argv in calls if argv[0] == 'systemd-run')
            self.assertIn(str(shims / 'codex'), run)
            path = next(arg for arg in run if arg.startswith('--setenv=PATH='))
            self.assertIn(str(shims), path)
            self.assertIn('/usr/bin', path.removeprefix('--setenv=PATH=').split(':'))

    def test_the_search_path_keeps_order_and_drops_duplicates(self):
        from omodachi_core.agent_chat_owner import codex_search_path
        value = codex_search_path('/usr/local/bin:/usr/bin', '/opt/x/bin:/usr/bin:relative', '/home/u')
        parts = value.split(':')
        self.assertEqual(parts[:3], ['/usr/local/bin', '/usr/bin', '/opt/x/bin'])
        self.assertIn('/home/u/.local/share/mise/shims', parts)
        self.assertEqual(len(parts), len(set(parts)))
        self.assertNotIn('relative', parts)


class ToolRowsAndBlankMessagesTests(unittest.IsolatedAsyncioTestCase):
    async def chat(self, turns=()):
        rpc = FakeCodex()
        rpc.loaded = ['old-thread']
        rpc.turns = lambda thread: list(turns)
        chat = CodexDefaultAgentChat(DefaultAgentBinding('h', 'old-thread', 'ws://127.0.0.1:47311', 'a' * 64), rpc)
        await chat.attach()
        return chat, rpc

    def drain(self, chat):
        out = []
        while not chat.events.empty(): out.append(chat.events.get_nowait()['event'])
        return out

    def run_tool(self, chat, turn_status):
        notify = lambda method, **p: chat._receive({'method': method, 'params': {'threadId': 'old-thread', **p}}, 0)
        notify('turn/started', turn={'id': 't1'})
        notify('item/started', turnId='t1', item={'type': 'commandExecution', 'id': 'cmd', 'command': 'sleep 8',
                                                   'status': 'inProgress', 'aggregatedOutput': ''})
        # codex never sends item/completed for a command that could not start.
        notify('turn/completed', turn={'id': 't1', 'status': turn_status})

    async def test_a_finished_turn_closes_the_tools_it_left_running(self):
        for turn_status, expected in (('interrupted', 'interrupted'), ('failed', 'failed'), ('completed', 'failed')):
            chat, _ = await self.chat()
            self.run_tool(chat, turn_status)
            events = self.drain(chat)
            closing = [e for e in events if e['type'] == 'tool' and e['value']['status'] != 'running']
            self.assertEqual([e['value']['status'] for e in closing], [expected], turn_status)
            # Closed before the turn's own end, while clients still follow it.
            self.assertLess(events.index(closing[0]),
                            next(i for i, e in enumerate(events) if e['type'] in ('turnFinished', 'turnFailed')))
            self.assertEqual(chat.snapshot_value()['rows'][0]['status'], expected)

    async def test_history_of_a_finished_turn_has_no_running_tool(self):
        chat, _ = await self.chat([{'id': 'old', 'status': 'interrupted', 'items': [
            {'type': 'commandExecution', 'id': 'cmd', 'command': 'x', 'status': 'inProgress', 'aggregatedOutput': ''}]},
            {'id': 'now', 'status': 'inProgress', 'items': [
            {'type': 'commandExecution', 'id': 'live', 'command': 'y', 'status': 'inProgress', 'aggregatedOutput': ''}]}])
        rows = {r['id']: r['status'] for r in chat.snapshot_value()['rows']}
        self.assertEqual(rows, {'cmd': 'interrupted', 'live': 'running'})

    async def test_whitespace_only_agent_messages_never_leave_core(self):
        chat, _ = await self.chat([{'id': 'old', 'status': 'completed', 'items': [
            {'type': 'agentMessage', 'id': 'blank', 'text': ' '},
            {'type': 'agentMessage', 'id': 'said', 'text': 'Done.'}]}])
        self.assertEqual([r['id'] for r in chat.snapshot_value()['rows']], ['said'])
        notify = lambda method, **p: chat._receive({'method': method, 'params': {'threadId': 'old-thread', **p}}, 0)
        notify('turn/started', turn={'id': 't1'})
        notify('item/started', turnId='t1', item={'type': 'agentMessage', 'id': 'space', 'text': ''})
        notify('item/agentMessage/delta', turnId='t1', itemId='space', delta=' ')
        notify('item/completed', turnId='t1', item={'type': 'agentMessage', 'id': 'space', 'text': ' '})
        notify('item/started', turnId='t1', item={'type': 'agentMessage', 'id': 'real', 'text': ''})
        notify('item/agentMessage/delta', turnId='t1', itemId='real', delta='\n')
        notify('item/agentMessage/delta', turnId='t1', itemId='real', delta='Hello')
        notify('item/completed', turnId='t1', item={'type': 'agentMessage', 'id': 'real', 'text': '\nHello'})
        messages = [e['value'] for e in self.drain(chat) if e['type'] == 'message']
        self.assertTrue(messages and all(m['text'].strip() for m in messages))
        self.assertEqual({m['id'] for m in messages}, {'real'})
        self.assertEqual(messages[0]['text'], '\nHello')  # streaming kept: the first visible delta
        self.assertEqual([r['id'] for r in chat.snapshot_value()['rows']], ['said', 'real'])
        self.assertNotIn('space', chat.rows)


if __name__ == '__main__':
    unittest.main()
