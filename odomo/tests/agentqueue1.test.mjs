// AGENT-QUEUE-1: Odomo's queue, checklist, decline-with-a-reason and earlier
// messages, on core's fixtures generated from real codex 0.160 frames.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import * as A from '../src/lib/agent.js';
import {HostClient} from '../src/lib/api.js';
import {en, zh} from '../src/lib/i18n.js';

const FIXTURES = new URL('../../contracts/fixtures/', import.meta.url);
const fixture = (name) => JSON.parse(readFileSync(new URL(name, FIXTURES), 'utf8'));
const render = async () => {
  const {runnerImport} = await import('vite');
  const {module} = await runnerImport(new URL('./render.jsx', import.meta.url).pathname,
    {configFile: false, logLevel: 'silent'});
  return module;
};

test('the snapshot carries codex\'s queue, its checklist and that there is more before', () => {
  const m = A.restore(A.initial(), fixture('agent-chat-snapshot-queue.json'));
  assert.deepEqual(m.queue.map((item) => item.text), ['And then run the tests on SQLite.', 'Keep the old port as a fallback.']);
  assert.equal(m.queue[0].requestID, 'request-fixture-q1');
  assert.equal(m.checklist.steps.length, 3);
  assert.equal(m.earlier, true);
  // a snapshot of an older core: no queue (steer instead), no checklist, nothing before
  const old = A.restore(A.initial(), fixture('agent-chat-snapshot-plan.json'));
  assert.equal(old.queue, null);
  assert.equal(old.checklist, null);
  assert.equal(old.earlier, false);
});

test('while a turn runs, Send queues - only when codex has a queue', () => {
  const m = {...A.restore(A.initial(), fixture('agent-chat-snapshot-queue.json')), phase: 'running', turnID: 't1', draft: 'next'};
  assert.ok(A.canQueue(m));
  assert.ok(!A.canQueue({...m, queue: null}));
  assert.ok(A.canSteer({...m, queue: null}));
  assert.ok(!A.canQueue({...m, draft: '  '}));
  assert.ok(!A.canQueue({...m, phase: 'ready', turnID: null}));
});

test('agent.queue.changed and agent.checklist.updated, in sequence', () => {
  const queued = fixture('agent-chat-event-queue.json');
  const ticked = fixture('agent-chat-event-checklist.json');
  let m = A.restore(A.initial(), {...fixture('agent-chat-snapshot-queue.json'), identity: queued.identity, sequence: 0,
    queue: [], checklist: null, activeTurn: ticked.event.checklist.turnID});
  m = A.apply(m, queued);
  assert.equal(m.queue.length, 2);
  m = A.apply(m, ticked);
  const line = A.checklistLine(m.checklist);
  assert.deepEqual(line, {done: 1, total: 3, current: 'Change the port'});
  // shown while a step is open or its turn runs; gone once both are over
  assert.ok(A.checklistShown(m));
  const done = {...m, checklist: {...m.checklist, steps: m.checklist.steps.map((s) => ({...s, status: 'completed'}))}};
  assert.ok(A.checklistShown(done), 'its turn still runs');
  assert.equal(A.checklistShown({...done, turnID: null, phase: 'ready'}), null);
  // left unfinished: shown until a later turn starts
  const row = {kind: 'message', id: 'message:x', itemID: 'x', turnID: m.checklist.turnID, role: 'user', text: 'go'};
  const left = {...m, turnID: null, phase: 'ready', rows: m.rows.concat(row)};
  assert.ok(A.checklistShown(left));
  assert.equal(A.checklistShown({...left, rows: left.rows.concat({...row, id: 'message:y', turnID: 'later'})}), null);
  // an unknown shape changes nothing and still advances the sequence
  m = A.apply(m, {identity: queued.identity, sequence: 3, event: {type: 'agent.checklist.updated', checklist: {steps: 'no'}}});
  assert.equal(m.checklist, null);
  assert.equal(m.lastSequence, 3);
});

