// TUTORIAL-1: the first-steps tour - shown once, skip, replay brings the
// tips back, works without localStorage - and where its callout lands.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runnerImport} from 'vite';
import {HINTS, dismissHint, hintSeen, resetHints} from '../src/lib/hints.js';
import {STEPS, armTour, availableSteps, calloutFrame, finishTour, nextStop, replayTour, startTour, tourPending}
  from '../src/lib/tour.js';
import {PANELS, barPanels} from '../src/lib/panels.js';
import {en, zh} from '../src/lib/i18n.js';

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    map,
  };
}

function fresh() {
  globalThis.localStorage = memoryStorage();
  return globalThis.localStorage;
}

function walk(state) {
  const seen = [];
  while (state) {
    seen.push(state.steps[state.index]);
    state = nextStop(state);
  }
  return seen;
}

test('armed by a pairing, it walks every stop once and never comes back by itself', () => {
  const store = fresh();
  assert.equal(startTour(PANELS), null, 'nothing armed, nothing starts');
  assert.equal(armTour(), true);
  assert.equal(tourPending(), true);
  assert.equal(store.getItem('odomo.tour.pending'), 'true', 'kept across a reload');
  const state = startTour(barPanels([]));
  assert.deepEqual(state, {steps: STEPS, index: 0});
  assert.ok(STEPS.length <= 6);
  assert.deepEqual(walk(state), ['bar', 'remote', 'agent', 'herdr', 'notifications', 'settings']);
  assert.equal(tourPending(), false);
  assert.equal(hintSeen('tour'), true);
  assert.equal(store.getItem('odomo.hint.tour'), 'true');
  assert.equal(armTour(), false, 'a second pairing does not bring it back');
  assert.equal(startTour(PANELS), null);
});

test('skip midway ends it as seen', () => {
  fresh();
  armTour();
  let state = startTour(PANELS);
  state = nextStop(state);
  assert.equal(state.steps[state.index], 'remote');
  finishTour();
  assert.equal(tourPending(), false);
  assert.equal(hintSeen('tour'), true);
});

test('Show the tour again brings the tour and every tip back, and only the tour is put away after', () => {
  const store = fresh();
  dismissHint('herdr');
  armTour();
  finishTour();
  assert.equal(hintSeen('herdr'), true);
  const state = replayTour(PANELS);
  assert.equal(state.steps[0], 'bar');
  for (const id of HINTS) assert.equal(hintSeen(id), false, id);
  assert.equal(store.getItem('odomo.hint.herdr'), null, 'written down, not just in memory');
  walk(state);
  assert.equal(hintSeen('tour'), true);
  assert.equal(hintSeen('herdr'), false, 'finishing the tour leaves the tips alone');
});

test('the tips keep the keys they always had', () => {
  const store = fresh();
  store.setItem('odomo.hint.herdr', 'true');
  assert.equal(hintSeen('herdr'), true, 'a tip put away before this build stays away');
  // AGENT-PLAN-1: no Agent tip - the slash list it pointed at is gone.
  assert.deepEqual(HINTS, ['herdr', 'tour']);
});

test('without localStorage (missing, or throwing) it still works, in memory', () => {
  delete globalThis.localStorage;
  assert.equal(armTour(), true);
  assert.equal(tourPending(), true);
  const state = startTour(PANELS);
  assert.equal(state.steps.length, 6);
  walk(state);
  assert.equal(hintSeen('tour'), true);
  assert.equal(armTour(), false);
  resetHints();
  globalThis.localStorage = {getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); },
    removeItem() { throw new Error('blocked'); }};
  assert.equal(hintSeen('herdr'), false);
  dismissHint('herdr');
  assert.equal(hintSeen('herdr'), true);
  replayTour(PANELS);
  assert.equal(hintSeen('herdr'), false);
  finishTour();
  resetHints();
  delete globalThis.localStorage;
});

test('an entry switched off has no stop; the bar and Settings always do', () => {
  assert.deepEqual(availableSteps(barPanels(['remote', 'herdr'])), ['bar', 'agent', 'notifications', 'settings']);
  assert.deepEqual(availableSteps(barPanels(PANELS)), ['bar', 'settings']);
  fresh();
  assert.equal(replayTour(barPanels(['agent'])).steps.length, 5);
});

