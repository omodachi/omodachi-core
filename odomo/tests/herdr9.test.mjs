// HERDR-9: the choices strip, the empty composer's keys and ⌫ - the same
// rules as the App (omodachi-ios `HerdrChoices.swift`, its HERDR9 tests). The
// codex frames are real `terminal.frame`s from the host (codex 0.160, herdr
// 0.8.2, a 200×55 pane in the throwaway session h9-test, model names
// replaced), the same files the App's tests read; the `collie--*.txt` panes
// are ColliePWA's test captures (MIT, `public/collie-LICENSE.txt`), which is
// where Claude Code comes from: it is never run on the test host.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runnerImport} from 'vite';
import {Mirror, paletteOf} from '../src/lib/term.js';
import {readChoices, passthroughKey, sameChoices, stripShortcut, pointerWalk} from '../src/lib/choices.js';
import * as H from '../src/lib/herdr.js';
import {en, zh} from '../src/lib/i18n.js';

const {module: render} = await runnerImport(new URL('./render.jsx', import.meta.url).pathname,
  {root: new URL('..', import.meta.url).pathname, configFile: false, logLevel: 'silent'});

const ZH = 'zh-Hans';
const fixture = (name) => new URL('./fixtures/herdr9/' + name, import.meta.url);

async function host(name) {
  const frame = JSON.parse(readFileSync(fixture('herdr9-' + name + '.json')));
  const mirror = new Mirror({cols: frame.width, rows: frame.height, palette: paletteOf(null)});
  await new Promise((done) => mirror.feed({full: true, width: frame.width, height: frame.height,
    bytes: new Uint8Array(Buffer.from(frame.bytes, 'base64'))}, done));
  const cursor = mirror.cursor();
  return readChoices(mirror.text(), cursor.hidden ? null : cursor.y);
}

function collie(name) {
  const raw = readFileSync(fixture('collie--' + name + '.txt'), 'utf8');
  return readChoices(raw.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '').replace(/\r/g, '').split('\n'));
}

const labels = (c) => (c ? c.options.map((o) => o.label) : []);
const keys = (c) => (c ? c.options.map((o) => o.keys) : []);

test('codex 0.160\'s command approval is its three choices, confirmed on the digit', async () => {
  const c = await host('codex-approval-exec');
  assert.equal(c.kind, 'menu');
  assert.deepEqual(labels(c), ['Yes, proceed',
    'Yes, and don\'t ask again for commands that start with `printf hi > hello.txt`',
    'No, and tell Codex what to do differently']);
  assert.deepEqual(keys(c), [['1'], ['2'], ['3']]);
  assert.deepEqual(c.options.map((o) => o.highlighted), [true, false, false]);
  assert.deepEqual(c.options.map((o) => o.lasting), [false, true, false]);
  assert.equal(c.escape, true);
  assert.equal(c.question, 'Would you like to run the following command?');
});

test('codex\'s patch and network approvals, its pickers and its question card', async () => {
  assert.deepEqual(labels(await host('codex-approval-patch')),
    ['Yes, proceed', 'Yes, and don\'t ask again for these files', 'No, and tell Codex what to do differently']);
  assert.ok((await host('codex-approval-network')).signature.includes('curl'));
  const model = await host('codex-model-picker');
  assert.equal(model.options[0].number, 4);
  assert.deepEqual(model.options.find((o) => o.number === 10).keys, ['Up', 'Enter'], 'past 9: the arrows');
  const moved = await host('codex-model-picker-moved');
  assert.notEqual(model.signature, moved.signature, 'a moved highlight is another screen');
  assert.deepEqual(labels(await host('codex-permissions')), ['Ask for approval', 'Approve for me', 'Full Access']);
  const fruit = await host('codex-ask-fruit');
  assert.deepEqual(labels(fruit), ['Apple', 'Pear', 'None of the above']);
  assert.equal(fruit.escape, false, 'esc interrupts the whole conversation there');
});

test('codex\'s idle screen, a draft, a numbered answer and a reconnect are not menus', async () => {
  for (const name of ['codex-idle', 'codex-draft', 'codex-numbered-answer', 'codex-slash-popup', 'codex-reconnecting']) {
    assert.equal(await host(name), null, name);
  }
});

test('a shell\'s [y/N] is Yes / No, each a letter and Enter', async () => {
  const c = await host('shell-yes-no');
  assert.equal(c.kind, 'yesNo');
  assert.deepEqual(keys(c), [['y', 'Enter'], ['n', 'Enter']]);
  assert.deepEqual(c.options.map((o) => o.highlighted), [false, true]);
  assert.deepEqual(keys(readChoices(['Are you sure you want to continue connecting (yes/no/[fingerprint])? '])),
    [['y', 'e', 's', 'Enter'], ['n', 'o', 'Enter']]);
  assert.equal(readChoices(['Continue? [Y/n]', '$ echo']), null);
});

