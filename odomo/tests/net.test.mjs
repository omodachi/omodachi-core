// LITE-1a: the event stream's reconnect and heartbeat, the API client and
// pairing, with fake sockets, a fake clock and a fake fetch.
import test from 'node:test';
import assert from 'node:assert/strict';
import {EventStream, backoff, eventsUrl, HEARTBEAT_SILENCE} from '../src/lib/events.js';
import {ApiError, HostClient, apiBase, invokeConfirmed} from '../src/lib/api.js';
import {pollClaim, renewIfDue, requestPairing} from '../src/lib/pairing.js';

function fakeTimers() {
  let now = 0;
  let id = 0;
  const pending = new Map();
  return {
    now: () => now,
    timers: {
      setTimeout: (fn, ms) => { pending.set(++id, {fn, at: now + ms}); return id; },
      clearTimeout: (key) => pending.delete(key),
    },
    async advance(ms) {
      const until = now + ms;
      for (;;) {
        const next = [...pending.entries()].filter(([, t]) => t.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        pending.delete(next[0]);
        now = next[1].at;
        next[1].fn();
        await new Promise((r) => setImmediate(r));
      }
      now = until;
    },
  };
}

class FakeSocket {
  static all = [];
  constructor(url) { this.url = url; this.closed = null; FakeSocket.all.push(this); }
  close(code, reason) { this.closed = {code, reason}; }
  open() { this.onopen && this.onopen(); }
  send(message) { this.onmessage({data: JSON.stringify(message)}); }
  drop(code = 1006, reason = '') { this.onclose({code, reason}); }
}

const flush = () => new Promise((r) => setImmediate(r));

function stream(overrides = {}) {
  FakeSocket.all = [];
  const clock = fakeTimers();
  const seen = {messages: [], status: [], unpaired: [], tickets: 0};
  const s = new EventStream({
    ticket: async () => { seen.tickets += 1; return {ticket: 't'.repeat(43)}; },
    url: (ticket, resume) => eventsUrl('https://host.example.ts.net/', ticket, resume),
    WebSocketImpl: FakeSocket,
    onMessage: (m) => seen.messages.push(m),
    onStatus: (s) => seen.status.push(s),
    onUnpaired: (r) => seen.unpaired.push(r),
    timers: clock.timers, now: clock.now, random: () => 0.5,
    ...overrides,
  });
  return {s, clock, seen};
}

test('events URL: wss, heartbeat, resume, ticket last', () => {
  assert.equal(eventsUrl('https://h.ts.net/', 'T', null), 'wss://h.ts.net/v1/events?heartbeat=1&ticket=T');
  assert.equal(eventsUrl('https://h.ts.net/', 'T', {since: 7, instance_id: 'i'}),
    'wss://h.ts.net/v1/events?heartbeat=1&since=7&instance_id=i&ticket=T');
  assert.equal(eventsUrl('http://127.0.0.1:8099/', 'T', null).slice(0, 5), 'ws://');
});

test('backoff doubles to a 30 s cap', () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 9].map((n) => backoff(n, () => 0.5)), [1000, 2000, 4000, 8000, 16000, 30000, 30000]);
});

test('a quiet socket is dead: silence closes it and the stream resumes from its cursor', async () => {
  const {s, clock, seen} = stream();
  s.start();
  await flush();
  const first = FakeSocket.all[0];
  first.open();
  first.send({type: 'snapshot', instance_id: 'inst', cursor: 5, state: {}});
  first.send({event: {seq: 6, type: 'notification.posted', payload: {id: 'n'}}, instance_id: 'inst', after_cursor: 5});
  assert.equal(seen.status.at(-1), 'connected');
  // heartbeats keep it alive
  for (let i = 0; i < 5; i++) {
    await clock.advance(15000);
    first.send({type: 'heartbeat', instance_id: 'inst'});
  }
  assert.equal(first.closed, null);
  // then nothing for 40 s
  await clock.advance(HEARTBEAT_SILENCE + 1);
  assert.equal(first.closed.code, 4000);
  assert.equal(seen.status.at(-1), 'connecting');
  await clock.advance(1000);
  await flush();
  const second = FakeSocket.all[1];
  assert.ok(second, 'reconnected');
  assert.match(second.url, /since=6&instance_id=inst&ticket=/);
  assert.equal(seen.tickets, 2, 'a fresh ticket per socket');
  s.stop();
});

