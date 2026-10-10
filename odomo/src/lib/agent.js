// ODOMO-2: the default agent's conversation, the App's model kept as plain
// functions (omodachi-ios herdr-4 `Agent/AgentChatModel.swift`,
// `AgentChatApprovals.swift`, `AgentChatStore`).
//
// The host is the authority: a snapshot replaces what this page holds, events
// apply only in sequence order for the same identity (a gap is a reconnect,
// never invented output), an approval is answered once and settles only on
// the host's reply or its `resolved` event. The user's own words show in the
// same frame as the tap (`localEcho`) and go when the provider echoes them.

import {actionsOf, changesOf} from './agentSteps.js';

export const PHASES = ['dormant', 'starting', 'ready', 'sending', 'running', 'cancelling', 'reconnecting', 'failed'];
export const LOCAL_ECHO = 'local-echo';

export function initial() {
  return {phase: 'dormant', identity: null, turnID: null, rows: [], localEcho: null, lastSequence: 0,
    error: null, approvals: [], status: {type: null, activeFlags: [], waiting: null}, usage: null,
    draft: '', handoff: {state: 'none'}, notice: null, selectedModel: null, selectedEffort: null, models: null,
    modelsLoading: false, modelsNotice: null, fresh: {state: 'none'}, modeBusy: false, diff: null,
    // AGENT-QUEUE-1: codex's queue (null: this codex has none - steer then),
    // codex's plan checklist, and whether there are rows before the first.
    queue: null, checklist: null, earlier: false, loadingEarlier: false};
}

// ── AGENT-NEW-1: a new conversation ───────────────────────────────────────
// What stays on this page when the conversation changes: the words being
// typed. Everything that belonged to the old conversation goes.
export function startOver(model) {
  return {...initial(), draft: model.draft, models: model.models};
}

// The host answered with a conversation that is not the one on screen: it
// was replaced (here or on another device). Not an error - follow it.
export function otherConversation(model, snapshot) {
  return !!model.identity && !!snapshot && !!snapshot.identity
    && typeof snapshot.identity.providerSessionID === 'string' && snapshot.identity.providerSessionID !== ''
    && !sameIdentity(model.identity, snapshot.identity);
}

// What the next turn carries: a pending choice (marked ↑) wins over what the
// provider last confirmed (AGENT-NEW-1: core now tells the thread's own model
// from the start, and a tap in the picker must show).
export function effectiveModel(model) {
  return model.selectedModel || (model.usage && model.usage.model) || null;
}
export function effectiveEffort(model) {
  return model.selectedEffort || (model.usage && model.usage.effort) || null;
}

// AGENT-NEW-1 (Leo): the transcript follows new output only while the person
// is at its end (within 48 px); scrolled up to read, it stays put.
export function atEnd(scrollTop, clientHeight, scrollHeight, slack = 48) {
  return scrollTop + clientHeight >= scrollHeight - slack;
}

// Why "New conversation" cannot run right now, as a key in t.agent, or null.
export function newRefusal(model) {
  if (!model.identity) return 'newUnavailable';
  if (pendingApprovals(model).length) return 'newWhileWaiting';
  if (['running', 'cancelling', 'sending', 'starting'].includes(model.phase) || model.turnID) return 'newWhileWorking';
  return null;
}

function sameIdentity(a, b) {
  return !!a && !!b && a.hostID === b.hostID && a.agentID === b.agentID && a.provider === b.provider
    && a.providerSessionID === b.providerSessionID;
}

// AGENT-COLLIE-1: a fileChange prompt's `files` / `added` / `removed` are drawn
// as its change summary, not as detail lines.
const INTERNAL = new Set(['item_id', 'turn_id', 'questions', 'approval_id', 'files', 'added', 'removed']);

// One scalar line of `details`, or null for a structure.
export function scalar(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === 'string') return value || null;
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : String(value);
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  if (Array.isArray(value)) {
    const parts = value.map(scalar).filter(Boolean);
    return parts.length ? parts.join(', ') : null;
  }
  if (typeof value === 'object') {
    const parts = Object.keys(value).sort().map((key) => {
      const v = scalar(value[key]);
      return v ? key + ': ' + v : null;
    }).filter(Boolean);
    return parts.length ? parts.join(', ') : null;
  }
  return null;
}

