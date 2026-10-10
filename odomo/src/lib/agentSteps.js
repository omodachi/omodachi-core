// AGENT-COLLIE-1: what a long codex turn looks like in the panel - each step
// in a few words, runs of steps folded into one line, every edit as a card
// with its change, and a way to find your place again. The App's
// `Agent/AgentChatSteps.swift`, as plain functions.
//
// The run folding (`groupRuns`, `LIVE_TAIL`), the step line and the find
// stepping (`step`, `splitHighlight`) are ported from Collie
// (https://github.com/AltanS/collie, web/src/components/chat-cards.tsx and
// web/src/lib/transcript-search.ts), MIT, Copyright (c) 2026 Altan Sarisin -
// see README.md, Provenance. Ported to core's own rows: the data is codex's
// structured items as core projects them, never a parsed screen.

import {fill} from './i18n.js';

// ── change cards ──────────────────────────────────────────────────────────

const str = (v) => (typeof v === 'string' && v !== '' ? v : null);
const count = (v) => (Number.isInteger(v) && v >= 0 ? v : 0);

// core's `changes[]`, read leniently: a change of a shape this page does not
// expect is dropped, never a reason to lose the row.
export function changesOf(value) {
  if (!Array.isArray(value)) return null;
  const rows = value.slice(0, 16).map((c) => {
    if (!c || typeof c !== 'object' || !str(c.path) || c.path.length > 4096) return null;
    if (!['add', 'delete', 'update'].includes(c.kind)) return null;
    return {path: c.path, kind: c.kind, movePath: str(c.move_path), added: count(c.added), removed: count(c.removed),
      diff: typeof c.diff === 'string' && c.diff !== '' ? c.diff.split('\n') : [],
      diffOmitted: Number.isInteger(c.diff_omitted) && c.diff_omitted > 0 ? c.diff_omitted : 0};
  }).filter(Boolean);
  return rows.length ? rows : null;
}

// core's `actions[]` (codex's `commandActions`), leniently.
export function actionsOf(value) {
  if (!Array.isArray(value)) return null;
  const rows = value.slice(0, 4).map((a) => {
    if (!a || typeof a !== 'object' || !['read', 'listFiles', 'search', 'unknown'].includes(a.type)) return null;
    const row = {type: a.type};
    for (const key of ['name', 'path', 'query']) if (str(a[key])) row[key] = a[key];
    return row;
  }).filter(Boolean);
  return rows.length ? rows : null;
}

export function fileName(path) {
  const parts = String(path).replace(/\/+$/, '').split('/').filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(path);
}

export function changeVerb(t, change) {
  if (change.kind === 'add') return t.agent.stepCreated;
  if (change.kind === 'delete') return t.agent.stepDeleted;
  return change.movePath ? t.agent.stepMoved : t.agent.stepEdited;
}

// Where a moved file went: its name, or its folder and name when the name
// stayed the same (as the App).
export function moveName(change) {
  if (!change.movePath) return null;
  const moved = fileName(change.movePath);
  if (moved !== fileName(change.path)) return moved;
  const parts = change.movePath.split('/').filter(Boolean);
  return parts.length >= 2 ? parts.slice(-2).join('/') : moved;
}

export function changeSubject(change) {
  return change.movePath ? fileName(change.path) + ' → ' + moveName(change) : fileName(change.path);
}

// One drawn diff line: a hunk (drawn "⋯"), added, removed or context. A
// `\ No newline at end of file` note is not a line.
export function diffLines(change) {
  const out = [];
  for (const raw of change.diff) {
    if (raw.startsWith('@@')) out.push({kind: 'hunk', text: ''});
    else if (raw.startsWith('\\')) continue;
    else if (raw.startsWith('+')) out.push({kind: 'add', text: raw.slice(1)});
    else if (raw.startsWith('-')) out.push({kind: 'del', text: raw.slice(1)});
    else out.push({kind: 'ctx', text: raw.startsWith(' ') ? raw.slice(1) : raw});
  }
  return out;
}

// A card shows this many diff lines before "Show N more lines".
export const DIFF_PREVIEW = 12;

// ── steps ─────────────────────────────────────────────────────────────────