test('a revoked credential stops the stream and unpairs, from the close frame or the ticket', async () => {
  let { s, seen } = stream();
  s.start();
  await flush();
  FakeSocket.all[0].drop(1008, 'credential revoked or expired: credential_revoked');
  assert.deepEqual(seen.unpaired, ['credential_revoked']);
  ({s, seen} = stream({ticket: async () => { throw new ApiError(401, 'permission_denied', 'x', {reason: 'credential_expired'}); }}));
  s.start();
  await flush();
  assert.deepEqual(seen.unpaired, ['credential_expired']);
  assert.equal(FakeSocket.all.length, 0);
});

test('an unreachable host backs off, goes offline, and kick() retries at once', async () => {
  let fail = true;
  const {s, clock, seen} = stream({ticket: async () => { if (fail) throw new ApiError(0, 'host_unreachable'); return {ticket: 'x'}; }});
  s.start();
  await flush();
  await clock.advance(1000);
  await clock.advance(2000);
  await clock.advance(4000);
  assert.equal(seen.status.at(-1), 'offline');
  fail = false;
  s.kick();
  await flush();
  assert.equal(FakeSocket.all.length, 1);
  FakeSocket.all[0].send({type: 'ready', instance_id: 'i', cursor: 1});
  assert.equal(seen.status.at(-1), 'connected');
  s.stop();
});

test('resync.required starts again from a snapshot', async () => {
  const {s, clock} = stream();
  s.start();
  await flush();
  FakeSocket.all[0].send({type: 'snapshot', instance_id: 'i', cursor: 3, state: {}});
  FakeSocket.all[0].send({event: {seq: 9, type: 'resync.required', payload: {}}, instance_id: 'i'});
  await clock.advance(1000);
  await flush();
  assert.doesNotMatch(FakeSocket.all[1].url, /since=/);
  s.stop();
});

function fakeFetch(answers) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({url, init, body: init.body ? JSON.parse(init.body) : undefined});
    const [status, body] = answers.shift();
    return {ok: status >= 200 && status < 300, status, json: async () => body};
  };
  fn.calls = calls;
  return fn;
}

test('the API base is the page origin, whatever the path', () => {
  assert.equal(apiBase('https://h.example.ts.net/odomo/'), 'https://h.example.ts.net/');
  assert.equal(apiBase('https://h.example.ts.net:8443/odomo/index.html?x=1'), 'https://h.example.ts.net:8443/');
});

test('client: bearer, no cookies, errors carry code and reason, 401 unpairs', async () => {
  const unpaired = [];
  const fetchImpl = fakeFetch([[200, {ok: 1}], [403, {error: {code: 'web_client_forbidden'}}],
    [401, {error: {code: 'permission_denied', reason: 'credential_revoked'}}]]);
  const client = new HostClient({base: 'https://h/', token: 'tok', fetchImpl, onUnpaired: (r) => unpaired.push(r)});
  assert.deepEqual(await client.get('/v1/state'), {ok: 1});
  assert.equal(fetchImpl.calls[0].init.headers.Authorization, 'Bearer tok');
  assert.equal(fetchImpl.calls[0].init.credentials, 'omit');
  await assert.rejects(client.get('/v1/remote/capabilities'), (e) => e.code === 'web_client_forbidden' && !e.unpaired);
  assert.deepEqual(unpaired, []);
  await assert.rejects(client.get('/v1/state'), (e) => e.unpaired && e.reason === 'credential_revoked');
  assert.deepEqual(unpaired, ['credential_revoked']);
});

