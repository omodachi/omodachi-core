// AGENT-UI-2: what Leo saw on a phone. The header's model control put the
// model and the effort on one line and both were cut; the composer's
// "Message your agent…" broke onto a second line and was clipped while a turn
// ran (Send/Steer and Interrupt leave the box about 100 px on a 375 px phone).
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {en, zh} from '../src/lib/i18n.js';
import {pickPlaceholder} from '../src/lib/placeholder.js';

const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');
const render = async () => {
  const {runnerImport} = await import('vite');
  const {module} = await runnerImport(new URL('./render.jsx', import.meta.url).pathname,
    {configFile: false, logLevel: 'silent'});
  return module;
};
// 16 px system face, roughly: what the box's text is drawn at.
const width = (text) => [...text].reduce((sum, ch) => sum + (/[　-鿿]/.test(ch) ? 16 : 8.6), 0);

test('the composer keeps the whole sentence while it fits, and the short one when it does not', () => {
  for (const t of [en, zh]) {
    const candidates = [t.agent.composer, t.agent.composerShort];
    assert.ok(t.agent.composerShort.length < t.agent.composer.length, t.agent.composerShort);
    assert.equal(pickPlaceholder(260, candidates, width), t.agent.composer);
    assert.equal(pickPlaceholder(width(t.agent.composerShort) + 1, candidates, width), t.agent.composerShort);
    // Nothing fits: the shorter one, never an empty box.
    assert.equal(pickPlaceholder(10, candidates, width), t.agent.composerShort);
  }
  // At 375 px with a turn running the box has ~100 px: the short English line fits it.
  assert.ok(width(en.agent.composerShort) < 100);
});

test('a placeholder is one line - it is never wrapped onto a second, and no phone rule widens the box past its edge', () => {
  assert.match(css, /\.composer__in::placeholder \{[^}]*white-space: nowrap/);
  assert.doesNotMatch(css, /\.composer__in \{min-width: 120px\}/);
});

test('the model control is two lines: the model, then the effort', async () => {
  const r = await render();
  const html = r.renderAgent({});
  assert.match(html, /data-testid="agent-model-control"[^>]*><span class="apanel__mvs"><span class="apanel__mv">/);
  assert.match(css, /\.apanel__mvs \{display: flex; flex-direction: column/);
  // The agent's name gives way first on a narrow phone.
  assert.match(css, /\.apanel__id \{[^}]*flex: 0 1000 auto/);
});

// ── The Herdr mirror's icons (Leo: icons in Odomo's terminal were missing) ──
// herdr8-vim.json is a real lualine status line: powerline separators
// U+E0B0/U+E0B2 and octicons U+F4ED/U+F487/U+F43A. JetBrains Mono Web is a
// 123-glyph subset and a phone has no Nerd Font, so each was an empty box.

test('a real status line\'s Nerd Font code points are what the bundled symbols face covers', async () => {
  const T = await import('../src/lib/term.js');
  const {decodeMessage} = await import('../src/lib/herdr.js');
  const frame = decodeMessage(readFileSync(new URL('./fixtures/herdr8/herdr8-vim.json', import.meta.url), 'utf8'));
  const text = new TextDecoder().decode(frame.bytes);
  const icons = [...text].map((ch) => ch.codePointAt(0)).filter((cp) => T.symbolsPart(cp));
  assert.deepEqual(icons.map((cp) => cp.toString(16)), ['e0b0', 'f4ed', 'f487', 'e0b2', 'e0b2', 'f43a']);
  assert.equal(T.symbolsPart(0xF0219), 'spua'); // nvim-web-devicons' Material Design file icons
  assert.equal(T.symbolsPart(0x41), null);
  assert.equal(T.symbolsPart(0x2500), null); // box drawing stays with the text faces
  // The family list: our mono first, then the symbols face, before anything local.
  assert.match(T.FONT_FAMILY, /^"JetBrains Mono Web", "Symbols Nerd Font Mono Web", /);
  assert.match(css, /--mono: "JetBrains Mono Web", "Symbols Nerd Font Mono Web",/);
});

test('the symbols face is two files behind unicode-range, with its MIT licence beside them', () => {
  const faces = css.match(/@font-face \{[^}]*Symbols Nerd Font Mono Web[^}]*\}/g) || [];
  assert.equal(faces.length, 2);
  assert.match(faces[0], /symbols-nerd-font-mono-bmp\.woff2[\s\S]*unicode-range: U\+E000-F8FF/);
  assert.match(faces[1], /symbols-nerd-font-mono-spua\.woff2[\s\S]*unicode-range: U\+F0000-FFFFD/);
  for (const name of ['symbols-nerd-font-mono-bmp.woff2', 'symbols-nerd-font-mono-spua.woff2']) {
    const bytes = readFileSync(new URL('../src/font/' + name, import.meta.url));
    assert.equal(bytes.subarray(0, 4).toString('latin1'), 'wOF2', name);
  }
  for (const where of ['../src/font/', '../public/']) {
    assert.match(readFileSync(new URL(where + 'SymbolsNerdFont-LICENSE.txt', import.meta.url), 'utf8'),
      /Copyright \(c\) 2014 Ryan L McIntyre/);
  }
});

