// ODOMO-2: Odomo's Herdr and Agent panels reply like the App. The logic the
// panels draw, without a browser: the keys row and how typing becomes
// `/keys` and `/text`, what the composer says after a send, the second tap,
// the More menu, pane names, push routing, the agent's conversation model,
// the client's paths, and the mirror's terminal core.
import test from 'node:test';
import assert from 'node:assert/strict';
import * as H from '../src/lib/herdr.js';
import * as A from '../src/lib/agent.js';
import {openTarget} from '../src/lib/push.js';
import {HostClient, ApiError, openSocket} from '../src/lib/api.js';
import {Mirror, paletteOf, scaleFor, color256, MIN_SCALE} from '../src/lib/term.js';
import {en, zh, fill} from '../src/lib/i18n.js';
import {herdrReason, agentErrorText} from '../src/lib/reasons.js';
import {PANELS, barPanels} from '../src/lib/panels.js';

// ── the keys row ──────────────────────────────────────────────────────────
test('the keys row is the App\'s: esc ⇥ ⇧⇥ ↩ ␣ ↑↓←→ ⌫ ^C ^D 1-5, in Herdr\'s own key names', () => {
  assert.deepEqual(H.KEY_ROW.map((cap) => cap.face),
    ['esc', '⇥', '⇧⇥', '↩', '␣', '↑', '↓', '←', '→', '⌫', '^C', '^D', '1', '2', '3', '4', '5']);
  assert.deepEqual(H.KEY_ROW.map((cap) => cap.key),
    ['Escape', 'Tab', 'shift+Tab', 'Enter', 'Space', 'Up', 'Down', 'Left', 'Right', 'Backspace', 'ctrl+c', 'ctrl+d',
      '1', '2', '3', '4', '5']);
  // the arrows and ⌫ (HERDR-9) repeat while held, nothing else does
  assert.deepEqual(H.KEY_ROW.filter((cap) => cap.repeats).map((cap) => cap.id), ['up', 'down', 'left', 'right', 'backspace']);
  // a phone splits after the arrows
  assert.equal(H.KEY_ROW[H.FIRST_ROW].id, 'backspace');
  for (const table of [en, zh]) {
    for (const cap of H.KEY_ROW) if (cap.name !== 'digit') assert.equal(typeof table.herdr.keyNames[cap.name], 'string', cap.name);
  }
});

const key = (k, extra = {}) => ({key: k, ctrlKey: false, altKey: false, shiftKey: false, metaKey: false, ...extra});

test('Type: a keydown becomes Herdr\'s key name, or is left to the text input, or is said to be unsendable', () => {
  assert.deepEqual(H.keyOf(key('Escape')), {key: 'Escape'});
  assert.deepEqual(H.keyOf(key('Enter')), {key: 'Enter'});
  assert.deepEqual(H.keyOf(key('Backspace')), {key: 'Backspace'});
  assert.deepEqual(H.keyOf(key('Tab', {shiftKey: true})), {key: 'shift+Tab'});
  assert.deepEqual(H.keyOf(key('ArrowUp')), {key: 'Up'});
  assert.deepEqual(H.keyOf(key('ArrowLeft', {ctrlKey: true})), {key: 'ctrl+Left'});
  assert.deepEqual(H.keyOf(key('F5')), {key: 'F5'});
  assert.deepEqual(H.keyOf(key('c', {ctrlKey: true})), {key: 'ctrl+c'});
  assert.deepEqual(H.keyOf(key('C', {ctrlKey: true, shiftKey: true})), {key: 'ctrl+c'});
  // a Mac's Option+X types "≈"; a terminal's Meta means the key itself
  assert.deepEqual(H.keyOf(key('≈', {altKey: true, code: 'KeyX'})), {key: 'alt+x'});
  assert.deepEqual(H.keyOf(key(' ', {ctrlKey: true})), {key: 'ctrl+Space'});
  // plain characters go through `beforeinput`, which is what carries IME,
  // dictation and the phone keyboards
  assert.deepEqual(H.keyOf(key('a')), {text: true});
  assert.deepEqual(H.keyOf(key('中')), {text: true});
  // Herdr 0.8.2 has no name for these
  for (const name of ['Home', 'End', 'PageUp', 'PageDown', 'Insert', 'Delete']) assert.deepEqual(H.keyOf(key(name)), {drop: true});
  for (const name of ['Shift', 'Control', 'Meta', 'Dead', 'Process']) assert.equal(H.keyOf(key(name)), null);
  assert.equal(H.keyOf(key('c', {metaKey: true})), null, 'the browser\'s own shortcuts stay its own');
  assert.equal(H.keyOf({...key('a'), isComposing: true}), null);
});

