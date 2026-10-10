// AGENT-COLLIE-1: Odomo's side of the Agent panel ideas borrowed from Collie -
// change cards, steps folded into runs, question cards that fit a phone, and
// finding your place in a long conversation. The rows come from core's own
// fixtures, which core builds from real codex 0.160 frames.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import * as A from '../src/lib/agent.js';
import * as S from '../src/lib/agentSteps.js';
import {en, zh} from '../src/lib/i18n.js';

const FIXTURES = new URL('../../contracts/fixtures/', import.meta.url);
const fixture = (name) => JSON.parse(readFileSync(new URL(name, FIXTURES), 'utf8'));
const ID = {hostID: 'h', agentID: 'default', provider: 'codex', providerSessionID: 't'};
const snap = (extra = {}) => ({identity: ID, rows: [], activeTurn: null, sequence: 0, pendingApprovals: [],
  status: {type: 'idle', activeFlags: [], waiting: null}, usage: null, ...extra});
const render = async () => {
  const {runnerImport} = await import('vite');
  const {module} = await runnerImport(new URL('./render.jsx', import.meta.url).pathname,
    {configFile: false, logLevel: 'silent'});
  return module;
};
const tool = (id, status = 'succeeded', extra = {}) => ({kind: 'tool', id: 'tool:' + id, itemID: id, turnID: 't1',
  name: 'bash -lc ' + id, status, detail: '', command: id, actions: [{type: 'unknown'}], ...extra});
const msg = (id, role, text) => ({kind: 'message', id: 'message:' + id, itemID: id, turnID: 't1', role, text});
const entriesOf = (rows) => rows.map((row) => ({kind: 'row', id: row.id, row}));

test('real file changes become change cards; the waiting prompt names them', () => {
  const m = A.restore(A.initial(), fixture('agent-chat-snapshot-changes.json'));
  const inside = m.rows.find((row) => row.itemID === 'call-p1-1');
  const outside = m.rows.find((row) => row.itemID === 'call-p2-2');
  const byName = Object.fromEntries(inside.changes.map((c) => [S.fileName(c.path), c]));
  assert.deepEqual(Object.keys(byName).sort(), ['app.py', 'notes.txt', 'old.txt', 'plan.md']);
  assert.equal(S.changeVerb(en, byName['app.py']), 'Edited');
  assert.equal(S.changeVerb(en, byName['plan.md']), 'Created');
  assert.equal(S.changeVerb(en, byName['old.txt']), 'Deleted');
  assert.equal(S.changeVerb(en, byName['notes.txt']), 'Moved');
  assert.equal(S.changeSubject(byName['notes.txt']), 'notes.txt → docs/notes.txt');
  assert.equal(S.changeVerb(zh, byName['app.py']), '编辑');
  const lines = S.diffLines(byName['app.py']);
  assert.equal(lines[0].kind, 'hunk');
  assert.deepEqual(lines.filter((l) => l.kind === 'add').length, byName['app.py'].added);
  assert.deepEqual(lines.filter((l) => l.kind === 'del').length, byName['app.py'].removed);
  assert.equal(S.stepText(en, inside), 'Edited ' + S.changeSubject(inside.changes[0]) + ' and 3 more');
  assert.equal(S.stepKind(inside), 'edit');
  assert.equal(S.editCount(inside), 4);
  // the prompt: its row, its files and counts; never a detail line for them
  const [waiting] = A.waitingApprovals(m);
  assert.equal(waiting.kind, 'fileChange');
  assert.equal(waiting.itemID, outside.itemID);
  assert.deepEqual(waiting.changeFiles, [outside.changes[0].path]);
  assert.deepEqual([waiting.changeAdded, waiting.changeRemoved], [2, 1]);
  assert.ok(!waiting.details.some((d) => ['files', 'added', 'removed'].includes(d.key)));
});

