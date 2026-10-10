// ODOMO-2: the Herdr mirror's screen.
//
// Herdr's `observe` sends the pane as ANSI (`terminal.frame`, base64; `full`
// repaints from scratch, the rest continue the last). A real VT emulator has
// to read it - codex's TUI, nvim and a shell prompt all move the cursor, use
// the alternate screen and scroll regions - so this is xterm.js's headless
// core (`@xterm/headless`, MIT, the same parser as xterm.js, no DOM), and the
// picture is drawn here on one <canvas>, cell by cell, from its buffer.
//
// Not xterm.js's own renderer: that one injects <style> elements, which
// Odomo's CSP (`style-src 'self'`, no inline style elements) refuses. A
// canvas needs no style at all, keeps a wide pane's columns exactly aligned
// (each glyph is placed at its cell, CJK and Nerd Font fallbacks included),
// and scales cleanly: the App's "fit to width" is a scale, never a resize
// of the pane (HERDR-4: the desktop's pane size does not move).

import {Terminal} from '@xterm/headless/lib-headless/xterm-headless.mjs';
import {detect} from './links.js';

export const BASE_FONT = 13; // HerdrStore.fontSize
export const LINE = 1.25;
// HERDR-4: fitting a 180-column pane on a phone stops at 7 pt.
export const MIN_SCALE = 7 / BASE_FONT;
export const MAX_SCALE = 3;
// AGENT-UI-2: Nerd Font code points come from the bundled symbols face
// (styles.css), never from whatever the phone happens to have - which is
// nothing, so they were empty boxes.
export const SYMBOLS_FAMILY = '"Symbols Nerd Font Mono Web"';
export const FONT_FAMILY = '"JetBrains Mono Web", ' + SYMBOLS_FAMILY + ', "JetBrains Mono", ui-monospace, SFMono-Regular, '
  + 'Menlo, Consolas, "Noto Sans Mono CJK SC", "PingFang SC", monospace';

// AGENT-UI-2: which of the symbols face's two files a code point is in.
export function symbolsPart(codepoint) {
  if (codepoint >= 0xF0000 && codepoint <= 0xFFFFD) return 'spua';
  if ((codepoint >= 0xE000 && codepoint <= 0xF8FF) || (codepoint >= 0x23FB && codepoint <= 0x23FE)
    || codepoint === 0x2B58) return 'bmp';
  return null;
}

// AGENT-UI-2: the block elements a TUI builds pictures and bars from are
// drawn as rectangles that fill the cell, like a terminal does - a font's
// glyph is shorter than the 1.25 line and left stripes between rows. The
// rectangle is [left, top, right, bottom] in fractions of the cell.
export function blockRect(codepoint) {
  if (codepoint === 0x2580) return [0, 0, 1, 0.5]; // ▀
  if (codepoint >= 0x2581 && codepoint <= 0x2588) return [0, 1 - (codepoint - 0x2580) / 8, 1, 1]; // ▁..█
  if (codepoint >= 0x2589 && codepoint <= 0x258F) return [0, 0, (0x2590 - codepoint) / 8, 1]; // ▉..▏
  if (codepoint === 0x2590) return [0.5, 0, 1, 1]; // ▐
  if (codepoint === 0x2594) return [0, 0, 1, 1 / 8]; // ▔
  if (codepoint === 0x2595) return [7 / 8, 0, 1, 1]; // ▕
  return null;
}

// AGENT-UI-2: the powerline separators lualine and prompts put between
// segments, as shapes that meet the cell's top and bottom edges (the font's
// are shorter than the line and left notches).
export function powerlineShape(codepoint) {
  switch (codepoint) {
    case 0xE0B0: return {fill: [[0, 0], [1, 0.5], [0, 1]]}; // 
    case 0xE0B2: return {fill: [[1, 0], [0, 0.5], [1, 1]]}; // 
    case 0xE0B1: return {line: [[0, 0], [1, 0.5], [0, 1]]}; // 
    case 0xE0B3: return {line: [[1, 0], [0, 0.5], [1, 1]]}; // 
    case 0xE0B4: return {arc: 'right'}; // 
    case 0xE0B6: return {arc: 'left'}; // 
    default: return null;
  }
}

