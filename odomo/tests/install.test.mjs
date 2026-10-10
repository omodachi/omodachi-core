// LITE-1e: where Odomo runs decides whether it asks to be installed, and on
// iPhone and iPad (outside the Home Screen app) the install screen is all
// there is - no pairing in the tab.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runnerImport} from 'vite';
import {appleDevice, installed, installGate, platformOf, promptStore, readInstall} from '../src/lib/install.js';
import {en, zh} from '../src/lib/i18n.js';
import {APP_NAME} from '../src/lib/name.js';

const UA = {
  iphone: 'Mozilla/5.0 (iPhone; CPU iPhone OS 26_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5 Mobile/15E148 Safari/604.1',
  ipadOs: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5 Safari/605.1.15',
  ipadMobile: 'Mozilla/5.0 (iPad; CPU OS 27_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0 Mobile/15E148 Safari/604.1',
  crios: 'Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/140.0 Mobile/15E148 Safari/604.1',
  android: 'Mozilla/5.0 (Linux; Android 16; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Mobile Safari/537.36',
  chromeMac: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36',
  safariMac: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15',
  firefoxLinux: 'Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0',
  linux: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36',
};

// A window as far as the decision reads one: the navigator and matchMedia.
function env(nav, modes = []) {
  return {navigator: nav, matchMedia: (query) => ({matches: modes.some((mode) => query === '(display-mode: ' + mode + ')')})};
}

test('the platform: iPhone and iPad (whatever the browser says), Android, everything else', () => {
  assert.equal(platformOf({userAgent: UA.iphone, maxTouchPoints: 5}), 'ios');
  assert.equal(platformOf({userAgent: UA.ipadOs, maxTouchPoints: 5}), 'ios', 'iPadOS Safari says Macintosh');
  assert.equal(platformOf({userAgent: UA.ipadMobile, maxTouchPoints: 5}), 'ios');
  assert.equal(platformOf({userAgent: UA.crios, maxTouchPoints: 5}), 'ios', 'Chrome on iOS is WebKit too');
  assert.equal(platformOf({userAgent: UA.android, maxTouchPoints: 5, userAgentData: {platform: 'Android', mobile: true}}), 'android');
  assert.equal(platformOf({userAgent: UA.android, maxTouchPoints: 5}), 'android');
  assert.equal(platformOf({userAgent: UA.safariMac, maxTouchPoints: 0}), 'desktop', 'a Mac has no touch screen');
  assert.equal(platformOf({userAgent: UA.chromeMac, maxTouchPoints: 0, userAgentData: {platform: 'macOS'}}), 'desktop');
  assert.equal(platformOf({userAgent: UA.ipadOs, maxTouchPoints: 5, userAgentData: {platform: 'macOS'}}), 'desktop',
    'Chromium on a touch Mac-alike has userAgentData');
  assert.equal(platformOf({userAgent: UA.linux, maxTouchPoints: 10, userAgentData: {platform: 'Linux'}}), 'desktop');
  assert.equal(platformOf({userAgent: UA.firefoxLinux, maxTouchPoints: 0}), 'desktop');
  assert.equal(platformOf(null), 'desktop');
  assert.equal(appleDevice({userAgent: UA.iphone}), 'iPhone');
  assert.equal(appleDevice({userAgent: UA.ipadOs}), 'iPad');
  assert.equal(appleDevice({userAgent: UA.ipadMobile}), 'iPad');
});

test('installed: any app display mode, or iOS navigator.standalone; a tab is not', () => {
  const nav = {userAgent: UA.iphone, maxTouchPoints: 5};
  assert.equal(installed(env(nav, ['browser'])), false);
  assert.equal(installed(env({...nav, standalone: false})), false);
  assert.equal(installed(env({...nav, standalone: true})), true);
  for (const mode of ['standalone', 'fullscreen', 'minimal-ui', 'window-controls-overlay']) {
    assert.equal(installed(env({userAgent: UA.chromeMac}, [mode])), true, mode);
  }
  assert.equal(installed({navigator: {}}), false, 'no matchMedia');
  assert.equal(installed({navigator: {}, matchMedia: () => { throw new Error('x'); }}), false);
  assert.equal(installed(null), false);
});

