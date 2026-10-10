// LINKS-1: links in what agents send - the shared detection and rewrite
// vectors (core's contracts/fixtures/links-detect.json, which the App runs
// too), the markdown subset, what a tap decides, the mirror's links (OSC 8
// and plain text), the tool rows' paths and the words in both languages.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runnerImport} from 'vite';
import * as L from '../src/lib/links.js';
import {parseBlocks, parseInline} from '../src/lib/markdown.js';
import {decide} from '../src/lib/useHostOpen.js';
import {Mirror} from '../src/lib/term.js';
import * as A from '../src/lib/agent.js';
import {en, zh} from '../src/lib/i18n.js';

const fixtures = new URL('../../contracts/fixtures/', import.meta.url);
const vectors = JSON.parse(readFileSync(new URL('links-detect.json', fixtures), 'utf8'));
const roads = JSON.parse(readFileSync(new URL('host-roads.json', fixtures), 'utf8'));
const strip = (links) => links.map(({kind, text}) => ({kind, text}));

test('every shared detection vector, mirror and prose', () => {
  assert.deepEqual(L.KNOWN_EXTENSIONS, vectors.known_extensions);
  for (const c of vectors.mirror) assert.deepEqual(strip(L.detect(c.text, 'mirror')), c.links, c.text);
  for (const c of vectors.prose) assert.deepEqual(strip(L.detect(c.text, 'prose')), c.links, c.text);
});

test('host-local addresses and their rewrite to this device\'s road', () => {
  for (const c of vectors.host_local) assert.equal(L.hostLocal(c.url, roads), c.host_local, c.url);
  for (const c of vectors.rewrite) assert.equal(L.rewrite(c.url, c.road, roads), c.expected, c.url);
  // A browser behind `tailscale serve` knows only its own hostname.
  assert.deepEqual(L.roadOf('omarchy-fixture.tail0000.ts.net', {endpoints: []}),
    {host: 'omarchy-fixture.tail0000.ts.net', kind: 'tailnet'});
  assert.equal(L.rewrite('http://localhost:5173/', L.roadOf('omarchy-fixture.tail0000.ts.net', roads), roads),
    'http://100.64.0.35:5173/');
});

test('a tap decides at once: here, rewritten, or on the computer', () => {
  const host = 'omarchy-fixture.tail0000.ts.net';
  assert.deepEqual(decide({kind: 'url', target: 'https://example.com/'}, roads, host), {action: 'here'});
  assert.deepEqual(decide({kind: 'url', target: 'http://localhost:8000/x'}, roads, host),
    {action: 'rewritten', address: 'http://100.64.0.35:8000/x'});
  assert.deepEqual(decide({kind: 'path', target: '~/l1test/a.md'}, roads, host), {action: 'computer'});
  // Before the roads arrive, localhost is still recognised.
  assert.equal(decide({kind: 'url', target: 'http://127.0.0.1:3000/'}, {endpoints: []}, '192.168.1.10').action, 'rewritten');
});

test('markdown: the blocks codex writes, and links of the right kind', () => {
  const snapshot = JSON.parse(readFileSync(new URL('agent-chat-snapshot-links.json', fixtures), 'utf8'));
  const reply = snapshot.rows.find((row) => row.id === 'item-fixture-reply');
  const blocks = parseBlocks(reply.text);
  assert.deepEqual(blocks.map((b) => b.type), ['heading', 'list', 'paragraph', 'code']);
  assert.equal(blocks[0].level, 2);
  assert.equal(blocks[3].text, 'python3 chart.py');
  const [chart, script] = blocks[1].items.map((item) => item[0].inline);
  assert.deepEqual(chart.find((n) => n.type === 'image'), {type: 'image', alt: 'Bar chart',
    target: '/home/user/l1test/chart.png', kind: 'path'});
  assert.equal(script.find((n) => n.type === 'link').kind, 'path');
  const words = blocks[2].inline;
  assert.deepEqual(words.find((n) => n.type === 'link'), {type: 'link', kind: 'url', target: 'http://localhost:8000/',
    children: [{type: 'text', text: 'http://localhost:8000/'}]});
  assert.deepEqual(words.find((n) => n.type === 'code'), {type: 'code', text: '~/l1test', link: {kind: 'path', target: '~/l1test'}});
  // Inline: bold, italic, code that is a path:line, a web link, nothing for snake_case.
  const line = parseInline('**Done** in *two* steps: `/h/a.py:12`, [docs](https://x.dev/a_(b)) and foo_bar_baz');
  assert.deepEqual(line.map((n) => n.type), ['strong', 'text', 'em', 'text', 'code', 'text', 'link', 'text']);
  assert.deepEqual(line[4].link, {kind: 'path', target: '/h/a.py:12'});
  assert.equal(line[6].target, 'https://x.dev/a_(b)');
  assert.equal(line[7].text, ' and foo_bar_baz');
  assert.deepEqual(parseBlocks('1. one\n2. two\n\n> quoted').map((b) => b.type), ['list', 'quote']);
});

