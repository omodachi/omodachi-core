// HERDR-11, the App's (omodachi-ios `ConnectionHealth.swift`,
// `HerdrStore+Reply.swift`) rules in pure functions, so they run under
// `node --test`: one honest connection clock, which launcher a new workspace
// starts, and the lighter reads. (HERDR-12 took the latest-reply card away.)

// ── §3 one connection clock ──────────────────────────────────────────────
// Everything that proves the host is there stamps one time; the rest is
// arithmetic. Amber after 4 s, red after 15 s with the cause. Whether the
// browser is online decides the words only, never liveness.
export const TROUBLE_MS = 4000;
export const LOST_MS = 15000;
// While the page is visible and nothing has proved the host alive for this
// long, one `GET /health` (no credential, ~200 bytes) asks.
export const PROBE_MS = 2000;

export function judge(now, since, online = true) {
  const silence = now - since;
  if (silence >= LOST_MS) return online ? 'unreachable' : 'offline';
  if (silence >= TROUBLE_MS) return 'trouble';
  return 'live';
}

// The banner's words, or null when the clock says live.
export function healthWords(health, t, host, everLive = true) {
  const words = t.bar;
  if (health === 'trouble') return everLive ? words.reconnecting : fill(words.connectingTo, host);
  if (health === 'offline') return words.deviceOffline;
  if (health === 'unreachable') return fill(words.unreachable, host);
  if (health === 'herdrDown') return fill(words.herdrDown, host);
  return null;
}

function fill(text, value) {
  return String(text).replace('%s', value);
}

export const isLost = (health) => health === 'offline' || health === 'unreachable' || health === 'herdrDown';

// The mirror is current while the clock is live and its stream is open; the
// moment either stops, it is dimmed and says when it last was.
export function mirrorStale({health, observing}) {
  return health !== 'live' || !observing;
}

// ── §4 a lighter state ───────────────────────────────────────────────────
// `herdr.layout.changed` names its session; only the panel's own is worth a
// read (an older core that did not say: read).
export function layoutEventIsMine(session, mine) {
  if (!session) return true;
  return !mine || session === mine;
}

// A catalog document that says "the one you hold is current" leaves the held
// rows standing; anything else replaces them.
export function keepCatalog(held, incoming) {
  if (!incoming) return held;
  if (incoming.unchanged === true && held && held.revision === incoming.revision) return held;
  return incoming;
}

// ── §1 launchers and favourites ──────────────────────────────────────────
// What a HERDR-11 core adds to `GET /v1/herdr/folders`. Null is an older
// core: no Start row, no stars.
export function folderExtras(value) {
  const rows = (list) => (Array.isArray(list) ? list.filter((row) => row && typeof row.path === 'string'
    && typeof row.display === 'string') : null);
  const launchers = Array.isArray(value && value.launchers)
    ? value.launchers.filter((row) => row && typeof row.command === 'string' && row.command
      && typeof row.label === 'string' && row.label)
    : null;
  return {favourites: rows(value && value.favourites), launchers};
}

// A launcher remembered from another host, or one this host dropped, falls
// back to a plain shell (null) - never to something else.
export function launcherFallback(launchers, chosen) {
  if (!chosen || !Array.isArray(launchers)) return null;
  return launchers.some((row) => row.command === chosen) ? chosen : null;
}

export function isFavourite(favourites, folder) {
  return Array.isArray(favourites) && !!folder && favourites.some((row) => row.path === folder.path);
}

export function toggled(favourites, folder) {
  const list = Array.isArray(favourites) ? favourites : [];
  return isFavourite(list, folder) ? list.filter((row) => row.path !== folder.path) : list.concat(folder);
}

// An empty folder is the home folder; it is sent as `~`, which an older core
// takes too.
export function folderToSend(path) {
  const value = String(path || '').trim();
  return value || '~';
}

export function clock(date) {
  try {
    return date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  } catch {
    return '';
  }
}
