import React, {useEffect, useLayoutEffect, useRef, useState} from 'react';
import OnceHint from '../OnceHint.jsx';
import Icon from '../Icon.jsx';
import {GLYPH} from '../Sidebar.jsx';
import LinkNotice from '../LinkNotice.jsx';
import {openHere, useHostOpen} from '../lib/useHostOpen.js';
import {faceWord, paneFaceState} from '../lib/agentFace.js';
import {useCopy} from '../copy.js';
import {fill} from '../lib/i18n.js';
import * as H from '../lib/herdr.js';
import {useHerdr} from '../lib/useHerdr.js';
import {scaleFor} from '../lib/term.js';
import {layout as wrapLayout} from '../lib/wrap.js';
import {passthroughKey} from '../lib/choices.js';
import {detect} from '../lib/links.js';
import {appLinks} from '../lib/applink.js';
import {herdrReason} from '../lib/reasons.js';
import {useAttachments} from '../lib/useAttachments.js';
import {AttachButton, AttachmentChips, AttachNotice, DropVeil, useDropTarget} from './Attachments.jsx';
import ConnectionBanner from '../ConnectionBanner.jsx';
import {clock, isFavourite} from '../lib/herdr11.js';
import * as O from '../lib/overview.js';

// ODOMO-2: panel ④, Herdr - the App's (omodachi-ios herdr-4
// `Surfaces/HerdrPanel/HerdrPanelView.swift`, `HerdrControlBar.swift`,
// `HerdrQuickDock.swift`).
//
// A touch development bench: the session switcher on top; the workspaces and
// their panes as a column (wide) or a strip (narrow); the selected pane's
// screen, fitted to the width; under it Type's strip, the Quick dock, what
// the last send did and the composer; then the keys and the bar with Type,
// Quick, the agent's own commands and the labelled More menu.

const WIDE = 700;
// The keys row, presets included, needs about this much to sit on one line.
const KEYS_ONE_ROW = 900;
const GRID = 248;

function noticeText(notice, t) {
  if (!notice) return null;
  if (notice.code) return herdrReason(t, notice.code);
  const text = t.herdr[notice.key] || '';
  return notice.value !== undefined ? fill(text, notice.value) : text;
}

function modeLabel(store, t) {
  if (store.typing) return t.herdr.modeTyping;
  const c = store.connection;
  if (c.kind === 'observing') return t.herdr.modeObserve;
  if (c.kind === 'connecting') return '…';
  if (c.kind === 'retrying') return fill(t.herdr.modeRetry, c.attempt);
  if (c.kind === 'unavailable') return t.herdr.modeOffline;
  return '—';
}

function useWidth(ref) {
  const [size, setSize] = useState({width: 0, height: 0});
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const read = () => setSize({width: el.clientWidth, height: el.clientHeight});
    read();
    if (typeof ResizeObserver !== 'function') return undefined;
    const observer = new ResizeObserver(read);
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return size;
}

// A small menu that opens above or below its button, as a list of rows.
function Menu({label, testid, open, onToggle, up = false, children, buttonClass = 'fbtn', face}) {
  const wrap = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const away = (event) => {
      if (wrap.current && !wrap.current.contains(event.target)) onToggle(false);
    };
    document.addEventListener('pointerdown', away);
    return () => document.removeEventListener('pointerdown', away);
  }, [open, onToggle]);
  return (
    <div className="hmenu" ref={wrap}>
      <button type="button" className={buttonClass} aria-haspopup="menu" aria-expanded={open} aria-label={label}
        data-testid={testid} onClick={() => onToggle(!open)}>{face}</button>
      {open ? <div className={'hmenu__list' + (up ? ' up' : '')} role="menu">{children}</div> : null}
    </div>
  );
}

function MenuItem({title, detail, glyph, destructive, onClick, testid, selected}) {
  return (
    <button type="button" role="menuitem" className={'hmenu__item' + (destructive ? ' danger' : '') + (selected ? ' on' : '')}
      data-testid={testid} onClick={onClick}>
      {glyph ? <Icon name={glyph} size={14} /> : <i className="hmenu__sp" />}
      <span className="hmenu__tx">
        <span>{title}</span>
        {detail ? <span className="hmenu__d">{detail}</span> : null}
      </span>
    </button>
  );
}

function SessionSwitcher({store, t, trailing, overview, overviewOpen, onOverview}) {
  const [open, setOpen] = useState(false);
  const title = store.selectedSession || store.ownedSession || 'Herdr';
  const detail = H.sessionDetail(store.sessions, store.selectedSession, {starting: store.starting, switching: store.switching}, t);
  return (
    <div className="hsess">
      {/* The list is read again as it opens: a session stopped or started on
          the computer since is shown as it is now. */}
      <Menu label={t.herdr.session} testid="herdr-session-switcher" open={open} onToggle={(next) => {
        if (next) store.refreshSessions();
        setOpen(next);
      }}
        buttonClass="hsess__btn" face={<><b>{title}</b><Icon name="down" size={12} /></>}>
        {store.sessions.map((session) => (session.running ? (
          <MenuItem key={session.name} title={session.name} selected={session.name === store.selectedSession}
            detail={overview ? O.countsText(t, O.sessionCounts(overview, session.name)) || undefined : undefined}
            testid={'herdr-session-' + session.name} onClick={() => {
              setOpen(false);
              if (session.readable !== false) store.selectSession(session.name);
            }} />
        ) : (
          <MenuItem key={session.name} glyph="play" title={fill(t.herdr.sessionStart, session.name)}
            detail={t.herdr.sessionStartDetail} testid={'herdr-session-start-' + session.name} onClick={() => {
              setOpen(false);
              store.startSession(session.name);
            }} />
        )))}
        <i className="hmenu__rule" />
        <MenuItem glyph="install" title={t.herdr.sessionNew} detail={t.herdr.sessionNewDetail} testid="herdr-new-session-open"
          onClick={() => {
            setOpen(false);
            store.openNewSession();
          }} />
      </Menu>
      <span className={'hsess__d' + (store.starting ? ' busy' : '')} data-testid="herdr-session-detail">{detail}</span>
      {/* OVERVIEW-1: what wants you across the Agent and every session, and
          the way into the list. Nothing to show: no button. */}
      {overview && O.rowsOf(overview).length ? (
        <button type="button" className={'hsum' + (overview.counts && overview.counts.needs_you > 0 ? ' hsum--needs' : '')
          + (overviewOpen ? ' on' : '')} aria-expanded={!!overviewOpen} aria-label={t.overview.open}
          title={t.overview.openHint} data-testid="herdr-overview-open" onClick={onOverview}>
          {O.countsText(t, overview.counts)}
        </button>
      ) : null}
      {trailing}
    </div>
  );
}

function NewSessionCard({store, t}) {
  const [name, setName] = useState('');
  const input = useRef(null);
  useEffect(() => {
    if (input.current) input.current.focus();
  }, []);
  const state = store.newSession;
  return (
    <div className="hnew" data-testid="herdr-new-session">
      <b>{t.herdr.newTitle}</b>
      <p>{t.herdr.newDetail}</p>
      <input ref={input} className="hnew__in" value={name} placeholder={t.herdr.newPlaceholder} aria-label={t.herdr.newTitle}
        autoCapitalize="off" autoCorrect="off" spellCheck={false} autoComplete="off" inputMode="url"
        data-testid="herdr-new-session-name"
        onChange={(event) => {
          setName(event.target.value);
          store.clearNewSessionError();
        }}
        onKeyDown={(event) => {
          if (event.key === 'Enter') store.createSession(name);
        }} />
      {state.error ? <p className="redt" data-testid="herdr-new-session-error">{herdrReason(t, state.error)}</p> : null}
      <div className="hnew__acts">
        {state.creating ? <span className="busy">{t.herdr.newCreating}</span> : (
          <>
            <button type="button" className="fbtn fbtn--primary" disabled={!name.trim() || undefined}
              data-testid="herdr-new-session-create" onClick={() => store.createSession(name)}>{t.herdr.newCreate}</button>
            <button type="button" className="fbtn fbtn--ghost" data-testid="herdr-new-session-cancel"
              onClick={store.cancelNewSession}>{t.agent.cancel}</button>
          </>
        )}
      </div>
    </div>
  );
}

