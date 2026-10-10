// TAIL-3b L8: Odomo's links into the Omodachi app.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runnerImport} from 'vite';
import {APP_ORIGIN, appLinks, getAppLink, hostIdOf} from '../src/lib/applink.js';
import {en, zh} from '../src/lib/i18n.js';
import {HostClient} from '../src/lib/api.js';
import {pollClaim} from '../src/lib/pairing.js';

const fixture = (name) => JSON.parse(readFileSync(new URL('../../contracts/fixtures/' + name, import.meta.url)));
const ID = 'a'.repeat(16) + '0123456789abcdef';

const F = '&from=odomo';

test('the two paths the app routes, with the host id, the from flag and nothing else', () => {
  assert.deepEqual(appLinks('remote', ID), {universal: 'https://omodachi.app/open/remote?host=' + ID + F,
    scheme: 'omodachi://open/remote?host=' + ID + F});
  assert.deepEqual(appLinks('host', ID), {universal: 'https://omodachi.app/open/host?host=' + ID + F,
    scheme: 'omodachi://open/host?host=' + ID + F});
  for (const target of ['remote', 'host', 'agent', 'herdr']) {
    for (const href of Object.values(appLinks(target, ID))) {
      const url = new URL(href);
      assert.deepEqual([...url.searchParams.keys()], ['host', 'from']);
      assert.equal(url.searchParams.get('host'), ID);
      assert.equal(url.searchParams.get('from'), 'odomo');
      assert.equal(url.hash, '');
    }
  }
  assert.equal(new URL(appLinks('remote', ID).universal).origin, APP_ORIGIN);
});

test('the host id is percent-encoded; without one the flag is all there is', () => {
  const odd = 'a b&c=d/e?f#g';
  const {universal, scheme} = appLinks('remote', odd);
  assert.equal(universal, 'https://omodachi.app/open/remote?host=a%20b%26c%3Dd%2Fe%3Ff%23g' + F);
  assert.equal(new URL(universal).searchParams.get('host'), odd);
  assert.equal(new URL(scheme).searchParams.get('host'), odd);
  assert.equal(new URL(scheme).searchParams.get('from'), 'odomo');
  for (const missing of [null, undefined, '']) {
    assert.deepEqual(appLinks('remote', missing), {universal: 'https://omodachi.app/open/remote?from=odomo',
      scheme: 'omodachi://open/remote?from=odomo'});
    assert.deepEqual(appLinks('host', missing), {universal: 'https://omodachi.app/open/host?from=odomo',
      scheme: 'omodachi://open/host?from=odomo'});
  }
  // Anything but the routes is the computer itself, never a new path.
  assert.equal(appLinks('../x', ID).universal, 'https://omodachi.app/open/host?host=' + ID + F);
});

// ODOMO-CARD: "Get the app" carries the same two things, and only a host id
// in the contract shape.
test('Get the app names this computer and Odomo, nothing else', () => {
  assert.equal(getAppLink(ID), 'https://omodachi.app/open/?host=' + ID + F);
  for (const bad of [null, undefined, '', 'a b', ID + '0', 'ABCDEF' + ID.slice(6)]) {
    assert.equal(getAppLink(bad), 'https://omodachi.app/open/?from=odomo', String(bad));
  }
  const url = new URL(getAppLink(ID));
  assert.deepEqual([...url.searchParams.keys()], ['host', 'from']);
  assert.equal(url.pathname, '/open/');
});

test('a host id is only ever what core sent, in the contract shape', () => {
  assert.equal(hostIdOf(ID), ID);
  for (const bad of [null, undefined, '', 42, {}, 'ABCDEF' + ID.slice(6), ID + '0', ID.slice(1), 'x'.repeat(32)]) {
    assert.equal(hostIdOf(bad), null, String(bad));
  }
  assert.equal(hostIdOf(fixture('health.json').host_id), fixture('health.json').host_id);
});

test('the claim keeps the host id with the credential', async () => {
  const claim = fixture('pairing-claim-web.json');
  const fetchImpl = async () => ({ok: true, status: 200, json: async () => claim});
  const client = new HostClient({base: 'https://h/', fetchImpl});
  const result = await pollClaim(client, {request_id: 'r', request_secret: 's'}, 0);
  assert.equal(result.state, 'claimed');
  assert.equal(result.credential.host_id, claim.host_id);
  const none = async () => ({ok: true, status: 200, json: async () => ({...claim, host_id: null})});
  const bare = await pollClaim(new HostClient({base: 'https://h/', fetchImpl: none}), {request_id: 'r', request_secret: 's'}, 0);
  assert.equal(bare.credential.host_id, null);
});