// `agent.approval.requested` / `pendingApprovals[]` -> what the card draws.
export function approvalOf(wire) {
  const details = wire && typeof wire.details === 'object' && wire.details ? wire.details : {};
  const lines = Object.keys(details).sort().filter((key) => !INTERNAL.has(key))
    .map((key) => ({key, label: key.replace(/_/g, ' '), value: scalar(details[key])}))
    .filter((row) => row.value);
  const questions = (Array.isArray(details.questions) ? details.questions : []).slice(0, 8).map((row) => {
    if (!row || typeof row !== 'object' || typeof row.id !== 'string' || !row.id) return null;
    const question = typeof row.question === 'string' ? row.question : typeof row.header === 'string' ? row.header : null;
    if (!question) return null;
    const options = (Array.isArray(row.options) ? row.options : []).filter((o) => typeof o === 'string');
    // AGENT-COLLIE-1: each option with the sentence codex sends beside it
    // (core's `choices`; from an older core, the bare labels), and codex's
    // `isOther` - the person may answer in their own words.
    const choices = (Array.isArray(row.choices) ? row.choices : []).map((c) => (c && typeof c === 'object'
      && typeof c.label === 'string' && c.label ? {label: c.label,
      description: typeof c.description === 'string' && c.description ? c.description : null} : null)).filter(Boolean);
    return {id: row.id, header: typeof row.header === 'string' ? row.header : null, question,
      secret: row.secret === true, options,
      choices: choices.length ? choices : options.map((label) => ({label, description: null})), other: row.other === true};
  }).filter(Boolean);
  // AGENT-FIX-2: the tool row this prompt is about (core's `item_id`).
  const itemID = typeof details.item_id === 'string' && details.item_id ? details.item_id : null;
  // AGENT-COLLIE-1: what a fileChange prompt allows, as core named it.
  const files = (Array.isArray(details.files) ? details.files : []).filter((f) => typeof f === 'string' && f).slice(0, 8);
  const lineCount = (v) => (Number.isInteger(v) && v >= 0 ? v : 0);
  return {requestID: String(wire.request_id), kind: String(wire.kind || ''), summary: wire.summary || '',
    details: lines, questions, decisions: Array.isArray(wire.decisions) ? wire.decisions.filter((d) => typeof d === 'string') : [],
    state: {kind: 'pending'}, itemID, afterRowID: null, answered: null,
    changeFiles: files, changeAdded: files.length ? lineCount(details.added) : 0,
    changeRemoved: files.length ? lineCount(details.removed) : 0};
}

// ── AGENT-FIX-2 (Leo: "通过的 permission 会在底部堆积") ─────────────────────
// A prompt arriving. The provider re-raising an id that still waits is the
// same prompt, replaced in place. The same id on an answered prompt is a new
// one (codex numbers requests per connection): the old line keeps its place
// under a name of its own.
export function receiveApproval(model, incoming) {
  const last = model.rows.length ? model.rows[model.rows.length - 1].id : null;
  const approval = {...incoming, afterRowID: last};
  const index = model.approvals.findIndex((a) => a.requestID === approval.requestID);
  const approvals = model.approvals.slice();
  if (index >= 0 && isPending(approvals[index])) {
    approvals[index] = {...approval, afterRowID: approvals[index].afterRowID || last};
    return {...model, approvals};
  }
  if (index >= 0) {
    approvals[index] = {...approvals[index], requestID: approvals[index].requestID + '~' + Math.random().toString(36).slice(2, 10)};
  }
  approvals.push(approval);
  return {...model, approvals};
}

