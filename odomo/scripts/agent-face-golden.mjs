// MOTION-1: golden frames for the animated Omodachi face, exported from the
// brand study itself (omodachi-brand `agent-motion/study.html` at the pinned
// commit), so iOS `AgentFace` and Odomo `agentFace.js` are both checked against
// the page Leo approved rather than against each other.
//
//   node odomo/scripts/agent-face-golden.mjs <omodachi-brand repo> <out.json>
//
// It reads study.html out of git at BRAND_COMMIT (never the working tree),
// runs the study's own model code (everything before its DOM clock) in a
// sandbox, and writes one record per case: the surface, the state, the time,
// Reduce Motion, the size, the frame the study resolved (pose, head offset,
// eye colour, dim) and the rendered cell grid as text rows:
//   . outside the head   H head band   S shade step   o face hole
//   E eye                R ring (background-coloured groove)   T ring track
// Draw order is the study's `draw()`: head, shade, track, ring, eyes.
import {execFileSync} from 'node:child_process';
import {writeFileSync} from 'node:fs';
import vm from 'node:vm';

export const BRAND_COMMIT = '8318df51dc07d32c371bcca078cea5c9438ef6ed';
const [brand, out] = process.argv.slice(2);
if (!brand || !out) {
  console.error('usage: agent-face-golden.mjs <omodachi-brand repo> <out.json>');
  process.exit(2);
}
const html = execFileSync('git', ['-C', brand, 'show', `${BRAND_COMMIT}:agent-motion/study.html`], {encoding: 'utf8'});
const tokens = execFileSync('git', ['-C', brand, 'show', `${BRAND_COMMIT}:agent-motion/tokens.json`], {encoding: 'utf8'});
const start = html.indexOf('<script>') + '<script>'.length;
const end = html.indexOf('/* ---------- clock ----------');
const source = html.slice(start, end) + `
;globalThis.__study = {TOKENS, HEADCELLS, project, sysFrame, SURF, LOOP, POSES, MODELS, base, O, draw};`;
const sandbox = {Math, JSON, Object, Array, Set, Map, Number, String, console};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const S = sandbox.__study;
if (JSON.stringify(S.TOKENS) !== JSON.stringify(JSON.parse(tokens))) throw new Error('study TOKENS differ from tokens.json');

function grid(f, r, g, shadeOn) {
  const n = 88 / g;
  const rows = Array.from({length: n}, () => Array(n).fill('.'));
  for (const [x, y] of S.HEADCELLS[g]) rows[y][x] = 'H';
  for (const [x, y] of r.face) rows[y][x] = 'o';
  if (shadeOn) for (const [x, y] of r.shade) rows[y][x] = 'S';
  for (const [x, y] of r.track || []) rows[y][x] = 'T';
  for (const [x, y] of r.ring || []) rows[y][x] = 'R';
  for (const [x, y] of r.eyes) if (y >= 0 && y < n && x >= 0 && x < n) rows[y][x] = 'E';
  return rows.map((row) => row.join(''));
}
const frameOf = (f) => ({hx: f.hx, hy: f.hy, yaw: f.yaw, pitch: f.pitch, roll: f.roll, dim: !!f.dim,
  eyeC: f.eyeC, eyes: f.eyes, ring: f.ring || null});

const cases = [];
// 1. The pose table (round 4 model), both grids, with the shade layer.
for (const p of S.POSES) for (const g of [4, 2]) {
  const f = S.base();
  Object.assign(f, {yaw: p[2] * S.TOKENS.turn.full_deg, pitch: p[3] * S.TOKENS.turn.full_deg, roll: p[4],
    eyes: S.O(1, {tilt: p[5]})});
  const r = S.project(f, g, S.MODELS.r4);
  cases.push({kind: 'pose', id: `pose-${p[0]}-g${g}`, g, shade: true, frame: frameOf(f), grid: grid(f, r, g, true)});
}
// 2. The system resolver: every surface the spec names, every state.
const SURFACES = ['bar', 'pane', 'tiles', 'header', 'large', 'odomo'];
const STATES = ['idle', 'thinking', 'working', 'waiting', 'done', 'error', 'stopped', 'offline'];
const TIMES = [0, 125, 500, 1000, 1750];
for (const surface of SURFACES) {
  const sizes = S.SURF[surface].sizes;
  for (const state of STATES) for (const size of sizes) {
    const code = S.SURF[surface].states[state] || '—';
    const runs = TIMES.map((t) => [t, false]).concat([[0, true]]);
    for (const [t0, rm] of runs) {
      const t = Math.floor(t0 / (1000 / 12)) * (1000 / 12);
      const id = `${surface}-${state}-${size}-${rm ? 'rm' : 't' + t0}`;
      if (code === '—' || (code.startsWith('V') && code !== 'V6')) {
        cases.push({kind: 'surface', id, surface, state, size, t, rm, code, grid: null});
        continue;
      }
      const {f, shade, g} = S.sysFrame(surface, state, t, rm, size);
      const r = S.project(f, g);
      cases.push({kind: 'surface', id, surface, state, size, t, rm, code, g, shade, frame: frameOf(f),
        grid: grid(f, r, g, shade)});
    }
  }
}
writeFileSync(out, JSON.stringify({
  note: 'MOTION-1 golden frames, exported from omodachi-brand agent-motion/study.html by odomo/scripts/agent-face-golden.mjs. Do not edit by hand.',
  brand_commit: BRAND_COMMIT,
  legend: {'.': 'outside', H: 'head band', S: 'shade', o: 'face hole', E: 'eye', R: 'ring groove', T: 'ring track'},
  cases,
}) + '\n');
console.log(`wrote ${cases.length} cases to ${out}`);