test('the gate: block / offer / none, per platform and display mode', () => {
  assert.equal(installGate({platform: 'ios', isInstalled: false}), 'block');
  assert.equal(installGate({platform: 'ios', isInstalled: true}), 'none');
  assert.equal(installGate({platform: 'android', isInstalled: false}), 'offer');
  assert.equal(installGate({platform: 'android', isInstalled: true}), 'none');
  assert.equal(installGate({platform: 'desktop', isInstalled: false}), 'offer');
  assert.equal(installGate({platform: 'desktop', isInstalled: true}), 'none');

  const cases = [
    // [what, navigator, display modes, gate, device]
    ['iPhone Safari tab', {userAgent: UA.iphone, maxTouchPoints: 5, standalone: false}, ['browser'], 'block', 'iPhone'],
    // SFSafariViewController: the same UA as Safari, not standalone.
    ['the Omodachi app’s Safari view', {userAgent: UA.iphone, maxTouchPoints: 5, standalone: false}, ['browser'], 'block', 'iPhone'],
    ['iPhone Home Screen app', {userAgent: UA.iphone, maxTouchPoints: 5, standalone: true}, ['standalone'], 'none', 'iPhone'],
    ['iPad Safari tab', {userAgent: UA.ipadOs, maxTouchPoints: 5, standalone: false}, ['browser'], 'block', 'iPad'],
    ['iPad Home Screen app', {userAgent: UA.ipadOs, maxTouchPoints: 5, standalone: true}, ['standalone'], 'none', 'iPad'],
    ['Chrome on iPhone', {userAgent: UA.crios, maxTouchPoints: 5}, ['browser'], 'block', 'iPhone'],
    ['Android Chrome tab', {userAgent: UA.android, maxTouchPoints: 5, userAgentData: {platform: 'Android'}}, ['browser'], 'offer', null],
    ['Android installed', {userAgent: UA.android, maxTouchPoints: 5, userAgentData: {platform: 'Android'}}, ['standalone'], 'none', null],
    ['desktop Chrome tab', {userAgent: UA.chromeMac, maxTouchPoints: 0, userAgentData: {platform: 'macOS'}}, ['browser'], 'offer', null],
    ['desktop Chrome app window', {userAgent: UA.chromeMac, maxTouchPoints: 0, userAgentData: {platform: 'macOS'}}, ['standalone'], 'none', null],
    ['Mac Safari tab', {userAgent: UA.safariMac, maxTouchPoints: 0}, ['browser'], 'offer', null],
    ['Linux Firefox', {userAgent: UA.firefoxLinux, maxTouchPoints: 0}, ['browser'], 'offer', null],
  ];
  for (const [what, nav, modes, gate, device] of cases) {
    const result = readInstall(env(nav, modes));
    assert.equal(result.gate, gate, what);
    assert.equal(result.device, device, what);
    assert.equal(result.installed, gate === 'none', what);
  }
});

function fakeTarget() {
  const handlers = {};
  return {
    addEventListener: (type, handler) => { (handlers[type] = handlers[type] || []).push(handler); },
    fire: (type, event) => (handlers[type] || []).forEach((handler) => handler(event)),
  };
}

test('Chromium’s install prompt: kept, called once inside the tap, and installed afterwards', async () => {
  const store = promptStore();
  const target = fakeTarget();
  store.listen(target);
  let changes = 0;
  store.subscribe(() => changes++);
  assert.deepEqual(store.state(), {available: false, installedNow: false});
  assert.equal(await store.prompt(), 'unavailable');

  let prevented = 0;
  let prompted = 0;
  target.fire('beforeinstallprompt', {preventDefault: () => prevented++, prompt: async () => { prompted++; },
    userChoice: Promise.resolve({outcome: 'dismissed'})});
  assert.equal(prevented, 1, 'Chrome’s own infobar stays away');
  assert.equal(store.state().available, true);
  assert.equal(await store.prompt(), 'dismissed');
  assert.equal(prompted, 1);
  assert.deepEqual(store.state(), {available: false, installedNow: false}, 'an event is used once');

  target.fire('beforeinstallprompt', {preventDefault() {}, prompt: async () => {}, userChoice: Promise.resolve({outcome: 'accepted'})});
  assert.equal(await store.prompt(), 'accepted');
  assert.deepEqual(store.state(), {available: false, installedNow: true});
  assert.ok(changes >= 4);

  const other = promptStore();
  const t2 = fakeTarget();
  other.listen(t2);
  t2.fire('appinstalled', {});
  assert.equal(other.state().installedNow, true);
  other.listen(null); // no window: nothing happens
});

const {module: render} = await runnerImport(new URL('./render.jsx', import.meta.url).pathname,
  {root: new URL('..', import.meta.url).pathname, configFile: false, logLevel: 'silent'});