test('the callout opens beside its entry, on screen, on a phone and on a desktop', () => {
  const phone = {width: 390, height: 844};
  const entry = {x: 0, y: 760, w: 44, h: 44};
  const f = calloutFrame({...phone, rect: entry, cardHeight: 220});
  assert.ok(f.x >= 44 + 10, 'to the right of the bar');
  assert.ok(f.x + f.w <= 390 - 12);
  assert.ok(f.y + f.h <= 844 - 12, 'slid up to stay on screen');
  const desk = calloutFrame({width: 1280, height: 800, rect: {x: 0, y: 300, w: 64, h: 44}, cardHeight: 200});
  assert.equal(desk.w, 360);
  assert.equal(desk.y + desk.h / 2, 322, 'centred on the entry');
  const tall = calloutFrame({width: 844, height: 390, rect: {x: 0, y: 0, w: 44, h: 390}, cardHeight: 2000});
  assert.equal(tall.h, 390 - 24, 'cut to the window; the words scroll inside');
  const none = calloutFrame({width: 400, height: 800, rect: null, cardHeight: 100});
  assert.equal(none.x + none.w / 2, 200);
});

test('every stop has words in both languages, and the same keys', () => {
  for (const id of STEPS) {
    for (const t of [en, zh]) {
      assert.ok(t.tour.steps[id].title && t.tour.steps[id].body, id);
    }
  }
  assert.deepEqual(Object.keys(en.tour).sort(), Object.keys(zh.tour).sort());
  for (const t of [en, zh]) assert.ok(t.settings.tourReplay && t.settings.tourReplayDetail && t.settings.tourGroup);
  // Odomo says Remote is the app's, and never promises a screen of its own.
  assert.match(en.tour.steps.remote.body, /Omodachi app/);
  assert.match(zh.tour.steps.remote.body, /Omodachi app/);
});

const {module: render} = await runnerImport(new URL('./render.jsx', import.meta.url).pathname,
  {root: new URL('..', import.meta.url).pathname, configFile: false, logLevel: 'silent'});

test('a stop is a labelled, skippable callout; the last one says Done', () => {
  const first = render.renderTour({steps: STEPS, index: 0});
  assert.match(first, /data-testid="tour-step-bar"/);
  assert.match(first, /role="dialog"/);
  assert.match(first, /aria-label="Tour"/);
  assert.match(first, /data-testid="tour-skip"/);
  assert.match(first, /data-testid="tour-next"/);
  assert.match(first, /Step 1 of 6/);
  const last = render.renderTour({steps: STEPS, index: 5}, 'zh-Hans');
  assert.match(last, /data-testid="tour-done"/);
  assert.doesNotMatch(last, /data-testid="tour-skip"/);
  assert.match(last, /第 6 步，共 6 步/);
  assert.match(last, /完成/);
});

test('Settings has the row, and the panels share one tip component', () => {
  const settings = readFileSync(new URL('../src/panels/SettingsPanel.jsx', import.meta.url), 'utf8');
  // ODOMO-SETTINGS-1: the row is a SettingAction now, given its testid as a prop
  // (the page still carries data-testid="settings-tour-replay"; odomosettings1 checks the markup).
  assert.match(settings, /testid="settings-tour-replay"/);
  for (const panel of ['HerdrPanel.jsx', 'AgentPanel.jsx']) {
    const source = readFileSync(new URL('../src/panels/' + panel, import.meta.url), 'utf8');
    assert.doesNotMatch(source, /function OnceHint/, panel);
    assert.doesNotMatch(source, /setLocal\('hint\./, panel);
    // AGENT-PLAN-1: only Herdr still has a tip.
    if (panel === 'HerdrPanel.jsx') assert.match(source, /OnceHint id=/, panel);
    else assert.doesNotMatch(source, /OnceHint/, panel);
  }
  // Nothing about the tour goes to the host.
  for (const file of ['../src/lib/tour.js', '../src/lib/hints.js', '../src/Tour.jsx']) {
    const source = readFileSync(new URL(file, import.meta.url), 'utf8');
    assert.doesNotMatch(source, /fetch\(|client\.|WebSocket/, file);
  }
});
