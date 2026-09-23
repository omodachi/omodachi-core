"""Controlled protocol tests only; never attach to a user's Codex daemon."""
import asyncio
import os
from pathlib import Path
import shutil
import tempfile
import unittest

import json

from omodachi_core.agent_chat_provider import (
    AgentChatError, CodexDefaultAgentChat, CodexJSONRPC, CodexUnixRPC, CodexWebSocketRPC,
    DefaultAgentBinding, project_item, read_codex_chatgpt_tokens,
)


class ControlledRPC:
    def __init__(self, loaded=True):
        self.loaded = loaded
        self.calls = []
        self.on_notification = lambda *_: None
        self.ordinal = 0
        self.closed = False
        self.start_count = 0
        self.answers = []
        self.errors = []
        self.on_server_request = lambda value: None

    async def request(self, method, params):
        result, _ = await self.request_fenced(method, params)
        return result

    async def request_fenced(self, method, params):
        self.calls.append((method, params))
        self.ordinal += 1
        if method == 'model/list':
            page = {'data': [{'id': 'gpt-6-astra', 'model': 'gpt-6-astra', 'displayName': 'GPT-6-Astra',
                              'description': 'big', 'hidden': False, 'isDefault': True,
                              'defaultReasoningEffort': 'medium',
                              'supportedReasoningEfforts': [{'reasoningEffort': 'low'}, {'reasoningEffort': 'high'}]}]}
            if not params.get('cursor'):
                page['nextCursor'] = 'page-2'
            return page, self.ordinal
        if method == 'turn/steer':
            return {}, self.ordinal
        if method == 'thread/loaded/list':
            return {'data': ['thread-owned'] if self.loaded else []}, self.ordinal
        if method in {'thread/resume', 'thread/read'}:
            return {'thread': {'id': 'thread-owned', 'turns': []}}, self.ordinal
        if method == 'turn/start':
            self.start_count += 1
            return {'turn': {'id': 'turn-one', 'status': 'inProgress'}}, self.ordinal
        if method == 'turn/interrupt':
            return {}, self.ordinal
        raise AssertionError(method)

    def notify(self, method, **params):
        self.ordinal += 1
        self.on_notification({'method': method, 'params': params}, self.ordinal)

    def server_request(self, method, request_id, **params):
        self.on_server_request({'id': request_id, 'method': method, 'params': params})

    async def respond(self, request_id, result):
        self.answers.append((request_id, result))

    async def respond_error(self, request_id, code, message):
        self.errors.append((request_id, code, message))

    async def close(self):
        self.closed = True


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def binding(self):
        return DefaultAgentBinding('host-owned', 'thread-owned', 'ws://127.0.0.1:47311', 'a' * 64)

    async def test_existing_identity_snapshot_stream_send_cancel(self):
        rpc = ControlledRPC()
        chat = CodexDefaultAgentChat(self.binding(), rpc)
        state = await chat.attach()
        self.assertEqual(state['identity']['providerSessionID'], 'thread-owned')
        self.assertFalse(any(m == 'thread/start' for m, _ in rpc.calls))
        result = await chat.send('test message', 'request-one')
        self.assertTrue(result['accepted'])
        duplicate = await chat.send('test message', 'request-one')
        self.assertTrue(duplicate['accepted'])
        self.assertEqual(rpc.start_count, 1)
        with self.assertRaisesRegex(AgentChatError, 'agent_busy'):
            await chat.send('another', 'request-two')
        rpc.notify('turn/started', threadId='thread-owned', turn={'id': 'turn-one'})
        rpc.notify('item/started', threadId='thread-owned', turnId='turn-one',
                   item={'type': 'agentMessage', 'id': 'answer', 'text': ''})
        rpc.notify('item/agentMessage/delta', threadId='thread-owned', turnId='turn-one', itemId='answer', delta='Actual JSON delta')
        rpc.notify('item/started', threadId='thread-owned', turnId='turn-one',
                   item={'type': 'commandExecution', 'id': 'tool', 'command': 'synthetic command', 'status': 'inProgress', 'aggregatedOutput': ''})
        rpc.notify('item/completed', threadId='thread-owned', turnId='turn-one',
                   item={'type': 'commandExecution', 'id': 'tool', 'command': 'synthetic command', 'status': 'completed', 'aggregatedOutput': 'synthetic output'})
        self.assertEqual(chat.rows['answer']['text'], 'Actual JSON delta')
        self.assertEqual(chat.rows['tool']['status'], 'succeeded')
        await chat.cancel('turn-one')
        self.assertEqual(chat.active_turn, 'turn-one', 'cancel ACK must not finish turn')
        rpc.notify('turn/completed', threadId='thread-owned', turn={'id': 'turn-one', 'status': 'interrupted'})
        self.assertIsNone(chat.active_turn)
        foreign_count = chat.sequence
        rpc.notify('turn/started', threadId='another-thread', turn={'id': 'unrelated'})
        self.assertEqual(chat.sequence, foreign_count)
        events = []
        while not chat.events.empty(): events.append(chat.events.get_nowait())
        first_answer = next(e for e in events if e['event']['type']=='message')
        self.assertEqual(first_answer['event']['value']['text'], '', 'queued partial event cannot mutate retroactively')
        self.assertTrue(events[-1]['event']['interrupted'])
        await chat.close()
        self.assertTrue(rpc.closed)
        self.assertFalse(any(m in {'thread/start','thread/archive'} for m, _ in rpc.calls))

    async def test_refuses_missing_owner_and_path_inference(self):
        rpc = ControlledRPC(loaded=False)
        chat = CodexDefaultAgentChat(self.binding(), rpc)
        with self.assertRaisesRegex(AgentChatError, 'default_agent_owner_not_attached'):
            await chat.attach()
        self.assertEqual([m for m, _ in rpc.calls], ['thread/loaded/list'])
        with self.assertRaises(AgentChatError):
            DefaultAgentBinding.from_ensured_agent('host', {'name':'default','agent':'codex','agent_session':{'agent':'codex','kind':'path','value':'/private/transcript'}}, endpoint='ws://127.0.0.1:47311')
        binding = DefaultAgentBinding.from_ensured_agent('host', {'name':'default','agent':'codex','agent_session':{'agent':'codex','kind':'id','value':'thread-owned'}}, endpoint='ws://127.0.0.1:47311', token='b' * 64)
        self.assertEqual(binding.thread_id, 'thread-owned')
        self.assertEqual(binding.remote_url, 'ws://127.0.0.1:47311')
        with self.assertRaises(AgentChatError):
            # A client-shaped endpoint is not a manager endpoint: nothing but
            # loopback or an absolute owned socket path may be connected to.
            DefaultAgentBinding('host', 'thread-owned', 'ws://10.0.0.5:47311')

    async def test_unknown_delivery_is_not_retried(self):
        class LostRPC(ControlledRPC):
            async def request(self, method, params):
                if method == 'turn/start':
                    self.start_count += 1
                    raise AgentChatError('provider_disconnected')
                return await super().request(method, params)
        rpc = LostRPC(); chat = CodexDefaultAgentChat(self.binding(), rpc)
        await chat.attach()
        with self.assertRaises(AgentChatError): await chat.send('one', 'same-id')
        self.assertEqual((await chat.send('one','same-id'))['delivery'], 'unknown')
        self.assertEqual(rpc.start_count, 1)
        with self.assertRaisesRegex(AgentChatError, 'request_conflict'): await chat.send('changed','same-id')

    async def test_snapshot_fence_discards_old_and_keeps_new_events(self):
        class RacingRPC(ControlledRPC):
            async def request_fenced(self, method, params):
                if method == 'thread/resume':
                    self.notify('turn/started', threadId='thread-owned', turn={'id':'old'})
                    barrier = self.ordinal + 1; self.ordinal = barrier
                    self.notify('turn/started', threadId='thread-owned', turn={'id':'new'})
                    return {'thread':{'id':'thread-owned','turns':[]}}, barrier
                return await super().request_fenced(method, params)
        chat = CodexDefaultAgentChat(self.binding(), RacingRPC())
        snapshot = await chat.attach()
        self.assertEqual(snapshot['activeTurn'], 'new')
        self.assertEqual(chat.events.qsize(), 1)

    def test_unknown_status_not_invented_and_reasoning_not_exposed(self):
        row = project_item({'id':'a','type':'commandExecution','status':'newUpstreamState'}, 't')
        self.assertEqual(row['status'], 'unknown')
        self.assertIsNone(project_item({'id':'b','type':'reasoning','text':'hidden'}, 't'))

    async def test_precise_missing_classification_does_not_swallow_generic_errors(self):
        for number, message, expected in [
            (-32600, 'thread not loaded: test-id', 'provider_thread_not_loaded'),
            (-32600, 'no rollout found for thread id test-id', 'provider_thread_rollout_missing'),
            (-32600, 'some other invalid request', 'provider_request_rejected'),
            (-32000, 'thread not loaded: test-id', 'provider_request_rejected')]:
            rpc = CodexJSONRPC.__new__(CodexJSONRPC)
            future = asyncio.get_running_loop().create_future()
            rpc.pending = {1: future}
            rpc._dispatch({'id':1,'error':{'code':number,'message':message}})
            with self.assertRaisesRegex(AgentChatError, expected): await future

    @unittest.skipUnless(shutil.which('codex'), 'local official CLI unavailable')
    async def test_real_official_process_handshake_in_isolated_empty_home(self):
        # A real installed official binary, but only initialize/list-loaded.
        # Never authenticate, read user history, start a thread or send a prompt.
        with tempfile.TemporaryDirectory(prefix='omodachi-agent-protocol-') as directory:
            env = {'PATH': os.environ.get('PATH',''), 'HOME': directory, 'CODEX_HOME': directory,
                   'XDG_CONFIG_HOME': directory, 'TMPDIR': directory, 'RUST_LOG':'off'}
            rpc = await CodexJSONRPC.spawn((shutil.which('codex'), 'app-server', '--stdio'), env=env, timeout=10)
            try:
                result = await rpc.initialize()
                self.assertIsInstance(result, dict)
                loaded = await rpc.request('thread/loaded/list', {'limit':10})
                self.assertEqual(loaded['data'], [])
            finally:
                await rpc.close()

    @unittest.skipUnless(shutil.which('codex'), 'local official CLI unavailable')
    async def test_real_official_unix_websocket_handshake(self):
        with tempfile.TemporaryDirectory(prefix='oma-unix-', dir='/tmp') as directory:
            socket = Path(directory)/'s.sock'
            env = {'PATH':os.environ.get('PATH',''), 'HOME':directory,
                   'CODEX_HOME':directory, 'TMPDIR':directory}
            process = await asyncio.create_subprocess_exec(shutil.which('codex'), 'app-server',
                '--listen', 'unix://'+str(socket), env=env,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            rpc = None
            try:
                for _ in range(100):
                    if socket.exists(): break
                    await asyncio.sleep(.02)
                rpc = await CodexUnixRPC.connect(socket, timeout=5)
                self.assertIsInstance(await rpc.initialize(),dict)
                loaded = await rpc.request('thread/loaded/list',{'limit':10})
                self.assertEqual(loaded['data'],[])
            finally:
                if rpc: await rpc.close()
                if process.returncode is None: process.terminate()
                await process.wait()


class ApprovalTests(unittest.IsolatedAsyncioTestCase):
    def binding(self):
        return DefaultAgentBinding('host-owned', 'thread-owned', 'ws://127.0.0.1:47311', 'a' * 64)

    async def attached(self, rpc):
        chat = CodexDefaultAgentChat(self.binding(), rpc)
        await chat.attach()
        while not chat.events.empty(): chat.events.get_nowait()
        return chat

    async def drain(self, chat):
        rows = []
        while not chat.events.empty():
            rows.append(chat.events.get_nowait()['event'])
        return rows

    async def test_command_approval_becomes_an_event_and_a_decision(self):
        rpc = ControlledRPC(); chat = await self.attached(rpc)
        rpc.server_request('item/commandExecution/requestApproval', 11, threadId='thread-owned',
                           itemId='item-1', turnId='turn-one', command='ls -la', cwd='/work', kind='command')
        [event] = await self.drain(chat)
        self.assertEqual(event['type'], 'agent.approval.requested')
        self.assertEqual((event['request_id'], event['kind'], event['summary']), ('11', 'commandExecution', 'ls -la'))
        self.assertEqual(event['details']['cwd'], '/work')
        self.assertEqual(event['decisions'], ['accept', 'acceptForSession', 'decline', 'cancel'])
        self.assertEqual(chat.snapshot_value()['pendingApprovals'][0]['request_id'], '11')
        with self.assertRaisesRegex(AgentChatError, 'agent_approval_decision_invalid'):
            await chat.resolve_approval('11', decision='sure')
        result = await chat.resolve_approval('11', decision='acceptForSession')
        self.assertEqual(rpc.answers, [(11, {'decision': 'acceptForSession'})])
        self.assertTrue(result['resolved'])
        self.assertEqual((await self.drain(chat))[0]['source'], 'client')
        self.assertEqual(chat.snapshot_value()['pendingApprovals'], [])
        with self.assertRaisesRegex(AgentChatError, 'agent_approval_unknown'):
            await chat.resolve_approval('11', decision='accept')

    async def test_permissions_echo_the_requested_profile_and_input_answers(self):
        rpc = ControlledRPC(); chat = await self.attached(rpc)
        profile = {'fileSystem': {'write': ['/work']}, 'network': {'enabled': True}}
        rpc.server_request('item/permissions/requestApproval', 12, threadId='thread-owned', itemId='i',
                           turnId='t', cwd='/work', reason='needs the network', permissions=profile)
        await chat.resolve_approval('12', decision='accept')
        self.assertEqual(rpc.answers[-1][1], {'permissions': profile, 'scope': 'turn'})
        rpc.server_request('item/permissions/requestApproval', 13, threadId='thread-owned', itemId='i',
                           turnId='t', cwd='/work', permissions=profile)
        await chat.resolve_approval('13', decision='decline')
        self.assertEqual(rpc.answers[-1][1], {'permissions': {}, 'scope': 'turn'})
        rpc.server_request('item/tool/requestUserInput', 14, threadId='thread-owned', itemId='i', turnId='t',
                           isBlocking=True, questions=[{'id': 'q1', 'header': 'Which branch?', 'question': 'pick'}])
        event = (await self.drain(chat))[-1]
        self.assertEqual((event['kind'], event['summary']), ('userInput', 'Which branch?'))
        with self.assertRaisesRegex(AgentChatError, 'agent_approval_input_required'):
            await chat.resolve_approval('14', decision='accept')
        await chat.resolve_approval('14', answers={'q1': ['main']})
        self.assertEqual(rpc.answers[-1][1], {'answers': {'q1': {'answers': ['main']}}})

    async def test_stale_prompt_and_foreign_thread_and_unknown_request(self):
        rpc = ControlledRPC(); chat = await self.attached(rpc)
        rpc.server_request('item/fileChange/requestApproval', 15, threadId='thread-owned', itemId='i',
                           turnId='t', reason='write two files')
        await self.drain(chat)
        rpc.notify('serverRequest/resolved', threadId='thread-owned', requestId=15)
        [event] = await self.drain(chat)
        self.assertEqual((event['type'], event['source']), ('agent.approval.resolved', 'elsewhere'))
        self.assertEqual(chat.pending_approvals, {})
        rpc.server_request('item/commandExecution/requestApproval', 16, threadId='another-thread', command='rm')
        await asyncio.sleep(0)
        self.assertEqual(rpc.errors[-1][1], -32601)
        self.assertEqual(chat.pending_approvals, {})

    async def test_status_usage_models_and_steering(self):
        rpc = ControlledRPC(); chat = await self.attached(rpc)
        rpc.notify('thread/status/changed', threadId='thread-owned',
                   status={'type': 'active', 'activeFlags': ['waitingOnApproval']})
        self.assertEqual(chat.status_value()['waiting'], 'approval')
        rpc.notify('thread/tokenUsage/updated', threadId='thread-owned', turnId='t',
                   tokenUsage={'last': {'inputTokens': 10, 'cachedInputTokens': 1, 'outputTokens': 2,
                                        'reasoningOutputTokens': 1, 'totalTokens': 13},
                               'total': {'inputTokens': 10, 'cachedInputTokens': 1, 'outputTokens': 2,
                                         'reasoningOutputTokens': 1, 'totalTokens': 13},
                               'modelContextWindow': 400000})
        rpc.notify('account/rateLimits/updated',
                   rateLimits={'planType': 'pro', 'primary': {'usedPercent': 12, 'windowDurationMins': 300}})
        rpc.notify('account/rateLimits/updated', rateLimits={'secondary': {'usedPercent': 4}})
        rpc.notify('thread/settings/updated', threadId='thread-owned', settings={'model': 'gpt-6-astra', 'effort': 'high'})
        usage = chat.usage_value()
        self.assertEqual(usage['tokens']['total']['totalTokens'], 13)
        self.assertEqual(usage['tokens']['modelContextWindow'], 400000)
        self.assertEqual(usage['rate_limits']['planType'], 'pro')
        # A sparse update merges; it never clears what it omits.
        self.assertEqual(usage['rate_limits']['primary']['usedPercent'], 12)
        self.assertEqual(usage['rate_limits']['secondary']['usedPercent'], 4)
        self.assertEqual((usage['model'], usage['effort']), ('gpt-6-astra', 'high'))
        models = await chat.models()
        self.assertEqual(models['default'], 'gpt-6-astra')
        self.assertEqual(models['models'][0]['efforts'], ['low', 'high'])
        self.assertEqual(len(models['models']), 1)  # the second page repeats the same id
        with self.assertRaisesRegex(AgentChatError, 'agent_not_working'):
            await chat.steer('one more thing', 'req-steer')
        await chat.send('hello', 'req-1', model='gpt-6-astra', effort='high')
        self.assertEqual(rpc.calls[-1][1]['model'], 'gpt-6-astra')
        self.assertEqual(rpc.calls[-1][1]['effort'], 'high')
        self.assertEqual((await chat.steer('one more thing', 'req-steer'))['delivery'], 'steered')
        self.assertEqual(rpc.calls[-1][0], 'turn/steer')
        self.assertEqual(rpc.calls[-1][1]['expectedTurnId'], 'turn-one')

    async def test_a_thread_with_no_rollout_yet_attaches_instead_of_failing(self):
        class FreshRPC(ControlledRPC):
            async def request_fenced(self, method, params):
                if method == 'thread/resume' or (method == 'thread/read' and params.get('includeTurns')):
                    self.calls.append((method, params))
                    self.ordinal += 1
                    # 0.154 answers a just-created thread this way.
                    raise AgentChatError('provider_thread_rollout_missing', rpc_code=-32601)
                return await super().request_fenced(method, params)
        rpc = FreshRPC()
        chat = CodexDefaultAgentChat(self.binding(), rpc)
        state = await chat.attach()
        self.assertEqual(state['identity']['providerSessionID'], 'thread-owned')
        self.assertEqual([m for m, _ in rpc.calls][-1], 'thread/read')
        self.assertFalse(any(m == 'thread/start' for m, _ in rpc.calls))

    async def test_credential_refresh_is_answered_from_the_codex_store(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'auth.json').write_text(json.dumps(
                {'tokens': {'access_token': 'at-1', 'account_id': 'acct-1'}, 'chatgpt_plan_type': 'pro'}))
            self.assertEqual(read_codex_chatgpt_tokens(home),
                             {'accessToken': 'at-1', 'chatgptAccountId': 'acct-1', 'chatgptPlanType': 'pro'})
            rpc = ControlledRPC()
            chat = CodexDefaultAgentChat(self.binding(), rpc, auth_provider=lambda: read_codex_chatgpt_tokens(home))
            await chat.attach()
            rpc.server_request('account/chatgptAuthTokens/refresh', 21, reason='expired')
            for _ in range(50):
                if rpc.answers: break
                await asyncio.sleep(0.01)
            self.assertEqual(rpc.answers, [(21, {'accessToken': 'at-1', 'chatgptAccountId': 'acct-1',
                                                 'chatgptPlanType': 'pro'})])
            events = [row for row in await self.drain(chat) if row['type'] == 'agent.auth.refreshed']
            self.assertEqual(events[-1]['refreshed'], True)
            # An API-key install has no ChatGPT tokens; declining is still an answer.
            (home / 'auth.json').write_text(json.dumps({'OPENAI_API_KEY': 'sk-x', 'auth_mode': 'apikey'}))
            with self.assertRaisesRegex(AgentChatError, 'agent_auth_tokens_unavailable'):
                read_codex_chatgpt_tokens(home)
            rpc.server_request('account/chatgptAuthTokens/refresh', 22, reason='expired')
            for _ in range(50):
                if rpc.errors: break
                await asyncio.sleep(0.01)
            self.assertEqual(rpc.errors[-1][:2], (22, -32001))
            await chat.close()


if __name__ == '__main__': unittest.main()
