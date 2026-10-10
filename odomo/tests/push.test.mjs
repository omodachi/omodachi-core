// LITE-1b L3: push in the page and in the worker, against core's own push
// contract fixtures (../contracts/fixtures/push-*.json).
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {KINDS, openTarget, snoozeUntil, snoozed, support, turnOff, turnOn, urlBase64ToBytes, prepare} from '../src/lib/push.js';
import {HostClient} from '../src/lib/api.js';

const fixture = (name) => JSON.parse(readFileSync(new URL('../../contracts/fixtures/' + name, import.meta.url)));
const IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 26_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5 Mobile/15E148 Safari/604.1';
const CHROME = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36';

test('the kinds are the host contract\'s', () => {
  assert.deepEqual(Object.keys(fixture('notifications-prefs.json').kinds), KINDS);
});

test('the host key decodes to an uncompressed P-256 point', () => {
  const bytes = urlBase64ToBytes(fixture('push-vapid-key.json').vapid_public_key);
  assert.equal(bytes.length, 65);
  assert.equal(bytes[0], 4);
});

test('what this browser can do', () => {
  const env = (ua, {secure = true, push = true, permission = 'default', standalone = false} = {}) => ({
    isSecureContext: secure,
    navigator: {userAgent: ua, serviceWorker: {}, maxTouchPoints: ua === IPHONE ? 5 : 0},
    matchMedia: () => ({matches: standalone}),
    ...(push ? {PushManager: function () {}, Notification: {permission}} : {}),
  });
  assert.equal(support(env(IPHONE, {push: false})).state, 'add_to_home_screen');
  assert.equal(support(env(IPHONE, {standalone: true})).state, 'ready');
  assert.equal(support(env(CHROME)).state, 'ready');
  assert.equal(support(env(CHROME, {permission: 'denied'})).state, 'denied');
  assert.equal(support(env(CHROME, {secure: false})).state, 'insecure');
  assert.equal(support(env(CHROME, {push: false})).state, 'unsupported');
});

test('every navigate the host sends opens a panel by id, nothing else', () => {
  const panels = {};
  for (const name of ['push-payload-agent-approval.json', 'push-payload-herdr-blocked-zh.json',
    'push-payload-host-notification.json']) {
    const url = new URL(fixture(name).notification.navigate);
    const target = openTarget(url.search);
    panels[name] = target;
  }
  // NOTIFY-2: the host id rides along, so the page can link the App there.
  const HOST = '0'.repeat(32);
  assert.deepEqual(panels['push-payload-agent-approval.json'], {panel: 'agent', ids: {ref: 'approval', host: HOST}});
  assert.deepEqual(panels['push-payload-herdr-blocked-zh.json'], {panel: 'herdr',
    ids: {pane: 'w1:p2', session: 'omodachi', agent: 'codex', host: HOST}});
  assert.deepEqual(panels['push-payload-host-notification.json'], {panel: 'notifications', ids: {note: '1789711988810-1'}});
  assert.equal(openTarget('?open=remote'), null);
  assert.equal(openTarget(''), null);
  assert.deepEqual(openTarget('?open=herdr&pane=<script>&ticket=x'), {panel: 'herdr', ids: {}});
  assert.equal(openTarget('?open=herds'), null);
});

test('subscribe is the first thing the tap does', async () => {
  const calls = [];
  const subscription = {toJSON: () => ({endpoint: 'https://web.push.apple.com/x', keys: {p256dh: 'p', auth: 'a'}}),
    unsubscribe: async () => { calls.push('unsubscribe'); return true; }};
  const registration = {pushManager: {
    subscribe: (options) => { calls.push(['subscribe', options.userVisibleOnly, options.applicationServerKey.length]); return Promise.resolve(subscription); },
    getSubscription: async () => subscription}};
  const client = {
    subscribePush: async (body) => { calls.push(['post', body]); return {}; },
    unsubscribePush: async () => { calls.push('delete'); return {}; },
  };
  const prepared = {registration, key: fixture('push-vapid-key.json').vapid_public_key};
  const pending = turnOn(client, prepared, {lang: 'en'});
  assert.deepEqual(calls, [['subscribe', true, 65]]);          // synchronously, before any await
  await pending;
  assert.deepEqual(calls[1], ['post', {endpoint: 'https://web.push.apple.com/x', keys: {p256dh: 'p', auth: 'a'}, expirationTime: null}]);
  await turnOff(client, prepared);
  assert.deepEqual(calls.slice(2), ['unsubscribe', 'delete']);
});

test('prepare reads the registration, the key and any subscription before the tap', async () => {
  const registration = {pushManager: {getSubscription: async () => null}};
  const prepared = await prepare({vapidKey: async () => ({vapid_public_key: 'k'})}, {ready: Promise.resolve(registration)});
  assert.deepEqual(prepared, {registration, key: 'k', existing: null});
});