// The conversation as it is drawn: each row followed by the answered prompts
// that belong to it - under the command it was about, else under the row that
// was last when it came. One whose rows are all gone is not drawn. A prompt
// still waiting is never here: it stays at the end (`waitingApprovals`).
export function timeline(model) {
  const present = new Set(model.rows.map((row) => row.id));
  const placed = new Map();
  for (const approval of model.approvals) {
    if (isPending(approval)) continue;
    const byItem = approval.itemID && present.has('tool:' + approval.itemID) ? 'tool:' + approval.itemID : null;
    const anchor = byItem || (approval.afterRowID && present.has(approval.afterRowID) ? approval.afterRowID : null);
    if (!anchor) continue;
    if (!placed.has(anchor)) placed.set(anchor, []);
    placed.get(anchor).push(approval);
  }
  const entries = [];
  for (const row of model.rows) {
    entries.push({kind: 'row', id: row.id, row});
    for (const approval of placed.get(row.id) || []) entries.push({kind: 'answered', id: 'approval:' + approval.requestID, approval});
  }
  return entries;
}

export function waitingApprovals(model) {
  return model.approvals.filter(isPending);
}

// AGENT-FIX-2: what core keeps of a command's output (`TOOL_DETAIL_LIMIT`),
// counted in code points as Python counts characters.
export const TOOL_DETAIL_LIMIT = 65536;

export function appendOutput(row, delta, truncated) {
  const points = Array.from(row.detail + delta);
  const excess = points.length - TOOL_DETAIL_LIMIT;
  const next = {...row, detail: excess > 0 ? points.slice(excess).join('') : points.join('')};
  if (Number.isInteger(truncated) && truncated > 0) next.detailTruncated = truncated;
  else if (excess > 0) next.detailTruncated = (row.detailTruncated || 0) + excess;
  return next;
}

export function isPending(approval) {
  return approval.state.kind !== 'resolved';
}

export function pendingApprovals(model) {
  return model.approvals.filter(isPending);
}

function rowOf(wire) {
  if (!wire || typeof wire.id !== 'string') return null;
  if (wire.kind === 'message' && (wire.role === 'user' || wire.role === 'assistant')) {
    const row = {kind: 'message', id: 'message:' + wire.id, itemID: wire.id, turnID: wire.turnID, role: wire.role,
      text: typeof wire.text === 'string' ? wire.text : ''};
    // ATTACH-1: how many pictures rode with the user's words.
    if (Number.isInteger(wire.images) && wire.images > 0) row.images = wire.images;
    // AGENT-PLAN-1: the plan codex proposed in Plan mode.
    if (wire.plan === true && wire.role === 'assistant') row.plan = true;
    // AGENT-QUEUE-1: a giant message travels cut; this many characters are not here.
    if (Number.isInteger(wire.text_truncated) && wire.text_truncated > 0) row.textTruncated = wire.text_truncated;
    return row;
  }
  if (wire.kind === 'tool') {
    // AGENT-NEW-1: `interrupted` - a tool whose turn the person stopped.
    const status = ['running', 'succeeded', 'failed', 'cancelled', 'interrupted'].includes(wire.status) ? wire.status : 'unknown';
    const row = {kind: 'tool', id: 'tool:' + wire.id, itemID: wire.id, turnID: wire.turnID, name: wire.name || 'Tool',
      status, detail: typeof wire.detail === 'string' ? wire.detail : ''};
    // LINKS-1: files a fileChange touched or an imageView looked at.
    if (Array.isArray(wire.paths)) {
      const paths = wire.paths.filter((path) => typeof path === 'string' && path).slice(0, 32);
      if (paths.length) row.paths = paths;
    }
    // AGENT-FIX-2: core kept only the newest part of the output.
    if (Number.isInteger(wire.detail_truncated) && wire.detail_truncated > 0) row.detailTruncated = wire.detail_truncated;
    // AGENT-COLLIE-1: a command's words without codex's shell, its reading,
    // exit code and duration; a file change, per file. All read leniently.
    if (typeof wire.command === 'string' && wire.command) row.command = wire.command;
    const actions = actionsOf(wire.actions);
    if (actions) row.actions = actions;
    if (Number.isInteger(wire.exit_code)) row.exitCode = wire.exit_code;
    if (Number.isInteger(wire.duration_ms) && wire.duration_ms >= 0) row.durationMs = wire.duration_ms;
    const changes = changesOf(wire.changes);
    if (changes) row.changes = changes;
    if (Number.isInteger(wire.changes_omitted) && wire.changes_omitted > 0) row.changesOmitted = wire.changes_omitted;
    return row;
  }
  return null;
}

