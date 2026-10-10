// AGENT-FIX-2 (Leo: "agent 在工具调用的时候，聊天记录会消失", "通过的
// permission 会在底部堆积"): Odomo's side. A command's output arrives as
// increments and is kept to its newest part; an answered approval is one line
// under its own command, never a pile at the bottom; a broken stream
// reconnects by itself.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import * as A from '../src/lib/agent.js';
import {recoveryDelay} from '../src/lib/useAgent.js';
import {en, zh} from '../src/lib/i18n.js';

const ID = {hostID: 'h', agentID: 'default', provider: 'codex', providerSessionID: 't'};
const FIXTURES = new URL('../../contracts/fixtures/', import.meta.url);
const fixture = (name) => JSON.parse(readFileSync(new URL(name, FIXTURES), 'utf8'));
const snap = (extra = {}) => ({identity: ID, rows: [], activeTurn: null, sequence: 0, pendingApprovals: [],
  status: {type: 'idle', activeFlags: [], waiting: null}, usage: null, ...extra});

function stream(model, events) {
  let m = model;
  for (const event of events) m = A.apply(m, {identity: ID, sequence: m.lastSequence + 1, event});
  return m;
}
const tool = (id, status = 'running') => ({type: 'tool', value: {kind: 'tool', id, turnID: 't1', name: 'make ' + id, status, detail: ''}});
const ask = (id, item) => ({type: 'agent.approval.requested', request_id: id, kind: 'commandExecution', summary: 'make',
  details: item ? {item_id: item, command: 'make'} : {command: 'make'}, decisions: ['accept', 'decline']});
const answer = (id, decision = 'accept') => ({type: 'agent.approval.resolved', request_id: id, decision, source: 'client'});

test('output arrives as increments and keeps its newest part, as core keeps it', () => {
  let m = stream(A.restore(A.initial(), snap({activeTurn: 't1'})), [tool('c1'),
    {type: 'tool.output', value: {id: 'c1', turnID: 't1', delta: 'one\n'}},
    {type: 'tool.output', value: {id: 'c1', turnID: 't1', delta: 'two\n'}}]);
  assert.equal(m.rows[0].detail, 'one\ntwo\n');
  m = stream(m, [{type: 'tool.output', value: {id: 'c1', turnID: 't1', delta: 'é'.repeat(A.TOOL_DETAIL_LIMIT), detail_truncated: 8}}]);
  assert.equal(Array.from(m.rows[0].detail).length, A.TOOL_DETAIL_LIMIT);
  assert.equal(m.rows[0].detailTruncated, 8);
  // output of a row this page does not hold: nothing changes, no gap
  const before = m;
  m = stream(m, [{type: 'tool.output', value: {id: 'nope', turnID: 't1', delta: 'x'}}]);
  assert.deepEqual(m.rows, before.rows);
  assert.equal(m.phase, 'running');
  assert.equal(m.lastSequence, before.lastSequence + 1);
});

test("core's fixtures: an output increment and a clipped row", () => {
  const event = fixture('agent-chat-event-tool-output.json');
  assert.equal(event.event.type, 'tool.output');
  let m = A.restore(A.initial(), snap({activeTurn: 'turn-fixture-01', sequence: event.sequence - 2,
    identity: event.identity}));
  m = A.apply(m, {identity: event.identity, sequence: event.sequence - 1, event: {type: 'tool', value:
    {kind: 'tool', id: 'item-fixture-build', turnID: 'turn-fixture-01', name: 'make', status: 'running', detail: ''}}});
  m = A.apply(m, event);
  assert.equal(m.rows[0].detail, 'cc -c main.c\ncc -o app main.o\n');
  const clipped = A.restore(A.initial(), fixture('agent-chat-snapshot-tool-output.json'));
  assert.ok(clipped.rows[0].detailTruncated > 0);
});

