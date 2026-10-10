// LINKS-1 (ATTACH-2 §2): the markdown agents write, as data - never HTML.
//
// Odomo used to show an agent's reply as plain text, so `## Status`, the
// backticks and `![chart](/home/...)` were all on screen as typed. This reads
// the subset codex actually writes into blocks and inline spans that
// `Markdown.jsx` turns into React elements. Nothing here is ever parsed as HTML:
// a `<script>` in a reply is text, and the CSP stays as it is.
//
// Blocks: headings, paragraphs, bullet and numbered lists, block quotes,
// fenced code (kept as one block, never re-flowed) and rules. Inline: code
// spans, **bold**, *italic*, [links](target), <autolinks>, images (shown as
// their alt text plus a link to the file - nothing is fetched), and bare web
// addresses and paths found in the words (`links.js`).

import {detect, linkOf, targetKind} from './links.js';

const FENCE = /^ {0,3}(`{3,}|~{3,})\s*([\w+.-]*)\s*$/;
const HEADING = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^ {0,3}([-*_])(\s*\1){2,}\s*$/;
const QUOTE = /^ {0,3}>\s?(.*)$/;
const BULLET = /^(\s*)([-*+])\s+(.*)$/;
const NUMBER = /^(\s*)(\d{1,9})[.)]\s+(.*)$/;

export function parseBlocks(source) {
  const lines = String(source || '').replace(/\r\n?/g, '\n').split('\n');
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    const fence = FENCE.exec(line);
    if (fence) {
      const marker = fence[1];
      const body = [];
      i++;
      while (i < lines.length && !(lines[i].trim().startsWith(marker[0].repeat(marker.length))
        && /^ {0,3}[`~]+\s*$/.test(lines[i]))) {
        body.push(lines[i]);
        i++;
      }
      i++; // the closing fence (or the end of the text)
      blocks.push({type: 'code', lang: fence[2] || '', text: body.join('\n')});
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      blocks.push({type: 'heading', level: heading[1].length, inline: parseInline(heading[2])});
      i++;
      continue;
    }
    if (RULE.test(line)) {
      blocks.push({type: 'rule'});
      i++;
      continue;
    }
    if (QUOTE.test(line)) {
      const body = [];
      while (i < lines.length && QUOTE.test(lines[i])) {
        body.push(QUOTE.exec(lines[i])[1]);
        i++;
      }
      blocks.push({type: 'quote', blocks: parseBlocks(body.join('\n'))});
      continue;
    }
    const bullet = BULLET.exec(line);
    const number = NUMBER.exec(line);
    if (bullet || number) {
      const ordered = !bullet;
      const pattern = ordered ? NUMBER : BULLET;
      const indent = (bullet || number)[1].length;
      const items = [];
      const start = ordered ? Number(number[2]) : null;
      while (i < lines.length) {
        const match = pattern.exec(lines[i]);
        if (match && match[1].length === indent) {
          items.push([match[3]]);
          i++;
          continue;
        }
        // A continuation: indented further than the marker, or a nested list.
        if (items.length && lines[i].trim() && /^\s+/.test(lines[i]) && (lines[i].match(/^\s*/)[0].length > indent)) {
          items[items.length - 1].push(lines[i].slice(Math.min(lines[i].match(/^\s*/)[0].length, indent + 2)));
          i++;
          continue;
        }
        if (!lines[i].trim() && i + 1 < lines.length && pattern.exec(lines[i + 1])
          && pattern.exec(lines[i + 1])[1].length === indent) {
          i++;
          continue;
        }
        break;
      }
      blocks.push({type: 'list', ordered, start, items: items.map((body) => parseBlocks(body.join('\n')))});
      continue;
    }
    const body = [];
    while (i < lines.length && lines[i].trim() && !FENCE.test(lines[i]) && !HEADING.test(lines[i])
      && !QUOTE.test(lines[i]) && !BULLET.test(lines[i]) && !NUMBER.test(lines[i]) && !RULE.test(lines[i])) {
      body.push(lines[i].trim());
      i++;
    }
    blocks.push({type: 'paragraph', inline: parseInline(body.join('\n'))});
  }
  return blocks;
}

// The closing `)` of a link target, allowing balanced parentheses inside.
function targetEnd(text, open) {
  let depth = 0;
  for (let j = open; j < text.length; j++) {
    const c = text[j];
    if (c === '\\') { j++; continue; }
    if (c === '(') depth++;
    else if (c === ')') {
      depth--;
      if (depth === 0) return j;
    } else if (c === '\n') return -1;
  }
  return -1;
}

