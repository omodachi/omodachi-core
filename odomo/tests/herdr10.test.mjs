// HERDR-10: Chinese on the mirror, codex's Chinese question card, the note
// for "None of the above" - the same rules and the same real frames as the
// App (omodachi-ios `HERDR10Tests`): herdr 0.8.2 `terminal.frame`s from the
// host's observe stream, codex 0.160 in the throwaway session h10-test on a
// 94×39 pane and a 60×29 one, a scripted stand-in model ("h10-scripted"), and
// one frame read from Leo's own `debug` pane. `*.visible.txt` is Herdr's own
// `pane read --source visible` of the same screen.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {Mirror, paletteOf, cellWidth} from '../src/lib/term.js';
import {readChoices, notesOpen, joinWrapped} from '../src/lib/choices.js';
import * as W from '../src/lib/wrap.js';

const fixture = (name) => new URL('./fixtures/herdr10/herdr10-' + name, import.meta.url);
const frame = (name) => JSON.parse(readFileSync(fixture(name + '.json')));
const stream = (name) => readFileSync(fixture(name + '.ndjson'), 'utf8').trim().split('\n').map((l) => JSON.parse(l));
const visible = (name) => readFileSync(fixture(name + '.visible.txt'), 'utf8').split('\n').map((l) => l.replace(/\s+$/, ''));

async function mirrorOf(frames) {
  const mirror = new Mirror({cols: frames[0].width, rows: frames[0].height, palette: paletteOf(null)});
  for (const f of frames) {
    await new Promise((done) => {
      if (!mirror.feed({full: f.full, width: f.width, height: f.height,
        bytes: new Uint8Array(Buffer.from(f.bytes, 'base64'))}, done)) done();
    });
  }
  return mirror;
}
const rows = (mirror) => mirror.text().map((l) => l.replace(/\s+$/, ''));
async function choices(name) {
  const mirror = await mirrorOf([frame(name)]);
  const cursor = mirror.cursor();
  return readChoices(mirror.text(), cursor.hidden ? null : cursor.y);
}

test('every cell is as wide as Herdr makes it: CJK, full-width, emoji, a table', async () => {
  for (const name of ['codex-answer-mixed', 'narrow-ask-q1', 'narrow-ask-q2', 'narrow-answer']) {
    const grid = rows(await mirrorOf([frame(name)]));
    visible(name).forEach((line, index) => {
      if (index < grid.length) assert.equal(grid[index], line, name + ' row ' + index);
    });
  }
  // xterm's own Unicode 6 gave an emoji one cell, leaving the second cell
  // of Herdr's two unwritten (a blank, or an old character on an increment).
  assert.equal(cellWidth(0x1F642), 2); // 🙂
  assert.equal(cellWidth(0x2705), 2); // ✅
  assert.equal(cellWidth(0x4E2D), 2); // 中
  assert.equal(cellWidth(0xFF1F), 2); // ？
  assert.equal(cellWidth(0x41), 1);
  assert.equal(cellWidth(0x2022), 1); // •
  assert.equal(cellWidth(0x301), 0); // a combining accent
});

test('a whole observe session, increments included, ends where a fresh full frame begins', async () => {
  assert.deepEqual(rows(await mirrorOf(stream('stream-94x39'))), rows(await mirrorOf([frame('codex-answer-mixed')])));
  assert.deepEqual(rows(await mirrorOf(stream('stream-60x29'))), rows(await mirrorOf([frame('narrow-answer')])));
});

test("codex's Chinese question card is a strip; None of the above opens the note", async () => {
  const card = await choices('codex-ask-two-q1');
  assert.deepEqual(card.options.map((o) => o.label), ['具体例子 (Recommended)', '核心原理', '实际操作', 'None of the above']);
  assert.equal(card.question, '你更喜欢哪种回答风格？');
  assert.deepEqual(card.options.map((o) => o.keys), [['1'], ['2'], ['3'], ['Down', 'Down', 'Down', 'Tab']]);
  assert.deepEqual(card.options.map((o) => o.opensNote), [false, false, false, true]);
  assert.equal(card.escape, false);
  const second = await choices('codex-ask-two-q2');
  assert.deepEqual(second.options.map((o) => o.keys), [['1'], ['2'], ['Down', 'Down', 'Tab']]);
  assert.equal(second.question, '回答要多详细？');
  const narrow = await choices('narrow-ask-q1');
  assert.equal(narrow.options[0].detail, '先用一个直观例子说明，再总结规律和要点。');
  assert.deepEqual(narrow.options[3].keys, ['Down', 'Down', 'Down', 'Tab']);
  assert.equal(await choices('codex-ask-notes-open'), null);
  assert.equal(await choices('codex-ask-notes-typed'), null);
  assert.equal(await choices('codex-answer-mixed'), null, '"Questions 1/1 answered" is the transcript');
  const approval = await choices('debug-approval-cjk');
  assert.deepEqual(approval.options.map((o) => o.keys), [['1'], ['2'], ['3']]);
});

test('the notes box open is seen, so the composer sends the note', async () => {
  assert.equal(notesOpen((await mirrorOf([frame('codex-ask-notes-open')])).text()), true);
  assert.equal(notesOpen((await mirrorOf([frame('codex-ask-two-q1')])).text()), false);
  assert.equal(joinWrapped(['再总', '结规律']), '再总结规律');
  assert.equal(joinWrapped(['add details in', 'notes (tab)']), 'add details in notes (tab)');
});

test('Wrap joins Chinese codex wrapped at 60 columns without a space', async () => {
  const mirror = await mirrorOf([frame('narrow-answer')]);
  const result = W.layout(mirror.grid(), 40, true);
  const lines = result.blocks.filter((b) => b.kind === 'text').map((b) => b.runs.map((r) => r.text).join(''));
  assert.ok(lines.some((l) => l.includes('增加背景、步骤和注意事项，同时避免重复和无关内容。如果某一步你想跳过，直接告诉我就行。')),
    lines.join('\n'));
});
