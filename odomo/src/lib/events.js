// `/v1/events` for a browser: a ticket, a heartbeat, and a reconnect that
// does not give up.
//
// TAIL-1 §5.5 found the native app showing "Connected" for minutes over a
// socket that had died with the tailnet path under it, because nothing ever
// listened for silence. A browser cannot see WebSocket pings at all, so Odomo
// opens the stream with `heartbeat=1` and core sends a small message every
// 15 s; hearing nothing for `silence` ms is a dead socket, closed and
// reopened. A reconnect resumes from the last cursor (`since` +
// `instance_id`), so nothing raised in between is lost; a restarted daemon
// answers with a fresh snapshot instead.
//
// Everything that touches the outside world is injected, so the whole
// behaviour runs under `node --test` with fake sockets and a fake clock.

export const HEARTBEAT_SILENCE = 40000;

export function backoff(attempt, random = Math.random) {
  const base = Math.min(30000, 1000 * 2 ** Math.min(attempt, 5));
  return Math.round(base * (0.8 + 0.4 * random()));
}

export class EventStream {
  constructor({ticket, url, WebSocketImpl, onMessage, onStatus = () => {}, onUnpaired = () => {},
               timers = {setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: (id) => clearTimeout(id)},
               now = () => Date.now(), random = () => Math.random(),
               silence = HEARTBEAT_SILENCE}) {
    Object.assign(this, {ticket, url, WebSocketImpl, onMessage, onStatus, onUnpaired, timers, now, random, silence});
    this.socket = null;
    this.attempt = 0;
    this.cursor = null;
    this.instance = null;
    this.stopped = true;
    this.retryTimer = null;
    this.watchTimer = null;
    this.status = 'idle';
    this.openedAt = 0;
  }

  setStatus(status) {
    if (this.status === status) return;
    this.status = status;
    this.onStatus(status);
  }

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect();
  }

  stop() {
    this.stopped = true;
    this.timers.clearTimeout(this.retryTimer);
    this.timers.clearTimeout(this.watchTimer);
    this.retryTimer = this.watchTimer = null;
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      try { socket.close(1000); } catch { /* already closed */ }
    }
    this.setStatus('idle');
  }

  // The page came back to the foreground, or the network did: do not wait
  // out a long backoff.
  kick() {
    if (this.stopped) return;
    if (this.socket && this.status === 'connected') {
      this.watch();
      return;
    }
    this.timers.clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.attempt = 0;
    if (!this.socket) this.connect();
  }

  watch() {
    this.timers.clearTimeout(this.watchTimer);
    this.watchTimer = this.timers.setTimeout(() => {
      // Silence: the path is gone even though nobody said so.
      const socket = this.socket;
      if (socket) this.drop(socket, 'silence');
    }, this.silence);
  }

  async connect() {
    if (this.stopped || this.socket) return;
    // A long outage stays `offline` while it retries; anything else is `connecting`.
    if (this.status !== 'offline') this.setStatus('connecting');
    let ticket;
    try {
      ticket = await this.ticket();
    } catch (error) {
      if (error && error.unpaired) {
        this.stopped = true;
        this.setStatus('idle');
        this.onUnpaired(error.reason || error.code);
        return;
      }
      return this.retry();
    }
    if (this.stopped || this.socket) return;
    const resume = this.instance !== null && this.cursor !== null ? {since: this.cursor, instance_id: this.instance} : null;
    let socket;
    try {
      socket = new this.WebSocketImpl(this.url(ticket.ticket, resume));
    } catch {
      return this.retry();
    }
    this.socket = socket;
    this.openedAt = this.now();
    socket.onopen = () => {
      if (this.socket !== socket) return;
      this.watch();
    };
    socket.onmessage = (event) => {
      if (this.socket !== socket) return;
      this.watch();
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (message.type === 'snapshot' || message.type === 'ready') {
        this.instance = message.instance_id;
        this.cursor = message.cursor;
        this.attempt = 0;
        this.setStatus('connected');
      } else if (message.event && Number.isInteger(message.event.seq)) {
        if (message.event.type === 'resync.required') {
          // The host dropped events for us: start again from a snapshot.
          this.instance = this.cursor = null;
          this.drop(socket, 'resync');
          return;
        }
        this.cursor = message.event.seq;
      }
      this.onMessage(message);
    };
    socket.onclose = (event) => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.timers.clearTimeout(this.watchTimer);
      const reason = String((event && event.reason) || '');
      if (event && event.code === 1008 && reason.startsWith('credential revoked or expired')) {
        this.stopped = true;
        this.setStatus('idle');
        this.onUnpaired(reason.split(': ')[1] || 'credential_revoked');
        return;
      }
      this.retry();
    };
    socket.onerror = () => { /* onclose follows */ };
  }

  drop(socket, why) {
    if (this.socket === socket) this.socket = null;
    this.timers.clearTimeout(this.watchTimer);
    try { socket.close(4000, why); } catch { /* gone */ }
    this.retry();
  }

  retry() {
    if (this.stopped) return;
    this.setStatus(this.attempt >= 2 ? 'offline' : 'connecting');
    const wait = backoff(this.attempt, this.random);
    this.attempt += 1;
    this.timers.clearTimeout(this.retryTimer);
    this.retryTimer = this.timers.setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, wait);
  }
}

export function eventsUrl(base, ticket, resume) {
  const url = new URL('v1/events', base);
  url.protocol = url.protocol === 'http:' ? 'ws:' : 'wss:';
  url.searchParams.set('heartbeat', '1');
  if (resume) {
    url.searchParams.set('since', String(resume.since));
    url.searchParams.set('instance_id', resume.instance_id);
  }
  url.searchParams.set('ticket', ticket);
  return url.href;
}