// HERDR-6: "New workspace…". A card under the switcher, as the new-session
// one (the App's A-55: no sheets): the recent and common folders, a one-level
// browser, the path itself and an optional name. Opening a folder in the
// browser is choosing it; Create lands this browser in the new workspace's
// first pane, and the computer's screen stays where it is.
function FolderRow({row, onPick, testid, current, star, t}) {
  const button = (
    <button type="button" className={'hfolder' + (current ? ' on' : '') + (star ? ' grow' : '')} data-testid={testid}
      title={row.display} onClick={() => onPick(row)}>
      <Icon name="folder" size={14} />
      <span className="hfolder__n">{row.display}</span>
    </button>
  );
  if (!star) return button;
  // HERDR-11: a star, only where the host keeps favourites.
  return (
    <div className="hnew__row">
      {button}
      <button type="button" className="hstar" aria-pressed={star.on} data-testid={testid + '-star'}
        aria-label={(star.on ? t.herdr.wsUnfavourite : t.herdr.wsFavourite) + ' ' + row.display}
        onClick={() => star.toggle(row)}><span aria-hidden="true">{star.on ? '\u2605' : '\u2606'}</span></button>
    </div>
  );
}

// HERDR-11 §1: what the new workspace starts with - a shell, or one of the
// host's launchers. No launchers (an older core, none installed): no row.
function LaunchRow({store, t}) {
  const launchers = store.newWorkspace.launchers;
  if (!Array.isArray(launchers) || !launchers.length) return null;
  const chip = (command, label, testid) => (
    <button key={testid} type="button" className="fbtn hlaunch__chip" aria-pressed={store.launcher === command}
      data-testid={testid} onClick={() => store.chooseLauncher(command)}>{label}</button>
  );
  return (
    <div className="hlaunch" data-testid="herdr-launch-row">
      <span className="hquick__t">{t.herdr.wsStart}</span>
      <div className="hlaunch__row">
        {chip(null, t.herdr.wsStartShell, 'herdr-launch-shell')}
        {launchers.map((row) => chip(row.command, row.label, 'herdr-launch-' + row.label))}
      </div>
      <p className="hbrowse__e">{t.herdr.wsStartDetail}</p>
    </div>
  );
}

export function NewWorkspaceCard({store, t}) {
  const card = store.newWorkspace;
  const [path, setPath] = useState('');
  const [name, setName] = useState('');
  const field = useRef(null);
  useEffect(() => {
    if (field.current) field.current.focus({preventScroll: true});
  }, []);
  const browse = card.browse;
  const pick = (row) => {
    setPath(row.display);
    store.clearNewWorkspaceError();
  };
  const open = (target, hidden = card.hidden) => store.browseFolder(target, hidden).then((value) => {
    if (value) setPath(value.display);
  });
  const create = () => store.createWorkspace(path, name);
  const session = store.selectedSession || store.ownedSession || '';
  // HERDR-11: stars only where the host keeps favourites (`favourites` null: an older core).
  const star = (row) => (Array.isArray(card.favourites)
    ? {on: isFavourite(card.favourites, row), toggle: store.toggleFavourite} : null);
  const chosen = (card.launchers || []).find((row) => row.command === store.launcher);
  return (
    <div className="hnew hnew--ws" data-testid="herdr-new-workspace">
      <b>{t.herdr.wsTitle}</b>
      <p>{fill(t.herdr.wsDetail, session)}</p>
      <div className="hnew__row">
        <input ref={field} className="hnew__in grow" value={path} placeholder={t.herdr.wsPathPlaceholder}
          aria-label={t.herdr.wsPath} autoCapitalize="off" autoCorrect="off" spellCheck={false} autoComplete="off"
          data-testid="herdr-new-workspace-path"
          onChange={(event) => {
            setPath(event.target.value);
            store.clearNewWorkspaceError();
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.isComposing) create();
          }} />
        <button type="button" className="fbtn" data-testid="herdr-new-workspace-browse"
          disabled={card.browsing || undefined} onClick={() => open(path.trim() || '~')}>{t.herdr.wsBrowse}</button>
      </div>
      {browse ? (
        <div className="hbrowse" data-testid="herdr-folder-browser">
          <div className="hbrowse__head">
            <button type="button" className="fbtn" data-testid="herdr-folder-up" disabled={!browse.parent || card.browsing || undefined}
              onClick={() => browse.parent && open(browse.parent.path)}>{t.herdr.wsUp}</button>
            <span className="hbrowse__at" data-testid="herdr-folder-here">{browse.display}</span>
            <span className="grow" />
            <label className="hbrowse__hid">
              <input type="checkbox" checked={!!card.hidden} data-testid="herdr-folder-hidden"
                onChange={(event) => open(browse.path, event.target.checked)} />
              {t.herdr.wsHidden}
            </label>
            <button type="button" className="pushed__x" aria-label={t.herdr.dismiss} onClick={store.closeBrowse}>
              <Icon name="close" size={12} />
            </button>
          </div>
          <div className="hbrowse__list">
            {browse.dirs.map((row) => (
              <FolderRow key={row.path + row.name} row={{...row, display: row.name}} testid={'herdr-folder-' + row.name}
                onPick={() => open(row.path)} />
            ))}
            {!browse.dirs.length ? <p className="hbrowse__e">{t.herdr.wsEmpty}</p> : null}
            {browse.truncated ? <p className="hbrowse__e">{t.herdr.wsTruncated}</p> : null}
          </div>
        </div>
      ) : (
        <div className="hbrowse__list" data-testid="herdr-folder-suggestions">
          {card.loading ? <p className="hbrowse__e busy">{t.herdr.wsLoading}</p> : null}
          {(card.favourites || []).length ? <span className="hquick__t">{t.herdr.wsFavourites}</span> : null}
          {(card.favourites || []).map((row) => (
            <FolderRow key={'f' + row.path} row={row} testid={'herdr-folder-favourite-' + row.display}
              current={row.display === path} onPick={pick} star={star(row)} t={t} />
          ))}
          {card.recent.length ? <span className="hquick__t">{t.herdr.wsRecent}</span> : null}
          {card.recent.map((row) => (
            <FolderRow key={'r' + row.path} row={row} testid={'herdr-folder-recent-' + row.display}
              current={row.display === path} onPick={pick} star={star(row)} t={t} />
          ))}
          {card.common.length ? <span className="hquick__t">{t.herdr.wsCommon}</span> : null}
          {card.common.map((row) => (
            <FolderRow key={'c' + row.path} row={row} testid={'herdr-folder-common-' + row.display}
              current={row.display === path} onPick={pick} star={star(row)} t={t} />
          ))}
        </div>
      )}
      <input className="hnew__in" value={name} placeholder={t.herdr.wsNamePlaceholder} aria-label={t.herdr.wsName}
        autoCapitalize="off" autoCorrect="off" spellCheck={false} autoComplete="off" data-testid="herdr-new-workspace-name"
        onChange={(event) => {
          setName(event.target.value);
          store.clearNewWorkspaceError();
        }}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.isComposing) create();
        }} />
      <LaunchRow store={store} t={t} />
      {card.error ? <p className="redt" data-testid="herdr-new-workspace-error">{herdrReason(t, card.error)}</p> : null}
      <div className="hnew__acts">
        {card.creating ? <span className="busy">{t.herdr.newCreating}</span> : (
          <>
            {/* HERDR-11: an empty folder is the home folder. */}
            <button type="button" className="fbtn fbtn--primary"
              data-testid="herdr-new-workspace-create" onClick={create}>
              {chosen ? fill(t.herdr.wsCreateAndStart, chosen.label) : t.herdr.newCreate}</button>
            <button type="button" className="fbtn fbtn--ghost" data-testid="herdr-new-workspace-cancel"
              onClick={store.cancelNewWorkspace}>{t.agent.cancel}</button>
          </>
        )}
      </div>
    </div>
  );
}

