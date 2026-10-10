// MOTION-1: the animated Omodachi face - the brand study's model, ported once.
//
// Source: omodachi-brand `agent-motion/study.html` at 8318df5 (Leo approved
// the design 2026-10-04): the whole light o is a spherical head, the dark
// hole and the eyes are the face painted on it; every grid cell is ray-cast
// onto that sphere, rotated back into the face's rest frame and tested there,
// then snapped to whole cells. The formulas below are the study's, line for
// line; every number is read from `agent-motion-tokens.json`, the verbatim
// copy of the brand's tokens.json (scripts/check_motion_tokens.py keeps it so).
// tests/motion1.test.mjs checks this module against 444 golden frames
// exported from the study itself (scripts/agent-face-golden.mjs).
//
// Pure: no DOM, no React. `AgentFace.jsx` draws what `faceFrame` returns.
import TOKENS from './agent-motion-tokens.json' with {type: 'json'};

export {TOKENS};
export const FPS = TOKENS.motion.fps;
export const FRAME = 1000 / FPS;

// ---------- geometry: the v6 glyph "o", local coords 0..88 ----------
export const HEAD_D = TOKENS.head.outline;

function polys(d) {
  const out = [];
  let cur = null;
  let x = 0;
  let y = 0;
  const tk = d.match(/[MHVZ]|-?\d+(\.\d+)?/g);
  for (let i = 0; i < tk.length; i++) {
    const c = tk[i];
    if (c === 'M') {
      x = +tk[++i];
      y = +tk[++i];
      cur = [[x, y]];
      out.push(cur);
    } else if (c === 'H') {
      x = +tk[++i];
      cur.push([x, y]);
    } else if (c === 'V') {
      y = +tk[++i];
      cur.push([x, y]);
    }
  }
  return out;
}

function inside(p, pt) {
  let r = false;
  for (let i = 0, j = p.length - 1; i < p.length; j = i++) {
    const [xi, yi] = p[i];
    const [xj, yj] = p[j];
    if (((yi > pt[1]) !== (yj > pt[1])) && (pt[0] < (xj - xi) * (pt[1] - yi) / (yj - yi) + xi)) r = !r;
  }
  return r;
}

const [HEAD] = polys(HEAD_D);
const headCells = (g) => {
  const n = 88 / g;
  const out = [];
  for (let gy = 0; gy < n; gy++) for (let gx = 0; gx < n; gx++) if (inside(HEAD, [gx * g + g / 2, gy * g + g / 2])) out.push([gx, gy]);
  return out;
};
export const HEADCELLS = {4: headCells(4), 2: headCells(2)};

// ---------- the procedural model (round 4: R = the head's own radius) ----------
const R = TOKENS.head.sphere_radius_u;
const C0 = TOKENS.head.center_u[0];
const M = {
  faceR: TOKENS.face.radius_u, faceN: TOKENS.face.exponent,
  eyeX: TOKENS.eyes.center_x_u, eyeY: TOKENS.eyes.center_y_u, eyeW: TOKENS.eyes.width_u, eyeH: TOKENS.eyes.height_u,
  TURN: TOKENS.turn.full_deg,
};
const rad = (d) => d * Math.PI / 180;
const N8 = [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [1, -1], [-1, 1], [-1, -1]];

// cell centre (px,py) in head units -> face-local (u,v), or null off the visible face sphere
function toFace(px, py, yaw, pitch, roll) {
  const X = (px - C0) / R;
  const Y = (py - C0) / R;
  const r2 = X * X + Y * Y;
  if (r2 >= 1) return null;
  const Z = Math.sqrt(1 - r2);
  const cy = Math.cos(rad(yaw));
  const sy = Math.sin(rad(yaw));
  const cp = Math.cos(rad(pitch));
  const sp = Math.sin(rad(pitch));
  const x = X * cy - Z * sy;
  const z1 = X * sy + Z * cy;
  const y = Y * cp - z1 * sp;
  const z = Y * sp + z1 * cp;
  if (z <= 0) return null;
  const cr = Math.cos(rad(roll));
  const sr = Math.sin(rad(roll));
  return [(x * cr + y * sr) * R, (-x * sr + y * cr) * R];
}
const inFace = (u, v) => Math.abs(u / M.faceR) ** M.faceN + Math.abs(v / M.faceR) ** M.faceN <= 1;