test('the link words exist in both languages, and are short', () => {
  for (const table of [en, zh]) {
    for (const key of ['openRemote', 'haveApp', 'remoteBody']) assert.equal(typeof table.next[key], 'string', key);
    assert.ok(table.next.openRemote.length <= 24 && table.next.haveApp.length <= 32);
    assert.doesNotMatch(table.next.remoteBody, /will link/);
  }
});

const {module: render} = await runnerImport(new URL('./render.jsx', import.meta.url).pathname,
  {root: new URL('..', import.meta.url).pathname, configFile: false, logLevel: 'silent'});

function anchors(html) {
  return [...html.matchAll(/<a ([^>]*)>([^<]*)<\/a>/g)].map(([, attrs, text]) => ({
    text, ...Object.fromEntries([...attrs.matchAll(/([a-z-]+)="([^"]*)"/g)].map(([, k, v]) => [k, v.replace(/&amp;/g, '&')])),
  }));
}

test('Remote: the universal link first, the scheme second, both in a new context', () => {
  for (const [lang, table] of [['en', en], ['zh-Hans', zh]]) {
    const links = anchors(render.renderNext({which: 'remote', hostId: ID}, lang));
    assert.deepEqual(links.map((a) => a.href), ['https://omodachi.app/open/remote?host=' + ID + F,
      'omodachi://open/remote?host=' + ID + F]);
    assert.deepEqual(links.map((a) => a.text), [table.next.openRemote, table.next.haveApp]);
    for (const a of links) {
      assert.equal(a.target, '_blank');
      assert.match(a.rel, /\bnoopener\b/);
      assert.match(a.rel, /\bnoreferrer\b/);
    }
  }
  const bare = anchors(render.renderNext({which: 'remote', hostId: null}));
  assert.deepEqual(bare.map((a) => a.href), ['https://omodachi.app/open/remote?from=odomo', 'omodachi://open/remote?from=odomo']);
});

// NOTIFY-2 / ODOMO-2: Agent and Herdr are real panels now. A push lands on
// the panel, says what was waiting in words (never a pane id), and keeps
// "Open in Omodachi" as a quiet second way, to that very place.
test('Agent and Herdr keep a quiet way into the app', () => {
  for (const [lang, table] of [['en', en], ['zh-Hans', zh]]) {
    const agent = anchors(render.renderAgent({hostId: ID}, lang));
    assert.deepEqual(agent.map((a) => a.href), ['https://omodachi.app/open/agent?host=' + ID + F]);
    assert.deepEqual(agent.map((a) => a.text), [table.next.openApp]);
    const herdr = anchors(render.renderHerdr({hostId: ID}, lang));
    assert.deepEqual(herdr.map((a) => a.href), ['https://omodachi.app/open/herdr?host=' + ID + F]);
    for (const a of [...agent, ...herdr]) {
      assert.equal(a.target, '_blank');
      assert.match(a.rel, /\bnoopener\b/);
      assert.match(a.rel, /\bnoreferrer\b/);
    }
  }
});

test('opened from a push: what was waiting is at the top, with the way to that very place', () => {
  const html = render.renderHerdr({hostId: ID, focus: {session: 'work', pane: 'w2:p3'}});
  const links = anchors(html);
  assert.equal(links.length, 1);                                // one way in, not two
  assert.equal(links[0].href, 'https://omodachi.app/open/herdr?host=' + ID + '&session=work&pane=w2%3Ap3' + F);
  assert.match(html, /data-testid="herdr-from-push"/);
  assert.match(html, new RegExp(en.next.waitingHerdr));
  // HERDR-4: who and where in words; the pane id stays in the link only.
  assert.match(html, /It is in the work session\./);
  assert.doesNotMatch(html.replace(/href="[^"]*"/g, ''), /w2:p3|w2%3Ap3/);
  for (const [lang, said] of [['en', /It is claude, in the work session\./], ['zh-Hans', /是 claude，在 work 会话里。/]]) {
    const pushed = render.renderHerdr({hostId: ID, focus: {session: 'work', pane: 'w2:p3', agent: 'claude'}}, lang);
    assert.match(pushed, said, lang);
    assert.doesNotMatch(pushed.replace(/href="[^"]*"/g, ''), /w2:p3|w2%3Ap3/, lang);
  }
  for (const [ref, key] of [['approval', 'waitingApproval'], ['input', 'waitingInput'], ['done', 'waitingDone']]) {
    for (const [lang, table] of [['en', en], ['zh-Hans', zh]]) {
      const agent = render.renderAgent({hostId: ID, focus: {ref}}, lang);
      assert.match(agent, /data-testid="agent-from-push"/);
      assert.ok(agent.includes('>' + table.next[key] + '<'), key);
      assert.equal(anchors(agent)[0].href, 'https://omodachi.app/open/agent?host=' + ID + F);
    }
  }
  // An unknown ref says nothing it cannot back up.
  assert.doesNotMatch(render.renderAgent({hostId: ID, focus: {ref: 'other'}}), /agent-from-push/);
});

test('a session or pane outside Herdr\'s alphabet never reaches a link', () => {
  for (const where of [{session: '--takeover', pane: 'w1:p1'}, {session: 'a/b', pane: 'w1:p1'}, {session: '', pane: 'w1:p1'}]) {
    assert.equal(appLinks('herdr', ID, where).universal, 'https://omodachi.app/open/herdr?host=' + ID + F);
  }
  assert.equal(appLinks('herdr', ID, {session: 'work', pane: 'x;y'}).universal,
    'https://omodachi.app/open/herdr?host=' + ID + '&session=work' + F);
  assert.equal(appLinks('herdr', ID, {pane: 'w1:p1'}).universal, 'https://omodachi.app/open/herdr?host=' + ID + F);
  // Only Herd's link carries a place.
  assert.equal(appLinks('agent', ID, {session: 'work', pane: 'w1:p1'}).universal, 'https://omodachi.app/open/agent?host=' + ID + F);
});

test('the push fixtures open the right panel and the right place in the app', async () => {
  const {openTarget} = await import('../src/lib/push.js');
  const herd = openTarget(new URL(fixture('push-payload-herdr-blocked-zh.json').notification.navigate).search);
  assert.equal(herd.panel, 'herdr');
  assert.deepEqual(herd.ids, {pane: 'w1:p2', session: 'omodachi', agent: 'codex', host: '0'.repeat(32)});
  assert.equal(openTarget('?open=herdr&agent=a%20b').ids.agent, undefined);
  assert.equal(openTarget('?open=herdr&agent=' + 'x'.repeat(40)).ids.agent, undefined);
  // ODOMO-2: a push from before (or an installed worker from then) says herd.
  assert.deepEqual(openTarget('?open=herd&session=work&pane=w1%3Ap1'), {panel: 'herdr', ids: {session: 'work', pane: 'w1:p1'}});
  const agent = openTarget(new URL(fixture('push-payload-agent-approval.json').notification.navigate).search);
  assert.deepEqual(agent, {panel: 'agent', ids: {ref: 'approval', host: '0'.repeat(32)}});
  assert.equal(openTarget('?open=agent&host=ZZ').ids.host, undefined);
  assert.equal(appLinks('herdr', herd.ids.host, herd.ids).universal,
    'https://omodachi.app/open/herdr?host=' + '0'.repeat(32) + '&session=omodachi&pane=w1%3Ap2' + F);
});

test('the NOTIFY-2 words exist in both languages, and the placeholders are gone', () => {
  for (const table of [en, zh]) {
    for (const key of ['openApp', 'waitingApproval', 'waitingInput', 'waitingDone', 'waitingHerdr',
      'waitingWhereAgent', 'waitingWhereSession']) assert.equal(typeof table.next[key], 'string', key);
    assert.equal((table.next.waitingWhereAgent.match(/%s/g) || []).length, 2);
    assert.equal((table.next.waitingWhereSession.match(/%s/g) || []).length, 1);
    assert.ok(table.next.openApp.length <= 24);
    assert.doesNotMatch(table.push.quietDetail, /connected|连着/);
  }
});

test('a menu row only the app runs links to the app on this computer', () => {
  // A pinned row is drawn without opening its group; pins are read from
  // localStorage, which Node lacks, so this one is lent for the render.
  const pins = {'odomo.pins.menu': JSON.stringify(['install.demo', 'omodachi.desktop'])};
  globalThis.location = {hostname: 'host.example'};
  globalThis.localStorage = {getItem: (key) => pins[key] ?? null, setItem() {}};
  try {
    const catalog = fixture('catalog.json');
    const links = anchors(render.renderMenu({catalog, state: null, hostId: ID}));
    assert.ok(links.length >= 2);
    for (const a of links) {
      assert.equal(a.href, 'https://omodachi.app/open/host?host=' + ID + F);
      assert.equal(a.text, en.menu.inApp);
      assert.equal(a.target, '_blank');
      assert.match(a.rel, /\bnoopener\b/);
    }
    for (const a of anchors(render.renderMenu({catalog, state: null, hostId: null}))) {
      assert.equal(a.href, 'https://omodachi.app/open/host?from=odomo');
    }
  } finally {
    delete globalThis.localStorage;
    delete globalThis.location;
  }
});