function drawShape(ctx, shape, x, y, w, h) {
  ctx.beginPath();
  if (shape.arc) {
    // A half disc as tall as the cell, its flat side on the segment's edge.
    const r = h / 2;
    const scaleX = w / r;
    ctx.save();
    ctx.translate(shape.arc === 'right' ? x : x + w, y + r);
    ctx.scale(scaleX, 1);
    ctx.arc(0, 0, r, shape.arc === 'right' ? -Math.PI / 2 : Math.PI / 2, shape.arc === 'right' ? Math.PI / 2 : Math.PI * 1.5);
    ctx.restore();
    ctx.closePath();
    ctx.fill();
    return;
  }
  const points = shape.fill || shape.line;
  points.forEach(([px, py], i) => (i ? ctx.lineTo(x + px * w, y + py * h) : ctx.moveTo(x + px * w, y + py * h)));
  if (shape.fill) {
    ctx.closePath();
    ctx.fill();
  } else {
    ctx.lineWidth = 1;
    ctx.strokeStyle = ctx.fillStyle;
    ctx.stroke();
  }
}

// The 16 slots, from the host theme's roles (TerminalPalette.ansiRoles).
const ROLES = ['darker_background', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'foreground',
  'dark_foreground', 'bright_red', 'bright_green', 'bright_yellow', 'bright_blue', 'bright_magenta',
  'bright_cyan', 'bright_foreground'];
// Tokyo Night until the host's theme arrives (styles.css's own default).
const DEFAULT = {background: '#1a1b26', foreground: '#a9b1d6', cursor: '#7aa2f7',
  ansi: ['#15161e', '#f7768e', '#9ece6a', '#e0af68', '#7aa2f7', '#bb9af7', '#7dcfff', '#a9b1d6',
    '#414868', '#f7768e', '#9ece6a', '#e0af68', '#7aa2f7', '#bb9af7', '#7dcfff', '#c0caf5']};
const HEX = /^#[0-9a-fA-F]{6}$/;

export function paletteOf(theme) {
  const colors = theme && theme.colors && typeof theme.colors === 'object' ? theme.colors : {};
  const terminal = theme && theme.shell && theme.shell.terminal && typeof theme.shell.terminal === 'object'
    ? theme.shell.terminal : {};
  const pick = (value, fallback) => (HEX.test(value || '') ? value : fallback);
  return {
    background: pick(terminal.background, pick(colors.background, DEFAULT.background)),
    foreground: pick(terminal.foreground, pick(colors.foreground, DEFAULT.foreground)),
    cursor: pick(terminal.cursor, pick(colors.accent, DEFAULT.cursor)),
    ansi: ROLES.map((role, i) => pick(terminal['color' + i], pick(colors[role], DEFAULT.ansi[i]))),
  };
}

// xterm's 256 colours: 16 from the palette, a 6x6x6 cube, 24 greys.
export function color256(index, palette) {
  if (index < 16) return palette.ansi[index];
  if (index < 232) {
    const n = index - 16;
    const step = (v) => (v === 0 ? 0 : 55 + v * 40);
    return rgb(step(Math.floor(n / 36)), step(Math.floor(n / 6) % 6), step(n % 6));
  }
  const g = 8 + (index - 232) * 10;
  return rgb(g, g, g);
}