function rotFwd(x, y, z, yaw, pitch, roll) {
  const cr = Math.cos(rad(roll));
  const sr = Math.sin(rad(roll));
  [x, y] = [x * cr - y * sr, x * sr + y * cr];
  const cp = Math.cos(rad(pitch));
  const sp = Math.sin(rad(pitch));
  [y, z] = [y * cp + z * sp, -y * sp + z * cp];
  const cy = Math.cos(rad(yaw));
  const sy = Math.sin(rad(yaw));
  [x, z] = [x * cy + z * sy, -x * sy + z * cy];
  return [x, y, z];
}

function eyeFrames(p) {
  const out = [];
  for (const s of [-1, 1]) {
    const u = (s * M.eyeX + p.gaze[0]) / R;
    const v = (M.eyeY + p.gaze[1]) / R;
    const z0 = Math.sqrt(Math.max(0, 1 - u * u - v * v));
    const [x, y, z] = rotFwd(u, v, z0, p.yaw, p.pitch, p.roll);
    if (z <= 0.05) {
      out.push(null);
      continue;
    }
    const lon0 = Math.atan2(u, z0);
    const lat0 = Math.asin(v);
    const lon = Math.atan2(x, z);
    const lat = Math.asin(Math.max(-1, Math.min(1, y)));
    const sx = Math.max(0.5, Math.cos(lon) / Math.cos(lon0));
    const sy = Math.max(0.5, Math.cos(lat) / Math.cos(lat0));
    out.push({s, cx: C0 + x * R, cy: C0 + y * R, sx, sy, roll: p.roll});
  }
  return out;
}

