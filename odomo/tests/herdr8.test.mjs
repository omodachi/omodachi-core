// HERDR-8: Herdr on a phone - the mirror as wrapped text, the same rules as
// the App (omodachi-ios `HerdrWrap`). The frames are real `terminal.frame`s
// from the host's observe stream (herdr 0.8.2, a 200×55 pane in the test
// session h8-test; the model's name redacted), the same six the App's tests use.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runnerImport} from 'vite';
import {Mirror, paletteOf} from '../src/lib/term.js';
import * as W from '../src/lib/wrap.js';
import {en, zh} from '../src/lib/i18n.js';

const {module: render} = await runnerImport(new URL('./render.jsx', import.meta.url).pathname,
  {root: new URL('..', import.meta.url).pathname, configFile: false, logLevel: 'silent'});

async function grid(name) {
  const frame = JSON.parse(readFileSync(new URL('./fixtures/herdr8/herdr8-' + name + '.json', import.meta.url)));
  const mirror = new Mirror({cols: frame.width, rows: frame.height, palette: paletteOf(null)});
  await new Promise((done) => mirror.feed({full: true, width: frame.width, height: frame.height,
    bytes: new Uint8Array(Buffer.from(frame.bytes, 'base64'))}, done));
  return mirror.grid();
}

const lines = (result) => result.blocks.map((block) => {
  if (block.kind === 'text') return block.runs.map((r) => r.text).join('');
  if (block.kind === 'rule') return 'RULE ' + block.runs.map((r) => r.text).join('');
  if (block.kind === 'wide') return 'WIDE ' + block.rows.map((row) => row.map((r) => r.text).join('')).join('\n');
  return '';
});

test('codex\'s answer is one line per paragraph again, its items stay items, the address is whole', async () => {
  const result = W.layout(await grid('codex-answer'), 44, true);
  assert.equal(result.fullScreen, false);
  const text = lines(result);
  const paragraph = text.find((l) => l.startsWith('• This tiny uptime checker'));
  assert.ok(paragraph && paragraph.endsWith('easy to interpret.'), paragraph);
  assert.equal(text.filter((l) => l.startsWith('  • ')).length, 4);
  assert.ok(text.some((l) => l.includes('(https://example.com/docs/uptime/configuration#retries-and-backoff-ceiling)')));
  assert.equal(text[text.length - 1], '  ← for agents · ? for shortcuts');
});

test('the logo moves left whole; claude\'s drawing pans as one block and its rules are cut', async () => {
  const welcome = lines(W.layout(await grid('codex-welcome'), 44, true)).filter((l) => l.includes('⣿'));
  assert.ok(welcome.length > 10);
  assert.ok(Math.min(...welcome.map((l) => l.length - l.trimStart().length)) <= 2);
  const claude = W.layout(await grid('claude-onboarding'), 44, true);
  assert.ok(claude.blocks.filter((b) => b.kind === 'wide').length <= 1);
  const rules = claude.blocks.filter((b) => b.kind === 'rule').map((b) => b.runs.map((r) => r.text).join(''));
  assert.equal(rules.length, 2);
  assert.ok(rules.every((r) => [...r].length <= 44));
});

test('a build log joins the terminal\'s own wrap, keeps its colours and ends at the prompt with its cursor', async () => {
  const result = W.layout(await grid('build-log'), 44, false);
  assert.equal(result.fullScreen, false);
  const text = lines(result);
  const ld = text.find((l) => l.startsWith('/usr/bin/ld:'));
  assert.ok(ld.endsWith('(see https://example.com/docs/uptime/build-troubleshooting#undefined-reference-to-x509)'), ld);
  assert.ok(text[text.length - 1].startsWith('~/h8test ❯'));
  const last = result.blocks[result.blocks.length - 1];
  assert.ok(last.runs.some((r) => r.cursor), 'the cursor is where it is');
  const all = result.blocks.flatMap((b) => b.runs || []);
  const failed = all.find((r) => r.text.includes('FAILED:'));
  assert.ok(failed.style && failed.style.bold && failed.style.fg, 'FAILED keeps its colour and weight');
});

test('a wide table pans as one block with its columns intact', async () => {
  const result = W.layout(await grid('wide-table'), 44, false);
  const wide = result.blocks.filter((b) => b.kind === 'wide');
  assert.equal(wide.length, 1);
  const rows = wide[0].rows.map((row) => row.map((r) => r.text).join(''));
  assert.ok(rows[0].startsWith('┌───'));
  assert.ok(rows.some((r) => r.startsWith('│ svc-11-uptime-checker │ ap-south-1 │')));
  assert.equal(lines(result)[0], '~/h8test ❯ python3 .tools/table.py');
});

