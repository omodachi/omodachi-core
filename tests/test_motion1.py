"""MOTION-1: `agent.chat.activity`, the one bit the animated face needs that
`agent.changed` did not carry - what the turn is doing right now."""
import asyncio
import json
from pathlib import Path
import unittest

from omodachi_core.agent_chat_provider import CodexDefaultAgentChat, DefaultAgentBinding
from omodachi_core.agent_chat_service import DefaultAgentChatService
from omodachi_core.hub import Hub
from tests.test_agent_chat_provider import ControlledRPC

ROOT = Path(__file__).resolve().parents[1]


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    async def attached(self):
        rpc = ControlledRPC()
        chat = CodexDefaultAgentChat(DefaultAgentBinding('host-owned', 'thread-owned', 'ws://127.0.0.1:47311', 'a' * 64), rpc)
        await chat.attach()
        while not chat.events.empty():
            chat.events.get_nowait()
        return rpc, chat

    def phase(self, chat):
        return chat.activity_value()['phase']

    async def test_a_turn_walks_thinking_tool_working_done(self):
        rpc, chat = await self.attached()
        self.assertEqual(chat.activity_value(), {'phase': 'idle', 'turn_id': None})
        rpc.notify('turn/started', threadId='thread-owned', turn={'id': 'turn-1'})
        self.assertEqual(chat.activity_value(), {'phase': 'thinking', 'turn_id': 'turn-1'})
        rpc.notify('item/started', threadId='thread-owned', turnId='turn-1', item={'type': 'reasoning', 'id': 'r1'})
        self.assertEqual(self.phase(chat), 'thinking')
        rpc.notify('item/started', threadId='thread-owned', turnId='turn-1',
                   item={'type': 'commandExecution', 'id': 'c1', 'command': 'ls', 'status': 'inProgress'})
        self.assertEqual(self.phase(chat), 'tool')
        # A message that starts while the command still runs does not hide it.
        rpc.notify('item/started', threadId='thread-owned', turnId='turn-1',
                   item={'type': 'agentMessage', 'id': 'm0', 'text': ''})
        self.assertEqual(self.phase(chat), 'tool')
        rpc.notify('item/completed', threadId='thread-owned', turnId='turn-1',
                   item={'type': 'commandExecution', 'id': 'c1', 'command': 'ls', 'status': 'completed'})
        self.assertEqual(self.phase(chat), 'thinking')
        rpc.notify('item/started', threadId='thread-owned', turnId='turn-1',
                   item={'type': 'agentMessage', 'id': 'm1', 'text': ''})
        self.assertEqual(self.phase(chat), 'working')
        rpc.notify('item/agentMessage/delta', threadId='thread-owned', turnId='turn-1', itemId='m1', delta='hi')
        self.assertEqual(self.phase(chat), 'working')
        rpc.notify('turn/completed', threadId='thread-owned', turn={'id': 'turn-1', 'status': 'completed'})
        self.assertEqual(chat.activity_value(), {'phase': 'done', 'turn_id': 'turn-1'})

    async def test_file_changes_and_tool_calls_are_tools_too(self):
        rpc, chat = await self.attached()
        rpc.notify('turn/started', threadId='thread-owned', turn={'id': 'turn-2'})
        rpc.notify('item/started', threadId='thread-owned', turnId='turn-2',
                   item={'type': 'fileChange', 'id': 'f1', 'changes': [], 'status': 'inProgress'})
        rpc.notify('item/started', threadId='thread-owned', turnId='turn-2',
                   item={'type': 'mcpToolCall', 'id': 't1', 'tool': 'x', 'status': 'inProgress'})
        self.assertEqual(self.phase(chat), 'tool')
        rpc.notify('item/completed', threadId='thread-owned', turnId='turn-2',
                   item={'type': 'fileChange', 'id': 'f1', 'changes': [], 'status': 'completed'})
        self.assertEqual(self.phase(chat), 'tool', 'one of two tools is still running')
        rpc.notify('item/completed', threadId='thread-owned', turnId='turn-2',
                   item={'type': 'mcpToolCall', 'id': 't1', 'tool': 'x', 'status': 'completed'})
        self.assertEqual(self.phase(chat), 'thinking')

    async def test_a_failed_turn_is_an_error_and_an_interrupted_one_is_stopped(self):
        # INTEGRATE-2 (Leo): an interrupt is the user's own doing, not an error.
        rpc, chat = await self.attached()
        for status, phase in (('failed', 'error'), ('interrupted', 'stopped'), ('failed', 'error')):
            rpc.notify('turn/started', threadId='thread-owned', turn={'id': 'turn-' + status})
            rpc.notify('turn/completed', threadId='thread-owned', turn={'id': 'turn-' + status, 'status': status})
            self.assertEqual(chat.activity_value(), {'phase': phase, 'turn_id': 'turn-' + status})
        rpc.notify('turn/started', threadId='thread-owned', turn={'id': 'turn-3'})
        self.assertEqual(chat.activity_value(), {'phase': 'thinking', 'turn_id': 'turn-3'})

    async def test_other_threads_and_lost_connections(self):
        rpc, chat = await self.attached()
        rpc.notify('turn/started', threadId='another-thread', turn={'id': 'foreign'})
        self.assertEqual(self.phase(chat), 'idle')
        chat._disconnected()
        self.assertEqual(chat.activity_value(), {'phase': 'offline', 'turn_id': None})


