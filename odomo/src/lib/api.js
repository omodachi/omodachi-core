// Talking to the host Odomo is served by. There is exactly one: the API base
// is the origin of `document.baseURI` (Odomo lives at /odomo/ on it), so the
// page can never be pointed at another host and needs no CORS.

export const CREDENTIAL_REASONS = ['credential_expired', 'credential_revoked', 'device_purged', 'unknown_credential'];

export class ApiError extends Error {
  constructor(status, code, message, {reason = null, detail = null} = {}) {
    super(message || code || String(status));
    this.status = status;
    this.code = code || 'http_' + status;
    this.reason = reason;
    this.detail = detail;
  }
  // The credential itself was refused: the browser is no longer paired.
  get unpaired() {
    return this.status === 401 && (CREDENTIAL_REASONS.includes(this.reason) || this.code === 'pairing_required'
      || this.code === 'permission_denied');
  }
}

export function apiBase(baseURI) {
  return new URL('/', baseURI).href;
}

export function newRequestId(random = (n) => crypto.getRandomValues(new Uint8Array(n))) {
  return 'odomo-' + Array.from(random(12), (b) => b.toString(16).padStart(2, '0')).join('');
}

export class HostClient {
  constructor({base, token = null, fetchImpl = (...args) => fetch(...args), onUnpaired = () => {}}) {
    this.base = base;
    this.token = token;
    this.fetch = fetchImpl;
    this.onUnpaired = onUnpaired;
  }

