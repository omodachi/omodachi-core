// LITE-1e: where Odomo asks to be installed, in Chrome (the real iOS check is
// the simulator; this one is repeatable).
//
//   HOST_CMD='<omodachi-host prefix>' node tests/e2e/install.mjs <odomo-url> <out-dir>
//
// System Chrome, throwaway contexts. Desktop: the pairing page, no guide, and
// a desktop pairs in the tab. Chrome dressed as an iPhone / iPad: only the
// install screen, and a credential already kept in that browser is never sent.
// The same iPhone as a Home Screen app (`navigator.standalone`): the pairing page.
// Android: the pairing page with the quiet offer under it. Nothing reaches
// omodachi.app. Writes `install-*.png` and `install-report.json`.
import {chromium} from 'playwright-core';
import {execSync} from 'node:child_process';
import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';

const [url, out = '.'] = process.argv.slice(2);
const HOST_CMD = process.env.HOST_CMD;
if (!url || !HOST_CMD) {
  console.error("usage: HOST_CMD='...' node tests/e2e/install.mjs <odomo-url> <out-dir>");
  process.exit(2);
}
mkdirSync(out, {recursive: true});
const report = {url, console: [], appRequests: [], authorized: [], results: {}};
const host = (args) => JSON.parse(execSync(HOST_CMD + ' ' + args, {encoding: 'utf8', timeout: 30000, shell: '/bin/sh'}));
const UA = {
  iphone: 'Mozilla/5.0 (iPhone; CPU iPhone OS 26_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5 Mobile/15E148 Safari/604.1',
  ipad: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5 Safari/605.1.15',
  android: 'Mozilla/5.0 (Linux; Android 16; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Mobile Safari/537.36',
};

const browser = await chromium.launch({channel: 'chrome', headless: process.env.HEADED !== '1'});

async function context(name, {kind, locale = 'en-GB', viewport, standalone = false}) {
  const options = {locale, viewport};
  if (kind !== 'desktop') Object.assign(options, {userAgent: UA[kind], hasTouch: true, isMobile: kind !== 'ipad'});
  const ctx = await browser.newContext(options);
  await ctx.route('https://omodachi.app/**', (route) => {
    report.appRequests.push(name + ' ' + route.request().url());
    return route.abort();
  });
  await ctx.addInitScript(({kind, standalone}) => {
    window.__bip = 0;
    window.addEventListener('beforeinstallprompt', () => { window.__bip += 1; });
    if (kind === 'iphone' || kind === 'ipad') {
      // iOS WebKit, as the page sees it: no userAgentData, a touch screen.
      Object.defineProperty(Navigator.prototype, 'userAgentData', {get: () => undefined});
      Object.defineProperty(Navigator.prototype, 'maxTouchPoints', {get: () => 5});
      // A Home Screen app on iOS says so here (Chrome cannot emulate display-mode).
      if (standalone) Object.defineProperty(Navigator.prototype, 'standalone', {get: () => true});
    }
    if (kind === 'android') {
      Object.defineProperty(Navigator.prototype, 'userAgentData', {get: () => ({platform: 'Android', mobile: true, brands: []})});
    }
  }, {kind, standalone});
  const page = await ctx.newPage();
  page.on('request', (r) => {
    if (r.headers().authorization) report.authorized.push(name + ' ' + new URL(r.url()).pathname);
  });
  page.on('console', (m) => { if (m.type() === 'error') report.console.push(name + ' ' + m.text()); });
  page.on('pageerror', (e) => report.console.push(name + ' pageerror: ' + e.message));
  return {ctx, page};
}

async function pairAndApprove(page) {
  await page.locator('[data-testid="pair-start"]').click();
  await page.locator('[data-testid="pair-card"]').waitFor({timeout: 15000});
  const pending = host('pair pending').result.requests;
  host('pair approve ' + pending[pending.length - 1].request_id);
  await page.locator('[data-testid="panel-menu"]').waitFor({timeout: 60000});
}

const shot = (page, file) => page.screenshot({path: join(out, file)});
const has = async (page, id) => (await page.locator(`[data-testid="${id}"]`).count()) > 0;

// 1. Desktop Chrome: no guide; it pairs in the tab; Settings says "In the browser".
{
  const {ctx, page} = await context('desktop', {kind: 'desktop', viewport: {width: 1280, height: 800}});
  await page.goto(url, {waitUntil: 'networkidle'});
  await page.waitForTimeout(800);
  const r = {guide: await has(page, 'install'), pair: await has(page, 'pair-start'),
    beforeinstallprompt: await page.evaluate(() => window.__bip), offer: await has(page, 'install-offer')};
  await shot(page, 'install-desktop-pair.png');
  await pairAndApprove(page);
  await page.locator('[data-testid="bar-settings"]').click();
  await page.locator('[data-testid="settings-install"]').waitFor({timeout: 10000});
  await page.locator('[data-testid="settings-install"]').scrollIntoViewIfNeeded();
  r.settingsInstalled = await page.locator('[data-testid="settings-install"]').getAttribute('data-installed');
  r.settingsOffer = await has(page, 'settings-install-offer');
  await shot(page, 'install-desktop-settings.png');
  // Headless Chrome never offers to install, so the prompt is played by a
  // synthetic event with Chrome's shape: the button appears, and a tap calls
  // prompt() once and hears the answer.
  await page.evaluate(() => {
    const event = new Event('beforeinstallprompt', {cancelable: true});
    event.prompt = () => { window.__prompted = (window.__prompted || 0) + 1; return Promise.resolve(); };
    event.userChoice = Promise.resolve({outcome: 'accepted', platform: 'web'});
    window.dispatchEvent(event);
  });
  await page.locator('[data-testid="settings-install-offer-button"]').waitFor({timeout: 5000});
  await shot(page, 'install-desktop-settings-prompt.png');
  await page.locator('[data-testid="settings-install-offer-button"]').click();
  await page.locator('[data-testid="settings-install"][data-installed="1"]').waitFor({timeout: 5000});
  r.syntheticPrompt = {called: await page.evaluate(() => window.__prompted), installedAfter: true};
  report.results.desktop = r;
  await ctx.close();
}