test('earlier messages go in front, and a later snapshot keeps them', () => {
  const snapshot = fixture('agent-chat-snapshot-queue.json');
  const page = fixture('agent-chat-history.json');
  let m = A.restore(A.initial(), snapshot);
  const before = m.rows.length;
  m = A.prependEarlier(m, page);
  assert.equal(m.rows.length, before + page.rows.length);
  assert.deepEqual(m.rows.slice(0, 2).map((row) => row.text), ['hello 5', 'Got it: hello 5']);
  assert.equal(m.earlier, true);
  // the stream reconnects: the snapshot starts at the same row, the older ones stay
  const again = A.restore(m, {...snapshot, sequence: snapshot.sequence + 1});
  assert.equal(again.rows.length, m.rows.length);
  assert.equal(again.rows[0].text, 'hello 5');
  // a page already here is not added twice
  assert.equal(A.prependEarlier(m, page).rows.length, m.rows.length);
});

test('a giant message is marked as cut', async () => {
  const snapshot = fixture('agent-chat-snapshot-queue.json');
  const rows = snapshot.rows.slice();
  rows[1] = {...rows[1], text: 'x'.repeat(10), text_truncated: 123};
  const m = A.restore(A.initial(), {...snapshot, rows});
  assert.equal(m.rows[1].textTruncated, 123);
});

test('the client speaks the new routes, with a decline\'s reason', async () => {
  const calls = [];
  const client = new HostClient({base: 'https://h.example/', token: 'x'});
  client.request = async (method, path, body) => { calls.push([method, path, body]); return {queue: []}; };
  await client.chatQueue('r1', 'later');
  await client.chatQueueItem('q-01', 'send');
  await client.chatQueueItem('q-01', 'edit', 'new words');
  await client.chatQueueItem('q-01', 'delete');
  await client.chatHistory('m-d1', 50);
  await client.chatResolve('7', {decision: 'decline', reason: 'Use /tmp.'});
  await client.chatResolve('8', {decision: 'decline'});
  assert.deepEqual(calls, [
    ['POST', '/v1/agent/default/chat/queue', {request_id: 'r1', text: 'later'}],
    ['POST', '/v1/agent/default/chat/queue/q-01:send', {}],
    ['POST', '/v1/agent/default/chat/queue/q-01:edit', {text: 'new words'}],
    ['POST', '/v1/agent/default/chat/queue/q-01:delete', {}],
    ['GET', '/v1/agent/default/chat/history?before=m-d1&limit=50', undefined],
    ['POST', '/v1/agent/default/chat/approvals/7', {decision: 'decline', reason: 'Use /tmp.'}],
    ['POST', '/v1/agent/default/chat/approvals/8', {decision: 'decline'}],
  ]);
  assert.throws(() => client.chatQueueItem('q/../x', 'send'));
});

test('the queue, the checklist and the reason drawn, EN and 中文', async () => {
  const r = await render();
  const m = A.restore(A.initial(), fixture('agent-chat-snapshot-queue.json'));
  const rows = r.renderQueue({queue: m.queue, running: true});
  assert.match(rows, /data-testid="agent-queue-count"[^>]*>2 waiting</);
  assert.match(rows, /They go out when this turn ends\./);
  assert.match(rows, /data-testid="agent-queue-send-q-01"[^>]*>Send now</);
  assert.match(rows, /data-testid="agent-queue-edit-button-q-01"[^>]*>Edit</);
  assert.match(rows, /data-testid="agent-queue-delete-q-01"[^>]*>Delete</);
  assert.match(r.renderQueue({queue: m.queue, running: false}), /Stopped\. Send them when you’re ready\./);
  assert.match(r.renderQueue({queue: m.queue, running: true}, 'zh-Hans'), /2 条在等/);
  assert.equal(r.renderQueue({queue: [], running: true}), '');
  // a clipped one has no Edit: its whole words are not here
  assert.doesNotMatch(r.renderQueue({queue: [{...m.queue[0], clipped: true}], running: true}), /agent-queue-edit-button/);
  const steps = r.renderChecklist({list: {...m.checklist, steps: [{step: 'Read the config', status: 'completed'},
    {step: 'Change the port', status: 'inProgress'}, {step: 'Run the tests', status: 'pending'}]}});
  assert.match(steps, /data-testid="agent-steps-progress"[^>]*>1 of 3</);
  assert.match(steps, />Change the port</);
  assert.match(r.renderChecklist({list: m.checklist}, 'zh-Hans'), /步骤/);
  const approval = A.approvalOf({request_id: '7', kind: 'commandExecution', summary: 'touch blocked.txt',
    details: {}, decisions: ['accept', 'acceptForSession', 'decline', 'cancel']});
  const card = r.renderApproval({approval, onDeclineWhy: () => {}});
  assert.match(card, /data-testid="agent-approval-why-7"[^>]*>Decline with a reason…</);
  assert.match(r.renderApproval({approval, onDeclineWhy: () => {}}, 'zh-Hans'), /拒绝并说明原因…/);
  // no reason offered where there is no decline to give one with
  assert.doesNotMatch(r.renderApproval({approval: {...approval, decisions: ['accept']}, onDeclineWhy: () => {}}), /agent-approval-why/);
});