const {module: render} = await runnerImport(new URL('./render.jsx', import.meta.url).pathname,
  {root: new URL('..', import.meta.url).pathname, configFile: false, logLevel: 'silent'});

test('markdown renders as elements, never as markup from the reply', () => {
  const html = render.renderMarkdown('## Hi\n\n<script>alert(1)</script> and <b>x</b>\n\n- [a](/h/a.md)\n- https://example.com\n\n'
    + '![chart](/h/c.png)\n\n```\n<i>kept</i>\n```');
  assert.match(html, /<h2 class="md__h md__h2">Hi<\/h2>/);
  assert.doesNotMatch(html, /<script>|<b>x<\/b>|<i>kept/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html, /<button type="button" class="md__file" data-testid="agent-link-path" title="\/h\/a.md">a<\/button>/);
  assert.match(html, /<a class="md__a" href="https:\/\/example.com" target="_blank" rel="noopener noreferrer"/);
  assert.match(html, /<span>chart<\/span><button[^>]*title="\/h\/c.png">c.png<\/button>/);
  assert.match(html, /<pre class="md__pre"><code>&lt;i&gt;kept&lt;\/i&gt;<\/code><\/pre>/);
});

test('the mirror: an OSC 8 address, and paths read from the text', async () => {
  const mirror = new Mirror({cols: 60, rows: 4});
  const frame = (text) => new Promise((resolve) => mirror.feed({full: true, width: 60, height: 4,
    bytes: new TextEncoder().encode(text)}, resolve));
  await frame('see \x1b]8;;https://developers.openai.com/codex\x07the docs\x1b]8;;\x07 or ~/l1test/report.md:12 x\r\n'
    + 'Download tui-out.csv now');
  assert.deepEqual(mirror.linkAt(5, 0), {kind: 'url', text: 'https://developers.openai.com/codex'});
  assert.deepEqual(mirror.linkAt(20, 0), {kind: 'path', text: '~/l1test/report.md:12'});
  assert.equal(mirror.linkAt(1, 0), null);
  assert.deepEqual(mirror.linkAt(12, 1), {kind: 'path', text: 'tui-out.csv'});
  // A path the terminal wrapped onto the next row is one path.
  const narrow = new Mirror({cols: 20, rows: 4});
  await new Promise((resolve) => narrow.feed({full: true, width: 20, height: 4,
    bytes: new TextEncoder().encode('at /home/user/l1test/deep/file.txt ok')}, resolve));
  assert.deepEqual(narrow.linkAt(2, 1), {kind: 'path', text: '/home/user/l1test/deep/file.txt'});
  mirror.dispose();
  narrow.dispose();
});

test('tool rows keep the paths core gave them', () => {
  const snapshot = JSON.parse(readFileSync(new URL('agent-chat-snapshot-links.json', fixtures), 'utf8'));
  const model = A.restore(A.initial(), snapshot);
  assert.deepEqual(model.rows.find((r) => r.itemID === 'item-fixture-view').paths, ['/home/user/l1test/chart.png']);
  assert.deepEqual(model.rows.find((r) => r.itemID === 'item-fixture-change').paths, ['/home/user/l1test/chart.py']);
  assert.equal(model.rows.find((r) => r.itemID === 'item-fixture-ask').paths, undefined);
  assert.equal(model.rows.some((r) => r.itemID === 'item-fixture-blank'), false);
});

test('the words, both languages, for every refusal core can give', () => {
  const schema = JSON.parse(readFileSync(new URL('../../contracts/host-open.schema.json', import.meta.url), 'utf8'));
  const codes = [...schema.description.matchAll(/`\d{3} (open_[a-z_]+)`/g)].map((m) => m[1]);
  assert.ok(codes.length >= 10);
  for (const table of [en, zh]) {
    for (const code of codes) assert.ok(table.links.refused[code], code);
    for (const key of ['opening', 'opened', 'openedAt', 'watch', 'here', 'hereHint', 'onComputer']) assert.ok(table.links[key], key);
  }
  const opened = render.renderLinkNotice({kind: 'opened', name: 'report.md', line: 12}, 'en', '0'.repeat(32));
  assert.match(opened, /Opened report.md on the computer, at line 12\./);
  assert.match(opened, /href="https:\/\/omodachi.app\/open\/remote\?host=0{32}&amp;from=odomo"/);
  const here = render.renderLinkNotice({kind: 'here', address: 'http://100.64.0.35:8000/', original: 'http://localhost:8000/'}, 'zh-Hans');
  assert.match(here, /已在新标签页打开 http:\/\/100.64.0.35:8000\//);
  assert.match(here, /在电脑上打开/);
  assert.match(render.renderLinkNotice({kind: 'refused', code: 'open_denied'}), /private place/);
  assert.match(render.renderLinkNotice({kind: 'refused', code: 'mystery'}), /\(mystery\)/);
});