class FakeChat:
    """Just enough of a provider chat for `_events` / `_publish_agent_state`."""
    def __init__(self):
        self.events = asyncio.Queue()
        self.pending_approvals = {}
        self.attached = True
        self.activity = {'phase': 'idle', 'turn_id': None}

    def status_value(self):
        return {'type': 'active', 'activeFlags': [], 'waiting': None}

    def usage_value(self):
        return {'tokens': None, 'rate_limits': None, 'model': None, 'effort': None}

    def activity_value(self):
        return dict(self.activity)


class PublishTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_changed_carries_the_phase_and_only_when_it_moves(self):
        hub = Hub()
        service = DefaultAgentChatService.__new__(DefaultAgentChatService)
        service.hub, service.chat, service.subscribers = hub, FakeChat(), set()
        service.sequence_path = Path('/dev/null')
        published = []
        original = service._publish_agent_state

        def counting():
            published.append(service.chat.activity_value()['phase'])
            original()
        service._publish_agent_state = counting
        service._publish_agent_state()
        self.assertEqual(hub.state_snapshot()['agent']['chat']['activity'], {'phase': 'idle', 'turn_id': None})
        task = asyncio.create_task(service._events())
        identity = {'agentID': 'default'}

        async def event(sequence, kind, phase=None):
            if phase:
                service.chat.activity = {'phase': phase, 'turn_id': 'turn-1'}
            await service.chat.events.put({'identity': identity, 'sequence': sequence, 'event': {'type': kind}})
            await asyncio.sleep(0.01)
        import omodachi_core.agent_chat_service as module
        saved = module.atomic_private_json
        module.atomic_private_json = lambda *_: None
        try:
            await event(1, 'turnStarted', 'thinking')
            await event(2, 'tool', 'tool')
            await event(3, 'tool')            # output delta: same phase, nothing published
            await event(4, 'message', 'working')
            await event(5, 'message')
            await event(6, 'turnFinished', 'done')
        finally:
            task.cancel()
            module.atomic_private_json = saved
        self.assertEqual(published, ['idle', 'thinking', 'tool', 'working', 'done'])
        self.assertEqual(hub.state_snapshot()['agent']['chat']['activity'], {'phase': 'done', 'turn_id': 'turn-1'})

    def test_the_contract_names_every_phase_and_the_fixture_validates(self):
        schema = json.loads((ROOT / 'contracts/state.schema.json').read_text())
        activity = schema['properties']['agent']['properties']['chat']['properties']['activity']
        self.assertEqual(activity['properties']['phase']['enum'],
                         ['idle', 'thinking', 'working', 'tool', 'done', 'error', 'stopped', 'offline'])
        fixture = json.loads((ROOT / 'contracts/fixtures/state-agent-activity.json').read_text())
        self.assertEqual(fixture['agent']['chat']['activity']['phase'], 'tool')


class TokenPinTests(unittest.TestCase):
    def test_the_odomo_copy_is_the_pinned_brand_file(self):
        import subprocess, sys
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/check_motion_tokens.py')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
