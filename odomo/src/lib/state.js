// The host state the event stream keeps: the snapshot, then each event's
// payload merged in the way core merged it (hub.update_state deep-merges a
// patch). `catalog.changed` carries no rows (PERF-4); the catalog is read
// again instead, so a merge never touches it.

export function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function merge(target, patch) {
  if (!isObject(target) || !isObject(patch)) return patch;
  const out = {...target};
  for (const [key, value] of Object.entries(patch)) {
    out[key] = isObject(value) && isObject(target[key]) ? merge(target[key], value) : value;
  }
  return out;
}

const STATE_EVENTS = new Set(['state.changed', 'host.changed', 'workspace.changed', 'bar.changed', 'agent.changed',
  'wake.changed', 'notifications.changed', 'workspace.layout_binding_changed',
  // NOTIFY-1 / OVERVIEW-1: what finished unseen, and the one "Needs you" list
  // (its rows and sessions are arrays, so a patch replaces them whole).
  'unseen.changed', 'overview.changed']);

export function applyEvent(state, event) {
  if (!state || !event || !STATE_EVENTS.has(event.type) || !isObject(event.payload)) return state;
  const {revision, ...patch} = event.payload;
  delete patch.catalog;
  const next = merge(state, patch);
  if (Number.isInteger(revision)) next.revision = revision;
  return next;
}

// The workspaces the bar draws: core's own rows, 1-10.
export function workspaces(state) {
  const items = state && state.workspace && Array.isArray(state.workspace.items) ? state.workspace.items : [];
  return items.filter((row) => isObject(row) && Number.isInteger(row.id) && row.id >= 1 && row.id <= 10);
}
