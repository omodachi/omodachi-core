// MOTION-1: the animated face. The port is checked against golden frames the
// brand study exported itself (scripts/agent-face-golden.mjs, brand 8318df5),
// then the state mapping and the gating rules are checked as plain functions.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import * as F from '../src/lib/agentFace.js';

const here = new URL('..', import.meta.url).pathname;
const golden = JSON.parse(readFileSync(join(here, 'tests/fixtures/agent-face-golden.json'), 'utf8'));

const close = (a, b, what) => assert.ok(Math.abs(a - b) <= 1e-9, `${what}: ${a} vs ${b}`);

function sameFrame(actual, expected, id) {
  for (const key of ['hx', 'hy']) assert.equal(actual[key], expected[key], `${id} ${key}`);
  for (const key of ['yaw', 'pitch', 'roll']) close(actual[key], expected[key], `${id} ${key}`);
  assert.equal(!!actual.dim, expected.dim, `${id} dim`);
  assert.equal(actual.eyeC, expected.eyeC, `${id} eyeC`);
  assert.equal(actual.eyes.kind, expected.eyes.kind, `${id} eyes`);
  if ('open' in expected.eyes) close(actual.eyes.open, expected.eyes.open, `${id} open`);
  assert.deepEqual(actual.ring || null, expected.ring, `${id} ring`);
}

test('the pinned golden file is the brand study at 8318df5', () => {
  assert.equal(golden.brand_commit, '8318df51dc07d32c371bcca078cea5c9438ef6ed');
  assert.equal(golden.cases.length, 504);
});

test('every pose of the study renders cell for cell (both grids, with the shade)', () => {
  const poses = golden.cases.filter((c) => c.kind === 'pose');
  assert.equal(poses.length, 24);
  for (const c of poses) {
    const f = F.base();
    Object.assign(f, {yaw: c.frame.yaw, pitch: c.frame.pitch, roll: c.frame.roll, eyes: c.frame.eyes});
    assert.deepEqual(F.gridText(F.project(f, c.g), c.g, true), c.grid, c.id);
  }
});

test('every surface × state × time × Reduce Motion case matches the study frame and grid', () => {
  const cases = golden.cases.filter((c) => c.kind === 'surface');
  assert.equal(cases.length, 480);
  let drawn = 0;
  for (const c of cases) {
    const code = F.codeOf(c.surface, c.state);
    assert.equal(code, c.code, c.id + ' code');
    if (c.grid === null) {
      assert.ok(code === '—' || F.isVendor(code), c.id);
      continue;
    }
    const {f, shade, g} = F.sysFrame(c.surface, c.state, c.t, c.rm, c.size);
    assert.equal(g, c.g, c.id + ' grid');
    assert.equal(shade, c.shade, c.id + ' shade');
    sameFrame(f, c.frame, c.id);
    assert.deepEqual(F.gridText(F.project(f, g), g, shade), c.grid, c.id);
    drawn++;
  }
  assert.ok(drawn > 300, `${drawn} drawn`);
});

test('at rest the face is the v6 counter, and the ring never touches the face', () => {
  // v6: rows 6..15 of the counter are 6/8/10×6/8/6 cells wide, eyes 2×3 cells.
  const rows = F.gridText(F.project(F.base(), 4), 4, false);
  assert.deepEqual(rows.slice(6, 16).map((row) => (row.match(/[oE]/g) || []).length), [6, 8, 10, 10, 10, 10, 10, 10, 8, 6]);
  assert.equal(rows.join('').split('E').length - 1, 12);
  for (let t = 0; t < 2400; t += F.FRAME) {
    const {f} = F.sysFrame('header', 'working', Math.floor(t), false, 32);
    const p = F.project(f, 4);
    const face = new Set(p.face.map((c) => c.join(',')));
    for (const [x, y] of p.ring) {
      for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) assert.ok(!face.has((x + dx) + ',' + (y + dy)));
    }
  }
});

