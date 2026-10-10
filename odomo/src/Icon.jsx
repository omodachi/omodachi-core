// Provenance: copied from omodachi-web `src/app/Icon.jsx` at main 0da28c9 (same author,
// MIT, like this repository). Kept in step by hand; the site's own build, which
// makes no requests and stores nothing, stays separate and untouched.
//
import React from 'react';

// The glyph set of omodachi-brand/design-system/ui-study-04.html, copied path
// for path. An 18 view box, butt caps, miter joins, 1.5 stroke, one colour,
// zero radius. The host's own Nerd Font code points are deliberately not used:
// there is no web font behind them, so they would render as tofu.
export const PATHS = {
  remote: 'M1 2h16v11H1z M6.5 16h5M9 13v3',
  agent: 'M1 2h16v10H5l-4 3v-3H1z M5 6h8M5 9h5',
  herdr: 'M1 2h16v14H1z M7 2v14M7 9h10',
  ssh: 'M1 2h16v14H1z M4 6l3 3-3 3M9.5 12H14',
  browser: 'M1 3h16v12H1z M1 7h16M3.5 5h1M6 5h1',
  apps: 'M2 2h5v5H2zM11 2h5v5h-5zM2 11h5v5H2zM11 11h5v5h-5z',
  book: 'M2 2h6l1 1v13l-1-1H2zM16 2h-6l-1 1v13l1-1h6z',
  bolt: 'M10.5 1L3 10h5l-1.5 7L15 8H9.5z',
  style: 'M9 1l7 8-7 8-7-8z M9 6l3 3-3 3-3-3z',
  setup: 'M2 5h14M2 13h14 M6 2v6M12 10v6',
  install: 'M9 1v10M5 7.5l4 4 4-4M2 15.5h14',
  remove: 'M2 4.5h14M4 4.5V16h10V4.5M7 2h4v2.5H7zM7 8v5M11 8v5',
  update: 'M16 9a7 7 0 1 1-2.6-5.4 M16 1v4h-4',
  about: 'M9 1l8 8-8 8-8-8z M9 5.5v1M9 8.5v4',
  power: 'M9 1.5v7 M4.4 4.4a6.5 6.5 0 1 0 9.2 0',
  search: 'M2 2h10v10H2z M12 12l4.5 4.5',
  right: 'M6.5 3L12 9l-5.5 6',
  left: 'M11.5 3L6 9l5.5 6',
  down: 'M3 6l6 6 6-6',
  up: 'M3 12l6-6 6 6',
  keyboard: 'M1 4h16v10H1z M4 7.5h1M7 7.5h1M10 7.5h1M13 7.5h1M5 11h8',
  pin: 'M9 1.5v6.5M5 8h8l-1.5 3.5h-5zM9 11.5V17',
  send: 'M2 9h13M10 4l5 5-5 5',
  close: 'M3.5 3.5l11 11M14.5 3.5l-11 11',
  check: 'M2.5 9l4.5 4.5L15.5 4',
  dot: 'M3 3h12v12H3z',
  bell: 'M4 8a5 5 0 0 1 10 0v4l2 2H2l2-2z M7 16h3.5',
  bellOff: 'M4 8a5 5 0 0 1 10 0v4l2 2H2l2-2z M7 16h3.5 M2 2l14 14',
  stop: 'M3.5 3.5h11v11h-11z',
  rotate: 'M3 9a6 6 0 1 1 6 6 M3 5v4h4 M6.5 11.5l2.5 2.5',
  touch: 'M9 5.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7z M9 1v2.5M9 14.5V17M1 9h2.5M14.5 9H17',
  vol: 'M2 6.5h3l4-4v13l-4-4H2z M12 5.5a6 6 0 0 1 0 7',
  monitor: 'M1 2h16v11H1z M5.5 16h7M9 13v3',
  reload: 'M16 9a7 7 0 1 1-2.6-5.4 M16 1v4h-4',
  lock: 'M3.5 8h11v8h-11z M6 8V5a3 3 0 0 1 6 0v3',
  moon: 'M14.5 11.8A6.6 6.6 0 0 1 6.2 3.5a6.6 6.6 0 1 0 8.3 8.3z',
  warn: 'M9 1.5L17 16H1z M9 7v4M9 13.2v.6',
  trackpad: 'M1 3h16v12H1z M1 11h16',
  folder: 'M1 3h6l2 2h8v11H1z',
  code: 'M6 4.5L1.5 9 6 13.5M12 4.5L16.5 9 12 13.5',
  clock: 'M9 1a8 8 0 1 0 0 16A8 8 0 0 0 9 1z M9 4.5V9l3 2',
  calendar: 'M1.5 3.5h15v13h-15z M1.5 7.5h15M5.5 1.5v4M12.5 1.5v4',
  build: 'M2 15.5h14M3.5 12.5h3v3h-3zM7.5 8.5h3v7h-3zM11.5 4.5h3v11h-3z',
  play: 'M4 2.5l11 6.5-11 6.5z',
  plug: 'M9 1v6M5 7h8v3a4 4 0 0 1-8 0zM9 14v3',
  copy: 'M5 1.5h11v11H5z M1.5 5.5H4v11h11v-2.5',
  compose: 'M9 2.5H2.5v13h13V9 M7.5 10.5l.8-2.8 6.2-6.2 2 2-6.2 6.2z',
  // Leo, 2026-10-05: the Agent header's new conversation is a bubble with
  // a plus, like the App's (nf-md-message_plus_outline there).
  newChat: 'M1 2h16v10H5l-4 3v-3H1z M9 4.5v5M6.5 7h5',
  media: 'M2 6.5h3l4-4v13l-4-4H2z M12 5.5a6 6 0 0 1 0 7',
  layers: 'M9 1.5L16.5 6 9 10.5 1.5 6z M1.5 11L9 15.5 16.5 11',
  brand: 'M1 2h16v14H1z',
  // WEB-9: the six bar entries as the App draws them (Icon.remote / agent /
  // herdr / ssh / settings / bell), redrawn on this grid. Leo: the glyphs do
  // not have to match pixel for pixel.
  display: 'M1.5 2.5h15v10h-15z M6 16h6M9 12.5V16',
  robot: 'M3 7.5h12v8H3z M9 3.5v4 M8.4 2.5h1.2 M6.5 11h1.5M10 11h1.5 M1 10v3M17 10v3',
  stack: 'M1.5 2.5h15v5.5h-15z M1.5 10h15v5.5h-15z M12.5 5.2h1.5M12.5 12.7h1.5',
  prompt: 'M2 4l5 5-5 5 M9 14.5h7',
  gear: 'M9 5.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7z M9 1v2.5M9 14.5V17M1 9h2.5M14.5 9H17 M3.3 3.3l1.8 1.8M12.9 12.9l1.8 1.8M3.3 14.7l1.8-1.8M12.9 5.1l1.8-1.8',
  lines: 'M2 4.5h14M2 9h14M2 13.5h14',
  // AGENT-PLAN-1: Plan mode - a list with its marks (the chip, the plan card).
  plan: 'M2 3.5h2.5v2.5H2z M2 7.75h2.5v2.5H2z M2 12h2.5v2.5H2z M7 4.75h9M7 9h9M7 13.25h6',
  question: 'M5.5 6a3.5 3.5 0 1 1 5 3.2c-1 .5-1.5 1.1-1.5 2.3v.5 M9 14.5v1.5',
  circle: 'M9 2.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13z',
  more: 'M3 8.2h1.6v1.6H3zM8.2 8.2h1.6v1.6H8.2zM13.4 8.2H15v1.6h-1.6z',
  // ATTACH-1: the paperclip (the App's Icon.attach, SF `paperclip` / nf
  // U+F0C6), a picture and a page, on the same grid.
  attach: 'M12.5 5.5v7a3.5 3.5 0 0 1-7 0V4a2.25 2.25 0 0 1 4.5 0v8a1 1 0 0 1-2 0V6',
  image: 'M1.5 3h15v12h-15z M1.5 12.5l4.5-4.5 3.5 3.5 2.5-2.5 4.5 4.5 M11.5 5.5h1.5v1.5h-1.5z',
  file: 'M3 1.5h8l4 4v11H3z M11 1.5v4h4 M6 9.5h6M6 12.5h6',
};