test('Type: what `beforeinput` adds, and how a run of it is sent', () => {
  assert.deepEqual(H.inputOf({inputType: 'insertText', data: 'x'}), {text: 'x'});
  assert.deepEqual(H.inputOf({inputType: 'insertLineBreak'}), {key: 'Enter'});
  assert.deepEqual(H.inputOf({inputType: 'deleteContentBackward'}), {key: 'Backspace'});
  assert.deepEqual(H.inputOf({inputType: 'deleteWordBackward'}), {key: 'ctrl+w'});
  assert.equal(H.inputOf({inputType: 'formatBold'}), null);
  // short and plain: single keys (a space is Space); long, or not plain: one
  // `/text`, never submitted
  assert.deepEqual(H.runWrites(':wq'), [{keys: [':', 'w', 'q']}]);
  assert.deepEqual(H.runWrites('a b'), [{keys: ['a', 'Space', 'b']}]);
  assert.deepEqual(H.runWrites('hello world'), [{text: 'hello world', submit: false}]);
  assert.deepEqual(H.runWrites('你好'), [{keys: ['你', '好']}]);
  assert.deepEqual(H.runWrites('a\tb'), [{text: 'a\tb', submit: false}]);
  // order is kept, neighbouring keys share a call, no call over 32 keys
  assert.deepEqual(H.mergeWrites([{keys: ['i']}, {keys: ['h']}, {text: 'pasted words', submit: false}, {keys: ['Escape']}]),
    [{keys: ['i', 'h']}, {text: 'pasted words', submit: false}, {keys: ['Escape']}]);
  const many = H.mergeWrites([{keys: Array(40).fill('x')}]);
  assert.deepEqual(many.map((w) => w.keys.length), [32, 8]);
});

// ── what the composer says ────────────────────────────────────────────────
test('the composer\'s honest line: Sent ✓, Sent (a short reply), Not submitted - reason', () => {
  assert.deepEqual(H.sendStateOf({submitted: true, proof: 'tail'}), {kind: 'sent'});
  assert.deepEqual(H.sendStateOf({submitted: true, proof: 'line'}), {kind: 'sentLight'});
  assert.deepEqual(H.sendStateOf({submitted: false, delivered: true, reason: 'not_seen'}), {kind: 'notSubmitted', reason: 'not_seen'});
  assert.deepEqual(H.sendStateOf({submitted: false, delivered: false, reason: 'herdr_agent_blocked'}),
    {kind: 'notSubmitted', reason: 'herdr_agent_blocked'});
  assert.deepEqual(H.sendStateOf({submitted: false}), {kind: 'notSubmitted', reason: 'not_seen'});
  assert.equal(H.sentLinger({kind: 'sent'}), 4000);
  assert.equal(H.sentLinger({kind: 'sentLight'}), 6000);
  assert.equal(en.herdr.sent, 'Sent ✓');
  assert.doesNotMatch(en.herdr.sentLight, /✓/);
  assert.equal(fill(en.herdr.notSubmitted, herdrReason(en, 'herdr_agent_blocked')),
    'Not submitted — The agent is waiting on a question, so nothing was typed; answer it with the choices above the box, or with the keys (1, ↓ ↩ or esc).');
  assert.ok(fill(zh.herdr.notSubmitted, herdrReason(zh, 'not_seen')).startsWith('没有提交 — 文字没有出现'));
  // a code nobody mapped is reportable, beside a sentence, never alone
  assert.equal(herdrReason(en, 'brand_new'), 'The host could not finish this Herdr request (brand_new).');
});

