// LITE-1b: Web Push end to end in desktop Chrome, from odomo/node_modules.
//
//   HOST_CMD='<shell command prefix>' node tests/e2e/push.mjs <odomo-url> <out-dir>
//
// The system Chrome (`channel: 'chrome'`) with a throwaway profile directory
// (push needs a real profile - an incognito context has none), notifications
// allowed for <odomo-url>'s origin only. It pairs, turns notifications on in
// Settings, then asks the host - `$HOST_CMD <step>`, run by sh - to raise each
// thing worth a push, and watches for the notification the service worker
// shows (`registration.getNotifications()`), its tag, its words and the URL a
// tap opens. Steps it asks the host for: approve, approval, resolve, notify.
//
// A tap on a desktop notification cannot be scripted (it is the OS's), so the
// tap is shown both ways the worker handles it: Odomo already open (the
// worker's own `postMessage` to the open page, sent from inside the worker)
// and Odomo closed (opening the notification's URL). Writes `push-report.json`
// and one screenshot per step.
import {chromium} from 'playwright-core';
import {execSync} from 'node:child_process';
import {mkdirSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

const [url, out = '.'] = process.argv.slice(2);
const HOST_CMD = process.env.HOST_CMD;
if (!url || !HOST_CMD) {
  console.error("usage: HOST_CMD='...' node tests/e2e/push.mjs <odomo-url> <out-dir>");
  process.exit(2);
}
const PREFIX = process.env.SHOT_PREFIX || 'push';
const DEADLINE = Number(process.env.PUSH_WAIT_S || 60) * 1000;
mkdirSync(out, {recursive: true});
const origin = new URL(url).origin;
const profile = mkdtempSync(join(process.env.PROFILE_ROOT || tmpdir(), 'odomo1b-chrome-'));
const report = {url, steps: [], console: [], responses: [], notifications: [], results: {}};
const host = (step) => {
  const started = Date.now();
  const output = execSync(HOST_CMD + ' ' + step, {encoding: 'utf8', timeout: 60000, shell: '/bin/sh'});
  report.steps.push({step, at: new Date().toISOString(), ms: Date.now() - started, output: output.trim().slice(-400)});
  return started;
};

const context = await chromium.launchPersistentContext(profile, {
  channel: 'chrome', headless: process.env.HEADED !== '1', viewport: {width: 1280, height: 800},
  locale: process.env.LOCALE || 'en-GB', permissions: [], args: ['--no-first-run', '--no-default-browser-check'],
});
await context.grantPermissions(['notifications'], {origin});
const page = context.pages()[0] || await context.newPage();
page.on('console', (m) => { if (m.type() === 'error' || m.type() === 'warning') report.console.push(m.type() + ': ' + m.text()); });
page.on('pageerror', (e) => report.console.push('pageerror: ' + e.message));
page.on('response', (r) => report.responses.push(r.status() + ' ' + r.request().method() + ' ' + new URL(r.url()).pathname));
// The host's tailnet name is on the Settings page; report screenshots mask it.
const MASK = (process.env.MASK || '.hostrow__tx span').split(',').filter(Boolean);
const shot = (target, name) => target.screenshot({path: join(out, PREFIX + '-' + name + '.png'),
  mask: MASK.map((selector) => target.locator(selector)), maskColor: '#000000'});
const testid = (target, id) => target.locator(`[data-testid="${id}"]`);

async function shown(target) {
  return target.evaluate(async () => {
    const registration = await navigator.serviceWorker.ready;
    return (await registration.getNotifications()).map((n) => ({title: n.title, body: n.body, tag: n.tag, url: n.data && n.data.url}));
  });
}

async function waitFor(predicate, what) {
  const end = Date.now() + DEADLINE;
  for (;;) {
    const value = await predicate();
    if (value) return value;
    if (Date.now() > end) throw new Error('timed out waiting for ' + what);
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}

try {
  await page.goto(url, {waitUntil: 'networkidle'});
  await testid(page, 'pair-start').click();
  await testid(page, 'pair-card').waitFor({timeout: 15000});
  report.results.device = await testid(page, 'pair-device').innerText();
  host('approve');
  await testid(page, 'panel-menu').waitFor({timeout: 60000});

  await testid(page, 'bar-settings').click();
  await testid(page, 'push-on').waitFor({timeout: 15000});
  await page.waitForFunction(() => !document.querySelector('[data-testid="push-on"]').disabled, null, {timeout: 15000});
  await shot(page, '01-settings-off');
  await testid(page, 'push-on').click();
  await page.locator('[data-testid="push-settings"][data-subscribed="1"]').waitFor({timeout: 30000});
  report.results.subscription = await page.evaluate(async () => {
    const s = await (await navigator.serviceWorker.ready).pushManager.getSubscription();
    return s ? new URL(s.endpoint).hostname : null;
  });
  await shot(page, '02-settings-on');

  // 1. the agent asks for approval -> a push, tagged, with the agent panel's URL
  let started = host('approval');
  const approval = await waitFor(async () => (await shown(page)).find((n) => n.tag === 'agent'), 'the approval push');
  report.notifications.push({...approval, after_ms: Date.now() - started});
  // 2. answered on the computer -> the host retracts it (FCM allows it)
  started = host('resolve');
  await waitFor(async () => !(await shown(page)).some((n) => n.tag === 'agent'), 'the retraction');
  report.results.retracted_after_ms = Date.now() - started;

  // 3. a desktop notification on the host -> a push about that notification
  await testid(page, 'bar-logo').click();
  started = host('notify');
  const note = await waitFor(async () => (await shown(page)).find((n) => n.tag.startsWith('note-')), 'the notification push');
  report.notifications.push({...note, after_ms: Date.now() - started});

  // The tap, with Odomo open: the worker tells the open page where to go.
  const [worker] = context.serviceWorkers().length ? context.serviceWorkers() : [await context.waitForEvent('serviceworker')];
  await worker.evaluate(async (target) => {
    const windows = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const client of windows) client.postMessage({type: 'omodachi-open', url: target});
  }, note.url);
  await testid(page, 'panel-notifications').waitFor({timeout: 10000});
  report.results.open_panel = await page.locator('.nrow.hl').count();
  await shot(page, '03-tap-odomo-open');

  // The tap, with Odomo closed: the notification's URL, in a new window.
  const fresh = await context.newPage();
  await fresh.goto(note.url, {waitUntil: 'networkidle'});
  await testid(fresh, 'panel-notifications').waitFor({timeout: 15000});
  report.results.closed_panel = await fresh.locator('.nrow.hl').count();
  await shot(fresh, '04-tap-odomo-closed');
  await fresh.close();

  // An approval push opens the agent panel and says what it was about.
  const approvalPage = await context.newPage();
  await approvalPage.goto(approval.url, {waitUntil: 'networkidle'});
  await testid(approvalPage, 'next-from-push').waitFor({timeout: 15000});
  await shot(approvalPage, '05-tap-approval');
  await approvalPage.close();
  report.results.ok = true;
} catch (error) {
  report.results.ok = false;
  report.results.error = String(error && error.stack || error);
  await shot(page, '99-failure').catch(() => {});
} finally {
  writeFileSync(join(out, PREFIX + '-report.json'), JSON.stringify(report, null, 2));
  await context.close();
  rmSync(profile, {recursive: true, force: true});
}
console.log(JSON.stringify(report.results));
process.exit(report.results.ok ? 0 : 1);