test('the default agent: core\'s activity, waiting, offline, priority', () => {
  const chat = (phase, extra = {}) => ({status: {type: 'active', activeFlags: [], waiting: null}, pending_approvals: 0,
    activity: {phase, turn_id: 'turn-1'}, ...extra});
  assert.deepEqual(F.agentFaceState({chat: null}), {state: 'idle', tool: false, turn: null});
  assert.equal(F.agentFaceState({chat: chat('idle')}).state, 'idle');
  assert.equal(F.agentFaceState({chat: chat('thinking')}).state, 'thinking');
  assert.deepEqual(F.agentFaceState({chat: chat('working')}), {state: 'working', tool: false, turn: 'turn-1'});
  assert.deepEqual(F.agentFaceState({chat: chat('tool')}), {state: 'working', tool: true, turn: 'turn-1'});
  assert.equal(F.agentFaceState({chat: chat('done')}).state, 'done');
  assert.equal(F.agentFaceState({chat: chat('error')}).state, 'error');
  assert.equal(F.agentFaceState({chat: chat('offline')}).state, 'offline');
  assert.equal(F.agentFaceState({connected: false, chat: chat('tool')}).state, 'offline');
  // Waiting: the provider's flag, core's status word, or a pending approval.
  assert.equal(F.agentFaceState({chat: chat('tool', {status: {waiting: 'approval'}})}).state, 'waiting');
  assert.equal(F.agentFaceState({chat: chat('thinking'), agentStatus: 'waiting_on_user_input'}).state, 'waiting');
  assert.equal(F.agentFaceState({chat: chat('thinking', {pending_approvals: 1})}).state, 'waiting');
  // offline > error > waiting > done.
  assert.equal(F.agentFaceState({chat: chat('error', {pending_approvals: 1})}).state, 'error');
  assert.equal(F.agentFaceState({chat: chat('done', {pending_approvals: 1})}).state, 'waiting');
  // The bar keeps done / error only until the panel has shown that turn.
  assert.equal(F.agentFaceState({chat: chat('done'), surface: 'odomo', seenTurn: 'turn-0'}).state, 'done');
  assert.equal(F.agentFaceState({chat: chat('done'), surface: 'odomo', seenTurn: 'turn-1'}).state, 'idle');
  assert.equal(F.agentFaceState({chat: chat('error'), surface: 'odomo', seenTurn: 'turn-1'}).state, 'idle');
  assert.equal(F.agentFaceState({chat: chat('done'), surface: 'header', seenTurn: 'turn-1'}).state, 'done');
});

test('a Herdr pane: Herdr\'s own word, no face without an agent', () => {
  assert.equal(F.paneFaceState({id: 'w1:p1'}), null);
  assert.equal(F.paneFaceState({agent: {kind: 'codex', status: 'working'}}), 'working');
  assert.equal(F.paneFaceState({agent: {kind: 'codex', status: 'blocked'}}), 'waiting');
  assert.equal(F.paneFaceState({agent: {kind: 'codex'}, agent_status: 'done'}), 'done');
  assert.equal(F.paneFaceState({agent: {kind: 'codex', status: 'idle'}}), 'idle');
  assert.equal(F.paneFaceState({agent: {kind: 'codex', status: 'unknown'}}), 'idle');
});

test('the matrix: vendor mark on the bar when idle / offline, the ring only while a tool runs', () => {
  assert.ok(F.isVendor(F.codeOf('odomo', 'idle')));
  assert.ok(F.isVendor(F.codeOf('odomo', 'offline')));
  assert.ok(!F.isVendor(F.codeOf('odomo', 'thinking')));
  assert.equal(F.sysFrame('header', 'working', 0, false, 32, {tool: false}).f.ring, undefined);
  assert.ok(F.sysFrame('header', 'working', 0, false, 32, {tool: true}).f.ring);
  // Reduce Motion: frozen ring, no hop.
  const rm = F.sysFrame('large', 'working', 700, true, 96);
  assert.equal(rm.f.hy, 0);
  assert.deepEqual(rm.f.ring.arcs, [[70, 40], [250, 40]]);
  // Shade only on the header and large art, fine grid only on large ≥ 96.
  assert.equal(F.sysFrame('tiles', 'working', 0, false, 32).shade, false);
  assert.equal(F.sysFrame('header', 'idle', 0, false, 32).shade, true);
  assert.equal(F.sysFrame('large', 'idle', 0, false, 96).g, 2);
  assert.equal(F.sysFrame('large', 'idle', 0, false, 64).g, 4);
});