// ── a second tap ──────────────────────────────────────────────────────────
test('danger: the first tap only arms, the second within 3 s sends, a late one arms again', () => {
  let state = H.secondTap(null, 'key:Ctrl Z', true, 1000);
  assert.deepEqual(state, {send: false, armed: {id: 'key:Ctrl Z', until: 4000}});
  assert.deepEqual(H.secondTap(state.armed, 'key:Ctrl Z', true, 3999), {send: true, armed: null});
  assert.equal(H.secondTap(state.armed, 'key:Ctrl Z', true, 4000).send, false, 'too late: arms again');
  assert.equal(H.secondTap(state.armed, 'key:Ctrl D', true, 2000).send, false, 'another row is its own first tap');
  assert.equal(H.secondTap(null, 'key:Ctrl U', false, 0).send, true, 'a safe row sends at once');
  // the presets: what ships less ^C/^D (already on the row); ^D and ^Z ask twice
  const shipped = H.shippedRows('codex');
  assert.deepEqual(H.presets(shipped).map((row) => [H.presetFace(row), row.danger]),
    [['^U', false], ['^R', false], ['^L', false], ['^Z', true]]);
  assert.deepEqual(H.presets({keys: [{label: 'Ctrl C', keys: ['ctrl+c'], operator: true, danger: false}]}).length, 1,
    'an operator\'s rows are kept whole');
  assert.equal(H.presetFace({label: 'save', keys: ['Escape', ':', 'w'], operator: true}), 'save');
});

test('what ships when core has no rows: the App\'s own Quick, harness and presets', () => {
  assert.deepEqual(H.shippedRows(null).quick, [{id: 'confirm', title: null, items: ['y', 'n'], operator: false}]);
  const claude = H.shippedRows('claude-code');
  assert.equal(claude.family, 'claude');
  assert.deepEqual(claude.harness.map((row) => row.command), ['/model', '/effort', '/compact', '/resume']);
  assert.deepEqual(H.shippedRows('codex').harness.map((row) => row.command), ['/model', '/compact', '/resume']);
  assert.deepEqual(H.shippedRows('codex').quick[1].items, ['continue', 'commit and push', 'retry', 'skip']);
  assert.equal(H.harnessTitle({id: 'model', label: null, command: '/model'}, zh), '模型');
  assert.equal(H.harnessTitle({id: null, label: 'Plan', command: '/plan'}, en), 'Plan');
  assert.equal(H.operatorWarning({sources: [{file: 'keys.toml', warning: {code: 'operator_file_unparsed'}}]}, en),
    'keys.toml on the computer could not be read, so the last version that worked is still in use.');
});

// ── the layout ────────────────────────────────────────────────────────────
const LAYOUT = {session: 'work', focused: {pane_id: 'w1:p2'}, revision: 3, workspaces: [
  {id: 'w1', number: 1, label: 'omodachi', focused: true, active_tab_id: 'w1:t1', tabs: [
    {id: 'w1:t1', panes: [
      {id: 'w1:p1', title: '', cwd: '/home/u/work/omodachi', agent: null, size: {cols: 180, rows: 48}},
      {id: 'w1:p2', title: '', cwd: '', agent: {name: 'codex', kind: 'codex', status: 'blocked'}, agent_status: 'blocked',
        size: {cols: 90, rows: 48}},
      {id: 'w1:p3', title: '', cwd: '', agent: null}]},
    {id: 'w1:t2', panes: [{id: 'w1:p4', title: 'logs', cwd: '/var/log'}]}]},
  {id: 'w2', number: 2, active_tab_id: 'w2:t1', tabs: [{id: 'w2:t1', panes: [{id: 'w2:p1', focused: true}]}]}]};

test('a pane is named the way a person would, never w1:p2, and "Waiting for you" is said in words', () => {
  const numbered = (n) => fill(en.herdr.paneNumbered, n);
  assert.equal(H.paneName(LAYOUT, 'w1:p1', numbered), 'omodachi');
  assert.equal(H.paneName(LAYOUT, 'w1:p2', numbered), 'codex');
  assert.equal(H.paneName(LAYOUT, 'w1:p3', numbered), 'Pane 3');
  assert.equal(H.paneName(LAYOUT, 'w1:p4', numbered), 'logs');
  for (const pane of H.panesOf(LAYOUT)) assert.doesNotMatch(H.paneName(LAYOUT, pane.id, numbered), /^w\d+:p\d+$/);
  assert.equal(H.isWaiting(H.paneOf(LAYOUT, 'w1:p2')), true);
  assert.equal(H.isWaiting(H.paneOf(LAYOUT, 'w1:p1')), false);
  assert.equal(H.agentLabel(H.paneOf(LAYOUT, 'w1:p2')), 'codex · blocked');
  assert.equal(en.herdr.waiting, 'Waiting for you');
  assert.equal(zh.herdr.waiting, '在等你');
  assert.equal(H.preferredPane(LAYOUT), 'w1:p2');
  assert.equal(H.workspacePane(LAYOUT, 'w2'), 'w2:p1');
  assert.deepEqual(H.paneGeometry(H.paneOf(LAYOUT, 'w1:p1')), {cols: 180, rows: 48});
  assert.equal(H.paneGeometry(H.paneOf(LAYOUT, 'w1:p3')), null);
  assert.equal(H.rowsAgent(H.paneOf(LAYOUT, 'w1:p2')), 'codex');
  assert.equal(H.rowsAgent(H.paneOf(LAYOUT, 'w1:p1')), null);
});