test('a change card draws its diff, folds the rest, and lights up while it waits', async () => {
  const r = await render();
  const m = A.restore(A.initial(), fixture('agent-chat-snapshot-changes.json'));
  const outside = m.rows.find((row) => row.itemID === 'call-p2-2');
  const html = r.renderToolRow({row: outside, waiting: true});
  assert.match(html, /data-testid="agent-change-call-p2-2-0"/);
  assert.match(html, /achg--wait/);
  assert.match(html, /Waiting for you to allow this change/);
  assert.match(html, />\+2<\/span> <span class="achg__del">−1</);
  assert.match(html, /aria-label="2 added, 1 removed"/);
  assert.match(html, /mode = fast/);
  const long = {...outside, changes: [{...outside.changes[0], diff: Array.from({length: 30}, (_, i) => '+l' + i), added: 30,
    diffOmitted: 7}], changesOmitted: 2};
  const folded = r.renderToolRow({row: long});
  assert.match(folded, /Show 18 more lines/);
  assert.doesNotMatch(folded, />l12</);
  assert.match(folded, /7 more lines stay on the computer\./);
  assert.match(folded, /2 more files changed; they are on the computer\./);
  assert.match(r.renderToolRow({row: long}, 'zh-Hans'), /再显示 18 行/);
  // in an open run an edit is one line until tapped, unless a prompt waits on it
  const compact = r.renderToolRow({row: outside, compact: true});
  assert.doesNotMatch(compact, /class="achg__d"/);
  assert.match(compact, /Edited/);
  assert.match(r.renderToolRow({row: outside, compact: true, waiting: true}), /achg--wait/);
  // the waiting card: files, counts and the way to the change
  const [waiting] = A.waitingApprovals(m);
  const card = r.renderApproval({approval: waiting, row: outside, onSee: () => {}});
  assert.match(card, /1 file · /);
  assert.match(card, /settings\.txt/);
  assert.match(card, /data-testid="agent-change-see-0"[^>]*>See the change</);
  // an old row without `changes` draws as it always did
  const old = r.renderToolRow({row: {...tool('x'), command: undefined, actions: undefined, name: 'File changes'}});
  assert.match(old, /File changes/);
  assert.doesNotMatch(old, /achg/);
});

test('real commands read as steps; a run folds them into one line', () => {
  const m = A.restore(A.initial(), fixture('agent-chat-snapshot-steps.json'));
  const steps = m.rows.filter((row) => row.kind === 'tool');
  assert.deepEqual(steps.map((row) => S.stepText(en, row)),
    ['$ git status', 'Read app.py', '$ ls -la', 'Searched TODO', 'Read app.py', '$ false']);
  assert.deepEqual(steps.map((row) => S.stepText(zh, row)),
    ['$ git status', '读取 app.py', '$ ls -la', '搜索 TODO', '读取 app.py', '$ false']);
  assert.equal(steps[5].exitCode, 1);
  const items = S.groupRuns(A.timeline(m));
  assert.deepEqual(items.map((item) => item.kind), ['entry', 'run', 'entry']);
  const run = items[1];
  assert.equal(run.id, 'run:call-s0-4');
  assert.deepEqual(S.runCounts(run), {commands: 2, reads: 2, searches: 2, edits: 0, others: 0});
  assert.equal(S.runSummary(en, S.runCounts(run)), '2 commands, 2 files read, 2 searches');
  assert.equal(S.runSummary(zh, S.runCounts(run)), '2 条命令，读了 2 个文件，2 次搜索');
  assert.equal(S.runFailed(run), 1);
  assert.equal(S.runSummary(en, {commands: 1, reads: 0, searches: 1, edits: 1, others: 1}),
    '1 command, 1 search, 1 edit, 1 other step');
});

test('fewer than three steps stay as they are; a message ends a run; answered lines ride along', () => {
  const rows = [tool('a'), tool('b'), msg('m', 'assistant', 'x'), tool('c'), tool('d'), tool('e')];
  const entries = entriesOf(rows);
  entries.splice(4, 0, {kind: 'answered', id: 'approval:1', approval: {summary: 'make'}});
  const items = S.groupRuns(entries);
  assert.deepEqual(items.map((item) => item.kind), ['entry', 'entry', 'entry', 'run']);
  assert.equal(items[3].entries.length, 4);
  // an answered line with no step before it is a single entry
  assert.equal(S.groupRuns([{kind: 'answered', id: 'approval:2', approval: {}}])[0].kind, 'entry');
});

test('a working run shows its newest six steps, earlier when a step waits, and stays open', () => {
  const rows = Array.from({length: 10}, (_, i) => tool('s' + i, i === 9 ? 'running' : 'succeeded'));
  const [run] = S.groupRuns(entriesOf(rows));
  assert.ok(S.runLive(run, new Set()));
  assert.equal(S.tailStart(run, new Set()), 4);
  assert.equal(S.stepsBefore(run, 4), 4);
  assert.equal(S.tailStart(run, new Set(['s1'])), 1);
  // the person's tap wins; a held run stays open after it finishes
  assert.equal(S.runMode(undefined, true, false), 'tail');
  assert.equal(S.runMode(undefined, false, true), 'tail');
  assert.equal(S.runMode(undefined, false, false), 'folded');
  assert.equal(S.runMode(false, true, true), 'folded');
  assert.equal(S.runMode(true, false, false), 'all');
});

