import React, {useEffect, useRef, useState} from 'react';
import Icon, {Symbol} from './Icon.jsx';
import AgentFace from './AgentFace.jsx';
import {codeOf, faceWord, isVendor} from './lib/agentFace.js';
import {fill} from './lib/i18n.js';
import {workspaces} from './lib/state.js';
import {unreadCount} from './panels/NotificationsPanel.jsx';
import {herdrNeedsYou, overviewOf} from './lib/overview.js';

// The App's one bar on the left edge (omodachi-ios `Shell/BarView.swift`),
// adapted from omodachi-web `src/app/Sidebar.jsx` at main 0da28c9: the logo
// (panel 1: the Omarchy menu and the keybindings), the connection square, the
// host's workspaces, then Odomo's entries. The workspaces are the host's own
// rows from the event stream, and a tap selects one on the host.
//
// LITE-1c, after Leo missed Remote on his iPad: the icons say what they are.
// Where the bar has room (`layout.named`: an iPad or a desktop window) each
// entry carries its name in small type under the icon. Everywhere, a pointer
// resting on an entry, keyboard focus, or touching and holding one shows its
// full name beside the bar - and a hold never also opens it. On iPhone and
// iPad, Remote is not a panel but the link into the app (`remoteHref`), with a
// small corner mark to say it leaves Odomo.

export const GLYPH = {herdr: 'stack', agent: 'robot', remote: 'display', settings: 'gear', notifications: 'bell'};
export const HOLD_MS = 450;
export const HOVER_MS = 400;
export const PEEK_MS = 1500;

