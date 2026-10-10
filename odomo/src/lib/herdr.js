// ODOMO-2: what Odomo's Herdr panel knows, without a screen.
//
// Everything here is the App's own behaviour, read from omodachi-ios on
// herdr-4 and kept beside the file it came from:
//
//   Herdr/HerdrKeys.swift          the keys row, the key names Herdr takes
//                                  (`pane.send_keys`), and how typed input is
//                                  turned into keys or text
//   Herdr/HerdrControls.swift      the "More" menu and what each item asks for
//   Herdr/HerdrOperatorRows.swift  the Quick dock and key presets, and what
//                                  ships when core has no rows to give
//   Herdr/HerdrWire.swift          pane names (never Herdr's `w1:p2`), frames
//   Herdr/HerdrStore.swift         the composer's "Sent ✓ / Sent / Not
//                                  submitted", the second tap, Type's idle stop
//
// Nothing here takes the pane: the screen is `observe` (read-only, at the
// pane's own size), and every write is one `/keys` or `/text` call.

// ── the keys row (HerdrKeyCap.row) ────────────────────────────────────────
// `face` is the cap, `key` is Herdr's spelling, `name` picks the spoken label.
export const KEY_ROW = [
  {id: 'esc', face: 'esc', key: 'Escape', name: 'escape'},
  {id: 'tab', face: '⇥', key: 'Tab', name: 'tab'},
  {id: 'shift-tab', face: '⇧⇥', key: 'shift+Tab', name: 'shiftTab'},
  {id: 'enter', face: '↩', key: 'Enter', name: 'enter'},
  {id: 'space', face: '␣', key: 'Space', name: 'space'},
  {id: 'up', face: '↑', key: 'Up', name: 'up', repeats: true},
  {id: 'down', face: '↓', key: 'Down', name: 'down', repeats: true},
  {id: 'left', face: '←', key: 'Left', name: 'left', repeats: true},
  {id: 'right', face: '→', key: 'Right', name: 'right', repeats: true},
  // HERDR-9: what is on the pane's input line goes without Type.
  {id: 'backspace', face: '⌫', key: 'Backspace', name: 'backspace', repeats: true},
  {id: 'ctrl-c', face: '^C', key: 'ctrl+c', name: 'ctrlC'},
  {id: 'ctrl-d', face: '^D', key: 'ctrl+d', name: 'ctrlD'},
  ...[1, 2, 3, 4, 5].map((n) => ({id: 'digit-' + n, face: String(n), key: String(n), name: 'digit'})),
];
// On a phone the row splits after the arrows (HerdrKeyCap.firstRow).
export const FIRST_ROW = 9;

// core's caps (`herdr_bridge.py`): keys per call, bytes of text.
export const MAX_KEYS = 32;
export const MAX_TEXT_BYTES = 16 * 1024;
// A run of plain characters at most this long goes as single keys.
export const MAX_KEY_RUN = 8;
// Type turns itself off after this long without a key.
export const TYPING_IDLE_MS = 300 * 1000;
// A `danger` / `confirm` row: the second tap within this fires it.
export const ARM_MS = 3000;

// ── identifiers, as core's bridge spells them ─────────────────────────────
export const PANE = /^w[0-9]{1,9}:p[0-9]{1,9}$/;
export const TAB = /^w[0-9]{1,9}:t[0-9]{1,9}$/;
export const WORKSPACE = /^w[0-9]{1,9}$/;
export const SESSION = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
// HERDR-5 NEW_SESSION_NAME: what a person types for a new one.
export const NEW_SESSION = /^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$/;
const AGENT = /^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$/;

// ── the layout (HerdrWire.swift) ──────────────────────────────────────────
export function panesOf(layout) {
  const out = [];
  for (const workspace of (layout && layout.workspaces) || []) {
    for (const tab of workspace.tabs || []) for (const pane of tab.panes || []) out.push(pane);
  }
  return out;
}

export function paneOf(layout, id) {
  return panesOf(layout).find((pane) => pane.id === id) || null;
}

export function tabOf(layout, paneId) {
  for (const workspace of (layout && layout.workspaces) || []) {
    for (const tab of workspace.tabs || []) if ((tab.panes || []).some((pane) => pane.id === paneId)) return tab;
  }
  return null;
}

