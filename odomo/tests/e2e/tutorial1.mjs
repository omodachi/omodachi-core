// TUTORIAL-1: walk Odomo's first-steps tour in the system Chrome, in a
// fresh throwaway context per size and language, against <odomo-url>
// (a host that approves the pairing - a lab VM or a local harness).
//
//   node tests/e2e/tutorial1.mjs <odomo-url> <out-dir>
//
// Per size x language: pair -> the tour starts on panel 1 -> one screenshot
// per stop -> Done -> Settings shows "Show the tour again". Then, once: Skip
// midway; a tap on the bar steps out of a replayed tour; the replay brings a
// put-away tip back; a reload does not bring the tour back. Writes
// `tutorial1-report.json` with every console error and CSP violation.
import {chromium} from 'playwright-core';
import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';

const [url, out = '.'] = process.argv.slice(2);
if (!url) {
  console.error('usage: node tests/e2e/tutorial1.mjs <odomo-url> <out-dir>');
  process.exit(2);
}
mkdirSync(out, {recursive: true});
const SIZES = [
  ['desktop', 1280, 800], ['ipad-landscape', 1180, 820], ['ipad-portrait', 820, 1180],
  ['phone-portrait', 390, 844], ['phone-landscape', 844, 390],
];
const LANGS = [['en', 'en-GB'], ['zh', 'zh-CN']];
const STEPS = ['bar', 'remote', 'agent', 'herdr', 'notifications', 'settings'];
const report = {url, console: [], csp: [], errors: {}, results: {}, failures: []};
const browser = await chromium.launch({channel: 'chrome', headless: process.env.HEADED !== '1'});

async function open(width, height, locale) {
  const context = await browser.newContext({viewport: {width, height}, locale, deviceScaleFactor: 2});
  await context.addInitScript(() => {
    window.__csp = [];
    document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));
  });
  const page = await context.newPage();
  page.on('console', (m) => { if (m.type() === 'error') report.console.push(m.text()); });
  page.on('pageerror', (e) => report.console.push('pageerror: ' + e.message));
  page.on('response', (r) => {
    if (r.status() < 400) return;
    const key = r.status() + ' ' + r.request().method() + ' ' + new URL(r.url()).pathname;
    report.errors[key] = (report.errors[key] || 0) + 1;
  });
  const testid = (id) => page.locator(`[data-testid="${id}"]`);
  await page.goto(url, {waitUntil: 'networkidle'});
  await testid('pair-start').click({timeout: 15000});
  await testid('panel-menu').waitFor({timeout: 60000});
  return {context, page, testid};
}

const check = (ok, what) => {
  if (!ok) report.failures.push(what);
};

for (const [size, width, height] of SIZES) {
  for (const [lang, locale] of LANGS) {
    const tag = size + '-' + lang;
    const {context, page, testid} = await open(width, height, locale);
    try {
      for (const [index, step] of STEPS.entries()) {
        await testid('tour-step-' + step).waitFor({timeout: 10000});
        await page.waitForTimeout(350);
        const box = await testid('tour-card').boundingBox();
        check(box && box.x >= 0 && box.y >= 0 && box.x + box.width <= width + 1 && box.y + box.height <= height + 1,
          tag + ' ' + step + ' callout off screen');
        // Every word is readable: shown whole, or scrollable only when the
        // callout already takes all the height there is.
        const fit = await testid('tour-card').evaluate((el) => {
          const words = el.querySelector('.tour__words');
          return {clipped: words.scrollHeight > words.clientHeight + 1, card: el.offsetHeight, room: innerHeight - 24};
        });
        check(!fit.clipped || fit.card >= fit.room - 2, tag + ' ' + step + ' words cut: ' + JSON.stringify(fit));
        await page.screenshot({path: join(out, `TUTORIAL-1-odomo-${tag}-${index + 1}-${step}.png`)});
        await testid(index === STEPS.length - 1 ? 'tour-done' : 'tour-next').click();
      }
      await testid('tour').waitFor({state: 'detached', timeout: 5000});
      await testid('bar-settings').click();
      await testid('settings-tour-replay').waitFor({timeout: 5000});
      await page.screenshot({path: join(out, `TUTORIAL-1-odomo-${tag}-7-settings-row.png`)});
      if (size === 'desktop' || size === 'phone-portrait') {
        await testid('bar-agent').click();
        await testid('agent-new').waitFor({timeout: 10000});
        await page.waitForTimeout(400);
        await page.screenshot({path: join(out, `TUTORIAL-1-odomo-${tag}-8-agent-new-icon.png`)});
      }
      report.results[tag] = 'walked';
      report.csp.push(...(await page.evaluate(() => window.__csp)));
    } catch (error) {
      report.failures.push(tag + ': ' + error.message.split('\n')[0]);
    }
    await context.close();
  }
}

