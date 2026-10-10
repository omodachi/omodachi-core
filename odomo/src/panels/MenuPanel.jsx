import React, {useEffect, useMemo, useRef, useState} from 'react';
import Icon from '../Icon.jsx';
import {PanelArea, PanelTitle, GroupLabel, Row, EmptyState, KeyBlock, Field, PinButton} from '../ds.jsx';
import {useCopy} from '../copy.js';
import {fill} from '../lib/i18n.js';
import {ApiError, invokeConfirmed, newRequestId} from '../lib/api.js';
import {ROOT_GLYPH, action, buildTree, confirmTap, needsConfirm, pathOf, rootOf, search, shortcutRows} from '../lib/catalog.js';
import * as storage from '../lib/storage.js';
import {appLinks} from '../lib/applink.js';

// Panel 1 (L7), adapted from omodachi-web `src/app/panels/MenuPanel.jsx` at
// main 0da28c9: the same two halves, the same 360 + 420 side-by-side rule,
// the same pins and long press - with the host's real catalog and keybindings
// behind it instead of the site's mock, and rows that really run.
//
// A tap on a `confirm` row arms it ("Tap again to run", 3 s) and sends
// nothing; the second tap sends, and the 409 `confirmation_required` core
// answers is followed once with its token (STORE-6 §B3). Pins live in this
// browser's localStorage only. A row only the app can run says "In the app"
// and that badge is a link into the app on this computer (TAIL-3b L8).

function useLongPress(run) {
  const timer = useRef(0);
  const fired = useRef(false);
  const cancel = () => {
    clearTimeout(timer.current);
    timer.current = 0;
  };
  return {
    handlers: {
      onPointerDown: () => {
        fired.current = false;
        timer.current = setTimeout(() => {
          fired.current = true;
          run();
        }, 550);
      },
      onPointerUp: cancel,
      onPointerLeave: cancel,
      onPointerCancel: cancel,
      onContextMenu: (event) => event.preventDefault(),
    },
    consumed: () => {
      const was = fired.current;
      fired.current = false;
      return was;
    },
  };
}

function usePins(half) {
  const [pins, setPins] = useState(() => storage.local('pins.' + half, []));
  const toggle = (id, label) => setPins((list) => {
    const next = list.includes(id) ? list.filter((x) => x !== id) : list.concat(id).slice(-64);
    storage.setLocal('pins.' + half, next);
    // ODOMO-2 (N-39 rev 5, as the App): a pin keeps its label, so a row the
    // host no longer has is still named - dimmed, and never removed silently.
    if (label) {
      const labels = storage.local('pins.' + half + '.labels', {}) || {};
      storage.setLocal('pins.' + half + '.labels', {...labels, [id]: label});
    }
    return next;
  });
  return [Array.isArray(pins) ? pins : [], toggle];
}

function pinLabel(half, id) {
  const labels = storage.local('pins.' + half + '.labels', {}) || {};
  return typeof labels[id] === 'string' ? labels[id] : id;
}

// A pinned row the host no longer has: its label, dimmed, "Gone from the host".
function Tombstone({half, id, editing, onPin}) {
  const {t} = useCopy();
  const label = pinLabel(half, id);
  return (
    <Row glyph="warn" title={label} detail={t.menu.gone} disabled testid={'pin-tombstone-' + id}
      trailing={editing ? <PinButton pinned label={fill(t.menu.unpin, label)} onClick={onPin} /> : null} />
  );
}

// One runner per half: the armed confirm row, the held confirm token, and the
// toast that says what the host did.
function useRunner(client) {
  const {t} = useCopy();
  const [armed, setArmed] = useState(null);
  const [toast, setToast] = useState(null);
  const tokens = useRef(new Map());
  useEffect(() => {
    if (!armed) return undefined;
    const timer = setTimeout(() => {
      setArmed(null);
      tokens.current.delete(armed.id);
    }, Math.max(0, armed.until - Date.now()));
    return () => clearTimeout(timer);
  }, [armed]);
  useEffect(() => {
    if (!toast || toast.kind === 'sent') return undefined;
    const timer = setTimeout(() => setToast(null), 3200);
    return () => clearTimeout(timer);
  }, [toast]);

  const run = async (id, label, confirm, body) => {
    const held = tokens.current.get(id);
    const decision = held ? {send: true, armed: null} : confirmTap(armed, id, confirm, Date.now());
    setArmed(decision.armed);
    if (!decision.send) return;
    tokens.current.delete(id);
    const at = Date.now();
    setToast({label, kind: 'sent', detail: t.menu.sent, at});
    try {
      const payload = {request_id: newRequestId(), ...body, ...(held ? {confirm_token: held} : {})};
      const result = await invokeConfirmed(client, id, payload, {confirmed: confirm || !!held});
      const ok = result && result.status === 'accepted';
      const detail = result && result.status === 'prepared' ? t.menu.inApp : ok ? t.menu.ran
        : t.menu.failed + (result && result.code ? ' · ' + result.code : '');
      setToast({label, kind: ok ? 'ran' : 'failed', detail, at});
    } catch (error) {
      if (error instanceof ApiError && error.confirmToken) {
        // A row the catalog did not mark, which the host wants confirmed: arm
        // it as its own first tap would have, holding the token.
        tokens.current.set(id, error.confirmToken);
        setArmed({id, until: Date.now() + 3000});
        setToast(null);
        return;
      }
      setToast({label, kind: 'failed', detail: t.menu.failed + (error && error.code ? ' · ' + error.code : ''), at});
    }
  };
  return {armed: armed && armed.id, toast, run};
}

