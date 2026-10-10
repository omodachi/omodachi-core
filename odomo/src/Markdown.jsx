import React from 'react';
import Icon from './Icon.jsx';
import {parseBlocks} from './lib/markdown.js';
import {splitHighlight} from './lib/agentSteps.js';

// AGENT-COLLIE-1: the words find is looking for, marked where they occur. Set
// for the length of one synchronous `Markdown` call and read by `marked`.
let MARK = '';

export function marked(text, query = MARK) {
  if (!query) return text;
  return splitHighlight(text, query).map((piece, i) => (piece.hit
    ? <mark key={'m' + i} className="afind-mark">{piece.text}</mark> : piece.text));
}

// LINKS-1: an agent's reply, drawn from `markdown.js`'s blocks with React
// elements only (a reply is never parsed as HTML, so nothing in it can become markup).
//
// `onLink({kind, target})` decides what a tap does: a web address opens in a
// new tab (rewritten when it points at the computer itself), a path asks to
// open on the computer. A web address is still a real <a href>, so the
// browser's own long-press and middle-click keep working.

function linkElement(node, key, onLink, children) {
  if (node.kind === 'url') {
    return (
      <a key={key} className="md__a" href={node.target} target="_blank" rel="noopener noreferrer"
        data-testid="agent-link-url" onClick={(event) => {
          if (onLink && onLink({kind: 'url', target: node.target}) === false) event.preventDefault();
        }}>{children}</a>
    );
  }
  if (node.kind === 'path') {
    return (
      <button key={key} type="button" className="md__file" data-testid="agent-link-path" title={node.target}
        onClick={() => onLink && onLink({kind: 'path', target: node.target})}>{children}</button>
    );
  }
  if (node.kind === 'app') {
    return <a key={key} className="md__a" href={node.target} data-testid="agent-link-app">{children}</a>;
  }
  return <span key={key}>{children}</span>;
}

function fileName(target) {
  const value = String(target || '').replace(/[:#][^/]*$/, '');
  return value.split('/').filter(Boolean).pop() || target;
}

function inline(nodes, onLink, prefix = 'i') {
  return nodes.map((node, index) => {
    const key = prefix + index;
    switch (node.type) {
      case 'text': return <React.Fragment key={key}>{marked(node.text)}</React.Fragment>;
      case 'break': return <br key={key} />;
      case 'strong': return <strong key={key}>{inline(node.children, onLink, key)}</strong>;
      case 'em': return <em key={key}>{inline(node.children, onLink, key)}</em>;
      case 'code': {
        const code = <code key={key} className="md__code">{marked(node.text)}</code>;
        return node.link ? linkElement({kind: node.link.kind, target: node.link.target}, key, onLink, code) : code;
      }
      case 'link': return linkElement(node, key, onLink, inline(node.children, onLink, key));
      case 'image':
        // ATTACH-2: never fetched. The alt text says what it is; the file
        // opens on the computer like any other.
        return (
          <span key={key} className="md__img">
            <Icon name="image" size={14} />
            <span>{node.alt || fileName(node.target)}</span>
            {node.kind === 'path' || node.kind === 'url'
              ? linkElement(node, key + 'l', onLink, fileName(node.target)) : null}
          </span>
        );
      default: return null;
    }
  });
}

function block(node, onLink, key) {
  switch (node.type) {
    case 'heading': {
      const Tag = 'h' + Math.min(6, Math.max(1, node.level));
      return <Tag key={key} className={'md__h md__h' + node.level}>{inline(node.inline, onLink, key)}</Tag>;
    }
    case 'paragraph': return <p key={key} className="md__p">{inline(node.inline, onLink, key)}</p>;
    case 'code': return <pre key={key} className="md__pre" data-lang={node.lang || undefined}><code>{marked(node.text)}</code></pre>;
    case 'rule': return <hr key={key} className="md__hr" />;
    case 'quote': return <blockquote key={key} className="md__q">{node.blocks.map((b, i) => block(b, onLink, key + 'q' + i))}</blockquote>;
    case 'list': {
      const items = node.items.map((blocks, i) => (
        <li key={key + 'li' + i}>{blocks.map((b, j) => block(b, onLink, key + 'li' + i + 'b' + j))}</li>
      ));
      return node.ordered
        ? <ol key={key} className="md__ol" start={node.start && node.start !== 1 ? node.start : undefined}>{items}</ol>
        : <ul key={key} className="md__ul">{items}</ul>;
    }
    default: return null;
  }
}

export default function Markdown({text, onLink, children, mark = ''}) {
  const blocks = parseBlocks(text);
  MARK = mark || '';
  try {
    return (
      <div className="md" data-testid="agent-markdown">
        {blocks.map((node, i) => block(node, onLink, 'b' + i))}
        {children}
      </div>
    );
  } finally {
    MARK = '';
  }
}