test('gating: what runs a clock, the three-instance cap, the low frame rate', () => {
  assert.equal(F.animates({surface: 'odomo', state: 'idle', size: 22}), false);
  assert.equal(F.animates({surface: 'odomo', state: 'thinking', size: 22, dpr: 1}), false, '@1x below 24: static');
  assert.equal(F.animates({surface: 'odomo', state: 'thinking', size: 22, dpr: 2}), true);
  assert.equal(F.animates({surface: 'odomo', state: 'error', size: 22, dpr: 2}), false, 'A· is a static pose');
  assert.equal(F.animates({surface: 'pane', state: 'working', size: 24}), false);
  assert.equal(F.animates({surface: 'pane', state: 'waiting', size: 24}), true);
  assert.equal(F.animates({surface: 'header', state: 'thinking', size: 32, reduced: true}), false);
  const keep = F.rankAnimating([
    {id: 'a', state: 'thinking', order: 0}, {id: 'b', state: 'working', tool: false, order: 1},
    {id: 'c', state: 'working', tool: true, order: 2}, {id: 'd', state: 'waiting', order: 3},
    {id: 'e', state: 'waiting', order: 4}]);
  assert.deepEqual([...keep].sort(), ['c', 'd', 'e']);
  assert.equal(F.frameInterval(0), 1000 / 12);
  assert.equal(F.frameInterval(600000), 1000 / 6);
});

test('the pose blend leaves the old pose quickly and settles within half a second', () => {
  const from = {yaw: 20, pitch: 10};
  const target = {yaw: 0, pitch: 0};
  assert.deepEqual(F.blend(target, null, target, 0), target);
  const start = F.blend(target, from, target, 0);
  close(start.yaw, 20, 'starts where it was');
  const later = F.blend(target, from, target, 500);
  assert.ok(Math.abs(later.yaw) < 0.1 && Math.abs(later.pitch) < 0.1, JSON.stringify(later));
});

test('waiting eyes: accent, yellow only where accent is the foreground (kanagawa)', () => {
  assert.equal(F.waitingEyeRole({fg: '#dcd7ba', accent: '#dcd7ba', yellow: '#c0a36e', bg: '#1f1f28'}), 'yellow');
  assert.equal(F.waitingEyeRole({fg: '#a9b1d6', accent: '#7aa2f7', yellow: '#e0af68', bg: '#1a1b26'}), 'accent');
  assert.equal(F.waitingEyeRole({fg: '#575279', accent: '#56949f', yellow: '#ea9d34', bg: '#faf4ed'}), 'accent');
  assert.equal(F.waitingEyeRole(null), 'accent');
});

// INTEGRATE-2 (Leo): a turn the user interrupted is `stopped` - a calm face,
// not the red error. Real failures keep the error.
test('an interrupted turn is stopped: closed eyes, full opacity, never red, never moving', async () => {
  const chat = (phase) => ({status: {type: 'idle', activeFlags: [], waiting: null}, pending_approvals: 0,
    activity: {phase, turn_id: 'turn-9'}});
  assert.equal(F.agentFaceState({chat: chat('stopped')}).state, 'stopped');
  assert.equal(F.agentFaceState({chat: chat('error')}).state, 'error', 'a failure is still an error');
  // The bar keeps it until the panel has shown that turn, like done / error.
  assert.equal(F.agentFaceState({chat: chat('stopped'), surface: 'odomo', seenTurn: 'turn-8'}).state, 'stopped');
  assert.equal(F.agentFaceState({chat: chat('stopped'), surface: 'odomo', seenTurn: 'turn-9'}).state, 'idle');
  for (const [surface, size] of [['odomo', 22], ['odomo', 24], ['bar', 24], ['header', 32], ['large', 96]]) {
    for (const t of [0, 250, 500, 1000, 1750, 2900]) {
      const {f, code} = F.sysFrame(surface, 'stopped', t, false, size);
      assert.equal(code, 'A·', `${surface} is a static pose`);
      assert.equal(f.eyes.kind, 'closed', 'offline\'s eye bars');
      assert.equal(f.eyeC, 'fg', 'never red');
      assert.equal(f.dim, false, 'full opacity');
      assert.deepEqual([f.hx, f.hy, f.yaw, f.pitch, f.roll], [0, 0, 0, 0, 0], 'no hop, no shake, no nod');
      assert.equal(f.ring, undefined);
    }
    assert.equal(F.animates({surface, state: 'stopped', size, dpr: 2}), false, 'no clock at all');
  }
  assert.equal(F.codeOf('pane', 'stopped'), '—', 'Herdr panes have no interrupt');
  const {en, zh} = await import('../src/lib/i18n.js');
  assert.equal(F.faceWord(en, {state: 'stopped'}), 'stopped');
  assert.equal(F.faceWord(zh, {state: 'stopped'}), '已停止');
  assert.notEqual(F.faceWord(en, {state: 'stopped'}), F.faceWord(en, {state: 'error'}));
});