function statusOf(wire) {
  return {type: wire && typeof wire.type === 'string' ? wire.type : null,
    activeFlags: wire && Array.isArray(wire.activeFlags) ? wire.activeFlags : [],
    waiting: wire && typeof wire.waiting === 'string' ? wire.waiting : null};
}

// The wire's usage, flattened for the header (AgentChatUsage).
export function usageOf(wire) {
  if (!wire || typeof wire !== 'object') return null;
  const tokens = wire.tokens || {};
  const limits = wire.rate_limits || {};
  const windows = [limits.primary, limits.secondary].filter((w) => w && typeof w === 'object');
  return {lastTotal: tokens.last && Number.isInteger(tokens.last.totalTokens) ? tokens.last.totalTokens : null,
    contextWindow: Number.isInteger(tokens.modelContextWindow) ? tokens.modelContextWindow : null,
    windows: windows.map((w) => ({usedPercent: typeof w.usedPercent === 'number' ? w.usedPercent : null,
      minutes: Number.isInteger(w.windowDurationMins) ? w.windowDurationMins : null})),
    model: typeof wire.model === 'string' ? wire.model : null, effort: typeof wire.effort === 'string' ? wire.effort : null,
    // AGENT-PLAN-1: the thread's collaboration mode as codex reports it.
    mode: wire.mode === 'plan' || wire.mode === 'default' ? wire.mode : null};
}

// ── AGENT-PLAN-1: Plan mode ───────────────────────────────────────────────
export function planOn(model) {
  return !!(model.usage && model.usage.mode === 'plan');
}

// Why the mode cannot change now, as a key in t.agent, or null. codex's TUI
// refuses /plan while a turn runs; so does the host.
export function modeRefusal(model) {
  if (!model.identity) return 'newUnavailable';
  if (model.modeBusy) return 'planSwitching';
  if (['running', 'cancelling', 'sending', 'starting'].includes(model.phase) || model.turnID) return 'planWhileWorking';
  if (pendingApprovals(model).length) return 'planWhileWorking';
  return null;
}

// The plan the person can act on: the newest plan row, while Plan mode is on
// and nothing has been said after it.
export function actionablePlan(model) {
  if (!planOn(model)) return null;
  for (let index = model.rows.length - 1; index >= 0; index -= 1) {
    const row = model.rows[index];
    if (row.kind === 'message' && row.role === 'user') return null;
    if (row.kind === 'message' && row.plan) return row.id;
  }
  return null;
}

export function contextFraction(usage) {
  if (!usage || !usage.contextWindow || usage.lastTotal === null) return null;
  return Math.min(1, Math.max(0, usage.lastTotal / usage.contextWindow));
}

export function windowLabel(minutes) {
  if (minutes === 300) return '5h';
  if (minutes === 10080) return 'weekly';
  if (minutes === null || minutes === undefined) return 'quota';
  if (minutes >= 1440) return Math.floor(minutes / 1440) + 'd';
  if (minutes >= 60) return Math.floor(minutes / 60) + 'h';
  return minutes + 'm';
}

// ── AGENT-QUEUE-1: the queue, the checklist, earlier messages ─────────────
// codex's own queue as core carries it, or null when this codex has none.
export function queueOf(wire) {
  if (!Array.isArray(wire)) return null;
  return wire.slice(0, 20).map((item) => (item && typeof item === 'object' && typeof item.id === 'string' && item.id
    && typeof item.text === 'string' ? {id: item.id, requestID: typeof item.request_id === 'string' ? item.request_id : '',
      text: item.text, images: Number.isInteger(item.images) && item.images > 0 ? item.images : 0,
      clipped: item.text_clipped === true} : null)).filter(Boolean);
}

const STEP_STATUS = ['pending', 'inProgress', 'completed'];