function PanelToast({toast}) {
  if (!toast) return null;
  return (
    <div className="ptoast" role="status" data-testid="panel-toast" data-kind={toast.kind}>
      <Icon name={toast.kind === 'failed' ? 'warn' : 'check'} size={13} />
      <span className="ptoast__l">{toast.label}</span>
      <span className="ptoast__d">{toast.detail}</span>
    </div>
  );
}

function SymbolSmall() {
  return (
    <svg viewBox="0 0 96 96" width="18" height="18" fill="currentColor" shapeRendering="crispEdges" aria-hidden="true">
      <g transform="translate(4 4)">
        <path fillRule="evenodd"
          d="M0 20H4V12H8V8H12V4H20V0H68V4H76V8H80V12H84V20H88V68H84V76H80V80H76V84H68V88H20V84H12V80H8V76H4V68H0Z M24 32V56H28V60H32V64H56V60H60V56H64V32H60V28H56V24H32V28H28V32Z" />
        <path d="M32 36H40V48H32Z M48 36H56V48H48Z" />
      </g>
    </svg>
  );
}

function Glyph({name}) {
  if (name === 'symbol') return <span className="row__sym"><SymbolSmall /></span>;
  return <Icon name={name || 'circle'} size={18} />;
}

function PinnedGroup({editing, setEditing, empty, hint, testid, children}) {
  const {t} = useCopy();
  return (
    <div className="pinned" data-testid={testid}>
      <GroupLabel action={
        <button type="button" className="glabel__act" onClick={() => setEditing(!editing)} data-testid={testid + '-edit'}>
          {editing ? t.menu.done : t.menu.edit}
        </button>
      }>{t.menu.pinned}</GroupLabel>
      {empty ? <EmptyState title={t.menu.pinnedEmpty} detail={hint} testid={testid + '-empty'} /> : children}
      <div className="pinned__rule" />
    </div>
  );
}

function MenuRow({tree, entry, depth, glyph, expanded, armed, editing, pinned, appHref, onTap, onPin}) {
  const {t} = useCopy();
  const press = useLongPress(onPin);
  const kids = tree.childrenOf(entry.id).length > 0;
  const kind = action(entry);
  return (
    <Row glyph={<Glyph name={glyph} />} title={entry.label} depth={depth} testid={'menu-row-' + entry.id}
      disabled={!kids && kind === 'unavailable'} aria-expanded={kids ? expanded : undefined}
      onClick={() => {
        if (press.consumed()) return;
        onTap(entry);
      }}
      {...press.handlers}
      trailing={
        <span className="row__trail">
          {armed ? <span className="tapagain" data-testid="tap-again">{t.menu.tapAgain}</span> : null}
          {!kids && kind === 'app' ? (
            <a className="row__badge" href={appHref} target="_blank" rel="noopener noreferrer"
              data-testid={'menu-app-' + entry.id}>{t.menu.inApp}</a>
          ) : null}
          {kids ? (
            <span className={'row__cv' + (expanded ? ' open' : '')}>
              <Icon name={expanded ? 'down' : 'right'} size={14} />
            </span>
          ) : null}
          {editing ? (
            <PinButton pinned={pinned} label={fill(pinned ? t.menu.unpin : t.menu.pin, entry.label)} onClick={onPin} />
          ) : null}
        </span>
      }
    />
  );
}

function hostName(state) {
  return (state && state.host && typeof state.host.name === 'string' && state.host.name) || location.hostname.split('.')[0];
}

