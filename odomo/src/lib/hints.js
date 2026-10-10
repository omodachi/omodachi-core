// TUTORIAL-1: the one registry for everything Odomo explains once - the
// Herdr tip (HERDR-4) and the first-steps tour - so "Show the tour again" in
// Settings is one reset that brings them all back. The same as the App's
// `OnceHints` (omodachi-ios `Host/OnceHints.swift`). AGENT-PLAN-1: the Agent
// tip ("Tap / to see the commands") went with the slash list it pointed at.
//
// Remembered in this browser's localStorage under the keys the tips have
// always used (`odomo.hint.<id>`), so a tip put away before this build stays
// away. localStorage can be missing or blocked (a private window, a full
// disk): then this page remembers in memory, and Odomo works the same until
// it is reloaded. Nothing here is ever sent to the host.
import * as storage from './storage.js';

export const HINTS = ['herdr', 'tour'];

// Only what localStorage could not keep.
const memory = new Set();

export function hintSeen(id) {
  return storage.local('hint.' + id, false) === true || memory.has(id);
}

export function dismissHint(id) {
  if (storage.setLocal('hint.' + id, true)) memory.delete(id);
  else memory.add(id);
}

export function resetHints() {
  for (const id of HINTS) {
    memory.delete(id);
    storage.removeLocal('hint.' + id);
  }
}
