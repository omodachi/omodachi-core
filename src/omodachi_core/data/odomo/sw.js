// Odomo service worker (LITE-1a; push since LITE-1b).
//
// It caches nothing on purpose: Odomo is the computer's own page and only means
// anything while the computer answers, and a stale copy of the app talking to
// a newer core is exactly what serving Odomo from core is meant to rule out.
//
// Push. The host sends a declarative Web Push message ({"web_push": 8030,
// "notification": {...}}). Safari on a Home Screen app shows that by itself and
// never wakes this worker; every other browser hands it to `push` below, and
// this worker always shows it - a push that shows nothing costs a browser's
// patience (Safari revokes after three, Chrome shows its own "updated in the
// background"). The only push that may close instead of show is the host's
// retraction (`{"omodachi": "retract", "tag"}`), which the host never sends to
// Apple's push service; if one ever arrives there anyway, it is shown.
//
// A notification's URL is this host's own Odomo with ids only (which panel,
// which pane, which notification). A tap focuses an open Odomo and tells it
// where to go, or opens one there.
'use strict';

const SCOPE = new URL('./', self.location.href);

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

function safeUrl(value) {
  try {
    const url = new URL(value, SCOPE);
    if (url.origin === SCOPE.origin && url.pathname.startsWith(SCOPE.pathname)) return url.href;
  } catch (error) {
    // fall through
  }
  return SCOPE.href;
}

function clip(value, limit) {
  const text = typeof value === 'string' ? value : '';
  return text.length > limit ? text.slice(0, limit - 1) + '…' : text;
}

function parse(event) {
  try {
    return event.data ? event.data.json() : null;
  } catch (error) {
    return null;
  }
}

async function onApple() {
  try {
    const subscription = await self.registration.pushManager.getSubscription();
    return !!subscription && /(^|\.)push\.apple\.com$/.test(new URL(subscription.endpoint).hostname);
  } catch (error) {
    return false;
  }
}

async function show(message) {
  const note = message && message.web_push === 8030 && message.notification ? message.notification : {};
  const title = clip(note.title, 80) || 'Omodachi';
  await self.registration.showNotification(title, {
    body: clip(note.body, 160),
    tag: typeof note.tag === 'string' ? note.tag.slice(0, 32) : undefined,
    lang: note.lang === 'zh-Hans' ? 'zh-Hans' : 'en',
    dir: 'ltr',
    icon: new URL('icon-192.png', SCOPE).href,
    badge: new URL('icon-192.png', SCOPE).href,
    data: {url: safeUrl(note.navigate)},
  });
}

async function handlePush(message) {
  if (message && message.omodachi === 'retract' && typeof message.tag === 'string') {
    const open = await self.registration.getNotifications({tag: message.tag});
    open.forEach((notification) => notification.close());
    if (await onApple()) {
      await show({web_push: 8030, notification: {title: 'Omodachi', body: '', tag: message.tag}});
    }
    return;
  }
  await show(message);
}

self.addEventListener('push', (event) => {
  event.waitUntil(handlePush(parse(event)));
});

async function focusOrOpen(url) {
  const windows = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
  const odomo = windows.find((client) => client.url.startsWith(SCOPE.href));
  if (odomo) {
    odomo.postMessage({type: 'omodachi-open', url});
    return odomo.focus();
  }
  return self.clients.openWindow(url);
}

self.addEventListener('notificationclick', (event) => {
  const url = safeUrl(event.notification.data && event.notification.data.url);
  event.notification.close();
  event.waitUntil(focusOrOpen(url));
});

// The browser replaced the subscription (expired, or its push service rotated
// it). Subscribe again with the same host key and tell the host, with the
// credential the page keeps in this origin's IndexedDB.
function credential() {
  return new Promise((resolve) => {
    let request;
    try {
      request = indexedDB.open('odomo', 1);
    } catch (error) {
      resolve(null);
      return;
    }
    request.onupgradeneeded = () => request.result.createObjectStore('kv');
    request.onerror = () => resolve(null);
    request.onsuccess = () => {
      try {
        const get = request.result.transaction('kv', 'readonly').objectStore('kv').get('credential');
        get.onsuccess = () => resolve(get.result && typeof get.result.token === 'string' ? get.result.token : null);
        get.onerror = () => resolve(null);
      } catch (error) {
        resolve(null);
      }
    };
  });
}

async function resubscribe(event) {
  const token = await credential();
  if (!token) return;
  const headers = {Authorization: 'Bearer ' + token, Accept: 'application/json'};
  let key = event.oldSubscription && event.oldSubscription.options && event.oldSubscription.options.applicationServerKey;
  if (!key) {
    const response = await fetch(new URL('/v1/push/vapid-public-key', SCOPE).href, {headers, credentials: 'omit'});
    if (!response.ok) return;
    const raw = (await response.json()).vapid_public_key;
    const padded = raw.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (raw.length % 4)) % 4);
    key = Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
  }
  const subscription = event.newSubscription ||
    await self.registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: key});
  const json = subscription.toJSON();
  await fetch(new URL('/v1/push/subscriptions', SCOPE).href, {
    method: 'POST', credentials: 'omit',
    headers: {...headers, 'Content-Type': 'application/json'},
    body: JSON.stringify({endpoint: json.endpoint, keys: json.keys, expirationTime: json.expirationTime ?? null}),
  });
}

self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil(resubscribe(event).catch(() => {}));
});
