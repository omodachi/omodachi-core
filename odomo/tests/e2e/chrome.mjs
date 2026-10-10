// LITE-1a: drive Odomo in desktop Chrome, from odomo/node_modules.
//
//   node tests/e2e/chrome.mjs <odomo-url> <out-dir> [steps]
//
// The browser is the system Chrome (`channel: 'chrome'`) in a fresh,
// throwaway context: no profile, no cookies, nothing of anybody's. It pairs
// (and waits, up to APPROVE_S seconds, for someone to approve on the
// computer), then walks the steps named on the command line and writes one
// screenshot per step plus `chrome-report.json`: every console error, every
// CSP violation and every response status it saw. Nothing here talks to any
// host but the one in <odomo-url>.
import {chromium} from 'playwright-core';
import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';

const [url, out = '.', stepList = 'pair,menu,keys'] = process.argv.slice(2);
if (!url) {
  console.error('usage: node tests/e2e/chrome.mjs <odomo-url> <out-dir> [steps]');
  process.exit(2);
}
const steps = stepList.split(',');
const APPROVE_S = Number(process.env.APPROVE_S || 120);
const WIDTH = Number(process.env.WIDTH || 1280);
const HEIGHT = Number(process.env.HEIGHT || 800);
mkdirSync(out, {recursive: true});

const report = {url, steps, console: [], csp: [], responses: [], results: {}};
const browser = await chromium.launch({channel: 'chrome', headless: process.env.HEADED !== '1'});
const context = await browser.newContext({viewport: {width: WIDTH, height: HEIGHT}, locale: process.env.LOCALE || 'en-GB'});
await context.addInitScript(() => {
  window.__csp = [];
  document.addEventListener('securitypolicyviolation', (e) => window.__csp.push({directive: e.violatedDirective, blocked: e.blockedURI}));
});
const page = await context.newPage();
page.on('console', (m) => { if (m.type() === 'error' || m.type() === 'warning') report.console.push(m.type() + ': ' + m.text()); });
page.on('pageerror', (e) => report.console.push('pageerror: ' + e.message));
page.on('response', (r) => report.responses.push(r.status() + ' ' + r.request().method() + ' ' + new URL(r.url()).pathname));
const shot = (name) => page.screenshot({path: join(out, name + '.png')});
const testid = (id) => page.locator(`[data-testid="${id}"]`);

try {
  await page.goto(url, {waitUntil: 'networkidle'});
  if (steps.includes('pair')) {
    await testid('pair-start').waitFor({timeout: 15000});
    await shot('chrome-01-pair');
    await testid('pair-start').click();
    await testid('pair-card').waitFor({timeout: 15000});
    report.results.device = await testid('pair-device').innerText();
    console.log('WAITING device=' + report.results.device);
    await shot('chrome-02-waiting');
    await testid('panel-menu').waitFor({timeout: APPROVE_S * 1000});
  } else {
    await testid('panel-menu').waitFor({timeout: 30000});
  }
  await testid('bar-connection').and(page.locator('[data-status="connected"]')).waitFor({timeout: 30000});
  await page.locator('[data-testid^="menu-row-"]').first().waitFor({timeout: 30000});
  report.results.menuRows = await page.locator('[data-testid^="menu-row-"]').count();
  if (steps.includes('menu')) await shot('chrome-03-menu');
  if (steps.includes('keys')) {
    const search = testid('keys-search');
    await search.fill(process.env.KEY_QUERY || 'time');
    const row = page.locator('[data-testid^="key-row-"] button:not([disabled])').first();
    report.results.keyLabel = await row.getAttribute('aria-label');
    await row.click();
    await page.locator('[data-testid="panel-toast"][data-kind="ran"], [data-testid="panel-toast"][data-kind="failed"]').first().waitFor({timeout: 15000});
    report.results.keyToast = await testid('panel-toast').getAttribute('data-kind');
    await shot('chrome-04-keybinding');
  }
  if (steps.includes('notifications')) {
    await testid('bar-notifications').click();
    await testid('panel-notifications').waitFor();
    await shot('chrome-05-notifications');
  }
  if (steps.includes('settings')) {
    await testid('bar-settings').click();
    await testid('panel-settings').waitFor();
    await shot('chrome-06-settings');
  }
  report.results.ok = true;
} catch (error) {
  report.results.ok = false;
  report.results.error = String(error && error.message || error).split('\n')[0];
  await shot('chrome-99-failure').catch(() => {});
} finally {
  report.csp = await page.evaluate(() => window.__csp || []).catch(() => ['unreadable']);
  writeFileSync(join(out, 'chrome-report.json'), JSON.stringify(report, null, 2));
  await context.close();
  await browser.close();
}
console.log(JSON.stringify({ok: report.results.ok, error: report.results.error, csp: report.csp.length,
  consoleErrors: report.console.length, results: report.results}));
process.exit(report.results.ok ? 0 : 1);
