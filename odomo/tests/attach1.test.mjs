// ATTACH-1: what the paperclip offers and refuses, the chip grammar (markers
// in the draft, what Send carries), the upload call, the agent's echo of a
// message that carried files, and the words in both languages.
import test from 'node:test';
import assert from 'node:assert/strict';
import * as AT from '../src/lib/attachments.js';
import * as A from '../src/lib/agent.js';
import {HostClient, ApiError} from '../src/lib/api.js';
import {en, zh, fill} from '../src/lib/i18n.js';
import {readFileSync} from 'node:fs';

const file = (name, size = 10, type = '') => ({name, size, type});

test('the picker refuses before a byte goes: too big, SVG, anything not a picture, a PDF or text', () => {
  const cases = [
    [file('shot.png', 10, 'image/png'), null],
    [file('IMG_0001.HEIC', 10, 'image/heic'), null],
    [file('IMG_0002', 10, 'image/jpeg'), null], // a camera roll entry with no extension
    [file('paper.pdf', 10, 'application/pdf'), null],
    [file('main.py', 10, 'text/x-python'), null],
    [file('notes.md', 10, ''), null],
    [file('big.png', AT.MAX_BYTES + 1, 'image/png'), 'tooLarge'],
    [file('exact.png', AT.MAX_BYTES, 'image/png'), null],
    [file('drawing.svg', 10, 'image/svg+xml'), 'svg'],
    [file('drawing.svg', 10, ''), 'svg'],
    [file('bundle.zip', 10, 'application/zip'), 'badType'],
    [file('photo.avif', 10, 'image/avif'), 'badType'],
    [file('Makefile', 10, ''), 'badType'],
    [file('empty.txt', 0, 'text/plain'), 'empty'],
  ];
  for (const [f, expected] of cases) assert.equal(AT.rejectAttachment(f), expected, f.name);
  assert.equal(AT.MAX_BYTES, 25 * 1024 * 1024);
  // photos and files are two inputs: a photo library needs an image-only accept
  assert.ok(AT.PHOTO_ACCEPT.split(',').every((entry) => entry.startsWith('image/')));
  assert.ok(AT.FILE_ACCEPT.includes('.pdf') && AT.FILE_ACCEPT.includes('.py') && !AT.FILE_ACCEPT.includes('svg'));
});

test('a marker goes in at the caret, never welded to a word, and comes out with one space', () => {
  assert.deepEqual(AT.insertMarker('look at', null, '[Image #1]'), {text: 'look at [Image #1] ', caret: 19});
  assert.deepEqual(AT.insertMarker('', 0, '[File #1]'), {text: '[File #1] ', caret: 10});
  assert.equal(AT.insertMarker('ab', 1, '[Image #2]').text, 'a [Image #2] b');
  assert.equal(AT.removeMarker('see [Image #1] and [File #2] now', '[Image #1]'), 'see and [File #2] now');
  assert.equal(AT.removeMarker('[File #2]', '[File #2]'), '');
  assert.equal(AT.markerFor({n: 3, kind: 'image'}), '[Image #3]');
  assert.equal(AT.markerFor({n: 4, kind: 'file'}), '[File #4]');
  assert.equal(AT.markerMissing('hello', {n: 1, kind: 'image'}), true);
  assert.equal(AT.markerMissing('hello [Image #1]', {n: 1, kind: 'image'}), false);
  assert.equal(AT.shortName('a-very-long-file-name.py'), 'a-very-long-f…');
  assert.equal(AT.attachmentKind(file('x.heic')), 'image');
  assert.equal(AT.attachmentKind(file('x.pdf', 1, 'application/pdf')), 'file');
});

test('Send carries the uploaded chips, in order, with their markers - never one still uploading', () => {
  const chips = [{n: 1, kind: 'image', status: 'ready', id: 'a'.repeat(32)},
    {n: 2, kind: 'file', status: 'uploading', id: null},
    {n: 3, kind: 'file', status: 'ready', id: 'b'.repeat(32)}];
  assert.deepEqual(AT.sendRefs(chips), [{id: 'a'.repeat(32), marker: '[Image #1]'}, {id: 'b'.repeat(32), marker: '[File #3]'}]);
  assert.equal(AT.uploading(chips), true);
  assert.equal(AT.uploading([chips[0]]), false);
});

test('core\'s refusal codes become words', () => {
  assert.equal(AT.refusalKey('upload_too_large'), 'tooLarge');
  assert.equal(AT.refusalKey('upload_svg_refused'), 'svg');
  assert.equal(AT.refusalKey('upload_type_refused'), 'badType');
  assert.equal(AT.refusalKey('upload_not_text'), 'notText');
  assert.equal(AT.refusalKey('upload_heic_unconvertible'), 'heic');
  assert.equal(AT.refusalKey('attachment_gone'), 'gone');
  assert.equal(AT.refusalKey('host_unreachable'), 'failed');
  for (const table of [en, zh]) {
    for (const key of ['tooLarge', 'svg', 'badType', 'notText', 'heic', 'empty', 'gone', 'failed', 'tooMany', 'wait']) {
      assert.equal(typeof table.attach[key], 'string', key);
    }
  }
  assert.equal(fill(en.attach.tooLarge, 'big.png'), 'big.png is over 25 MB, too big to send.');
  assert.equal(fill(zh.attach.tooLarge, 'big.png'), 'big.png 超过 25 MB，太大了，发不了。');
  assert.equal(AT.uploadQuery('a.png', {kind: 'herdr', session: 'a1test', pane: 'w1:p1'}).pane, 'w1:p1');
  assert.deepEqual(AT.uploadQuery('a.png', {kind: 'agent'}), {name: 'a.png', target: 'agent'});
});