// The rules, once, at desktop size.
{
  const {context, page, testid} = await open(1280, 800, 'en-GB');
  try {
    await testid('tour-step-bar').waitFor({timeout: 10000});
    await testid('tour-next').click();
    await testid('tour-step-remote').waitFor();
    await testid('tour-skip').click();
    await testid('tour').waitFor({state: 'detached', timeout: 5000});
    report.results.skip = 'ended';
    // Put the Herdr tip away, then Show the tour again (AGENT-PLAN-1: the
    // Agent panel has no tip any more).
    await testid('bar-herdr').click();
    await testid('hint-herdr').waitFor({timeout: 10000});
    await testid('hint-herdr').locator('button').click();
    await testid('hint-herdr').waitFor({state: 'detached'});
    await testid('bar-settings').click();
    await testid('settings-tour-replay').click();
    await testid('tour-step-bar').waitFor({timeout: 5000});
    await page.screenshot({path: join(out, 'TUTORIAL-1-odomo-desktop-en-8-replay-from-settings.png')});
    // A tap on the bar steps out of the tour, and still opens the panel.
    await testid('bar-herdr').click();
    await testid('tour').waitFor({state: 'detached', timeout: 5000});
    await testid('hint-herdr').waitFor({timeout: 10000});
    await page.screenshot({path: join(out, 'TUTORIAL-1-odomo-desktop-en-9-tip-back.png')});
    report.results.replay = 'tip back, tap stepped out';
    // A reload: seen, so no tour.
    await page.reload({waitUntil: 'networkidle'});
    await testid('panel-menu').waitFor({timeout: 30000}).catch(() => {});
    await page.waitForTimeout(1500);
    check(await testid('tour').count() === 0, 'tour came back after a reload');
    // Escape skips; keyboard focus is on the stop's heading.
    await testid('bar-settings').click();
    await testid('settings-tour-replay').click();
    await testid('tour-step-bar').waitFor({timeout: 5000});
    const focused = await page.evaluate(() => document.activeElement && document.activeElement.dataset.testid);
    check(focused === 'tour-step-bar', 'focus did not move to the callout: ' + focused);
    await page.keyboard.press('Escape');
    await testid('tour').waitFor({state: 'detached', timeout: 5000});
    report.results.keyboard = 'focus on the heading, Escape skips';
    // Reduce motion: no transition on the callout.
    await page.emulateMedia({reducedMotion: 'reduce', colorScheme: 'light'});
    await testid('settings-tour-replay').click();
    await testid('tour-step-bar').waitFor({timeout: 5000});
    const motion = await testid('tour-card').evaluate((el) => getComputedStyle(el).transitionDuration);
    check(/^0s/.test(motion), 'reduced motion still moves: ' + motion);
    report.results.reducedMotion = motion;
  } catch (error) {
    report.failures.push('rules: ' + error.message.split('\n')[0]);
  }
  report.csp.push(...(await page.evaluate(() => window.__csp)));
  await context.close();
}

await browser.close();
writeFileSync(join(out, 'tutorial1-report.json'), JSON.stringify(report, null, 2));
console.log(JSON.stringify({results: report.results, failures: report.failures, errors: report.errors,
  console: report.console.length,
  csp: report.csp.length}, null, 2));
process.exit(report.failures.length ? 1 : 0);
