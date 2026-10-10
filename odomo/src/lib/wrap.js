// HERDR-8: the pane's screen as text that fits a phone - omodachi-ios
// `Herdr/HerdrWrap.swift`, rule for rule, so the App and Odomo wrap a pane the
// same way.
//
// Leo, on Herdr on a phone: "手机完全是不可用的，无论是 fit 还是 actual". The
// pane's geometry stays the desk's (never resized, attached or zoomed); Herdr's
// observe stream is already the rendered screen, so its rows are drawn as
// styled text at the normal terminal size and soft-wrapped to the width the
// phone has (Collie's answer to the same input).
//
// * A text row wraps. Rows an app hard-wrapped at the pane's width are joined
//   back into one line; a row the terminal itself wrapped is joined without a
//   space, which also mends a cut URL. Which rows the terminal wrapped is its
//   own flag when the grid has one (`grid.wrapped`); Herdr's frames place
//   every row and carry none, so there it is read from the cells (`join`).
// * A rule (a run of `─`) is cut to the width.
// * A framed row (`│ … │`) keeps its left edge and drops the right one.
// * A wide block - a box-drawn table, space-aligned columns, a drawing - is
//   kept as it is and pans sideways on its own.
// * A block that sits far to the right (codex's logo) moves left whole.
// A full-screen program (vim, less, top, a status bar across the bottom) is a
// picture: `fullScreen` says so and the mirror shows the grid with a note.
import * as storage from './storage.js';

const RULE = new Set(['─', '━', '═', '╌', '╍', '┄', '┅', '┈', '┉', '╴', '╶', '╸', '╺', '▔', '▁', '-', '=', '_',
  '╭', '╮', '╰', '╯', '┌', '┐', '└', '┘', '├', '┤', '┬', '┴', '┼', '╔', '╗', '╚', '╝', '╠', '╣', '╦', '╩', '╬',
  '┏', '┓', '┗', '┛', '┣', '┫', '┳', '┻', '╋', '╞', '╡', '╪']);
const HORIZONTAL = new Set(['─', '━', '═', '╌', '╍', '┄', '┅', '┈', '┉', '▔', '▁', '-', '=', '_']);
const BARS = new Set(['│', '┃', '║', '▏', '▕', '▎', '▌', '▐', '|']);
const JOINTS = new Set(['┬', '┴', '┼', '╦', '╩', '╬', '┳', '┻', '╋', '╪', '├', '┤', '╠', '╣']);
const MARKERS = new Set(['•', '◦', '▪', '‣', '-', '*', '+', '›', '❯', '>', '└', '⎿', '⏺', '●', '○', '✔', '✗', '✓', '✘']);

export function isBox(text) {
  if (!text || [...text].length !== 1) return false;
  const code = text.codePointAt(0);
  return code >= 0x2500 && code <= 0x259f;
}

const written = (cell) => cell.text !== ' ' && cell.text !== '';

function classify(source, cursorX, width) {
  const cells = source.slice();
  let end = cells.length;
  while (end > 0 && !written(cells[end - 1])) end--;
  if (cursorX != null && cursorX >= end && cursorX < cells.length) end = cursorX + 1;
  let lead = 0;
  while (lead < end && cells[lead].text === ' ' && cursorX !== lead) lead++;
  if (end === 0) return {cells, lead: 0, end: 0, cursorX, kind: cursorX == null ? 'blank' : 'text'};
  const chars = cells.slice(lead, end).filter(written).map((c) => c.text);
  const horizontal = chars.filter((c) => HORIZONTAL.has(c)).length;
  if (horizontal >= 8 && chars.every((c) => RULE.has(c))) return {cells, lead, end, cursorX, kind: 'rule'};
  if (chars.length > 2 && BARS.has(chars[0]) && BARS.has(chars[chars.length - 1])
    && chars.slice(1, -1).every((c) => !BARS.has(c))) {
    let right = end - 1;
    cells[right] = {text: ' ', style: cells[right].style};
    while (right > lead + 1 && cells[right - 1].text === ' ') right--;
    end = Math.max(lead + 1, right);
    if (cursorX != null && cursorX >= end) end = Math.min(cells.length, cursorX + 1);
    return {cells, lead, end, cursorX, kind: 'framed'};
  }
  return {cells, lead, end, cursorX, kind: 'text'};
}

