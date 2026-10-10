// AGENT-NEW-1: Odomo's "New conversation", the conversation it follows when
// the default one is replaced, tool rows that end, and slash commands that
// answer in sentences - never the provider's structure (Leo: /model printed
// raw JSON).
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import * as A from '../src/lib/agent.js';
import {HostClient} from '../src/lib/api.js';
import {en, zh} from '../src/lib/i18n.js';
import {agentErrorText} from '../src/lib/reasons.js';

const OLD = {hostID: 'h', agentID: 'default', provider: 'codex', providerSessionID: 'thread-old'};
const NEW = {...OLD, providerSessionID: 'thread-new'};
const snap = (identity, extra = {}) => ({identity, rows: [], activeTurn: null, sequence: 9, pendingApprovals: [],
  status: {type: 'idle', activeFlags: [], waiting: null}, usage: {model: 'gpt-6-sol', effort: 'high'}, ...extra});
const FIXTURES = new URL('../../contracts/fixtures/', import.meta.url);
const fixture = (name) => JSON.parse(readFileSync(new URL(name, FIXTURES), 'utf8'));

test('new conversation: only between turns, and with nothing waiting', () => {
  const ready = A.restore(A.initial(), snap(OLD, {rows: [{kind: 'message', id: 'm', turnID: 't', role: 'user', text: 'hi'}]}));
  assert.equal(A.newRefusal(ready), null);
  assert.equal(A.newRefusal(A.initial()), 'newUnavailable');
  assert.equal(A.newRefusal({...ready, phase: 'running', turnID: 't2'}), 'newWhileWorking');
  assert.equal(A.newRefusal({...ready, phase: 'sending'}), 'newWhileWorking');
  const waiting = A.restore(A.initial(), snap(OLD, {pendingApprovals: [{request_id: '7', kind: 'commandExecution',
    summary: 'pwd', details: {}, decisions: ['accept', 'decline']}]}));
  assert.equal(A.newRefusal(waiting), 'newWhileWaiting');
  for (const table of [en, zh]) {
    for (const key of ['newConversation', 'newBody', 'newStart', 'newLater', 'newStarting', 'newWhileWorking',
      'newWhileWaiting', 'newUnavailable']) assert.ok(table.agent[key], key);
  }
  assert.equal(en.agent.newBody, 'This starts a fresh conversation with the agent. The old one stays on the computer.');
});

test('a replaced conversation is followed, not an error; only the draft survives', () => {
  let m = A.restore(A.initial(), snap(OLD, {rows: [{kind: 'message', id: 'm', turnID: 't', role: 'assistant', text: 'old'}]}));
  m = {...m, draft: 'half a thought', selectedModel: 'x', fresh: {state: 'starting'}};
  assert.equal(A.restore(m, snap(NEW)), null, 'restore alone still refuses a foreign identity');
  assert.ok(A.otherConversation(m, snap(NEW)));
  assert.ok(!A.otherConversation(m, snap(OLD)));
  const next = A.restore(A.startOver(m), snap(NEW));
  assert.equal(next.identity.providerSessionID, 'thread-new');
  assert.deepEqual(next.rows, []);
  assert.equal(next.phase, 'ready');
  assert.equal(next.draft, 'half a thought');
  assert.equal(next.selectedModel, null);
  assert.deepEqual(next.fresh, {state: 'none'});
  // events of the old conversation no longer apply
  const stale = A.apply(next, {identity: OLD, sequence: 10, event: {type: 'turnStarted', turnID: 'x'}});
  assert.equal(stale, next);
});

test('the client asks for a new conversation for the one on screen', async () => {
  const calls = [];
  const client = new HostClient({base: 'https://h.example/', token: 't', fetchImpl: async (url, init) => {
    calls.push({path: new URL(url).pathname, method: init.method, body: JSON.parse(init.body)});
    return {ok: true, status: 200, json: async () => fixture('agent-chat-new.json')};
  }});
  const result = await client.chatNew('thread-fixture-01');
  assert.deepEqual(calls, [{path: '/v1/agent/default/chat:new', method: 'POST',
    body: {confirmed: true, expected_provider_session_id: 'thread-fixture-01'}}]);
  // the host's own answer restores straight into an empty conversation
  const next = A.restore(A.startOver(A.restore(A.initial(), snap({...OLD, hostID: 'omarchy-fixture',
    providerSessionID: 'thread-fixture-01'}))), result.snapshot);
  assert.equal(next.identity.providerSessionID, 'thread-fixture-02');
  assert.deepEqual(next.rows, []);
});

