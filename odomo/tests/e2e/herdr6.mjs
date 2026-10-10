// HERDR-6: a new Herdr workspace from Odomo, against a real host.
//
//   node tests/e2e/herdr6.mjs <odomo-url> <out-dir>
//
// System Chrome (`channel: 'chrome'`) in a throwaway context, as odomo2.mjs.
// Pairs (the host approves it), opens the Herdr panel on SESSION, opens the
// new-workspace card from the "+" after the workspaces, browses BROWSE
// (default ~/h6test), picks PICK (default proj), names it NAME, waits for
// `<out-dir>/go` (so the caller can read the desktop's focus first), creates
// it and checks this browser landed on the new workspace's first pane. Then a
// folder outside the home folder is refused in words, and the card is drawn in
// Chinese and at phone width. Writes screenshots and `herdr6-report.json`.
import {chromium} from 'playwright-core';
import {existsSync, mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';

const [url, out = '.'] = process.argv.slice(2);
if (!url) {
  console.error('usage: node tests/e2e/herdr6.mjs <odomo-url> <out-dir>');
  process.exit(2);
}
const SESSION = process.env.SESSION || 'h6test';
const BROWSE = process.env.BROWSE || '~/h6test';
const PICK = process.env.PICK || 'proj';
const NAME = process.env.NAME || 'from-odomo';
mkdirSync(out, {recursive: true});
const report = {url, console: [], responses: [], results: {}};
const browser = await chromium.launch({channel: 'chrome', headless: process.env.HEADED !== '1'});
const context = await browser.newContext({viewport: {width: 1280, height: 800}, locale: 'en-GB'});
const page = await context.newPage();
page.on('console', (m) => { if (m.type() === 'error') report.console.push(m.text()); });
page.on('pageerror', (e) => report.console.push('pageerror: ' + e.message));
page.on('response', (r) => report.responses.push(r.status() + ' ' + r.request().method() + ' ' + new URL(r.url()).pathname));
const testid = (id) => page.locator(`[data-testid="${id}"]`);
let n = 0;
const shot = (name) => page.screenshot({path: join(out, 'HERDR-6-odomo-' + String(++n).padStart(2, '0') + '-' + name + '.png')});
const wait = (ms) => page.waitForTimeout(ms);

async function openCard(fromMenu) {
  if (fromMenu) {
    await testid('herdr-more').click();
    await testid('herdr-menu-newWorkspace').click();
  } else {
    await testid('herdr-workspace-new').first().click();
  }
  await testid('herdr-new-workspace').waitFor();
  await wait(1500);
}

try {
  await page.goto(url, {waitUntil: 'networkidle'});
  await testid('pair-start').click();
  await testid('pair-card').waitFor({timeout: 15000});
  console.log('WAITING device=' + (await testid('pair-device').innerText()));
  await testid('panel-menu').waitFor({timeout: 180000});
  await testid('bar-herdr').click();
  await testid('panel-herdr').waitFor();
  await wait(2500);
  if ((await testid('herdr-session-switcher').innerText()).trim() !== SESSION) {
    await testid('herdr-session-switcher').click();
    await testid('herdr-session-' + SESSION).click();
    await wait(3000);
  }
  report.results.session = (await testid('herdr-session-switcher').innerText()).trim();

  await openCard(false);
  report.results.recent = await page.locator('[data-testid^="herdr-folder-recent-"]').evaluateAll((els) => els.map((e) => e.textContent));
  report.results.common = await page.locator('[data-testid^="herdr-folder-common-"]').evaluateAll((els) => els.map((e) => e.textContent));
  await shot('card');
  await testid('herdr-new-workspace-path').fill(BROWSE);
  await testid('herdr-new-workspace-browse').click();
  await testid('herdr-folder-browser').waitFor();
  await testid('herdr-folder-' + PICK).click();
  await page.waitForFunction((pick) => document.querySelector('[data-testid="herdr-new-workspace-path"]').value.endsWith('/' + pick), PICK);
  report.results.path = await testid('herdr-new-workspace-path').inputValue();
  await testid('herdr-new-workspace-name').fill(NAME);
  await shot('browse');

  // The caller reads the desktop's focus, then lets this go on.
  console.log('READY');
  for (let i = 0; i < 600 && !existsSync(join(out, 'go')); i += 1) await wait(500);
  const made = page.waitForResponse((r) => r.request().method() === 'POST' && r.url().includes('/workspaces'));
  await testid('herdr-new-workspace-create').click();
  const reply = await (await made).json();
  report.results.created = reply.workspace;
  report.results.createdAt = new Date().toISOString();
  console.log('CREATED ' + reply.workspace.id + ' ' + reply.workspace.display);
  const pane = testid('herdr-pane-' + reply.workspace.pane);
  await pane.and(page.locator('[aria-pressed="true"]')).waitFor({timeout: 20000});
  report.results.landed = true;
  await wait(2000);
  await shot('landed');

  await openCard(true);
  await testid('herdr-new-workspace-path').fill('/etc');
  await testid('herdr-new-workspace-create').click();
  await testid('herdr-new-workspace-error').waitFor();
  report.results.refusedOutside = await testid('herdr-new-workspace-error').innerText();
  await testid('herdr-new-workspace-path').fill('~/.ssh');
  await testid('herdr-new-workspace-create').click();
  await page.waitForFunction(() => /keys|密钥/.test(document.querySelector('[data-testid="herdr-new-workspace-error"]')?.textContent || ''));
  report.results.refusedPrivate = await testid('herdr-new-workspace-error').innerText();
  await testid('herdr-new-workspace-path').fill('/etc');
  await testid('herdr-new-workspace-create').click();
  await wait(1500);
  await shot('refused-outside-home');
  await testid('herdr-new-workspace-cancel').click();

  await page.evaluate(() => localStorage.setItem('odomo.language', JSON.stringify('zh-Hans')));
  await page.reload({waitUntil: 'networkidle'});
  await testid('bar-herdr').click();
  await testid('panel-herdr').waitFor();
  await wait(3000);
  await openCard(true);
  await shot('zh-card');
  await testid('herdr-new-workspace-path').fill(BROWSE);
  await testid('herdr-new-workspace-browse').click();
  await testid('herdr-folder-browser').waitFor();
  await wait(800);
  await shot('zh-browse');
  await testid('herdr-new-workspace-path').fill('/etc');
  await testid('herdr-new-workspace-create').click();
  await testid('herdr-new-workspace-error').waitFor();
  report.results.refusedZh = await testid('herdr-new-workspace-error').innerText();
  await shot('zh-refused');
  await testid('herdr-new-workspace-cancel').click();

  await page.setViewportSize({width: 390, height: 844});
  await wait(1500);
  await openCard(false);
  await shot('zh-phone-card');
  await page.evaluate(() => localStorage.setItem('odomo.language', JSON.stringify('en')));
  await page.reload({waitUntil: 'networkidle'});
  await testid('bar-herdr').click();
  await testid('panel-herdr').waitFor();
  await wait(3000);
  await openCard(false);
  await shot('phone-card');
} catch (error) {
  report.results.error = String(error && error.message || error).slice(0, 400);
  await shot('failed').catch(() => {});
} finally {
  report.responses = report.responses.filter((r) => !/^(200|101|304) /.test(r)).slice(0, 50);
  writeFileSync(join(out, 'herdr6-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report.results, null, 1));
  await browser.close();
}