test('a confirm row: 409 with a token is followed once when the person confirmed, held otherwise', async () => {
  const token = 'A'.repeat(43);
  const conflict = [409, {error: {code: 'confirmation_required', detail: {confirm_token: token, expires_in: 30}}}];
  let fetchImpl = fakeFetch([conflict, [200, {status: 'accepted'}]]);
  let client = new HostClient({base: 'https://h/', token: 't', fetchImpl});
  const result = await invokeConfirmed(client, 'system.reboot', {request_id: 'r1', catalog_revision: 'c'}, {confirmed: true});
  assert.equal(result.status, 'accepted');
  assert.equal(fetchImpl.calls[1].body.confirm_token, token);
  assert.notEqual(fetchImpl.calls[1].body.request_id, 'r1');
  assert.match(fetchImpl.calls[1].url, /\/v1\/actions\/system\.reboot:invoke$/);
  fetchImpl = fakeFetch([conflict]);
  client = new HostClient({base: 'https://h/', token: 't', fetchImpl});
  await assert.rejects(invokeConfirmed(client, 'x', {request_id: 'r'}, {confirmed: false}), (e) => e.confirmToken === token);
  assert.equal(fetchImpl.calls.length, 1);
  // twice in a row is a refusal, not a loop
  fetchImpl = fakeFetch([conflict, conflict]);
  client = new HostClient({base: 'https://h/', token: 't', fetchImpl});
  await assert.rejects(invokeConfirmed(client, 'x', {request_id: 'r'}, {confirmed: true}), (e) => e.code === 'confirmation_required');
  assert.equal(fetchImpl.calls.length, 2);
  // a malformed token is no token
  fetchImpl = fakeFetch([[409, {error: {code: 'confirmation_required', detail: {confirm_token: '<script>'}}}]]);
  client = new HostClient({base: 'https://h/', token: 't', fetchImpl});
  await assert.rejects(invokeConfirmed(client, 'x', {request_id: 'r'}, {confirmed: true}), (e) => !e.confirmToken);
});

test('pairing: a web request, the claim, a dead request, renewal', async () => {
  let fetchImpl = fakeFetch([[200, {request_id: 'pair_' + '0'.repeat(32), request_secret: 's'.repeat(43),
    device_id: 'odomo-1', expires_at: 1000, client_label: 'Web app · Safari on iPhone', remote_addr: '100.64.0.7'}]]);
  let client = new HostClient({base: 'https://h/', fetchImpl});
  const pending = await requestPairing(client, {deviceId: 'odomo-1', label: 'Safari on iPhone'});
  assert.deepEqual(fetchImpl.calls[0].body, {device_id: 'odomo-1', device_name: 'Safari on iPhone',
    client: {kind: 'web', label: 'Safari on iPhone'}});
  assert.equal(fetchImpl.calls[0].init.headers.Authorization, undefined);
  assert.equal(pending.client_label, 'Web app · Safari on iPhone');
  fetchImpl = fakeFetch([[200, {status: 'pending'}], [200, {status: 'claimed', credential: 'cred', device_id: 'odomo-1',
    issued_at: 1, credential_expires_at: 2, client_label: 'Web app · Safari on iPhone'}],
  [409, {error: {code: 'pairing_request_unavailable'}}]]);
  client = new HostClient({base: 'https://h/', fetchImpl});
  assert.deepEqual(await pollClaim(client, pending, 0), {state: 'pending'});
  const claimed = await pollClaim(client, pending, 0);
  assert.equal(claimed.state, 'claimed');
  assert.equal(claimed.credential.token, 'cred');
  assert.deepEqual(await pollClaim(client, pending, 0), {state: 'dead', reason: 'pairing_request_unavailable'});
  assert.deepEqual(await pollClaim(client, pending, 2000 * 1000), {state: 'dead', reason: 'expired'});
  fetchImpl = fakeFetch([[200, {renewable: true, expires_at: 5}], [200, {credential: 'new', issued_at: 9, credential_expires_at: 99}]]);
  client = new HostClient({base: 'https://h/', token: 'old', fetchImpl});
  assert.deepEqual(await renewIfDue(client, {token: 'old', device_id: 'odomo-1'}),
    {token: 'new', device_id: 'odomo-1', issued_at: 9, expires_at: 99});
});