test('the install screen: why, three steps, never a way to pair', () => {
  for (const [lang, table] of [['en', en], ['zh-Hans', zh]]) {
    for (const device of ['iPhone', 'iPad']) {
      const html = render.renderInstall({device}, lang);
      assert.match(html, /data-testid="install"/);
      assert.doesNotMatch(html, /pair-start|<button/, 'no Ask to pair and no way around the screen');
      assert.equal((html.match(/class="istep"/g) || []).length, 3);
      assert.equal((html.match(/<li>/g) || []).length, 4, 'four reasons');
      assert.ok(html.includes(table.install.step2), 'Add to Home Screen');
      assert.ok(html.includes(table.install.step3), 'open it from the Home Screen');
      assert.ok(html.includes(APP_NAME));
      assert.ok(html.includes(table.install.whyNotify.replace('%s', device)), 'locked ' + device);
      assert.ok(html.includes(table.install.whyFirst.replace('%s', device)), 'why it is pair-there');
      assert.doesNotMatch(html, /data-testid="install-old-pairing"/);
    }
    assert.match(render.renderInstall({hadPairing: true}, lang), /data-testid="install-old-pairing"/);
  }
});

test('the copy names the four reasons, the Share › Add to Home Screen steps, and takes the name from one place', () => {
  assert.match(en.install.whyNotify, /locked/);
  assert.match(en.install.whyNotify, /Omodachi app is closed/);
  assert.match(en.install.whyOpen, /full screen/);
  assert.match(en.install.whyStays, /signed in/);
  assert.match(en.install.whyStore, /store/);
  assert.match(en.install.step1, /Share/);
  assert.match(en.install.step2, /Add to Home Screen/);
  assert.match(en.install.step2Detail, /View More/);
  assert.match(zh.install.step2, /添加到主屏幕/);
  assert.match(zh.install.step2Detail, /查看更多/);
  for (const table of [en, zh]) {
    for (const [key, value] of Object.entries(table.install)) {
      assert.ok(value.trim().length > 0, key);
      assert.doesNotMatch(value, /Lite\b/, key);
    }
    assert.ok(table.install.title.includes(APP_NAME));
    assert.ok(table.install.installButton.includes(APP_NAME));
  }
});

test('the offer: Android always says how, a desktop only when it can install, never a pairing gate', () => {
  const android = render.renderOffer({platform: 'android'});
  assert.match(android, /data-testid="install-offer"/);
  assert.match(android, /install-offer-steps/, 'no prompt yet: the Chrome menu steps');
  assert.ok(android.includes(en.install.offerAndroid));
  assert.equal(render.renderOffer({platform: 'desktop'}), '', 'a desktop with no prompt: nothing');
  assert.ok(render.renderOffer({platform: 'android', compact: true}, 'zh-Hans').includes(zh.install.offerTitle));
});

test('Settings says Installed, or offers it; the steps also sit under push when a browser cannot have it', (t) => {
  globalThis.location = {hostname: 'omarchy.example.ts.net', host: 'omarchy.example.ts.net'};
  const hadWindow = 'window' in globalThis;
  if (!hadWindow) globalThis.window = {};
  t.after(() => {
    delete globalThis.location;
    if (!hadWindow) delete globalThis.window;
  });
  const ios = {platform: 'ios', installed: true, gate: 'none', device: 'iPhone'};
  for (const [lang, table] of [['en', en], ['zh-Hans', zh]]) {
    const html = render.renderSettings({install: ios}, lang);
    assert.match(html, /data-testid="settings-install" data-installed="1"/);
    assert.ok(html.includes(table.install.stateInstalled));
    assert.ok(html.includes(table.install.installedNote));
    const android = render.renderSettings({install: {platform: 'android', installed: false, gate: 'offer'}}, lang);
    assert.match(android, /data-installed="0"/);
    assert.ok(android.includes(table.install.stateBrowser));
    assert.match(android, /settings-install-offer/);
    const desktop = render.renderSettings({install: {platform: 'desktop', installed: false, gate: 'offer'}}, lang);
    assert.ok(desktop.includes(table.install.offerDesktop));
    assert.doesNotMatch(desktop, /settings-install-offer/);
  }
  assert.equal((render.renderSteps('zh-Hans').match(/class="istep"/g) || []).length, 3);
  // A browser that would have push only as a Home Screen app (support() says
  // add_to_home_screen): the sentence and the same three steps.
  globalThis.window = {isSecureContext: true, navigator: {userAgent: UA.iphone, maxTouchPoints: 5, serviceWorker: {}}};
  const push = render.renderPush('en');
  assert.match(push, /data-testid="push-blocked"/);
  assert.match(push, /data-testid="push-install-steps"/);
});

test('App: under the block gate the install screen replaces pairing, and a kept credential is not used', () => {
  const app = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8');
  const effect = app.slice(app.indexOf("if (install.gate === 'block')"), app.indexOf("if (value && typeof value.token === 'string') {\n          setCredential"));
  assert.match(effect, /setPhase\('install'\);\s*return;/);
  assert.doesNotMatch(effect, /setCredential/);
  assert.match(app, /phase === 'install' \? \(\s*<InstallScreen/);
  const pair = readFileSync(new URL('../src/PairScreen.jsx', import.meta.url), 'utf8');
  assert.match(pair, /install\.gate === 'offer' \? <InstallOffer/);
});
