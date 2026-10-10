// HERDR-11: the App's rules, in Odomo - one connection clock, launchers and
// favourites on the New workspace card, the lighter reads. (HERDR-12 took the
// latest-reply card away.)
import test from 'node:test';
import assert from 'node:assert/strict';
import {en, zh} from '../src/lib/i18n.js';
import {HostClient} from '../src/lib/api.js';
import {
  LOST_MS, TROUBLE_MS, folderExtras, folderToSend, healthWords, isFavourite, isLost, judge, keepCatalog,
  launcherFallback, layoutEventIsMine, mirrorStale, toggled,
} from '../src/lib/herdr11.js';

test('the clock goes amber at four seconds and red at fifteen, naming the cause', () => {
  assert.equal(judge(1000 + 3999, 1000), 'live');
  assert.equal(judge(1000 + TROUBLE_MS, 1000), 'trouble');
  assert.equal(judge(1000 + LOST_MS - 1, 1000, false), 'trouble');
  assert.equal(judge(1000 + LOST_MS, 1000, true), 'unreachable');
  assert.equal(judge(1000 + LOST_MS, 1000, false), 'offline');
  assert.equal(healthWords('live', en, 'omarchy'), null);
  assert.equal(healthWords('trouble', en, 'omarchy'), 'Reconnecting…');
  assert.equal(healthWords('trouble', en, 'omarchy', false), 'Connecting to omarchy…');
  assert.equal(healthWords('unreachable', en, 'omarchy'), 'Can’t reach omarchy');
  assert.equal(healthWords('offline', zh, 'omarchy'), '这台设备没联网');
  assert.equal(healthWords('herdrDown', zh, 'omarchy'), 'omarchy 上的 Herdr 没在运行');
  assert.ok(isLost('offline') && isLost('unreachable') && isLost('herdrDown') && !isLost('trouble'));
});

test('a mirror is current only while the clock is live and its stream is open', () => {
  assert.equal(mirrorStale({health: 'live', observing: true}), false);
  assert.equal(mirrorStale({health: 'trouble', observing: true}), true);
  assert.equal(mirrorStale({health: 'live', observing: false}), true);
});

test('only the panel\'s own session is read again; a catalog held stays when unchanged', () => {
  assert.equal(layoutEventIsMine(null, 'work'), true, 'an older core did not say');
  assert.equal(layoutEventIsMine('work', 'work'), true);
  assert.equal(layoutEventIsMine('debug', 'work'), false);
  const held = {revision: 'r1', entries: [{id: 'a'}]};
  assert.equal(keepCatalog(held, {revision: 'r1', entries: [], unchanged: true}), held);
  const fresh = {revision: 'r2', entries: []};
  assert.equal(keepCatalog(held, fresh), fresh);
  assert.equal(keepCatalog(held, null), held);
  const odd = {revision: 'r9', entries: [], unchanged: true};
  assert.equal(keepCatalog(held, odd), odd, 'unchanged against another revision is not a keep');
});

test('launchers and favourites come from a HERDR-11 core; an older one has neither', () => {
  const extras = folderExtras({favourites: [{path: '/h/notes', display: '~/notes', name: 'notes'}, {}],
    launchers: [{command: 'codex', label: 'codex'}, {command: '', label: 'x'}]});
  assert.deepEqual(extras.launchers, [{command: 'codex', label: 'codex'}]);
  assert.equal(extras.favourites.length, 1);
  assert.deepEqual(folderExtras({recent: []}), {favourites: null, launchers: null});
  assert.equal(launcherFallback([{command: 'codex', label: 'codex'}], 'codex'), 'codex');
  assert.equal(launcherFallback([{command: 'codex', label: 'codex'}], 'claude'), null, 'a dropped one: a plain shell');
  assert.equal(launcherFallback(null, 'codex'), null);
  const notes = {path: '/h/notes', display: '~/notes', name: 'notes'};
  assert.equal(isFavourite([notes], notes), true);
  assert.deepEqual(toggled([notes], notes), []);
  assert.deepEqual(toggled([], notes), [notes]);
  assert.equal(folderToSend('  '), '~');
  assert.equal(folderToSend('~/code'), '~/code');
});

function fakeFetch(answers, seen) {
  return async (url, options) => {
    seen.push({url, options});
    const path = new URL(url).pathname;
    const [status, body, headers = {}] = answers[path] || [404, {error: {code: 'route_unavailable'}}];
    return {status, ok: status >= 200 && status < 300, headers: {get: (name) => headers[name] || null},
      json: async () => body};
  };
}

test('the client sends If-None-Match, keeps a 304, sends the launcher and an empty folder as home', async () => {
  const seen = [];
  const client = new HostClient({base: 'https://h.invalid/', token: 'tok', fetchImpl: fakeFetch({
    '/v1/herdr/layout': [304, null, {ETag: '"omodachi:7"'}],
    '/v1/herdr/sessions/work/workspaces': [200, {selected: 'work', workspace: {pane: 'w3:p1'},
      launch: {command: 'codex', sent: true}}],
    '/v1/herdr/folders/favourites': [200, {favourites: [], launchers: []}],
  }, seen)});
  const layout = await client.herdrLayoutIf('"omodachi:7"');
  assert.equal(layout.data, null);
  assert.equal(seen.at(-1).options.headers['If-None-Match'], '"omodachi:7"');
  await client.herdrCreateWorkspace('work', '  ', null, 'codex');
  assert.deepEqual(JSON.parse(seen.at(-1).options.body), {cwd: '~', launch: 'codex'});
  await client.herdrCreateWorkspace('work', '~/code', 'x');
  assert.deepEqual(JSON.parse(seen.at(-1).options.body), {cwd: '~/code', name: 'x'});
  await client.herdrFavourite('~/notes', true);
  assert.deepEqual(JSON.parse(seen.at(-1).options.body), {path: '~/notes', favourite: true});
});