function markWide(rows, width) {
  const boxy = (row) => {
    if (row.kind !== 'text' && row.kind !== 'rule') return false;
    const chars = row.cells.slice(row.lead, row.end).map((c) => c.text);
    return row.kind === 'rule' ? chars.some((c) => JOINTS.has(c)) : chars.filter(isBox).length >= 2;
  };
  const tabular = (row) => {
    if (row.kind !== 'text') return false;
    let gaps = 0;
    let run = 0;
    for (const cell of row.cells.slice(row.lead, row.end)) {
      if (cell.text === ' ') run++;
      else {
        if (run >= 2) gaps++;
        run = 0;
      }
    }
    return gaps >= 3;
  };
  const candidate = rows.map((row) => row.end > width && (boxy(row) || tabular(row)));
  rows.forEach((row, i) => {
    if (!candidate[i]) return;
    const neighbour = (i > 0 && candidate[i - 1]) || (i + 1 < rows.length && candidate[i + 1]);
    if (boxy(row) || neighbour) row.kind = 'wide';
  });
  // A few rows (up to three) between two wide rows are the same picture.
  let i = 0;
  while (i < rows.length) {
    if (rows[i].kind !== 'wide') {
      i++;
      continue;
    }
    let next = i + 1;
    while (next < rows.length && (rows[next].kind === 'text' || rows[next].kind === 'blank')) next++;
    if (next < rows.length && rows[next].kind === 'wide' && next - i - 1 <= 3) {
      for (let gap = i + 1; gap < next; gap++) rows[gap].kind = 'wide';
    }
    i = next;
  }
}

function dedent(rows, width) {
  let start = 0;
  const texty = (row) => row.kind === 'text' || row.kind === 'framed';
  while (start < rows.length) {
    if (!texty(rows[start])) {
      start++;
      continue;
    }
    let end = start;
    while (end + 1 < rows.length && texty(rows[end + 1])) end++;
    const lead = Math.min(...rows.slice(start, end + 1).map((r) => r.lead));
    if (lead >= Math.max(8, Math.floor(width / 3))) {
      const shift = lead - 2;
      for (let n = start; n <= end; n++) {
        const row = rows[n];
        row.cells = row.cells.slice(shift).concat(Array.from({length: shift}, () => ({text: ' ', style: row.cells[0].style})));
        row.lead -= shift;
        row.end -= shift;
        if (row.cursorX != null) row.cursorX = Math.max(0, row.cursorX - shift);
      }
    }
    start = end + 1;
  }
}

function hanging(row) {
  let indent = row.lead;
  if (indent < row.end && (MARKERS.has(row.cells[indent].text) || BARS.has(row.cells[indent].text))) {
    indent++;
    while (indent < row.end && row.cells[indent].text === ' ') indent++;
  }
  return indent;
}

// A double-width character starts at `index` (its second cell is empty).
function isWide(cells, index) {
  if (index < 0 || index + 1 >= cells.length) return false;
  const text = cells[index].text;
  return !!text && text !== ' ' && cells[index + 1].text === '';
}

// The row opens like a line of its own: a list marker or a prompt.
function startsLine(row) {
  if (row.lead >= row.end) return false;
  const first = row.cells[row.lead].text;
  if (MARKERS.has(first)) return true;
  const second = row.lead + 1 < row.cells.length ? row.cells[row.lead + 1].text : ' ';
  return ['$', '%', '#'].includes(first) && second === ' ';
}

// Cells from the row's first written cell to the first blank one.
function firstWord(row) {
  let end = row.lead;
  while (end < row.end && row.cells[end].text !== ' ') end++;
  return end - row.lead;
}