test('Claude Code (ColliePWA\'s captures): permission, question, WebFetch, pointed list; and what is not a menu', () => {
  const bash = collie('claude--permission-bash');
  assert.deepEqual(keys(bash), [['1'], ['2'], ['3']]);
  assert.equal(bash.question, 'Do you want to proceed?');
  const select = collie('claude--select-menu');
  assert.deepEqual(labels(select), ['Red', 'Green', 'Blue', 'Chat about this']);
  assert.deepEqual(select.options[0].keys, ['1', 'Enter']);
  assert.equal(labels(collie('claude-lab--permission-webfetch--w82')).length, 3);
  assert.deepEqual(keys(collie('claude--trust-prompt-unnumbered')), [['Enter'], ['Down', 'Enter']]);
  assert.equal(collie('codex--v0156-approval-exec-wrapped-50').options[2].label, 'No, and tell Codex what to do differently');
  for (const name of ['claude-lab--transcript-dialog-lookalike--w82', 'claude--v2283-permission-amend-focused',
    'codex--ask-notes-focused']) {
    assert.equal(collie(name), null, name);
  }
});

test('ordinary output is not a menu', () => {
  assert.equal(readChoices(['Steps:', '  1. Build', '  2. Test', '', 'Press enter to continue']), null);
  assert.equal(readChoices(['› 1. Build', '› 2. Test', '', 'Press enter to confirm']), null);
  assert.equal(readChoices(['› 1. Build', '  2. Test', '', 'Press enter to confirm', '$ ls']), null);
  assert.equal(stripShortcut('Yes, proceed (y)'), 'Yes, proceed');
  assert.equal(stripShortcut('Apple (Recommended)'), 'Apple (Recommended)');
  assert.deepEqual(pointerWalk(2, 0), ['Up', 'Up', 'Enter']);
});

test('a tap is guarded: a different question in the same shape is not the one that was seen', async () => {
  const seen = await host('codex-approval-exec');
  assert.ok(sameChoices(seen, await host('codex-approval-exec')));
  assert.ok(!sameChoices(seen, await host('codex-approval-network')));
  assert.ok(!sameChoices(seen, null));
});

test('an empty composer hands ⌫, Return, the arrows, Tab and Esc to the pane; ⌫ is on the keys row', () => {
  const key = (k, extra = {}) => passthroughKey({key: k, ...extra});
  assert.equal(key('Backspace'), 'Backspace');
  assert.equal(key('Enter'), 'Enter');
  assert.equal(key('Enter', {shiftKey: true}), null);
  assert.equal(key('Tab', {shiftKey: true}), 'shift+Tab');
  assert.equal(key('Escape'), 'Escape');
  assert.deepEqual(['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].map((k) => key(k)), ['Up', 'Down', 'Left', 'Right']);
  assert.equal(key('c', {ctrlKey: true}), 'ctrl+c');
  assert.equal(key('a'), null, 'a letter is typing');
  assert.equal(key('Enter', {isComposing: true}), null, 'an input method\'s Return is its own');
  assert.equal(key('v', {metaKey: true}), null);
  const backspace = H.KEY_ROW.find((cap) => cap.id === 'backspace');
  assert.deepEqual(backspace, {id: 'backspace', face: '⌫', key: 'Backspace', name: 'backspace', repeats: true});
  assert.equal(H.KEY_ROW.indexOf(backspace), H.FIRST_ROW, 'first on the phone\'s second row');
});

test('the strip draws each option\'s own words, Esc, and Yes / No in both languages', async () => {
  const approval = await host('codex-approval-exec');
  const store = {choices: approval, choicesAnswered: false, answer() {}, answerEscape() {}};
  const html = render.renderChoices(store);
  assert.ok(html.includes('data-testid="herdr-choices"'));
  assert.ok(html.includes('Yes, proceed'));
  assert.ok(html.includes('data-testid="herdr-choices-escape"'));
  assert.ok(html.includes(en.herdr.choicesTitle));
  const yn = render.renderChoices({...store, choices: await host('shell-yes-no')}, ZH);
  assert.ok(yn.includes('>是<') && yn.includes('>否<'));
  const answered = render.renderChoices({...store, choicesAnswered: true}, ZH);
  assert.ok(answered.includes(zh.herdr.choicesAnswered));
  for (const k of ['choicesTitle', 'choicesHint', 'choicesSelected', 'choicesLasting', 'choicesEscapeHint', 'choicesYes',
    'choicesNo', 'choicesAnswered', 'choicesChanged', 'composerHint']) {
    assert.ok(en.herdr[k] && zh.herdr[k], k);
  }
  assert.equal(en.herdr.keyNames.backspace, 'Delete');
  assert.equal(zh.herdr.keyNames.backspace, '删除');
});
