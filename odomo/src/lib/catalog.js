// The Omarchy menu as core publishes it (`GET /v1/catalog`,
// contracts/catalog.schema.json), turned into what the Menu half draws.
//
// Rows hang off `parent_id` from `root`. A row is drawn when core says it is
// visible (or when its `when` could not be answered, MENU-3: the host decides
// on the tap). What a tap does depends on the row's route:
//   host     core runs it: `POST /v1/actions/{id}:invoke`
//   native   `shortcuts` opens the Keybindings half here; others are the App's
//   terminal / desktop   a surface only the App has: drawn, not runnable here
// `route.confirm` rows (A-68: power, erase, update…) take a second tap.

export const ROOT_GLYPH = {apps: 'apps', learn: 'book', trigger: 'bolt', style: 'style', setup: 'setup',
  install: 'install', remove: 'remove', update: 'update', about: 'about', system: 'power', omodachi: 'symbol'};

function shown(row) {
  if (!row || typeof row.id !== 'string' || typeof row.label !== 'string') return false;
  if (row.visible === true) return true;
  const when = row.conditions && row.conditions.when;
  return row.visible == null && !!when && when.status === 'unknown';
}

export function buildTree(catalog) {
  const entries = catalog && Array.isArray(catalog.entries) ? catalog.entries : [];
  const byId = new Map();
  const children = new Map();
  for (const row of entries) {
    if (!shown(row) || row.id === 'root') continue;
    byId.set(row.id, row);
  }
  for (const row of byId.values()) {
    const parent = row.parent_id || 'root';
    if (parent !== 'root' && !byId.has(parent)) continue;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(row);
  }
  for (const list of children.values()) list.sort((a, b) => (a.order ?? 0) - (b.order ?? 0) || a.label.localeCompare(b.label));
  // Only what hangs off root is in the menu (provider rows such as the
  // keybindings and workspace entries live elsewhere).
  const reachable = new Set();
  const walk = (id) => (children.get(id) || []).forEach((row) => { reachable.add(row.id); walk(row.id); });
  walk('root');
  return {
    revision: catalog && typeof catalog.revision === 'string' ? catalog.revision : null,
    byId: new Map([...byId].filter(([id]) => reachable.has(id))),
    childrenOf: (id) => (children.get(id) || []).filter((row) => reachable.has(row.id)),
  };
}

export function action(row) {
  const route = (row && row.route) || {};
  if (route.route === 'host') return route.supported && route.ready ? 'run' : 'unavailable';
  if (route.route === 'native') return route.native_view === 'shortcuts' ? 'keybindings' : 'app';
  if (route.route === 'terminal' || route.route === 'desktop') return 'app';
  return 'unavailable';
}

export function needsConfirm(row) {
  return !!(row && row.route && row.route.confirm === true);
}

export function pathOf(tree, id, rootLabel) {
  const parts = [];
  let cur = tree.byId.get(id);
  while (cur) {
    parts.unshift(cur.label);
    cur = cur.parent_id && cur.parent_id !== 'root' ? tree.byId.get(cur.parent_id) : null;
  }
  return parts.length ? parts.join(' / ') : rootLabel;
}

export function rootOf(tree, id) {
  let cur = tree.byId.get(id);
  while (cur && cur.parent_id && cur.parent_id !== 'root') cur = tree.byId.get(cur.parent_id);
  return cur ? cur.id : '';
}

export function search(tree, query, limit = 60) {
  const q = String(query || '').trim().toLowerCase();
  if (!q) return [];
  const hits = [];
  for (const row of tree.byId.values()) {
    const aliases = Array.isArray(row.aliases) ? row.aliases.join(' ') : '';
    if ((row.label + ' ' + aliases).toLowerCase().includes(q)) hits.push(row);
    if (hits.length >= limit) break;
  }
  return hits;
}

// Keybinding rows (`GET /v1/shortcuts`): drawn unless hidden - or, with
// Settings' "Show bindings that cannot run here" (CLIP-1 §1), drawn greyed.
export function shortcutRows(snapshot, showHidden = false) {
  const items = snapshot && Array.isArray(snapshot.items) ? snapshot.items : [];
  return items.filter((row) => row && typeof row.id === 'string' && typeof row.label === 'string'
    && (showHidden || row.hidden !== true))
    .sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
}

// A-68's two taps, as the App does them (HomeStore.passesConfirm): the first
// tap on a confirm row arms it for `window` ms and sends nothing; a tap on the
// armed row sends. Any other row's tap disarms.
export function confirmTap(armed, id, confirm, now, window = 3000) {
  if (!confirm) return {send: true, armed: null};
  if (armed && armed.id === id && armed.until > now) return {send: true, armed: null};
  return {send: false, armed: {id, until: now + window}};
}