// One pose at grid g (units per cell): face, eyes, shade and ring cells.
export function project(p, g) {
  const face = [];
  const F = new Set();
  const ef = eyeFrames(p);
  for (const [gx, gy] of HEADCELLS[g]) {
    const q = toFace(gx * g + g / 2, gy * g + g / 2, p.yaw, p.pitch, p.roll);
    if (q && inFace(q[0], q[1])) {
      face.push([gx, gy]);
      F.add(gx + ',' + gy);
    }
  }
  // Volume: band cells more than ~90° from the face's direction get one flat
  // shade step (drawn only where the surface allows it).
  const fd = rotFwd(0, 0, 1, p.yaw, p.pitch, p.roll);
  let shade = [];
  for (const [gx, gy] of HEADCELLS[g]) {
    if (F.has(gx + ',' + gy)) continue;
    const X = (gx * g + g / 2 - C0) / R;
    const Y = (gy * g + g / 2 - C0) / R;
    const r2 = X * X + Y * Y;
    let P;
    if (r2 < 1) P = [X, Y, Math.sqrt(1 - r2)];
    else {
      const l = Math.sqrt(r2);
      P = [X / l, Y / l, 0];
    }
    if (P[0] * fd[0] + P[1] * fd[1] + P[2] * fd[2] < TOKENS.shade.threshold_dot) shade.push([gx, gy]);
  }
  {
    const S = new Set(shade.map((c) => c.join(',')));
    shade = shade.filter(([x, y]) => N8.filter(([dx, dy]) => S.has((x + dx) + ',' + (y + dy))).length
      >= TOKENS.shade.cleanup_min_neighbours);
  }
  // Face ring (C): an annulus in the face's own coordinates, on the band, at
  // least one band cell away from the face.
  const ring = [];
  const track = [];
  if (p.ring) {
    const RT = TOKENS.ring;
    const n = M.faceN;
    const rg = p.ring;
    const rin = rg.rin ?? RT.inner_u;
    const rout = rg.rout ?? RT.outer_u;
    const near = (x, y) => N8.some(([dx, dy]) => F.has((x + dx) + ',' + (y + dy)));
    for (const [gx, gy] of HEADCELLS[g]) {
      if (F.has(gx + ',' + gy) || near(gx, gy)) continue;
      const q = toFace(gx * g + g / 2, gy * g + g / 2, p.yaw, p.pitch, p.roll);
      if (!q) continue;
      const rho = (Math.abs(q[0]) ** n + Math.abs(q[1]) ** n) ** (1 / n);
      if (rho < rin || rho >= rout) continue;
      const a = ((Math.atan2(q[0], -q[1]) * 180 / Math.PI) + 360) % 360;
      if (rg.arcs) {
        if (rg.arcs.some(([st, sp]) => ((a - st) % 360 + 360) % 360 < sp)) ring.push([gx, gy]);
      } else if (rg.kind === 'progress') {
        const seg = 360 / RT.variants.progress.segments;
        if ((a % seg) < seg - RT.variants.progress.gap_deg) {
          if (a < rg.progress * 360) ring.push([gx, gy]);
          else track.push([gx, gy]);
        }
      } else ring.push([gx, gy]);
    }
  }
  // Eyes: the projected frame, snapped to whole cells, with the keep-out rule
  // (face on all eight sides, one cell between the eyes).
  const k = 4 / g;
  const ok = ([x, y]) => F.has(x + ',' + y) && N8.every(([dx, dy]) => F.has((x + dx) + ',' + (y + dy)));
  const e = p.eyes || {kind: 'none'};
  const boxes = ef.map((fr) => {
    if (!fr || e.kind === 'none') return null;
    const wC = Math.max(1, Math.round(M.eyeW * fr.sx / g));
    const hC = Math.max(1, Math.round(M.eyeH * fr.sy / g));
    const x0 = Math.round((fr.cx - wC * g / 2) / g);
    const y0 = Math.round((fr.cy - hC * g / 2) / g);
    return {s: fr.s, x0, y0, wC, hC, sx: fr.sx, tilt: (e.tilt === fr.s) ? k : 0};
  });
  const G = TOKENS.eyes.glyphs;
  const cellsOf = (b, dx, dy) => {
    const out = [];
    const X = b.x0 + dx;
    const Y = b.y0 + dy + b.tilt;
    const put = (x, y) => {
      for (let j = 0; j < k; j++) for (let i = 0; i < k; i++) out.push([X + x * k + i, Y + y * k + j]);
    };
    switch (e.kind) {
      case 'open':
      case 'pair': {
        const o = e.kind === 'pair' ? (b.s < 0 ? e.l : e.r) : (e.open ?? 1);
        const h = Math.max(1, Math.round(b.hC * o));
        for (let j = b.hC - h; j < b.hC; j++) for (let i = 0; i < b.wC; i++) out.push([X + i, Y + j]);
        break;
      }
      case 'closed':
        for (let i = 0; i < b.wC; i++) out.push([X + i, Y + b.hC - 1]);
        break;
      case 'happy': {
        const cx = Math.round((b.wC / k - 3) / 2 + b.s * 0.5);
        for (const [x, y] of G.happy.cells) put(cx + x, y);
        break;
      }
      case 'cross': {
        const narrow = b.sx < G.cross.narrow_below_sx;
        const cx = narrow ? Math.round((b.wC / k - 2) / 2) : Math.round((b.wC / k - 3) / 2 + b.s * 0.5);
        const pat = !narrow ? G.cross.cells : b.s > 0 ? G.cross.narrow_right : G.cross.narrow_left;
        for (const [x, y] of pat) put(cx + x, Math.round(b.hC / k) - 3 + y);
        break;
      }
      default:
    }
    return out;
  };
  const fx = face.reduce((a, c) => a + c[0], 0) / face.length;
  const fy = face.reduce((a, c) => a + c[1], 0) / face.length;
  const eyes = [];
  const placed = [];
  const touches = (c) => c.filter(([x, y]) => placed.some(([px, py]) => Math.abs(px - x) <= 1 && Math.abs(py - y) <= 1)).length;
  for (const b of boxes.filter(Boolean).sort((a, c) => c.sx - a.sx)) {
    const cx = b.x0 + b.wC / 2;
    const cy = b.y0 + b.hC / 2;
    const sx = Math.sign(Math.round(fx - cx));
    const sy = Math.sign(Math.round(fy - cy));
    const shifts = Math.abs(p.yaw) >= Math.abs(p.pitch)
      ? [[sx, 0], [0, sy], [sx, sy], [2 * sx, 0], [0, 2 * sy]]
      : [[0, sy], [sx, 0], [sx, sy], [0, 2 * sy], [2 * sx, 0]];
    const trims = [];
    if (e.kind === 'open' || e.kind === 'pair') {
      const outer = b.s < 0 ? {x0: b.x0 + 1, wC: b.wC - 1} : {wC: b.wC - 1};
      const inner = b.s < 0 ? {wC: b.wC - 1} : {x0: b.x0 + 1, wC: b.wC - 1};
      for (const t of [outer, {y0: b.y0 + 1, hC: b.hC - 1}, {hC: b.hC - 1}, inner]) {
        const bb = Object.assign({}, b, t);
        if (bb.wC >= 1 && bb.hC >= 1 && bb.wC * bb.hC >= 2) trims.push(bb);
      }
    }
    const tries = [[b, 0, 0], ...trims.map((t) => [t, 0, 0]), ...shifts.map(([dx, dy]) => [b, dx, dy]),
      ...trims.flatMap((t) => shifts.map(([dx, dy]) => [t, dx, dy]))];
    let best = null;
    for (const [bb, dx, dy] of tries) {
      const c = cellsOf(bb, dx, dy);
      const bad = c.filter((q) => !ok(q)).length + touches(c);
      if (!best || bad < best.bad) best = {c, bad};
      if (!bad) break;
    }
    const keep = best.c.filter((q) => ok(q) && !touches([q]));
    eyes.push(...keep);
    placed.push(...keep);
  }
  return {face, eyes, shade, ring, track};
}