  url(path) {
    return new URL(path.replace(/^\//, ''), this.base).href;
  }

  async request(method, path, body, {auth = true, timeout = 15000} = {}) {
    const headers = {Accept: 'application/json'};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (auth) {
      if (!this.token) throw new ApiError(401, 'pairing_required', 'not paired');
      headers.Authorization = 'Bearer ' + this.token;
    }
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), timeout) : null;
    let response;
    try {
      response = await this.fetch(this.url(path), {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body),
        credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer',
        signal: controller ? controller.signal : undefined,
      });
    } catch (error) {
      throw new ApiError(0, 'host_unreachable', String(error && error.message || error));
    } finally {
      if (timer) clearTimeout(timer);
    }
    let data = null;
    try {
      data = await response.json();
    } catch {
      data = null;
    }
    if (!response.ok) {
      const error = data && data.error ? data.error : {};
      const failure = new ApiError(response.status, error.code, error.message, {reason: error.reason || null,
        detail: error.detail || null});
      if (auth && failure.unpaired) this.onUnpaired(failure.reason || failure.code);
      throw failure;
    }
    return data;
  }

  // HERDR-11: a GET with an `If-None-Match` (and any other header), where
  // `304` is an answer: `{status, data, etag}`, data null on a 304 - the copy
  // on hand stands. Errors throw as `request` does.
  async conditional(path, etag, extra = {}, {timeout = 15000} = {}) {
    if (!this.token) throw new ApiError(401, 'pairing_required', 'not paired');
    const headers = {Accept: 'application/json', Authorization: 'Bearer ' + this.token, ...extra};
    if (etag) headers['If-None-Match'] = etag;
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), timeout) : null;
    let response;
    try {
      response = await this.fetch(this.url(path), {method: 'GET', headers, credentials: 'omit', cache: 'no-store',
        redirect: 'error', referrerPolicy: 'no-referrer', signal: controller ? controller.signal : undefined});
    } catch (error) {
      throw new ApiError(0, 'host_unreachable', String(error && error.message || error));
    } finally {
      if (timer) clearTimeout(timer);
    }
    const tag = response.headers && typeof response.headers.get === 'function' ? response.headers.get('ETag') : null;
    if (response.status === 304) return {status: 304, data: null, etag: tag || etag || null};
    let data = null;
    try { data = await response.json(); } catch { data = null; }
    if (!response.ok) {
      const error = data && data.error ? data.error : {};
      const failure = new ApiError(response.status, error.code, error.message, {reason: error.reason || null});
      if (failure.unpaired) this.onUnpaired(failure.reason || failure.code);
      throw failure;
    }
    return {status: response.status, data, etag: tag};
  }

  get(path, options) { return this.request('GET', path, undefined, options); }
  post(path, body = {}, options) { return this.request('POST', path, body, options); }
  put(path, body = {}, options) { return this.request('PUT', path, body, options); }
  del(path, options) { return this.request('DELETE', path, undefined, options); }

  // ── pairing (unauthenticated) ────────────────────────────────────────
  pairingRequest(body) { return this.post('/v1/pairing/requests', body, {auth: false}); }
  claim(requestId, secret) {
    return this.post('/v1/pairing/requests/' + encodeURIComponent(requestId) + '/claim', {request_secret: secret}, {auth: false});
  }
  health() { return this.get('/health', {auth: false}); }

  // ── this credential ──────────────────────────────────────────────────
  credential() { return this.get('/v1/pairing/credential'); }
  renew() { return this.post('/v1/pairing/renew', {}); }

  // ── what LITE-1a draws ───────────────────────────────────────────────
  theme() { return this.get('/v1/theme'); }
  catalog() { return this.get('/v1/catalog'); }
  // HERDR-11: the catalog unless it is still the one held (`304`).
  catalogIf(etag) { return this.conditional('/v1/catalog', etag); }
  shortcuts() { return this.get('/v1/shortcuts'); }
  notifications() { return this.get('/v1/notifications'); }
  dnd(enabled) { return enabled === undefined ? this.get('/v1/notifications/dnd') : this.post('/v1/notifications/dnd', {enabled}); }
  selectWorkspace(id) { return this.post('/v1/workspaces/' + Number(id) + '/select', {}); }
  ticket(path) { return this.post('/v1/ws-ticket', {path}); }

  // ── LITE-1b: push ────────────────────────────────────────────────────
  vapidKey() { return this.get('/v1/push/vapid-public-key'); }
  subscribePush(subscription) { return this.post('/v1/push/subscriptions', subscription); }
  unsubscribePush() { return this.del('/v1/push/subscriptions'); }
  prefs() { return this.get('/v1/notifications/prefs'); }
  setPrefs(expectedRevision, changes) {
    return this.put('/v1/notifications/prefs', {expected_revision: expectedRevision, changes});
  }

  invoke(entryId, body) {
    return this.post('/v1/actions/' + encodeURIComponent(entryId) + ':invoke', body);
  }

  // ── ODOMO-2: Herdr (the App's `CompanionHostClient` herdr calls) ──────
  // Every id is checked against Herdr's own shape before it becomes part of
  // a path; a value that is not is a 400 here, never a request.
  herdrLayout() { return this.get('/v1/herdr/layout'); }
  // HERDR-11: `ETag: "<session>:<revision>"`; 304 keeps the layout on hand.
  herdrLayoutIf(etag) { return this.conditional('/v1/herdr/layout', etag); }
  // HERDR-11: star or un-star a folder (answers with the folders document).
  herdrFavourite(path, favourite) {
    return this.post('/v1/herdr/folders/favourites', {path: String(path), favourite: !!favourite});
  }
  herdrSessions() { return this.get('/v1/herdr/sessions'); }
  herdrSelectSession(name) { return this.post('/v1/herdr/sessions/' + checked(name, SESSION_ID) + '/select', {}); }
  herdrStartSession(name) { return this.post('/v1/herdr/sessions/' + checked(name, SESSION_ID) + '/start', {}); }
  herdrCreateSession(name) { return this.post('/v1/herdr/sessions', {name: checked(name, NEW_SESSION_ID)}); }
  // HERDR-6: a new workspace in a folder, and the folders it is offered in.
  // The folder is the person's own text; core checks it, never this page.
  herdrCreateWorkspace(session, cwd, name, launch) {
    // HERDR-11: an empty folder is the home folder, sent as `~`; `launch` is
    // one of the host's own launchers, exactly as it listed it.
    const body = {cwd: String(cwd || '').trim() || '~'};
    if (name) body.name = name;
    if (launch) body.launch = String(launch);
    return this.post('/v1/herdr/sessions/' + checked(session, SESSION_ID) + '/workspaces', body);
  }
  herdrFolders(under, hidden) {
    const query = new URLSearchParams();
    if (under) query.set('under', String(under));
    if (under && hidden) query.set('hidden', '1');
    const text = query.toString();
    return this.get('/v1/herdr/folders' + (text ? '?' + text : ''));
  }
  herdrOperatorRows(agent) {
    return this.get('/v1/herdr/operator-rows' + (agent ? '?agent=' + encodeURIComponent(checked(agent, AGENT_ID)) : ''));
  }
  herdrPaneAction(pane, action, body) {
    if (!['split', 'zoom', 'close'].includes(action)) throw new ApiError(400, 'herdr_action_unsupported');
    return this.post('/v1/herdr/panes/' + checked(pane, PANE_ID) + '/' + action, body || {});
  }
  herdrKeys(pane, keys) { return this.post('/v1/herdr/panes/' + checked(pane, PANE_ID) + '/keys', {keys}); }
  herdrText(pane, text, submit, attachments) {
    const body = {text, submit: !!submit};
    if (attachments && attachments.length) body.attachments = attachments;
    return this.post('/v1/herdr/panes/' + checked(pane, PANE_ID) + '/text', body, {timeout: 30000});
  }
  herdrNewTab(workspace) {
    return this.post('/v1/herdr/workspaces/' + checked(workspace, WORKSPACE_ID) + '/tabs', {focus: false});
  }
  herdrCloseTab(workspace, tab) {
    checked(workspace, WORKSPACE_ID);
    if (!checked(tab, TAB_ID).startsWith(workspace + ':')) throw new ApiError(400, 'invalid_tab');
    return this.del('/v1/herdr/workspaces/' + workspace + '/tabs/' + tab);
  }

  // ── ATTACH-1: a file for the Agent or a Herdr pane ────────────────────
  // The body is the file itself; `fetch` cannot report upload progress, so
  // this one call is an XMLHttpRequest. `onProgress(fraction)`; `abort()` on
  // the returned promise stops it.
  upload(file, query, onProgress = () => {}, XHR = globalThis.XMLHttpRequest) {
    if (!this.token) return Promise.reject(new ApiError(401, 'pairing_required', 'not paired'));
    const url = new URL(this.url('/v1/uploads'));
    for (const [key, value] of Object.entries(query || {})) url.searchParams.set(key, String(value));
    let xhr = null;
    const promise = new Promise((resolve, reject) => {
      xhr = new XHR();
      xhr.open('POST', url.href);
      xhr.setRequestHeader('Authorization', 'Bearer ' + this.token);
      xhr.setRequestHeader('Accept', 'application/json');
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.timeout = 10 * 60 * 1000;
      if (xhr.upload) {
        xhr.upload.onprogress = (event) => {
          if (event.lengthComputable && event.total > 0) onProgress(Math.min(1, event.loaded / event.total));
        };
      }
      xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch { data = null; }
        if (xhr.status >= 200 && xhr.status < 300 && data) return resolve(data);
        const error = data && data.error ? data.error : {};
        const failure = new ApiError(xhr.status, error.code, error.message, {reason: error.reason || null});
        if (failure.unpaired) this.onUnpaired(failure.reason || failure.code);
        return reject(failure);
      };
      xhr.onerror = () => reject(new ApiError(0, 'host_unreachable', 'upload failed'));
      xhr.ontimeout = () => reject(new ApiError(0, 'host_unreachable', 'upload timed out'));
      xhr.onabort = () => reject(new ApiError(0, 'upload_cancelled', 'cancelled'));
      xhr.send(file);
    });
    promise.abort = () => { try { if (xhr) xhr.abort(); } catch { /* gone */ } };
    return promise;
  }
  deleteUpload(id) { return this.del('/v1/uploads/' + checked(id, UPLOAD_ID)); }

  // ── ODOMO-2: the default agent's chat ─────────────────────────────────
  chatEnsure() { return this.post('/v1/agent/default:ensure', {surface: 'chat'}, {timeout: 60000}); }
  chatSnapshot() { return this.get('/v1/agent/default/chat'); }
  chatSend(requestId, text, model, effort, attachments) {
    const body = {request_id: requestId, text};
    if (model) body.model = model;
    if (effort) body.effort = effort;
    if (attachments && attachments.length) body.attachments = attachments;
    return this.post('/v1/agent/default/chat/messages', body, {timeout: 30000});
  }
  chatSteer(requestId, text) { return this.post('/v1/agent/default/chat/steer', {request_id: requestId, text}); }
  chatInterrupt(turnId) { return this.post('/v1/agent/default/chat/interrupt', {turn_id: turnId}); }
  // Exactly one of `decision` and `input`: two bodies, never one with a null.
  chatResolve(requestId, answer) {
    const path = '/v1/agent/default/chat/approvals/' + checked(String(requestId), SAFE_ID);
    // AGENT-QUEUE-1: a decline may say why, in one line.
    if (answer.decision && answer.reason) return this.post(path, {decision: answer.decision, reason: answer.reason});
    return this.post(path, answer.decision ? {decision: answer.decision} : {input: answer.input});
  }
  // AGENT-QUEUE-1: Send while a turn runs - codex's own queue - and a waiting
  // message's Send now / Edit / Delete; earlier messages a page at a time.
  chatQueue(requestId, text) { return this.post('/v1/agent/default/chat/queue', {request_id: requestId, text}); }
  chatQueueItem(id, action, text = null) {
    if (!['send', 'edit', 'delete'].includes(action)) throw new Error('queue action');
    const path = '/v1/agent/default/chat/queue/' + checked(String(id), SAFE_ID) + ':' + action;
    return this.post(path, action === 'edit' ? {text} : {});
  }
  chatHistory(before, limit = 100) {
    const query = ['limit=' + encodeURIComponent(String(limit))];
    if (before) query.unshift('before=' + encodeURIComponent(checked(String(before), SAFE_ID)));
    return this.get('/v1/agent/default/chat/history?' + query.join('&'));
  }
  chatModels() { return this.get('/v1/agent/default/models'); }
  // AGENT-PLAN-1: codex's collaboration mode for the turns that follow (the
  // header's Plan chip): `plan` or `default`.
  chatMode(mode) { return this.post('/v1/agent/default/chat/mode', {mode}); }
  // AGENT-NEW-1: a fresh default conversation for the one on screen.
  chatNew(expectedSessionId) {
    return this.post('/v1/agent/default/chat:new',
      {confirmed: true, expected_provider_session_id: expectedSessionId}, {timeout: 60000});
  }
  chatHandoffPrepare() { return this.post('/v1/agent/default/chat/handoff:prepare', {}); }
  chatHandoffConfirm(planId) {
    return this.post('/v1/agent/default/chat/handoff:confirm', {plan_id: planId, confirmed: true}, {timeout: 60000});
  }
}

