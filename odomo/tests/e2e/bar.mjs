// LITE-1c: the bar in desktop Chrome, and Chrome dressed as an iPad and an
// iPhone (the real iOS check is the simulator; this one is repeatable).
//
//   HOST_CMD='<omodachi-host prefix>' node tests/e2e/bar.mjs <odomo-url> <out-dir>
//
// System Chrome, throwaway contexts, nothing of anybody's. Each context pairs
// and is approved through HOST_CMD (`pair pending` / `pair approve <id>`).
// Nothing ever reaches omodachi.app: every request to it is aborted and
// counted, and no link to it is followed. Writes `bar-*.png` and
// `bar-report.json`.
import {chromium} from 'playwright-core';
import {execSync} from 'node:child_process';
import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';

const [url, out = '.'] = process.argv.slice(2);
const HOST_CMD = process.env.HOST_CMD;
if (!url || !HOST_CMD) {
  console.error("usage: HOST_CMD='...' node tests/e2e/bar.mjs <odomo-url> <out-dir>");
  process.exit(2);
}
mkdirSync(out, {recursive: true});
const report = {url, console: [], failures: [], appRequests: [], popups: [], results: {}};
const host = (args) => JSON.parse(execSync(HOST_CMD + ' ' + args, {encoding: 'utf8', timeout: 30000, shell: '/bin/sh'}));
const IPAD_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15';
const IPHONE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1';

const browser = await chromium.launch({channel: 'chrome', headless: process.env.HEADED !== '1'});

async function open(name, options = {}, apple = false) {
  const context = await browser.newContext({locale: process.env.LOCALE || 'en-GB', ...options});
  await context.route('https://omodachi.app/**', (route) => {
    report.appRequests.push(name + ' ' + route.request().url());
    return route.abort();
  });
  if (apple) {
    // What iOS WebKit looks like to the page: no userAgentData, a touch screen.
    await context.addInitScript(() => {
      Object.defineProperty(Navigator.prototype, 'userAgentData', {get: () => undefined});
      Object.defineProperty(Navigator.prototype, 'maxTouchPoints', {get: () => 5});
    });
  }
  const page = await context.newPage();
  // Any page after this one is a link that was followed.
  context.on('page', (opened) => report.popups.push(name + ' ' + opened.url()));
  page.on('response', (r) => { if (r.status() >= 400) report.failures.push(name + ' ' + r.status() + ' ' + new URL(r.url()).pathname); });
  page.on('console', (m) => { if (m.type() === 'error' || m.type() === 'warning') report.console.push(name + ' ' + m.type() + ': ' + m.text()); });
  page.on('pageerror', (e) => report.console.push(name + ' pageerror: ' + e.message));
  await page.goto(url, {waitUntil: 'networkidle'});
  await page.locator('[data-testid="pair-start"]').click();
  await page.locator('[data-testid="pair-card"]').waitFor({timeout: 15000});
  const pending = host('pair pending').result.requests;
  const request = pending[pending.length - 1];
  host('pair approve ' + request.request_id);
  await page.locator('[data-testid="panel-menu"]').waitFor({timeout: Number(process.env.APPROVE_S || 90) * 1000});
  await page.locator('[data-testid="bar-connection"][data-status="connected"]').waitFor({timeout: 30000});
  await page.waitForTimeout(400);
  return {context, page};
}

const testid = (page, id) => page.locator(`[data-testid="${id}"]`);
const shot = (page, name) => page.screenshot({path: join(out, 'bar-' + name + '.png')});
const remoteTag = (page) => testid(page, 'bar-remote').evaluate((el) => ({tag: el.tagName.toLowerCase(),
  href: el.getAttribute('href'), target: el.getAttribute('target'), label: el.getAttribute('aria-label')}));

// A touch held in place, through the DevTools protocol.
async function hold(page, id, ms) {
  const box = await testid(page, id).boundingBox();
  const point = {x: Math.round(box.x + box.width / 2), y: Math.round(box.y + box.height / 2)};
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [point]});
  await page.waitForTimeout(ms);
  const peek = await testid(page, 'bar-peek').textContent().catch(() => null);
  return {cdp, point, peek};
}

