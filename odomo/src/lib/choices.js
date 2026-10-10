// HERDR-9: the choices a pane is asking for, read off the screen the mirror
// shows - the same rules as the App (omodachi-ios `HerdrChoices.swift`), which
// ported them from ColliePWA (AltanS/collie `25468b0`, MIT; licence in
// `public/collie-LICENSE.txt`): `web/src/lib/harness/codex/approval.ts`,
// `codex/ask.ts`, `claude/prompt-select.ts`, `claude/markers.ts` and
// `harness/menu-hints.ts`.
//
// A menu is a shape, never words alone: a hint footer among the last rows
// (`Press enter to confirm or esc to cancel`, `Enter to select · Esc to
// cancel`, `enter select · esc back`), a contiguous run of numbered options
// right above it, and exactly one of them carrying the selection marker
// (› or ❯). A `(y/n)` / `[Y/n]` question on the cursor's row is the other.
// A tap sends what that program takes: the digit (codex; Claude's permission
// rows), the digit then Enter (Claude's "Enter to select"), the arrows from
// the marked row plus Enter (past 9, or an unnumbered list), y / n and Enter.

const MARKERS = new Set(['›', '❯', '▶', '▸']);
const FOOTER_REACH = 3;
const FOOTER_GAP = 3;
const OPTION_REACH = 40;
const QUESTION_REACH = 12;

const chars = (text) => Array.from(text);
export const isBlank = (text) => /^ *$/.test(text);
export const indent = (text) => text.length - text.replace(/^ +/, '').length;
export const trimEnd = (text) => text.replace(/[ \u0000]+$/, '');

function optionRow(text) {
  let rest = text.replace(/^ +/, '');
  let pointed = false;
  const first = chars(rest)[0];
  if (first && MARKERS.has(first)) {
    pointed = true;
    rest = rest.slice(first.length).replace(/^ +/, '');
  }
  const column = chars(text).length - chars(rest).length;
  const m = /^(\d{1,2})[.)] (.*)$/.exec(rest);
  if (!m) return null;
  const number = Number(m[1]);
  const label = m[2].trim();
  if (!number || !label) return null;
  return {row: 0, number, pointed, column, text: label};
}

const pointerPrefix = (text) => {
  const first = chars(text.replace(/^ +/, ''))[0];
  return !!first && MARKERS.has(first);
};
const isContinuation = (texts, row) => !isBlank(texts[row]) && indent(texts[row]) >= 4;

export function isFooter(text) {
  const value = text.trim().toLowerCase();
  if (!value || value.length > 200 || optionRow(text) || pointerPrefix(text)) return false;
  if (value.includes('ctrl+g to edit')) return true; // Claude's plan approval
  const names = value.includes('enter') || value.includes('esc') || value.includes('⏎') || value.includes('↵');
  const verbs = ['confirm', 'select', 'submit', 'continue', 'cancel', 'back', 'dismiss', 'navigate', 'amend', 'choose', 'close'];
  return names && verbs.some((verb) => value.includes(verb));
}

const isTrailingHint = (text) => {
  const value = text.trim().toLowerCase();
  return value.length <= 60 && (value.startsWith('esc to ') || value.startsWith('ctrl+') || value.startsWith('tab to '));
};
const isHintLine = (text) => {
  const value = text.trim().toLowerCase();
  if (value.length > 120 || !value.includes(' to ')) return false;
  return value.includes('tab') || value.includes('ctrl') || value.includes('←') || value.includes('→');
};
const notesFocused = (text) => text.trim().toLowerCase().startsWith('tab or esc to clear notes');

// HERDR-10: codex's notes box is open; what the composer sends is the note.
export function notesOpen(rows) {
  const texts = rows.map(trimEnd);
  let end = texts.length - 1;
  while (end >= 0 && isBlank(texts[end])) end--;
  if (end < 0) return false;
  return texts.slice(Math.max(0, end - 2), end + 1).some(notesFocused);
}