export function checklistOf(wire) {
  if (!wire || typeof wire !== 'object' || !Array.isArray(wire.steps)) return null;
  const steps = wire.steps.slice(0, 32).map((step) => (step && typeof step.step === 'string' && step.step
    ? {step: step.step, status: STEP_STATUS.includes(step.status) ? step.status : 'pending'} : null)).filter(Boolean);
  if (!steps.length) return null;
  return {turnID: typeof wire.turnID === 'string' ? wire.turnID : null, steps,
    explanation: typeof wire.explanation === 'string' && wire.explanation ? wire.explanation : null};
}

// The pinned checklist shows while the plan is active: its turn still
// running, or a step still open and no later turn since. A plan the model left
// behind (a later turn, or one it never finished) is history, and goes.
export function checklistShown(model) {
  const list = model.checklist;
  if (!list || !list.turnID) return null;
  if (list.turnID === model.turnID) return list;
  const open = list.steps.some((step) => step.status !== 'completed');
  const last = model.rows.length ? model.rows[model.rows.length - 1].turnID : null;
  return open && !model.turnID && last === list.turnID ? list : null;
}

// "2 of 3" and the step being done (else the next to do, else the last).
export function checklistLine(list) {
  const done = list.steps.filter((step) => step.status === 'completed').length;
  const current = list.steps.find((step) => step.status === 'inProgress')
    || list.steps.find((step) => step.status === 'pending') || list.steps[list.steps.length - 1];
  return {done, total: list.steps.length, current: current.step};
}

// While a turn runs, Send puts the words in codex's queue - when this codex
// has one. Otherwise it steers, as before.
export function canQueue(model) {
  return (model.phase === 'running' || model.phase === 'cancelling') && !!model.identity && model.queue !== null
    && model.draft.trim() !== '';
}

// A snapshot replaces the rows; older rows this page already loaded stay in
// front of it while the snapshot still starts where they end.
function keepEarlier(model, rows, earlier) {
  if (!earlier || !rows.length || !model.rows.length) return {rows, earlier};
  const first = model.rows.findIndex((row) => row.id === rows[0].id);
  if (first <= 0) return {rows, earlier};
  const fresh = new Set(rows.map((row) => row.id));
  return {rows: model.rows.slice(0, first).filter((row) => !fresh.has(row.id)).concat(rows), earlier: model.earlier};
}

// A page of earlier rows (`GET …/chat/history`), in front of what is here.
export function prependEarlier(model, page) {
  const have = new Set(model.rows.map((row) => row.id));
  const rows = (page && Array.isArray(page.rows) ? page.rows : []).map(rowOf).filter((row) => row && !have.has(row.id));
  return {...model, rows: rows.concat(model.rows), earlier: !!(page && page.earlier), loadingEarlier: false};
}

// ── transitions ───────────────────────────────────────────────────────────

// Entering the panel is the ensure (A-63). Repeated taps share one.
export function beginEnsure(model) {
  if (!(model.phase === 'dormant' || model.phase === 'failed' || (model.phase === 'reconnecting' && !model.identity))) return null;
  return {...model, phase: 'starting', error: null};
}

// A snapshot is the host's whole answer.
export function restore(model, snapshot) {
  if (!snapshot || !snapshot.identity) return null;
  const identity = snapshot.identity;
  if (identity.agentID !== 'default' || !identity.providerSessionID || !Number.isInteger(snapshot.sequence)
      || snapshot.sequence < 0) return null;
  if (model.identity && !sameIdentity(model.identity, identity)) return null;
  if (model.identity && snapshot.sequence < model.lastSequence) return null;
  const {rows, earlier} = keepEarlier(model, (snapshot.rows || []).map(rowOf).filter(Boolean), snapshot.earlier === true);
  // A prompt this page already placed keeps its place; a new one goes after
  // the row that is last now.
  const lastRow = rows.length ? rows[rows.length - 1].id : null;
  const pending = (snapshot.pendingApprovals || []).map(approvalOf).map((a) => {
    const known = model.approvals.find((b) => b.requestID === a.requestID);
    return {...a, afterRowID: (known && known.afterRowID) || lastRow};
  });
  const waiting = new Set(pending.map((a) => a.requestID));
  // Answered while this page was away: kept as result rows, not deleted.
  const kept = model.approvals.filter((a) => !waiting.has(a.requestID))
    .map((a) => (isPending(a) ? {...a, state: {kind: 'resolved', decision: null, source: 'elsewhere'}} : a));
  return {...model, identity, rows, localEcho: null, turnID: snapshot.activeTurn || null,
    lastSequence: snapshot.sequence, phase: snapshot.activeTurn ? 'running' : 'ready', error: null,
    approvals: pending.concat(kept), status: statusOf(snapshot.status), usage: usageOf(snapshot.usage),
    // AGENT-QUEUE-1. An older core says none of these.
    queue: queueOf(snapshot.queue), checklist: checklistOf(snapshot.checklist),
    earlier: earlier && rows.length > 0, loadingEarlier: false};
}