function MenuHalf({client, state, hostId, catalog, segments, onKeybindings}) {
  const {t} = useCopy();
  const [query, setQuery] = useState('');
  const [expanded, setExpanded] = useState([]);
  const [editing, setEditing] = useState(false);
  const [pins, togglePin] = usePins('menu');
  const runner = useRunner(client);
  const tree = useMemo(() => buildTree(catalog), [catalog]);
  const appHref = appLinks('host', hostId).universal;
  const q = query.trim();

  const tap = (entry) => {
    if (tree.childrenOf(entry.id).length) {
      setExpanded((prev) => (prev.includes(entry.id) ? prev.filter((x) => x !== entry.id) : prev.concat(entry.id)));
      return;
    }
    const kind = action(entry);
    if (kind === 'keybindings') return onKeybindings();
    if (kind !== 'run') return;
    runner.run(entry.id, entry.label, needsConfirm(entry), {catalog_revision: tree.revision || ''});
  };

  const rows = [];
  if (!q) {
    const walk = (parent, depth) => {
      for (const entry of tree.childrenOf(parent)) {
        const open = expanded.includes(entry.id);
        rows.push(
          <MenuRow key={entry.id} tree={tree} entry={entry} depth={depth}
            glyph={depth === 0 ? ROOT_GLYPH[entry.id] : 'circle'} expanded={open}
            armed={runner.armed === entry.id} editing={editing} pinned={pins.includes(entry.id)} appHref={appHref}
            onTap={tap} onPin={() => togglePin(entry.id, entry.label)} />,
        );
        if (open) walk(entry.id, depth + 1);
      }
    };
    walk('root', 0);
  }
  const hits = q ? search(tree, q) : [];

  return (
    <PanelArea shape="work" testid="menu-half" label={t.panels.menu}
      title={<span className="hostsw" data-testid="host-name"><PanelTitle>{hostName(state)}</PanelTitle></span>}
      accessory={segments}>
      <Field placeholder={t.menu.search} value={query} onChange={setQuery} onClear={() => setQuery('')}
        clearLabel={t.menu.clear} testid="menu-search" />
      <div className="plist">
        {!catalog ? <EmptyState title={t.menu.loading} testid="menu-loading" /> : null}
        {catalog && !q ? (
          <PinnedGroup editing={editing} setEditing={setEditing} empty={!pins.length} hint={t.menu.pinnedMenuHint}
            testid="pinned-menu">
            {pins.map((id) => (tree.byId.get(id) ? (
              <MenuRow key={id} tree={tree} entry={tree.byId.get(id)} depth={0} glyph={ROOT_GLYPH[rootOf(tree, id)]}
                expanded={false} armed={runner.armed === id} editing={editing} pinned appHref={appHref} onTap={tap}
                onPin={() => togglePin(id, tree.byId.get(id).label)} />
            ) : <Tombstone key={id} half="menu" id={id} editing={editing} onPin={() => togglePin(id)} />))}
          </PinnedGroup>
        ) : null}
        {catalog && q ? (
          hits.length ? (
            <>
              <GroupLabel>{t.menu.results}</GroupLabel>
              {hits.map((entry) => (
                <Row key={entry.id} glyph={<Glyph name={ROOT_GLYPH[rootOf(tree, entry.id)]} />} title={entry.label}
                  detail={pathOf(tree, entry.parent_id, t.menu.rootPath)} testid={'menu-hit-' + entry.id}
                  onClick={() => tap(entry)}
                  trailing={
                    <span className="row__trail">
                      {runner.armed === entry.id ? <span className="tapagain">{t.menu.tapAgain}</span> : null}
                      <PinButton pinned={pins.includes(entry.id)}
                        label={fill(pins.includes(entry.id) ? t.menu.unpin : t.menu.pin, entry.label)}
                        onClick={() => togglePin(entry.id, entry.label)} />
                    </span>
                  } />
              ))}
            </>
          ) : (
            <EmptyState title={t.menu.noMatch} detail={t.menu.noMatchHint} testid="menu-empty" />
          )
        ) : null}
        {catalog && !q ? (
          <>
            <GroupLabel>{t.menu.group}</GroupLabel>
            <div data-testid="omarchy-menu">{rows}</div>
          </>
        ) : null}
      </div>
      <PanelToast toast={runner.toast} />
    </PanelArea>
  );
}

function KeyRow({row, editing, pinned, armed, onPin, onRun}) {
  const {t} = useCopy();
  const press = useLongPress(onPin);
  return (
    <div className={'krow' + (row.enabled ? '' : ' dis')} data-testid={'key-row-' + row.id}>
      <button type="button" className="krow__tap" disabled={!row.enabled || undefined} aria-label={row.label}
        title={row.enabled ? t.keys.runHint : undefined}
        onClick={() => {
          if (press.consumed()) return;
          onRun(row);
        }}
        {...press.handlers}>
        <span className="krow__tx">
          <span className="krow__t1">{row.label}</span>
          {!row.enabled ? <span className="krow__t2">{t.menu.unavailable}</span> : null}
          {armed ? <span className="krow__t2 tapagain">{t.menu.tapAgain}</span> : null}
        </span>
        <KeyBlock display={row.shortcut_display || ''} />
      </button>
      {editing ? (
        <PinButton pinned={pinned} label={fill(pinned ? t.menu.unpin : t.menu.pin, row.label)} onClick={onPin} />
      ) : null}
    </div>
  );
}