// Core sends these only for a commandExecution.
export function isCommand(row) {
  return !!row.command || !!(row.actions && row.actions.length) || Number.isInteger(row.exitCode);
}

export function stepKind(row) {
  if (row.changes && row.changes.length) return 'edit';
  const kinds = new Set((row.actions || []).map((a) => a.type));
  if (kinds.size && [...kinds].every((k) => k === 'read' || k === 'listFiles' || k === 'search')) {
    return kinds.has('read') && kinds.size === 1 ? 'read' : 'search';
  }
  return isCommand(row) ? 'command' : 'other';
}

function commandLine(row) {
  const words = row.command || row.name || '';
  return words.split('\n')[0];
}

// "$ git status", "Read app.py", "Searched TODO", "Edited app.py".
export function stepLine(t, row) {
  if (row.changes && row.changes.length) {
    const first = row.changes[0];
    const more = row.changes.length - 1;
    const subject = more > 0 ? fill(t.agent.stepAndMore, changeSubject(first), more) : changeSubject(first);
    return {verb: changeVerb(t, first), subject, mono: true};
  }
  switch (stepKind(row)) {
    case 'read': {
      const names = (row.actions || []).map((a) => a.name).filter(Boolean);
      return {verb: t.agent.stepRead, subject: names.length ? names.join(', ') : commandLine(row), mono: true};
    }
    case 'search': {
      const action = (row.actions || []).find((a) => a.type !== 'read') || row.actions[0];
      // codex names the folder only when the command did.
      if (action.type === 'listFiles') {
        return action.path ? {verb: t.agent.stepListed, subject: action.path, mono: true}
          : {verb: '$', subject: commandLine(row), mono: true};
      }
      return {verb: t.agent.stepSearched, subject: action.query || action.path || commandLine(row), mono: true};
    }
    case 'command':
      return {verb: '$', subject: commandLine(row), mono: true};
    default:
      return {verb: '', subject: row.name || '', mono: false};
  }
}

export function stepText(t, row) {
  const line = stepLine(t, row);
  return line.verb ? line.verb + ' ' + line.subject : line.subject;
}

// How many files an edit step changed (a run counts files, not rows).
export function editCount(row) {
  return (row.changes ? row.changes.length : 0) + (row.changesOmitted || 0);
}

// ── runs ──────────────────────────────────────────────────────────────────

// A working run shows its newest steps (Collie `LIVE_TAIL`).
export const LIVE_TAIL = 6;

const isStep = (entry) => entry.kind === 'row' && entry.row.kind === 'tool';

// Splits the timeline into single entries and runs: `minRun` steps or more in
// a row become a run, fewer stay as they are; an answered line rides with
// the steps before it; a message ends a run (Collie `groupRuns`, minRun 3).
export function groupRuns(entries, minRun = 3) {
  const out = [];
  let pending = [];
  const flush = () => {
    const steps = pending.filter(isStep);
    if (steps.length >= minRun) out.push({kind: 'run', id: 'run:' + steps[0].row.itemID, entries: pending});
    else for (const entry of pending) out.push({kind: 'entry', id: entry.id, entry});
    pending = [];
  };
  for (const entry of entries) {
    if (isStep(entry) || (entry.kind === 'answered' && pending.length)) pending.push(entry);
    else {
      flush();
      out.push({kind: 'entry', id: entry.id, entry});
    }
  }
  flush();
  return out;
}

export function runTools(run) {
  return run.entries.filter(isStep).map((entry) => entry.row);
}

export function runFailed(run) {
  return runTools(run).filter((row) => row.status === 'failed').length;
}

// `waiting`: the item ids a still-waiting prompt is about.
export function runLive(run, waiting) {
  return runTools(run).some((row) => row.status === 'running' || waiting.has(row.itemID));
}

export function runCounts(run) {
  const counts = {commands: 0, reads: 0, searches: 0, edits: 0, others: 0};
  for (const row of runTools(run)) {
    const kind = stepKind(row);
    if (kind === 'command') counts.commands += 1;
    else if (kind === 'read') counts.reads += 1;
    else if (kind === 'search') counts.searches += 1;
    else if (kind === 'edit') counts.edits += editCount(row);
    else counts.others += 1;
  }
  return counts;
}

