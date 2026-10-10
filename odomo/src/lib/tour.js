// TUTORIAL-1: the first-steps tour, Odomo's half (the App's is
// omodachi-ios `Host/FirstStepsTour.swift`). At most six short stops along
// the bar, shown once after this browser is first paired, skippable at every
// stop, and again from Settings. It points at the bar and never opens a
// panel, so walking it sends nothing to the host.
//
// What it remembers is this browser's own: "seen" is the `tour` hint
// (lib/hints.js), "still to show" is `odomo.tour.pending`, both through the
// try/catch wrappers in lib/storage.js, with memory standing in when
// localStorage is not there.
import * as storage from './storage.js';
import {dismissHint, hintSeen, resetHints} from './hints.js';

export const STEPS = ['bar', 'remote', 'agent', 'herdr', 'notifications', 'settings'];
export const START_DELAY_MS = 600;

// Only when localStorage could not keep it.
let memoryPending = false;

export function tourPending() {
  return storage.local('tour.pending', false) === true || memoryPending;
}

function setPending(value) {
  const kept = value ? storage.setLocal('tour.pending', true) : storage.removeLocal('tour.pending');
  memoryPending = value && !kept;
}

// The stops there is something on the bar for: an entry switched off in
// Settings has no slot to point at. The bar and Settings are always there.
export function availableSteps(panels) {
  const on = new Set(Array.isArray(panels) ? panels : []);
  return STEPS.filter((id) => id === 'bar' || id === 'settings' || on.has(id));
}

// A pairing landed. Arms the tour unless this browser has seen it.
export function armTour() {
  if (hintSeen('tour')) return false;
  setPending(true);
  return true;
}

// The panel stage is on panel 1: an armed tour starts. Returns its state.
export function startTour(panels) {
  if (!tourPending() || hintSeen('tour')) return null;
  const steps = availableSteps(panels);
  return steps.length ? {steps, index: 0} : null;
}

// "Show the tour again": every hint comes back and the tour starts at once.
export function replayTour(panels) {
  resetHints();
  setPending(true);
  return {steps: availableSteps(panels), index: 0};
}

// Next, or the end after the last stop (`null`).
export function nextStop(state) {
  if (!state) return null;
  if (state.index + 1 < state.steps.length) return {...state, index: state.index + 1};
  finishTour();
  return null;
}

// Skip, Done, and the person going into a panel themselves: seen.
export function finishTour() {
  setPending(false);
  dismissHint('tour');
}

// Where the callout goes, beside its target on the content side of the
// left-edge bar: as wide as allowed (at most `max`), centred on the target
// and slid to stay in the window. `rect` and the result are {x, y, w, h}.
export function calloutFrame({width, height, rect, cardHeight, margin = 12, gap = 10, max = 360}) {
  if (!rect) {
    const w = Math.max(0, Math.min(max, width - 2 * margin));
    const h = Math.min(cardHeight, height - 2 * margin);
    return {x: (width - w) / 2, y: (height - h) / 2, w, h};
  }
  const x = Math.max(margin, rect.x + rect.w + gap);
  const w = Math.max(0, Math.min(max, width - margin - x));
  const h = Math.min(cardHeight, height - 2 * margin);
  const centred = rect.y + rect.h / 2 - h / 2;
  const y = Math.max(margin, Math.min(centred, height - margin - h));
  return {x, y, w, h};
}