// ---------- motion: targets per state, critically damped, stateless ----------
const OMEGA = TOKENS.motion.spring.omega;
const g1 = (tau) => (tau <= 0 ? 0 : 1 - (1 + OMEGA * tau) * Math.exp(-OMEGA * tau));
function track(t, loop, segs, smooth = true) {
  const l = ((t % loop) + loop) % loop;
  if (!smooth) {
    let v = segs[0][1];
    for (const [s, val] of segs) if (l >= s) v = val;
    return v;
  }
  let v = segs[segs.length - 1][1];
  for (let k = 0; k < segs.length; k++) {
    const prev = k ? segs[k - 1][1] : segs[segs.length - 1][1];
    v += (segs[k][1] - prev) * g1((l - segs[k][0]) / 1000);
  }
  return v;
}
const T = M.TURN;
const BLINK = TOKENS.motion.blink;
const BL = (t, period, phase = 0) => {
  const l = ((t + phase) % period + period) % period;
  if (l >= BLINK.duration_ms) return 1;
  const p = l / BLINK.duration_ms;
  return Math.max(BLINK.min_open, p < BLINK.close_fraction ? 1 - p / BLINK.close_fraction
    : (p - BLINK.close_fraction) / (1 - BLINK.close_fraction));
};
const ST = TOKENS.states;
const dbl = (t, period) => Math.min(BL(t, period), BL(t, period, -ST.waiting.double_blink_gap_ms));
const win = (t, period, a, b) => {
  const l = t % period;
  return l >= a && l < b;
};
const shakeYaw = (t, period, len) => (win(t, period, 0, len)
  ? ((Math.floor((t % period) / FRAME) % 2) ? ST.error.shake.yaw * T : -ST.error.shake.yaw * T) : 0);
const O = (open = 1, extra = {}) => Object.assign({kind: 'open', open}, extra);
export const LOOP = {idle: ST.idle.loop_ms, thinking: ST.thinking.loop_ms, working: ST.working.loop_ms,
  waiting: ST.waiting.loop_ms, done: ST.done.loop_ms, error: ST.error.loop_ms, stopped: 3000, offline: 3000};
const scale = (segs) => segs.map(([at, v]) => [at, v * T]);

export const base = () => ({hx: 0, hy: 0, yaw: 0, pitch: 0, roll: 0, gaze: [0, 0], eyes: O(), eyeC: 'fg', headC: 'fg', dim: false});
const idleF = (f, t, rm) => {
  if (rm) return;
  f.eyes = O(BL(t, ST.idle.blink_every_ms, -3000));
  f.yaw = track(t, ST.idle.loop_ms, [[0, 0], ...scale(ST.idle.glance)]);
};