// Lines a narrow pane wrapped, joined back: a space between words, none
// inside Chinese or Japanese text ("再总" + "结规律" is "再总结规律").
const WIDE_TEXT = /[\u1100-\u115F\u2E80-\u303E\u3041-\u33FF\u3400-\u4DBF\u4E00-\u9FFF\uA000-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF\uFE30-\uFE4F\uFF00-\uFF60\uFFE0-\uFFE6]/;
export function joinWrapped(parts) {
  let out = '';
  for (const part of parts) {
    if (!part) continue;
    if (out && !(WIDE_TEXT.test(out[out.length - 1]) && WIDE_TEXT.test(part[0]))) out += ' ';
    out += part;
  }
  return out;
}
const isScrollMark = (text) => {
  const value = text.trim();
  return (value.startsWith('↑') || value.startsWith('↓')) && chars(value).length <= 24;
};
const isRule = (text) => {
  const value = text.trim();
  return chars(value).length >= 3 && chars(value).every((c) => '─━═╌┄┈-▔▁'.includes(c));
};
const checkbox = (label) => /^(\[[ xX✔✓]\]|☐|☒)/.test(label.trim());
const freeText = (label) => {
  const value = label.toLowerCase();
  return value.startsWith('type something') || value.startsWith('tell claude what to change')
    || value.startsWith('tell codex what to change');
};
const amendField = (lines) => {
  const whole = lines.map((l) => l.trim()).join(' ').toLowerCase();
  if (whole.startsWith('yes, and tell claude what to do next')) return true;
  return whole.startsWith('no, and tell claude what to do differently') && !whole.endsWith('(esc)');
};

export function lasting(label) {
  const value = label.toLowerCase().replace(/’/g, "'");
  return ["don't ask again", 'always', 'for this session', 'during this session', 'for this conversation',
    'in the future', 'remember', 'every time', 'from now on'].some((w) => value.includes(w));
}

const offersEscape = (footer) => footer.includes('esc') && ['cancel', 'back', 'dismiss', 'close'].some((w) => footer.includes(w));

export function stripShortcut(label) {
  const value = label.trim();
  const m = /^(.*?)\s*\(([^()]*)\)$/.exec(value);
  if (!m) return value;
  const inner = m[2].toLowerCase();
  const hint = chars(inner).length === 1 || ['esc', 'enter', 'tab', 'shift+tab'].includes(inner)
    || inner.startsWith('ctrl+') || inner.startsWith('shift+');
  return hint ? m[1].trim() : value;
}

export function pointerWalk(pointed, target) {
  const steps = target - pointed;
  return [...Array(Math.abs(steps)).fill(steps > 0 ? 'Down' : 'Up'), 'Enter'];
}

function question(texts, first) {
  let nearest = null;
  let asks = null;
  for (let row = first - 1; row >= 0 && first - row <= QUESTION_REACH; row--) {
    const text = texts[row];
    if (isRule(text)) break;
    if (isBlank(text)) continue;
    const value = text.trim();
    if (!nearest) nearest = {text: value, row};
    // HERDR-10: a Chinese question ends in a full-width "？".
    const ends = value.endsWith('?') || value.endsWith('？');
    if (ends && !value.startsWith('Reason:')) return {text: value, row};
    if (!asks && (value.includes('?') || value.includes('？'))) asks = {text: value, row};
  }
  return asks || nearest;
}

function wrapsLabel(lines, option) {
  if (lines.length < 2) return false;
  const labelColumn = option.column + String(option.number).length + 2;
  return lines.slice(1).every((l) => indent(l) <= labelColumn + 1);
}