function KeysHalf({client, state, catalog, shortcuts, segments, showUnrunnable = false}) {
  const {t} = useCopy();
  const [query, setQuery] = useState('');
  const [editing, setEditing] = useState(false);
  const [pins, togglePin] = usePins('keys');
  const runner = useRunner(client);
  const rows = useMemo(() => shortcutRows(shortcuts, showUnrunnable), [shortcuts, showUnrunnable]);
  const byId = useMemo(() => new Map(rows.map((row) => [row.id, row])), [rows]);
  const q = query.trim().toLowerCase();
  const list = q ? rows.filter((row) => row.label.toLowerCase().includes(q) || (row.shortcut_display || '').toLowerCase().includes(q)) : rows;
  const run = (row) => {
    const body = {catalog_revision: (catalog && catalog.revision) || (shortcuts && shortcuts.revision) || '',
      execution_context: {surface: 'omarchy'}};
    if (state && Number.isInteger(state.revision)) body.state_revision = state.revision;
    const target = state && state.focus && state.focus.target_token;
    if (row.requires_target && typeof target === 'string' && target) body.target_token = target;
    runner.run(row.action_ref || row.id, row.label, false, body);
  };
  return (
    <PanelArea shape="work" testid="keys-half" label={t.panels.keybindings} title={t.panels.keybindings} accessory={segments}>
      <Field placeholder={t.keys.search} value={query} onChange={setQuery} onClear={() => setQuery('')}
        clearLabel={t.menu.clear} testid="keys-search" />
      <div className="plist">
        {!q ? (
          <PinnedGroup editing={editing} setEditing={setEditing} empty={!pins.length} hint={t.menu.pinnedKeyHint}
            testid="pinned-keys">
            {pins.map((id) => (byId.get(id) ? (
              <KeyRow key={id} row={byId.get(id)} editing={editing} pinned armed={runner.armed === id}
                onPin={() => togglePin(id, byId.get(id).label)} onRun={run} />
            ) : (shortcuts ? <Tombstone key={id} half="keys" id={id} editing={editing} onPin={() => togglePin(id)} /> : null)))}
          </PinnedGroup>
        ) : null}
        {!shortcuts ? <EmptyState title={t.menu.loading} /> : null}
        {list.map((row) => (
          <KeyRow key={row.id} row={row} editing={editing} pinned={pins.includes(row.id)}
            armed={runner.armed === (row.action_ref || row.id)} onPin={() => togglePin(row.id, row.label)} onRun={run} />
        ))}
        {shortcuts && !list.length ? <EmptyState title={t.keys.noMatch} /> : null}
      </div>
      <PanelToast toast={runner.toast} />
    </PanelArea>
  );
}

function Segments({column, onChange}) {
  const {t} = useCopy();
  return (
    <div className="pseg" role="tablist" aria-label={t.panels.menu} data-testid="panel-segments">
      {[['menu', 'lines', t.panels.panelTab], ['keys', 'keyboard', t.panels.keybindings]].map(([id, glyph, label]) => (
        <button key={id} type="button" role="tab" aria-selected={column === id} className={column === id ? 'on' : ''}
          data-testid={'panel-segment-' + id} onClick={() => onChange(id)}>
          <Icon name={glyph} size={14} />
          {label}
        </button>
      ))}
    </div>
  );
}

export default function MenuPanel({client, state, layout, hostId, catalog, shortcuts, column, onColumn, onKeybindings,
  showUnrunnable = false}) {
  if (layout.twoColumn) {
    return (
      <div className="menu2" data-testid="panel-menu" data-mode="two">
        <div className="menu2__menu">
          <MenuHalf client={client} state={state} hostId={hostId} catalog={catalog} onKeybindings={() => {}} />
        </div>
        <i className="menu2__rule" />
        <div className="menu2__keys">
          <KeysHalf client={client} state={state} catalog={catalog} shortcuts={shortcuts} showUnrunnable={showUnrunnable} />
        </div>
      </div>
    );
  }
  const segments = <Segments column={column} onChange={onColumn} />;
  return (
    <div className="menu1" data-testid="panel-menu" data-mode="segmented">
      {column === 'keys' ? (
        <KeysHalf client={client} state={state} catalog={catalog} shortcuts={shortcuts} segments={segments}
          showUnrunnable={showUnrunnable} />
      ) : (
        <MenuHalf client={client} state={state} hostId={hostId} catalog={catalog} segments={segments}
          onKeybindings={onKeybindings} />
      )}
    </div>
  );
}