test('the More menu: new workspace, new tab, close tab, close pane - never split, zoom or focus', () => {
  // HERDR-13 took out split right, split down and zoom.
  assert.deepEqual(H.MENU, ['newWorkspace', 'newTab', 'closeTab', 'close']);
  for (const gone of ['splitRight', 'splitDown', 'zoom']) {
    assert.equal(H.controlRequest(gone, LAYOUT, 'w1:p2').kind, 'unsupported', gone);
  }
  assert.deepEqual(H.controlRequest('close', LAYOUT, 'w1:p2'), {kind: 'pane', pane: 'w1:p2', action: 'close', body: {}});
  assert.deepEqual(H.controlRequest('newTab', LAYOUT, 'w1:p2'), {kind: 'newTab', workspace: 'w1'});
  assert.deepEqual(H.controlRequest('closeTab', LAYOUT, 'w1:p4'), {kind: 'closeTab', workspace: 'w1', tab: 'w1:t2'});
  assert.deepEqual(H.controlRequest('closeTab', LAYOUT, 'w2:p1'), {kind: 'unsupported', reason: 'onlyOneTab'});
  assert.deepEqual(H.controlRequest('close', LAYOUT, null), {kind: 'unsupported', reason: 'noSelectedPane'});
  assert.deepEqual(H.controlRequest('nextPane', LAYOUT, 'w2:p1'), {kind: 'select', pane: 'w1:p1'});
  assert.ok(H.CONFIRMS.has('close') && H.CONFIRMS.has('closeTab') && !H.CONFIRMS.has('newTab'));
  for (const action of H.MENU) {
    for (const table of [en, zh]) assert.equal(typeof table.herdr[action], 'string', action);
    assert.notEqual(H.controlRequest(action, LAYOUT, 'w1:p1').action, 'focus');
  }
});

test('sessions: the switcher\'s line, a stopped session, and a new name checked before core is asked', () => {
  const sessions = [{name: 'work', running: true, owned: false, readable: true, workspaces: 2, panes: 5, agents: 1},
    {name: 'omodachi', running: true, owned: true, readable: true, workspaces: 1, panes: 0, agents: 0},
    {name: 'default', running: false, owned: false, readable: false}];
  assert.equal(H.sessionDetail(sessions, 'work', {}, en), '2 workspaces · 5 panes · 1 agents');
  assert.equal(H.sessionDetail(sessions, 'omodachi', {}, en), 'No pane yet');
  assert.equal(H.sessionDetail(sessions, 'default', {}, en), 'Stopped');
  assert.equal(H.sessionDetail(sessions, 'work', {starting: 'default'}, zh), '正在电脑上启动 default…');
  assert.equal(H.stoppedSession(sessions, 'default'), 'default');
  assert.equal(H.stoppedSession(sessions, 'work'), null);
  assert.equal(H.newSessionProblem('o2test', sessions), null);
  assert.equal(H.newSessionProblem('work', sessions), 'herdr_session_exists');
  for (const bad of ['', '..', '-x', 'a b', 'x'.repeat(33), '名字']) assert.equal(H.newSessionProblem(bad, sessions), 'invalid_session_name', bad);
});

