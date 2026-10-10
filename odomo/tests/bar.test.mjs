// LITE-1c: Remote is one tap on iPhone and iPad, the bar says what it is, the
// name is in one place, and a browser paired as Lite keeps its pairing.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runnerImport} from 'vite';
import {appleTouch, appLinks, GET_APP, getAppLink} from '../src/lib/applink.js';
import {describe, NAMED_BAR, BAR} from '../src/layout.js';
import {en, zh} from '../src/lib/i18n.js';
import {APP_NAME} from '../src/lib/name.js';
import {dropOldWorker, moveDatabase, moveLocal} from '../src/lib/migrate.js';

const ID = 'a'.repeat(16) + '0123456789abcdef';
const UA = {
  iphone: 'Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1',
  ipadOs: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15',
  crios: 'Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/140.0 Mobile/15E148 Safari/604.1',
  android: 'Mozilla/5.0 (Linux; Android 16; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Mobile Safari/537.36',
  chromeMac: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36',
  firefoxMac: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 15.0; rv:140.0) Gecko/20100101 Firefox/140.0',
  linux: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36',
};

test('iPhone and iPad are told apart from everything else', () => {
  assert.equal(appleTouch({userAgent: UA.iphone, maxTouchPoints: 5}), true);
  assert.equal(appleTouch({userAgent: UA.iphone, maxTouchPoints: 5, standalone: true}), true);
  assert.equal(appleTouch({userAgent: UA.crios, maxTouchPoints: 5}), true);
  assert.equal(appleTouch({userAgent: UA.ipadOs, maxTouchPoints: 5}), true, 'iPadOS Safari says Macintosh');
  assert.equal(appleTouch({userAgent: UA.ipadOs, maxTouchPoints: 0}), false, 'a Mac');
  assert.equal(appleTouch({userAgent: UA.firefoxMac, maxTouchPoints: 0}), false);
  assert.equal(appleTouch({userAgent: UA.android, maxTouchPoints: 5, userAgentData: {platform: 'Android', mobile: true}}), false);
  assert.equal(appleTouch({userAgent: UA.chromeMac, maxTouchPoints: 0, userAgentData: {platform: 'macOS'}}), false);
  // Chromium on a touch Mac-alike still has userAgentData: never the app link.
  assert.equal(appleTouch({userAgent: UA.ipadOs, maxTouchPoints: 5, userAgentData: {platform: 'macOS'}}), false);
  assert.equal(appleTouch({userAgent: UA.linux, maxTouchPoints: 10, userAgentData: {platform: 'Linux'}}), false);
  assert.equal(appleTouch({userAgent: 'x', standalone: true, maxTouchPoints: 5}), true);
  assert.equal(appleTouch({userAgent: 'x', standalone: true, maxTouchPoints: 0}), false);
  assert.equal(appleTouch(null), false);
});

test('a word under each icon where there is room: iPads either way up and desktops, never phones', () => {
  for (const [w, h] of [[820, 1180], [1180, 820], [768, 1024], [1024, 768], [1280, 800], [1366, 1024]]) {
    const layout = describe(w, h);
    assert.equal(layout.named, true, `${w}x${h}`);
    assert.equal(layout.areaWidth, w - NAMED_BAR);
  }
  for (const [w, h] of [[402, 874], [874, 402], [375, 667], [440, 956], [956, 440]]) {
    const layout = describe(w, h);
    assert.equal(layout.named, false, `${w}x${h}`);
    assert.equal(layout.areaWidth, w - BAR);
  }
});

test('the bar names are short and in both languages', () => {
  for (const table of [en, zh]) {
    for (const id of ['menu', 'herdr', 'agent', 'remote', 'notifications', 'settings']) {
      assert.equal(typeof table.bar.names[id], 'string', id);
      // 10px system type in a 62px column: the panels' own names.
      assert.ok(table.bar.names[id].length <= 13, table.bar.names[id]);
    }
    assert.match(table.bar.opensApp, /%s/);
  }
});