// 2. iPhone / iPad tab, EN and zh: only the install screen.
for (const [kind, viewport] of [['iphone', {width: 402, height: 874}], ['ipad', {width: 820, height: 1180}]]) {
  for (const locale of ['en-GB', 'zh-CN']) {
    const name = kind + '-' + locale;
    const {ctx, page} = await context(name, {kind, locale, viewport});
    await page.goto(url, {waitUntil: 'networkidle'});
    await page.locator('[data-testid="install"]').waitFor({timeout: 10000});
    report.results[name] = {guide: true, pair: await has(page, 'pair-start'), buttons: await page.locator('button').count(),
      steps: await page.locator('.istep').count()};
    await page.screenshot({path: join(out, `install-${kind}-${locale.slice(0, 2)}.png`), fullPage: true});
    await ctx.close();
  }
}

// 3. An iPhone tab that already holds a pairing: still the install screen, and
//    the credential is never sent.
{
  const {ctx, page} = await context('iphone-kept', {kind: 'iphone', viewport: {width: 402, height: 874}});
  await page.goto(url, {waitUntil: 'networkidle'});
  await page.evaluate(() => new Promise((resolve, reject) => {
    const open = indexedDB.open('odomo', 1);
    open.onupgradeneeded = () => open.result.createObjectStore('kv');
    open.onsuccess = () => {
      const tx = open.result.transaction('kv', 'readwrite');
      tx.objectStore('kv').put({token: 'kept-in-the-tab', device_id: 'odomo-kept', expires_at: 4102444800}, 'credential');
      tx.oncomplete = () => { open.result.close(); resolve(); };
      tx.onerror = () => reject(tx.error);
    };
  }));
  await page.reload({waitUntil: 'networkidle'});
  await page.locator('[data-testid="install-old-pairing"]').waitFor({timeout: 10000});
  await page.waitForTimeout(1500);
  report.results.iphoneKept = {guide: await has(page, 'install'), oldPairingNote: true, shell: await has(page, 'app'),
    authorizedRequests: report.authorized.filter((line) => line.startsWith('iphone-kept')).length};
  // The page scrolls inside .pair: show its end, where the note is.
  await page.evaluate(() => { const pair = document.querySelector('.pair'); pair.scrollTop = pair.scrollHeight; });
  await page.screenshot({path: join(out, 'install-iphone-kept-pairing.png')});
  await ctx.close();
}

// 4. The same iPhone as a Home Screen app (navigator.standalone; Chrome cannot
//    emulate display-mode): no guide, it pairs.
{
  const {ctx, page} = await context('iphone-standalone', {kind: 'iphone', viewport: {width: 402, height: 874}, standalone: true});
  await page.goto(url, {waitUntil: 'networkidle'});
  await page.waitForTimeout(800);
  const r = {standalone: await page.evaluate(() => navigator.standalone === true),
    guide: await has(page, 'install'), pair: await has(page, 'pair-start')};
  await pairAndApprove(page);
  await page.locator('[data-testid="bar-settings"]').click();
  await page.locator('[data-testid="settings-install"]').waitFor({timeout: 10000});
  await page.locator('[data-testid="settings-install"]').scrollIntoViewIfNeeded();
  r.settingsInstalled = await page.locator('[data-testid="settings-install"]').getAttribute('data-installed');
  await shot(page, 'install-iphone-standalone-settings.png');
  report.results.iphoneStandalone = r;
  await ctx.close();
}

// 5. Android Chrome tab: pairing stays here, with the offer under it.
{
  const {ctx, page} = await context('android', {kind: 'android', viewport: {width: 412, height: 915}});
  await page.goto(url, {waitUntil: 'networkidle'});
  await page.waitForTimeout(800);
  report.results.android = {guide: await has(page, 'install'), pair: await has(page, 'pair-start'),
    offer: await has(page, 'install-offer'), offerButton: await has(page, 'install-offer-button'),
    offerSteps: await has(page, 'install-offer-steps')};
  await page.screenshot({path: join(out, 'install-android-pair.png'), fullPage: true});
  await ctx.close();
}

await browser.close();
writeFileSync(join(out, 'install-report.json'), JSON.stringify(report, null, 2));
const R = report.results;
const ok = !R.desktop.guide && R.desktop.pair && R.desktop.settingsInstalled === '0' && R.desktop.syntheticPrompt.called === 1
  && ['iphone-en-GB', 'iphone-zh-CN', 'ipad-en-GB', 'ipad-zh-CN'].every((k) => R[k].guide && !R[k].pair && R[k].buttons === 0 && R[k].steps === 3)
  && R.iphoneKept.guide && !R.iphoneKept.shell && R.iphoneKept.authorizedRequests === 0
  && R.iphoneStandalone.standalone && !R.iphoneStandalone.guide && R.iphoneStandalone.pair && R.iphoneStandalone.settingsInstalled === '1'
  && !R.android.guide && R.android.pair && R.android.offer
  && report.appRequests.length === 0;
console.log(JSON.stringify(report.results, null, 1));
console.log(ok ? 'install e2e: OK' : 'install e2e: FAILED');
process.exit(ok ? 0 : 1);
