import React, {useEffect, useRef, useState} from 'react';
import * as F from './lib/agentFace.js';

// MOTION-1: the animated Omodachi face (brand agent-motion 8318df5), drawn as
// inline SVG cells. The model is `lib/agentFace.js`; this decides only *when*
// to draw: at 12 fps (6 after ten minutes in one state), and only while the
// face is on screen, the page is visible, the face's surface and state call
// for motion, Reduce Motion is off, and it is one of the three faces allowed
// to move at once (waiting > working with a ring > the rest). Anything else
// is the state's static pose, drawn once, with no timer at all.

// ---- the three-at-once registry, shared by every face on the page ----
const wanting = new Map();
const listeners = new Set();
let allowed = new Set();
let order = 0;
let nextId = 0;

function publish() {
  allowed = F.rankAnimating([...wanting].map(([id, entry]) => ({id, ...entry})));
  for (const listener of listeners) listener();
}

function useAllowed(id, entry) {
  const [, rerender] = useState(0);
  useEffect(() => {
    const listener = () => rerender((n) => n + 1);
    listeners.add(listener);
    return () => listeners.delete(listener);
  }, []);
  const key = entry ? entry.state + '|' + (entry.tool ? 1 : 0) : '';
  useEffect(() => {
    if (!entry) return undefined;
    wanting.set(id, {...entry, order: order++});
    publish();
    return () => {
      wanting.delete(id);
      publish();
    };
  }, [id, key]); // eslint-disable-line react-hooks/exhaustive-deps
  return !!entry && allowed.has(id);
}

// Exposed for tests and the e2e walk: how many faces have a clock right now.
export function animatingCount() {
  return allowed.size;
}

function useReducedMotion() {
  const query = () => {
    try {
      return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)');
    } catch {
      return null;
    }
  };
  const [reduced, setReduced] = useState(() => {
    const q = query();
    return !!(q && q.matches);
  });
  useEffect(() => {
    const q = query();
    if (!q || !q.addEventListener) return undefined;
    const change = () => setReduced(q.matches);
    q.addEventListener('change', change);
    return () => q.removeEventListener('change', change);
  }, []);
  return reduced;
}

function usePageVisible() {
  const [visible, setVisible] = useState(() => typeof document === 'undefined' || document.visibilityState !== 'hidden');
  useEffect(() => {
    const change = () => setVisible(document.visibilityState !== 'hidden');
    document.addEventListener('visibilitychange', change);
    return () => document.removeEventListener('visibilitychange', change);
  }, []);
  return visible;
}

function useOnScreen(ref) {
  const [on, setOn] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver !== 'function') {
      setOn(true);
      return undefined;
    }
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) setOn(entry.isIntersecting);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return on;
}

// The host theme's colours, as #rrggbb, for the kanagawa rule.
function palette() {
  try {
    const style = getComputedStyle(document.documentElement);
    const read = (name) => style.getPropertyValue(name).trim();
    return {fg: read('--om-fg'), accent: read('--om-accent'), yellow: read('--om-yellow'), bg: read('--om-bg')};
  } catch {
    return null;
  }
}

const EYE = {fg: 'currentColor', red: 'var(--om-red)'};

export default function AgentFace({surface, state, tool = false, size, label, testid = 'agent-face'}) {
  const ref = useRef(null);
  const [id] = useState(() => 'face-' + nextId++);
  const reduced = useReducedMotion();
  const pageVisible = usePageVisible();
  const onScreen = useOnScreen(ref);
  const dpr = typeof window !== 'undefined' && window.devicePixelRatio ? window.devicePixelRatio : 1;
  const wants = F.animates({surface, state, size, reduced, dpr}) && onScreen && pageVisible;
  const running = useAllowed(id, wants ? {state, tool} : null);

  // When the state was entered, and the pose it was left in.
  const entered = useRef({state, tool, at: 0, from: null});
  const last = useRef(null);
  const [now, setNow] = useState(0);
  const clock = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());
  if (entered.current.state !== state) {
    entered.current = {state, tool, at: clock(), from: last.current};
  }

  useEffect(() => {
    if (!running) return undefined;
    let timer = 0;
    const tick = () => {
      const at = clock();
      setNow(at);
      timer = setTimeout(tick, F.frameInterval(at - entered.current.at));
    };
    tick();
    return () => clearTimeout(timer);
  }, [running]);

  const t = running ? Math.max(0, Math.floor((now - entered.current.at) / F.FRAME) * F.FRAME) : 0;
  const rm = !running;
  const code = F.codeOf(surface, state);
  const resolved = F.sysFrame(surface, state, t, rm, size,
    {tool, staticBelow: dpr >= 1.5 ? 0 : undefined});
  let f = resolved.f;
  // B¹: the hop on entering done / waiting plays once, not every loop.
  if (code.includes('B1') && t >= F.LOOP[state]) f = {...f, hy: 0};
  if (running && entered.current.from && t < 600) {
    const target0 = F.sysFrame(surface, state, 0, false, size, {tool, staticBelow: dpr >= 1.5 ? 0 : undefined}).f;
    f = F.blend(f, entered.current.from, target0, now - entered.current.at);
  }
  last.current = {yaw: f.yaw, pitch: f.pitch};

  const g = resolved.g;
  const cells = F.project(f, g);
  const ringSet = new Set(cells.ring.map((c) => c.join(',')));
  const holes = F.cellsPath(cells.face.concat(cells.ring), g);
  const shade = resolved.shade ? cells.shade.filter((c) => !ringSet.has(c.join(','))) : [];
  const eyeColour = f.eyeC === 'accent'
    ? (F.waitingEyeRole(palette()) === 'yellow' ? 'var(--om-yellow)' : 'var(--om-accent)')
    : EYE[f.eyeC] || 'currentColor';
  const labelled = label ? {role: 'img', 'aria-label': label} : {'aria-hidden': true};
  return (
    <span ref={ref} className="aface" style={{width: size + 'px', height: size + 'px'}} data-testid={testid}
      data-surface={surface} data-state={state} data-tool={tool ? 'true' : 'false'}
      data-animating={running ? 'true' : 'false'} {...labelled}>
      <svg viewBox="0 0 96 96" width={size} height={size} shapeRendering="crispEdges" aria-hidden="true" focusable="false">
        <g transform={`translate(${4 + f.hx} ${4 + f.hy})`} opacity={f.dim ? TOKENS_DIM : 1}>
          <path fillRule="evenodd" d={F.HEAD_D + ' ' + holes} fill="currentColor" />
          {shade.length ? (
            <path d={F.cellsPath(shade, g)} style={{fill: SHADE_FILL}} />
          ) : null}
          {cells.eyes.length ? <path d={F.cellsPath(cells.eyes, g)} fill={eyeColour} /> : null}
        </g>
      </svg>
    </span>
  );
}

const TOKENS_DIM = F.TOKENS.states.offline.opacity;
// One flat step: the head colour mixed with the background (tokens.shade).
const SHADE_FILL = `color-mix(in srgb, currentColor ${Math.round((1 - F.TOKENS.shade.mix_with_bg) * 100)}%, var(--om-bg))`;
