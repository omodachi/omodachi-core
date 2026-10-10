// What Odomo keeps in this browser, and nothing else: its device id, the
// credential the host issued it, a pairing request in flight. IndexedDB, one
// object store, one origin (the host's own). When IndexedDB is not available
// (a private window) it falls back to memory: Odomo still works, it just asks
// to pair again next time.
//
// Pins and the language choice are conveniences and live in localStorage.

const DB = 'odomo';
const STORE = 'kv';

function open() {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === 'undefined') return reject(new Error('no indexedDB'));
    const request = indexedDB.open(DB, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

const memory = new Map();
let database = null;

async function db() {
  if (database === false) return null;
  if (database) return database;
  try {
    database = await open();
  } catch {
    database = false;
    return null;
  }
  return database;
}

function run(mode, action) {
  return db().then((handle) => {
    if (!handle) return action(null);
    return new Promise((resolve, reject) => {
      const tx = handle.transaction(STORE, mode);
      const request = action(tx.objectStore(STORE));
      tx.oncomplete = () => resolve(request ? request.result : undefined);
      tx.onerror = () => reject(tx.error);
    });
  });
}

export async function get(key) {
  const value = await run('readonly', (store) => (store ? store.get(key) : null));
  return value === undefined || value === null ? (memory.has(key) ? memory.get(key) : null) : value;
}

export async function set(key, value) {
  memory.set(key, value);
  await run('readwrite', (store) => (store ? store.put(value, key) : null));
}

export async function del(key) {
  memory.delete(key);
  await run('readwrite', (store) => (store ? store.delete(key) : null));
}

export function local(key, fallback) {
  try {
    const raw = localStorage.getItem('odomo.' + key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

export function setLocal(key, value) {
  try {
    localStorage.setItem('odomo.' + key, JSON.stringify(value));
    return true;
  } catch {
    // a full or blocked localStorage only loses a convenience
    return false;
  }
}

// TUTORIAL-1: forgetting a convenience is as safe as keeping one.
export function removeLocal(key) {
  try {
    localStorage.removeItem('odomo.' + key);
    return true;
  } catch {
    // nothing to forget in a blocked localStorage
    return false;
  }
}