// OVERVIEW-1: a workspace's most urgent state (red: needs you, green square:
// finished and not seen, accent: working), in a slot that is always there.
function StateDot({state, t, testid}) {
  return (
    <i className={'hsdot' + (state ? ' hsdot--' + state : '')} role={state ? 'img' : undefined}
      aria-label={state ? t.overview.chip[state] : undefined} aria-hidden={state ? undefined : 'true'}
      data-testid={state ? testid : undefined} data-state={state || undefined} />
  );
}

function rowStatus(row) {
  return row.state === 'needs_you' ? 'blocked' : row.state === 'working' ? 'working' : 'done';
}

// OVERVIEW-1: "What wants you" - under the switcher, like the new-session
// card. Grouped by state in core's order; a row says the ask and where, and
// a tap goes there: the Agent panel, or that session's pane.
export function OverviewCard({overview, t, onClose, onRow}) {
  const groups = O.groups(overview);
  return (
    <div className="hnew hover" data-testid="herdr-overview" role="region" aria-label={t.overview.title}>
      <div className="hover__head">
        <b>{t.overview.title}</b>
        <span className="grow" />
        <button type="button" className="fbtn fbtn--ghost" data-testid="herdr-overview-close" onClick={onClose}>
          {t.overview.close}</button>
      </div>
      {groups.length ? groups.map((group) => (
        <div key={group.state} className="hover__group" data-testid={'herdr-overview-' + group.state}>
          <span className={'hover__sec hover__sec--' + group.state}>{t.overview.sections[group.state]}</span>
          {group.rows.map((row) => (
            <button key={row.id} type="button" className="hover__row" data-testid={'overview-row-' + row.id}
              onClick={() => onRow(row)}>
              <span className={'hmark hmark--' + rowStatus(row)} aria-hidden="true"><Icon name={GLYPH.agent} size={18} /></span>
              <span className="hover__tx">
                <span className="hover__ask">{O.askText(t, row)}</span>
                <span className="hover__where">{O.whereText(row, t)}</span>
              </span>
            </button>
          ))}
        </div>
      )) : <p data-testid="herdr-overview-empty">{t.overview.empty}</p>}
    </div>
  );
}

function Dot({pane}) {
  const status = (pane.agent && pane.agent.status) || pane.agent_status || 'unknown';
  return <i className={'hdot hdot--' + status} aria-hidden="true" />;
}

// HERDR-7 (Leo: "herdr 没必要为了 agent 加 agent 动画啦"): a pane with an
// agent shows a still marker, the bar's own Agent glyph, in the App's status
// colour for Herdr's word (working accent, blocked red, done green, idle and
// anything else muted; omodachi-ios `HerdrAgentMarker`). The animated face is
// the Agent panel's and the bar's. A plain pane keeps the dot. The state is
// said in words in the row's label.
function Mark({pane}) {
  const status = H.paneStatus(pane);
  if (!status) return <Dot pane={pane} />;
  return (
    <span className={'hmark hmark--' + status} aria-hidden="true" data-testid={'herdr-marker-' + pane.id}
      data-status={status}><Icon name={GLYPH.agent} size={18} /></span>
  );
}

// OVERVIEW-1 / NOTIFY-1: the pane's agent finished and nobody has opened it since.
function UnseenMark({overview, session, pane, t}) {
  const row = O.paneRow(overview, session, pane);
  if (!row || row.state !== 'done') return null;
  return <i className="hsdot hsdot--done" role="img" aria-label={t.overview.chip.done}
    data-testid={'herdr-unseen-' + pane} />;
}

function faceSuffix(pane, t) {
  const state = paneFaceState(pane);
  return state ? ', ' + faceWord(t, {state}) : '';
}

export function GridColumn({store, t, numbered, overview}) {
  const layout = store.layout;
  const current = H.workspaceOf(layout, store.selected);
  return (
    <nav className="hgrid" data-testid="herdr-grid" aria-label={t.panels.herdr}>
      {(layout && layout.workspaces || []).map((workspace) => (
        <div key={workspace.id}>
          <button type="button" className={'hws' + (current && current.id === workspace.id ? ' on' : '')}
            data-testid={'herdr-workspace-' + workspace.id} onClick={() => store.selectWorkspace(workspace.id)}>
            <b>{workspace.number !== undefined && workspace.number !== null ? workspace.number : workspace.id}</b>
            <span className="hws__l">{workspace.label || ''}</span>
            <span className="grow" />
            {overview ? <StateDot state={O.workspaceWorst(overview, store.selectedSession, workspace.id)} t={t}
              testid={'herdr-workspace-dot-' + workspace.id} /> : null}
            {workspace.focused ? <span className="hws__f">{t.herdr.focused}</span> : null}
          </button>
          {(workspace.tabs || []).map((tab) => (
            <div key={tab.id}>
              {workspace.tabs.length > 1 ? (
                <span className="htab">{fill(t.herdr.tab, tab.label || tab.number || tab.id)}
                  {tab.zoomed ? <span className="accent"> {t.herdr.zoomed}</span> : null}</span>
              ) : null}
              {(tab.panes || []).map((pane) => {
                const name = H.paneName(layout, pane.id, numbered);
                const sub = H.paneSubtitle(pane, name);
                return (
                  <button key={pane.id} type="button" className={'hpane' + (pane.id === store.selected ? ' on' : '')}
                    aria-pressed={pane.id === store.selected} data-testid={'herdr-pane-' + pane.id}
                    aria-label={name + (sub ? ', ' + sub : '') + (H.isWaiting(pane) ? ', ' + t.herdr.waiting : faceSuffix(pane, t))}
                    onClick={() => store.select(pane.id)}>
                    <Mark pane={pane} />
                    <span className="hpane__tx">
                      <span className="hpane__n">{name}</span>
                      {sub ? <span className={'hpane__s' + (pane.agent ? ' agent' : '')}>{sub}</span> : null}
                    </span>
                    <span className="grow" />
                    {H.isWaiting(pane) ? <span className="hwait" data-testid="herdr-waiting">{t.herdr.waiting}</span> : null}
                    {overview ? <UnseenMark overview={overview} session={store.selectedSession} pane={pane.id} t={t} /> : null}
                    {pane.zoomed ? <Icon name="layers" size={12} /> : null}
                  </button>
                );
              })}
            </div>
          ))}
        </div>
      ))}
      {!(layout && layout.workspaces && layout.workspaces.length) ? (
        <p className="hgrid__e">{store.loadingLayout ? t.herdr.loading : t.herdr.noWorkspace}</p>
      ) : null}
      {store.stopped ? null : (
        <button type="button" className="hws hws--new" data-testid="herdr-workspace-new" onClick={store.openNewWorkspace}>
          <b aria-hidden="true">+</b><span className="hws__l">{t.herdr.newWorkspace}</span>
        </button>
      )}
    </nav>
  );
}