// Direction A (the face turns): the base everywhere.
export function frameA(s, t, rm) {
  const f = base();
  switch (s) {
    case 'idle': idleF(f, t, rm); break;
    case 'thinking': {
      f.pitch = track(t, ST.thinking.loop_ms, scale(ST.thinking.pitch));
      if (rm) {
        f.yaw = ST.thinking.static_pose[0] * T;
        f.pitch = ST.thinking.static_pose[1] * T;
        break;
      }
      f.yaw = track(t, ST.thinking.loop_ms, scale(ST.thinking.yaw));
      f.eyes = O(BL(t, ST.thinking.loop_ms, -ST.thinking.blink_at_ms));
      break;
    }
    case 'working': {
      f.pitch = ST.working.pitch * T;
      if (rm) break;
      f.yaw = track(t, ST.working.loop_ms, scale(ST.working.yaw));
      f.pitch = ST.working.pitch * T * (1 - 0.25 * Math.abs(f.yaw) / T);
      f.eyes = O(BL(t, ST.working.blink_every_ms, -1800));
      break;
    }
    case 'waiting':
      f.pitch = ST.waiting.pose[1] * T;
      f.eyeC = 'accent';
      if (!rm) f.eyes = O(dbl(t, ST.waiting.loop_ms));
      break;
    case 'done':
      f.eyes = {kind: 'happy'};
      if (!rm) f.pitch = track(t, ST.done.loop_ms, scale(ST.done.nod));
      break;
    case 'error':
      f.eyes = {kind: 'cross'};
      f.eyeC = 'red';
      if (!rm) f.yaw = shakeYaw(t, ST.error.loop_ms, 500);
      break;
    case 'stopped':
      // INTEGRATE-2: interrupted by the user - offline's closed eyes, upright,
      // full opacity, foreground eyes; static on every surface (A·).
      f.eyes = {kind: ST.stopped.eyes};
      break;
    case 'offline':
      f.pitch = ST.offline.pose[1] * T;
      f.eyes = {kind: 'closed'};
      f.dim = true;
      break;
    default:
  }
  return f;
}

const HOP = TOKENS.motion.hop;
// Direction B (the head hops): only its head offset is ever used on top of A.
export function hopB(s, t) {
  switch (s) {
    case 'working': return {hx: 0, hy: (t % HOP.working_period_ms) < HOP.duration_ms ? -HOP.height_u : 0};
    case 'waiting': {
      const l = t % HOP.waiting_period_ms;
      return {hx: 0, hy: (l < 167 || (l >= 333 && l < 500)) ? -HOP.height_u : 0};
    }
    case 'done': return {hx: 0, hy: win(t, 3000, 0, 250) ? -HOP.height_u : 0};
    case 'error': return {hx: win(t, 3000, 0, 500) ? ((Math.floor((t % 3000) / FRAME) % 2) ? 4 : -4) : 0, hy: 0};
    case 'offline': return {hx: 0, hy: 4};
    default: return {hx: 0, hy: 0};
  }
}

// C2 sides: two arcs left and right of the face, sweeping within ±sweep of the horizontal.
export function ringSides(t, rm) {
  const o = TOKENS.ring.variants.sides;
  const n = Math.round(2 * o.sweep_deg / o.step_deg) + 1;
  const off = rm ? 0 : -o.sweep_deg + (Math.floor(t / o.step_ms) % n) * o.step_deg;
  return {kind: 'sides', arcs: [[90 + off - o.span_deg / 2, o.span_deg], [270 + off - o.span_deg / 2, o.span_deg]]};
}

