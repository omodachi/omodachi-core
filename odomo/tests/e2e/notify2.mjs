// NOTIFY-2: Odomo's push and the App in front, end to end in desktop Chrome.
//
//   HOST_CMD='<shell command prefix>' node tests/e2e/notify2.mjs <odomo-url> <out-dir>
//
// The system Chrome with a throwaway profile directory (as push.mjs). It pairs,
// turns notifications on, then asks `$HOST_CMD <step>` for:
//   approve                    approve this browser on the computer
//   native-back / native-front put the native App (a simulator) behind / in front
//   block <pane> / unblock <pane>   a Herdr pane in a session nobody opened
// and checks the rule core now keeps: a `herdr_blocked` push still arrives
// while the App is in the background, and is held back while the App is in
// front and alerting that kind itself. The push's URL then opens Odomo's Herd
// page with what was waiting at the top and "Open in Omodachi" pointing at the
// App's own link for that session and pane. Writes `notify2-report.json` and
// one screenshot per step.
import {chromium} from 'playwright-core';
import {execSync} from 'node:child_process';
import {mkdirSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

const [url, out = '.'] = process.argv.slice(2);
const HOST_CMD = process.env.HOST_CMD;
if (!url || !HOST_CMD) {
  console.error("usage: HOST_CMD='...' node tests/e2e/notify2.mjs <odomo-url> <out-dir>");
  process.exit(2);
}
const PREFIX = process.env.SHOT_PREFIX || 'notify2';
const DEADLINE = Number(process.env.PUSH_WAIT_S || 60) * 1000;
const QUIET_S = Number(process.env.QUIET_WAIT_S || 40) * 1000;
const PANE_BACK = process.env.PANE_BACK || 'w1:p2';
const PANE_FRONT = process.env.PANE_FRONT || 'w1:p1';
mkdirSync(out, {recursive: true});
const origin = new URL(url).origin;
const profile = mkdtempSync(join(process.env.PROFILE_ROOT || tmpdir(), 'odomo-n2-chrome-'));
const report = {url: '<odomo>', steps: [], console: [], notifications: [], results: {}};
const host = (step) => {
  const started = Date.now();
  const output = execSync(HOST_CMD + ' ' + step, {encoding: 'utf8', timeout: 90000, shell: '/bin/sh'});
  report.steps.push({step, at: new Date().toISOString(), ms: Date.now() - started, output: output.trim().slice(-300)});
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
async function clear(target) {
  await target.evaluate(async () => {
    for (const n of await (await navigator.serviceWorker.ready).getNotifications()) n.close();
  });
}
async function waitFor(predicate, what, deadline = DEADLINE) {
  const end = Date.now() + deadline;
  for (;;) {
    const value = await predicate();
    if (value) return value;
    if (Date.now() > end) throw new Error('timed out waiting for ' + what);
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}
const paneTag = (pane) => ('herdr-' + pane).replace(/[^A-Za-z0-9_-]/g, '-');
const redact = (value) => (value || '').replace(/https:\/\/[^/]+/, 'https://<odomo-host>');

try {
  await page.goto(url, {waitUntil: 'networkidle'});
  await testid(page, 'pair-start').click();
  await testid(page, 'pair-card').waitFor({timeout: 15000});
  host('approve');
  await testid(page, 'panel-menu').waitFor({timeout: 60000});
  await testid(page, 'bar-settings').click();
  await testid(page, 'push-on').waitFor({timeout: 15000});
  await page.waitForFunction(() => !document.querySelector('[data-testid="push-on"]').disabled, null, {timeout: 15000});
  await testid(page, 'push-on').click();
  await page.locator('[data-testid="push-settings"][data-subscribed="1"]').waitFor({timeout: 30000});
  await testid(page, 'push-quiet').scrollIntoViewIfNeeded();
  await shot(page, '01-settings-quiet-explained');
  report.results.quiet_text = await testid(page, 'push-quiet').innerText();

  // 1. The App is behind: a pane blocked in an unopened session is pushed.
  host('unblock ' + PANE_BACK);
  host('native-back');
  await clear(page);
  let started = host('block ' + PANE_BACK);
  const back = await waitFor(async () => (await shown(page)).find((n) => n.tag === paneTag(PANE_BACK)),
    'the push with the App in the background');
  report.notifications.push({...back, url: redact(back.url), after_ms: Date.now() - started, app: 'background'});
  report.results.background_push = true;

  // 2. The App is in front and alerts herdr_blocked itself: held back.
  host('unblock ' + PANE_FRONT);
  host('native-front');
  await clear(page);
  started = host('block ' + PANE_FRONT);
  const front = await waitFor(async () => (await shown(page)).find((n) => n.tag === paneTag(PANE_FRONT)),
    'nothing', QUIET_S).catch(() => null);
  report.results.foreground_push = front ? 'ARRIVED (should not)' : 'held back for ' + QUIET_S / 1000 + ' s';
  report.results.foreground_quiet = !front;
  // …and once the App goes behind again, the next one arrives.
  host('unblock ' + PANE_FRONT);
  host('native-back');
  started = host('block ' + PANE_FRONT);
  const after = await waitFor(async () => (await shown(page)).find((n) => n.tag === paneTag(PANE_FRONT)),
    'the push after the App left the front');
  report.notifications.push({...after, url: redact(after.url), after_ms: Date.now() - started, app: 'background again'});

  // 3. The push's URL (a tap) opens Herd with the place and the way into the App.
  const herd = await context.newPage();
  await herd.goto(back.url, {waitUntil: 'networkidle'});
  await testid(herd, 'next-from-push').waitFor({timeout: 15000});
  report.results.herd_title = await testid(herd, 'next-waiting-title').innerText();
  report.results.herd_where = await testid(herd, 'next-waiting-where').innerText();
  report.results.herd_link = await testid(herd, 'next-open-app').getAttribute('href');
  report.results.herd_scheme = await testid(herd, 'next-open-scheme').getAttribute('href');
  await shot(herd, '02-herd-open-in-omodachi');
  // The Agent page the same way (an approval push's URL, built as core builds it).
  const agentUrl = new URL(back.url);
  agentUrl.search = '?open=agent&ref=approval' + (agentUrl.searchParams.get('host') ? '&host=' + agentUrl.searchParams.get('host') : '');
  await herd.goto(agentUrl.href, {waitUntil: 'networkidle'});
  await testid(herd, 'next-from-push').waitFor({timeout: 15000});
  report.results.agent_link = await testid(herd, 'next-open-app').getAttribute('href');
  await shot(herd, '03-agent-open-in-omodachi');
  // The Herd page at iPhone and iPad widths, in both languages (the
  // language is Odomo's own choice in this profile, as Settings stores it).
  for (const lang of ['en', 'zh-Hans']) {
    await herd.evaluate((value) => localStorage.setItem('odomo.language', JSON.stringify(value)), lang);
    for (const [device, size] of [['iphone', {width: 402, height: 874}], ['ipad', {width: 834, height: 1194}]]) {
      await herd.setViewportSize(size);
      await herd.goto(back.url, {waitUntil: 'networkidle'});
      await testid(herd, 'next-from-push').waitFor({timeout: 15000});
      await shot(herd, `04-herd-${device}-${lang}`);
    }
  }
  report.results.herd_title_zh = await testid(herd, 'next-waiting-title').innerText();
  await herd.close();
  report.results.ok = report.results.background_push && report.results.foreground_quiet;
} catch (error) {
  report.results.ok = false;
  report.results.error = String(error && error.stack || error);
  await shot(page, '99-failure').catch(() => {});
} finally {
  writeFileSync(join(out, PREFIX + '-report.json'), JSON.stringify(report, null, 2));
  await context.close();
  rmSync(profile, {recursive: true, force: true});
}
console.log(report.results.ok ? 'notify2 e2e: OK' : 'notify2 e2e: FAILED');
process.exit(report.results.ok ? 0 : 1);