export function PaneStrip({store, t, numbered, overview}) {
  const layout = store.layout;
  const current = H.workspaceOf(layout, store.selected);
  return (
    <nav className="hstrip" data-testid="herdr-grid" aria-label={t.panels.herdr}>
      {(layout && layout.workspaces || []).map((workspace) => (
        <React.Fragment key={workspace.id}>
          <button type="button" className={'hchip hchip--ws' + (current && current.id === workspace.id ? ' on' : '')}
            data-testid={'herdr-workspace-' + workspace.id} onClick={() => store.selectWorkspace(workspace.id)}>
            {workspace.label || (workspace.number !== undefined && workspace.number !== null ? String(workspace.number) : '·')}
            {overview ? <StateDot state={O.workspaceWorst(overview, store.selectedSession, workspace.id)} t={t}
              testid={'herdr-workspace-dot-' + workspace.id} /> : null}
          </button>
          {(workspace.tabs || []).flatMap((tab) => tab.panes || []).map((pane) => {
            const name = H.agentName(pane) || H.paneName(layout, pane.id, numbered);
            return (
              <button key={pane.id} type="button" className={'hchip' + (pane.id === store.selected ? ' on' : '')}
                aria-pressed={pane.id === store.selected} data-testid={'herdr-pane-' + pane.id}
                aria-label={name + (H.isWaiting(pane) ? ', ' + t.herdr.waiting : faceSuffix(pane, t))}
                onClick={() => store.select(pane.id)}>
                <Mark pane={pane} />
                <span>{name}</span>
                {H.isWaiting(pane) ? <span className="hwait" data-testid="herdr-waiting">{t.herdr.waiting}</span> : null}
                {overview ? <UnseenMark overview={overview} session={store.selectedSession} pane={pane.id} t={t} /> : null}
              </button>
            );
          })}
        </React.Fragment>
      ))}
      {store.stopped ? null : (
        <button type="button" className="hchip hchip--ws hchip--new" aria-label={t.herdr.newWorkspace}
          title={t.herdr.newWorkspace} data-testid="herdr-workspace-new" onClick={store.openNewWorkspace}>+</button>
      )}
    </nav>
  );
}

// The mirror: the pane's screen, fitted to the width (double tap or the chip
// for 1:1; pinch for anything between). A single tap puts the composer's
// keyboard away. While Type is on, a hidden field holds the keyboard and
// every key goes to `/keys`.
function Mirror({store, t, onLink}) {
  const wrap = useRef(null);
  const scroll = useRef(null);
  const canvas = useRef(null);
  const input = useRef(null);
  const size = useWidth(wrap);
  const mirror = store.mirror;
  const pointers = useRef(new Map());
  const pinch = useRef(null);
  const lastTap = useRef(null);
  const tapTimer = useRef(0);
  // Whether the person is reading the bottom of a picture taller than the
  // stage (a TUI's input line); only then does a shorter stage keep it there.
  const stick = useRef(false);
  const [, setTick] = useState(0);

  useEffect(() => {
    mirror.attach(canvas.current);
    mirror.onPainted = () => setTick((n) => (n + 1) % 1e6);
    return () => {
      mirror.onPainted = null;
    };
  }, [mirror]);

  const zoom = store.zoom;
  useLayoutEffect(() => {
    mirror.setScale(scaleFor(zoom, mirror.cols, mirror.cell.width, Math.max(0, size.width - 2)));
  });
  // HERDR-8: Wrap - the pane's rows as text that fits this width. A full-screen
  // program shows its grid instead, with a note, unless Wrap was picked on
  // purpose.
  const agent = !!H.agentName(store.selectedPane);
  const columns = Math.max(8, Math.floor((size.width - 16) / (mirror.cell.width || 7.8)));
  const wrapped = zoom.mode === 'wrap' ? wrapLayout(mirror.grid(), columns, agent) : null;
  const showsWrap = !!wrapped && (!wrapped.fullScreen || zoom.forced);
  const shown = showsWrap ? 'wrap' : zoom.mode === 'actual' ? 'actual' : 'fit';
  const cycle = () => store.toggleZoom(shown);

  // The first picture of a pane shows where its cursor is; a TUI that hides
  // its cursor (codex) is read from the bottom.
  useEffect(() => {
    const el = scroll.current;
    if (!el || !store.firstFrame) return;
    requestAnimationFrame(() => {
      const cursor = mirror.cursor();
      const row = mirror.cell.height * mirror.scale;
      if (cursor.hidden) el.scrollTop = el.scrollHeight;
      else el.scrollTop = Math.max(0, (cursor.y + 1) * row - el.clientHeight + row);
      stick.current = el.scrollHeight > el.clientHeight && el.scrollTop + el.clientHeight >= el.scrollHeight - 4;
      if (zoom.mode !== 'fit') el.scrollLeft = Math.max(0, cursor.x * mirror.cell.width * mirror.scale - el.clientWidth / 2);
    });
  }, [store.firstFrame]); // eslint-disable-line react-hooks/exhaustive-deps

  // Watching the bottom keeps watching the bottom when the stage gets shorter.
  useLayoutEffect(() => {
    const el = scroll.current;
    if (!el) return;
    if (stick.current) {
      el.scrollTop = el.scrollHeight;
      return;
    }
    // Otherwise the line with the cursor stays on screen.
    const cursor = mirror.cursor();
    if (cursor.hidden) return;
    const row = mirror.cell.height * mirror.scale;
    const bottom = (cursor.y + 1) * row;
    if (bottom > el.scrollTop + el.clientHeight) el.scrollTop = bottom - el.clientHeight + row / 2;
  }, [size.height]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const el = input.current;
    if (!store.typing || !el) return undefined;
    el.focus({preventScroll: true});
    const before = (event) => {
      if (event.isComposing || event.inputType === 'insertCompositionText') return;
      if (event.inputType === 'insertFromPaste') return; // the paste event has it
      const value = H.inputOf(event);
      event.preventDefault();
      if (!value) return;
      if (value.key) store.typeKey(value.key);
      else store.typeText(value.text);
    };
    const composed = (event) => {
      if (event.data) store.typeText(event.data);
      setTimeout(() => {
        el.value = '';
      }, 0);
    };
    el.addEventListener('beforeinput', before);
    el.addEventListener('compositionend', composed);
    return () => {
      el.removeEventListener('beforeinput', before);
      el.removeEventListener('compositionend', composed);
    };
  }, [store.typing, store.typeKey, store.typeText]); // eslint-disable-line react-hooks/exhaustive-deps

  // LINKS-1: the cell under a point of the picture.
  const cellAt = (x, y) => {
    const el = canvas.current;
    if (!el || !el.getBoundingClientRect) return null;
    const box = el.getBoundingClientRect();
    const col = Math.floor((x - box.left) / (mirror.cell.width * mirror.scale));
    const row = Math.floor((y - box.top) / (mirror.cell.height * mirror.scale));
    return col >= 0 && row >= 0 && col < mirror.cols && row < mirror.rows ? {col, row} : null;
  };

  const openLinkAt = (point) => {
    const cell = point && onLink ? cellAt(point.x, point.y) : null;
    const link = cell ? mirror.linkAt(cell.col, cell.row) : null;
    if (link) onLink(link);
    return !!link;
  };

  const singleTap = (point) => {
    // A tap on a link opens it; everywhere else a tap does what it did.
    if (openLinkAt(point)) return;
    if (store.typing) {
      if (input.current) input.current.focus({preventScroll: true});
      return;
    }
    const active = document.activeElement;
    if (active && active !== document.body && active.blur) active.blur();
  };

  const onPointerDown = (event) => {
    if (event.pointerType !== 'touch') return;
    pointers.current.set(event.pointerId, {x: event.clientX, y: event.clientY});
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      pinch.current = {distance: Math.hypot(a.x - b.x, a.y - b.y) || 1, scale: mirror.scale};
    }
  };
  const onPointerMove = (event) => {
    if (!pointers.current.has(event.pointerId)) return;
    pointers.current.set(event.pointerId, {x: event.clientX, y: event.clientY});
    if (pinch.current && pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      const distance = Math.hypot(a.x - b.x, a.y - b.y) || 1;
      store.pinchZoom(pinch.current.scale * (distance / pinch.current.distance));
    }
  };
  const onPointerUp = (event) => {
    const was = pointers.current.get(event.pointerId);
    pointers.current.delete(event.pointerId);
    if (pinch.current) {
      if (pointers.current.size === 0) pinch.current = null;
      return;
    }
    if (event.pointerType !== 'touch' || !was) return;
    // A link opens on the tap itself, not after the double-tap wait: a new
    // tab has to open inside the gesture, or iOS refuses it.
    if (Math.hypot(was.x - event.clientX, was.y - event.clientY) < 10 && openLinkAt({x: event.clientX, y: event.clientY})) {
      clearTimeout(tapTimer.current);
      lastTap.current = null;
      return;
    }
    const now = Date.now();
    const last = lastTap.current;
    if (last && now - last.at < 320 && Math.hypot(last.x - event.clientX, last.y - event.clientY) < 30) {
      clearTimeout(tapTimer.current);
      lastTap.current = null;
      cycle();
      return;
    }
    lastTap.current = {at: now, x: event.clientX, y: event.clientY};
    clearTimeout(tapTimer.current);
    const point = {x: event.clientX, y: event.clientY};
    tapTimer.current = setTimeout(() => singleTap(point), 330);
  };

  return (
    <div className="mirror" ref={wrap}>
      <div className={'mirror__scroll' + (zoom.mode !== 'actual' ? ' fit' : '') + (showsWrap ? ' hidden' : '')} ref={scroll}
        data-testid="herdr-terminal" aria-hidden={showsWrap || undefined}
        onScroll={(event) => {
          const el = event.currentTarget;
          stick.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 4;
        }}
        onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={onPointerUp}
        onClick={(event) => {
          if (event.nativeEvent.pointerType !== 'touch' && event.detail === 1) singleTap({x: event.clientX, y: event.clientY});
        }}
        onDoubleClick={(event) => {
          if (event.nativeEvent.pointerType !== 'touch') cycle();
        }}>
        <canvas ref={canvas} className="mirror__canvas" aria-label={t.panels.herdr} role="img" />
      </div>
      {showsWrap ? (
        // HERDR-10: one view per pane in a session, so a selection held in
        // the last pane's text never holds this pane's back.
        <WrapView key={(store.selectedSession || '') + '\u001f' + (store.selected || '')} result={wrapped}
          palette={mirror.palette} onLink={onLink} onDoubleTap={cycle} onSingleTap={() => singleTap(null)} />
      ) : null}
      {wrapped && wrapped.fullScreen && !showsWrap ? (
        <div className="mirror__note" data-testid="herdr-fullscreen-note">{t.herdr.fullScreenNote}</div>
      ) : null}
      <button type="button" className="fbtn mirror__zoom" data-testid="herdr-zoom" title={store.phone ? t.herdr.mirrorHintPhone : t.herdr.mirrorHint} onClick={cycle}>
        {shown === 'wrap' ? t.herdr.zoomWrap : shown === 'actual' ? t.herdr.zoomActual : t.herdr.zoomFit}
      </button>
      {store.typing ? (
        <textarea ref={input} className="typein" data-testid="herdr-type-input" aria-label={t.herdr.type}
          autoCapitalize="off" autoCorrect="off" spellCheck={false} autoComplete="off" rows={1}
          onKeyDown={(event) => {
            const read = H.keyOf(event.nativeEvent);
            if (!read || read.text) return;
            event.preventDefault();
            if (read.drop) store.typeDrop();
            else store.typeKey(read.key);
          }}
          onPaste={(event) => {
            const text = event.clipboardData && event.clipboardData.getData('text/plain');
            event.preventDefault();
            if (text) store.typeText(text.replace(/\r\n/g, '\n'));
          }}
          onBlur={() => {
            // Putting the keyboard away is how a person says "stop typing".
            setTimeout(() => {
              if (document.activeElement !== input.current) store.stopTyping();
            }, 200);
          }} />
      ) : null}
    </div>
  );
}