test('three approvals in a row: each answered one stays under its own command', () => {
  let m = A.restore(A.initial(), snap());
  m = stream(m, [{type: 'turnStarted', turnID: 't1'},
    {type: 'message', value: {kind: 'message', id: 'u1', turnID: 't1', role: 'user', text: 'build it'}}]);
  for (const index of [0, 1, 2]) {
    m = stream(m, [tool('c' + index), ask(String(index), 'c' + index)]);
    assert.deepEqual(A.waitingApprovals(m).map((a) => a.requestID), [String(index)], 'the waiting card is at the end');
    m = stream(m, [answer(String(index)), tool('c' + index, 'succeeded')]);
  }
  m = stream(m, [{type: 'message', value: {kind: 'message', id: 'a1', turnID: 't1', role: 'assistant', text: 'done'}}]);
  assert.deepEqual(A.waitingApprovals(m), []);
  assert.deepEqual(A.timeline(m).map((e) => e.id), ['message:u1', 'tool:c0', 'approval:0', 'tool:c1', 'approval:1',
    'tool:c2', 'approval:2', 'message:a1']);
});

test('a prompt with no command sits after the row that was last, through a re-read', () => {
  let m = A.restore(A.initial(), snap({activeTurn: 't1', rows: [{kind: 'message', id: 'u1', turnID: 't1', role: 'user', text: 'hi'}]}));
  m = stream(m, [ask('5'), {type: 'message', value: {kind: 'message', id: 'a1', turnID: 't1', role: 'assistant', text: 'later'}}]);
  m = A.restore(m, snap({activeTurn: 't1', sequence: m.lastSequence,
    rows: [{kind: 'message', id: 'u1', turnID: 't1', role: 'user', text: 'hi'},
      {kind: 'message', id: 'a1', turnID: 't1', role: 'assistant', text: 'later'}],
    pendingApprovals: [ask('5')]}));
  m = stream(m, [answer('5')]);
  assert.deepEqual(A.timeline(m).map((e) => e.id), ['message:u1', 'approval:5', 'message:a1']);
});

test('an id the provider reuses after a reconnect is a new prompt; the old line stays', () => {
  let m = A.restore(A.initial(), snap({activeTurn: 't1'}));
  m = stream(m, [tool('c0'), ask('0', 'c0'), answer('0'), tool('c1'), ask('0', 'c1')]);
  assert.equal(A.waitingApprovals(m).length, 1);
  m = stream(m, [answer('0', 'decline')]);
  const entries = A.timeline(m);
  assert.deepEqual(entries.map((e) => e.kind), ['row', 'answered', 'row', 'answered']);
  assert.equal(entries[1].approval.state.decision, 'accept');
  assert.equal(entries[3].approval.state.decision, 'decline');
});

test('a broken stream reconnects by itself, with a pause that grows and stops growing', () => {
  assert.deepEqual([0, 1, 2, 3, 4, 9].map(recoveryDelay), [500, 1000, 2000, 4000, 8000, 8000]);
  const hook = readFileSync(new URL('../src/lib/useAgent.js', import.meta.url), 'utf8');
  for (const path of ["message.type === 'resync_required'", "ref.current.phase === 'reconnecting'", 'onClose: () =>']) {
    const at = hook.indexOf(path);
    assert.ok(at > 0, path);
    assert.ok(hook.slice(at, at + 400).includes('recover(generation)'), path + ' reconnects by itself');
  }
});

test('the panel draws the timeline, waiting prompts last, and says when output was cut', () => {
  const panel = readFileSync(new URL('../src/panels/AgentPanel.jsx', import.meta.url), 'utf8');
  assert.ok(panel.includes('A.timeline(model)'));
  assert.ok(panel.includes('A.waitingApprovals(model)'));
  assert.ok(!panel.includes('model.approvals.map('), 'no list of every approval after the rows');
  for (const table of [en, zh]) assert.ok(table.agent.toolTruncated.includes('%s'));
});

test('a host that lost its agent is ensured again, not asked for the snapshot forever', () => {
  const hook = readFileSync(new URL('../src/lib/useAgent.js', import.meta.url), 'utf8');
  const at = hook.indexOf("error.code !== 'agent_chat_not_ready'");
  assert.ok(at > 0);
  assert.ok(hook.slice(at, at + 200).includes('client.chatEnsure()'));
});