// The `]` that closes a link's text, allowing nested brackets.
function labelEnd(text, open) {
  let depth = 0;
  for (let j = open; j < text.length; j++) {
    const c = text[j];
    if (c === '\\') { j++; continue; }
    if (c === '[') depth++;
    else if (c === ']') {
      depth--;
      if (depth === 0) return j;
    }
  }
  return -1;
}

function cleanTarget(raw) {
  let value = raw.trim();
  // `[x](<path with spaces>)` and `[x](path "title")`
  if (value.startsWith('<') && value.includes('>')) return value.slice(1, value.indexOf('>'));
  const title = value.match(/^(\S+)\s+["'(].*["')]$/);
  if (title) value = title[1];
  return value;
}

function words(text, out) {
  // Bare addresses and paths in the words become links (prose rules).
  let at = 0;
  for (const link of detect(text, 'prose')) {
    if (link.start > at) out.push({type: 'text', text: text.slice(at, link.start)});
    out.push({type: 'link', kind: link.kind, target: link.text, children: [{type: 'text', text: link.text}]});
    at = link.end;
  }
  if (at < text.length) out.push({type: 'text', text: text.slice(at)});
}

export function parseInline(source) {
  const text = String(source || '');
  const out = [];
  let plain = '';
  const flush = () => {
    if (plain) words(plain, out);
    plain = '';
  };
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (c === '\\' && i + 1 < text.length && /[\\`*_[\]()#+\-.!<>~|]/.test(text[i + 1])) {
      plain += text[i + 1];
      i += 2;
      continue;
    }
    if (c === '`') {
      const run = text.slice(i).match(/^`+/)[0];
      const close = text.indexOf(run, i + run.length);
      if (close > 0) {
        flush();
        const code = text.slice(i + run.length, close).replace(/^ (.*) $/, '$1');
        const link = linkOf(code, 'prose') || linkOf(code, 'mirror');
        out.push(link ? {type: 'code', text: code, link: {kind: link.kind, target: link.text}} : {type: 'code', text: code});
        i = close + run.length;
        continue;
      }
    }
    if ((c === '!' && text[i + 1] === '[') || c === '[') {
      const open = c === '!' ? i + 1 : i;
      const close = labelEnd(text, open);
      if (close > 0 && text[close + 1] === '(') {
        const end = targetEnd(text, close + 1);
        if (end > 0) {
          flush();
          const label = text.slice(open + 1, close);
          const target = cleanTarget(text.slice(close + 2, end));
          if (c === '!') out.push({type: 'image', alt: label, target, kind: targetKind(target)});
          else out.push({type: 'link', kind: targetKind(target), target, children: parseInline(label)});
          i = end + 1;
          continue;
        }
      }
    }
    if (c === '<') {
      const close = text.indexOf('>', i);
      const inner = close > 0 ? text.slice(i + 1, close) : '';
      if (close > 0 && /^(https?:\/\/|omodachi:)[^\s<>]+$/i.test(inner)) {
        flush();
        out.push({type: 'link', kind: targetKind(inner), target: inner, children: [{type: 'text', text: inner}]});
        i = close + 1;
        continue;
      }
    }
    if ((c === '*' || c === '_') && text[i + 1] === c) {
      const close = text.indexOf(c + c, i + 2);
      if (close > i + 2) {
        flush();
        out.push({type: 'strong', children: parseInline(text.slice(i + 2, close))});
        i = close + 2;
        continue;
      }
    }
    if ((c === '*' || c === '_') && text[i + 1] && !/\s/.test(text[i + 1])
      && (c === '*' || i === 0 || /[\s(\[]/.test(text[i - 1]))) {
      let close = i + 1;
      while ((close = text.indexOf(c, close)) > 0 && (text[close + 1] === c || /\s/.test(text[close - 1]))) close++;
      if (close > i + 1 && (c === '*' || !/\w/.test(text[close + 1] || ''))) {
        flush();
        out.push({type: 'em', children: parseInline(text.slice(i + 1, close))});
        i = close + 1;
        continue;
      }
    }
    if (c === '\n') {
      flush();
      out.push({type: 'break'});
      i++;
      continue;
    }
    plain += c;
    i++;
  }
  flush();
  return out;
}