// HERDR-8: the colours of one run of text, as the grid would paint it.
function runStyle(run, palette) {
  const style = run.style;
  let fg = style && style.fg;
  let bg = style && style.bg;
  if (style && style.inverse) {
    const swap = bg || palette.background;
    bg = fg || palette.foreground;
    fg = swap;
  }
  if (run.cursor) {
    bg = palette.cursor;
    fg = palette.background;
  }
  if (!style && !run.cursor) return undefined;
  const css = {};
  if (fg) css.color = fg;
  if (bg) css.background = bg;
  if (style && style.bold) css.fontWeight = 600;
  if (style && style.italic) css.fontStyle = 'italic';
  if (style && style.dim) css.opacity = 0.6;
  const lines = [style && style.underline ? 'underline' : '', style && style.strike ? 'line-through' : ''].filter(Boolean);
  if (lines.length) css.textDecoration = lines.join(' ');
  return css;
}

// One line's runs as spans; an address or a path in the joined text (and a
// link the program marked) is a tap target, as on the grid (LINKS-1).
function Runs({runs, palette, onLink, detectLinks}) {
  const text = runs.map((run) => run.text).join('');
  const found = detectLinks ? detect(text, 'mirror') : [];
  const out = [];
  let offset = 0;
  runs.forEach((run, index) => {
    const start = offset;
    const end = offset + run.text.length;
    offset = end;
    const style = runStyle(run, palette);
    if (run.link) {
      out.push(<span key={index} className="wrapv__a" style={style} onClick={() => onLink({kind: 'url', text: run.link})}>{run.text}</span>);
      return;
    }
    let at = start;
    for (const link of found) {
      if (link.end <= at || link.start >= end) continue;
      if (link.start > at) out.push(<span key={index + ':' + at} style={style}>{text.slice(at, link.start)}</span>);
      const to = Math.min(end, link.end);
      out.push(<span key={index + ':' + Math.max(at, link.start)} className="wrapv__a" style={style}
        onClick={() => onLink({kind: link.kind, text: link.text})}>{text.slice(Math.max(at, link.start), to)}</span>);
      at = to;
    }
    if (at < end) out.push(<span key={index + ':' + at} style={style}>{text.slice(at, end)}</span>);
  });
  return out;
}

// HERDR-8: the wrapped mirror. Newest line at the bottom while the reader is
// there; scrolled up, it stays put; while text is selected, it holds still.
export function WrapView({result, palette, onLink, onDoubleTap, onSingleTap}) {
  const el = useRef(null);
  const stick = useRef(true);
  const held = useRef(result);
  const lastTap = useRef(null);
  const timer = useRef(0);
  const selecting = () => {
    const selection = typeof window !== 'undefined' && window.getSelection ? window.getSelection() : null;
    return !!(selection && !selection.isCollapsed && el.current && el.current.contains(selection.anchorNode));
  };
  if (!selecting()) held.current = result;
  const shown = held.current;
  useLayoutEffect(() => {
    if (stick.current && el.current) el.current.scrollTop = el.current.scrollHeight;
  });
  useEffect(() => () => clearTimeout(timer.current), []);
  return (
    <div className="wrapv" ref={el} data-testid="herdr-wrap"
      style={{background: palette.background, color: palette.foreground}}
      onScroll={(event) => {
        const box = event.currentTarget;
        stick.current = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
      }}
      onPointerUp={(event) => {
        if (event.pointerType !== 'touch') return;
        const now = Date.now();
        const last = lastTap.current;
        if (last && now - last.at < 320 && Math.hypot(last.x - event.clientX, last.y - event.clientY) < 30) {
          clearTimeout(timer.current);
          lastTap.current = null;
          onDoubleTap();
          return;
        }
        lastTap.current = {at: now, x: event.clientX, y: event.clientY};
        clearTimeout(timer.current);
        timer.current = setTimeout(onSingleTap, 330);
      }}
      onDoubleClick={(event) => {
        if (event.nativeEvent.pointerType !== 'touch') onDoubleTap();
      }}>
      {shown.blocks.map((block, index) => {
        if (block.kind === 'blank') return <div key={index} className="wrapv__l">{' '}</div>;
        if (block.kind === 'rule') {
          return <div key={index} className="wrapv__r"><Runs runs={block.runs} palette={palette} onLink={onLink} /></div>;
        }
        if (block.kind === 'wide') {
          return (
            <div key={index} className="wrapv__w" data-testid="herdr-wrap-wide">
              <pre>{block.rows.map((row, n) => (
                <React.Fragment key={n}>{n ? '\n' : null}<Runs runs={row} palette={palette} onLink={onLink} /></React.Fragment>
              ))}</pre>
            </div>
          );
        }
        return (
          <div key={index} className="wrapv__l" style={block.indent ? {paddingLeft: block.indent + 'ch', textIndent: -block.indent + 'ch'} : undefined}>
            <Runs runs={block.runs} palette={palette} onLink={onLink} detectLinks />
          </div>
        );
      })}
    </div>
  );
}