export function workspaceOf(layout, paneId) {
  for (const workspace of (layout && layout.workspaces) || []) {
    if ((workspace.tabs || []).some((tab) => (tab.panes || []).some((pane) => pane.id === paneId))) return workspace;
  }
  return null;
}

function trimmed(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

// The last folder of the working directory: `~/work/omodachi` -> `omodachi`.
export function cwdTail(pane) {
  const cwd = trimmed(pane && pane.cwd);
  if (!cwd) return null;
  const parts = cwd.replace(/\/+$/, '').split('/');
  const tail = parts[parts.length - 1];
  return tail || cwd;
}

export function agentName(pane) {
  const agent = pane && pane.agent;
  if (!agent) return null;
  return trimmed(agent.kind) || trimmed(agent.name);
}

// HERDR-4 §1.3: the agent's kind and Herdr's own status word, never rewritten.
export function agentLabel(pane) {
  const agent = pane && pane.agent;
  if (!agent) return null;
  return [agent.kind || agent.name, agent.status || pane.agent_status].filter(Boolean).join(' · ') || null;
}

// HERDR-4 §3.3: blocked is said in words, not by colour only.
// HERDR-7: the status a pane's still marker is coloured by, or null for a
// pane with no agent (it keeps the dot). Herdr's word where it is one of the
// four the App colours; anything else reads as idle (muted).
export function paneStatus(pane) {
  if (!pane || !pane.agent) return null;
  const status = pane.agent.status || pane.agent_status;
  return ['working', 'blocked', 'done'].includes(status) ? status : 'idle';
}

export function isWaiting(pane) {
  return !!pane && (pane.agent_status === 'blocked' || (!!pane.agent && pane.agent.status === 'blocked'));
}

// The pane as a person names it: its title, else its folder, else the agent,
// else "Pane 2" by its place in its tab. Never `w1:p2`.
export function paneName(layout, id, numbered) {
  const pane = paneOf(layout, id);
  if (!pane) return numbered('1');
  const title = trimmed(pane.title) || cwdTail(pane);
  if (title) return title;
  const agent = agentName(pane);
  if (agent) return agent;
  const tab = tabOf(layout, id);
  const index = tab ? tab.panes.findIndex((row) => row.id === id) : 0;
  return numbered(String(Math.max(0, index) + 1));
}

export function paneSubtitle(pane, name) {
  const label = agentLabel(pane);
  if (label) return label;
  const tail = cwdTail(pane);
  return tail && tail !== name ? tail : null;
}

// The pane a freshly opened panel shows: the one Herdr says is focused, else
// the first. Never a guess.
export function preferredPane(layout) {
  const focused = layout && layout.focused && layout.focused.pane_id;
  if (focused && paneOf(layout, focused)) return focused;
  const panes = panesOf(layout);
  const row = panes.find((pane) => pane.focused) || panes[0];
  return row ? row.id : null;
}

// A workspace tapped here is chosen here (HERDR-3): its active tab's focused
// pane, else its first. The computer stays put.
export function workspacePane(layout, id) {
  const workspace = ((layout && layout.workspaces) || []).find((row) => row.id === id);
  if (!workspace) return null;
  const tabs = workspace.tabs || [];
  const active = tabs.find((tab) => tab.id === workspace.active_tab_id) || tabs[0];
  const pane = (active && ((active.panes || []).find((row) => row.focused) || (active.panes || [])[0]))
    || tabs.flatMap((tab) => tab.panes || [])[0];
  return pane ? pane.id : null;
}

// The pane's own size on the computer. Watching asks for exactly this, so a
// wide desktop pane is never cut to the phone's width.
export function paneGeometry(pane) {
  const size = pane && pane.size;
  if (!size || !Number.isInteger(size.cols) || !Number.isInteger(size.rows) || size.cols <= 0 || size.rows <= 0) return null;
  return {cols: clamp(size.cols, 1, 1000), rows: clamp(size.rows, 1, 1000)};
}

export function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

// What the operator rows are read for: Herdr's agent name, or null (a shell).
export function rowsAgent(pane) {
  const agent = pane && pane.agent;
  if (!agent) return null;
  const raw = String(agent.name || agent.kind || '').trim().toLowerCase();
  return raw.length <= 40 && AGENT.test(raw) ? raw : null;
}

// ── sessions (HerdrPanelView.swift HerdrSessionSwitcher) ──────────────────
export function sessionDetail(sessions, selected, state, t) {
  if (state.starting) return fillOne(t.herdr.sessionStarting, state.starting);
  if (state.switching) return t.herdr.sessionSwitching;
  const current = (sessions || []).find((row) => row.name === selected);
  if (!current) return '';
  if (!current.running) return t.herdr.sessionStopped;
  if (!current.readable) return t.herdr.sessionUnreadable;
  if (current.readable && (current.panes || 0) === 0) return t.herdr.sessionEmpty;
  const shape = t.herdr.sessionShape.replace('%1', String(current.workspaces || 0))
    .replace('%2', String(current.panes || 0)).replace('%3', String(current.agents || 0));
  return current.owned ? t.herdr.sessionOwned + ' · ' + shape : shape;
}

function fillOne(template, value) {
  return template.replace('%s', String(value));
}

// HERDR-5: the session on screen, when the host says it is stopped.
export function stoppedSession(sessions, selected) {
  const row = (sessions || []).find((item) => item.name === selected);
  return row && !row.running ? row.name : null;
}

// The new-session card's own check, before core is asked: the reason code.
export function newSessionProblem(name, sessions) {
  const value = String(name || '').trim();
  if (!NEW_SESSION.test(value)) return 'invalid_session_name';
  if ((sessions || []).some((row) => row.name === value)) return 'herdr_session_exists';
  return null;
}

// ── HERDR-6: a new workspace in a folder ─────────────────────────────────
// core checks the folder (`host_paths.py`); these are only the card's own
// early words for what core would refuse anyway.
export const MAX_FOLDER_CHARS = 4096;
export const MAX_WORKSPACE_NAME = 64;
const CONTROL = /[\u0000-\u001f\u007f]/;

export function folderProblem(path) {
  const value = String(path || '').trim();
  if (!value || value.length > MAX_FOLDER_CHARS || CONTROL.test(value)) return 'invalid_folder';
  return null;
}

export function workspaceNameProblem(name) {
  const value = String(name || '').trim();
  if (!value) return null;
  if ([...value].length > MAX_WORKSPACE_NAME || CONTROL.test(value)) return 'invalid_workspace_name';
  return null;
}

// The rows a folders reply offers, each `{path, display, name}`, tolerating
// a core that sent nothing (an older one answers 404 and the card keeps its
// path field).
export function folderRows(value) {
  const rows = (list) => (Array.isArray(list) ? list.filter((row) => row && typeof row.path === 'string'
    && typeof row.display === 'string') : []);
  return {recent: rows(value && value.recent), common: rows(value && value.common)};
}

// ── the "More" menu (HerdrControls.swift) ─────────────────────────────────
// HERDR-6: "New workspace…" sits with the other "new" item; it needs no pane.
// HERDR-13 took out split right, split down and zoom: the mirror shows one
// pane at a time, so a split read as a new tab and zoom changed nothing here.
// Core keeps both routes; only the menu dropped them.
export const MENU = ['newWorkspace', 'newTab', 'closeTab', 'close'];
export const CONFIRMS = new Set(['close', 'closeTab']);

// What one item asks for against the layout on screen, or why it cannot.
export function controlRequest(action, layout, selected) {
  // A new workspace is the session's, not a pane's: it works on an empty one.
  if (action === 'newWorkspace') return {kind: 'newWorkspace'};
  if (!layout || !selected || !paneOf(layout, selected)) return {kind: 'unsupported', reason: 'noSelectedPane'};
  switch (action) {
    case 'previousPane':
    case 'nextPane': {
      const all = panesOf(layout).map((pane) => pane.id);
      const index = all.indexOf(selected);
      if (all.length < 2 || index < 0) return {kind: 'unsupported', reason: 'onlyOnePane'};
      const step = action === 'nextPane' ? 1 : all.length - 1;
      return {kind: 'select', pane: all[(index + step) % all.length]};
    }
    case 'close': return {kind: 'pane', pane: selected, action: 'close', body: {}};
    case 'newTab': {
      const workspace = workspaceOf(layout, selected);
      return workspace ? {kind: 'newTab', workspace: workspace.id} : {kind: 'unsupported', reason: 'noSelectedPane'};
    }
    case 'closeTab': {
      const workspace = workspaceOf(layout, selected);
      const tab = tabOf(layout, selected);
      if (!workspace || !tab) return {kind: 'unsupported', reason: 'noSelectedPane'};
      // Closing the last tab would close the workspace with it.
      if ((workspace.tabs || []).length < 2) return {kind: 'unsupported', reason: 'onlyOneTab'};
      return {kind: 'closeTab', workspace: workspace.id, tab: tab.id};
    }
    default: return {kind: 'unsupported', reason: 'noSelectedPane'};
  }
}

// ── operator rows (HerdrOperatorRows.swift) ───────────────────────────────
const FAMILIES = ['claude', 'codex', 'opencode', 'omp', 'grok', 'agy', 'antigravity'];

export function family(name) {
  for (const value of FAMILIES) if (name.startsWith(value)) return value;
  if (name === 'pi' || name.startsWith('pi-') || name.startsWith('pi.')) return 'pi';
  return name;
}

// What ships, for a core without the route: the same rows core's defaults give.
export function shippedRows(agent) {
  const name = agent ? agent.toLowerCase() : null;
  const fam = name ? family(name) : 'shell';
  const quick = name === null
    ? [{id: 'confirm', title: null, items: ['y', 'n'], operator: false}]
    : [{id: 'confirm', title: null, items: ['yes', 'no'], operator: false},
      {id: 'common', title: null, items: ['continue', 'commit and push', 'retry', 'skip'], operator: false}];
  const keys = [['c', false], ['d', true], ['u', false], ['r', false], ['l', false], ['z', true]]
    .map(([letter, danger]) => ({label: 'Ctrl ' + letter.toUpperCase(), keys: ['ctrl+' + letter], danger, operator: false}));
  const names = fam === 'claude' ? ['model', 'effort', 'compact', 'resume'] : fam === 'codex' ? ['model', 'compact', 'resume'] : [];
  const harness = names.map((id) => ({id, label: null, command: '/' + id, confirm: false, operator: false}));
  return {agent: name, family: fam, quick, keys, harness, commands: [], sources: []};
}

// The presets beside the keys row: core's rows, less any that send exactly
// what a fixed key already does (^C, ^D). An operator's rows are kept whole.
export function presets(rows) {
  const fixed = new Set(KEY_ROW.map((cap) => cap.key.toLowerCase()));
  return ((rows && rows.keys) || []).filter((row) => row.operator || row.keys.length !== 1
    || !fixed.has(String(row.keys[0]).toLowerCase()));
}

// What ships reads like the keys row's own caps (`^U`); an operator's row
// keeps the words it was given.
export function presetFace(row) {
  if (row.operator || row.keys.length !== 1) return row.label;
  const key = String(row.keys[0]).toLowerCase();
  if (!key.startsWith('ctrl+') || key.length !== 6) return row.label;
  return '^' + key.slice(5).toUpperCase();
}

export function harnessTitle(row, t) {
  if (row.label) return row.label;
  return ({model: t.herdr.harnessModel, effort: t.herdr.harnessEffort, compact: t.herdr.harnessCompact,
    resume: t.herdr.harnessResume})[row.id] || row.command;
}

export function quickTitle(group, t) {
  if (group.title) return group.title;
  return ({confirm: t.herdr.quickConfirm, common: t.herdr.quickCommon})[group.id] || '';
}

export function operatorWarning(rows, t) {
  for (const source of (rows && rows.sources) || []) {
    if (source.warning) {
      return (source.warning.code === 'operator_file_unparsed' ? t.herdr.operatorUnparsed : t.herdr.operatorDropped)
        .replace('%s', source.file);
    }
  }
  return null;
}

// ── a second tap (HerdrArmed) ─────────────────────────────────────────────
// `armed` is {id, until}. The first tap on a row that asks for two arms it and
// sends nothing; a tap on the armed row within the window sends.
export function secondTap(armed, id, asks, now, window = ARM_MS) {
  if (!asks) return {send: true, armed};
  if (armed && armed.id === id && armed.until > now) return {send: true, armed: null};
  return {send: false, armed: {id, until: now + window}};
}

// ── what the composer says after a send (HerdrSendState) ──────────────────
// `/text` answers {delivered, submitted, reason, proof}.
export function sendStateOf(result) {
  if (!result) return {kind: 'failed', reason: 'herdr_unavailable'};
  if (result.submitted === true) return {kind: result.proof === 'line' ? 'sentLight' : 'sent'};
  return {kind: 'notSubmitted', reason: result.reason || 'not_seen'};
}

// How long a "Sent" line stays before it clears itself.
export function sentLinger(state) {
  return state.kind === 'sentLight' ? 6000 : state.kind === 'sent' ? 4000 : 0;
}

// ── Type into terminal (HerdrKeyEncoder) ──────────────────────────────────
// A letter, digit, punctuation or symbol: what Herdr types as itself.
export function keyable(char) {
  if (typeof char !== 'string' || [...char].length !== 1) return false;
  return /^[\p{L}\p{N}\p{P}\p{S}]$/u.test(char);
}

// A run of typed characters: single keys when it is short and plain, one
// `/text` (not submitted) when it is a paste, a dictated word, or IME output.
export function runWrites(text) {
  const chars = [...String(text || '')];
  if (!chars.length) return [];
  const plain = chars.every((c) => c === ' ' || keyable(c));
  if (plain && chars.length <= MAX_KEY_RUN) return [{keys: chars.map((c) => (c === ' ' ? 'Space' : c))}];
  return [{text: chars.join(''), submit: false}];
}

// Several writes in order, with neighbouring key writes merged into calls of
// at most MAX_KEYS - order is kept, so a key typed after a paste cannot
// overtake it.
export function mergeWrites(writes) {
  const out = [];
  for (const write of writes) {
    const last = out[out.length - 1];
    if (write.keys && last && last.keys && last.keys.length + write.keys.length <= MAX_KEYS) {
      last.keys = last.keys.concat(write.keys);
    } else if (write.keys && write.keys.length > MAX_KEYS) {
      for (let i = 0; i < write.keys.length; i += MAX_KEYS) out.push({keys: write.keys.slice(i, i + MAX_KEYS)});
    } else {
      out.push(write.keys ? {keys: write.keys.slice()} : {...write});
    }
  }
  return out;
}

const NAMED = {
  Escape: 'Escape', Esc: 'Escape', Tab: 'Tab', Enter: 'Enter', Backspace: 'Backspace',
  ArrowUp: 'Up', ArrowDown: 'Down', ArrowLeft: 'Left', ArrowRight: 'Right', Up: 'Up', Down: 'Down',
  Left: 'Left', Right: 'Right',
};
// Herdr 0.8.2 has no name for these; they are dropped and the panel says so.
export const UNSENDABLE = new Set(['Home', 'End', 'PageUp', 'PageDown', 'Insert', 'Delete']);
const IGNORED = new Set(['Shift', 'Control', 'Alt', 'Meta', 'CapsLock', 'NumLock', 'ScrollLock', 'Fn', 'FnLock',
  'Hyper', 'Super', 'OS', 'Dead', 'Process', 'Unidentified', 'AltGraph', 'ContextMenu', 'Compose']);

// One keydown, read into Herdr's key name: {key} to send, {drop: true} for a
// key Herdr cannot send, {text: true} to leave a plain character to the
// `beforeinput` that follows (it is what carries IME and the phone keyboards),
// or null for a key that is nothing on its own (Shift, a dead key).
export function keyOf(event) {
  const key = event && event.key;
  if (!key || event.isComposing || IGNORED.has(key)) return null;
  if (event.metaKey) return null; // the browser's and the system's own
  const mods = [];
  if (event.ctrlKey) mods.push('ctrl');
  if (event.altKey) mods.push('alt');
  if (UNSENDABLE.has(key)) return {drop: true};
  let base = null;
  if (/^F([1-9]|1[0-2])$/.test(key)) base = key;
  else if (key === ' ' ) base = 'Space';
  else if (NAMED[key]) base = NAMED[key];
  if (base) {
    if (event.shiftKey && (base === 'Tab' || /^(Up|Down|Left|Right|F\d+)$/.test(base))) mods.push('shift');
    return {key: mods.concat(base).join('+')};
  }
  if ([...key].length === 1) {
    if (mods.length) {
      // ctrl+c, alt+x: the letter itself, lower case, as Herdr spells it
      // (an alt that produced another character on a Mac keyboard is read
      // from the physical key, which is what a terminal's Meta means).
      let char = key;
      if (event.altKey && event.code && /^Key[A-Z]$/.test(event.code)) char = event.code.slice(3);
      if (event.altKey && event.code && /^Digit[0-9]$/.test(event.code)) char = event.code.slice(5);
      if (/^[A-Za-z]$/.test(char)) char = char.toLowerCase();
      if (!keyable(char)) return {drop: true};
      return {key: mods.concat(char).join('+')};
    }
    return {text: true};
  }
  return null;
}

// A `beforeinput` in Type mode: what it adds to the pane.
export function inputOf(event) {
  const type = event && event.inputType;
  if (!type) return null;
  if (type === 'insertText' || type === 'insertReplacementText' || type === 'insertFromPaste'
      || type === 'insertFromDrop' || type === 'insertCompositionText') {
    return typeof event.data === 'string' && event.data ? {text: event.data} : null;
  }
  if (type === 'insertLineBreak' || type === 'insertParagraph') return {key: 'Enter'};
  if (type === 'deleteContentBackward' || type === 'deleteWordBackward' || type === 'deleteSoftLineBackward') {
    return {key: type === 'deleteContentBackward' ? 'Backspace' : 'ctrl+w'};
  }
  return null;
}

// ── frames (HerdrStreamDecoder) ───────────────────────────────────────────
export const MAX_FRAME_BYTES = 4 * 1024 * 1024;

export function decodeMessage(text) {
  let object;
  try {
    object = JSON.parse(text);
  } catch {
    return null;
  }
  if (!object || typeof object.type !== 'string') return null;
  if (object.type === 'terminal.frame') {
    if ((object.encoding || 'ansi') !== 'ansi') return null;
    const raw = typeof object.bytes === 'string' ? object.bytes : '';
    if (!/^[A-Za-z0-9+/=\r\n ]*$/.test(raw)) return null;
    let bytes;
    try {
      bytes = base64Bytes(raw.replace(/[\r\n ]/g, ''));
    } catch {
      return null;
    }
    if (bytes.length > MAX_FRAME_BYTES) return null;
    return {type: 'frame', full: object.full === true, width: Number.isInteger(object.width) ? object.width : null,
      height: Number.isInteger(object.height) ? object.height : null, bytes};
  }
  if (object.type === 'terminal.closed') {
    const reason = typeof object.reason === 'string' && /^[A-Za-z0-9_]{1,120}$/.test(object.reason) ? object.reason : null;
    return {type: 'closed', reason};
  }
  return {type: 'other'};
}

export function base64Bytes(text) {
  const raw = atob(text);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

// HerdrReconnectPolicy: 0.5, 1, 2, 4, 8, 8… seconds, never hammering a host.
export function reconnectDelay(attempt) {
  if (attempt <= 0) return 0;
  return Math.min(8000, 2 ** (Math.min(attempt, 8) - 1) * 500);
}

// ── pushes (NOTIFY-2, ODOMO-2) ────────────────────────────────────────────
// A push about a blocked pane names a session and a pane; the panel goes to
// that session (selecting it for this device when it is another) and shows
// that pane. Anything not in Herdr's own shapes is ignored.
export function pushPlace(ids) {
  if (!ids) return null;
  const session = typeof ids.session === 'string' && SESSION.test(ids.session) ? ids.session : null;
  const pane = session && typeof ids.pane === 'string' && PANE.test(ids.pane) ? ids.pane : null;
  return session ? {session, pane} : null;
}