// ── pushes ────────────────────────────────────────────────────────────────
test('a push lands in Odomo: open=agent on Agent, open=herdr (or an old herd) on that session and pane', () => {
  assert.deepEqual(openTarget('?open=agent&ref=approval'), {panel: 'agent', ids: {ref: 'approval'}});
  const herdr = openTarget('?open=herdr&session=work&pane=w1%3Ap2&agent=codex');
  assert.deepEqual(herdr, {panel: 'herdr', ids: {session: 'work', pane: 'w1:p2', agent: 'codex'}});
  assert.deepEqual(openTarget('?open=herd&session=work&pane=w1%3Ap2').panel, 'herdr');
  assert.deepEqual(H.pushPlace(herdr.ids), {session: 'work', pane: 'w1:p2'});
  assert.deepEqual(H.pushPlace({session: 'work', pane: 'x;y'}), {session: 'work', pane: null});
  assert.equal(H.pushPlace({session: '--takeover'}), null);
  assert.equal(H.pushPlace({pane: 'w1:p1'}), null, 'a pane only with its session');
  assert.equal(A.pushRef({ref: 'approval'}), 'approval');
  assert.equal(A.pushRef({ref: 'other'}), null);
});

test('the bar: the App\'s order and names, less SSH; Settings cannot be switched off', () => {
  assert.deepEqual(PANELS, ['remote', 'agent', 'herdr', 'settings', 'notifications']);
  assert.deepEqual(barPanels(['agent', 'settings']), ['remote', 'herdr', 'settings', 'notifications']);
  assert.equal(en.panels.herdr, 'Herdr');
  assert.equal(zh.panels.herdr, 'Herdr');
  for (const table of [en, zh]) {
    assert.equal(table.panels.herd, undefined);
    assert.equal(table.bar.names.herd, undefined);
    assert.doesNotMatch(JSON.stringify(table), /\bHerd\b/, 'Herdr, never Herd');
  }
});

// ── the agent ─────────────────────────────────────────────────────────────
const ID = {hostID: 'h', agentID: 'default', provider: 'codex', providerSessionID: 'thread-1'};
const SNAP = {identity: ID, rows: [{kind: 'message', id: 'm1', turnID: 't0', role: 'assistant', text: 'hi'}], activeTurn: null,
  sequence: 4, acceptedRequestIDs: [], pendingApprovals: [], status: {type: 'idle'}, usage: null};

test('the agent: a snapshot, then events in sequence; a gap is a reconnect, never invented output', () => {
  let m = A.restore(A.initial(), SNAP);
  assert.equal(m.phase, 'ready');
  m = {...m, draft: 'do it'};
  const begun = A.beginSend(m);
  assert.equal(begun.text, 'do it');
  assert.equal(begun.model.draft, '');
  assert.equal(begun.model.localEcho.text, 'do it');
  m = begun.model;
  m = A.apply(m, {identity: ID, sequence: 5, event: {type: 'turnStarted', turnID: 't1'}});
  assert.equal(m.phase, 'running');
  m = A.apply(m, {identity: ID, sequence: 6, event: {type: 'message', value: {kind: 'message', id: 'u1', turnID: 't1', role: 'user', text: 'do it'}}});
  assert.equal(m.localEcho, null, 'the provider echoed it: one copy');
  m = A.apply(m, {identity: ID, sequence: 7, event: {type: 'agent.approval.requested', request_id: 7, kind: 'commandExecution',
    summary: 'touch x', details: {command: 'touch x', item_id: 'i', cwd: '/home/u'}, decisions: ['accept', 'acceptForSession', 'decline', 'cancel']}});
  const card = m.approvals[0];
  assert.equal(card.requestID, '7');
  assert.deepEqual(card.details.map((d) => d.key), ['command', 'cwd'], 'item_id is the provider\'s, not the reader\'s');
  assert.deepEqual(card.decisions, ['accept', 'acceptForSession', 'decline', 'cancel']);
  assert.equal(A.pendingApprovals(m).length, 1);
  const deciding = A.beginDecision(m, '7', 'accept');
  assert.equal(deciding.approvals[0].state.kind, 'deciding');
  assert.equal(A.beginDecision(deciding, '7', 'decline'), null, 'one answer per prompt');
  m = A.settleDecision(deciding, '7', 'accept', 'client');
  assert.equal(A.pendingApprovals(m).length, 0);
  const gap = A.apply(m, {identity: ID, sequence: 9, event: {type: 'turnFinished', turnID: 't1'}});
  assert.equal(gap.phase, 'reconnecting');
  const foreign = A.apply(m, {identity: {...ID, providerSessionID: 'other'}, sequence: 8, event: {type: 'turnFinished', turnID: 't1'}});
  assert.equal(foreign, m, 'another session\'s event changes nothing');
  m = A.apply(m, {identity: ID, sequence: 8, event: {type: 'turnFinished', turnID: 't1'}});
  assert.equal(m.phase, 'ready');
  // a snapshot after reconnect keeps answered prompts as history
  const again = A.restore(m, {...SNAP, sequence: 8});
  assert.equal(again.approvals[0].state.kind, 'resolved');
});