// "4 commands, 2 edits" - each kind counted on its own, in a fixed order.
export function runSummary(t, counts) {
  const a = t.agent;
  const part = (n, one, many) => (n > 0 ? (n === 1 ? one : fill(many, n)) : null);
  return [part(counts.commands, a.runCommand, a.runCommands), part(counts.reads, a.runRead, a.runReads),
    part(counts.searches, a.runSearch, a.runSearches), part(counts.edits, a.runEdit, a.runEdits),
    part(counts.others, a.runOther, a.runOthers)].filter(Boolean).join(a.runSep);
}

// Where the drawn tail starts: the newest LIVE_TAIL steps, or earlier when a
// step that waits for the person is further up.
export function tailStart(run, waiting) {
  let steps = 0;
  let start = run.entries.length;
  for (let index = run.entries.length - 1; index >= 0; index -= 1) {
    if (isStep(run.entries[index])) steps += 1;
    if (steps > LIVE_TAIL) break;
    start = index;
  }
  const firstWaiting = run.entries.findIndex((entry) => isStep(entry) && waiting.has(entry.row.itemID));
  return firstWaiting >= 0 ? Math.min(start, firstWaiting) : start;
}

export function stepsBefore(run, index) {
  return run.entries.slice(0, index).filter(isStep).length;
}

// How a run draws. The person's own tap wins (`choice` true/false); a run
// that is working, or was while on screen (`held`), shows its tail; any
// other run is one folded line.
export function runMode(choice, live, held) {
  if (choice === true) return 'all';
  if (choice === false) return 'folded';
  return live || held ? 'tail' : 'folded';
}

// ── find ──────────────────────────────────────────────────────────────────

// Only what is on screen with every output folded is searched - the words,
// the step lines, the files an edit names, an answered line - so every match
// is one you can see. Never a tool's output (Collie `searchableText`).
export function searchable(t, entry) {
  if (entry.kind === 'answered') {
    return [entry.approval.summary || '', ...(entry.approval.answered || [])].join('\n');
  }
  const row = entry.row;
  if (row.kind === 'message') return row.text || '';
  const files = (row.changes || []).map((c) => changeVerb(t, c) + ' ' + changeSubject(c));
  return [stepText(t, row), ...files].join('\n');
}

export function contains(text, query) {
  const needle = String(query || '').trim().toLowerCase();
  return needle !== '' && String(text || '').toLowerCase().includes(needle);
}

// The ids of the entries that match, oldest first. A substring, never a
// pattern: a stray `(` must not throw or quietly match nothing.
export function findMatches(t, entries, query) {
  return entries.filter((entry) => contains(searchable(t, entry), query)).map((entry) => entry.id);
}

// The next stop strictly after (or before) `from`, wrapping at the ends.
// `order` is every entry id in drawn order; `from` null means "past the end"
// (Collie `step`).
export function step(stops, order, from, forward) {
  if (!stops.length) return null;
  const position = new Map(order.map((id, index) => [id, index]));
  const at = from !== null && from !== undefined && position.has(from) ? position.get(from) : (forward ? -1 : order.length);
  const indexed = stops.filter((id) => position.has(id)).map((id) => [id, position.get(id)]);
  if (!indexed.length) return null;
  if (forward) return (indexed.find(([, i]) => i > at) || indexed[0])[0];
  const before = indexed.filter(([, i]) => i < at);
  return (before.length ? before[before.length - 1] : indexed[indexed.length - 1])[0];
}

// `text` split on every case-insensitive occurrence of `query`; a hit piece
// is drawn marked (Collie `splitHighlight`).
export function splitHighlight(text, query) {
  const needle = String(query || '').trim().toLowerCase();
  const value = String(text || '');
  if (!needle || !value) return [{text: value, hit: false}];
  const hay = value.toLowerCase();
  const pieces = [];
  let at = 0;
  for (;;) {
    const found = hay.indexOf(needle, at);
    if (found === -1) break;
    if (found > at) pieces.push({text: value.slice(at, found), hit: false});
    pieces.push({text: value.slice(found, found + needle.length), hit: true});
    at = found + needle.length;
  }
  if (!pieces.length) return [{text: value, hit: false}];
  if (at < value.length) pieces.push({text: value.slice(at), hit: false});
  return pieces;
}
