// ODOMO-2: drive Odomo's Agent and Herdr panels in desktop Chrome.
//
//   node tests/e2e/odomo2.mjs <odomo-url> <out-dir> [steps]
//
// System Chrome (`channel: 'chrome'`) in a throwaway context, as chrome.mjs.
// Steps (comma separated): pair, agent, approve, herdr, keys, composer,
// quick, type, zh, phone, pad. Environment: AGENT_TEXT (the message), PANE
// (a pane id to select), TYPE_TEXT (keys typed in Type mode, `\n` is Enter),
// SESSION (a session to switch to), APPROVE_S. Writes one screenshot per
// step and `odomo2-report.json` (console errors, CSP violations, statuses).
import {chromium} from 'playwright-core';
import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';

const [url, out = '.', stepList = 'pair,agent,herdr'] = process.argv.slice(2);
if (!url) {
  console.error('usage: node tests/e2e/odomo2.mjs <odomo-url> <out-dir> [steps]');
  process.exit(2);
}
const steps = stepList.split(',');
const APPROVE_S = Number(process.env.APPROVE_S || 120);
mkdirSync(out, {recursive: true});
const report = {url, steps, console: [], csp: [], responses: [], results: {}};
const browser = await chromium.launch({channel: 'chrome', headless: process.env.HEADED !== '1'});
const context = await browser.newContext({viewport: {width: Number(process.env.WIDTH || 1280), height: Number(process.env.HEIGHT || 800)},
  locale: process.env.LOCALE || 'en-GB', permissions: process.env.PUSH ? ['notifications'] : []});
await context.addInitScript(() => {
  window.__csp = [];
  document.addEventListener('securitypolicyviolation', (e) => window.__csp.push({directive: e.violatedDirective, blocked: e.blockedURI}));
});
const page = await context.newPage();
page.on('console', (m) => { if (m.type() === 'error' || m.type() === 'warning') report.console.push(m.type() + ': ' + m.text()); });
page.on('pageerror', (e) => report.console.push('pageerror: ' + e.message));
page.on('response', (r) => report.responses.push(r.status() + ' ' + r.request().method() + ' ' + new URL(r.url()).pathname));
const testid = (id) => page.locator(`[data-testid="${id}"]`);
let n = 0;
const shot = (name) => page.screenshot({path: join(out, String(++n).padStart(2, '0') + '-' + name + '.png')});
const step = async (name, fn) => {
  if (!steps.includes(name)) return;
  const started = Date.now();
  try {
    await fn();
    report.results[name + 'Step'] = {ok: true, ms: Date.now() - started};
  } catch (error) {
    report.results[name + 'Step'] = {ok: false, error: String(error && error.message || error).slice(0, 400)};
    await shot('failed-' + name).catch(() => {});
    console.log('FAILED ' + name + ': ' + report.results[name + 'Step'].error);
  }
};

