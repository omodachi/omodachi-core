// HERDR-6: a new Herdr workspace from Odomo, in a folder the person picks -
// the App's card. The client's two paths, the card's own early words, the
// rows core offers (from the contract's own fixtures), the card as drawn in
// both languages, and the "+" at the end of the workspaces.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runnerImport} from 'vite';
import * as H from '../src/lib/herdr.js';
import {HostClient, ApiError} from '../src/lib/api.js';
import {en, zh, fill} from '../src/lib/i18n.js';
import {herdrReason} from '../src/lib/reasons.js';

const fixture = (name) => JSON.parse(readFileSync(new URL('../../contracts/fixtures/' + name, import.meta.url)));

function recorder() {
  const calls = [];
  const client = new HostClient({base: 'https://h.example/', token: 't', fetchImpl: async (url, init) => {
    calls.push({url, method: init.method, body: init.body ? JSON.parse(init.body) : undefined});
    return {ok: true, status: 200, json: async () => ({})};
  }});
  return {client, calls};
}

test('the client: one POST into the session, one GET for folders; the folder is the person\'s own text', async () => {
  const {client, calls} = recorder();
  await client.herdrCreateWorkspace('h6test', '~/h6test/proj', 'proj');
  await client.herdrCreateWorkspace('h6test', '~/code', null);
  await client.herdrFolders();
  await client.herdrFolders('~/Work & play', false);
  await client.herdrFolders('~/Work', true);
  assert.deepEqual(calls.map((c) => c.method + ' ' + new URL(c.url).pathname + new URL(c.url).search), [
    'POST /v1/herdr/sessions/h6test/workspaces', 'POST /v1/herdr/sessions/h6test/workspaces',
    'GET /v1/herdr/folders', 'GET /v1/herdr/folders?under=%7E%2FWork+%26+play', 'GET /v1/herdr/folders?under=%7E%2FWork&hidden=1']);
  assert.deepEqual(calls[0].body, {cwd: '~/h6test/proj', name: 'proj'});
  assert.deepEqual(calls[1].body, {cwd: '~/code'}, 'no name: Herdr names it after its folder');
  assert.equal(new URL(calls[3].url).searchParams.get('under'), '~/Work & play');
  // Never a focus, never a session name that is not one.
  for (const call of calls) assert.ok(!('focus' in (call.body || {})));
  for (const bad of ['../x', '-x', '', null]) assert.throws(() => client.herdrCreateWorkspace(bad, '~'), ApiError);
});

test('the card\'s own early words are the codes core would answer with', () => {
  assert.equal(H.folderProblem('~/Work'), null);
  assert.equal(H.folderProblem('/etc'), null, 'outside home is core\'s to say, with its reason');
  for (const bad of ['', '   ', 'a\nb', 'x\u0000', 'x'.repeat(4097)]) assert.equal(H.folderProblem(bad), 'invalid_folder', JSON.stringify(bad).slice(0, 12));
  assert.equal(H.workspaceNameProblem(''), null);
  assert.equal(H.workspaceNameProblem('项目 2'), null);
  assert.equal(H.workspaceNameProblem('x'.repeat(64)), null);
  for (const bad of ['x'.repeat(65), 'a\tb']) assert.equal(H.workspaceNameProblem(bad), 'invalid_workspace_name');
});

test('the rows core offers, read from the contract\'s own fixtures', () => {
  const offered = H.folderRows(fixture('herdr-folders.json'));
  assert.deepEqual(offered.recent.map((row) => row.display), ['~/Work/omodachi', '~/code']);
  assert.deepEqual(offered.common.map((row) => row.display), ['~', '~/Work', '~/Projects', '~/code']);
  // A browse answer has no suggestions, an older core none at all.
  assert.deepEqual(H.folderRows(fixture('herdr-folders-browse.json')), {recent: [], common: []});
  assert.deepEqual(H.folderRows(null), {recent: [], common: []});
  const made = fixture('herdr-workspace-create.json');
  assert.ok(H.PANE.test(made.workspace.pane), 'the pane the browser lands on');
  assert.equal(made.workspace.focused, false);
});