// The row starts with a number followed by a space, `.` or `)`.
function numbered(cells, start) {
  let i = start;
  while (i < cells.length && /^[0-9]$/.test(cells[i].text)) i++;
  return i > start && i < cells.length && [' ', '.', ')'].includes(cells[i].text);
}

// Room an app may leave at the right edge when it wraps its own words.
const WRAP_SLACK = 2;

// REL2-POLISH-2 (the App's `HerdrWrap.join`, rule for rule). A terminal wrap
// is the terminal's own flag when the grid has one; without it (Herdr's
// frames) the row has to fill the width and be cut mid-word, the next row
// carrying on at the first column. An app's word wrap only moves a word down
// because it did not fit: when the next row's first word would have fitted,
// the next row is a new line (nvim's line 1 was joined with line 2).
function join(row, next, cols) {
  if (row.kind !== 'text' || next.kind !== 'text') return null;
  if (next.continued != null) {
    if (next.continued) return 'direct';
  } else if (row.end >= cols && row.lead < cols && next.lead === 0 && next.end > 0
    && row.cells[row.end - 1].text !== ' ' && !startsLine(next)) {
    return 'direct';
  }
  const length = next.end - next.lead;
  if (row.end < cols - 16 || length <= 0 || !(next.lead > 0 || row.lead === 0)) return null;
  const first = next.cells[next.lead];
  if (first && MARKERS.has(first.text)) return null;
  const hang = hanging(row);
  if (next.lead !== hang && next.lead !== row.lead) return null;
  const last = row.cells[row.end - 1].text;
  if ('.:!?'.includes(last) && last !== '' && next.lead === row.lead && hang === row.lead) return null;
  // Numbered rows (an editor's line numbers, a numbered list) are lines of
  // their own, however close to the edge the one before ends.
  if (numbered(row.cells, row.lead) && numbered(next.cells, next.lead)) return null;
  // HERDR-10: Chinese or Japanese wrapped by the app has no spaces to put
  // back - joined at the next row's indent with none ('tight').
  if (isWide(row.cells, row.end - 1) && isWide(next.cells, next.lead)) return 'tight';
  if (row.end + 1 + firstWord(next) <= cols - WRAP_SLACK) return null;
  return 'space';
}

const sameStyle = (a, b) => a === b || JSON.stringify(a) === JSON.stringify(b);

function runs(cells, from, to, cursorX) {
  const out = [];
  for (let x = from; x < Math.min(to, cells.length); x++) {
    const cell = cells[x];
    if (cell.text === '') continue;
    const cursor = x === cursorX;
    const last = out[out.length - 1];
    if (last && !cursor && !last.cursor && last.link === (cell.link || null) && sameStyle(last.style, cell.style)) last.text += cell.text;
    else out.push({text: cell.text, style: cell.style, link: cell.link || null, cursor});
  }
  return out;
}

// Rows joined into one line meet in the middle of what is one run of text (a
// URL the terminal cut): put it back together.
function merge(line) {
  const out = [];
  for (const run of line) {
    const last = out[out.length - 1];
    if (last && !last.cursor && !run.cursor && last.link === run.link && sameStyle(last.style, run.style)) {
      out[out.length - 1] = {...last, text: last.text + run.text};
    } else {
      out.push(run);
    }
  }
  return out;
}

// vim's `~` and ruler, less's prompt, top's header, a painted status bar.
export function looksFullScreen(rows, cols) {
  if (!rows.length || !cols) return false;
  const texts = rows.map((row) => row.map((c) => c.text).join('').trim());
  if (texts.filter((t) => t === '~').length >= 3) return true;
  const ruler = /\b\d+,\d+(-\d+)?\s+(All|Top|Bot|\d+%)\s*$/;
  if (texts.slice(-2).some((t) => ruler.test(t))) return true;
  const last = texts[texts.length - 1];
  if (last === ':' || last.endsWith('(END)') || /^lines \d+-\d+/.test(last)) return true;
  if (texts.slice(0, 12).some((t) => /^\s*PID\s+USER\b/.test(t))) return true;
  for (const row of rows.slice(-2)) {
    const painted = row.filter((c) => c.style && (c.style.inverse || c.style.bg)).length;
    if (painted >= cols * 0.9) return true;
  }
  return false;
}

