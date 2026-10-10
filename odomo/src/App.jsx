import React, {Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {Copy, useCopy} from './copy.js';
import {LANGUAGES, pickLanguage} from './lib/i18n.js';
import {HostClient, apiBase} from './lib/api.js';
import {EventStream, eventsUrl} from './lib/events.js';
import {applyEvent} from './lib/state.js';
import {overviewOf} from './lib/overview.js';
import {PROBE_MS, judge, keepCatalog} from './lib/herdr11.js';
import ConnectionBanner from './ConnectionBanner.jsx';
import {applyTheme} from './lib/theme.js';
import {renewIfDue} from './lib/pairing.js';
import {appLinks, appleTouch, hostIdOf} from './lib/applink.js';
import {openTarget} from './lib/push.js';
import {barPanels} from './lib/panels.js';
import {agentFaceState} from './lib/agentFace.js';
import * as storage from './lib/storage.js';
import {migrate, moveLocal} from './lib/migrate.js';
import {useLayout} from './layout.js';
import {readInstall} from './lib/install.js';
import PairScreen from './PairScreen.jsx';
import InstallScreen from './Install.jsx';
import Sidebar from './Sidebar.jsx';
import MenuPanel from './panels/MenuPanel.jsx';
import NotificationsPanel, {notificationEvent} from './panels/NotificationsPanel.jsx';
import SettingsPanel from './panels/SettingsPanel.jsx';
import Tour from './Tour.jsx';
import {START_DELAY_MS, armTour, finishTour, nextStop, replayTour, startTour, tourPending} from './lib/tour.js';
import NextPanel from './panels/NextPanel.jsx';
import AgentPanel from './panels/AgentPanel.jsx';
// ODOMO-2: the Herdr panel carries the terminal core (@xterm/headless); it
// is its own chunk, fetched the first time the panel opens.
const HerdrPanel = lazy(() => import('./panels/HerdrPanel.jsx'));

// Odomo: the host's own web app (LITE-1a).
//
// Two screens. Until this browser holds a credential it is the pairing page
// (PairScreen). After that it is the App's layout - the bar down the left
// edge and one panel: panel 1 (the Omarchy menu and the keybindings), and
// the App's entries in the App's order (A-50: Remote / Agent / Herdr / SSH /
// Settings / Notifications) less what Odomo does not have - SSH, which a
// browser cannot open (ODOMO-2 report), and Remote's screen, which stays the
// App's: Remote is a link into it (TAIL-3b L8; on iPhone and iPad the bar's
// Remote is that link itself, LITE-1c). Agent and Herdr reply like the App's
// (ODOMO-2). Every word is a text node; nothing the host sends is ever
// parsed as HTML.



export default function App() {
  const [override, setOverride] = useState(() => {
    // LITE-1c: the old names first, so a language chosen under Lite still holds.
    moveLocal(typeof localStorage === 'undefined' ? null : localStorage);
    return storage.local('language', null);
  });
  const lang = pickLanguage(typeof navigator !== 'undefined' ? navigator.languages : [], override);
  const copy = useMemo(() => ({t: LANGUAGES[lang], lang}), [lang]);
  useEffect(() => {
    document.documentElement.lang = lang === 'zh-Hans' ? 'zh-Hans' : 'en';
  }, [lang]);

  const base = useMemo(() => apiBase(document.baseURI), []);
  // LITE-1e: read once. On iPhone and iPad outside the Home Screen app this is
  // 'block': the install screen is the whole page and a pairing kept in this
  // tab is not used (a UX gate, not a security control - core is unchanged).
  const [install] = useState(() => readInstall(typeof window === 'undefined' ? null : window));
  const [hadPairing, setHadPairing] = useState(false);
  const [phase, setPhase] = useState('loading');
  const [credential, setCredential] = useState(null);
  const [lost, setLost] = useState(null);

  // A refused credential, from any request or the event stream: forget it
  // here, completely, and say so.
  const unpair = useCallback(async (reason) => {
    await storage.del('credential');
    setCredential(null);
    setLost(reason || 'credential_revoked');
    setPhase('unpaired');
  }, []);

  useEffect(() => {
    let live = true;
    // LITE-1c: a browser paired while Odomo was Lite keeps its pairing.
    migrate({store: storage, idb: typeof indexedDB === 'undefined' ? null : indexedDB,
      local: typeof localStorage === 'undefined' ? null : localStorage,
      container: 'serviceWorker' in navigator ? navigator.serviceWorker : null})
      .catch(() => null)
      .then(() => storage.get('credential'))
      .then((value) => {
        if (!live) return;
        if (install.gate === 'block') {
          setHadPairing(!!(value && typeof value.token === 'string'));
          setPhase('install');
          return;
        }
        if (value && typeof value.token === 'string') {
          setCredential(value);
          setPhase('ready');
        } else setPhase('unpaired');
      });
    return () => {
      live = false;
    };
  }, [install.gate]);

  const paired = useCallback(async (value) => {
    await storage.set('credential', value);
    setLost(null);
    setCredential(value);
    setPhase('ready');
  }, []);

  // TUTORIAL-1: a pairing that landed (not a renewal of one) arms the tour.
  const pairedFresh = useCallback(async (value) => {
    armTour();
    await paired(value);
  }, [paired]);

  const chooseLanguage = (value) => {
    storage.setLocal('language', value);
    setOverride(value);
  };

  return (
    <Copy.Provider value={copy}>
      {phase === 'loading' ? null : phase === 'install' ? (
        <InstallScreen device={install.device} hadPairing={hadPairing} />
      ) : phase === 'ready' && credential ? (
        <Shell base={base} credential={credential} onCredential={paired} onUnpair={unpair}
          language={override} onLanguage={chooseLanguage} install={install} />
      ) : (
        <PairScreen base={base} lost={lost} onPaired={pairedFresh} install={install} />
      )}
    </Copy.Provider>
  );
}

function Shell({base, credential, onCredential, onUnpair, language, onLanguage, install}) {
  const {t} = useCopy();
  const layout = useLayout();
  // LITE-1b: a notification's URL (or the worker, for an Odomo already open)
  // says which panel to open, by id.
  const [opened] = useState(() => openTarget(location.search));
  const [panel, setPanel] = useState(() => (opened ? opened.panel : 'menu'));
  const [focus, setFocus] = useState(() => (opened ? opened.ids : null));
  const [column, setColumn] = useState('menu');
  const [status, setStatus] = useState('connecting');
  const [state, setState] = useState(null);
  const [catalog, setCatalog] = useState(null);
  const [shortcuts, setShortcuts] = useState(null);
  const [notifications, setNotifications] = useState(null);
  const [dnd, setDnd] = useState(false);
  const [build, setBuild] = useState(null);
  // ODOMO-2: the host theme (the Herdr mirror's palette), core's Herdr
  // projection moving (`herdr.layout.changed`), and the bar's switches.
  const [theme, setTheme] = useState(null);
  // HERDR-11 §4: `herdr.layout.changed` with the session it was about, so
  // the Herdr panel re-reads only for its own.
  const [herdrRevision, setHerdrRevision] = useState({n: 0, session: null});
  // HERDR-11 §3: the one connection clock - every event-socket message and
  // every answered probe stamps it; amber at 4 s, red at 15 s.
  const [health, setHealth] = useState('live');
  const lastLive = useRef(Date.now());
  const everLive = useRef(false);
  const streamRef = useRef(null);
  const catalogTag = useRef(null);
  const [barOff, setBarOff] = useState(() => storage.local('bar.off', []));
  const [showUnrunnable, setShowUnrunnable] = useState(() => storage.local('keys.unrunnable', false) === true);
  // TAIL-3b L8: the links into the app carry this host's id. The claim gave
  // it; a browser paired before that reads it once from `/health`.
  const [healthHostId, setHealthHostId] = useState(null);
  const hostId = hostIdOf(credential.host_id) || healthHostId;
  // LITE-1c: on iPhone and iPad, Remote in the bar is the link into the app.
  const [touchApple] = useState(() => appleTouch(typeof navigator !== 'undefined' ? navigator : null));
  const remoteHref = touchApple ? appLinks('remote', hostId).universal : null;
  const onUnpairRef = useRef(onUnpair);
  onUnpairRef.current = onUnpair;

  const client = useMemo(() => new HostClient({base, token: credential.token,
    onUnpaired: (reason) => onUnpairRef.current(reason)}), [base, credential.token]);

  const refresh = useMemo(() => ({
    theme: () => client.theme().then((value) => {
      applyTheme(value);
      setTheme(value);
    }).catch(() => {}),
    // HERDR-11 §4: the catalog read again only when it moved (`304`).
    catalog: () => client.catalogIf(catalogTag.current).then((answer) => {
      if (answer.etag) catalogTag.current = answer.etag;
      if (answer.data) setCatalog((held) => keepCatalog(held, answer.data));
    }).catch(() => {}),
    shortcuts: () => client.shortcuts().then(setShortcuts).catch(() => {}),
    notifications: () => client.notifications().then((value) => setNotifications(value.notifications || [])).catch(() => {}),
    dnd: () => client.dnd().then((value) => setDnd(value.dnd === true)).catch(() => {}),
  }), [client]);

  // The event stream, and the reads it tells us to do again.
  useEffect(() => {
    const stream = new EventStream({
      ticket: () => client.ticket('/v1/events'),
      url: (ticket, resume) => eventsUrl(base, ticket, resume),
      WebSocketImpl: WebSocket,
      onStatus: setStatus,
      onUnpaired: (reason) => onUnpairRef.current(reason),
      onMessage: (message) => {
        lastLive.current = Date.now();
        everLive.current = true;
        if (message.type === 'snapshot') {
          setState(message.state);
          if (message.state && message.state.catalog) setCatalog((held) => keepCatalog(held, message.state.catalog));
          // A fresh snapshot can follow a gap: read the lists again.
          refresh.notifications();
          refresh.shortcuts();
          refresh.dnd();
          return;
        }
        const event = message.event;
        if (!event) return;
        // LITE-1b (NEXT-1 §6.1): dismissed on the desktop or by another
        // client - the row stays, as history, like everywhere else. NOTIFY-3:
        // unless it is a request that was decided (`withdrawn`) - then it goes.
        if (event.type === 'notification.posted' || event.type === 'notification.updated') {
          setNotifications((rows) => notificationEvent(rows, event));
        }
        if (event.type === 'notifications.changed') {
          refresh.notifications();
          refresh.dnd();
        }
        if (event.type === 'catalog.changed') {
          refresh.catalog();
          refresh.shortcuts();
        }
        if (event.type === 'theme.changed') refresh.theme();
        if (event.type === 'herdr.layout.changed') {
          const session = event.payload && typeof event.payload.session === 'string' ? event.payload.session : null;
          setHerdrRevision((r) => ({n: r.n + 1, session}));
        }
        setState((current) => applyEvent(current, event));
      },
    });
    stream.start();
    streamRef.current = stream;
    const wake = () => {
      if (document.visibilityState === 'visible') stream.kick();
    };
    document.addEventListener('visibilitychange', wake);
    window.addEventListener('online', wake);
    window.addEventListener('pageshow', wake);
    return () => {
      document.removeEventListener('visibilitychange', wake);
      window.removeEventListener('online', wake);
      window.removeEventListener('pageshow', wake);
      stream.stop();
      streamRef.current = null;
    };
  }, [client, base, refresh]);

  // HERDR-11 §3. The event socket's own heartbeat is every 15 s, too slow for
  // a 4 s clock, and a browser cannot ping a WebSocket. So while the page is
  // visible and nothing has proved the host alive for 2 s, one
  // `GET /health` (no credential, a few hundred bytes) asks; a hidden page
  // asks nothing. The network coming back reconnects at once (`online`).
  useEffect(() => {
    lastLive.current = Date.now();
    let probing = false;
    const tick = () => {
      const now = Date.now();
      const visible = typeof document === 'undefined' || document.visibilityState === 'visible';
      if (!visible) {
        lastLive.current = now;
        setHealth('live');
        return;
      }
      if (!probing && now - lastLive.current >= PROBE_MS) {
        probing = true;
        client.health().then(() => {
          lastLive.current = Date.now();
          everLive.current = true;
        }).catch(() => {}).finally(() => {
          probing = false;
        });
      }
      setHealth(judge(now, lastLive.current, typeof navigator === 'undefined' || navigator.onLine !== false));
    };
    const timer = setInterval(tick, 1000);
    const online = () => {
      if (streamRef.current) streamRef.current.kick();
      tick();
    };
    window.addEventListener('online', online);
    window.addEventListener('offline', tick);
    return () => {
      clearInterval(timer);
      window.removeEventListener('online', online);
      window.removeEventListener('offline', tick);
    };
  }, [client]);
  const retryNow = useCallback(() => {
    if (streamRef.current) streamRef.current.kick();
    client.health().then(() => {
      lastLive.current = Date.now();
      setHealth('live');
    }).catch(() => {});
  }, [client]);
  const hostName = (state && state.host && state.host.name) || (typeof location !== 'undefined' ? location.hostname : '');

  // A tap on a notification while Odomo is already open (`sw.js` posts it).
  useEffect(() => {
    if (!('serviceWorker' in navigator)) return undefined;
    const listen = (message) => {
      const data = message.data;
      if (!data || data.type !== 'omodachi-open' || typeof data.url !== 'string') return;
      let target = null;
      try {
        target = openTarget(new URL(data.url, document.baseURI).search);
      } catch {
        target = null;
      }
      if (target) {
        setPanel(target.panel);
        setFocus(target.ids);
      }
    };
    navigator.serviceWorker.addEventListener('message', listen);
    return () => navigator.serviceWorker.removeEventListener('message', listen);
  }, []);

  useEffect(() => {
    refresh.theme();
    refresh.notifications();
    refresh.shortcuts();
    refresh.dnd();
    // The build of the page that is running, from the host's own header.
    fetch(new URL('./', document.baseURI).href, {method: 'HEAD', cache: 'no-store', credentials: 'omit'})
      .then((response) => setBuild(response.headers.get('X-Omodachi-Odomo-Build')))
      .catch(() => {});
  }, [refresh]);

  useEffect(() => {
    if (hostIdOf(credential.host_id)) return undefined;
    let live = true;
    client.health().then((value) => {
      if (live) setHealthHostId(hostIdOf(value && value.host_id));
    }).catch(() => {});
    return () => {
      live = false;
    };
  }, [client, credential.host_id]);

  // CORE-2 §1 renewal: on open and every six hours while open.
  useEffect(() => {
    let live = true;
    const renew = () => renewIfDue(client, credential).then((next) => {
      if (live && (next.token !== credential.token || next.expires_at !== credential.expires_at)) onCredential(next);
    }).catch(() => {});
    renew();
    const timer = setInterval(renew, 6 * 3600 * 1000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [client, credential, onCredential]);

  // MOTION-1: the agent's face, from what core says in `state.agent`
  // (`agent.changed`). On the bar a finished or failed turn keeps its face
  // until the Agent panel has been open while it was the latest turn.
  const agent = state && state.agent ? state.agent : null;
  const faceInput = {connected: status !== 'offline', chat: agent && agent.chat ? agent.chat : null,
    agentStatus: agent ? agent.status : null};
  const seenKey = 'agent.seen.' + (hostId || 'host');
  const [seenTurn, setSeenTurn] = useState(() => storage.local(seenKey, null));
  useEffect(() => setSeenTurn(storage.local(seenKey, null)), [seenKey]);
  const panelFace = agentFaceState({...faceInput, surface: 'header'});
  useEffect(() => {
    if (panel !== 'agent' || !panelFace.turn || panelFace.turn === seenTurn) return;
    if (panelFace.state !== 'done' && panelFace.state !== 'error') return;
    storage.setLocal(seenKey, panelFace.turn);
    setSeenTurn(panelFace.turn);
  }, [panel, panelFace.turn, panelFace.state, seenTurn, seenKey]);
  const barFace = agentFaceState({...faceInput, seenTurn, surface: 'odomo'});

  const open = (id) => {
    setFocus(null);
    setPanel((current) => (current === id ? 'menu' : id));
  };
  const props = {client, state, layout, hostId};
  const panels = barPanels(barOff);

  // TUTORIAL-1: the tour. An armed one starts on panel 1 (where a pairing
  // lands), never over a panel the person already went into; going into a
  // panel - a tap on the bar, a push - ends it as seen, like Skip and Done.
  const [tour, setTour] = useState(null);
  const tourPanel = useRef(panel);
  useEffect(() => {
    if (tour || panel !== 'menu' || !tourPending()) return undefined;
    const timer = setTimeout(() => setTour(startTour(panels)), START_DELAY_MS);
    return () => clearTimeout(timer);
  }, [panel, tour, panels.join()]);
  useEffect(() => {
    if (tourPanel.current === panel) return;
    tourPanel.current = panel;
    if (tour) {
      finishTour();
      setTour(null);
    }
  }, [panel, tour]);
  const endTour = useCallback(() => {
    finishTour();
    setTour(null);
  }, []);
  const chooseBar = (id, on) => {
    const next = on ? barOff.filter((x) => x !== id) : Array.from(new Set(barOff.concat(id)));
    storage.setLocal('bar.off', next);
    setBarOff(next);
  };
  let view;
  if (panel === 'agent') {
    view = <AgentPanel client={client} base={base} hostId={hostId} focus={focus} face={panelFace} />;
  } else if (panel === 'herdr') {
    view = (
      <Suspense fallback={<div className="hempty"><p className="busy">{t.herdr.loading}</p></div>}>
        <HerdrPanel client={client} base={base} hostId={hostId} theme={theme} revision={herdrRevision} focus={focus}
          health={health} hostName={hostName} overview={overviewOf(state)} onAgent={() => {
            setFocus(null);
            setPanel('agent');
          }} />
      </Suspense>
    );
  } else if (panel === 'notifications') {
    view = <NotificationsPanel {...props} rows={notifications} dnd={dnd}
      onDnd={(on) => client.dnd(on).then((value) => setDnd(value.dnd === true)).catch(() => {})} highlight={focus && focus.note} />;
  } else if (panel === 'settings') {
    view = <SettingsPanel {...props} client={client} status={status} credential={credential} build={build} hostId={hostId}
      language={language} onLanguage={onLanguage} onForget={() => onUnpair('forgotten')} install={install}
      barOff={barOff} onBar={chooseBar} onTour={() => setTour(replayTour(panels))} showUnrunnable={showUnrunnable} onShowUnrunnable={(on) => {
        storage.setLocal('keys.unrunnable', on);
        setShowUnrunnable(on);
      }} />;
  } else if (panel === 'remote') {
    view = <NextPanel which="remote" hostId={hostId} />;
  } else {
    view = <MenuPanel {...props} catalog={catalog} shortcuts={shortcuts} column={column} onColumn={setColumn}
      showUnrunnable={showUnrunnable}
      onKeybindings={() => setColumn('keys')} />;
  }
  return (
    <div className="app" data-dev={layout.device} data-panel={panel} data-status={status} data-testid="app">
      <div className="layer" data-testid="app-layer">
        <Sidebar t={t} panel={panel} panels={panels} status={status} state={state} notifications={notifications}
          dnd={dnd} named={layout.named} remoteHref={remoteHref} agentFace={barFace} onLogo={() => setPanel('menu')} onPanel={open}
          onWorkspace={(id) => client.selectWorkspace(id).catch(() => {})} />
        <main className="stage" key={panel}>
          <ConnectionBanner health={health} host={hostName} everLive={everLive.current} onRetry={retryNow} />
          {view}
        </main>
      </div>
      {tour ? <Tour state={tour} onNext={() => setTour((current) => nextStop(current))} onSkip={endTour} /> : null}
    </div>
  );
}
