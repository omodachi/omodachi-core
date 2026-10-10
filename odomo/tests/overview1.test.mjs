// OVERVIEW-1: Odomo's half of the one "Needs you" list - grouping in core's
// order, what each row says in both languages, the counts the bar, the session
// menu and the chips read, and the event that moves it - against core's own
// fixture (`contracts/fixtures/state-overview1.json`).
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {en, zh} from '../src/lib/i18n.js';
import {applyEvent} from '../src/lib/state.js';
import * as O from '../src/lib/overview.js';

const state = JSON.parse(readFileSync(new URL('../../contracts/fixtures/state-overview1.json', import.meta.url)));
const overview = state.overview;
const panel = readFileSync(new URL('../src/panels/HerdrPanel.jsx', import.meta.url), 'utf8');
const bar = readFileSync(new URL('../src/Sidebar.jsx', import.meta.url), 'utf8');

test('the list is grouped by state in core\'s order, never re-sorted', () => {
  const groups = O.groups(overview);
  assert.deepEqual(groups.map((g) => g.state), ['needs_you', 'working', 'done']);
  assert.deepEqual(groups[0].rows.map((r) => r.id), ['agent', 'herdr:work:w1:p1', 'herdr:work:w2:p1']);
  assert.deepEqual(groups[1].rows.map((r) => r.id), ['herdr:work:w1:p2']);
  assert.deepEqual(groups[2].rows.map((r) => r.id), ['herdr:omodachi:w1:p3']);
  assert.deepEqual(O.groups({rows: [], counts: {}, sessions: []}), []);
  assert.equal(O.overviewOf({}), null);
  assert.equal(O.overviewOf(state), overview);
});

test('every ask in both languages, with and without its question', () => {
  const cases = [
    [{ask: 'approve_command', state: 'needs_you'}, 'Approve a command', '批准一条命令'],
    [{ask: 'approve_files', state: 'needs_you'}, 'Approve file changes', '批准文件改动'],
    [{ask: 'approve_permissions', state: 'needs_you'}, 'Approve more access', '批准更多权限'],
    [{ask: 'approve', state: 'needs_you'}, 'Approve a request', '批准一个请求'],
    [{ask: 'answer', state: 'needs_you'}, 'Answer a question', '回答一个问题'],
    [{ask: 'pick', state: 'needs_you', question: 'Proceed?'}, 'Pick an option: Proceed?', '选一个：Proceed?'],
    [{ask: 'pick', state: 'needs_you', question: null}, 'Pick an option', '选一个选项'],
    [{ask: 'yes_no', state: 'needs_you', question: 'Overwrite?'}, 'Yes or no: Overwrite?', '是或否：Overwrite?'],
    [{ask: 'yes_no', state: 'needs_you'}, 'Yes or no', '回答是或否'],
    [{ask: 'waiting', state: 'needs_you'}, 'Waiting for you', '在等你'],
    [{ask: 'working', state: 'working'}, 'Working on it', '正在做'],
    [{ask: 'your_move', state: 'done', question: '要提交吗？'}, 'Your move: 要提交吗？', '该你了：要提交吗？'],
    [{ask: 'your_move', state: 'done', question: ''}, 'Finished', '做完了'],
    [{ask: 'finished', state: 'done'}, 'Finished', '做完了'],
    [{ask: 'failed', state: 'done'}, 'Stopped on an error', '出错停下了'],
    [{ask: 'something_new', state: 'needs_you'}, 'Waiting for you', '在等你'],
    [{ask: 'something_new', state: 'working'}, 'Working on it', '正在做'],
    [{ask: 'something_new', state: 'done'}, 'Finished', '做完了'],
  ];
  for (const [row, english, chinese] of cases) {
    assert.equal(O.askText(en, row), english, JSON.stringify(row));
    assert.equal(O.askText(zh, row), chinese, JSON.stringify(row));
  }
  assert.equal(O.askText(en, overview.rows[2]), 'Pick an option: Would you like to run the following command?');
});

test('where a row is: the agent, then Agent or session · workspace · pane', () => {
  assert.equal(O.whereText(overview.rows[0], en), 'codex · Agent');
  assert.equal(O.whereText(overview.rows[2], en), 'codex · work · api');           // pane "api" = workspace "api"
  assert.equal(O.whereText(overview.rows[1], en), 'codex · omodachi · ~ · notes');
  assert.equal(O.whereText({source: 'herdr', agent: null, session: 's', workspace: null, pane: 'p'}, en), 's · p');
});

test('counts: non-zero only, needs you first, and per session', () => {
  assert.equal(O.countsText(en, overview.counts), 'Needs you 3 · Done 1 · Working 1');
  assert.equal(O.countsText(zh, overview.counts), '等你 3 · 做完 1 · 在做 1');
  assert.equal(O.countsText(en, {needs_you: 0, working: 2, done: 0}), 'Working 2');
  assert.equal(O.countsText(en, {needs_you: 0, working: 0, done: 0}), '');
  assert.deepEqual(O.sessionCounts(overview, 'work'), {needs_you: 2, working: 1, done: 0});
  assert.deepEqual(O.sessionCounts(overview, 'nope'), {needs_you: 0, working: 0, done: 0});
  assert.equal(O.herdrNeedsYou(overview), 2, 'the Agent is not counted on Herdr');
  assert.equal(O.herdrNeedsYou(null), 0);
});

