// NOTIFY-1: Odomo's half - the "alert me even at the computer" switch and the
// opt-in "finished, not seen" kind, against core's own prefs fixture.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {KINDS, OFF_BY_DEFAULT} from '../src/lib/push.js';
import {en, zh} from '../src/lib/i18n.js';

const fixture = (name) => JSON.parse(readFileSync(new URL('../../contracts/fixtures/' + name, import.meta.url)));
const source = readFileSync(new URL('../src/panels/PushSettings.jsx', import.meta.url), 'utf8');

test('the finished kind is the host\'s, and off unless asked for', () => {
  const prefs = fixture('notifications-prefs.json');
  assert.ok(KINDS.includes('herdr_done'));
  assert.deepEqual(OFF_BY_DEFAULT, ['herdr_done']);
  assert.equal(prefs.kinds.herdr_done, false);
  assert.equal(prefs.quiet_at_desk, true);
});

test('both languages say what the desk switch does, in whole sentences', () => {
  for (const table of [en, zh]) {
    for (const key of ['desk', 'deskDetail', 'herdr_done']) assert.ok(table.push[key], key);
    assert.match(table.push.deskDetail, /[.。]$/);
  }
  assert.match(en.push.deskDetail, /two minutes/);
  assert.match(zh.push.deskDetail, /两分钟/);
});

test('the switch is "alert me anyway": on means not quiet', () => {
  assert.match(source, /on=\{prefs\.quiet_at_desk === false\}/);
  assert.match(source, /change\(\{quiet_at_desk: !value\}\)/);
  assert.match(source, /OFF_BY_DEFAULT\.includes\(kind\) \? prefs\.kinds\[kind\] === true/);
});
