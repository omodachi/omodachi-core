// NOTIFY-3: a request decided on the computer leaves the list; a row opens on a
// tap and says where a request is decided; every row can be hidden, and all of
// them at once. Rows and events are core's own contract fixtures.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runnerImport} from 'vite';
import {en, zh} from '../src/lib/i18n.js';

const fixtures = new URL('../../contracts/fixtures/', import.meta.url);
const read = (name) => JSON.parse(readFileSync(new URL(name, fixtures), 'utf8'));
const listing = read('notifications.json').notifications;
const posted = read('event-notification-posted.json');
const updated = read('event-notification-updated.json');
const withdrawn = read('event-notification-withdrawn.json');
const {module: R} = await runnerImport(new URL('./render.jsx', import.meta.url).pathname);

test('the fixtures are what core says: a request row carries its kind', () => {
  const request = listing.find((row) => row.app === 'Omodachi');
  assert.equal(request.kind, 'pairing_request');
  assert.equal(listing.find((row) => row.app !== 'Omodachi').kind, undefined);
  assert.equal(withdrawn.payload.withdrawn, true);
  assert.equal(withdrawn.payload.id, request.id);
});

test('withdrawn removes the row; leaving the screen keeps it as history', () => {
  let rows = R.notificationEvent([], posted);
  assert.deepEqual(rows.map((row) => row.id), [posted.payload.id]);
  rows = R.notificationEvent(rows, updated);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].active, false, 'a toast that left the screen is history');
  rows = R.notificationEvent(rows, withdrawn);
  assert.deepEqual(rows, [], 'a decided request is gone');
  // Nothing else is touched, and nonsense is ignored.
  const other = listing.filter((row) => row.app !== 'Omodachi');
  assert.deepEqual(R.notificationEvent(other, withdrawn), other);
  assert.equal(R.notificationEvent(other, {type: 'notification.updated', payload: {id: 7}}), other);
});

test('an opened request row says where it is decided, in both languages', () => {
  const request = listing.find((row) => row.kind === 'pairing_request');
  assert.equal(R.requestNote(request, en.notifications), en.notifications.decideOnComputer);
  assert.equal(R.requestNote({...request, kind: 'ssh_request'}, zh.notifications), zh.notifications.decideOnComputer);
  assert.equal(R.requestNote(listing.find((row) => !row.kind), en.notifications), null);
  assert.match(en.notifications.decideOnComputer, /on the computer/);
  assert.match(zh.notifications.decideOnComputer, /电脑上/);
});

test('every row is a tap target, can be hidden, and Clear all is offered', () => {
  for (const lang of ['en', 'zh-Hans']) {
    const html = R.renderNotifications({rows: listing}, lang);
    const t = lang === 'en' ? en : zh;
    for (const row of listing) {
      assert.match(html, new RegExp(`data-testid="notification-open-${row.id}"`), 'the text is a button');
      assert.match(html, new RegExp(`data-testid="notification-hide-${row.id}"`), 'and it can be hidden');
    }
    assert.match(html, /data-kind="pairing_request"/);
    assert.match(html, /data-testid="notifications-clear"/);
    assert.ok(html.includes('>' + t.notifications.clear + '<'));
    assert.doesNotMatch(html, /notification-why-/, 'closed rows say nothing extra');
  }
  const empty = R.renderNotifications({rows: []});
  assert.doesNotMatch(empty, /notifications-clear/, 'nothing to clear, no button');
});