function upsert(rows, row) {
  const index = rows.findIndex((r) => r.id === row.id);
  if (index < 0) return rows.concat(row);
  const next = rows.slice();
  next[index] = row;
  return next;
}

// One event off the stream. Returns the next model, or the same one when the
// event does not apply; a gap returns a model in `reconnecting`.
export function apply(model, envelope) {
  if (!envelope || !sameIdentity(model.identity, envelope.identity)) return model;
  const sequence = envelope.sequence;
  if (!Number.isInteger(sequence) || sequence <= model.lastSequence) return model;
  if (model.phase === 'reconnecting' || sequence !== model.lastSequence + 1) return {...model, phase: 'reconnecting'};
  const event = envelope.event || {};
  let next = model;
  switch (event.type) {
    case 'turnStarted':
      next = {...model, turnID: event.turnID || null, phase: 'running'};
      break;
    case 'message': {
      const row = rowOf(event.value);
      if (!row || row.turnID !== model.turnID) return model;
      // ATTACH-1: words that carried attachments come back with core's paths
      // in them, so any user row of the turn is their echo.
      const echo = row.role === 'user' && model.localEcho && (row.text === model.localEcho.text
        || model.localEcho.attached) ? null : model.localEcho;
      next = {...model, rows: upsert(model.rows, row), localEcho: echo};
      break;
    }
    case 'tool': {
      const row = rowOf(event.value);
      if (!row || row.turnID !== model.turnID) return model;
      next = {...model, rows: upsert(model.rows, row)};
      break;
    }
    case 'tool.output': {
      // AGENT-FIX-2: a running command's new output, as an increment. Output
      // of a row this page does not hold changes nothing (its completion
      // brings it) and still advances the sequence.
      const value = event.value || {};
      const index = model.rows.findIndex((r) => r.id === 'tool:' + value.id);
      if (index >= 0 && value.turnID === model.turnID && typeof value.delta === 'string') {
        const rows = model.rows.slice();
        rows[index] = appendOutput(rows[index], value.delta, value.detail_truncated);
        next = {...model, rows};
      }
      break;
    }
    case 'turnFinished':
      if (event.turnID !== model.turnID) return model;
      next = {...model, turnID: null, phase: 'ready', localEcho: null};
      break;
    case 'turnFailed':
      if (event.turnID !== model.turnID) return model;
      next = {...model, turnID: null, phase: 'failed', error: {code: 'turn_failed'}, localEcho: null};
      break;
    case 'agent.approval.requested': {
      if (!event.request_id || !event.kind) return model;
      next = receiveApproval(model, approvalOf(event));
      break;
    }
    case 'agent.approval.resolved': {
      const id = event.request_id === undefined ? null : String(event.request_id);
      const source = event.source || 'elsewhere';
      next = {...model, approvals: model.approvals.map((a) => (a.requestID === id
        ? {...a, state: {kind: 'resolved', decision: event.decision || null, source},
          answered: source === 'client' ? a.answered || null : null} : a))};
      break;
    }
    case 'agent.status.changed':
      next = {...model, status: statusOf(event.status)};
      break;
    case 'agent.usage.updated': {
      const usage = usageOf(event.usage);
      // The provider confirming what the turn used: a pending choice that
      // landed is no longer pending.
      next = {...model, usage,
        selectedModel: usage && usage.model && usage.model === model.selectedModel ? null : model.selectedModel,
        selectedEffort: usage && usage.effort && usage.effort === model.selectedEffort ? null : model.selectedEffort};
      break;
    }
    case 'agent.queue.changed':
      // AGENT-QUEUE-1: what codex's queue holds now (any client changed it).
      next = {...model, queue: queueOf(event.queue)};
      break;
    case 'agent.checklist.updated':
      next = {...model, checklist: checklistOf(event.checklist)};
      break;
    case 'connectionLost':
      return {...model, lastSequence: sequence, phase: 'reconnecting'};
    default:
      // A receipt with no surface still advances the sequence.
      break;
  }
  return {...next, lastSequence: sequence};
}