test('the refusals and a tool the person stopped are said in both languages', () => {
  for (const table of [en, zh]) {
    for (const code of ['agent_turn_active', 'agent_approval_pending', 'agent_identity_changed',
      'agent_conversation_changing']) {
      assert.ok(A.KNOWN_CODES.has(code), code);
      const text = agentErrorText(table, {code});
      assert.ok(text && !text.includes(code), code);
    }
    assert.ok(table.agent.toolStatus.interrupted);
  }
  const m = A.restore(A.initial(), snap(OLD, {activeTurn: 't', rows: []}));
  const row = A.apply(m, {identity: OLD, sequence: 10, event: {type: 'tool', value: {kind: 'tool', id: 'c', turnID: 't',
    name: 'sleep 8', status: 'interrupted', detail: ''}}}).rows[0];
  assert.equal(row.status, 'interrupted');
});

// AGENT-PLAN-1 (Leo: "甚至是 / 的优化整个都可以砍掉 只透传"): the slash list, the
// answers it printed and the reasons it gave are gone - what is typed is a
// message. agentplan1.test.mjs covers what replaced them.

test('the header has the quiet New conversation control and the icon exists', async () => {
  const icons = readFileSync(new URL('../src/Icon.jsx', import.meta.url), 'utf8');
  // Leo, 2026-10-05: a bubble with a plus, like the App's.
  assert.match(icons, /newChat: '/);
  const panel = readFileSync(new URL('../src/panels/AgentPanel.jsx', import.meta.url), 'utf8');
  assert.match(panel, /data-testid="agent-new"[\s\S]{0,200}<Icon name="newChat"/);
  const {runnerImport} = await import('vite');
  const {module: render} = await runnerImport(new URL('./render.jsx', import.meta.url).pathname,
    {configFile: false, logLevel: 'silent'});
  for (const lang of ['en', 'zh-Hans']) {
    const html = render.renderAgent({hostId: 'h'}, lang);
    assert.match(html, /data-testid="agent-new"/);
    assert.match(html, lang === 'en' ? /aria-label="New conversation"/ : /aria-label="新对话"/);
  }
});

test('the transcript follows new output only while the person is at its end', () => {
  assert.ok(A.atEnd(900, 100, 1000));
  assert.ok(A.atEnd(860, 100, 1000), 'within 48 px counts as the end');
  assert.ok(!A.atEnd(400, 100, 1000), 'scrolled up to read: stays put');
  const source = readFileSync(new URL('../src/panels/AgentPanel.jsx', import.meta.url), 'utf8');
  // opens at the end: pinned from the start, and again for a new conversation
  assert.match(source, /const pinned = useRef\(true\)/);
  assert.match(source, /useEffect\(\(\) => \{ pinned\.current = true; \}, \[sessionKey, picker\]\)/);
  // streaming text (not only new rows) keeps it at the end
  assert.match(source, /\[model\.rows\.length, tail,/);
});

test('a picked model shows at once, even when the thread\'s own is known', () => {
  let m = A.restore(A.initial(), snap(OLD, {usage: {model: 'gpt-6.1-sol', effort: 'medium'}}));
  assert.equal(A.effectiveEffort(m), 'medium');
  m = {...m, selectedModel: 'gpt-6.1-sol', selectedEffort: 'low'};
  assert.equal(A.effectiveEffort(m), 'low');
  m = A.apply(m, {identity: OLD, sequence: 10, event: {type: 'agent.usage.updated', usage: {model: 'gpt-6.1-sol', effort: 'low'}}});
  assert.equal(m.selectedEffort, null);
  assert.equal(A.effectiveEffort(m), 'low');
});
