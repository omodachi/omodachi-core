// ODOMO-2: the Herdr panel's store, as omodachi-ios `Herdr/HerdrStore.swift`
// does it on herdr-4.
//
// The Collie way, unchanged: the browser never takes the pane. The screen is
// `observe` at the pane's own size (the desktop's pane does not move); keys,
// the composer, Quick and Type are one-shot `/keys` and `/text` calls that
// take nothing and resize nothing. Choosing a pane or a workspace here never
// moves the computer (no `pane.focus`, no `workspace focus`).

import {useCallback, useEffect, useRef, useState} from 'react';
import * as H from './herdr.js';
import {ApiError, openSocket} from './api.js';
import {Mirror, paletteOf} from './term.js';
import {nextMode, phoneLike, preferredMode, rememberMode} from './wrap.js';
import {readChoices, sameChoices, notesOpen} from './choices.js';
import * as storage from './storage.js';
import {folderExtras, launcherFallback, layoutEventIsMine, mirrorStale, toggled} from './herdr11.js';

const IDLE = {kind: 'idle'};
// HERDR-6: the new-workspace card, closed.
const NO_WORKSPACE_CARD = {open: false, loading: false, recent: [], common: [], browse: null, browsing: false,
  hidden: false, creating: false, error: null, favourites: null, launchers: null};