test('the agent: a failed send puts the words back; an answer is values, not a blank', () => {
  const m = {...A.restore(A.initial(), SNAP), draft: 'words'};
  const begun = A.beginSend(m);
  const failed = A.sendFailed(begun.model, begun.text, {key: 'sendUnconfirmed'});
  assert.equal(failed.draft, 'words');
  assert.equal(failed.localEcho, null);
  assert.equal(agentErrorText(en, failed.error), en.agent.sendUnconfirmed);
  assert.equal(agentErrorText(zh, {code: 'existing_thread_handoff_required'}), '现有的 agent 需要做一次连接交接。');
  assert.deepEqual(A.submission({q1: 'yes', q2: '  '}), {q1: ['yes']});
  assert.equal(A.beginSend({...m, draft: '   '}), null);
  assert.equal(A.safeCode('surprise'), 'agent_chat_unavailable');
});

// ── the client ────────────────────────────────────────────────────────────
function recorder() {
  const calls = [];
  const client = new HostClient({base: 'https://h.example/', token: 't', fetchImpl: async (url, init) => {
    calls.push({url, method: init.method, body: init.body ? JSON.parse(init.body) : undefined});
    return {ok: true, status: 200, json: async () => ({})};
  }});
  return {client, calls};
}

test('the client asks exactly the routes the App does, and nothing reaches a path unchecked', async () => {
  const {client, calls} = recorder();
  await client.herdrKeys('w1:p1', ['Escape']);
  await client.herdrText('w1:p1', 'yes', true);
  await client.herdrPaneAction('w1:p1', 'split', {direction: 'down'});
  await client.herdrNewTab('w1');
  await client.herdrCloseTab('w1', 'w1:t2');
  await client.herdrStartSession('o2test');
  await client.herdrCreateSession('o2new');
  await client.herdrOperatorRows('codex');
  await client.chatSend('r1', 'hi', null, null);
  await client.chatResolve('7', {decision: 'accept'});
  await client.chatResolve('8', {input: {q: ['x']}});
  // AGENT-PLAN-1: a slash command is a message like any other.
  await client.chatSend('r2', '/model gpt', null, null);
  assert.deepEqual(calls.map((c) => c.method + ' ' + new URL(c.url).pathname + new URL(c.url).search), [
    'POST /v1/herdr/panes/w1:p1/keys', 'POST /v1/herdr/panes/w1:p1/text', 'POST /v1/herdr/panes/w1:p1/split',
    'POST /v1/herdr/workspaces/w1/tabs', 'DELETE /v1/herdr/workspaces/w1/tabs/w1:t2', 'POST /v1/herdr/sessions/o2test/start',
    'POST /v1/herdr/sessions', 'GET /v1/herdr/operator-rows?agent=codex', 'POST /v1/agent/default/chat/messages',
    'POST /v1/agent/default/chat/approvals/7', 'POST /v1/agent/default/chat/approvals/8',
    'POST /v1/agent/default/chat/messages']);
  assert.deepEqual(calls[11].body, {request_id: 'r2', text: '/model gpt'});
  assert.equal(typeof client.chatExecute, 'undefined', 'the web client has no command route');
  assert.deepEqual(calls[1].body, {text: 'yes', submit: true});
  assert.deepEqual(calls[3].body, {focus: false}, 'a tab made here does not move the computer');
  assert.deepEqual(calls[9].body, {decision: 'accept'});
  assert.deepEqual(calls[10].body, {input: {q: ['x']}});
  for (const bad of [() => client.herdrKeys('../x', ['a']), () => client.herdrPaneAction('w1:p1', 'focus', {}),
    () => client.herdrCloseTab('w1', 'w2:t1'), () => client.herdrStartSession('-x'), () => client.chatResolve('a/b', {decision: 'x'})]) {
    assert.throws(bad, ApiError);
  }
});