function numbered(texts, bottom, footer, footerText) {
  const options = [];
  let pending = [];
  let sawScroll = false;
  let row = bottom;
  let floor = Math.max(0, bottom - OPTION_REACH);
  while (row >= floor) {
    const text = texts[row];
    const option = optionRow(text);
    if (option) {
      option.row = row;
      option.text = [option.text, ...pending].join('\n');
      pending = [];
      options.unshift(option);
      row--;
      continue;
    }
    if (isScrollMark(text)) { sawScroll = true; row--; continue; }
    if (isRule(text) && (options.length || pending.length)) { row--; continue; }
    if (!isBlank(text) && indent(text) >= 4) {
      pending.unshift(text);
      row--;
      floor = Math.max(0, floor - 1);
      continue;
    }
    break;
  }
  if (options.length < 2 || options[options.length - 1].row < bottom - 8) return null;
  const top = row;
  if (options.some((o, i) => o.number !== options[0].number + i)) return null;
  if (options.length > 30) return null;
  if (options[0].number !== 1 && !sawScroll && !(top >= 0 && isScrollMark(texts[top]))) return null;
  const pointedAt = options.findIndex((o) => o.pointed);
  if (pointedAt < 0 || options.filter((o) => o.pointed).length !== 1) return null;
  if (options.some((o) => checkbox(o.text.split('\n')[0]))) return null;

  const selectThenEnter = footerText.includes('enter to select');
  // HERDR-10: codex's question card says "tab to add notes" on its footer,
  // or on its own row above it when a narrow pane wraps it.
  const notesCard = texts.slice(Math.min(bottom + 1, footer), footer + 1).join(' ').toLowerCase()
    .includes('tab to add notes');
  const result = [];
  for (const [offset, option] of options.entries()) {
    const lines = option.text.split('\n');
    const head = lines[0].trim();
    if (freeText(head) || amendField(lines)) {
      if (option.pointed) return null;
      continue;
    }
    const split = head.split('  ').map((s) => s.trim()).filter(Boolean);
    let label = split[0] || head;
    let detail = split.slice(1).join(' ');
    const more = lines.slice(1).map((l) => l.trim()).filter(Boolean);
    if (more.length) {
      if (!detail && !selectThenEnter && wrapsLabel(lines, option)) label = joinWrapped([label, ...more]);
      else detail = joinWrapped([detail, ...more]);
    }
    label = stripShortcut(label);
    // HERDR-10: "None of the above" walks the marker there and opens the
    // notes box (Tab); the composer takes the note, its Return submits.
    const opensNote = notesCard && label.toLowerCase() === 'none of the above';
    const keys = opensNote ? [...pointerWalk(pointedAt, offset).slice(0, -1), 'Tab']
      : option.number <= 9
        ? (selectThenEnter ? [String(option.number), 'Enter'] : [String(option.number)])
        : pointerWalk(pointedAt, offset);
    result.push({index: result.length, label, detail: detail || null, keys, highlighted: option.pointed,
      lasting: lasting(label), number: option.number, opensNote});
  }
  if (!result.length) return null;
  const first = options[0].row;
  const q = question(texts, first);
  const start = Math.max(0, Math.min(q ? q.row : first, first - QUESTION_REACH));
  return {kind: 'menu', question: q ? q.text : null, options: result, escape: offersEscape(footerText),
    signature: texts.slice(start, footer + 1).join('\n')};
}

function footerless(texts, end) {
  const menu = numbered(texts, end, end, '');
  if (!menu) return null;
  const first = menu.options[0];
  const last = menu.options[menu.options.length - 1];
  if (!first.label.toLowerCase().startsWith('yes') || !last.label.toLowerCase().startsWith('no')) return null;
  if (!menu.question || !menu.question.startsWith('Do you want to') || !menu.question.endsWith('?')) return null;
  return menu;
}

function pointed(texts, bottom, footer, footerText) {
  if (!footerText.includes('enter')) return null;
  const rows = [];
  let pointerIndent = -1;
  for (let row = bottom; row >= Math.max(0, bottom - 24); row--) {
    const text = texts[row];
    if (isBlank(text)) continue;
    if (optionRow(text)) return null;
    const isPointed = pointerPrefix(text);
    const lead = indent(text);
    if (pointerIndent < 0) {
      pointerIndent = isPointed ? lead : lead - 2;
      if (pointerIndent < 0) return null;
    }
    if (isPointed && lead === pointerIndent) {
      rows.unshift({row, label: chars(text).slice(lead + 1).join('').trim(), pointed: true});
    } else if (!isPointed && lead === pointerIndent + 2 && lead > 0) {
      rows.unshift({row, label: text.trim(), pointed: false});
    } else {
      break;
    }
  }
  const at = rows.findIndex((r) => r.pointed);
  if (rows.length < 2 || at < 0 || rows.filter((r) => r.pointed).length !== 1) return null;
  const options = rows.map((r, i) => ({index: i, label: stripShortcut(r.label), detail: null, keys: pointerWalk(at, i),
    highlighted: r.pointed, lasting: lasting(r.label), number: null}));
  const first = rows[0].row;
  const q = question(texts, first);
  const start = Math.max(0, Math.min(q ? q.row : first, first - QUESTION_REACH));
  return {kind: 'menu', question: q ? q.text : null, options, escape: offersEscape(footerText),
    signature: texts.slice(start, footer + 1).join('\n')};
}

