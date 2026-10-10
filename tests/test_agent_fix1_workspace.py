"""AGENT-FIX-1: the default thread runs in the current workspace directory.

A thread created before the omadochi -> omodachi rename keeps its old cwd in
codex; that directory is gone, so every approved command failed with
"Failed to create unified exec process: No such file or directory" and
view_image could not find existing files. Core now carries its workspace on
thread/resume and on every turn/start, and recreates the directory if missing.
Controlled fakes only; never a user's codex daemon.
"""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from omodachi_core.agent_chat_owner import OwnedCodexAgent
from omodachi_core.agent_chat_provider import (
    AgentChatError, CodexDefaultAgentChat, DefaultAgentBinding, resume_in_workspace,
)

STALE = '/home/someone/.local/share/omadochi/agent-workspace'


class ThreadRPC:
    """A codex that remembers each thread's cwd the way 0.154 does."""
    def __init__(self, *, reject_resume_cwd=False):
        self.calls = []
        self.cwd = {'thread-owned': STALE}
        self.reject_resume_cwd = reject_resume_cwd
        self.ordinal = 0
        self.on_notification = lambda *_: None
        self.on_server_request = lambda value: None
        self.on_disconnect = lambda: None

    async def initialize(self): return {}
    async def close(self): pass

    async def request(self, method, params):
        result, _ = await self.request_fenced(method, params)
        return result

    async def request_fenced(self, method, params):
        self.calls.append((method, dict(params)))
        self.ordinal += 1
        thread = params.get('threadId')
        if method == 'thread/loaded/list':
            return {'data': list(self.cwd)}, self.ordinal
        if method == 'thread/resume':
            if 'cwd' in params:
                if self.reject_resume_cwd:
                    raise AgentChatError('provider_request_rejected', rpc_code=-32600)
                self.cwd[thread] = params['cwd']
            return {'thread': {'id': thread, 'cwd': self.cwd[thread], 'turns': []},
                    'cwd': self.cwd[thread]}, self.ordinal
        if method == 'thread/start':
            self.cwd['created'] = params['cwd']
            return {'thread': {'id': 'created', 'cwd': params['cwd']}}, self.ordinal
        if method == 'turn/start':
            if 'cwd' in params:
                self.cwd[thread] = params['cwd']
            # What codex actually does with a command in a missing cwd.
            ran = Path(self.cwd[thread]).is_dir()
            return {'turn': {'id': 'turn-' + str(self.ordinal), 'status': 'inProgress', 'ran': ran}}, self.ordinal
        if method == 'thread/queue/list':  # AGENT-QUEUE-1: a codex without the queue
            raise AgentChatError('provider_request_rejected')
        raise AssertionError(method)


class WorkspaceCorrectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.root = self.home / '.config/omodachi/structured-default'
        self.workspace = self.home / '.local/share/omodachi/agent-workspace'
        self.rpc = ThreadRPC()

        async def proxy(): return self.rpc
        self.owner = OwnedCodexAgent(self.root, self.workspace, 'testhost', rpc_factory=proxy)
        self.owner._write_endpoint(47311)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def stale_record(self):
        # Exactly the shape on Leo's host: owner.json was already migrated
        # (cwd says omodachi) but the thread inside codex still says omadochi.
        self.root.mkdir(parents=True, exist_ok=True)
        record = self.owner._record('thread-owned', creation_pending=False)
        record['migrated_from'] = {'cwd': STALE, 'socket': None, 'daemon_unit': None}
        self.owner._save(record)

    async def test_resume_moves_the_existing_thread_into_the_workspace(self):
        self.stale_record()
        binding = await self.owner.ensure_new_or_owned()
        self.assertEqual(binding.thread_id, 'thread-owned')  # same thread, history kept
        self.assertEqual(binding.cwd, str(self.workspace))
        self.assertTrue(self.workspace.is_dir())  # created when missing
        self.assertEqual(self.rpc.calls[0], ('thread/resume', {'threadId': 'thread-owned', 'cwd': str(self.workspace)}))
        self.assertEqual(self.rpc.cwd['thread-owned'], str(self.workspace))
        self.assertFalse(any(m == 'thread/start' for m, _ in self.rpc.calls))

    async def test_old_metadata_spelling_is_migrated_and_thread_corrected(self):
        self.root.mkdir(parents=True, exist_ok=True)
        old = self.owner._record('thread-owned', creation_pending=False)
        old['cwd'] = STALE
        (self.root / 'owner.json').write_text(json.dumps(old))
        binding = await self.owner.ensure_new_or_owned()
        saved = json.loads((self.root / 'owner.json').read_text())
        self.assertEqual(saved['cwd'], str(self.workspace))
        self.assertEqual(saved['migrated_from']['cwd'], STALE)
        self.assertEqual(saved['thread_id'], 'thread-owned')
        self.assertEqual(binding.cwd, str(self.workspace))
        self.assertEqual(self.rpc.cwd['thread-owned'], str(self.workspace))

    async def test_every_turn_carries_the_workspace_and_a_command_can_start(self):
        self.stale_record()
        binding = await self.owner.ensure_new_or_owned()
        chat = CodexDefaultAgentChat(binding, self.rpc)
        await chat.attach()
        resumes = [p for m, p in self.rpc.calls if m == 'thread/resume']
        self.assertTrue(resumes and all(p.get('cwd') == str(self.workspace) for p in resumes))
        shutil.rmtree(self.workspace)  # the user removed it while core ran
        await chat.send('touch fix1-probe', 'request-one')
        turn = [p for m, p in self.rpc.calls if m == 'turn/start'][-1]
        self.assertEqual(turn['cwd'], str(self.workspace))
        self.assertTrue(self.workspace.is_dir())
        self.assertTrue(Path(self.rpc.cwd['thread-owned']).is_dir())

    async def test_a_refused_resume_override_still_resumes_and_turns_fix_it(self):
        self.rpc.reject_resume_cwd = True
        self.stale_record()
        binding = await self.owner.ensure_new_or_owned()
        self.assertEqual([m for m, _ in self.rpc.calls], ['thread/resume', 'thread/resume'])
        self.assertNotIn('cwd', self.rpc.calls[1][1])
        self.assertEqual(self.rpc.cwd['thread-owned'], STALE)
        chat = CodexDefaultAgentChat(binding, self.rpc)
        await chat.attach()
        await chat.send('touch fix1-probe', 'request-one')
        self.assertEqual(self.rpc.cwd['thread-owned'], str(self.workspace))

    async def test_other_resume_errors_are_not_swallowed(self):
        class Missing(ThreadRPC):
            async def request_fenced(self, method, params):
                self.calls.append((method, dict(params)))
                raise AgentChatError('provider_disconnected')
        rpc = Missing()
        with self.assertRaisesRegex(AgentChatError, 'provider_disconnected'):
            await resume_in_workspace(rpc, 'thread-owned', str(self.workspace))
        self.assertEqual(len(rpc.calls), 1)

    async def test_new_default_thread_starts_in_the_workspace(self):
        binding = await self.owner.ensure_new_or_owned()
        self.assertEqual(self.rpc.calls, [('thread/start', {'cwd': str(self.workspace)})])
        self.assertEqual(binding.cwd, str(self.workspace))

    async def test_binding_without_cwd_keeps_the_old_wire_shape(self):
        binding = DefaultAgentBinding('host', 'thread-owned', 'ws://127.0.0.1:47311', 'a' * 64)
        chat = CodexDefaultAgentChat(binding, self.rpc)
        await chat.attach()
        await chat.send('hello', 'request-one')
        self.assertNotIn('cwd', [p for m, p in self.rpc.calls if m == 'thread/resume'][0])
        self.assertNotIn('cwd', [p for m, p in self.rpc.calls if m == 'turn/start'][0])

    def test_binding_rejects_a_relative_cwd(self):
        with self.assertRaisesRegex(AgentChatError, 'structured_agent_binding_required'):
            DefaultAgentBinding('host', 'thread-owned', 'ws://127.0.0.1:47311', 'a' * 64, cwd='agent-workspace')


if __name__ == '__main__':
    unittest.main()
