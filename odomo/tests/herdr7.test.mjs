// HERDR-7: Herdr shows which pane runs an agent with a still marker - the
// bar's own Agent glyph in the App's status colour - and says its state in
// words. The animated face stays in the Agent panel and on the bar (Leo:
// "herdr 没必要为了 agent 加 agent 动画啦").
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runnerImport} from 'vite';
import * as H from '../src/lib/herdr.js';
import {en, zh} from '../src/lib/i18n.js';

const {module: render} = await runnerImport(new URL('./render.jsx', import.meta.url).pathname,
  {root: new URL('..', import.meta.url).pathname, configFile: false, logLevel: 'silent'});

const pane = (id, agent, extra = {}) => ({id, tab_id: 'w1:t1', workspace_id: 'w1', title: id, cwd: '~/code/uptime',
  focused: false, zoomed: false, agent_status: agent ? agent.status : 'unknown', agent, ...extra});
const layout = {session: 'omodachi', workspaces: [{id: 'w1', label: 'uptime', number: 1, focused: true, tabs: [
  {id: 'w1:t1', label: '1', number: 1, focused: true, zoomed: false, panes: [
    pane('w1:p1', {kind: 'codex', status: 'blocked'}),
    pane('w1:p2', null),
    pane('w1:p3', {kind: 'claude', status: 'working'}),
    pane('w1:p4', {kind: 'codex', status: 'done'}),
    pane('w1:p5', {kind: 'pi', status: 'idle'}),
  ]}]}]};

test('the marker\'s status: Herdr\'s word the App colours, idle otherwise, none without an agent', () => {
  assert.equal(H.paneStatus({id: 'w1:p1'}), null);
  assert.equal(H.paneStatus(null), null);
  assert.equal(H.paneStatus({agent: {kind: 'codex', status: 'working'}}), 'working');
  assert.equal(H.paneStatus({agent: {kind: 'codex', status: 'blocked'}}), 'blocked');
  assert.equal(H.paneStatus({agent: {kind: 'codex'}, agent_status: 'done'}), 'done');
  assert.equal(H.paneStatus({agent: {kind: 'codex', status: 'idle'}}), 'idle');
  assert.equal(H.paneStatus({agent: {kind: 'codex', status: 'unknown'}}), 'idle');
});

for (const wide of [true, false]) {
  test('Herdr ' + (wide ? 'rows' : 'chips') + ': a still marker per agent pane, no face, the state in words', () => {
    for (const [lang, table] of [['en', en], ['zh-Hans', zh]]) {
      const html = render.renderHerdrPanes(layout, wide, lang);
      assert.doesNotMatch(html, /class="aface"|data-surface=|agent-face-/, 'no animated face in Herdr');
      const markers = [...html.matchAll(/data-testid="herdr-marker-([^"]+)" data-status="([^"]+)"/g)].map((m) => [m[1], m[2]]);
      assert.deepEqual(markers, [['w1:p1', 'blocked'], ['w1:p3', 'working'], ['w1:p4', 'done'], ['w1:p5', 'idle']]);
      assert.match(html, /class="hmark hmark--blocked" aria-hidden="true"/);
      // The shell pane keeps the dot.
      assert.equal((html.match(/class="hdot /g) || []).length, 1);
      // The words a screen reader hears: waiting, working, done, idle.
      assert.ok(html.includes(table.herdr.waiting), lang);
      for (const state of ['working', 'done', 'idle']) assert.ok(html.includes(', ' + table.face.states[state] + '"'), lang + ' ' + state);
    }
  });
}

test('the marker is the bar\'s Agent glyph, and its colours are the App\'s status colours', () => {
  const html = render.renderHerdrPanes(layout, true);
  const sidebar = render.renderSidebar({});
  const robot = (s) => (s.match(/<path d="[^"]*"/g) || []);
  const marker = html.slice(html.indexOf('herdr-marker-w1:p1'));
  const glyph = robot(marker)[0];
  assert.ok(glyph && robot(sidebar).includes(glyph), 'the same path the bar draws for Agent');
  const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');
  assert.match(css, /\.hmark \{[^}]*color: var\(--om-dim\)/);
  assert.match(css, /\.hmark--working \{color: var\(--om-accent\)\}/);
  assert.match(css, /\.hmark--blocked \{color: var\(--om-red\)\}/);
  assert.match(css, /\.hmark--done \{color: var\(--om-green\)\}/);
  const source = readFileSync(new URL('../src/panels/HerdrPanel.jsx', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /AgentFace/, 'the Herdr panel does not draw the face');
});