class FakeXHR {
  static last = null;
  constructor() { FakeXHR.last = this; this.headers = {}; this.upload = {}; }
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader(key, value) { this.headers[key] = value; }
  send(body) { this.body = body; }
  answer(status, value) {
    this.status = status;
    this.responseText = JSON.stringify(value);
    this.onload();
  }
}

test('the upload is the file as the body, with the credential, the name and target in the query, and its progress', async () => {
  const client = new HostClient({base: 'https://h.example/', token: 'tok'});
  const seen = [];
  const pending = client.upload({name: 'x'}, AT.uploadQuery('shot 1.png', {kind: 'agent'}), (f) => seen.push(f), FakeXHR);
  const xhr = FakeXHR.last;
  assert.equal(xhr.method, 'POST');
  const url = new URL(xhr.url);
  assert.equal(url.pathname, '/v1/uploads');
  assert.equal(url.searchParams.get('name'), 'shot 1.png');
  assert.equal(url.searchParams.get('target'), 'agent');
  assert.equal(xhr.headers.Authorization, 'Bearer tok');
  xhr.upload.onprogress({lengthComputable: true, loaded: 5, total: 10});
  xhr.answer(200, {id: 'a'.repeat(32), name: 'shot 1.png', kind: 'image'});
  assert.equal((await pending).kind, 'image');
  assert.deepEqual(seen, [0.5]);

  const refused = client.upload({name: 'y'}, {name: 'x.svg'}, () => {}, FakeXHR);
  FakeXHR.last.answer(415, {error: {code: 'upload_svg_refused', message: 'no'}});
  await assert.rejects(refused, (error) => error instanceof ApiError && error.code === 'upload_svg_refused');
  assert.throws(() => client.deleteUpload('../x'), ApiError);
});

test('sends carry attachments only when there are some', async () => {
  const bodies = [];
  const client = new HostClient({base: 'https://h.example/', token: 'tok', fetchImpl: async (url, init) => {
    bodies.push([new URL(url).pathname, JSON.parse(init.body)]);
    return {ok: true, json: async () => ({accepted: true, images: 'native'})};
  }});
  await client.chatSend('r1', 'hi', null, null, []);
  await client.chatSend('r2', 'see [Image #1]', null, null, [{id: 'a'.repeat(32), marker: '[Image #1]'}]);
  await client.herdrText('w1:p1', 'run [File #1]', true, [{id: 'b'.repeat(32), marker: '[File #1]'}]);
  assert.equal('attachments' in bodies[0][1], false);
  assert.deepEqual(bodies[1][1].attachments, [{id: 'a'.repeat(32), marker: '[Image #1]'}]);
  assert.equal(bodies[2][0], '/v1/herdr/panes/w1:p1/text');
  assert.equal(bodies[2][1].attachments.length, 1);
});

test('a message that carried files can be sent with no words, and its echo goes with the host\'s row', () => {
  const ready = {...A.initial(), phase: 'ready', identity: {hostID: 'h', agentID: 'default', provider: 'codex',
    providerSessionID: 't'}, turnID: 'turn-1'};
  assert.equal(A.canSend(ready), false);
  assert.equal(A.canSend(ready, true), true);
  const begun = A.beginSend({...ready, draft: 'what is [Image #1]'}, {count: 1, images: 1});
  assert.equal(begun.model.localEcho.attached, true);
  assert.equal(begun.model.localEcho.images, 1);
  const running = {...begun.model, phase: 'running'};
  const echoed = A.apply(running, {identity: ready.identity, sequence: 1, event: {type: 'message',
    value: {id: 'u1', kind: 'message', role: 'user', turnID: 'turn-1', text: 'what is [Image #1]', images: 1}}});
  assert.equal(echoed.localEcho, null);
  assert.equal(echoed.rows[0].images, 1);
});

test('the paperclip, a picture and a page are drawn on the same grid', () => {
  const icons = readFileSync(new URL('../src/Icon.jsx', import.meta.url), 'utf8');
  for (const name of ['attach', 'image', 'file']) assert.match(icons, new RegExp('\\n  ' + name + ": 'M"), name);
  assert.deepEqual(Object.keys(en.attach).sort(), Object.keys(zh.attach).sort());
});

test('the Agent composer draws the paperclip with its two inputs, in both languages', async () => {
  const {runnerImport} = await import('vite');
  const {module: render} = await runnerImport(new URL('./render.jsx', import.meta.url).pathname,
    {configFile: false, logLevel: 'silent'});
  for (const [lang, table] of [['en', en], ['zh-Hans', zh]]) {
    const html = render.renderAgent({hostId: 'h'}, lang);
    assert.match(html, /data-testid="agent-attach"/);
    assert.ok(html.includes('aria-label="' + table.attach.button + '"'), lang);
    assert.match(html, /data-testid="agent-attach-photo-input"[^>]*|accept="image\/png/);
    assert.ok(html.includes('accept="' + AT.FILE_ACCEPT + '"'));
  }
});
