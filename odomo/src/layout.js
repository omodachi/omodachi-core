// Provenance: copied from omodachi-web `src/app/layout.js` at main 0da28c9 (same author,
// MIT, like this repository). Kept in step by hand; the site's own build, which
// makes no requests and stores nothing, stays separate and untouched.
//
import {useEffect, useState} from 'react';

// The viewport, read as a device.
//
// WEB-9 section 0.5: a window at least 1024 wide is an iPad held in landscape, and
// anything narrower is an iPhone held upright. Both carry the App's one bar
// down the left edge, 44 wide, because that is where the bar is in every
// picture of the App on a real computer (vm-07-host-menu-on-ipad).
//
// The rest is omodachi-ios `DesignSystem/Primitives/PanelArea.swift`:
//
//   PanelMeasure.sideBySide  panel 1 puts the menu and the keybindings side
//                            by side only in landscape and only when the
//                            panel area is at least 360 + 5 + 420 = 785 wide.
//   PanelMeasure.isNarrow    a column under 470 stacks what would sit in a row.
//   RemoteEntryCards         the two Remote cards sit side by side once both
//                            fit at 260 each, plus the gap and the padding.

export const PAD_MIN = 1024;
export const BAR = 44;
// LITE-1c: a screen whose short side is at least this (every iPad, either
// way up, and a desktop window) has room for a word under each bar icon; the
// bar is then this wide. A phone's bar stays 44 and icon-only.
export const NAMED_MIN = 600;
export const NAMED_BAR = 64;
export const SIDE_BY_SIDE = 360 + 5 + 420;
export const NARROW = 470;
export const CARD_MIN = 260;

export function describe(width, height) {
  const device = width >= PAD_MIN ? 'pad' : 'phone';
  const vertical = height > width;
  const named = Math.min(width, height) >= NAMED_MIN;
  const areaWidth = Math.max(0, width - (named ? NAMED_BAR : BAR));
  return {
    width,
    height,
    device,
    vertical,
    named,
    areaWidth,
    twoColumn: device === 'pad' && !vertical && areaWidth >= SIDE_BY_SIDE,
    narrow: areaWidth < NARROW,
    stackCards: device === 'phone' || areaWidth < 2 * CARD_MIN + 12 + 24,
  };
}

function read() {
  if (typeof window === 'undefined') return describe(1440, 900);
  return describe(window.innerWidth, window.innerHeight);
}

export function useLayout() {
  const [layout, setLayout] = useState(read);
  useEffect(() => {
    let frame = 0;
    const update = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => setLayout(read()));
    };
    window.addEventListener('resize', update);
    window.addEventListener('orientationchange', update);
    update();
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener('resize', update);
      window.removeEventListener('orientationchange', update);
    };
  }, []);
  return layout;
}
