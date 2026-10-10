// LITE-1c: Odomo used to be called Lite and lived at /lite/. The host answers
// /lite/... with a redirect to /odomo/, which is the same origin - so a browser
// that paired back then still has its credential here, under the old names.
// Once, on start: what it kept moves to the new names (never over anything
// newer), the old database is deleted, and the old /lite/ service worker is
// unregistered (it and its push subscription belong to a scope that is gone;
// notifications are turned on again in Settings).
//
// This file is the only place the old names are written down.

const OLD_DB = 'omodachi-lite';
const OLD_LOCAL = 'omodachi-lite.';
const NEW_LOCAL = 'odomo.';
const OLD_SCOPE = '/lite/';

function request(value) {
  return new Promise((resolve, reject) => {
    value.onsuccess = () => resolve(value.result);
    value.onerror = () => reject(value.error);
  });
}

// localStorage: pins, the language, what was seen or hidden.
export function moveLocal(local) {
  let moved = 0;
  if (!local) return moved;
  try {
    const keys = [];
    for (let i = 0; i < local.length; i++) {
      const key = local.key(i);
      if (typeof key === 'string' && key.startsWith(OLD_LOCAL)) keys.push(key);
    }
    for (const key of keys) {
      const next = NEW_LOCAL + key.slice(OLD_LOCAL.length);
      if (local.getItem(next) === null) {
        local.setItem(next, local.getItem(key));
        moved++;
      }
      local.removeItem(key);
    }
  } catch {
    // a blocked localStorage only loses conveniences
  }
  return moved;
}

// IndexedDB: the device id, the credential, a pairing in flight.
export async function moveDatabase(idb, store) {
  if (!idb || typeof idb.databases !== 'function') return 0;
  const list = await idb.databases();
  if (!list.some((row) => row && row.name === OLD_DB)) return 0;
  let moved = 0;
  const db = await request(idb.open(OLD_DB, 1));
  try {
    if (db.objectStoreNames.contains('kv')) {
      const read = db.transaction('kv', 'readonly').objectStore('kv');
      const [keys, values] = await Promise.all([request(read.getAllKeys()), request(read.getAll())]);
      for (let i = 0; i < keys.length; i++) {
        if ((await store.get(keys[i])) === null) {
          await store.set(keys[i], values[i]);
          moved++;
        }
      }
    }
  } finally {
    db.close();
  }
  await request(idb.deleteDatabase(OLD_DB)).catch(() => {});
  return moved;
}

export async function dropOldWorker(container) {
  if (!container || typeof container.getRegistrations !== 'function') return 0;
  let dropped = 0;
  for (const registration of await container.getRegistrations()) {
    let path = '';
    try {
      path = new URL(registration.scope).pathname;
    } catch {
      path = '';
    }
    if (path === OLD_SCOPE && (await registration.unregister())) dropped++;
  }
  return dropped;
}

export async function migrate({store, idb, local, container}) {
  const result = {local: moveLocal(local), database: 0, worker: 0};
  try {
    result.database = await moveDatabase(idb, store);
  } catch {
    result.database = 0;
  }
  try {
    result.worker = await dropOldWorker(container);
  } catch {
    result.worker = 0;
  }
  return result;
}