test('every new sentence is said in both languages', () => {
  for (const key of ['queueCount', 'queueWhenEnds', 'queueWaiting', 'queueSendNow', 'queueEdit', 'queueDelete', 'queueSave',
    'queueLabel', 'queueEditGone', 'queueUnconfirmed', 'stepsTitle', 'stepsProgress', 'stepsShow', 'stepsHide', 'stepDone',
    'stepNow', 'stepTodo', 'declineWhy', 'declinePlaceholder', 'earlier', 'earlierLoading', 'trimmed', 'planKeep', 'planKeepHint']) {
    assert.ok(en.agent[key], 'en ' + key);
    assert.ok(zh.agent[key], 'zh ' + key);
    assert.notEqual(en.agent[key], zh.agent[key], key);
  }
});

test('the panel wires the queue, the checklist, earlier messages and Keep planning', () => {
  const panel = readFileSync(new URL('../src/panels/AgentPanel.jsx', import.meta.url), 'utf8');
  assert.match(panel, /<QueueRows queue=\{model\.queue\}/);
  assert.match(panel, /A\.checklistShown\(model\) \? <Checklist/);
  assert.match(panel, /data-testid="agent-earlier"/);
  assert.match(panel, /onDeclineWhy=\{store\.declineWithReason\}/);
  assert.match(panel, /onKeep: \(\) =>/);
  assert.match(panel, /if \(queueing\) store\.queue\(\); else store\.steer\(\);/);
});

test('Leo\'s looping question card: the same three questions come back at the review, with the answers', async () => {
  const r = await render();
  const wire = {request_id: '2', kind: 'userInput', summary: 'Database', decisions: [], details: {questions: [
    {id: 'db', header: 'Database', question: 'Which database?', options: ['SQLite', 'Postgres'], other: true},
    {id: 'port', header: 'Port', question: 'Which port?', options: ['9090', '8080'], other: true},
    {id: 'token', header: 'Token', question: 'Token?', options: [], secret: true}]}};
  let m = A.restore(A.initial(), {identity: {hostID: 'h', agentID: 'default', provider: 'codex', providerSessionID: 't'},
    rows: [], activeTurn: 't1', sequence: 0});
  const envelope = (sequence, event) => ({identity: m.identity, sequence, event});
  m = A.apply(m, envelope(1, {type: 'agent.approval.requested', ...wire}));
  const first = m.approvals[0];
  // What the card keeps as the person answers; Answer moves it to the review, without the secret.
  r.keepQuestionDraft(first, {picked: {db: 'Postgres', port: '8080'}, typed: {token: 's3cret'}, at: 3});
  const card = r.renderApproval({approval: first});
  assert.match(card, /data-testid="agent-question-step-2"/);
  // answered, then codex sends the identical request again (same id)
  m = A.settleDecision(m, '2', 'input', 'client');
  m = A.apply(m, envelope(2, {type: 'agent.approval.requested', ...wire}));
  const again = A.waitingApprovals(m)[0];
  assert.equal(again.requestID, '2');
  const drawn = r.renderApproval({approval: again});
  assert.match(drawn, /Check your answers/);
  assert.match(drawn, /Postgres/);
  assert.match(drawn, /8080/);
  assert.doesNotMatch(drawn, /s3cret/);
  // other questions under the same id start clean
  const other = A.approvalOf({...wire, details: {questions: [{id: 'x', question: 'Something else?', options: []}]}});
  assert.equal(r.savedQuestionDraft(other), null);
});