let failed = null;
try {
  // 1. Desktop Chrome: named bar, the name on hover, Remote is the panel.
  {
    const {context, page} = await open('desktop', {viewport: {width: 1280, height: 800}});
    report.results.desktop = {named: await testid(page, 'app-bar').evaluate((el) => el.classList.contains('sbar--named')),
      remote: await remoteTag(page)};
    await shot(page, 'desktop-named');
    await testid(page, 'bar-remote').hover();
    await testid(page, 'bar-peek').waitFor({timeout: 3000});
    report.results.desktop.hover = await testid(page, 'bar-peek').textContent();
    await shot(page, 'desktop-hover-remote');
    await testid(page, 'bar-remote').click();
    await testid(page, 'panel-remote').waitFor({timeout: 5000});
    report.results.desktop.panel = await page.locator('[data-testid="panel-remote"] a').evaluateAll((links) =>
      links.map((a) => a.getAttribute('href')));
    await page.mouse.move(640, 400);
    await page.waitForTimeout(300);
    await shot(page, 'desktop-remote-panel');
    await testid(page, 'bar-settings').click();
    await testid(page, 'settings-app-open').scrollIntoViewIfNeeded();
    report.results.desktop.settings = await page.locator('[data-testid^="settings-app-"]').evaluateAll((links) =>
      links.map((a) => [a.textContent, a.getAttribute('href')]));
    await shot(page, 'desktop-settings-app');
    await context.close();
  }
  // 2. Chrome as an iPad (portrait): named bar, Remote is the link; a hold
  //    shows the name and opens nothing. Then option B, icons only, for the
  //    report: the same page with the words hidden (CSP bypassed for that).
  {
    const {context, page} = await open('ipad', {viewport: {width: 820, height: 1180}, userAgent: IPAD_UA, hasTouch: true,
      isMobile: true, bypassCSP: true}, true);
    report.results.ipad = {remote: await remoteTag(page)};
    await shot(page, 'ipad-option-a-named');
    const {cdp, peek} = await hold(page, 'bar-remote', 700);
    report.results.ipad.holdPeek = peek;
    await shot(page, 'ipad-hold-remote');
    await cdp.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
    await page.waitForTimeout(600);
    report.results.ipad.popupsAfterHold = report.popups.filter((p) => p.startsWith('ipad')).length;
    await page.addStyleTag({content: '.bslot__name{display:none!important}.sbar--named{width:46px!important}' +
      '.sbar--named .bslot,.sbar--named .ws{width:44px!important;height:44px!important}' +
      '.sbar--named .bslot__out{top:7px!important;right:7px!important}'});
    await page.waitForTimeout(200);
    await shot(page, 'ipad-option-b-icons');
    await context.close();
  }
  // 3. Chrome as an iPhone: icons only (no room), Remote is the link.
  {
    const {context, page} = await open('iphone', {viewport: {width: 402, height: 874}, userAgent: IPHONE_UA, hasTouch: true,
      isMobile: true}, true);
    report.results.iphone = {named: await testid(page, 'app-bar').evaluate((el) => el.classList.contains('sbar--named')),
      remote: await remoteTag(page)};
    await shot(page, 'iphone-icons');
    const {cdp, peek} = await hold(page, 'bar-settings', 700);
    report.results.iphone.holdPeek = peek;
    await shot(page, 'iphone-hold-settings');
    await cdp.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
    await page.waitForTimeout(400);
    report.results.iphone.panelAfterHold = await page.locator('[data-testid="app"]').getAttribute('data-panel');
    await context.close();
  }
  report.results.ok = true;
} catch (error) {
  failed = error;
  report.results.error = String(error && error.stack || error);
} finally {
  await browser.close();
  writeFileSync(join(out, 'bar-report.json'), JSON.stringify(report, null, 2));
}
console.log(JSON.stringify(report.results, null, 2));
console.log('omodachi.app requests:', report.appRequests.length, 'popups:', report.popups.length, 'console:', report.console.length,
  'http >= 400:', [...new Set(report.failures)].join(', '));
process.exit(failed ? 1 : 0);