test('blocks and powerline separators fill their cell; every other code point is left to the fonts', async () => {
  const T = await import('../src/lib/term.js');
  assert.deepEqual(T.blockRect(0x2588), [0, 0, 1, 1]); // █
  assert.deepEqual(T.blockRect(0x2580), [0, 0, 1, 0.5]); // ▀
  assert.deepEqual(T.blockRect(0x2584), [0, 0.5, 1, 1]); // ▄
  assert.deepEqual(T.blockRect(0x258C), [0, 0, 0.5, 1]); // ▌
  assert.deepEqual(T.blockRect(0x2590), [0.5, 0, 1, 1]); // ▐
  assert.equal(T.blockRect(0x2591), null); // ░ is a pattern, not a rectangle
  assert.deepEqual(T.powerlineShape(0xE0B0).fill, [[0, 0], [1, 0.5], [0, 1]]);
  assert.deepEqual(T.powerlineShape(0xE0B2).fill, [[1, 0], [0, 0.5], [1, 1]]);
  assert.ok(T.powerlineShape(0xE0B1).line);
  assert.equal(T.powerlineShape(0xF4ED), null);
});

test('the mirror draws a real frame with its icons, cell for cell', async () => {
  const T = await import('../src/lib/term.js');
  const {decodeMessage} = await import('../src/lib/herdr.js');
  const frame = decodeMessage(readFileSync(new URL('./fixtures/herdr8/herdr8-vim.json', import.meta.url), 'utf8'));
  const calls = [];
  const ctx = new Proxy({}, {
    get: (target, key) => (key in target ? target[key]
      : (...args) => { calls.push([key, ...args]); }),
    set: (target, key, value) => { target[key] = value; if (key === 'font') calls.push(['font', value]); return true; },
  });
  const canvas = {width: 0, height: 0, style: {}, getContext: () => ctx};
  const mirror = new T.Mirror({cols: frame.width, rows: frame.height, measure: {width: 7.8, height: 17}});
  await new Promise((done) => mirror.feed(frame, done));
  mirror.canvas = canvas;
  mirror.dirty = true;
  mirror.paint();
  const texts = calls.filter((c) => c[0] === 'fillText').map((c) => c[1]);
  for (const icon of ['', '', '']) assert.ok(texts.includes(icon), icon.codePointAt(0).toString(16));
  // The separators are shapes, not text.
  assert.ok(!texts.includes('') && !texts.includes(''));
  // An icon is drawn whole: its font is never wider than the room it has (one
  // cell, or two when the next one is blank), and never the 13 px text squeezed.
  const iconFonts = calls.filter((c, i) => c[0] === 'font' && calls.slice(i + 1).find((n) => n[0] === 'fillText')?.[1] === '');
  assert.ok(iconFonts.length);
  assert.ok(texts.includes('README.md') || texts.includes('R'), 'the text is still drawn');
});
