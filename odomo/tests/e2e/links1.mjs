// LINKS-1: links in Odomo against a real host, in a throwaway Chrome context.
//
//   node tests/e2e/links1.mjs <odomo-url> <out-dir> <plan.json>
//
// plan.json:
//   {"locale": "en-GB",
//    "agent": {"prompt": "...", "clicks": [{"testid": "agent-link-path", "text": "report.md", "shot": "a-01"}]},
//    "herdr": {"session": "l1test", "pane": "w1:p1", "cols": 94, "rows": 40,
//              "clicks": [{"row": 12, "col": 40, "shot": "h-01"}]}}
//
// It pairs (someone approves on the computer), then for each click records
// what the page did: the new tab's address (a web link), or the notice line
// (a file opened on the computer, or why not). One screenshot per click and
// `links1-report.json`. Nothing is typed into any pane; the agent prompt goes
// to the default Agent conversation, which is the only one there is.
import {chromium} from 'playwright-core';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';

const [url, out = '.', planPath] = process.argv.slice(2);
if (!url || !planPath) {
  console.error('usage: node tests/e2e/links1.mjs <odomo-url> <out-dir> <plan.json>');
  process.exit(2);
}
const plan = JSON.parse(readFileSync(planPath, 'utf8'));
mkdirSync(out, {recursive: true});
const report = {url, console: [], csp: [], popups: [], results: []};
const browser = await chromium.launch({channel: 'chrome', headless: process.env.HEADED !== '1'});
const storage = process.env.STATE && (() => { try { return JSON.parse(readFileSync(process.env.STATE, 'utf8')); } catch { return undefined; } })();
const context = await browser.newContext({viewport: {width: Number(process.env.WIDTH || 1280), height: Number(process.env.HEIGHT || 860)},
  locale: plan.locale || 'en-GB', storageState: storage});
await context.addInitScript(() => {
  window.__csp = [];
  document.addEventListener('securitypolicyviolation', (e) => window.__csp.push({directive: e.violatedDirective, blocked: e.blockedURI}));
});
const page = await context.newPage();
page.on('console', (m) => { if (m.type() === 'error') report.console.push(m.text()); });
page.on('pageerror', (e) => report.console.push('pageerror: ' + e.message));
context.on('page', async (popup) => {
  try { await popup.waitForLoadState('domcontentloaded', {timeout: 8000}); } catch { /* a page that does not load */ }
  report.popups.push(popup.url());
  await popup.close().catch(() => {});
});
const testid = (id) => page.locator(`[data-testid="${id}"]`);
const shot = (name) => page.screenshot({path: join(out, name + '.png')});
const settle = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function notice(prefix) {
  const line = testid(prefix + '-text');
  try {
    await page.waitForFunction((id) => {
      const el = document.querySelector(`[data-testid="${id}"]`);
      return el && !/^(Opening|正在电脑上打开)/.test(el.textContent);
    }, prefix + '-text', {timeout: 15000});
    return await line.innerText();
  } catch {
    return null;
  }
}

try {
  await page.goto(url, {waitUntil: 'networkidle'});
  if (await testid('pair-start').count()) {
    await testid('pair-start').click();
    await testid('pair-card').waitFor({timeout: 15000});
    console.log('WAITING device=' + await testid('pair-device').innerText());
  }
  await testid('panel-menu').waitFor({timeout: 180000});
  if (process.env.STATE) writeFileSync(process.env.STATE, JSON.stringify(await context.storageState({indexedDB: true})));
  if (plan.agent) {
    await page.goto(new URL('?open=agent', url).href, {waitUntil: 'networkidle'});
    await testid('panel-agent').waitFor({timeout: 30000});
    if (plan.agent.prompt) {
      const before = await page.locator('[data-testid="agent-markdown"]').count();
      await testid('agent-chat-composer').fill(plan.agent.prompt);
      await testid('agent-send').click();
      await page.waitForFunction((n) => document.querySelectorAll('[data-testid="agent-markdown"]').length > n
        && !document.querySelector('[data-testid="agent-interrupt"]'), before, {timeout: 180000});
      await settle(1000);
    }
    await shot((plan.agent.prefix || 'agent') + '-00-reply');
    for (const click of plan.agent.clicks || []) {
      const target = page.locator(`[data-testid="${click.testid}"]`, {hasText: click.text}).last();
      const popups = report.popups.length;
      await page.locator('[data-testid="agent-link-notice"] .pushed__x').click().catch(() => {});
      await target.scrollIntoViewIfNeeded();
      await target.click();
      await settle(2500);
      const said = await notice('agent-link-notice');
      report.results.push({where: 'agent', ...click, notice: said, popup: report.popups.slice(popups)});
      await shot(click.shot);
      if (click.computer) {
        await testid('agent-link-notice-computer').click();
        report.results.push({where: 'agent', computer: true, notice: await notice('agent-link-notice')});
        await shot(click.shot + '-computer');
      }
    }
  }
  if (plan.herdr) {
    // A push's landing: that session, that pane (NOTIFY-2).
    await page.goto(new URL('?open=herdr&session=' + plan.herdr.session + '&pane=' + plan.herdr.pane, url).href,
      {waitUntil: 'networkidle'});
    await testid('panel-herdr').waitFor({timeout: 30000});
    await page.locator('canvas.mirror__canvas').waitFor({timeout: 30000});
    await settle(4000);
    if (plan.herdr.zoom === 'actual') await testid('herdr-zoom').click();
    await settle(1000);
    await shot((plan.herdr.prefix || 'herdr') + '-00-mirror');
    const box = await page.locator('canvas.mirror__canvas').boundingBox();
    for (const click of plan.herdr.clicks || []) {
      const x = box.x + (click.col + 0.5) * (box.width / plan.herdr.cols);
      const y = box.y + (click.row + 0.5) * (box.height / plan.herdr.rows);
      const popups = report.popups.length;
      // Each click is read on its own: the line the last one left goes first.
      await page.locator('[data-testid="herdr-link-notice"] .pushed__x').click().catch(() => {});
      await page.mouse.click(x, y);
      await settle(2500);
      const said = await notice('herdr-link-notice');
      report.results.push({where: 'herdr', ...click, notice: said, popup: report.popups.slice(popups)});
      await shot(click.shot);
    }
  }
} catch (error) {
  report.error = String(error && error.stack || error);
  await shot('error').catch(() => {});
} finally {
  report.csp = await page.evaluate(() => window.__csp || []).catch(() => []);
  writeFileSync(join(out, 'links1-report.json'), JSON.stringify(report, null, 2));
  await browser.close();
}