export function connectionLost(model) {
  return {...model, phase: 'reconnecting'};
}

export function fail(model, error) {
  return {...model, phase: 'failed', error};
}

export function canSend(model, attached = false) {
  return model.phase === 'ready' && !!model.identity && (model.draft.trim() !== '' || !!attached);
}

export function canCancel(model) {
  return model.phase === 'running' && !!model.turnID;
}

export function canSteer(model) {
  return (model.phase === 'running' || model.phase === 'cancelling') && !!model.turnID && !!model.identity
    && model.draft.trim() !== '';
}

// The words go on screen in the frame of the tap, the composer empties.
// `attached`: how many chips ride with the words (ATTACH-1), and how many of
// them are pictures - the echo shows the count the host will echo back.
export function beginSend(model, attached = null) {
  const count = attached ? attached.count || 0 : 0;
  if (!canSend(model, count > 0)) return null;
  const text = model.draft;
  const echo = {kind: 'message', id: 'message:' + LOCAL_ECHO, itemID: LOCAL_ECHO, turnID: '', role: 'user', text};
  if (count) echo.attached = true;
  if (attached && attached.images) echo.images = attached.images;
  return {model: {...model, phase: 'sending', error: null, draft: '', localEcho: echo}, text};
}

// The send did not land: the words go back into the composer.
export function sendFailed(model, text, error) {
  return {...model, localEcho: null, draft: model.draft === '' ? text : model.draft, phase: 'failed', error};
}

export function sendAcknowledged(model) {
  // The ack only confirms the request; the turn's own events move the phase.
  return model.phase === 'sending' ? {...model, phase: model.turnID ? 'running' : 'sending'} : model;
}

// AGENT-PLAN-1 (Leo: no slash list - what is typed goes through). What the
// host did with the words. Four basics it handles itself: `/plan`, `/compact`
// and `/diff` start no turn of their own (`delivery: "command"`), so nothing
// waits for one and the words do not stay on screen as if sent; `/diff`
// brings the conversation's file changes. `/plan <words>` and `/review` are
// turns, but codex records other words than the ones typed, so any user row
// of the turn is the echo.
export function sendAnswered(model, reply) {
  if (reply && reply.delivery === 'command') {
    const next = {...model, localEcho: null,
      phase: model.phase === 'sending' ? (model.turnID ? 'running' : 'ready') : model.phase};
    if (reply.command === 'diff') {
      return {...next, diff: {changes: changesOf(reply.changes) || [],
        omitted: Number.isInteger(reply.changes_omitted) && reply.changes_omitted > 0 ? reply.changes_omitted : 0}};
    }
    return next;
  }
  const next = sendAcknowledged(model);
  return reply && reply.command && next.localEcho ? {...next, localEcho: {...next.localEcho, attached: true}} : next;
}

// D-15: a turn is in flight and nothing has come back yet.
export function awaitingFirstToken(model) {
  if (model.phase === 'sending') return true;
  if (model.phase !== 'running') return false;
  if (!model.turnID) return true;
  return !model.rows.some((row) => (row.kind === 'message' ? row.role === 'assistant' && row.turnID === model.turnID
    && row.text !== '' : row.turnID === model.turnID));
}

// The cursor belongs to the running turn's last assistant row only.
export function streaming(model, row) {
  return row.kind === 'message' && row.role === 'assistant' && model.phase === 'running'
    && row.turnID === model.turnID && model.rows.length > 0 && model.rows[model.rows.length - 1].id === row.id;
}

