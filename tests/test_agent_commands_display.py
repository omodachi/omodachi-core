"""AGENT-NEW-1 (Leo: /model showed raw JSON): no slash command answers with a
provider structure.

Every command in the catalog is executed against a codex that answers with the
real 0.160 shapes (`model/list`, `skills/list`, `mcpServerStatus/list`,
`account/rateLimits/read`, `thread/read`, ...). Each one must either answer
with a plain sentence and a `display` the clients can word themselves, open a
native thing the clients know, or be unavailable with a reason they can say.
Nothing that a phone would print may look like JSON or a developer string.
"""
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from omodachi_core.agent_commands import AgentCommands, REASONS
from omodachi_core.service import ServiceError

# Things only a serializer writes: braces, brackets, `"key":`, a Python repr
# of a dict, or the old developer reason. (Names the person chose - an MCP
# server called `node_repl` - are theirs and pass.)
JSONISH = re.compile(r"[{}\[\]]|\"\s*:|'\s*:|TUI|原生|\bNone\b|\bnull\b")
DISPLAY_KEYS = {'pwd', 'status', 'model.choose', 'model.selected', 'skills', 'mcp', 'usage', 'rename', 'compact',
                'new', 'copy', 'export', 'plan.on', 'plan.off', 'plan.sent'}
NATIVE_ACTIONS = {'choose_model', 'new_conversation', 'copy_last_response', 'export_conversation'}

PROVIDER = {
    'thread/read': {'thread': {'id': 'existing', 'cwd': '/home/u/.local/share/omodachi/agent-workspace',
                               'name': None, 'status': {'type': 'active', 'activeFlags': []}}},
    'model/list': {'data': [{'id': 'gpt-6-sol', 'model': 'gpt-6-sol', 'displayName': 'GPT-6 Sol',
                             'supportedReasoningEfforts': [{'reasoningEffort': 'high'}, {'reasoningEffort': 'xhigh'}]}],
                   'nextCursor': None},
    'skills/list': {'data': [{'cwd': '/w', 'errors': [], 'skills': [
        {'name': 'imagegen', 'description': 'x', 'path': '/p', 'scope': 'system', 'enabled': True, 'pluginId': None},
        {'name': 'pdf', 'description': 'y', 'path': '/q', 'scope': 'user', 'enabled': True, 'pluginId': None}]}]},
    'mcpServerStatus/list': {'data': [{'name': 'node_repl', 'runtimeStatus': None, 'pluginId': None, 'httpOrigin': None,
                                       'serverInfo': None, 'serverCapabilities': {'tools': {}},
                                       'tools': {'js': {'name': 'js'}}, 'toolsError': None, 'resources': [],
                                       'resourceTemplates': [], 'authStatus': 'unsupported'}], 'nextCursor': None},
    'account/rateLimits/read': {'ordinaryUsageAllowed': True, 'rateLimits': {
        'limitId': 'codex', 'limitName': None, 'normalModelSlug': None,
        'primary': {'usedPercent': 12.4, 'windowDurationMins': 300, 'resetsAt': 1789700000},
        'secondary': {'usedPercent': 41, 'windowDurationMins': 10080, 'resetsAt': 1790200000},
        'credits': None, 'individualLimit': None, 'spendControlReached': None, 'planType': 'pro',
        'rateLimitReachedType': None}, 'rateLimitsByLimitId': None, 'rateLimitResetCredits': None,
        'accountId': None, 'rateLimitUpsell': None},
    'thread/name/set': {}, 'thread/compact/start': {},
}
ARGUMENTS = {'rename': 'Release notes', 'mcp': 'verbose'}


