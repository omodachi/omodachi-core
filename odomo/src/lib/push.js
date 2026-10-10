// LITE-1b L3: Web Push for this browser, against its own host.
//
// The host (core `push.py`) keeps one subscription per web device and sends a
// declarative Web Push message - Safari on a Home Screen app shows it with no
// script at all; every other browser hands it to `sw.js`, which shows it. The
// rules this file keeps:
//
// - Safari only subscribes inside the tap that asked for it, so the VAPID key
//   and the worker registration are read *before* the tap (`prepare`) and the
//   tap's handler calls `subscribe` first, with nothing awaited ahead of it.
// - iOS has push only in a Home Screen web app, never in a Safari tab; this
//   says which of the two it is instead of offering a switch that cannot work.
// - Turning it off unsubscribes here and tells the host; the host also drops a
//   subscription its push service says is gone (404/410).

export const KINDS = ['agent_approval', 'agent_needs_input', 'agent_done', 'herdr_blocked', 'host_notification', 'herdr_done'];
// NOTIFY-1: kinds the host keeps off until asked for.
export const OFF_BY_DEFAULT = ['herdr_done'];

export function urlBase64ToBytes(text) {
  const padded = String(text).replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (String(text).length % 4)) % 4);
  const raw = atob(padded);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

// What this browser can do: `ready`, or why not.
export function support(env = globalThis) {
  const nav = env.navigator || {};
  if (!env.isSecureContext) return {state: 'insecure'};
  if (!('serviceWorker' in nav)) return {state: 'unsupported'};
  const standalone = !!((env.matchMedia && env.matchMedia('(display-mode: standalone)').matches) || nav.standalone);
  const ios = /iPhone|iPad|iPod/.test(nav.userAgent || '') || (/Macintosh/.test(nav.userAgent || '') && (nav.maxTouchPoints || 0) > 1);
  if (!('PushManager' in env) || typeof env.Notification === 'undefined') {
    // iOS keeps both away from a Safari tab: it is the Home Screen app that has them.
    return {state: ios && !standalone ? 'add_to_home_screen' : 'unsupported'};
  }
  if (env.Notification.permission === 'denied') return {state: 'denied'};
  return {state: 'ready', ios, standalone};
}

// Before the tap: the registration and the host's key, so the tap's handler
// can subscribe without awaiting anything first.
export async function prepare(client, sw = globalThis.navigator && globalThis.navigator.serviceWorker) {
  const [registration, key] = await Promise.all([sw.ready, client.vapidKey()]);
  const existing = await registration.pushManager.getSubscription();
  return {registration, key: key.vapid_public_key, existing};
}

// Inside the tap. `subscribe` is the first thing it does.
export async function turnOn(client, prepared, {lang}) {
  const subscription = await prepared.registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToBytes(prepared.key),
  });
  const json = subscription.toJSON();
  await client.subscribePush({endpoint: json.endpoint, keys: json.keys,
    expirationTime: json.expirationTime === undefined ? null : json.expirationTime});
  return subscription;
}

export async function turnOff(client, prepared) {
  const subscription = prepared && (await prepared.registration.pushManager.getSubscription());
  if (subscription) await subscription.unsubscribe().catch(() => false);
  await client.unsubscribePush();
}

// Snooze choices: `null` (off) or hours from now.
export function snoozeUntil(hours, now = Date.now()) {
  return hours ? Math.floor(now / 1000) + hours * 3600 : null;
}

export function snoozed(prefs, now = Date.now()) {
  return !!(prefs && Number.isInteger(prefs.snooze_until) && prefs.snooze_until * 1000 > now);
}

// A notification's `navigate` (or the SW's postMessage) -> which panel to open.
// ODOMO-2: core says `herdr` now; `herd` is what a push sent before it, or an
// installed worker from then, still carries - both open the Herdr panel.
export const PANELS = ['agent', 'herdr', 'notifications'];
const ALIASES = {herd: 'herdr'};
export function openTarget(search) {
  let params;
  try {
    params = new URLSearchParams(search || '');
  } catch {
    return null;
  }
  const raw = params.get('open');
  const panel = ALIASES[raw] || raw;
  if (!PANELS.includes(panel)) return null;
  const ids = {};
  for (const key of ['ref', 'note', 'pane', 'session']) {
    const value = params.get(key);
    if (value && /^[A-Za-z0-9_.:-]{1,256}$/.test(value)) ids[key] = value;
  }
  // HERDR-4: the agent's name, so the card can say who without a pane id.
  const agent = params.get('agent');
  if (agent && /^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$/.test(agent)) ids.agent = agent;
  // NOTIFY-2: the computer the push came from, so its page can link the App.
  const host = params.get('host');
  if (host && /^[0-9a-f]{32}$/.test(host)) ids.host = host;
  return {panel, ids};
}