test('a run draws folded, as its tail, or whole', async () => {
  const r = await render();
  const rows = Array.from({length: 8}, (_, i) => tool('s' + i, i === 3 ? 'failed' : 'succeeded'));
  const [run] = S.groupRuns(entriesOf(rows));
  const folded = r.renderRun({run: {...run, waiting: new Set()}, mode: 'folded'});
  assert.match(folded, /8 commands/);
  assert.match(folded, /1 failed/);
  assert.match(folded, /\$ s7/);
  assert.match(folded, /aria-expanded="false"/);
  const tail = r.renderRun({run: {...run, waiting: new Set()}, mode: 'tail'});
  assert.match(tail, /2 earlier steps/);
  assert.equal((tail.match(/data-entry=/g) || []).length, 6);
  const all = r.renderRun({run: {...run, waiting: new Set()}, mode: 'all'}, 'zh-Hans');
  assert.match(all, /收起这些步骤/);
  assert.equal((all.match(/data-entry=/g) || []).length, 8);
});

test('a real question keeps descriptions, Other and secret; the answer line never shows a secret', () => {
  const event = fixture('agent-chat-event-question.json');
  let m = A.restore(A.initial(), snap({identity: event.identity, sequence: event.sequence - 1, activeTurn: 't'}));
  m = A.apply(m, event);
  const [approval] = A.waitingApprovals(m);
  const [branch, token] = approval.questions;
  assert.deepEqual(branch.choices, [
    {label: 'main (Recommended)', description: 'The default branch everyone pulls from.'},
    {label: 'release', description: 'Only for tagged builds.'}]);
  assert.ok(branch.other && token.other && token.secret && !branch.secret);
  const input = A.submission({branch: 'release', token: 's3cret'});
  m = A.beginAnswer(m, approval.requestID, input);
  assert.deepEqual(m.approvals[0].answered, ['release']);
  assert.equal(m.approvals[0].state.kind, 'deciding');
  m = A.settleDecision(m, approval.requestID, 'input', 'client');
  assert.ok(!JSON.stringify(m).includes('s3cret'));
  // a failed answer forgets what it said; one answered on the host says nothing of ours
  let n = A.beginAnswer(A.apply(A.restore(A.initial(), snap({identity: event.identity, sequence: 0, activeTurn: 't'})),
    event), approval.requestID, input);
  assert.equal(A.failDecision(n, approval.requestID).approvals[0].answered, null);
  n = A.settleDecision(n, approval.requestID, null, 'elsewhere');
  assert.equal(n.approvals[0].answered, null);
  // an older core: bare labels, no Other
  const old = A.approvalOf({request_id: 1, kind: 'userInput', details: {questions: [{id: 'q', question: 'Pick',
    options: ['A', 'B']}]}});
  assert.deepEqual(old.questions[0].choices, [{label: 'A', description: null}, {label: 'B', description: null}]);
  assert.equal(old.questions[0].other, false);
});

test('question cards: stacked options, Other, a password field, a stepper and the answered line', async () => {
  const r = await render();
  const event = fixture('agent-chat-event-question.json');
  const approval = A.approvalOf(event.event);
  const html = r.renderApproval({approval});
  assert.match(html, /Question 1 of 2/);
  assert.match(html, /role="radio"[^>]*data-testid="agent-option-branch-0"/);
  assert.match(html, /The default branch everyone pulls from\./);
  assert.match(html, /data-testid="agent-option-branch-other"[^>]*>.*Other…/);
  assert.match(html, /data-testid="agent-question-next-0"[^>]*>Next</);
  assert.match(r.renderApproval({approval}, 'zh-Hans'), /第 1 题，共 2 题/);
  // one question: no stepper; a secret question with no options is a password
  const one = A.approvalOf({request_id: 2, kind: 'userInput', details: {questions: [{id: 'pw', question: 'Password?',
    secret: true, options: []}]}});
  const single = r.renderApproval({approval: one});
  assert.doesNotMatch(single, /Question 1 of/);
  assert.match(single, /type="password"/);
  // answered: what this page sent, or just "answered"
  const done = {...approval, state: {kind: 'resolved', decision: 'input', source: 'client'}, answered: ['release']};
  assert.match(r.renderApproval({approval: done}), /Answered: release/);
  assert.match(r.renderApproval({approval: {...done, answered: null}}), />answered</);
  assert.match(r.renderApproval({approval: {...done, state: {kind: 'resolved', decision: 'input', source: 'elsewhere'}}}),
    /answered on the host/);
  assert.match(r.renderApproval({approval: done}, 'zh-Hans'), /已回答：release/);
});