function KeyCap({face, label, testid, onPress, repeats, armed}) {
  const timer = useRef(0);
  const stop = () => {
    clearTimeout(timer.current);
    clearInterval(timer.current);
    timer.current = 0;
  };
  return (
    <button type="button" className={'kcap' + (armed ? ' armed' : '')} aria-label={label} data-testid={testid}
      // Keep the focus where it is: a key pressed while Type is on must not
      // take the keyboard from it.
      onMouseDown={(event) => event.preventDefault()}
      onPointerDown={() => {
        if (!repeats) return;
        stop();
        timer.current = setTimeout(() => {
          timer.current = setInterval(onPress, 90);
        }, 400);
      }}
      onPointerUp={stop} onPointerLeave={stop} onPointerCancel={stop}
      onClick={onPress}>{face}</button>
  );
}

function KeyRows({store, t, compact}) {
  const presets = H.presets(store.rows);
  const caps = (list) => list.map((cap) => (
    <KeyCap key={cap.id} face={cap.face} testid={'herdr-key-' + cap.id} repeats={cap.repeats}
      label={fill(t.herdr.keySend, cap.name === 'digit' ? cap.face : t.herdr.keyNames[cap.name])}
      onPress={() => store.sendKey(cap)} />
  ));
  const presetCaps = presets.length ? (
    <>
      <i className="krule" aria-hidden="true" />
      {presets.map((row) => {
        const armed = store.isArmed('key:' + row.label);
        return (
          <KeyCap key={row.label} face={H.presetFace(row) + (armed ? '?' : '')} armed={armed}
            label={fill(t.herdr.keySend, row.label)} testid={'herdr-preset-' + row.label} onPress={() => store.sendPreset(row)} />
        );
      })}
    </>
  ) : null;
  if (!compact) {
    return <div className="krows" data-testid="herdr-keys" aria-label={t.herdr.keys}><div className="krow1">{caps(H.KEY_ROW)}{presetCaps}</div></div>;
  }
  return (
    <div className="krows compact" data-testid="herdr-keys" aria-label={t.herdr.keys}>
      <div className="krow1 fill">{caps(H.KEY_ROW.slice(0, H.FIRST_ROW))}</div>
      <div className="krow1 fill" data-testid="herdr-keys-second">{caps(H.KEY_ROW.slice(H.FIRST_ROW))}{presetCaps}</div>
    </div>
  );
}