export function useHerdr({client, base, theme, revision, focus, WebSocketImpl, health = 'live'}) {
  const [layout, setLayout] = useState(null);
  const [sessions, setSessions] = useState([]);
  const [selectedSession, setSelectedSession] = useState(null);
  const [ownedSession, setOwnedSession] = useState(null);
  const [switching, setSwitching] = useState(false);
  const [starting, setStarting] = useState(null);
  const [newSession, setNewSession] = useState({open: false, creating: false, error: null});
  const [newWorkspace, setNewWorkspace] = useState(NO_WORKSPACE_CARD);
  const [selected, setSelected] = useState(null);
  const [connection, setConnection] = useState(IDLE);
  const [loadingLayout, setLoadingLayout] = useState(false);
  const [notice, setNotice] = useState(null);
  const [typing, setTyping] = useState(false);
  const [sendState, setSendState] = useState(IDLE);
  const [drafts, setDrafts] = useState({});
  const [rows, setRows] = useState(() => H.shippedRows(null));
  const [quickOpen, setQuickOpen] = useState(false);
  const [armed, setArmed] = useState(null);
  // HERDR-8: how the mirror shows the pane - 'wrap' (text that fits), 'fit',
  // 'actual' or a pinch ('custom'); `forced` is Wrap chosen on purpose, which
  // is shown even over a full-screen program.
  const [zoom, setZoom] = useState({mode: 'fit', scale: 1, forced: false});
  // AGENT-UI-3: a phone (the device, either way up) cycles Wrap ↔ Fit only.
  const [phone] = useState(() => phoneLike());
  const [keysOpen, setKeysOpen] = useState(false);
  const [firstFrame, setFirstFrame] = useState(0);
  // HERDR-9: the question on the pane's screen, and one just answered (the
  // strip waits for the screen to move on instead of taking a second tap).
  const [choices, setChoices] = useState(null);
  const [answered, setAnswered] = useState(null);
  // HERDR-11 §1: what the New workspace card starts (null: a plain shell).
  const [launcher, setLauncher] = useState(() => storage.local('herdr.launcher', null));
  // HERDR-11 §3: the host answers, Herdr on it does not.
  const [herdrDown, setHerdrDown] = useState(false);

  const live = useRef({layout: null, selected: null, session: null, sessions: [], typing: false, rowsFor: undefined});
  live.current.layout = layout;
  live.current.selected = selected;
  live.current.session = selectedSession;
  live.current.sessions = sessions;
  live.current.typing = typing;
  // HERDR-10: a draft belongs to a pane in a session - `w1:p1` is a pane in
  // every session.
  const draftKey = (pane) => (live.current.session || '') + '\u001f' + pane;

  const mirror = useRef(null);
  if (!mirror.current) mirror.current = new Mirror();
  const generation = useRef(0);
  const stream = useRef(null);
  const openedWith = useRef(null);
  const attempt = useRef(0);
  const retryTimer = useRef(0);
  const layoutTimer = useRef(0);
  const layoutAttempt = useRef(0);
  const layoutBusy = useRef(false);
  const sessionsBusy = useRef(false);
  const queue = useRef(Promise.resolve());
  const typed = useRef([]);
  const typedTimer = useRef(0);
  const idleTimer = useRef(0);
  const sentTimer = useRef(0);
  const armTimer = useRef(0);
  const pendingPane = useRef(null);
  const pendingNewTab = useRef(null);
  const appeared = useRef(true);
  const choicesRef = useRef(null);
  const answeredTimer = useRef(0);
  const layoutTag = useRef(null);
  const layoutAgain = useRef(false);
  const healthRef = useRef(health);
  healthRef.current = health;

  // HERDR-9: the choices on the screen this instant.
  const currentChoices = useCallback(() => {
    const cursor = mirror.current.cursor();
    return readChoices(mirror.current.text(), cursor.hidden ? null : cursor.y);
  }, []);
  const readScreen = useCallback(() => {
    const fresh = currentChoices();
    const was = choicesRef.current;
    if (!(fresh === was || (fresh && was && fresh.signature === was.signature && sameChoices(fresh, was)))) {
      choicesRef.current = fresh;
      setChoices(fresh);
    }
    setAnswered((sig) => (sig && (!fresh || fresh.signature !== sig) ? null : sig));
  }, [currentChoices]);

  useEffect(() => {
    mirror.current.setPalette(paletteOf(theme));
  }, [theme]);

  const say = useCallback((value) => setNotice(value), []);
  const sayError = useCallback((error) => setNotice({code: (error && error.code) || 'herdr_unavailable'}), []);

  // ── the stream ──────────────────────────────────────────────────────────
  const closeStream = useCallback(() => {
    clearTimeout(retryTimer.current);
    retryTimer.current = 0;
    if (stream.current) stream.current.close();
    stream.current = null;
    attempt.current = 0;
  }, []);

  const openStream = useCallback((pane) => {
    if (!pane || !appeared.current) return;
    if (stream.current) stream.current.close();
    const current = generation.current;
    const geometry = H.paneGeometry(H.paneOf(live.current.layout, pane)) || {cols: 80, rows: 24};
    openedWith.current = geometry;
    mirror.current.resize(geometry.cols, geometry.rows);
    mirror.current.awaitingFull = true;
    setConnection({kind: 'connecting'});
    let first = true;
    const handle = openSocket({client, base, path: '/v1/herdr/panes/' + pane + '/observe', query: geometry, WebSocketImpl,
      onMessage: (data) => {
        if (generation.current !== current || live.current.selected !== pane || stream.current !== handle) return;
        if (first) {
          first = false;
          attempt.current = 0;
          setConnection({kind: 'observing'});
        }
        const message = H.decodeMessage(data);
        if (!message) return;
        if (message.type === 'frame') {
          const wasAwaiting = mirror.current.awaitingFull;
          mirror.current.feed(message, () => {
            if (message.full && wasAwaiting) setFirstFrame((n) => n + 1);
            readScreen();
          });
        } else if (message.type === 'closed') {
          if (message.reason) setNotice({key: 'streamClosed', value: message.reason});
        }
      },
      onClose: () => {
        if (generation.current !== current || live.current.selected !== pane || stream.current !== handle) return;
        stream.current = null;
        // Exponential backoff, and never a re-run on the host: a reconnect
        // asks Herdr to repaint, which the fresh stream's full frame does.
        attempt.current += 1;
        const n = attempt.current;
        setConnection(n > 6 ? {kind: 'unavailable'} : {kind: 'retrying', attempt: n});
        clearTimeout(retryTimer.current);
        retryTimer.current = setTimeout(() => {
          if (generation.current === current && live.current.selected === pane && appeared.current) openStream(pane);
        }, H.reconnectDelay(n));
      }});
    stream.current = handle;
  }, [client, base, WebSocketImpl]);

  // `observe` reads no stdin: the bridge restarts it at the new size, and the
  // next frame is a full one.
  const resizeStream = useCallback((geometry) => {
    if (!stream.current || !geometry) return;
    const was = openedWith.current;
    if (was && was.cols === geometry.cols && was.rows === geometry.rows) return;
    openedWith.current = geometry;
    mirror.current.awaitingFull = true;
    stream.current.send(JSON.stringify({type: 'resize', cols: geometry.cols, rows: geometry.rows}));
  }, []);

  // ── typing ──────────────────────────────────────────────────────────────
  const stopTyping = useCallback((idle) => {
    clearTimeout(idleTimer.current);
    if (!live.current.typing) return;
    live.current.typing = false;
    setTyping(false);
    if (idle) setNotice({key: 'typingIdleOff'});
  }, []);

  const touchTyping = useCallback(() => {
    clearTimeout(idleTimer.current);
    idleTimer.current = setTimeout(() => stopTyping(true), H.TYPING_IDLE_MS);
  }, [stopTyping]);

  // ── writes, one at a time in the order they were made ───────────────────
  const enqueue = useCallback((pane, write, done) => {
    queue.current = queue.current.then(async () => {
      let result = null;
      let failure = null;
      try {
        if (write.keys) result = await client.herdrKeys(pane, write.keys);
        else result = await client.herdrText(pane, write.text, write.submit, write.attachments);
      } catch (error) {
        failure = error;
      }
      if (done) done(result, failure);
      else if (failure && live.current.selected === pane) sayError(failure);
    });
    return queue.current;
  }, [client, sayError]);

  // ── selection ───────────────────────────────────────────────────────────
  const refreshRows = useCallback((pane, force) => {
    const agent = H.rowsAgent(pane);
    if (!force && live.current.rowsFor === agent) return;
    live.current.rowsFor = agent;
    client.herdrOperatorRows(agent).then((value) => {
      if (live.current.rowsFor === agent) setRows(value);
    }).catch(() => {
      if (live.current.rowsFor === agent) setRows(H.shippedRows(agent));
      live.current.rowsFor = undefined;
    });
  }, [client]);

  const select = useCallback((pane) => {
    if (pane === live.current.selected) {
      if (pane && !stream.current) openStream(pane);
      return;
    }
    stopTyping();
    closeStream();
    live.current.selected = pane;
    setSelected(pane);
    setSendState(IDLE);
    setQuickOpen(false);
    setArmed(null);
    choicesRef.current = null;
    setChoices(null);
    setAnswered(null);
    // HERDR-8: the mode this kind of pane was last shown in.
    setZoom({mode: preferredMode(H.agentName(H.paneOf(live.current.layout, pane)), phone), scale: 1, forced: false});
    mirror.current.clear();
    refreshRows(H.paneOf(live.current.layout, pane), false);
    if (!pane) {
      setConnection(IDLE);
      return;
    }
    openStream(pane);
  }, [closeStream, openStream, refreshRows, stopTyping]);

  const apply = useCallback((value) => {
    live.current.layout = value;
    setLayout(value);
    setLoadingLayout(false);
    clearTimeout(layoutTimer.current);
    layoutAttempt.current = 0;
    if (value.session) {
      live.current.session = value.session;
      setSelectedSession(value.session);
    }
    setConnection((c) => (c.kind === 'unavailable' ? IDLE : c));
    const wanted = pendingPane.current;
    if (wanted && H.paneOf(value, wanted)) {
      pendingPane.current = null;
      select(wanted);
      return;
    }
    const current = live.current.selected;
    if (!current || !H.paneOf(value, current)) {
      // The pane we were on is gone: fall to what Herdr says is focused.
      select(H.preferredPane(value));
      return;
    }
    // A tab this browser just made: show its pane, without moving the computer.
    const pending = pendingNewTab.current;
    if (pending) {
      const workspace = (value.workspaces || []).find((row) => row.id === pending.workspace);
      const fresh = workspace && (workspace.tabs || []).flatMap((tab) => tab.panes || []).find((pane) => !pending.known.has(pane.id));
      if (fresh) {
        pendingNewTab.current = null;
        select(fresh.id);
        return;
      }
    }
    // The pane changed size on the computer: the mirror follows, at its size.
    const geometry = H.paneGeometry(H.paneOf(value, current));
    if (geometry) {
      mirror.current.resize(geometry.cols, geometry.rows);
      resizeStream(geometry);
    }
    refreshRows(H.paneOf(value, current), false);
  }, [refreshRows, resizeStream, select]);

  const refreshSessions = useCallback(() => {
    if (sessionsBusy.current || !appeared.current) return;
    sessionsBusy.current = true;
    const current = generation.current;
    client.herdrSessions().then((value) => {
      if (generation.current !== current) return;
      setSessions(value.sessions || []);
      live.current.sessions = value.sessions || [];
      live.current.session = value.selected;
      setSelectedSession(value.selected);
      setOwnedSession(value.owned);
      // HERDR-5: the session on screen stopped on the computer: its last
      // picture is not true any more, so it goes, and the stage offers Start.
      if (H.stoppedSession(value.sessions, value.selected)) {
        setNotice(null);
        stopTyping();
        closeStream();
        live.current.selected = null;
        live.current.layout = null;
        setSelected(null);
        setLayout(null);
        setConnection(IDLE);
        mirror.current.clear();
      }
    }).catch(() => {
      if (generation.current === current && !live.current.sessions.length) setSelectedSession(null);
    }).finally(() => {
      sessionsBusy.current = false;
    });
  }, [client, closeStream, stopTyping]);

  const refreshLayout = useCallback(() => {
    if (!appeared.current) return;
    if (layoutBusy.current) {
      // HERDR-11: a change that arrives mid-read gets a read of its own.
      layoutAgain.current = true;
      return;
    }
    layoutBusy.current = true;
    const current = generation.current;
    if (!live.current.layout) setLoadingLayout(true);
    // HERDR-11 §4: `304` when this browser already has this revision.
    client.herdrLayoutIf(live.current.layout ? layoutTag.current : null).then((answer) => {
      if (generation.current !== current || !appeared.current) return;
      setHerdrDown(false);
      layoutTag.current = answer.etag || null;
      if (answer.data) apply(answer.data);
      else setLoadingLayout(false);
    }).catch((error) => {
      if (generation.current !== current || !appeared.current) return;
      setLoadingLayout(false);
      layoutTag.current = null;
      // HERDR-11 §3: the host answered and said Herdr does not - a cause of
      // its own, said in the panel's banner instead of the notice row.
      const down = !!error && error.status === 503 && error.code === 'herdr_unavailable' && healthRef.current === 'live';
      setHerdrDown(down);
      if (!live.current.layout) setConnection({kind: 'unavailable'});
      if (!down && !H.stoppedSession(live.current.sessions, live.current.session)) sayError(error);
      refreshSessions();
      // A failed read asks again, with the stream's own backoff.
      layoutAttempt.current += 1;
      clearTimeout(layoutTimer.current);
      layoutTimer.current = setTimeout(() => refreshLayout(), Math.max(1000, H.reconnectDelay(layoutAttempt.current)));
    }).finally(() => {
      layoutBusy.current = false;
      if (layoutAgain.current) {
        layoutAgain.current = false;
        refreshLayout();
      }
    });
  }, [client, apply, refreshSessions, sayError]);

  const resetForSession = useCallback(() => {
    stopTyping();
    closeStream();
    clearTimeout(layoutTimer.current);
    layoutAttempt.current = 0;
    generation.current += 1;
    live.current.selected = null;
    live.current.layout = null;
    layoutTag.current = null;
    setSelected(null);
    setLayout(null);
    mirror.current.clear();
  }, [closeStream, stopTyping]);

  const switchTo = useCallback((call) => {
    resetForSession();
    setConnection({kind: 'connecting'});
    setSwitching(true);
    const current = generation.current;
    call().then((value) => {
      if (generation.current !== current) return;
      live.current.session = value.selected;
      setSelectedSession(value.selected);
      setOwnedSession(value.owned);
      setSwitching(false);
      setStarting(null);
      setNotice(null);
      refreshLayout();
      refreshSessions();
    }).catch((error) => {
      if (generation.current !== current) return;
      setSwitching(false);
      setStarting(null);
      setConnection({kind: 'unavailable'});
      sayError(error);
      refreshSessions();
    });
  }, [refreshLayout, refreshSessions, resetForSession, sayError]);

  const selectSession = useCallback((name) => {
    if (name === live.current.session || switching) return;
    switchTo(() => client.herdrSelectSession(name));
  }, [client, switchTo, switching]);

  const startSession = useCallback((name) => {
    if (switching) return;
    setStarting(name);
    switchTo(() => client.herdrStartSession(name));
  }, [client, switchTo, switching]);

  const openNewSession = useCallback(() => {
    setNewWorkspace(NO_WORKSPACE_CARD);
    setNewSession({open: true, creating: false, error: null});
  }, []);
  const cancelNewSession = useCallback(() => setNewSession({open: false, creating: false, error: null}), []);
  const clearNewSessionError = useCallback(() => setNewSession((s) => (s.error ? {...s, error: null} : s)), []);

  const createSession = useCallback((typedName) => {
    const name = String(typedName || '').trim();
    if (newSession.creating || switching) return;
    const problem = H.newSessionProblem(name, live.current.sessions);
    if (problem) {
      setNewSession({open: true, creating: false, error: problem});
      return;
    }
    setNewSession({open: true, creating: true, error: null});
    client.herdrCreateSession(name).then((value) => {
      setNewSession({open: false, creating: false, error: null});
      resetForSession();
      live.current.session = value.selected;
      setSelectedSession(value.selected);
      setOwnedSession(value.owned);
      setNotice(null);
      refreshLayout();
      refreshSessions();
    }).catch((error) => {
      setNewSession({open: true, creating: false, error: (error && error.code) || 'herdr_unavailable'});
    });
  }, [client, newSession.creating, refreshLayout, refreshSessions, resetForSession, switching]);

  // ── HERDR-6: a new workspace in a folder ───────────────────────────────
  // The card under the switcher: recent and common folders, a one-level
  // browser and a path field; Create lands this browser in the new
  // workspace's first pane - its own view only, the computer stays put.
  const openNewWorkspace = useCallback(() => {
    setNewSession({open: false, creating: false, error: null});
    setNewWorkspace({...NO_WORKSPACE_CARD, open: true, loading: true});
    client.herdrFolders().then((value) => {
      const extras = folderExtras(value);
      setNewWorkspace((card) => (card.open ? {...card, loading: false, ...H.folderRows(value), ...extras} : card));
      // A launcher remembered from another host, or one this host dropped:
      // a plain shell, never something else.
      setLauncher((chosen) => launcherFallback(extras.launchers, chosen));
    }).catch((error) => {
      // No suggestions is not a dead end: the path field still works.
      setNewWorkspace((card) => (card.open ? {...card, loading: false,
        error: error && error.status === 404 ? null : (error && error.code) || 'herdr_unavailable'} : card));
    });
  }, [client]);

  const cancelNewWorkspace = useCallback(() => setNewWorkspace(NO_WORKSPACE_CARD), []);
  const clearNewWorkspaceError = useCallback(() => setNewWorkspace((card) => (card.error ? {...card, error: null} : card)), []);

  // One level of a folder. Opening one is choosing it: the card's path is
  // whatever folder the browser shows.
  const browseFolder = useCallback((path, hidden) => {
    const problem = H.folderProblem(path);
    if (problem) {
      setNewWorkspace((card) => ({...card, error: problem}));
      return Promise.resolve(null);
    }
    setNewWorkspace((card) => ({...card, browsing: true, error: null}));
    return client.herdrFolders(path, hidden).then((value) => {
      const browse = value && value.browse;
      setNewWorkspace((card) => (card.open ? {...card, browsing: false, browse: browse || null, hidden: !!hidden} : card));
      return browse || null;
    }).catch((error) => {
      setNewWorkspace((card) => (card.open ? {...card, browsing: false, error: (error && error.code) || 'herdr_unavailable'} : card));
      return null;
    });
  }, [client]);

  const closeBrowse = useCallback(() => setNewWorkspace((card) => ({...card, browse: null})), []);

  // HERDR-11 §1: the Start row's choice, remembered in this browser.
  const chooseLauncher = useCallback((command) => {
    setLauncher(command || null);
    storage.setLocal('herdr.launcher', command || null);
  }, []);

  // HERDR-11 §1: a star on a folder row; the host keeps the list for all its
  // devices and answers with the folders document.
  const toggleFavourite = useCallback((folder) => {
    if (!folder || !Array.isArray(newWorkspace.favourites)) return;
    const on = !newWorkspace.favourites.some((row) => row.path === folder.path);
    setNewWorkspace((card) => ({...card, favourites: toggled(card.favourites, folder)}));
    client.herdrFavourite(folder.path, on).then((value) => {
      setNewWorkspace((card) => (card.open ? {...card, ...H.folderRows(value), ...folderExtras(value)} : card));
    }).catch((error) => {
      setNewWorkspace((card) => (card.open ? {...card, favourites: toggled(card.favourites, folder),
        error: (error && error.code) || 'herdr_unavailable'} : card));
    });
  }, [client, newWorkspace.favourites]);

  const createWorkspace = useCallback((path, name) => {
    if (newWorkspace.creating || switching) return;
    const session = live.current.session;
    // HERDR-11: an empty folder is the home folder.
    const typedPath = String(path || '').trim();
    const problem = !session ? 'herdr_unavailable'
      : (typedPath ? H.folderProblem(typedPath) : null) || H.workspaceNameProblem(name);
    if (problem) {
      setNewWorkspace((card) => ({...card, error: problem}));
      return;
    }
    setNewWorkspace((card) => ({...card, creating: true, error: null}));
    const launch = (newWorkspace.launchers || []).find((row) => row.command === launcher) || null;
    client.herdrCreateWorkspace(session, typedPath, String(name || '').trim() || null,
      launch ? launch.command : null).then((value) => {
      setNewWorkspace(NO_WORKSPACE_CARD);
      const pane = value && value.workspace && value.workspace.pane;
      if (value && value.selected && value.selected !== live.current.session) {
        resetForSession();
        live.current.session = value.selected;
        setSelectedSession(value.selected);
        if (value.owned) setOwnedSession(value.owned);
      }
      // The first layout read that has it shows it (`apply`).
      pendingPane.current = pane || null;
      // HERDR-11: a launcher that could not be typed leaves a shell; said once.
      setNotice(value && value.launch && value.launch.sent === false && launch
        ? {key: 'launchNotSent', value: launch.label} : null);
      refreshLayout();
      refreshSessions();
    }).catch((error) => {
      setNewWorkspace((card) => ({...card, creating: false, error: (error && error.code) || 'herdr_unavailable'}));
    });
  }, [client, launcher, newWorkspace.creating, newWorkspace.launchers, refreshLayout, refreshSessions, resetForSession,
    switching]);

  const selectWorkspace = useCallback((id) => {
    const pane = H.workspacePane(live.current.layout, id);
    if (pane) select(pane);
  }, [select]);

  // ── HERDR-9: choices and the empty composer's keys ──────────────────────
  // A tap re-reads the screen first and sends only at the question that was
  // on show; a different one in the same shape is refused, never answered.
  const sendChoiceKeys = useCallback((keys, shown) => {
    const pane = live.current.selected;
    if (!pane) { setNotice({key: 'noSelectedPane'}); return false; }
    const fresh = currentChoices();
    if (!sameChoices(fresh, shown)) {
      choicesRef.current = fresh;
      setChoices(fresh);
      setNotice({key: 'choicesChanged'});
      return false;
    }
    setAnswered(shown.signature);
    clearTimeout(answeredTimer.current);
    answeredTimer.current = setTimeout(() => setAnswered((sig) => (sig === shown.signature ? null : sig)), 4000);
    enqueue(pane, {keys});
    return true;
  }, [currentChoices, enqueue]);
  // HERDR-10: bumped when the composer should take the keyboard (the notes
  // box of codex's question card was just opened from the strip).
  const [noteRequests, setNoteRequests] = useState(0);
  const answer = useCallback((choice) => {
    const shown = choicesRef.current;
    if (!shown) return;
    const option = shown.options.find((o) => o.index === choice.index);
    if (!option) return;
    if (sendChoiceKeys(option.keys, shown) && option.opensNote) setNoteRequests((n) => n + 1);
  }, [sendChoiceKeys]);
  const answerEscape = useCallback(() => {
    const shown = choicesRef.current;
    if (shown && shown.escape) sendChoiceKeys(['Escape'], shown);
  }, [sendChoiceKeys]);
  const passKey = useCallback((key) => {
    const pane = live.current.selected;
    if (!pane) return setNotice({key: 'noSelectedPane'});
    if (live.current.typing) touchTyping();
    enqueue(pane, {keys: [key]});
    return undefined;
  }, [enqueue, touchTyping]);

  // ── keys, composer, Quick, presets, harness ─────────────────────────────
  const sendKey = useCallback((cap) => {
    const pane = live.current.selected;
    if (!pane) return setNotice({key: 'noSelectedPane'});
    if (live.current.typing) touchTyping();
    enqueue(pane, {keys: [cap.key]});
    return undefined;
  }, [enqueue, touchTyping]);

  const finishSend = useCallback((pane, result, failure, clearIf) => {
    if (failure) {
      if (live.current.selected === pane) setSendState({kind: 'failed', code: failure.code || 'herdr_unavailable'});
      return;
    }
    const state = H.sendStateOf(result);
    if (state.kind === 'sent' || state.kind === 'sentLight') {
      if (clearIf !== null) {
        const key = draftKey(pane);
        setDrafts((all) => (all[key] === clearIf ? {...all, [key]: ''} : all));
      }
      if (live.current.selected !== pane) return setSendState(IDLE);
      setSendState(state);
      clearTimeout(sentTimer.current);
      sentTimer.current = setTimeout(() => setSendState((s) => (s.kind === state.kind ? IDLE : s)), H.sentLinger(state));
    } else if (live.current.selected === pane) {
      setSendState(state);
    }
    return undefined;
  }, []);

  const submit = useCallback((text, clearIf = null, attachments = null) => {
    const pane = live.current.selected;
    if (!pane) return setNotice({key: 'noSelectedPane'});
    clearTimeout(sentTimer.current);
    setSendState({kind: 'sending'});
    const write = {text, submit: true};
    if (attachments && attachments.refs.length) write.attachments = attachments.refs;
    enqueue(pane, write, (result, failure) => {
      // ATTACH-1: the chips go once the host typed their paths; a refused or
      // failed send keeps them with the words.
      if (!failure && result && result.delivered && attachments && attachments.onSent) attachments.onSent();
      finishSend(pane, result, failure, clearIf);
    });
    return undefined;
  }, [enqueue, finishSend]);

  const draft = selected ? drafts[draftKey(selected)] || '' : '';
  const draftsRef = useRef(drafts);
  draftsRef.current = drafts;
  const getDraft = useCallback(() => (live.current.selected ? draftsRef.current[draftKey(live.current.selected)] || '' : ''), []);
  const setDraft = useCallback((value) => {
    const pane = live.current.selected;
    if (!pane) return;
    const key = draftKey(pane);
    draftsRef.current = {...draftsRef.current, [key]: value};
    setDrafts((all) => ({...all, [key]: value}));
    setSendState((s) => (s.kind === 'notSubmitted' || s.kind === 'failed' ? IDLE : s));
  }, []);

  // ATTACH-1: `attachments` is `{refs, onSent}` from the chips; core swaps
  // each marker for its file's path.
  const sendDraft = useCallback((attachments = null) => {
    const pane = live.current.selected;
    if (!pane) return setNotice({key: 'noSelectedPane'});
    // Return is the submit, and core is the one that presses it.
    const key = draftKey(pane);
    const text = (drafts[key] || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    const attached = !!(attachments && attachments.refs && attachments.refs.length);
    if ((!text.trim() && !attached) || sendState.kind === 'sending') return undefined;
    if (new TextEncoder().encode(text).length > H.MAX_TEXT_BYTES) {
      return setSendState({kind: 'failed', code: 'herdr_text_too_long'});
    }
    // HERDR-10: codex's notes box is open (the strip's "None of the above"
    // opened it). core types no submitted reply into a pane whose agent is
    // blocked on a question, but this box is where the words belong: the
    // words, then Enter, which submits the answer with its note.
    if (!attached && mirror.current && notesOpen(mirror.current.text())) {
      const typed = drafts[key];
      clearTimeout(sentTimer.current);
      setSendState({kind: 'sending'});
      enqueue(pane, {text, submit: false});
      enqueue(pane, {keys: ['Enter']}, (result, failure) => {
        if (failure) {
          if (live.current.selected === pane) setSendState({kind: 'failed', code: failure.code || 'herdr_unavailable'});
          return;
        }
        setDrafts((all) => (all[key] === typed ? {...all, [key]: ''} : all));
        if (live.current.selected !== pane) return setSendState(IDLE);
        setSendState({kind: 'sent'});
        sentTimer.current = setTimeout(() => setSendState((s) => (s.kind === 'sent' ? IDLE : s)), 4000);
        return undefined;
      });
      return undefined;
    }
    submit(text, drafts[key], attached ? attachments : null);
    return undefined;
  }, [drafts, sendState.kind, submit, enqueue]);

  const isArmed = useCallback((id) => !!armed && armed.id === id && armed.until > Date.now(), [armed]);

  const tapTwice = useCallback((id, asks) => {
    const decision = H.secondTap(armed, id, asks, Date.now());
    setArmed(decision.armed);
    clearTimeout(armTimer.current);
    if (decision.armed) armTimer.current = setTimeout(() => setArmed(null), H.ARM_MS);
    return decision.send;
  }, [armed]);

  const sendQuick = useCallback((text) => {
    setQuickOpen(false);
    submit(text);
  }, [submit]);

  const sendHarness = useCallback((row) => {
    if (!tapTwice('harness:' + row.command, row.confirm)) return;
    setQuickOpen(false);
    submit(row.command);
  }, [submit, tapTwice]);

  const runCommand = useCallback((row) => {
    if (!tapTwice('command:' + row.command, row.confirm)) return;
    setQuickOpen(false);
    if (row.arg_hint) {
      setDraft(row.command + ' ');
      return;
    }
    submit(row.command);
  }, [setDraft, submit, tapTwice]);

  const sendPreset = useCallback((row) => {
    const pane = live.current.selected;
    if (!pane) return setNotice({key: 'noSelectedPane'});
    if (!tapTwice('key:' + row.label, row.danger)) {
      // The cap keeps its size; the line above says what the second tap is for.
      setNotice({key: 'tapAgainToSend', value: row.label});
      return undefined;
    }
    if (row.danger) setNotice(null);
    enqueue(pane, {keys: row.keys});
    return undefined;
  }, [enqueue, tapTwice]);

  const toggleQuick = useCallback(() => {
    setQuickOpen((open) => {
      if (!open) {
        stopTyping();
        refreshRows(H.paneOf(live.current.layout, live.current.selected), true);
      }
      return !open;
    });
    setArmed(null);
  }, [refreshRows, stopTyping]);

  // ── Type into terminal ──────────────────────────────────────────────────
  const startTyping = useCallback(() => {
    if (!live.current.selected) return setNotice({key: 'noSelectedPane'});
    live.current.typing = true;
    setTyping(true);
    setQuickOpen(false);
    touchTyping();
    return undefined;
  }, [touchTyping]);

  const flushTyped = useCallback(() => {
    typedTimer.current = 0;
    const pane = live.current.selected;
    const writes = typed.current;
    typed.current = [];
    if (!pane || !writes.length) return;
    for (const write of H.mergeWrites(writes)) enqueue(pane, write);
  }, [enqueue]);

  const typedWrite = useCallback((write) => {
    if (!live.current.typing) return;
    touchTyping();
    typed.current.push(write);
    // One tick of keystrokes becomes one request.
    if (!typedTimer.current) typedTimer.current = setTimeout(flushTyped, 0);
  }, [flushTyped, touchTyping]);

  const typeKey = useCallback((key) => typedWrite({keys: [key]}), [typedWrite]);
  const typeText = useCallback((text) => {
    for (const write of H.runWrites(text)) typedWrite(write);
  }, [typedWrite]);
  const typeDrop = useCallback(() => setNotice({code: 'herdr_key_unsupported'}), []);

  // ── the More menu ───────────────────────────────────────────────────────
  const perform = useCallback((request) => {
    const current = generation.current;
    const done = (promise) => promise.then(() => {
      if (generation.current !== current) return;
      setNotice(null);
      refreshLayout();
    }).catch((error) => {
      if (generation.current !== current) return;
      sayError(error);
      refreshLayout();
    });
    switch (request.kind) {
      case 'unsupported': return setNotice({key: request.reason});
      case 'newWorkspace': return openNewWorkspace();
      case 'select': return select(request.pane);
      case 'pane':
        if (request.action === 'close' && request.pane === live.current.selected) closeStream();
        return done(client.herdrPaneAction(request.pane, request.action, request.body));
      case 'newTab':
        pendingNewTab.current = {workspace: request.workspace, known: new Set(H.panesOf(live.current.layout).map((p) => p.id))};
        return done(client.herdrNewTab(request.workspace));
      case 'closeTab': {
        const tab = H.tabOf(live.current.layout, live.current.selected);
        if (tab && tab.id === request.tab) closeStream();
        return done(client.herdrCloseTab(request.workspace, request.tab));
      }
      default: return undefined;
    }
  }, [client, closeStream, openNewWorkspace, refreshLayout, sayError, select]);

  // HERDR-8: double tap and the chip go Wrap → Fit → Actual → Wrap, from what
  // is showing (`shown`: a full-screen program shows its grid under Wrap);
  // AGENT-UI-3: Wrap ↔ Fit on a phone.
  const toggleZoom = useCallback((shown) => setZoom((z) => {
    const next = nextMode(typeof shown === 'string' ? shown : z.mode, phone);
    rememberMode(H.agentName(H.paneOf(live.current.layout, live.current.selected)), next);
    return {mode: next, scale: 1, forced: next === 'wrap'};
  }), [phone]);
  const toggleKeys = useCallback(() => setKeysOpen((open) => !open), []);
  const pinchZoom = useCallback((scale) => setZoom({mode: 'custom', scale}), []);

  // ── lifetime ────────────────────────────────────────────────────────────
  useEffect(() => {
    appeared.current = true;
    refreshLayout();
    refreshSessions();
    const wake = () => {
      if (document.visibilityState !== 'visible') {
        // Nothing keeps typing from a phone in a pocket.
        stopTyping();
        return;
      }
      if (live.current.selected && !stream.current) openStream(live.current.selected);
      refreshLayout();
      refreshSessions();
    };
    document.addEventListener('visibilitychange', wake);
    window.addEventListener('online', wake);
    return () => {
      appeared.current = false;
      document.removeEventListener('visibilitychange', wake);
      window.removeEventListener('online', wake);
      generation.current += 1;
      stopTyping();
      closeStream();
      clearTimeout(layoutTimer.current);
      clearTimeout(sentTimer.current);
      clearTimeout(armTimer.current);
      clearTimeout(typedTimer.current);
      clearTimeout(answeredTimer.current);
    };
  }, [closeStream, openStream, refreshLayout, refreshSessions, stopTyping]);

  // core's projection moved (`herdr.layout.changed` on the event stream):
  // the snapshot is re-read, never patched here.
  // HERDR-11 §4: only this panel's own session is worth a read.
  useEffect(() => {
    const n = revision && typeof revision === 'object' ? revision.n : revision;
    if (!n) return;
    const session = revision && typeof revision === 'object' ? revision.session : null;
    if (layoutEventIsMine(session, live.current.session)) refreshLayout();
  }, [revision, refreshLayout]);

  // ── HERDR-11 §3: a mirror not known to be current ───────────────────────
  // A browser cannot ping the observe socket, so "current" is: the stream is
  // open and the app's one clock is live. While it is, the time is kept each
  // second; the moment it is not, the mirror is dimmed with that time.
  const stale = !!selected && mirrorStale({health, observing: connection.kind === 'observing'})
    && connection.kind !== 'connecting';
  const fineAt = useRef(Date.now());
  useEffect(() => {
    if (stale) return undefined;
    fineAt.current = Date.now();
    const timer = setInterval(() => { fineAt.current = Date.now(); }, 1000);
    return () => clearInterval(timer);
  }, [stale]);

  // A push named a session and a pane: go there. OVERVIEW-1: so does a tap
  // on a row of the "Needs you" list (`land`).
  const landRef = useRef(null);
  landRef.current = (place) => {
    if (!place || !place.session) return;
    pendingPane.current = place.pane;
    if (live.current.session && live.current.session !== place.session) {
      switchTo(() => client.herdrSelectSession(place.session));
    } else if (place.pane && live.current.layout && H.paneOf(live.current.layout, place.pane)) {
      pendingPane.current = null;
      select(place.pane);
    } else {
      // The session is not known yet: select it; the first layout lands on the pane.
      if (!live.current.session) switchTo(() => client.herdrSelectSession(place.session));
    }
  };
  const land = useCallback((place) => landRef.current(place), []);
  useEffect(() => {
    const place = H.pushPlace(focus);
    if (place) landRef.current(place);
  }, [focus]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectedPane = H.paneOf(layout, selected);
  return {
    layout, sessions, selectedSession, ownedSession, switching, starting, newSession, newWorkspace, selected, selectedPane,
    connection, loadingLayout, notice, typing, sendState, draft, rows, quickOpen, armed, zoom, phone, firstFrame, keysOpen,
    noteRequests, choices, choicesAnswered: !!answered && !!choices && answered === choices.signature, answer, answerEscape, passKey,
    mirror: mirror.current,
    stopped: H.stoppedSession(sessions, selectedSession),
    select, selectWorkspace, selectSession, land, refreshSessions, startSession, openNewSession, cancelNewSession, createSession,
    clearNewSessionError, openNewWorkspace, cancelNewWorkspace, clearNewWorkspaceError, browseFolder, closeBrowse,
    createWorkspace, sendKey, setDraft, getDraft, sendDraft, sendQuick, sendHarness, runCommand, sendPreset, toggleQuick,
    // HERDR-11
    launcher, chooseLauncher, toggleFavourite, herdrDown, retryLayout: () => refreshLayout(),
    stale, staleAt: fineAt.current,
    isArmed, startTyping, stopTyping, typeKey, typeText, typeDrop, perform, toggleZoom, pinchZoom, toggleKeys,
    dismissNotice: () => setNotice(null), dismissSendState: () => setSendState(IDLE),
    readOnlyTap: () => setNotice({key: 'readOnly'}),
  };
}

export {ApiError};