const {module: render} = await runnerImport(new URL('./render.jsx', import.meta.url).pathname,
  {root: new URL('..', import.meta.url).pathname, configFile: false, logLevel: 'silent'});

function tags(html, name) {
  return [...html.matchAll(new RegExp('<' + name + ' ([^>]*)>', 'g'))].map(([, attrs]) =>
    Object.fromEntries([...attrs.matchAll(/([a-z-]+)="([^"]*)"/g)].map(([, k, v]) => [k, v.replace(/&amp;/g, '&')])));
}

test('on iPhone and iPad, Remote in the bar is a real anchor to the app, in a new context', () => {
  const href = appLinks('remote', ID).universal;
  for (const [lang, table] of [['en', en], ['zh-Hans', zh]]) {
    const html = render.renderSidebar({remoteHref: href}, lang);
    const remote = tags(html, 'a').find((a) => a['data-testid'] === 'bar-remote');
    assert.ok(remote, 'an <a>');
    assert.equal(remote.href, 'https://omodachi.app/open/remote?host=' + ID + '&from=odomo');
    assert.equal(remote.target, '_blank');
    assert.match(remote.rel, /\bnoopener\b/);
    assert.match(remote.rel, /\bnoreferrer\b/);
    assert.equal(remote['aria-label'], table.bar.opensApp.replace('%s', table.panels.remote));
    assert.ok(!tags(html, 'button').some((b) => b['data-testid'] === 'bar-remote'));
    // The other entries stay buttons.
    for (const id of ['herdr', 'agent', 'notifications', 'settings']) {
      assert.ok(tags(html, 'button').some((b) => b['data-testid'] === 'bar-' + id), id);
    }
  }
});

test('elsewhere Remote stays the panel', () => {
  const html = render.renderSidebar({remoteHref: null});
  assert.ok(tags(html, 'button').some((b) => b['data-testid'] === 'bar-remote' && b['aria-label'] === en.panels.remote));
  assert.deepEqual(tags(html, 'a'), []);
});

test('named: every entry and the logo carry their word; not named: icons only, same accessible names', () => {
  for (const [lang, table] of [['en', en], ['zh-Hans', zh]]) {
    const html = render.renderSidebar({named: true}, lang);
    assert.match(html, /class="sbar sbar--named"/);
    const words = [...html.matchAll(/<span class="bslot__name" aria-hidden="true">([^<]*)<\/span>/g)].map((m) => m[1]);
    assert.deepEqual(words, ['menu', 'remote', 'agent', 'herdr', 'settings', 'notifications'].map((id) => table.bar.names[id]));
  }
  const plain = render.renderSidebar({named: false});
  assert.doesNotMatch(plain, /bslot__name|sbar--named/);
  for (const id of ['remote', 'agent', 'herdr', 'settings', 'notifications']) {
    assert.ok(tags(plain, 'button').some((b) => b['data-testid'] === 'bar-' + id && b['aria-label'] === en.panels[id]));
  }
});

test('Settings has a way to the app and to get it, and never a store address', (t) => {
  // Settings reads the page's own address and asks what the browser can do.
  globalThis.location = {hostname: 'omarchy.example.ts.net', host: 'omarchy.example.ts.net'};
  const hadWindow = 'window' in globalThis;
  if (!hadWindow) globalThis.window = {};
  t.after(() => {
    delete globalThis.location;
    if (!hadWindow) delete globalThis.window;
  });
  for (const [lang, table] of [['en', en], ['zh-Hans', zh]]) {
    const html = render.renderSettings({hostId: ID}, lang);
    const links = tags(html, 'a');
    const open = links.find((a) => a['data-testid'] === 'settings-app-open');
    const get = links.find((a) => a['data-testid'] === 'settings-app-get');
    assert.equal(open.href, 'https://omodachi.app/open/host?host=' + ID + '&from=odomo');
    // ODOMO-CARD: "Get the app" names this computer and Odomo too.
    assert.equal(get.href, getAppLink(ID));
    assert.equal(get.href, GET_APP + '?host=' + ID + '&from=odomo');
    assert.equal(GET_APP, 'https://omodachi.app/open/');
    for (const a of [open, get]) {
      assert.equal(a.target, '_blank');
      assert.match(a.rel, /\bnoopener\b/);
    }
    assert.ok(html.includes('>' + table.settings.appOpen + '<') && html.includes('>' + table.settings.appGet + '<'));
    assert.doesNotMatch(html, /apps\.apple\.com|testflight\.apple\.com/);
  }
  const bareLinks = tags(render.renderSettings({hostId: null}), 'a');
  assert.equal(bareLinks.find((a) => a['data-testid'] === 'settings-app-open').href, 'https://omodachi.app/open/host?from=odomo');
  assert.equal(bareLinks.find((a) => a['data-testid'] === 'settings-app-get').href, 'https://omodachi.app/open/?from=odomo');
});

test('the name is in one place: name.js, the built index.html and the manifest', () => {
  assert.equal(APP_NAME, 'Odomo');
  const source = readFileSync(new URL('../src/lib/i18n.js', import.meta.url), 'utf8')
    .split('\n').filter((line) => !line.trim().startsWith('//')).join('\n');
  assert.doesNotMatch(source, /Odomo|Lite\b/, 'i18n.js fills the name in from name.js');
  for (const table of [en, zh]) {
    const all = JSON.stringify(table);
    assert.doesNotMatch(all, /\bLite\b|Omodachi Lite/);
    assert.ok(table.pair.lead.startsWith(APP_NAME));
  }
  const page = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  assert.doesNotMatch(page, /Odomo/);
  assert.equal(page.match(/%APP_NAME%/g).length, 2);
  const built = new URL('../../src/omodachi_core/data/odomo/', import.meta.url);
  const html = readFileSync(new URL('index.html', built), 'utf8');
  assert.match(html, new RegExp('<title>' + APP_NAME + '</title>'));
  assert.match(html, new RegExp('name="apple-mobile-web-app-title" content="' + APP_NAME + '"'));
  const manifest = JSON.parse(readFileSync(new URL('manifest.webmanifest', built), 'utf8'));
  assert.equal(manifest.name, APP_NAME);
  assert.equal(manifest.short_name, APP_NAME);
});

function fakeLocal(entries) {
  const map = new Map(Object.entries(entries));
  return {
    get length() { return map.size; },
    key: (i) => [...map.keys()][i] ?? null,
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    map,
  };
}

test('a browser paired as Lite keeps its pins and language, never over newer ones', () => {
  const local = fakeLocal({'omodachi-lite.pins.menu': '["a"]', 'omodachi-lite.language': '"zh-Hans"',
    'odomo.language': '"en"', 'other.key': '1'});
  assert.equal(moveLocal(local), 1);
  assert.deepEqual(Object.fromEntries(local.map), {'odomo.language': '"en"', 'other.key': '1', 'odomo.pins.menu': '["a"]'});
  assert.equal(moveLocal(local), 0);
  assert.equal(moveLocal(null), 0);
});

test('the old /lite/ worker is unregistered, nothing else', async () => {
  const gone = [];
  const reg = (scope) => ({scope, unregister: async () => { gone.push(scope); return true; }});
  const container = {getRegistrations: async () => [reg('https://h.ts.net/lite/'), reg('https://h.ts.net/odomo/'),
    reg('https://h.ts.net/literal/')]};
  assert.equal(await dropOldWorker(container), 1);
  assert.deepEqual(gone, ['https://h.ts.net/lite/']);
  assert.equal(await dropOldWorker(null), 0);
});

test('no old database: nothing is read or deleted', async () => {
  const calls = [];
  const idb = {databases: async () => [{name: 'odomo'}], open: () => calls.push('open'), deleteDatabase: () => calls.push('delete')};
  assert.equal(await moveDatabase(idb, {get: async () => null, set: async () => {}}), 0);
  assert.deepEqual(calls, []);
  assert.equal(await moveDatabase({}, null), 0, 'a browser without indexedDB.databases()');
});
