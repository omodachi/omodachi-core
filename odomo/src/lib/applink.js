// TAIL-3b L8: the links from Odomo into the Omodachi app (LITE-0 §2).
//
// The primary link is the universal link on omodachi.app: with the app
// installed it opens the app; without it, the omodachi.app page opens in a
// sheet with "Get Omodachi". `omodachi://` opens an installed app too, but
// without one Safari says the address is invalid - so it is only ever the
// small second link. The app routes exactly these paths:
//
//   https://omodachi.app/open/remote?host=<host_id>   omodachi://open/remote?host=<host_id>
//   https://omodachi.app/open/host?host=<host_id>     omodachi://open/host?host=<host_id>
//   https://omodachi.app/open/agent?host=<host_id>    (NOTIFY-2: the Agent panel)
//   https://omodachi.app/open/herdr?host=<host_id>&session=<name>&pane=<id>
//                                                     (NOTIFY-2: that pane in Herdr)
//
// The only things a link carries are this computer's host id (the one core
// puts on /health and in the pairing claim), for Herdr a session name and a
// pane id in Herdr's own alphabets, and `from=odomo`. Without a host id there
// is no `host`, and the app shows its own list of computers.
//
// ODOMO-CARD: `from=odomo` is a flag and nothing more. On an iPhone or iPad
// Odomo only ever gets past its install page inside the Home Screen web app
// (LITE-1e `installGate`), so a link from it that reaches the app says "Odomo
// is on this device, for this computer", and the app stops offering to
// install it there. Every link into the app carries it, "Get the app" too.

export const APP_ORIGIN = 'https://omodachi.app';
export const APP_SCHEME = 'omodachi:';
// LITE-1c: "Get the app" - the omodachi.app page that lists where the app is
// (App Store once it is live, TestFlight, source). Odomo never names a store
// address itself; the site does. With the app installed, iOS opens the app.
export const GET_APP = APP_ORIGIN + '/open/';
// ODOMO-CARD: the flag on every link into the app (see the top of this file).
export const FROM = 'from=odomo';

const TARGETS = ['remote', 'host', 'agent', 'herdr'];
// contracts/pairing-claim.schema.json `host_id`.
const HOST_ID = /^[0-9a-f]{32}$/;
// core herdr_bridge.SESSION_NAME and PANE_ID.
const SESSION = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
const PANE = /^w[0-9]{1,9}:p[0-9]{1,9}$/;

// A host id as core sends it, or null. Never anything made up.
export function hostIdOf(value) {
  return typeof value === 'string' && HOST_ID.test(value) ? value : null;
}

// `where` is Herdr's {session, pane}: each is kept only in Herdr's own shape,
// and a pane only together with its session.
export function appLinks(target, hostId, where) {
  const which = TARGETS.includes(target) ? target : 'host';
  const query = [];
  if (typeof hostId === 'string' && hostId) query.push('host=' + encodeURIComponent(hostId));
  if (which === 'herdr' && where && typeof where.session === 'string' && SESSION.test(where.session)) {
    query.push('session=' + encodeURIComponent(where.session));
    if (typeof where.pane === 'string' && PANE.test(where.pane)) query.push('pane=' + encodeURIComponent(where.pane));
  }
  query.push(FROM);
  const path = '/open/' + which + '?' + query.join('&');
  return {universal: APP_ORIGIN + path, scheme: APP_SCHEME + '/' + path};
}

// ODOMO-CARD: "Get the app" - the omodachi.app page, which with the app
// installed opens it (`/open/`, the app's `.app` target). It names this
// computer and Odomo too, so that tap teaches the app the same as the others.
export function getAppLink(hostId) {
  const host = hostIdOf(hostId);
  return GET_APP + '?' + (host ? 'host=' + host + '&' : '') + FROM;
}

// LITE-1c: is this an iPhone or an iPad? There a tap on a universal link opens
// the app, so Remote in the bar is the link itself; anywhere else (Android, a
// desktop) Remote stays the panel that explains it. Every browser on iOS is
// WebKit, and none of them has `navigator.userAgentData` (Chromium's); iPadOS
// Safari calls itself a Mac but has a touch screen; a Home Screen app on iOS
// also has `navigator.standalone`.
export function appleTouch(nav) {
  if (!nav || nav.userAgentData) return false;
  const ua = String(nav.userAgent || '');
  const touch = (nav.maxTouchPoints || 0) > 1;
  if (/\b(iPhone|iPad|iPod)\b/.test(ua)) return true;
  if (/\bMacintosh\b/.test(ua) && touch) return true;
  return nav.standalone === true && touch;
}