test('a workspace chip shows its most urgent state; a finished pane its square', () => {
  assert.equal(O.workspaceWorst(overview, 'work', 'w1'), 'needs_you');
  assert.equal(O.workspaceWorst(overview, 'omodachi', 'w1'), 'done');
  assert.equal(O.workspaceWorst(overview, 'work', 'w9'), null);
  const working = {rows: [{id: 'x', state: 'working', session: 's', workspace_id: 'w1', pane_id: 'w1:p1'}]};
  assert.equal(O.workspaceWorst(working, 's', 'w1'), 'working');
  assert.equal(O.paneRow(overview, 'omodachi', 'w1:p3').state, 'done');
  assert.equal(O.paneRow(overview, 'omodachi', 'w1:p1'), null);
});

test('overview.changed replaces the list, it does not merge rows', () => {
  const next = applyEvent(state, {type: 'overview.changed', payload: {revision: 99,
    overview: {rows: [overview.rows[1]], counts: {needs_you: 0, working: 0, done: 1}, sessions: []}}});
  assert.deepEqual(next.overview.rows.map((r) => r.id), ['herdr:omodachi:w1:p3']);
  assert.deepEqual(next.overview.sessions, []);
  assert.equal(next.revision, 99);
  const unseen = applyEvent(state, {type: 'unseen.changed', payload: {unseen: {agent: true, herdr: []}}});
  assert.equal(unseen.unseen.agent, true);
});

test('both languages carry every string the list uses', () => {
  for (const table of [en, zh]) {
    for (const key of ['title', 'open', 'openHint', 'empty', 'close', 'agent', 'needsYouBadge']) assert.ok(table.overview[key], key);
    for (const state of O.STATES) {
      assert.ok(table.overview.sections[state]);
      assert.ok(table.overview.count[state].includes('%s'));
      assert.ok(table.overview.chip[state]);
    }
  }
});

test('the panel and the bar are wired to the list', () => {
  assert.match(panel, /data-testid="herdr-overview-open"/);
  assert.match(panel, /data-testid="herdr-overview"/);
  assert.match(panel, /store\.land\(\{session: row\.session, pane: row\.pane_id\}\)/);
  assert.match(bar, /count=\{id === 'herdr' \? waiting : 0\}/);
});

import {runnerImport} from 'vite';
const {module: render} = await runnerImport(new URL('./render.jsx', import.meta.url).pathname,
  {root: new URL('..', import.meta.url).pathname, configFile: false, logLevel: 'silent'});

test('the card draws its sections in order, each row its ask and where', () => {
  for (const [lang, table] of [['en', en], ['zh-Hans', zh]]) {
    const html = render.renderOverview(overview, lang);
    assert.match(html, /data-testid="herdr-overview"/);
    const at = (id) => html.indexOf('data-testid="overview-row-' + id + '"');
    assert.ok(at('agent') > 0 && at('agent') < at('herdr:work:w1:p1') && at('herdr:work:w2:p1') < at('herdr:work:w1:p2')
      && at('herdr:work:w1:p2') < at('herdr:omodachi:w1:p3'));
    for (const state of O.STATES) assert.ok(html.includes(table.overview.sections[state]), state);
    assert.ok(html.includes(O.askText(table, overview.rows[1])));
  }
  const empty = render.renderOverview({rows: [], counts: {needs_you: 0, working: 0, done: 0}, sessions: []});
  assert.match(empty, /Nothing needs you right now\./);
});

test('chips carry a dot slot always; it is coloured only where something is', () => {
  const layout = {session: 'work', workspaces: [
    {id: 'w1', label: 'api', number: 1, tabs: [{id: 'w1:t1', panes: [{id: 'w1:p1', title: 'api', agent_status: 'blocked',
      agent: {kind: 'codex', status: 'blocked'}}]}]},
    {id: 'w3', label: 'docs', number: 3, tabs: [{id: 'w3:t1', panes: [{id: 'w3:p1', title: 'docs', agent_status: 'unknown'}]}]}]};
  for (const wide of [true, false]) {
    const html = render.renderHerdrPanes(layout, wide, 'en', overview);
    assert.match(html, /data-testid="herdr-workspace-dot-w1" data-state="needs_you"/);
    assert.ok(!html.includes('herdr-workspace-dot-w3'), 'no state, no dot - but the slot is there');
    assert.equal((html.match(/class="hsdot/g) || []).length >= 2, true);
  }
  const done = {session: 'omodachi', workspaces: [{id: 'w1', label: '~', number: 1, tabs: [{id: 'w1:t1', panes: [
    {id: 'w1:p3', title: 'notes', agent_status: 'idle', agent: {kind: 'codex', status: 'idle'}}]}]}]};
  assert.match(render.renderHerdrPanes(done, false, 'en', overview), /data-testid="herdr-unseen-w1:p3"/);
  assert.ok(!render.renderHerdrPanes(done, false, 'en', undefined).includes('hsdot'), 'no list, nothing drawn');
});

test('the bar counts what waits on you in Herdr, and says it', () => {
  const html = render.renderSidebar({state});
  assert.match(html, /data-testid="bar-herdr-count"[^>]*>2</);
  assert.ok(html.includes('2 waiting for you'));
  assert.ok(!render.renderSidebar({state: {...state, overview: {...overview, sessions: []}}}).includes('bar-herdr-count'));
});