function rgb(r, g, b) {
  return '#' + [r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('');
}

function rgbOf(value) {
  return rgb((value >> 16) & 255, (value >> 8) & 255, value & 255);
}

// `fit` follows the width it is given; `actual` is 1:1; `custom` is a pinch.
export function scaleFor(zoom, cols, cellWidth, width) {
  if (zoom.mode === 'actual') return 1;
  if (zoom.mode === 'custom') return Math.min(MAX_SCALE, Math.max(MIN_SCALE, zoom.scale));
  if (!cols || !cellWidth || !width) return 1;
  return Math.min(1.5, Math.max(MIN_SCALE, width / (cols * cellWidth)));
}

// HERDR-10: cell widths as Herdr counts them. xterm.js measures with Unicode
// 6 unless told otherwise, where an emoji (🙂 ✅ ⏳) takes one cell; Herdr,
// like the App's SwiftTerm, gives it two. Herdr places the cell after every
// wide character by absolute position, so with one cell the second one was
// never written: a blank after each emoji on a full frame, and whatever had
// been there before on an increment - the stale leftovers Leo saw in the App
// for a different reason. East Asian Wide and Full-width ranges, and
// anything drawn as an emoji by default, take two cells; combining marks and
// format characters none.
const WIDE = [[0x1100, 0x115F], [0x231A, 0x231B], [0x2329, 0x232A], [0x2E80, 0x303E], [0x3041, 0x33FF],
  [0x3400, 0x4DBF], [0x4E00, 0x9FFF], [0xA000, 0xA4CF], [0xA960, 0xA97F], [0xAC00, 0xD7A3], [0xF900, 0xFAFF],
  [0xFE10, 0xFE19], [0xFE30, 0xFE6F], [0xFF00, 0xFF60], [0xFFE0, 0xFFE6], [0x16FE0, 0x16FE4], [0x17000, 0x18CFF],
  [0x1B000, 0x1B2FF], [0x1F200, 0x1F2FF], [0x20000, 0x2FFFD], [0x30000, 0x3FFFD]];
const EMOJI = /\p{Emoji_Presentation}/u;
const ZERO = /[\p{Mn}\p{Me}\p{Cf}]/u;
export function cellWidth(codepoint) {
  if (codepoint === 0) return 0;
  if (codepoint < 0x20 || (codepoint >= 0x7f && codepoint < 0xa0)) return 0;
  if (codepoint < 0x300) return 1;
  const char = String.fromCodePoint(codepoint);
  if (codepoint === 0x200D || ZERO.test(char)) return 0;
  if ((codepoint >= 0x1160 && codepoint <= 0x11FF) || (codepoint >= 0xFE00 && codepoint <= 0xFE0F)) return 0;
  for (const [low, high] of WIDE) if (codepoint >= low && codepoint <= high) return 2;
  return EMOJI.test(char) ? 2 : 1;
}
const HERDR_WIDTHS = {
  version: 'herdr',
  wcwidth: cellWidth,
  // xterm's own providers' rule: a zero-width character joins the one
  // before it, which keeps its width (UnicodeService's property layout).
  charProperties(codepoint, preceding) {
    let width = cellWidth(codepoint);
    let join = width === 0 && preceding !== 0;
    if (join) {
      const before = (preceding >> 1) & 3;
      if (before === 0) join = false; else if (before > width) width = before;
    }
    return ((width & 3) << 1) | (join ? 1 : 0);
  },
};

export class Mirror {
  constructor({cols = 80, rows = 24, palette = DEFAULT, measure = null} = {}) {
    this.term = new Terminal({cols, rows, scrollback: 0, allowProposedApi: true, convertEol: false});
    try {
      this.term.unicode.register(HERDR_WIDTHS);
      this.term.unicode.activeVersion = HERDR_WIDTHS.version;
    } catch {
      // an xterm without the (experimental) provider API keeps Unicode 6
    }
    this.palette = palette;
    this.cell = measure || {width: 7.8, height: Math.ceil(BASE_FONT * LINE)};
    this.canvas = null;
    this.scale = 1;
    this.dirty = true;
    this.frame = 0;
    this.awaitingFull = true;
    this.onPainted = null;
    this._cell = null;
  }

  get cols() { return this.term.cols; }
  get rows() { return this.term.rows; }

  attach(canvas) {
    this.canvas = canvas;
    if (canvas && canvas.getContext) {
      const ctx = canvas.getContext('2d');
      if (ctx) {
        ctx.font = BASE_FONT + 'px ' + FONT_FAMILY;
        const width = ctx.measureText('MMMMMMMMMM').width / 10;
        if (width > 0) this.cell = {width, height: Math.ceil(BASE_FONT * LINE)};
      }
    }
    this.invalidate();
  }

  setPalette(palette) {
    this.palette = palette;
    this.invalidate();
  }

  resize(cols, rows) {
    if (!Number.isInteger(cols) || !Number.isInteger(rows) || cols < 1 || rows < 1) return;
    if (cols !== this.term.cols || rows !== this.term.rows) {
      this.term.resize(Math.min(1000, cols), Math.min(1000, rows));
      this.invalidate();
    }
  }

  // One decoded `terminal.frame`. An increment before the first full frame
  // has nothing to apply to and is dropped (§1.1).
  feed(frame, done) {
    if (frame.full) {
      this.awaitingFull = false;
      this.term.reset();
      if (frame.width && frame.height) this.resize(frame.width, frame.height);
    } else if (this.awaitingFull) {
      return false;
    }
    this.term.write(frame.bytes, () => {
      this.invalidate();
      if (done) done();
    });
    return true;
  }

  clear() {
    this.awaitingFull = true;
    this.term.reset();
    this.invalidate();
  }

  setScale(scale) {
    if (Math.abs(scale - this.scale) > 0.001) {
      this.scale = scale;
      this.invalidate();
    }
  }

  // The picture's size at the current scale, in CSS pixels.
  size() {
    return {width: Math.ceil(this.term.cols * this.cell.width * this.scale),
      height: Math.ceil(this.term.rows * this.cell.height * this.scale)};
  }

  cursor() {
    const buffer = this.term.buffer.active;
    return {x: buffer.cursorX, y: buffer.cursorY, hidden: this.cursorHidden()};
  }

  cursorHidden() {
    const core = this.term._core;
    return !!(core && core.coreService && core.coreService.isCursorHidden);
  }

  invalidate() {
    this.dirty = true;
    if (this.frame || typeof requestAnimationFrame !== 'function') return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      this.paint();
    });
  }

  dispose() {
    if (this.frame && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(this.frame);
    this.frame = 0;
    this.canvas = null;
    this.term.dispose();
  }

  // LINKS-1: the link under one cell, or null. An OSC 8 hyperlink first
  // (codex marks its web addresses that way and Herdr's observe passes it
  // through); otherwise the row's text - joined with the rows the terminal
  // wrapped it onto - read with the shared detection rules, where a path is
  // only ever plain text.
  linkAt(col, row) {
    const buffer = this.term.buffer.active;
    if (!Number.isInteger(col) || !Number.isInteger(row) || row < 0 || row >= this.term.rows || col < 0) return null;
    const y = buffer.viewportY + row;
    const line = buffer.getLine(y);
    if (!line) return null;
    const cell = line.getCell(col);
    const id = cell && cell.extended ? cell.extended.urlId : 0;
    if (id) {
      try {
        const service = this.term._core && this.term._core._oscLinkService;
        const data = service && service.getLinkData(id);
        if (data && typeof data.uri === 'string' && /^https?:\/\//i.test(data.uri)) return {kind: 'url', text: data.uri};
      } catch {
        // an internal that moved: fall back to the text
      }
    }
    let first = y;
    while (first > 0 && buffer.getLine(first) && buffer.getLine(first).isWrapped) first--;
    let last = y;
    while (buffer.getLine(last + 1) && buffer.getLine(last + 1).isWrapped) last++;
    let text = '';
    let offset = 0;
    for (let n = first; n <= last; n++) {
      const segment = buffer.getLine(n).translateToString(n === last);
      if (n === y) offset = text.length + Math.min(col, segment.length);
      text += segment;
    }
    const hit = detect(text, 'mirror').find((link) => offset >= link.start && offset < link.end);
    return hit ? {kind: hit.kind, text: hit.text} : null;
  }

  // HERDR-8: the screen as cells for `lib/wrap.js`: what is drawn, in what
  // colours (the palette's, resolved here), and the OSC 8 link on it.
  // REL2-POLISH-2: `wrapFlags` - the bytes were text the terminal laid out
  // itself, so xterm's `isWrapped` is real and goes along. Herdr's frames
  // place every row, so for them it is never set and is left out.
  grid({wrapFlags = false} = {}) {
    const buffer = this.term.buffer.active;
    const palette = this.palette;
    const cell = buffer.getNullCell();
    const cursor = this.cursor();
    const rows = [];
    for (let y = 0; y < this.term.rows; y++) {
      const line = buffer.getLine(buffer.viewportY + y);
      const row = [];
      for (let x = 0; x < this.term.cols; x++) {
        if (!line || !line.getCell(x, cell)) {
          row.push({text: ' ', style: null});
          continue;
        }
        if (cell.getWidth() === 0) {
          row.push({text: '', style: null});
          continue;
        }
        const chars = cell.getChars() || ' ';
        const fg = cell.isFgDefault() ? null : cell.isFgRGB() ? rgbOf(cell.getFgColor()) : color256(cell.getFgColor(), palette);
        const bg = cell.isBgDefault() ? null : cell.isBgRGB() ? rgbOf(cell.getBgColor()) : color256(cell.getBgColor(), palette);
        const style = {fg, bg, bold: !!cell.isBold(), dim: !!cell.isDim(), italic: !!cell.isItalic(),
          underline: !!cell.isUnderline(), inverse: !!cell.isInverse(), strike: !!cell.isStrikethrough()};
        const plain = !fg && !bg && !style.bold && !style.dim && !style.italic && !style.underline && !style.inverse && !style.strike;
        let link = null;
        const id = cell.extended ? cell.extended.urlId : 0;
        if (id) {
          try {
            const service = this.term._core && this.term._core._oscLinkService;
            const data = service && service.getLinkData(id);
            if (data && typeof data.uri === 'string' && /^https?:\/\//i.test(data.uri)) link = data.uri;
          } catch {
            // an internal that moved: the text is still read for links
          }
        }
        row.push({text: chars, style: plain ? null : style, link});
      }
      rows.push(row);
    }
    const grid = {cols: this.term.cols, rows, cursor: cursor.hidden ? null : {x: cursor.x, y: cursor.y}};
    if (wrapFlags) {
      grid.wrapped = rows.map((_, y) => {
        const line = buffer.getLine(buffer.viewportY + y);
        return !!(line && line.isWrapped);
      });
    }
    return grid;
  }

  // The screen as text, one string a row (tests, and nothing else).
  text() {
    const buffer = this.term.buffer.active;
    const out = [];
    for (let y = 0; y < this.term.rows; y++) {
      const line = buffer.getLine(buffer.viewportY + y);
      out.push(line ? line.translateToString(true) : '');
    }
    return out;
  }

  // AGENT-UI-2: a canvas does not wait for a web font: the first paint with
  // an icon asks for its file and paints again once it is there.
  loadSymbols(parts) {
    if (!parts.size || typeof document === 'undefined' || !document.fonts || !document.fonts.load) return;
    this._symbols = this._symbols || new Set();
    for (const part of parts) {
      if (this._symbols.has(part)) continue;
      this._symbols.add(part);
      const sample = part === 'spua' ? String.fromCodePoint(0xF0001) : '\uE0A0';
      document.fonts.load(BASE_FONT + 'px ' + SYMBOLS_FAMILY, sample).then(() => this.invalidate(), () => {});
    }
  }

  paint() {
    const canvas = this.canvas;
    if (!canvas || !this.dirty) return;
    this.dirty = false;
    const ctx = canvas.getContext && canvas.getContext('2d');
    if (!ctx) return;
    const ratio = (typeof devicePixelRatio === 'number' && devicePixelRatio) || 1;
    const {width, height} = this.size();
    const pixelsW = Math.max(1, Math.round(width * ratio));
    const pixelsH = Math.max(1, Math.round(height * ratio));
    if (canvas.width !== pixelsW) canvas.width = pixelsW;
    if (canvas.height !== pixelsH) canvas.height = pixelsH;
    canvas.style.width = width + 'px';
    canvas.style.height = height + 'px';
    const k = this.scale * ratio;
    ctx.setTransform(k, 0, 0, k, 0, 0);
    const palette = this.palette;
    const cw = this.cell.width;
    const ch = this.cell.height;
    ctx.fillStyle = palette.background;
    ctx.fillRect(0, 0, this.term.cols * cw, this.term.rows * ch);
    ctx.textBaseline = 'middle';
    const buffer = this.term.buffer.active;
    const cursor = this.cursor();
    const cell = this._cell || (this._cell = buffer.getNullCell());
    const fonts = {};
    const font = (bold, italic) => {
      const key = (bold ? 'b' : '') + (italic ? 'i' : '');
      if (!fonts[key]) fonts[key] = (italic ? 'italic ' : '') + (bold ? '600 ' : '400 ') + BASE_FONT + 'px ' + FONT_FAMILY;
      return fonts[key];
    };
    let current = '';
    const wanted = new Set();
    const glyphs = [];
    for (let y = 0; y < this.term.rows; y++) {
      const line = buffer.getLine(buffer.viewportY + y);
      if (!line) continue;
      // The row's backgrounds first, then its glyphs: an icon may reach into
      // the blank cell after it (AGENT-UI-2), and that cell's background must
      // not be painted over it afterwards.
      glyphs.length = 0;
      for (let x = 0; x < this.term.cols; x++) {
        if (!line.getCell(x, cell)) continue;
        const width = cell.getWidth();
        if (width === 0) continue; // the second half of a wide glyph
        let fg = cell.isFgDefault() ? palette.foreground : cell.isFgRGB() ? rgbOf(cell.getFgColor())
          : color256(cell.getFgColor(), palette);
        let bg = cell.isBgDefault() ? null : cell.isBgRGB() ? rgbOf(cell.getBgColor()) : color256(cell.getBgColor(), palette);
        if (cell.isInverse()) {
          const swap = bg || palette.background;
          bg = fg;
          fg = swap;
        }
        const isCursor = !cursor.hidden && x === cursor.x && y === cursor.y;
        if (isCursor) {
          bg = palette.cursor;
          fg = palette.background;
        }
        const span = Math.max(1, width);
        if (bg) {
          ctx.fillStyle = bg;
          ctx.fillRect(x * cw, y * ch, span * cw + 0.5, ch + 0.5);
        }
        const chars = cell.getChars();
        const blank = !chars || chars === ' ' || cell.isInvisible();
        glyphs.push({x, span, fg, chars: blank ? '' : chars, bg, bold: cell.isBold(), italic: cell.isItalic(), dim: cell.isDim(),
          underline: cell.isUnderline(), strike: cell.isStrikethrough()});
      }
      for (let i = 0; i < glyphs.length; i++) {
        const g = glyphs[i];
        if (!g.chars && !g.underline && !g.strike) continue;
        const left = g.x * cw;
        const top = y * ch;
        ctx.globalAlpha = g.dim ? 0.6 : 1;
        ctx.fillStyle = g.fg;
        const codepoint = g.chars ? g.chars.codePointAt(0) : 0;
        const single = g.chars.length === String.fromCodePoint(codepoint || 32).length;
        const rect = single ? blockRect(codepoint) : null;
        const shape = single && !rect ? powerlineShape(codepoint) : null;
        const part = single && !rect && !shape ? symbolsPart(codepoint) : null;
        if (rect) {
          ctx.fillRect(left + rect[0] * g.span * cw, top + rect[1] * ch, (rect[2] - rect[0]) * g.span * cw + 0.5,
            (rect[3] - rect[1]) * ch + 0.5);
        } else if (shape) {
          drawShape(ctx, shape, left, top, g.span * cw, ch);
        } else if (part) {
          // A Nerd Font icon is a square em: drawn whole, at the cell's
          // width - or two cells' when the next one is blank on the same
          // background, as terminals do - never squeezed sideways.
          wanted.add(part);
          const next = glyphs[i + 1];
          const room = (next && next.x === g.x + g.span && !next.chars && next.bg === g.bg ? 2 : 1) * g.span * cw;
          const size = Math.min(BASE_FONT, room);
          const style = (g.bold ? '600 ' : '400 ') + size + 'px ' + FONT_FAMILY;
          if (style !== current) {
            ctx.font = style;
            current = style;
          }
          ctx.fillText(g.chars, left + (room - size) / 2, top + ch / 2 + 0.5, room);
        } else if (g.chars) {
          const style = font(g.bold, g.italic);
          if (style !== current) {
            ctx.font = style;
            current = style;
          }
          ctx.fillText(g.chars, left, top + ch / 2 + 0.5, g.span * cw);
        }
        if (g.underline) ctx.fillRect(left, top + ch - 1.5, g.span * cw, 1);
        if (g.strike) ctx.fillRect(left, top + ch / 2, g.span * cw, 1);
        ctx.globalAlpha = 1;
      }
    }
    this.loadSymbols(wanted);
    if (this.onPainted) this.onPainted();
  }
}