test('the client speaks the push routes', async () => {
  const sent = [];
  const fetchImpl = async (url, init) => {
    sent.push([init.method, new URL(url).pathname, init.body ? JSON.parse(init.body) : undefined, init.headers.Authorization]);
    return {ok: true, status: 200, json: async () => ({})};
  };
  const client = new HostClient({base: 'https://h.example.ts.net/', token: 'tok', fetchImpl});
  await client.vapidKey();
  await client.subscribePush({endpoint: 'e', keys: {p256dh: 'p', auth: 'a'}});
  await client.prefs();
  await client.setPrefs(3, {lang: 'zh-Hans'});
  await client.unsubscribePush();
  assert.deepEqual(sent, [
    ['GET', '/v1/push/vapid-public-key', undefined, 'Bearer tok'],
    ['POST', '/v1/push/subscriptions', {endpoint: 'e', keys: {p256dh: 'p', auth: 'a'}}, 'Bearer tok'],
    ['GET', '/v1/notifications/prefs', undefined, 'Bearer tok'],
    ['PUT', '/v1/notifications/prefs', {expected_revision: 3, changes: {lang: 'zh-Hans'}}, 'Bearer tok'],
    ['DELETE', '/v1/push/subscriptions', undefined, 'Bearer tok'],
  ]);
});

test('snooze', () => {
  assert.equal(snoozeUntil(0), null);
  assert.equal(snoozeUntil(1, 1_800_000_000_000), 1_800_003_600);
  assert.equal(snoozed({snooze_until: 1_800_003_600}, 1_800_000_000_000), true);
  assert.equal(snoozed({snooze_until: null}), false);
});

// ── the worker ──────────────────────────────────────────────────────────
function worker({endpoint = 'https://fcm.googleapis.com/fcm/send/x', clients = []} = {}) {
  const listeners = {};
  const shown = [];
  const closed = [];
  const opened = [];
  const open = [{tag: 'agent', close: () => closed.push('agent')}];
  const self = {
    location: {href: 'https://omarchy-fixture.tail0000.ts.net/odomo/sw.js'},
    addEventListener: (type, fn) => { listeners[type] = fn; },
    skipWaiting: () => {},
    registration: {
      showNotification: async (title, options) => { shown.push({title, ...options}); },
      getNotifications: async ({tag}) => open.filter((n) => n.tag === tag),
      pushManager: {getSubscription: async () => ({endpoint})},
    },
    clients: {claim: async () => {}, matchAll: async () => clients, openWindow: async (url) => { opened.push(url); }},
  };
  self.self = self;
  vm.runInNewContext(readFileSync(new URL('../public/sw.js', import.meta.url), 'utf8'), {self, URL, Promise, console, atob});
  const fire = async (type, init) => {
    let done;
    const event = {...init, waitUntil: (promise) => { done = promise; }};
    listeners[type](event);
    await done;
  };
  return {fire, shown, closed, opened};
}

const data = (value) => ({json: () => (typeof value === 'string' ? JSON.parse(value) : value)});

test('the worker shows every declarative push, with ids-only links on this host', async () => {
  const sw = worker();
  const message = fixture('push-payload-herdr-blocked-zh.json');
  await sw.fire('push', {data: data(message)});
  assert.equal(sw.shown.length, 1);
  assert.equal(sw.shown[0].title, message.notification.title);
  assert.equal(sw.shown[0].tag, 'herdr-w1-p2');
  assert.equal(sw.shown[0].lang, 'zh-Hans');
  assert.equal(sw.shown[0].data.url, message.notification.navigate);
});

test('the worker shows something for anything else too', async () => {
  const sw = worker();
  await sw.fire('push', {data: data('not json')});
  await sw.fire('push', {data: null});
  await sw.fire('push', {data: data({web_push: 8030, notification: {title: 'x', navigate: 'https://evil.example/'}})});
  assert.deepEqual(sw.shown.map((n) => n.title), ['Omodachi', 'Omodachi', 'x']);
  assert.equal(sw.shown[2].data.url, 'https://omarchy-fixture.tail0000.ts.net/odomo/');
});

test('a retraction closes by tag, and on Apple still shows something', async () => {
  const fcm = worker();
  await fcm.fire('push', {data: data(fixture('push-payload-retract.json'))});
  assert.deepEqual(fcm.closed, ['agent']);
  assert.equal(fcm.shown.length, 0);
  const apple = worker({endpoint: 'https://web.push.apple.com/x'});
  await apple.fire('push', {data: data(fixture('push-payload-retract.json'))});
  assert.equal(apple.shown.length, 1);
});

test('a tap focuses an open Odomo and tells it where, or opens one', async () => {
  const posted = [];
  let focused = 0;
  const odomo = {url: 'https://omarchy-fixture.tail0000.ts.net/odomo/', postMessage: (m) => posted.push(m), focus: async () => { focused++; }};
  const sw = worker({clients: [{url: 'https://omarchy-fixture.tail0000.ts.net/other', postMessage() {}, focus() {}}, odomo]});
  const url = 'https://omarchy-fixture.tail0000.ts.net/odomo/?open=notifications&note=1-1';
  let closedNote = false;
  await sw.fire('notificationclick', {notification: {data: {url}, close: () => { closedNote = true; }}});
  assert.ok(closedNote);
  assert.deepEqual(JSON.parse(JSON.stringify(posted)), [{type: 'omodachi-open', url}]);  // another realm's objects
  assert.equal(focused, 1);
  const none = worker();
  await none.fire('notificationclick', {notification: {data: {url: 'javascript:alert(1)'}, close() {}}});
  assert.deepEqual(none.opened, ['https://omarchy-fixture.tail0000.ts.net/odomo/']);
});