export function beginDecision(model, requestID, decision) {
  const index = model.approvals.findIndex((a) => a.requestID === requestID);
  if (index < 0 || model.approvals[index].state.kind !== 'pending') return null;
  const approvals = model.approvals.slice();
  approvals[index] = {...approvals[index], state: {kind: 'deciding', decision}};
  return {...model, approvals};
}

// AGENT-COLLIE-1: a `userInput` answer going out. The card locks like a
// decision, and the answered line will say what was chosen - every answer
// except a secret one, which is never kept.
export function beginAnswer(model, requestID, input) {
  const next = beginDecision(model, requestID, 'input');
  if (!next) return null;
  return {...next, approvals: next.approvals.map((a) => {
    if (a.requestID !== requestID) return a;
    const shown = a.questions.filter((q) => !q.secret && input[q.id]).map((q) => input[q.id].join(', ')).filter(Boolean);
    return {...a, answered: shown.length ? shown : null};
  })};
}

export function settleDecision(model, requestID, decision, source) {
  return {...model, approvals: model.approvals.map((a) => (a.requestID === requestID
    ? {...a, state: {kind: 'resolved', decision: decision || null, source},
      answered: source === 'client' ? a.answered || null : null} : a))};
}

export function failDecision(model, requestID) {
  return {...model, approvals: model.approvals.map((a) => (a.requestID === requestID && a.state.kind === 'deciding'
    ? {...a, state: {kind: 'pending'}, answered: null} : a))};
}

// Only the questions that were answered are sent; an empty answer is not a choice.
export function submission(answers) {
  const out = {};
  for (const [id, value] of Object.entries(answers || {})) {
    if (typeof value === 'string' && value.trim()) out[id] = [value];
  }
  return out;
}

export const MODEL_LABEL = {accept: 'approvalAccept', acceptForSession: 'approvalAcceptSession',
  decline: 'approvalDecline', cancel: 'cancel'};

// ── handoff (AgentChatHandoff.swift) ──────────────────────────────────────
export function planUsable(plan) {
  return !!plan && plan.status === 'prepared' && plan.requires_confirmation === true && plan.agent_id === 'default'
    && typeof plan.provider_session_id === 'string' && plan.provider_session_id !== '';
}

export function handoffConfirmed(plan, result) {
  return !!result && result.status === 'completed' && result.same_thread === true && result.herdr_pane_registered === true
    && result.provider_session_id === plan.provider_session_id && result.pane_id === plan.pane_id;
}

// The host's chat codes this page acts on; anything else is one state.
export const KNOWN_CODES = new Set(['existing_thread_handoff_required', 'agent_busy', 'agent_pane_busy',
  'handoff_rolled_back', 'handoff_plan_expired', 'handoff_target_changed', 'handoff_stop_unconfirmed',
  'handoff_attach_unconfirmed', 'handoff_thread_unconfirmed', 'agent_owner_start_failed',
  'agent_owner_start_timeout', 'agent_owner_socket_unavailable', 'thread_creation_unconfirmed',
  'provider_request_rejected', 'structured_agent_kind_unsupported', 'agent_state_unknown', 'agent_not_working',
  'agent_approval_unknown', 'agent_approval_decision_invalid', 'default_agent_unset', 'agent_chat_unavailable',
  'agent_management_unavailable', 'cancellation_pending', 'history_unavailable',
  // AGENT-NEW-1
  'agent_turn_active', 'agent_approval_pending', 'agent_identity_changed', 'agent_conversation_changing',
  'new_conversation_confirmation_required', 'agent_thread_creation_unconfirmed',
  // AGENT-PLAN-1
  'agent_mode_unavailable', 'invalid_agent_mode', 'agent_command_while_working',
  // AGENT-QUEUE-1
  'agent_queue_unavailable', 'agent_queue_item_gone', 'agent_history_row_unknown']);

export function safeCode(code) {
  return KNOWN_CODES.has(code) ? code : 'agent_chat_unavailable';
}

// ── pushes ────────────────────────────────────────────────────────────────
// `?open=agent&ref=approval|input|done`: what the panel says on top until it
// has the host's own answer.
export function pushRef(ids) {
  const ref = ids && ids.ref;
  return ['approval', 'input', 'done'].includes(ref) ? ref : null;
}