test('the More menu has it, and it needs no pane: an empty session can make its first', () => {
  assert.ok(H.MENU.includes('newWorkspace'));
  assert.deepEqual(H.controlRequest('newWorkspace', null, null), {kind: 'newWorkspace'});
  for (const table of [en, zh]) assert.equal(typeof table.herdr.newWorkspace, 'string');
});

test('every refusal is a sentence, in both languages', () => {
  for (const code of ['invalid_folder', 'folder_outside_home', 'folder_private', 'folder_not_found', 'folder_unreadable',
    'invalid_workspace_name']) {
    for (const table of [en, zh]) {
      const text = herdrReason(table, code);
      assert.ok(text && !text.includes(code), code);
    }
  }
  assert.equal(herdrReason(en, fixture('http-error-herdr-folder-private.json').error.code), en.reasons.herdr.folder_private);
  assert.match(fill(en.herdr.wsDetail, 'h6test'), /h6test/);
  assert.match(fill(zh.herdr.wsDetail, 'h6test'), /h6test/);
});

const {module: render} = await runnerImport(new URL('./render.jsx', import.meta.url).pathname,
  {root: new URL('..', import.meta.url).pathname, configFile: false, logLevel: 'silent'});

const noop = () => {};
function store(card) {
  return {selectedSession: 'h6test', ownedSession: 'omodachi', clearNewWorkspaceError: noop, browseFolder: () => Promise.resolve(null),
    closeBrowse: noop, createWorkspace: noop, cancelNewWorkspace: noop,
    newWorkspace: {open: true, loading: false, recent: [], common: [], browse: null, browsing: false, hidden: false,
      creating: false, error: null, ...card}};
}

test('the card: recent and common folders, the path, Browse, an optional name, Create', () => {
  const folders = fixture('herdr-folders.json');
  for (const [lang, table] of [['en', en], ['zh-Hans', zh]]) {
    const html = render.renderNewWorkspace(store({recent: folders.recent, common: folders.common}), lang);
    assert.match(html, /data-testid="herdr-new-workspace"/);
    for (const id of ['path', 'browse', 'name', 'create', 'cancel']) assert.match(html, new RegExp('data-testid="herdr-new-workspace-' + id + '"'));
    assert.match(html, /data-testid="herdr-folder-recent-~\/Work\/omodachi"/);
    assert.match(html, /data-testid="herdr-folder-common-~"/);
    assert.ok(html.includes(table.herdr.wsRecent) && html.includes(table.herdr.wsCommon));
    assert.ok(html.includes(fill(table.herdr.wsDetail, 'h6test').replace(/'/g, '&#x27;')));
    assert.doesNotMatch(html, /herdr-folder-browser/);
  }
});

test('the card browsing one level, and saying why not in words', () => {
  const browse = fixture('herdr-folders-browse.json').browse;
  const html = render.renderNewWorkspace(store({browse, error: 'folder_private'}));
  assert.match(html, /data-testid="herdr-folder-browser"/);
  assert.match(html, /data-testid="herdr-folder-here">~\/Work</);
  assert.match(html, /data-testid="herdr-folder-omodachi"/);
  assert.match(html, /data-testid="herdr-folder-notes"/);
  assert.doesNotMatch(html, /herdr-folder-\.cache/);
  assert.match(html, /data-testid="herdr-folder-up"/);
  assert.match(html, /data-testid="herdr-new-workspace-error"/);
  assert.ok(html.includes(en.reasons.herdr.folder_private));
  const top = render.renderNewWorkspace(store({browse: {...browse, parent: null, dirs: [], truncated: true}}), 'zh-Hans');
  assert.match(top, /data-testid="herdr-folder-up" disabled=""/, 'the home folder has no Up');
  assert.ok(top.includes(zh.herdr.wsEmpty) && top.includes(zh.herdr.wsTruncated));
});

test('a small "+" ends the workspaces, named for a screen reader', () => {
  for (const [lang, table] of [['en', en], ['zh-Hans', zh]]) {
    const html = render.renderHerdr({hostId: 'h'}, lang);
    assert.match(html, /data-testid="herdr-workspace-new"/);
    assert.ok(html.includes('aria-label="' + table.herdr.newWorkspace + '"'));
  }
});