test('vim is a full-screen program; an agent\'s screen never is', async () => {
  const vim = await grid('vim');
  assert.equal(W.layout(vim, 44, false).fullScreen, true);
  assert.equal(W.layout(vim, 44, true).fullScreen, false);
  for (const name of ['build-log', 'wide-table']) assert.equal(W.layout(await grid(name), 44, false).fullScreen, false, name);
});

test('the App and Odomo wrap the same frames into the same lines', async () => {
  // The App's own assertions, line for line, on the codex answer and the log.
  const codex = lines(W.layout(await grid('codex-answer'), 44, true));
  assert.ok(codex.includes('› Without running any commands or touching files, write a short README section for a tiny uptime checker: one paragraph of about four sentences, then a bullet list of four options with one-line explanations, then a link to https://example.com/docs/uptime/configuration#retries-and-backoff-ceiling for details.'));
  assert.deepEqual(lines(W.layout(await grid('codex-answer'), 30, true)), lines(W.layout(await grid('codex-answer'), 80, true)));
});

test('modes: Wrap → Fit → Actual → Wrap; a phone starts on Wrap, a desk on Fit; remembered per kind', () => {
  assert.equal(W.nextMode('wrap'), 'fit');
  assert.equal(W.nextMode('fit'), 'actual');
  assert.equal(W.nextMode('actual'), 'wrap');
  assert.equal(W.nextMode('custom'), 'actual', 'a pinch counts as fit');
  assert.equal(W.preferredMode('h8-codex-' + Date.now(), true), 'wrap');
  assert.equal(W.preferredMode('h8-codex-' + Date.now(), false), 'fit');
});

test('AGENT-UI-3: a phone cycles Wrap ↔ Fit only, as the iPhone App; its words say so', () => {
  assert.equal(W.nextMode('wrap', true), 'fit');
  assert.equal(W.nextMode('fit', true), 'wrap');
  assert.equal(W.nextMode('custom', true), 'wrap', 'a pinch counts as fit');
  assert.equal(W.nextMode('actual', true), 'wrap');
  assert.equal(W.nextMode('fit', false), 'actual', 'a tablet or a desk keeps Actual size');
  assert.equal(en.herdr.hintPhone, 'Double-tap the screen to switch between Wrap and Fit to width. Keys holds Esc, the arrows and ^C.');
  assert.equal(zh.herdr.hintPhone, '双击画面，就能在换行显示和适应宽度之间切换。「按键」里有 Esc、方向键和 ^C。');
  assert.equal(en.herdr.mirrorHintPhone, 'Switches between wrapped text and the screen fitted to the width.');
  assert.equal(zh.herdr.mirrorHintPhone, '在换行显示和适应宽度之间切换。');
  for (const table of [en, zh]) assert.doesNotMatch(table.herdr.hintPhone, /Actual|原始大小/);
});

test('AGENT-UI-3: nothing in the mirror widens the panel past the screen', () => {
  const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');
  assert.match(css, /\.mirror \{position: relative; flex: 1; min-width: 0;/);
  assert.match(css, /\.hstage > \.mirror \{overflow: hidden\}/);
});

test('the wrap view draws text, a panning block, a link and the cursor; no canvas', async () => {
  const palette = paletteOf(null);
  const html = render.renderWrap(W.layout(await grid('build-log'), 44, false), palette);
  assert.match(html, /data-testid="herdr-wrap"/);
  assert.doesNotMatch(html, /<canvas/);
  assert.match(html, /class="wrapv__a"[^>]*>https:\/\/example\.com\/docs\/uptime\/build-troubleshooting#undefined-reference-to-x509</);
  assert.ok(html.includes('background:' + palette.cursor), 'the cursor cell');
  const table = render.renderWrap(W.layout(await grid('wide-table'), 44, false), palette);
  assert.match(table, /class="wrapv__w" data-testid="herdr-wrap-wide"/);
});

test('the words, in both languages', () => {
  for (const table of [en, zh]) {
    for (const key of ['zoomWrap', 'mirrorHint', 'fullScreenNote', 'keysHint', 'hintPhone']) assert.ok(table.herdr[key], key);
  }
  assert.equal(en.herdr.zoomWrap, 'Wrap');
  assert.equal(zh.herdr.zoomWrap, '换行显示');
});