// `grid` is {cols, rows: [[{text, style, link}]], cursor: {x, y} | null,
// wrapped?: [bool]} - `wrapped`, when the source has it, says per row that
// the terminal wrapped the row before onto it.
export function layout(grid, width, agent) {
  width = Math.max(8, width || 0);
  const rows = grid.rows.map((cells, y) => {
    const row = classify(cells, grid.cursor && grid.cursor.y === y ? grid.cursor.x : null, width);
    if (Array.isArray(grid.wrapped)) row.continued = !!grid.wrapped[y];
    return row;
  });
  const fullScreen = !agent && looksFullScreen(rows.map((r) => r.cells), grid.cols);
  markWide(rows, width);
  dedent(rows, width);
  const first = rows.findIndex((r) => r.kind !== 'blank');
  let last = rows.length - 1;
  while (last >= 0 && rows[last].kind === 'blank') last--;
  const blocks = [];
  if (first < 0) return {blocks, fullScreen};
  let i = first;
  let previousBlank = false;
  while (i <= last) {
    const row = rows[i];
    if (row.kind === 'blank') {
      if (!previousBlank) blocks.push({kind: 'blank'});
      previousBlank = true;
      i++;
      continue;
    }
    if (row.kind === 'wide') {
      const block = [];
      while (i <= last && rows[i].kind === 'wide') {
        block.push(runs(rows[i].cells, 0, rows[i].end, rows[i].cursorX));
        i++;
      }
      blocks.push({kind: 'wide', rows: block});
    } else if (row.kind === 'rule') {
      blocks.push({kind: 'rule', runs: runs(row.cells, 0, Math.min(row.end, row.lead + width - 1), row.cursorX)});
      i++;
    } else {
      let line = runs(row.cells, 0, row.end, row.cursorX);
      const indent = hanging(row);
      let current = row;
      let joint;
      while (i + 1 <= last && (joint = join(current, rows[i + 1], grid.cols))) {
        const next = rows[i + 1];
        if (joint === 'space') line.push({text: ' ', style: null, link: null, cursor: false});
        line = line.concat(runs(next.cells, joint === 'direct' ? 0 : next.lead, next.end, next.cursorX));
        current = next;
        i++;
      }
      blocks.push({kind: 'text', runs: merge(line), indent: Math.min(indent, Math.floor(width / 2))});
      i++;
    }
    previousBlank = false;
  }
  return {blocks, fullScreen};
}

// ── modes ──────────────────────────────────────────────────────────────────
// Wrap → Fit → Actual → Wrap, as the App's `HerdrMirrorMode`; remembered per
// kind of pane (a shell, or an agent by name) in this browser only.
// AGENT-UI-3: on a phone only Wrap ↔ Fit, as the iPhone App (HERDR-12);
// Actual size is for a tablet or a desk.
export const MODES = ['wrap', 'fit', 'actual'];
export function nextMode(mode, phone = false) {
  const from = mode === 'custom' ? 'fit' : mode;
  if (phone) return from === 'wrap' ? 'fit' : 'wrap';
  return MODES[(MODES.indexOf(from) + 1) % MODES.length];
}

export function phoneLike() {
  try {
    const coarse = typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches;
    const small = typeof screen === 'object' && screen && Math.min(screen.width, screen.height) < 600;
    return !!(coarse && small);
  } catch {
    return false;
  }
}

export function preferredMode(kind, phone = phoneLike()) {
  const stored = storage.local('herdr.mirror.' + (kind || 'shell'), null);
  // A phone that remembered Actual size (before it left the phone's cycle)
  // opens that kind on Fit.
  if (phone && stored === 'actual') return 'fit';
  return MODES.includes(stored) ? stored : (phone ? 'wrap' : 'fit');
}

export function rememberMode(kind, mode) {
  storage.setLocal('herdr.mirror.' + (kind || 'shell'), mode);
}
