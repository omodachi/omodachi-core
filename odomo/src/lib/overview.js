// OVERVIEW-1: one list of everything that wants you - the Agent conversation
// and every Herdr pane with an agent, in every running session - as core keeps
// it (`state.overview`, `overview.changed`; omodachi-core `overview.py`).
// The App's twin is omodachi-ios `Host/OverviewWire.swift` and
// `Surfaces/HerdrPanel/HerdrOverview.swift`.
//
// Rows arrive in position order (the Agent, then sessions by name and each
// session's panes in layout order) and are grouped by state here, never
// re-sorted. A question is the agent's own words and is shown verbatim.

import {fill} from './i18n.js';

export const STATES = ['needs_you', 'working', 'done'];

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

// The list, or null when the state has none (then nothing of it is drawn).
export function overviewOf(state) {
  const value = state && state.overview;
  return isObject(value) && Array.isArray(value.rows) ? value : null;
}

export function rowsOf(overview) {
  return overview && Array.isArray(overview.rows) ? overview.rows.filter((row) => isObject(row) && STATES.includes(row.state)) : [];
}

// `[{state, rows}]` for the states that have rows, in core's order.
export function groups(overview) {
  const rows = rowsOf(overview);
  return STATES.map((state) => ({state, rows: rows.filter((row) => row.state === state)}))
    .filter((group) => group.rows.length);
}

function byState(state, t) {
  if (state === 'needs_you') return t.herdr.waiting;
  if (state === 'working') return t.overview.ask.working;
  return t.overview.ask.finished;
}

// What the row asks, in a few words.
export function askText(t, row) {
  const ask = t.overview.ask;
  const question = row && typeof row.question === 'string' && row.question.trim() ? row.question.trim() : null;
  switch (row && row.ask) {
    case 'pick': return question ? fill(ask.pickQuestion, question) : ask.pick;
    case 'yes_no': return question ? fill(ask.yesNoQuestion, question) : ask.yes_no;
    case 'your_move': return question ? fill(ask.yourMove, question) : ask.finished;
    case 'waiting': return t.herdr.waiting;
    case 'approve_command': case 'approve_files': case 'approve_permissions': case 'approve': case 'answer':
    case 'working': case 'finished': case 'failed':
      return ask[row.ask];
    default: return byState(row && row.state, t);
  }
}

// Where it is: the agent, then the Agent panel or the session, workspace and
// pane (a pane named like its workspace is said once).
export function whereText(row, t) {
  if (!row) return '';
  const parts = [row.agent];
  if (row.source === 'agent') parts.push(t ? t.overview.agent : 'Agent');
  else {
    parts.push(row.session, row.workspace);
    if (row.pane !== row.workspace) parts.push(row.pane);
  }
  return parts.filter((part) => typeof part === 'string' && part.trim()).join(' · ');
}

// "Needs you 2 · Done 1 · Working 3": the non-zero counts, in that order.
export function countsText(t, counts) {
  if (!counts) return '';
  return ['needs_you', 'done', 'working'].filter((state) => Number(counts[state]) > 0)
    .map((state) => fill(t.overview.count[state], counts[state])).join(' · ');
}

export function sessionCounts(overview, name) {
  const row = overview && Array.isArray(overview.sessions) ? overview.sessions.find((s) => s && s.name === name) : null;
  if (row) return {needs_you: row.needs_you || 0, working: row.working || 0, done: row.done || 0};
  const counts = {needs_you: 0, working: 0, done: 0};
  for (const r of rowsOf(overview)) if (r.session === name) counts[r.state] += 1;
  return counts;
}

// The bar's red count: what waits on you in Herdr, every session.
export function herdrNeedsYou(overview) {
  if (!overview) return 0;
  if (Array.isArray(overview.sessions)) {
    return overview.sessions.reduce((sum, row) => sum + (row && Number(row.needs_you) > 0 ? Number(row.needs_you) : 0), 0);
  }
  return rowsOf(overview).filter((row) => row.source === 'herdr' && row.state === 'needs_you').length;
}

// A workspace chip's dot: needs_you, then done, then working; null for none.
export function workspaceWorst(overview, session, workspace) {
  const states = new Set(rowsOf(overview).filter((row) => row.session === session && row.workspace_id === workspace)
    .map((row) => row.state));
  return ['needs_you', 'done', 'working'].find((state) => states.has(state)) || null;
}

export function paneRow(overview, session, pane) {
  return rowsOf(overview).find((row) => row.session === session && row.pane_id === pane) || null;
}