def text_of(value):
    """Every string a client could draw from this value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from text_of(item)
    elif isinstance(value, list):
        for item in value:
            yield from text_of(item)


class EveryCommandSpeaksPlainly(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        async def rpc(method, params):
            return json.loads(json.dumps(PROVIDER[method]))
        async def snapshot():
            return {'rows': [{'kind': 'message', 'role': 'user', 'text': 'hi', 'id': 'a', 'turnID': 't'},
                             {'kind': 'message', 'role': 'assistant', 'text': 'Hello there.', 'id': 'b', 'turnID': 't'}]}
        async def set_mode(kind):
            adapter.mode = kind
            return {'mode': kind, 'changed': True}
        adapter = SimpleNamespace(binding=SimpleNamespace(thread_id='existing', cwd='/w'), active_turn=None,
                                  rpc=SimpleNamespace(request=rpc), settings={'model': 'gpt-6-sol', 'effort': 'high'},
                                  snapshot=snapshot, mode='default', set_mode=set_mode)
        async def require(authorize):
            return adapter
        self.commands = AgentCommands(SimpleNamespace(require=require, manager=SimpleNamespace(cwd=Path('/w'))))

    def assertPlain(self, text, where):
        self.assertIsInstance(text, str, where)
        self.assertTrue(text.strip(), where)
        self.assertIsNone(JSONISH.search(text), (where, text))

    async def test_every_catalog_command(self):
        seen = {'runs': 0, 'native': 0, 'unavailable': 0}
        for number, row in enumerate(self.commands.listing()['commands']):
            name = row['id']
            payload = {'revision': self.commands.revision, 'arguments': ARGUMENTS.get(name, ''),
                       'request_id': 'r' + str(number)}
            # codex's own one-line description (it may say "TUI"): never a structure.
            self.assertIsNone(re.search(r'[{}\[\]]|"\s*:', row['description']), name)
            if not row['available']:
                self.assertIn(row['reason_code'], REASONS, name)
                self.assertPlain(row['reason'], name + ' reason')
                with self.assertRaisesRegex(ServiceError, 'agent_command_unsupported'):
                    await self.commands.execute(name, payload, lambda: None)
                seen['unavailable'] += 1
                continue
            result = await self.commands.execute(name, payload, lambda: None)
            self.assertPlain(result['result']['summary'], name + ' summary')
            self.assertIn(result['display']['key'], DISPLAY_KEYS, name)
            for arg, value in result['display']['args'].items():
                # Scalars or lists of scalars: nothing a client could dump.
                values = value if isinstance(value, list) else [value]
                for item in values:
                    if isinstance(item, dict):
                        self.assertTrue(all(isinstance(v, (int, float, str, type(None))) for v in item.values()),
                                        (name, arg))
                    else:
                        self.assertIsInstance(item, (int, float, str, bool, type(None)), (name, arg))
            if 'native_request' in result:
                self.assertIn(result['native_request']['action'], NATIVE_ACTIONS, name)
                seen['native'] += 1
            else:
                seen['runs'] += 1
        self.assertEqual(seen, {'runs': 8, 'native': 5, 'unavailable': 44})

    async def test_model_with_and_without_arguments(self):
        rev = self.commands.revision
        plain = await self.commands.execute('model', {'revision': rev, 'arguments': '', 'request_id': 'm1'}, lambda: None)
        self.assertEqual(plain['native_request']['action'], 'choose_model')
        chosen = await self.commands.execute('model', {'revision': rev, 'arguments': 'gpt-6-sol xhigh',
                                                       'request_id': 'm2'}, lambda: None)
        self.assertEqual((chosen['native_request']['model'], chosen['native_request']['effort']), ('gpt-6-sol', 'xhigh'))
        self.assertEqual(chosen['result']['summary'], 'The next message uses GPT-6 Sol · xhigh.')

    def test_the_old_raw_answers_are_what_this_rejects(self):
        # What Leo saw: `/model` answered with the provider's own model list.
        self.assertIsNotNone(JSONISH.search(json.dumps({'data': PROVIDER['model/list']['data']}, indent=2)))
        self.assertIsNotNone(JSONISH.search("idle · {'type': 'idle'}"))
        self.assertIsNotNone(JSONISH.search('此命令的官方 TUI 操作尚未接入原生 App'))


if __name__ == '__main__':
    unittest.main()