try {
  await page.goto(url, {waitUntil: 'networkidle'});
  await step('pair', async () => {
    await testid('pair-start').click();
    await testid('pair-card').waitFor({timeout: 15000});
    report.results.device = await testid('pair-device').innerText();
    console.log('WAITING device=' + report.results.device);
    await testid('panel-menu').waitFor({timeout: APPROVE_S * 1000});
  });
  await testid('bar-connection').and(page.locator('[data-status="connected"]')).waitFor({timeout: 30000});
  report.results.bar = await page.locator('[data-testid^="bar-"][aria-label]').evaluateAll((els) => els.map((e) => e.dataset.testid));

  await step('agent', async () => {
    await testid('bar-agent').click();
    await testid('panel-agent').waitFor();
    await page.waitForFunction(() => /·/.test(document.querySelector('[data-testid="agent-identity"] b')?.textContent || ''), null, {timeout: 60000});
    await shot('agent');
    if (process.env.AGENT_TEXT) {
      await testid('agent-chat-composer').fill(process.env.AGENT_TEXT);
      await testid('agent-send').click();
      await page.locator('[data-testid^="agent-message-"]').last().waitFor();
      report.results.agentSent = true;
    }
  });
  await step('approve', async () => {
    const card = page.locator('[data-testid^="agent-approval-accept-"]').first();
    await card.waitFor({timeout: Number(process.env.APPROVAL_S || 180) * 1000});
    await shot('agent-approval');
    report.results.approval = await page.locator('[data-testid^="agent-approval-"]:not([data-testid*="-accept"]):not([data-testid*="-decline"])')
      .first().innerText();
    await card.click();
    await page.locator('[data-testid^="agent-approval-resolved-"]').first().waitFor({timeout: 30000});
    await shot('agent-approved');
  });
  await step('herdr', async () => {
    await testid('bar-herdr').click();
    await testid('panel-herdr').waitFor();
    if (process.env.SESSION) {
      await testid('herdr-session-switcher').click();
      const start = testid('herdr-session-start-' + process.env.SESSION);
      if (await start.count()) await start.click();
      else await testid('herdr-session-' + process.env.SESSION).click();
      await page.waitForFunction((name) => document.querySelector('[data-testid="herdr-session-switcher"]')?.textContent.includes(name),
        process.env.SESSION, {timeout: 30000});
    }
    if (process.env.PANE) await testid('herdr-pane-' + process.env.PANE).click();
    await testid('herdr-terminal').waitFor({timeout: 30000});
    await page.waitForFunction(() => document.querySelector('[data-testid="herdr-mode"]')?.textContent === 'watching'
      || !document.querySelector('[data-testid="herdr-mode"]'), null, {timeout: 30000});
    await page.waitForTimeout(800);
    report.results.canvas = await page.locator('.mirror__canvas').evaluate((c) => ({width: c.width, height: c.height,
      css: c.style.width}));
    await shot('herdr');
  });
  await step('keys', async () => {
    for (const id of (process.env.KEYS || 'digit-1').split(' ')) {
      await testid('herdr-key-' + id).click();
      await page.waitForTimeout(700);
    }
    await shot('herdr-keys');
  });
  await step('composer', async () => {
    await testid('herdr-composer').fill(process.env.COMPOSER_TEXT || 'echo from Odomo');
    await testid('herdr-send').click();
    await testid('herdr-send-state').waitFor({timeout: 30000});
    report.results.composer = await testid('herdr-send-state').innerText();
    await shot('herdr-composer');
  });
  await step('quick', async () => {
    await testid('herdr-quick').click();
    await testid('herdr-quick-dock').waitFor();
    await shot('herdr-quick');
    const item = process.env.QUICK_ITEM || 'yes';
    const button = testid('herdr-quick-item-' + item);
    if (await button.count()) {
      await button.click();
      await testid('herdr-send-state').waitFor({timeout: 30000});
      report.results.quick = await testid('herdr-send-state').innerText();
    }
  });
  await step('type', async () => {
    await testid('herdr-type').click();
    await testid('herdr-type-strip').waitFor();
    const text = (process.env.TYPE_TEXT || 'ls\\n').replace(/\\n/g, '\n').replace(/\\e/g, '\x1b');
    for (const char of text) {
      if (char === '\n') await page.keyboard.press('Enter');
      else if (char === '\x1b') await page.keyboard.press('Escape');
      else await page.keyboard.type(char);
      await page.waitForTimeout(60);
    }
    await page.waitForTimeout(1200);
    await shot('herdr-type');
    await testid('herdr-type-stop').click();
    report.results.typeStopped = (await testid('herdr-type-strip').count()) === 0;
  });
  await step('zh', async () => {
    await page.evaluate(() => localStorage.setItem('odomo.language', JSON.stringify('zh-Hans')));
    await page.reload({waitUntil: 'networkidle'});
    await testid('bar-herdr').click();
    await testid('herdr-terminal').waitFor({timeout: 30000});
    await page.waitForTimeout(1000);
    await shot('herdr-zh');
    await testid('bar-agent').click();
    await testid('panel-agent').waitFor();
    await page.waitForTimeout(1500);
    await shot('agent-zh');
  });
  report.results.csp = await page.evaluate(() => window.__csp);
} finally {
  report.console = report.console.slice(0, 50);
  report.responses = report.responses.filter((r) => !r.startsWith('200') && !r.startsWith('101') && !r.startsWith('304')).slice(0, 50);
  writeFileSync(join(out, 'odomo2-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report.results, null, 1));
  await browser.close();
}