// ---------- the usage matrix (study §11.1; the codes are the study's) ----------
export const SURFACES = {
  bar: {grid: '4', shade: false,
    states: {idle: 'V', thinking: 'A', working: 'A+C', waiting: 'A+B1', done: 'A+B1', error: 'A·', stopped: 'A·', offline: 'V·'}},
  pane: {grid: '4', shade: false,
    states: {idle: 'A·', thinking: '—', working: 'A·', waiting: 'A', done: 'A·', error: '—', stopped: '—', offline: 'A·'}},
  tiles: {grid: '4', shade: false,
    states: {idle: 'A·', thinking: '—', working: 'A', waiting: 'A+B1', done: 'A+B1', error: '—', stopped: '—', offline: 'A·'}},
  header: {grid: '4', shade: true,
    states: {idle: 'A', thinking: 'A', working: 'A+C', waiting: 'A+B1', done: 'A+B1', error: 'A', stopped: 'A·', offline: 'A·'}},
  large: {grid: '4 · ≥96 px 用 2', shade: true,
    states: {idle: 'A', thinking: 'A', working: 'A+B+C', waiting: 'A+B', done: 'A+B1', error: 'A', stopped: 'A·', offline: 'A·'}},
  odomo: {grid: '4', shade: false, staticBelow: 24,
    states: {idle: 'V', thinking: 'A', working: 'A+C', waiting: 'A+B1', done: 'A+B1', error: 'A·', stopped: 'A·', offline: 'V·'}},
};

export const codeOf = (surface, state) => SURFACES[surface].states[state] || '—';
export const isVendor = (code) => code.startsWith('V') && code !== 'V6';

// The study's resolver: one surface + one state + time -> a frame and how to draw it.
// `tool` = a tool / command is running (the C ring shows only then; the study's
// sheets always show it for working). `staticBelow` is passed explicitly so a
// high-density screen can animate the 22 px bar face (MOTION-1: @1x only).
export function sysFrame(surface, state, t, rm, size, {tool = true, staticBelow} = {}) {
  const S = SURFACES[surface];
  const code = codeOf(surface, state);
  const below = staticBelow === undefined ? S.staticBelow : staticBelow;
  const stat = rm || code.includes('·') || (below && size < below);
  const f = frameA(state, t, stat);
  if (!stat && code.includes('B')) {
    if (code.includes('B1')) {
      if (state === 'done') f.hy = win(t, LOOP.done, 0, HOP.duration_ms) ? -HOP.height_u : 0;
      if (state === 'waiting') {
        const l = t % LOOP.waiting;
        f.hy = (l < 167 || (l >= 333 && l < 500)) ? -HOP.height_u : 0;
      }
    } else if (size >= HOP.loop_min_px) {
      const b = hopB(state, t);
      f.hx = b.hx;
      f.hy = b.hy;
    }
  }
  if (tool && code.includes('C') && state === 'working' && size >= TOKENS.ring.min_px) f.ring = ringSides(t, stat);
  return {f, code, static: !!stat, shade: S.shade && size >= TOKENS.shade.min_px,
    g: (S.grid.includes('2') && size >= TOKENS.canvas.fine_grid_min_px) ? 2 : 4};
}

// ---------- state mapping (what core already says; nothing invented) ----------
export const PRIORITY = TOKENS.priority;
const WAITING = new Set(['waiting_on_approval', 'waiting_on_user_input']);

// The default agent, from `state.agent` (core's `agent.changed`). `seenTurn`
// matters only on the bar: there a done / error / stopped face stays until the Agent
// panel has shown that turn.
export function agentFaceState({connected = true, chat = null, agentStatus = null, seenTurn = null, surface = 'header'} = {}) {
  const activity = chat && chat.activity && typeof chat.activity === 'object' ? chat.activity : null;
  const phase = activity ? activity.phase : null;
  const candidates = [];
  if (!connected || phase === 'offline') candidates.push('offline');
  const unseen = surface !== 'bar' && surface !== 'odomo' ? true : !activity || activity.turn_id !== seenTurn;
  if (phase === 'error' && unseen) candidates.push('error');
  if (phase === 'stopped' && unseen) candidates.push('stopped');
  if ((chat && chat.status && chat.status.waiting) || WAITING.has(agentStatus)
    || (chat && Number.isInteger(chat.pending_approvals) && chat.pending_approvals > 0)) candidates.push('waiting');
  if (phase === 'done' && unseen) candidates.push('done');
  if (phase === 'working' || phase === 'tool') candidates.push('working');
  if (phase === 'thinking') candidates.push('thinking');
  candidates.push('idle');
  const state = PRIORITY.find((s) => candidates.includes(s));
  return {state, tool: state === 'working' && phase === 'tool', turn: activity ? activity.turn_id : null};
}