function menu(texts, end) {
  let footer = -1;
  let seen = 0;
  for (let index = end; index >= 0 && seen < FOOTER_REACH; index--) {
    const text = texts[index];
    if (isBlank(text)) continue;
    if (notesFocused(text)) return null;
    if (isFooter(text)) { footer = index; break; }
    if (!isTrailingHint(text)) break;
    seen++;
  }
  if (footer <= 0) return footerless(texts, end);
  const footerText = texts[footer].toLowerCase();
  let bottom = footer - 1;
  for (let gap = 0; bottom >= 0 && gap < FOOTER_GAP && !optionRow(texts[bottom]) && !pointerPrefix(texts[bottom]); gap++) {
    if (!(isBlank(texts[bottom]) || isScrollMark(texts[bottom]) || isHintLine(texts[bottom]))) break;
    bottom--;
  }
  if (bottom < 0) return null;
  if (optionRow(texts[bottom]) || isContinuation(texts, bottom)) {
    const found = numbered(texts, bottom, footer, footerText);
    if (found) return found;
  }
  return pointed(texts, bottom, footer, footerText);
}

const YES_NO = /[([]\s*(y|Y|yes|Yes|YES)\s*\/\s*(n|N|no|No|NO)\s*(?:\/.*?)?[)\]]\s*[:?]?\s*$/;

function yesNo(texts, end, cursorRow) {
  if (cursorRow != null && cursorRow !== end) return null;
  const text = texts[end];
  const m = YES_NO.exec(text);
  if (!m) return null;
  const [yes, no] = [m[1], m[2]];
  const keys = (word) => [...Array.from(word.toLowerCase()), 'Enter'];
  return {
    kind: 'yesNo', question: text.trim(), escape: false,
    options: [
      {index: 0, label: 'yes', detail: null, keys: keys(yes), highlighted: yes === 'Y' && no === 'n', lasting: false, number: null},
      {index: 1, label: 'no', detail: null, keys: keys(no), highlighted: no === 'N' && yes === 'y', lasting: false, number: null},
    ],
    signature: texts.slice(Math.max(0, end - 3), end + 1).join('\n'),
  };
}

// The choices at the bottom of `rows` (one string a screen row), or null.
export function readChoices(rows, cursorRow = null) {
  const texts = rows.map(trimEnd);
  let end = texts.length - 1;
  while (end >= 0 && isBlank(texts[end])) end--;
  if (end < 0) return null;
  return menu(texts, end) || yesNo(texts, end, cursorRow);
}

// Whether two reads are the same question (what a tap is guarded with).
export const sameChoices = (a, b) => !!a && !!b && a.signature === b.signature
  && a.options.length === b.options.length && a.options.every((o, i) => o.keys.join(' ') === b.options[i].keys.join(' '));

// Keys from an empty composer that go straight to the pane, in Herdr's names
// (REMOTE-INPUT-2b's rule); null when the key is the box's own.
export function passthroughKey(event) {
  if (event.isComposing || event.metaKey) return null;
  const k = event.key;
  if (event.ctrlKey && !event.altKey && /^[a-z]$/i.test(k)) return 'ctrl+' + k.toLowerCase();
  if (event.ctrlKey || event.altKey) return null;
  switch (k) {
    case 'Backspace': return event.shiftKey ? null : 'Backspace';
    case 'Enter': return event.shiftKey ? null : 'Enter';
    case 'Tab': return event.shiftKey ? 'shift+Tab' : 'Tab';
    case 'Escape': return 'Escape';
    case 'ArrowUp': return event.shiftKey ? null : 'Up';
    case 'ArrowDown': return event.shiftKey ? null : 'Down';
    case 'ArrowLeft': return event.shiftKey ? null : 'Left';
    case 'ArrowRight': return event.shiftKey ? null : 'Right';
    default: return null;
  }
}
