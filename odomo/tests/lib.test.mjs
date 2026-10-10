// LITE-1a: Odomo's behaviour that does not need a browser, against core's own
// contract fixtures wherever one exists (../contracts/fixtures).
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {en, zh, pickLanguage, fill} from '../src/lib/i18n.js';
import {browserLabel, newDeviceId} from '../src/lib/device.js';
import {themeVariables, rgb} from '../src/lib/theme.js';
import {buildTree, action, needsConfirm, confirmTap, search, shortcutRows, pathOf} from '../src/lib/catalog.js';
import {merge, applyEvent, workspaces} from '../src/lib/state.js';

const fixture = (name) => JSON.parse(readFileSync(new URL('../../contracts/fixtures/' + name, import.meta.url)));

function keys(value, prefix = '') {
  return Object.entries(value).flatMap(([k, v]) => (typeof v === 'object' ? keys(v, prefix + k + '.') : [prefix + k]));
}

test('both languages have exactly the same strings', () => {
  assert.deepEqual(keys(zh).sort(), keys(en).sort());
  for (const key of keys(en)) {
    const pick = (table) => key.split('.').reduce((v, k) => v[k], table);
    assert.equal((pick(en).match(/%s/g) || []).length, (pick(zh).match(/%s/g) || []).length, key);
  }
});

test('language: Chinese for zh-*, English otherwise, Settings wins', () => {
  assert.equal(pickLanguage(['zh-Hans-CN', 'en']), 'zh-Hans');
  assert.equal(pickLanguage(['zh-TW']), 'zh-Hans');
  assert.equal(pickLanguage(['fr-FR', 'en-GB']), 'en');
  assert.equal(pickLanguage(['de']), 'en');
  assert.equal(pickLanguage(['zh-CN'], 'en'), 'en');
  assert.equal(pickLanguage([], 'klingon'), 'en');
  assert.equal(fill('%s of %s', 1, 2), '1 of 2');
});

test('the approval card label comes from the user agent', () => {
  const iphone = 'Mozilla/5.0 (iPhone; CPU iPhone OS 26_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5 Mobile/15E148 Safari/604.1';
  const ipad = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5 Safari/605.1.15';
  const chrome = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36';
  const android = 'Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0 Mobile Safari/537.36';
  const firefox = 'Mozilla/5.0 (X11; Linux x86_64; rv:150.0) Gecko/20100101 Firefox/150.0';
  assert.equal(browserLabel(iphone), 'Safari on iPhone');
  assert.equal(browserLabel(ipad, 5), 'Safari on iPad');
  assert.equal(browserLabel(ipad, 0), 'Safari on Mac');
  assert.equal(browserLabel(chrome), 'Chrome on Mac');
  assert.equal(browserLabel(android), 'Chrome on Android');
  assert.equal(browserLabel(firefox), 'Firefox on Linux');
  assert.match(newDeviceId(), /^odomo-[0-9a-f]{16}$/);
  // core's device id pattern
  assert.match(newDeviceId(), /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/);
});

test('the host theme becomes the stylesheet variables', () => {
  const theme = fixture('theme.json');
  const vars = themeVariables(theme);
  assert.equal(vars['--om-bg'], theme.colors.background);
  assert.equal(vars['--om-accent'], theme.colors.accent);
  assert.equal(vars['--om-sec'], theme.colors.light_foreground);
  assert.equal(vars['--fgr'], rgb(theme.colors.foreground));
  assert.equal(rgb('#1a1b26'), '26, 27, 38');
  // nothing that is not a colour gets through to CSS
  assert.deepEqual(themeVariables({colors: {background: 'red;}body{display:none', accent: 'url(x)'}}), {});
  assert.deepEqual(themeVariables(null), {});
});

test('the menu tree hangs off root, and each row knows what a tap does', () => {
  const catalog = fixture('catalog.json');
  const tree = buildTree(catalog);
  const top = tree.childrenOf('root').map((row) => row.id);
  assert.ok(top.includes('apps') && top.includes('learn'));
  for (const id of top) assert.equal(tree.byId.get(id).parent_id, 'root');
  assert.equal(action({route: {route: 'host', supported: true, ready: true}}), 'run');
  assert.equal(action({route: {route: 'host', supported: false, ready: false}}), 'unavailable');
  assert.equal(action({route: {route: 'native', native_view: 'shortcuts'}}), 'keybindings');
  assert.equal(action({route: {route: 'terminal', supported: true, ready: true}}), 'app');
  assert.equal(action({route: {route: 'desktop'}}), 'app');
  assert.equal(action(tree.byId.get('learn.keybindings')), 'keybindings');
  assert.equal(needsConfirm({route: {confirm: true}}), true);
  assert.equal(needsConfirm({route: {}}), false);
  const hits = search(tree, 'keyb');
  assert.ok(hits.some((row) => row.id === 'learn.keybindings'));
  assert.equal(pathOf(tree, 'learn', 'Omarchy menu'), 'Learn');
  // a row hidden by its condition is not drawn; an unknown condition is (MENU-3)
  const hidden = buildTree({entries: [{id: 'a', label: 'A', parent_id: 'root', visible: false},
    {id: 'b', label: 'B', parent_id: 'root', visible: null, conditions: {when: {status: 'unknown'}}},
    {id: 'c', label: 'C', parent_id: 'gone', visible: true}]});
  assert.deepEqual(hidden.childrenOf('root').map((row) => row.id), ['b']);
  assert.equal(hidden.byId.has('c'), false);
});

test('a confirm row takes two taps inside the window', () => {
  let d = confirmTap(null, 'system.reboot', true, 1000);
  assert.deepEqual(d, {send: false, armed: {id: 'system.reboot', until: 4000}});
  assert.equal(confirmTap(d.armed, 'system.reboot', true, 3999).send, true);
  assert.equal(confirmTap(d.armed, 'system.reboot', true, 4001).send, false);
  assert.equal(confirmTap(d.armed, 'other', true, 2000).send, false);
  assert.deepEqual(confirmTap(d.armed, 'about', false, 2000), {send: true, armed: null});
});

test('keybinding rows: drawn unless hidden, in the host order', () => {
  const rows = shortcutRows(fixture('shortcuts.json'));
  assert.ok(rows.length > 3);
  assert.ok(rows.every((row) => row.hidden !== true));
  for (let i = 1; i < rows.length; i++) assert.ok((rows[i - 1].order ?? 0) <= (rows[i].order ?? 0));
});

test('state: the snapshot, then each event merged the way core merged it', () => {
  const snapshot = fixture('wss-snapshot.json').state;
  const next = applyEvent(snapshot, {type: 'workspace.changed', payload: {workspace: {active: 3}, revision: 99}});
  assert.equal(next.workspace.active, 3);
  assert.equal(next.revision, 99);
  assert.deepEqual(next.workspace.items, snapshot.workspace.items);
  assert.equal(applyEvent(snapshot, {type: 'notification.posted', payload: {id: 'x'}}), snapshot);
  const withCatalog = applyEvent(snapshot, {type: 'state.changed', payload: {catalog: {entries: []}, revision: 5}});
  assert.equal(withCatalog.catalog, snapshot.catalog);
  assert.ok(workspaces(snapshot).length >= 1);
  assert.deepEqual(merge({a: {b: 1, c: 2}}, {a: {b: 3}}), {a: {b: 3, c: 2}});
});