test('find searches what is on screen, never output, and steps with wrap', () => {
  const rows = [msg('u1', 'user', 'Fix the PORT please'), tool('grep -rn port .', 'succeeded',
    {detail: 'secret port in output', actions: [{type: 'search', query: 'port'}]}), msg('a1', 'assistant', 'Done.'),
  tool('ls', 'succeeded', {detail: 'port.txt'}), msg('u2', 'user', 'thanks')];
  const entries = entriesOf(rows);
  const order = entries.map((e) => e.id);
  const hits = S.findMatches(en, entries, 'port');
  assert.deepEqual(hits, ['message:u1', 'tool:grep -rn port .']);
  assert.deepEqual(S.findMatches(en, entries, '  '), []);
  assert.deepEqual(S.findMatches(en, entries, '(port'), []);
  assert.equal(S.step(hits, order, null, true), 'message:u1');
  assert.equal(S.step(hits, order, 'message:u1', true), 'tool:grep -rn port .');
  assert.equal(S.step(hits, order, 'tool:grep -rn port .', true), 'message:u1');
  assert.equal(S.step(hits, order, 'message:u1', false), 'tool:grep -rn port .');
  assert.equal(S.step([], order, null, true), null);
  assert.deepEqual(S.splitHighlight('Fix the PORT, port', 'port'),
    [{text: 'Fix the ', hit: false}, {text: 'PORT', hit: true}, {text: ', ', hit: false}, {text: 'port', hit: true}]);
});

test('the find bar says where it is in both languages', async () => {
  const r = await render();
  const html = r.renderFindBar({query: 'port', count: 3, current: 1});
  assert.match(html, />2\/3</);
  assert.match(html, /aria-label="Match 2 of 3"/);
  assert.match(html, /aria-label="Previous match"/);
  assert.match(r.renderFindBar({query: 'x', count: 0, current: -1}), /aria-label="No matches"/);
  assert.match(r.renderFindBar({query: '', count: 0, current: -1}, 'zh-Hans'), /placeholder="在对话里查找"/);
  const panel = readFileSync(new URL('../src/panels/AgentPanel.jsx', import.meta.url), 'utf8');
  for (const id of ['agent-find', 'agent-jump-newest', 'agent-jump-unseen']) {
    assert.match(panel, new RegExp('data-testid="' + id + '"'), id);
  }
  // AGENT-UI-3: no ↑ / ↓ between your own messages - scrolling does it, and
  // they sat on the conversation.
  for (const gone of ['agent-yours', 'yourMessages', 'stepYours']) assert.doesNotMatch(panel, new RegExp(gone), gone);
  assert.equal(S.yourMessages, undefined);
  assert.match(panel, /event\.metaKey \|\| event\.ctrlKey/);
});

test('new fields of a shape this page does not expect are dropped, never the row', () => {
  let m = A.restore(A.initial(), snap({activeTurn: 't1'}));
  const bad = {type: 'tool', value: {kind: 'tool', id: 'x', turnID: 't1', name: 'n', status: 'running', detail: '',
    command: 7, actions: 'no', exit_code: '1', duration_ms: -3, changes: [null, {path: 3}, {path: '/a', kind: 'weird'},
      {path: '/b', kind: 'add', diff: 5, added: 'x'}], changes_omitted: 'two'}};
  m = A.apply(m, {identity: ID, sequence: 1, event: bad});
  const [row] = m.rows;
  assert.equal(row.status, 'running');
  assert.equal(row.command, undefined);
  assert.equal(row.actions, undefined);
  assert.equal(row.exitCode, undefined);
  assert.equal(row.durationMs, undefined);
  assert.deepEqual(row.changes, [{path: '/b', kind: 'add', movePath: null, added: 0, removed: 0, diff: [], diffOmitted: 0}]);
  assert.equal(row.changesOmitted, undefined);
  const q = A.approvalOf({request_id: 3, kind: 'userInput', details: {questions: [{id: 'q', question: 'Q',
    options: ['A'], choices: 'nope', other: 'yes'}], files: 'nope', added: 'x'}});
  assert.deepEqual(q.questions[0].choices, [{label: 'A', description: null}]);
  assert.equal(q.questions[0].other, false);
  assert.deepEqual(q.changeFiles, []);
});

test('every new string is in both languages and says the same slots', () => {
  const keys = Object.keys(en.agent).filter((k) => /^(step|run|change|question|find|jump|your|toolExit|answeredWith)/.test(k));
  assert.ok(keys.length >= 50, String(keys.length));
  for (const key of keys) {
    assert.equal(typeof zh.agent[key], 'string', key);
    assert.equal((en.agent[key].match(/%s/g) || []).length, (zh.agent[key].match(/%s/g) || []).length, key);
  }
});