// Herdr's own alphabets (core `herdr_bridge.py`) and the App's safe id.
export const PANE_ID = /^w[0-9]{1,9}:p[0-9]{1,9}$/;
export const TAB_ID = /^w[0-9]{1,9}:t[0-9]{1,9}$/;
export const WORKSPACE_ID = /^w[0-9]{1,9}$/;
export const SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
export const NEW_SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$/;
export const AGENT_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$/;
export const SAFE_ID = /^[A-Za-z0-9_.:-]{1,128}$/;
export const UPLOAD_ID = /^[0-9a-f]{32}$/;

export function checked(value, pattern) {
  if (typeof value !== 'string' || !pattern.test(value)) throw new ApiError(400, 'invalid_request', 'bad identifier');
  return value;
}

// ODOMO-2: one WebSocket on a ticket (`POST /v1/ws-ticket`), for the Herdr
// mirror and the agent's events. It opens once and says when it closes; the
// caller decides whether and when to open another (only it knows whether its
// panel is still on screen).
export function openSocket({client, base, path, query = {}, WebSocketImpl = globalThis.WebSocket,
                            onOpen = () => {}, onMessage = () => {}, onClose = () => {}}) {
  let socket = null;
  let closed = false;
  const handle = {
    send(text) {
      if (socket && socket.readyState === 1) socket.send(text);
    },
    close() {
      closed = true;
      if (socket) {
        try { socket.close(1000); } catch { /* gone */ }
      }
    },
    get open() { return !!socket && socket.readyState === 1; },
  };
  client.ticket(path).then((minted) => {
    if (closed) return;
    const url = new URL(path.replace(/^\//, ''), base);
    url.protocol = url.protocol === 'http:' ? 'ws:' : 'wss:';
    for (const [key, value] of Object.entries(query)) url.searchParams.set(key, String(value));
    url.searchParams.set('ticket', minted.ticket);
    try {
      socket = new WebSocketImpl(url.href);
    } catch (error) {
      onClose({code: 0, reason: '', error});
      return;
    }
    socket.onopen = () => { if (!closed) onOpen(); };
    socket.onmessage = (event) => { if (!closed) onMessage(event.data); };
    socket.onclose = (event) => {
      if (!closed) onClose({code: event && event.code, reason: String((event && event.reason) || '')});
    };
    socket.onerror = () => { /* onclose follows */ };
  }).catch((error) => {
    if (!closed) onClose({code: 0, reason: '', error});
  });
  return handle;
}

// A row the host marked `confirm` (or answered `409 confirmation_required`)
// runs on a second call that carries the one-use token (RELEASE-9 B4,
// STORE-6 §B3). When the person already gave the second tap here, the call
// goes again at once with the token - once, never a loop.
export async function invokeConfirmed(client, entryId, body, {confirmed}) {
  try {
    return await client.invoke(entryId, body);
  } catch (error) {
    const token = error && error.code === 'confirmation_required' && error.detail && error.detail.confirm_token;
    if (!(error instanceof ApiError) || !token || !/^[A-Za-z0-9_-]{43}$/.test(token)) throw error;
    if (!confirmed) {
      error.confirmToken = token;
      throw error;
    }
    return client.invoke(entryId, {...body, request_id: newRequestId(), confirm_token: token});
  }
}
