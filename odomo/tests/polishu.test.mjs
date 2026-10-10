// POLISH-U: focusing a box on an iPhone no longer scrolls the page up. While
// the on-screen keyboard is up the root box is the visible part of the screen
// above it (lib/viewport.js + styles.css). Measured on an iPhone 17 simulator,
// Home Screen Odomo: the keyboard with its accessory bar left 488 of 874 points
// and iOS had moved the page 396 points up, taking the header with it.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {readViewport, applyViewport, followKeyboard, KEYBOARD_MIN} from '../src/lib/viewport.js';

function fakeRoot() {
  const style = new Map();
  return {
    dataset: {},
    clientHeight: 874,
    style: {
      setProperty: (name, value) => style.set(name, value),
      removeProperty: (name) => style.delete(name),
      get: (name) => style.get(name),
      size: () => style.size,
    },
  };
}

function fakeView({height = 874, offsetTop = 0, pageTop, scrollY = 0, scale = 1, inner = 874} = {}) {
  const listeners = {vv: {}, view: {}};
  const root = fakeRoot();
  const vv = {
    height, offsetTop, scale, pageTop: pageTop === undefined ? scrollY + offsetTop : pageTop,
    addEventListener: (type, fn) => { listeners.vv[type] = fn; },
    removeEventListener: (type) => { delete listeners.vv[type]; },
  };
  const scrolled = [];
  const frames = [];
  const view = {
    visualViewport: vv, innerHeight: inner, scrollY, scrollX: 0,
    document: {documentElement: root},
    requestAnimationFrame: (fn) => frames.push(fn),
    cancelAnimationFrame: () => {},
    addEventListener: (type, fn) => { listeners.view[type] = fn; },
    removeEventListener: (type) => { delete listeners.view[type]; },
    scrollTo: (x, y) => { scrolled.push([x, y]); view.scrollY = y; },
  };
  // One animation frame: whatever was scheduled runs.
  const flush = () => { while (frames.length) frames.shift()(); };
  return {view, vv, root, listeners, scrolled, flush};
}

test('no keyboard: nothing is said, and browser chrome is not a keyboard', () => {
  assert.equal(readViewport(null), null);
  assert.equal(readViewport({document: {documentElement: {}}}), null);
  assert.equal(readViewport(fakeView().view).keyboard, false);
  // A bar coming or going changes the visible height by less than a keyboard.
  assert.equal(readViewport(fakeView({height: 874 - KEYBOARD_MIN + 1}).view).keyboard, false);
  // A pinch zoom shrinks the visual viewport too; that is not a keyboard.
  assert.equal(readViewport(fakeView({height: 300, scale: 2}).view).keyboard, false);
});

test('the keyboard: the visible part above it, and where it begins', () => {
  // The simulator's numbers: 488 left above the keyboard and its bar, the
  // page moved 396 up by iOS.
  const reading = readViewport(fakeView({height: 488, offsetTop: 0, scrollY: 396}).view);
  assert.deepEqual(reading, {keyboard: true, height: 488, pageTop: 396, offsetTop: 0});
  // A tab: iOS pans the visual viewport inside the layout one instead.
  assert.equal(readViewport(fakeView({height: 420.4, offsetTop: 120.6}).view).offsetTop, 121);
});

test('applied to <html>: a data attribute and three custom properties, all taken back after', () => {
  const root = fakeRoot();
  assert.equal(applyViewport(root, {keyboard: true, height: 488, pageTop: 396, offsetTop: 0}), true);
  assert.equal(root.dataset.keyboard, 'up');
  assert.equal(root.style.get('--om-vv-height'), '488px');
  assert.equal(root.style.get('--om-vv-page-top'), '396px');
  assert.equal(root.style.get('--om-vv-offset-top'), '0px');
  assert.equal(applyViewport(root, {keyboard: true, height: 500, pageTop: 380, offsetTop: 0}), false, 'still up');
  assert.equal(applyViewport(root, {keyboard: false}), true);
  assert.equal(root.dataset.keyboard, undefined);
  assert.equal(root.style.size(), 0);
});

test('it follows the visual viewport, and the page is put back when the keyboard goes', () => {
  const {view, vv, root, listeners, scrolled, flush} = fakeView();
  const stop = followKeyboard(view);
  assert.deepEqual(Object.keys(listeners.vv).sort(), ['resize', 'scroll']);
  assert.deepEqual(Object.keys(listeners.view).sort(), ['focusin', 'focusout']);
  assert.equal(root.dataset.keyboard, undefined);
  // Focus: the keyboard comes up and iOS scrolls the page.
  vv.height = 488; view.scrollY = 396; vv.pageTop = 396;
  listeners.view.focusin();
  listeners.vv.resize();
  listeners.vv.scroll();
  flush();
  assert.equal(root.dataset.keyboard, 'up');
  assert.equal(root.style.get('--om-vv-page-top'), '396px');
  // Blur: the keyboard goes; whatever iOS scrolled goes with it.
  vv.height = 874; vv.pageTop = 396;
  listeners.vv.resize();
  flush();
  assert.equal(root.dataset.keyboard, undefined);
  assert.deepEqual(scrolled, [[0, 0]]);
  stop();
  assert.deepEqual(Object.keys(listeners.vv), []);
  assert.deepEqual(Object.keys(listeners.view), []);
  assert.equal(followKeyboard({visualViewport: null})(), undefined, 'no visualViewport: nothing to follow');
});

test('the page is wired to it: main.jsx follows, styles.css sizes the root by it', () => {
  const main = readFileSync(new URL('../src/main.jsx', import.meta.url), 'utf8');
  assert.match(main, /followKeyboard\(window\)/);
  const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');
  assert.match(css, /html\[data-keyboard="up"\] \.app, html\[data-keyboard="up"\] \.pair \{\s*top: var\(--om-vv-offset-top, 0px\); bottom: auto; height: var\(--om-vv-height, 100%\);/);
  const standalone = css.slice(css.lastIndexOf('@media (display-mode: standalone)'));
  assert.match(standalone, /top: var\(--om-vv-page-top, 0px\); height: var\(--om-vv-height, 100lvh\)/);
  // Last in the file, so it wins over the Home Screen root's 100lvh.
  assert.ok(css.lastIndexOf('--om-vv-page-top') > css.indexOf('height: 100lvh}'));
});