const FILLED = new Set(['dot', 'play']);

export default function Icon({name, size = 18, className}) {
  const d = PATHS[name];
  if (!d) return null;
  const filled = FILLED.has(name);
  return (
    <svg
      className={className}
      viewBox="0 0 18 18"
      width={size}
      height={size}
      fill={filled ? 'currentColor' : 'none'}
      stroke={filled ? 'none' : 'currentColor'}
      strokeWidth="1.5"
      strokeLinecap="butt"
      strokeLinejoin="miter"
      aria-hidden="true"
      focusable="false"
      style={{display: 'block', flex: 'none'}}
    >
      <path d={d} />
    </svg>
  );
}

// omodachi-brand/concepts/v6-omodachi-symbol.svg. Brand rule: the symbol and
// the wordmark are alternatives and never appear side by side, so the symbol
// belongs to the bar and the wordmark to the page inside the browser panel.
export const SYMBOL_PATH =
  'M0 20H4V12H8V8H12V4H20V0H68V4H76V8H80V12H84V20H88V68H84V76H80V80H76V84H68V88H20V84H12V80H8V76H4V68H0Z M24 32V56H28V60H32V64H56V60H60V56H64V32H60V28H56V24H32V28H28V32Z';
export const SYMBOL_EYES = 'M32 36H40V48H32Z M48 36H56V48H48Z';

// With no title the mark is decorative: the button around it already carries
// the label. An empty aria-label on role="img" is itself an error, so the role
// and the label leave together.
export function Symbol({size = 22, title = ''}) {
  return (
    <svg
      viewBox="0 0 96 96"
      width={size}
      height={size}
      fill="currentColor"
      shapeRendering="crispEdges"
      role={title ? 'img' : undefined}
      aria-label={title || undefined}
      aria-hidden={title ? undefined : 'true'}
      focusable="false"
      style={{display: 'block', flex: 'none'}}
    >
      <g transform="translate(4 4)">
        <path fillRule="evenodd" d={SYMBOL_PATH} />
        <path d={SYMBOL_EYES} />
      </g>
    </svg>
  );
}