// The state in words (`t.face.states`), for labels: the face is never the only signal.
export function faceWord(t, face) {
  if (!face || !face.state) return t.face.states.idle;
  return face.tool && face.state === 'working' ? t.face.states.tool : t.face.states[face.state];
}

// A Herdr pane: Herdr's own word; it has no thinking / tool distinction.
export function paneFaceState(pane) {
  if (!pane || !pane.agent) return null;
  const status = pane.agent.status || pane.agent_status;
  return {idle: 'idle', working: 'working', blocked: 'waiting', done: 'done'}[status] || 'idle';
}

// ---------- the waiting eye colour (study (b)): accent, unless accent is the foreground ----------
const lum = (h) => {
  const c = [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
    .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
};
export const contrast = (a, b) => {
  const x = lum(a);
  const y = lum(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
};
const oklab = (h) => {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
    .map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s, 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s];
};
export const deltaE = (a, b) => {
  const x = oklab(a);
  const y = oklab(b);
  return Math.hypot(x[0] - y[0], x[1] - y[1], x[2] - y[2]);
};
const HEX = /^#[0-9a-fA-F]{6}$/;
// `palette` = {fg, accent, yellow, bg} as #rrggbb. 'yellow' only for kanagawa-like themes.
export function waitingEyeRole(palette) {
  const W = TOKENS.colors.waiting_eyes;
  if (!palette || ![palette.fg, palette.accent, palette.yellow, palette.bg].every((v) => HEX.test(v || ''))) return 'accent';
  return deltaE(palette.accent, palette.fg) < W.close_threshold_oklab_de
    && contrast(palette.yellow, palette.bg) >= W.min_contrast_vs_background ? 'yellow' : 'accent';
}

// ---------- what a face does over time ----------
// The pose blend on a state change: from the last drawn yaw / pitch to the new
// state's track, by the same critically damped closed form (~250 ms).
export function blend(target, from, target0, tauMs) {
  if (!from) return target;
  const tau = tauMs / 1000;
  const k = (1 + OMEGA * tau) * Math.exp(-OMEGA * tau);
  return {...target, yaw: target.yaw + (from.yaw - target0.yaw) * k, pitch: target.pitch + (from.pitch - target0.pitch) * k};
}

// Does this face need a clock at all? Static codes, vendor marks, Reduce
// Motion and small @1x faces render once.
export function animates({surface, state, size, reduced, dpr = 1}) {
  const code = codeOf(surface, state);
  if (reduced || code === '—' || isVendor(code) || code.includes('·')) return false;
  const below = SURFACES[surface].staticBelow;
  if (below && size < below && dpr < 1.5) return false;
  return true;
}

// Which faces may move when more than `max` want to: waiting first, then
// working with a ring, then the rest, earliest registered first.
export function rankAnimating(entries, max = TOKENS.motion.max_animating_instances) {
  const rank = (e) => (e.state === 'waiting' ? 0 : e.state === 'working' && e.tool ? 1 : 2);
  return new Set(entries.slice().sort((a, b) => rank(a) - rank(b) || a.order - b.order).slice(0, max).map((e) => e.id));
}

// The frame interval: 12 fps, 6 fps after ten minutes in one state.
export const frameInterval = (elapsedMs) => 1000 / (elapsedMs >= TOKENS.motion.low_fps_after_ms ? TOKENS.motion.low_fps : FPS);

// The composite the golden frames are written in ('.' H S o T R E).
export function gridText(r, g, shadeOn) {
  const n = 88 / g;
  const rows = Array.from({length: n}, () => Array(n).fill('.'));
  for (const [x, y] of HEADCELLS[g]) rows[y][x] = 'H';
  for (const [x, y] of r.face) rows[y][x] = 'o';
  if (shadeOn) for (const [x, y] of r.shade) rows[y][x] = 'S';
  for (const [x, y] of r.track || []) rows[y][x] = 'T';
  for (const [x, y] of r.ring || []) rows[y][x] = 'R';
  for (const [x, y] of r.eyes) if (y >= 0 && y < n && x >= 0 && x < n) rows[y][x] = 'E';
  return rows.map((row) => row.join(''));
}

// SVG path data for a set of cells.
export const cellsPath = (cells, g) => cells.map(([x, y]) => `M${x * g} ${y * g}h${g}v${g}h-${g}z`).join('');