function QuickDock({store, t, harnessOnBar}) {
  const rows = store.rows;
  const warning = H.operatorWarning(rows, t);
  const armedTap = (title, id, onPress, testid, hint) => {
    const armed = store.isArmed(id);
    return (
      <button key={id} type="button" className={'fbtn' + (armed ? ' armed' : '')} title={hint} data-testid={testid}
        onClick={onPress}>{armed ? t.herdr.tapAgain : title}</button>
    );
  };
  return (
    <div className="hquick" data-testid="herdr-quick-dock">
      {warning ? <p className="warnt" data-testid="herdr-quick-warning">{warning}</p> : null}
      {!harnessOnBar && rows.harness.length ? (
        <div className="hquick__g" data-testid="herdr-quick-harness">
          <span className="hquick__t">{t.herdr.quickHarness}</span>
          <div className="hquick__r">
            {rows.harness.map((row) => armedTap(H.harnessTitle(row, t), 'harness:' + row.command, () => store.sendHarness(row),
              'herdr-harness-' + (row.id || row.command), fill(t.herdr.harnessHint, row.command)))}
          </div>
        </div>
      ) : null}
      {rows.quick.map((group) => (
        <div key={group.id || 'op:' + group.title} className="hquick__g" data-testid={'herdr-quick-' + (group.id || 'op')}>
          <span className="hquick__t">{H.quickTitle(group, t)}</span>
          <div className="hquick__r">
            {group.items.map((item) => (
              <button key={item} type="button" className="fbtn" data-testid={'herdr-quick-item-' + item}
                onClick={() => store.sendQuick(item)}>{item}</button>
            ))}
          </div>
        </div>
      ))}
      {rows.commands.length ? (
        <div className="hquick__g" data-testid="herdr-quick-commands">
          <span className="hquick__t">{t.herdr.quickCommands}</span>
          <div className="hquick__r">
            {rows.commands.map((row) => armedTap(row.command + (row.arg_hint ? ' ' + row.arg_hint : ''), 'command:' + row.command,
              () => store.runCommand(row), 'herdr-command-' + row.command, row.description || ''))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function SendState({store, t}) {
  const s = store.sendState;
  if (s.kind === 'idle' || s.kind === 'sending') return null;
  const [text, tone, dismissible] = s.kind === 'sent' ? [t.herdr.sent, 'ok', false]
    : s.kind === 'sentLight' ? [t.herdr.sentLight, 'plain', true]
      : s.kind === 'notSubmitted' ? [fill(t.herdr.notSubmitted, herdrReason(t, s.reason)), 'warn', true]
        : [herdrReason(t, s.code), 'bad', true];
  return (
    <div className={'hstate hstate--' + tone}>
      <span data-testid="herdr-send-state">{text}</span>
      {dismissible ? (
        <button type="button" className="pushed__x" aria-label={t.herdr.dismiss} onClick={store.dismissSendState}>
          <Icon name="close" size={12} />
        </button>
      ) : null}
    </div>
  );
}

// HERDR-9: the pane's own choices, as buttons, right above the composer
// (the App's HerdrChoicesStrip): one per option in the pane's order, its own
// words on one line, the row the pane has marked marked here too, a choice
// that keeps applying ("don't ask again") dimmer and never the main one.
export function ChoicesStrip({store, t}) {
  const choices = store.choices;
  if (!choices) return null;
  const answered = store.choicesAnswered;
  const value = (choice) => [choice.highlighted ? t.herdr.choicesSelected : '', choice.lasting ? t.herdr.choicesLasting : '']
    .filter(Boolean).join(' ');
  return (
    <div className={'hchoices' + (answered ? ' answered' : '')} data-testid="herdr-choices" role="group"
      aria-label={t.herdr.choicesTitle}>
      <div className="hchoices__head">
        <span data-testid="herdr-choices-title">{answered ? t.herdr.choicesAnswered : t.herdr.choicesTitle}</span>
        <span className="grow" />
        {choices.escape ? (
          <button type="button" className="fbtn" data-testid="herdr-choices-escape" disabled={answered || undefined}
            title={t.herdr.choicesEscapeHint} onClick={store.answerEscape}>{t.herdr.keyNames.escape}</button>
        ) : null}
      </div>
      {choices.kind === 'yesNo' ? (
        <div className="hchoices__yn">
          {choices.options.map((choice) => (
            <button key={choice.index} type="button" className={'fbtn' + (choice.highlighted ? ' on' : '')}
              data-testid={'herdr-choice-' + choice.index} disabled={answered || undefined} title={t.herdr.choicesHint}
              aria-description={value(choice) || undefined} onClick={() => store.answer(choice)}>
              {choice.index === 0 ? t.herdr.choicesYes : t.herdr.choicesNo}
            </button>
          ))}
        </div>
      ) : (
        <div className="hchoices__list">
          {choices.options.map((choice) => (
            <button key={choice.index} type="button" disabled={answered || undefined} title={t.herdr.choicesHint}
              className={'hchoice' + (choice.highlighted && !choice.lasting ? ' on' : '') + (choice.lasting ? ' lasting' : '')}
              data-testid={'herdr-choice-' + choice.index} aria-label={choice.label}
              aria-description={value(choice) || undefined} onClick={() => store.answer(choice)}>
              <span className="hchoice__n" aria-hidden="true">{choice.number == null ? '·' : choice.number}</span>
              <span className="hchoice__l">{choice.label}</span>
              {choice.highlighted ? <span className="hchoice__m" aria-hidden="true">›</span> : null}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function wantsEnterToSend() {
  try {
    return typeof matchMedia === 'function' && matchMedia('(hover: hover) and (pointer: fine)').matches;
  } catch {
    return false;
  }
}

function InputArea({store, t, harnessOnBar, att, composerRef}) {
  const enterSends = useRef(wantsEnterToSend());
  // ATTACH-1: the uploaded chips ride with Send; core types their paths.
  const send = () => {
    if (att.uploading) return att.say('wait');
    store.sendDraft(att.chips.length ? {refs: att.refs(), onSent: att.clear} : null);
    return undefined;
  };
  const name = store.selected ? H.paneName(store.layout, store.selected, (n) => fill(t.herdr.paneNumbered, n)) : '';
  return (
    <div className="hinput">
      {store.typing ? (
        <div className="htype" data-testid="herdr-type-strip">
          <Icon name="keyboard" size={16} />
          <span>{fill(t.herdr.typeStrip, name)}</span>
          <span className="grow" />
          <button type="button" className="fbtn" data-testid="herdr-type-stop" onMouseDown={(event) => event.preventDefault()}
            onClick={() => store.stopTyping()}>{t.herdr.stop}</button>
        </div>
      ) : null}
      {store.quickOpen && store.selected ? <QuickDock store={store} t={t} harnessOnBar={harnessOnBar} /> : null}
      {store.selected ? <ChoicesStrip store={store} t={t} /> : null}
      <SendState store={store} t={t} />
      {store.selected && !store.typing ? <AttachNotice att={att} t={t} testid="herdr-attach-notice" /> : null}
      {store.selected && !store.typing ? <AttachmentChips att={att} draft={store.draft} t={t} testid="herdr-chips" /> : null}
      {store.selected && !store.typing ? (
        <div className="composer">
          <div className="composer__box">
          <AttachButton att={att} t={t} testid="herdr-attach" />
          {/* A pane takes commands: no autocorrect turning `--clean` into `—clean`. */}
          <textarea ref={composerRef} className="composer__in" rows={1} placeholder={t.herdr.composer} aria-label={t.herdr.composer}
            aria-description={t.herdr.composerHint}
            autoCapitalize="off" autoCorrect="off" spellCheck={false} autoComplete="off" onPaste={att.onPaste}
            value={store.draft} data-testid="herdr-composer" onChange={(event) => store.setDraft(event.target.value)}
            onKeyDown={(event) => {
              // HERDR-9: an empty box hands its editing keys to the pane
              // (⌫, Return, the arrows, Tab, Esc), as Remote's does.
              if (!store.draft && !att.chips.length) {
                const key = passthroughKey(event);
                if (key) {
                  event.preventDefault();
                  store.passKey(key);
                  return;
                }
              }
              if (event.key !== 'Enter' || event.isComposing) return;
              if (event.metaKey || event.ctrlKey || (enterSends.current && !event.shiftKey)) {
                event.preventDefault();
                send();
              }
            }} />
          </div>
          <button type="button" className="fbtn fbtn--primary composer__b" data-testid="herdr-send"
            disabled={(!store.draft.trim() && !att.ready) || att.uploading || store.sendState.kind === 'sending' || undefined}
            title={att.uploading ? t.attach.wait : undefined}
            onClick={send}>{store.sendState.kind === 'sending' ? t.herdr.sending : t.herdr.send}</button>
        </div>
      ) : null}
    </div>
  );
}

function ControlBar({store, t, wide, foldKeys, onConfirm}) {
  const [more, setMore] = useState(false);
  const harness = store.rows.harness;
  return (
    <div className="hbar" data-testid="herdr-control-bar">
      <button type="button" className={'fbtn hbar__t' + (store.typing ? ' on' : '')} aria-pressed={store.typing}
        title={t.herdr.typeHint} data-testid="herdr-type" onMouseDown={(event) => event.preventDefault()}
        onClick={() => (store.typing ? store.stopTyping() : store.startTyping())}>
        <Icon name="keyboard" size={14} />{t.herdr.type}
      </button>
      <button type="button" className={'fbtn hbar__t' + (store.quickOpen ? ' on' : '')} aria-pressed={store.quickOpen}
        title={t.herdr.quickHint} data-testid="herdr-quick" onClick={store.toggleQuick}>
        <Icon name="bolt" size={14} />{t.herdr.quick}
      </button>
      {/* HERDR-8: on a phone held upright the keys rows fold behind this. */}
      {foldKeys ? (
        <button type="button" className={'fbtn hbar__t' + (store.keysOpen ? ' on' : '')} aria-pressed={store.keysOpen}
          title={t.herdr.keysHint} data-testid="herdr-keys-toggle" onClick={store.toggleKeys}>
          <Icon name="keyboard" size={14} />{t.herdr.keys}
        </button>
      ) : null}
      {wide && harness.length ? (
        <div className="hbar__h" aria-label={t.herdr.quickHarness}>
          {harness.map((row) => {
            const armed = store.isArmed('harness:' + row.command);
            return (
              <button key={row.command} type="button" className={'fbtn accentb' + (armed ? ' armed' : '')}
                title={fill(t.herdr.harnessHint, row.command)} data-testid={'herdr-bar-harness-' + (row.id || row.command)}
                onClick={() => store.sendHarness(row)}>{armed ? t.herdr.tapAgain : H.harnessTitle(row, t)}</button>
            );
          })}
        </div>
      ) : null}
      <span className="grow" />
      {wide ? <span className={'hbar__mode' + (store.typing ? ' on' : '')} data-testid="herdr-mode">{modeLabel(store, t)}</span> : null}
      <Menu label={t.herdr.more} testid="herdr-more" open={more} onToggle={setMore} up buttonClass="fbtn hbar__t"
        face={<><Icon name="more" size={14} />{t.herdr.more}</>}>
        {H.MENU.map((action) => {
          const request = H.controlRequest(action, store.layout, store.selected);
          const reason = request.kind === 'unsupported' ? t.herdr[request.reason] : null;
          return (
            <MenuItem key={action} title={t.herdr[action]} detail={reason} testid={'herdr-menu-' + action}
              glyph={{newWorkspace: 'folder', newTab: 'install', closeTab: 'close', close: 'close'}[action]}
              destructive={H.CONFIRMS.has(action) && !reason} onClick={() => {
                setMore(false);
                if (H.CONFIRMS.has(action) && !reason) onConfirm({action, request});
                else store.perform(request);
              }} />
          );
        })}
      </Menu>
    </div>
  );
}

// `focus` is what a push named: {session, pane, agent}.
export default function HerdrPanel({client, base, theme, revision, focus, hostId, WebSocketImpl = globalThis.WebSocket,
                                    health = 'live', hostName = '', overview = null, onAgent = null}) {
  const {t} = useCopy();
  const store = useHerdr({client, base, theme, revision, focus, WebSocketImpl, health});
  const root = useRef(null);
  // ATTACH-1: chips belong to one pane's draft; another pane drops them.
  const composerRef = useRef(null);
  const placeRef = useRef({session: null, pane: null});
  placeRef.current = {session: store.selectedSession, pane: store.selected};
  const target = useRef(() => ({kind: 'herdr', session: placeRef.current.session || undefined,
    pane: placeRef.current.pane || undefined})).current;
  const att = useAttachments({client, target, getDraft: store.getDraft, setDraft: store.setDraft, inputRef: composerRef});
  const drop = useDropTarget(att, wantsEnterToSend() && !!store.selected && !store.typing);
  const attRef = useRef(att);
  attRef.current = att;
  const firstPane = useRef(true);
  useEffect(() => {
    if (firstPane.current) {
      firstPane.current = false;
      return;
    }
    attRef.current.discard();
  }, [store.selected]);
  // HERDR-10: the strip opened codex's notes box; the note is written in the
  // composer, and its Return submits the answer with it.
  useEffect(() => {
    if (store.noteRequests && composerRef.current) composerRef.current.focus({preventScroll: true});
  }, [store.noteRequests]);
  const size = useWidth(root);
  const wide = size.width >= WIDE;
  // HERDR-8: a phone held upright - the mirror gets the room.
  const phone = !wide && size.height > size.width;
  const [confirm, setConfirm] = useState(null);
  // OVERVIEW-1: the "Needs you" list under the switcher.
  const [overviewOpen, setOverviewOpen] = useState(false);
  const goTo = (row) => {
    setOverviewOpen(false);
    if (row.source === 'agent') {
      if (onAgent) onAgent();
      return;
    }
    store.land({session: row.session, pane: row.pane_id});
  };
  const [pushed, setPushed] = useState(() => H.pushPlace(focus));
  useEffect(() => setPushed(H.pushPlace(focus)), [focus]);
  const numbered = (n) => fill(t.herdr.paneNumbered, n);
  const notice = noticeText(store.notice, t);
  // LINKS-1: links on the mirror - web addresses here, files on the computer.
  const links = useHostOpen({client});
  const where = pushed ? (focus && focus.agent ? fill(t.next.waitingWhereAgent, focus.agent, pushed.session)
    : fill(t.next.waitingWhereSession, pushed.session)) : null;

  let stage;
  if (!store.selected && store.stopped) {
    stage = (
      <div className="hempty" data-testid="herdr-stopped">
        <b>{t.herdr.sessionStopped}</b>
        <p>{t.herdr.sessionStartDetail}</p>
        <button type="button" className="fbtn fbtn--primary" data-testid="herdr-session-start-here"
          disabled={!!store.starting || undefined} onClick={() => store.startSession(store.stopped)}>
          {fill(t.herdr.sessionStart, store.stopped)}
        </button>
      </div>
    );
  } else if (!store.selected) {
    stage = (
      <div className="hempty" data-testid="herdr-no-pane">
        <b>{t.herdr.noPanes}</b>
        <p>{store.loadingLayout ? t.herdr.loadingLayout : t.herdr.noPanesDetail}</p>
      </div>
    );
  } else {
    stage = (
      <>
        {/* HERDR-11 §3: a mirror not known to be current is dimmed and says when. */}
        <div className={'mirror' + (store.stale ? ' hmirror--stale' : '')} data-testid="herdr-mirror-wrap">
          <Mirror store={store} t={t} onLink={(link) => {
            const source = {kind: 'herdr', pane: store.selected, session: store.selectedSession || undefined};
            const target = {kind: link.kind, target: link.text};
            if (links.openLink(source, target)) openHere(link.text);
          }} />
          {store.stale ? <div className="hstale" data-testid="herdr-mirror-stale">
            {fill(t.herdr.lastUpdate, clock(new Date(store.staleAt)))}</div> : null}
        </div>
      </>
    );
  }

  return (
    <section className={'hpanel' + (wide ? ' wide' : '')} ref={root} data-testid="panel-herdr" aria-label={t.panels.herdr}
      {...drop.handlers}>
      <DropVeil over={drop.over} t={t} />
      <SessionSwitcher store={store} t={t} overview={overview} overviewOpen={overviewOpen}
        onOverview={() => setOverviewOpen((open) => !open)} trailing={pushed ? null : (
        <a className="hpanel__app" href={appLinks('herdr', hostId, store.selected
          ? {session: store.selectedSession, pane: store.selected} : null).universal}
        target="_blank" rel="noopener noreferrer" data-testid="herdr-open-app-quiet">{t.next.openApp}</a>
      )} />
      {overview && overviewOpen ? <OverviewCard overview={overview} t={t} onClose={() => setOverviewOpen(false)}
        onRow={goTo} /> : null}
      {store.newSession.open ? <NewSessionCard store={store} t={t} /> : null}
      {store.newWorkspace.open ? <NewWorkspaceCard store={store} t={t} /> : null}
      {pushed ? (
        <div className="pushed" data-testid="herdr-from-push">
          <Icon name="bell" size={14} />
          <span className="pushed__t">{t.next.waitingHerdr}</span>
          <span className="pushed__w">{where}</span>
          <span className="grow" />
          <a className="pushed__app" href={appLinks('herdr', hostId, pushed).universal} target="_blank"
            rel="noopener noreferrer" data-testid="herdr-open-app">{t.next.openApp}</a>
          <button type="button" className="pushed__x" aria-label={t.herdr.dismiss} onClick={() => setPushed(null)}>
            <Icon name="close" size={12} />
          </button>
        </div>
      ) : null}
      <div className="hpanel__body">
        {wide ? <div className="hpanel__grid" style={{width: GRID + 'px'}}><GridColumn store={store} t={t} numbered={numbered} overview={overview} /></div>
          : <PaneStrip store={store} t={t} numbered={numbered} overview={overview} />}
        <div className="hstage">
          {store.herdrDown ? <ConnectionBanner health="herdrDown" host={hostName} inline onRetry={store.retryLayout} /> : null}
          {notice ? (
            <div className="hnotice">
              <span data-testid="herdr-notice">{notice}</span>
              <button type="button" className="pushed__x" aria-label={t.herdr.dismiss} data-testid="herdr-notice-dismiss"
                onClick={store.dismissNotice}><Icon name="close" size={12} /></button>
            </div>
          ) : null}
          <LinkNotice notice={links.notice} t={t} hostId={hostId} onDismiss={links.dismiss}
            onComputer={links.openURLOnComputer} testid="herdr-link-notice" />
          {store.selected ? <OnceHint id="herdr" text={store.phone || phone ? t.herdr.hintPhone : t.herdr.hint} once={phone} /> : null}
          {stage}
        </div>
      </div>
      <InputArea store={store} t={t} harnessOnBar={wide} att={att} composerRef={composerRef} />
      {/* One row when it fits, as the App's ViewThatFits; else the phone's two. */}
      {store.selected && (!phone || store.keysOpen) ? <KeyRows store={store} t={t} compact={size.width < KEYS_ONE_ROW} /> : null}
      <ControlBar store={store} t={t} wide={wide} foldKeys={phone} onConfirm={setConfirm} />
      {confirm ? (
        <div className="hconfirm" role="alertdialog" aria-label={confirm.action === 'closeTab' ? t.herdr.closeTabTitle : t.herdr.closePaneTitle}
          data-testid="herdr-confirm">
          <div className="hconfirm__box">
            <b>{confirm.action === 'closeTab' ? t.herdr.closeTabTitle : t.herdr.closePaneTitle}</b>
            <p>{confirm.action === 'closeTab' ? t.herdr.closeTabDetail : t.herdr.closePaneDetail}</p>
            <div className="hnew__acts">
              <button type="button" className="fbtn fbtn--danger" data-testid="herdr-confirm-yes" onClick={() => {
                store.perform(confirm.request);
                setConfirm(null);
              }}>{confirm.action === 'closeTab' ? t.herdr.closeTab : t.herdr.closePaneConfirm}</button>
              <button type="button" className="fbtn fbtn--ghost" data-testid="herdr-confirm-no"
                onClick={() => setConfirm(null)}>{t.agent.cancel}</button>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}