// The bubble's behaviour, shared by every entry. `show(el, text)` puts the
// name beside `el`; `hide()` takes it away.
export function usePeek() {
  const [peek, setPeek] = useState(null);
  const wait = useRef(0);
  const linger = useRef(0);
  const origin = useRef(null);
  const swallow = useRef(false);
  useEffect(() => () => {
    clearTimeout(wait.current);
    clearTimeout(linger.current);
  }, []);
  const show = (el, text) => {
    const box = el.getBoundingClientRect();
    clearTimeout(linger.current);
    setPeek({text, top: Math.round(box.top + box.height / 2), left: Math.round(box.right + 6)});
  };
  const hide = () => {
    clearTimeout(wait.current);
    clearTimeout(linger.current);
    setPeek(null);
  };
  const handlers = (text) => ({
    onPointerDown: (event) => {
      swallow.current = false;
      if (event.pointerType === 'mouse') return;
      clearTimeout(wait.current);
      origin.current = {x: event.clientX, y: event.clientY};
      const el = event.currentTarget;
      wait.current = setTimeout(() => {
        swallow.current = true;
        show(el, text);
        linger.current = setTimeout(() => setPeek(null), PEEK_MS);
      }, HOLD_MS);
    },
    onPointerMove: (event) => {
      const from = origin.current;
      if (from && Math.hypot(event.clientX - from.x, event.clientY - from.y) > 10) clearTimeout(wait.current);
    },
    onPointerUp: () => clearTimeout(wait.current),
    onPointerCancel: () => clearTimeout(wait.current),
    onPointerEnter: (event) => {
      if (event.pointerType !== 'mouse') return;
      const el = event.currentTarget;
      clearTimeout(wait.current);
      wait.current = setTimeout(() => show(el, text), HOVER_MS);
    },
    onPointerLeave: (event) => {
      if (event.pointerType === 'mouse') hide();
    },
    onFocus: (event) => {
      const el = event.currentTarget;
      if (el.matches && el.matches(':focus-visible')) show(el, text);
    },
    onBlur: hide,
    // A hold showed the name: the release that follows is not a tap.
    onClickCapture: (event) => {
      if (swallow.current) {
        swallow.current = false;
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      hide();
    },
    // Android's long-press menu (and iOS's link preview, see .sbar a) would
    // cover the name.
    onContextMenu: (event) => event.preventDefault(),
  });
  return {peek, handlers, hide};
}

function Slot({label, peekText, name, value, on, badge, count, href, onClick, testid, handlers, children}) {
  const inner = (
    <>
      <span className="bslot__ic">{children}</span>
      {name ? <span className="bslot__name" aria-hidden="true">{name}</span> : null}
      {value ? <span className="sr">{value}</span> : null}
      {badge ? <i className="bslot__badge" style={{background: 'var(--om-' + badge + ')'}} /> : null}
      {/* OVERVIEW-1: how many panes wait on you in Herdr, every session. */}
      {count ? <i className="bslot__count" aria-hidden="true" data-testid={testid + '-count'}>{count > 99 ? '99+' : count}</i> : null}
      {href ? <i className="bslot__out" aria-hidden="true" /> : null}
    </>
  );
  const common = {className: 'bslot' + (on ? ' on' : '') + (href ? ' bslot--out' : ''), 'aria-label': label,
    'data-testid': testid, ...handlers(peekText || label)};
  if (href) {
    // A real anchor, so the tap that follows it is the user's own: that is
    // what lets iOS hand a universal link to the app (LITE-0, TAIL-3b).
    return (
      <a {...common} href={href} target="_blank" rel="noopener noreferrer" data-link="app">{inner}</a>
    );
  }
  return (
    <button type="button" {...common} aria-pressed={on} onClick={onClick}>{inner}</button>
  );
}

// MOTION-1: the Agent entry is the agent's own mark while nothing is going on
// (idle; dimmed when offline) and our animated face for thinking / working /
// waiting / done / error (done and error until the Agent panel has shown them).
function AgentGlyph({face, t}) {
  const state = face ? face.state : 'idle';
  if (isVendor(codeOf('odomo', state))) {
    return (
      <span className={'bslot__vendor' + (state === 'offline' ? ' bslot__vendor--dim' : '')} data-testid="agent-vendor"
        data-state={state}><Icon name={GLYPH.agent} size={22} /></span>
    );
  }
  return <AgentFace surface="odomo" state={state} tool={!!face.tool} size={22} testid="agent-face-bar" />;
}

export default function Sidebar({t, panel, panels, status, state, notifications, dnd, named = false, remoteHref = null,
  agentFace = null, onLogo, onPanel, onWorkspace}) {
  const unread = dnd ? 0 : unreadCount(notifications);
  const rows = workspaces(state);
  const waiting = herdrNeedsYou(overviewOf(state));
  const statusText = status === 'connected' ? t.bar.connected : status === 'offline' ? t.bar.offline : t.bar.connecting;
  const {peek, handlers, hide} = usePeek();
  return (
    <nav className={'sbar' + (named ? ' sbar--named' : '')} aria-label={t.bar.label} data-testid="app-bar"
      onScroll={hide}>
      <div className="sbar__seg">
        <Slot label={t.bar.logo} name={named ? t.bar.names.menu : null} on={panel === 'menu'} testid="bar-logo"
          onClick={onLogo} handlers={handlers}>
          <span className="sbar__logo"><Symbol size={22} /></span>
        </Slot>
        <div className="sbar__conn" role="img" aria-label={t.bar.connection + ': ' + statusText} data-testid="bar-connection"
          data-status={status}>
          <i data-status={status} />
        </div>
        <div className="sbar__ws" role="group" aria-label={t.bar.workspaces}>
          {rows.map((row) => (
            <button key={row.id} type="button"
              className={'ws' + (row.active ? ' sel' : '') + (!row.occupied && !row.active ? ' vacant' : '')}
              aria-label={fill(t.bar.workspace, row.id)} aria-pressed={!!row.active} data-testid={'workspace-' + row.id}
              onClick={() => onWorkspace(row.id)}>
              <i>{row.id}</i>
              <span className="sr">{row.active ? t.bar.current : row.occupied ? t.bar.occupied : t.bar.empty}</span>
            </button>
          ))}
        </div>
      </div>
      <span className="grow" />
      <div className="sbar__seg" role="group" aria-label={t.bar.panels}>
        {panels.map((id) => {
          const out = id === 'remote' && remoteHref ? remoteHref : null;
          return (
            <Slot key={id} label={out ? fill(t.bar.opensApp, t.panels[id]) : t.panels[id]}
              name={named ? t.bar.names[id] : null} on={!out && panel === id} testid={'bar-' + id} href={out}
              value={id === 'notifications' && unread ? fill(t.bar.unread, unread)
                : id === 'agent' ? fill(t.face.label, faceWord(t, agentFace))
                  : id === 'herdr' && waiting ? fill(t.overview.needsYouBadge, waiting) : undefined}
              count={id === 'herdr' ? waiting : 0}
              badge={id === 'notifications' && unread ? 'accent' : undefined} onClick={() => onPanel(id)}
              handlers={handlers}>
              {id === 'agent' ? <AgentGlyph face={agentFace} t={t} />
                : <Icon name={id === 'notifications' && dnd ? 'moon' : GLYPH[id]} size={22} />}
            </Slot>
          );
        })}
      </div>
      {peek ? (
        <div className="bpeek" role="tooltip" data-testid="bar-peek" style={{top: peek.top + 'px', left: peek.left + 'px'}}>
          {peek.text}
        </div>
      ) : null}
    </nav>
  );
}
