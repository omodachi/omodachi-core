// REL2-POLISH-2: Wrap joins a row with the next only when the next row really
// continues it - the terminal's own wrap flag when the grid has one, and
// otherwise a row that fills the width and is cut mid-word, or an app's word
// wrap whose next word did not fit. The same cases as the App's
// `REL2POLISH2Tests`.
import test from 'node:test';
import assert from 'node:assert/strict';
import {Mirror, paletteOf} from '../src/lib/term.js';
import * as W from '../src/lib/wrap.js';

async function mirrorOf(cols, rows, text) {
  const mirror = new Mirror({cols, rows, palette: paletteOf(null)});
  await new Promise((done) => mirror.feed({full: true, width: cols, height: rows,
    bytes: new TextEncoder().encode(text)}, done));
  return mirror;
}

// A grid as Herdr's frames leave it: every row placed, no wrap flags.
function placed(rows, cols) {
  return {cols, cursor: null, rows: rows.map((row) => {
    const cells = [...row].map((text) => ({text, style: null}));
    while (cells.length < cols) cells.push({text: ' ', style: null});
    return cells;
  })};
}

const lines = (result) => result.blocks.filter((b) => b.kind === 'text').map((b) => b.runs.map((r) => r.text).join(''));

// The App's demo nvim pane (96 columns), its first rows: line 1 ends 15
// columns short of the edge and line 2 is a line of its own.
const NVIM = '\x1b[90m  1\x1b[0m  \x1b[33m"""A tiny uptime checker: prints the HTTP status of each URL it is given."""\x1b[0m\r\n'
  + '\x1b[90m  2\x1b[0m  \x1b[35mimport\x1b[0m sys\r\n\x1b[90m  3\x1b[0m  \x1b[35mimport\x1b[0m time\r\n';

test('nvim\'s first line is not joined with its second, with flags and without', async () => {
  const mirror = await mirrorOf(96, 8, NVIM);
  for (const wrapFlags of [true, false]) {
    const grid = mirror.grid({wrapFlags});
    assert.equal(Array.isArray(grid.wrapped), wrapFlags);
    const text = lines(W.layout(grid, 40, true));
    assert.ok(text[0].endsWith('it is given."""'), text[0]);
    assert.equal(text[1], '  2  import sys');
  }
});

test('a full-width row followed by a new line - numbered, a prompt - stays two lines', () => {
  const full = '  1  abcdefghij klmno'; // 21 columns
  for (const next of ['  2  import sys', '❯ ls', '$ ls']) {
    assert.deepEqual(lines(W.layout(placed([full, next], 21), 12, true)), [full, next], next);
  }
});

test('a real soft wrap is joined without a space: the terminal\'s flag, or a placed row cut mid-word', async () => {
  const address = 'see https://example.com/docs/uptime/build-troubleshooting#undefined-reference';
  const mirror = await mirrorOf(30, 6, address + '\r\n$ ');
  const flagged = mirror.grid({wrapFlags: true});
  assert.deepEqual(flagged.wrapped.slice(0, 3), [false, true, true]);
  assert.equal(lines(W.layout(flagged, 12, false))[0], address);
  const cut = [address.slice(0, 30), address.slice(30, 60), address.slice(60), '$ '];
  assert.equal(lines(W.layout(placed(cut, 30), 12, false))[0], address);
});

test('with flags a full row the terminal did not wrap is never one word', async () => {
  const mirror = await mirrorOf(10, 4, 'abcdefghij\r\nklm');
  const flagged = mirror.grid({wrapFlags: true});
  assert.deepEqual(flagged.wrapped.slice(0, 2), [false, false]);
  // (The trailing cell is the cursor's, after `klm`.)
  const trimmed = (grid) => lines(W.layout(grid, 8, false)).map((l) => l.trimEnd());
  assert.deepEqual(trimmed(flagged), ['abcdefghij klm']);
  assert.deepEqual(trimmed(mirror.grid()), ['abcdefghijklm'], 'without flags the same cells read as the terminal\'s own wrap');
});

test('an app\'s word wrap joins only when the next word did not fit', () => {
  assert.deepEqual(lines(W.layout(placed(['  This sentence was wrapped by the app', '  at the edge of the pane.'], 40), 20, true)),
    ['  This sentence was wrapped by the app at the edge of the pane.']);
  assert.deepEqual(lines(W.layout(placed(['  A line that ends before the edge', '  a new one.'], 40), 20, true)),
    ['  A line that ends before the edge', '  a new one.']);
});