test('a socket opens on a ticket for exactly its path, with the pane\'s own size', async () => {
  const opened = [];
  class Socket {
    constructor(url) { this.url = url; this.readyState = 1; opened.push(this); }
    send() {}
    close() {}
  }
  const client = {ticket: async (path) => ({ticket: 'T'.repeat(43), path})};
  const handle = openSocket({client, base: 'https://h.example/', path: '/v1/herdr/panes/w1:p1/observe',
    query: {cols: 180, rows: 48}, WebSocketImpl: Socket});
  await new Promise((r) => setTimeout(r, 0));
  const url = new URL(opened[0].url);
  assert.equal(url.protocol, 'wss:');
  assert.equal(url.pathname, '/v1/herdr/panes/w1:p1/observe');
  assert.deepEqual(Object.fromEntries(url.searchParams), {cols: '180', rows: '48', ticket: 'T'.repeat(43)});
  handle.close();
});

// ── the mirror ────────────────────────────────────────────────────────────
test('the mirror: Herdr\'s frames into a real terminal, full repaints, increments, the cursor', async () => {
  const mirror = new Mirror({cols: 20, rows: 3});
  const enc = (text) => new TextEncoder().encode(text);
  const frame = (text, full, extra = {}) => H.decodeMessage(JSON.stringify({type: 'terminal.frame', full,
    bytes: Buffer.from(text).toString('base64'), ...extra}));
  assert.equal(mirror.feed(frame('dropped', false)), false, 'an increment before the first full frame has nothing to apply to');
  await new Promise((done) => mirror.feed(frame('codex\r\n\x1b[31m> \x1b[0mprompt', true, {width: 30, height: 4}), done));
  assert.equal(mirror.cols, 30);
  assert.deepEqual(mirror.text().slice(0, 2), ['codex', '> prompt']);
  await new Promise((done) => mirror.feed(frame('\x1b[1;1Hnvim \x1b[?25l', false), done));
  assert.equal(mirror.text()[0], 'nvim ');
  assert.equal(mirror.cursor().hidden, true);
  await new Promise((done) => mirror.feed({full: true, width: 30, height: 4, bytes: enc('fresh')}, done));
  assert.deepEqual(mirror.text(), ['fresh', '', '', ''], 'full means from scratch');
  assert.equal(H.decodeMessage('{"type":"terminal.frame","bytes":"!!!"}'), null);
  assert.equal(H.decodeMessage('{"type":"terminal.frame","encoding":"other","bytes":""}'), null);
  assert.deepEqual(H.decodeMessage('{"type":"terminal.closed","reason":"pane_gone"}'), {type: 'closed', reason: 'pane_gone'});
  assert.deepEqual(H.decodeMessage('{"type":"later.thing"}'), {type: 'other'});
  mirror.dispose();
});

test('fit to width is a scale, never a resize; 7 pt is the floor; 1:1 is actual size', () => {
  assert.equal(scaleFor({mode: 'actual'}, 180, 7.8, 390), 1);
  assert.equal(scaleFor({mode: 'fit'}, 180, 7.8, 390), MIN_SCALE, 'a 180-column pane on a phone stops at 7 pt');
  assert.ok(Math.abs(scaleFor({mode: 'fit'}, 80, 7.8, 624) - 1) < 1e-9);
  assert.equal(scaleFor({mode: 'custom', scale: 10}, 80, 7.8, 624), 3);
  const palette = paletteOf({colors: {background: '#101010', foreground: '#eeeeee', red: '#ff0000', accent: '#00ff00'}});
  assert.equal(palette.background, '#101010');
  assert.equal(palette.ansi[1], '#ff0000');
  assert.equal(palette.cursor, '#00ff00');
  assert.equal(paletteOf({colors: {background: 'red; x'}}).background, '#1a1b26', 'only #rrggbb gets through');
  assert.equal(color256(196, palette), '#ff0000');
  assert.equal(color256(232, palette), '#080808');
});

test('every ODOMO-2 string is in both languages, and a phone keyboard never zooms the page', () => {
  const keys = (o, p = '') => Object.entries(o).flatMap(([k, v]) => (v && typeof v === 'object' ? keys(v, p + k + '.') : [p + k]));
  for (const section of ['herdr', 'agent', 'reasons']) assert.deepEqual(keys(en[section]).sort(), keys(zh[section]).sort(), section);
  assert.equal((en.herdr.sessionShape.match(/%[123]/g) || []).length, 3);
});
