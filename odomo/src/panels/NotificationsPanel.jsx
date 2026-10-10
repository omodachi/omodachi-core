import React, {useEffect, useState} from 'react';
import Icon from '../Icon.jsx';
import {PanelArea, GroupLabel, EmptyState} from '../ds.jsx';
import {useCopy} from '../copy.js';
import {fill} from '../lib/i18n.js';
import * as storage from '../lib/storage.js';

// Notifications (L4), adapted from omodachi-web
// `src/app/panels/NotificationsPanel.jsx` at main 0da28c9: the host's mirrored
// notifications (`GET /v1/notifications`, then `notification.posted` live on
// the event stream). Do Not Disturb is the host's own switch. Hiding a row is
// this browser's business only - it never dismisses anything on the computer.
//
// NOTIFY-3: a tap opens the row (the whole body, never cut). A row about one of
// Omodachi's own requests (`kind`) says where it is decided - on the computer -
// and core withdraws it (`withdrawn`) the moment it is, so it is never left
// here to be cleared by hand. "Clear all" hides every row at once.

const RULE = {critical: 'var(--om-red)', normal: 'var(--om-line)', low: 'var(--om-dim)'};

// NOTIFY-3: one event, applied to the list. `withdrawn` is a request decided
// on the computer: the row goes. Any other update is a toast that left the
// desktop's screen (LITE-1b): the row stays, as history.
export function notificationEvent(rows, event) {
  const payload = event && event.payload;
  if (!payload || typeof payload.id !== 'string') return rows;
  if (event.type === 'notification.posted') {
    return [payload, ...(rows || []).filter((row) => row.id !== payload.id)].slice(0, 200);
  }
  if (event.type === 'notification.updated') {
    if (payload.withdrawn === true) return (rows || []).filter((row) => row.id !== payload.id);
    return (rows || []).map((row) => (row.id === payload.id ? {...row, ...payload} : row));
  }
  return rows;
}

// NOTIFY-3: what an opened row adds under its text, or null.
export function requestNote(row, N) {
  return row && (row.kind === 'pairing_request' || row.kind === 'ssh_request') ? N.decideOnComputer : null;
}

// What arrived since this browser last opened the panel.
export function unreadCount(rows, seen = storage.local('notifications.seen', 0)) {
  return (rows || []).filter((row) => Number(row.timestamp) > seen).length;
}

function when(timestamp, lang) {
  try {
    return new Date(Number(timestamp)).toLocaleTimeString(lang === 'zh-Hans' ? 'zh-CN' : 'en-GB',
      {hour: '2-digit', minute: '2-digit'});
  } catch {
    return '';
  }
}

export default function NotificationsPanel({rows, dnd, onDnd, highlight}) {
  const {t, lang} = useCopy();
  const N = t.notifications;
  const [hidden, setHidden] = useState(() => new Set(storage.local('notifications.hidden', [])));
  const [seen] = useState(() => storage.local('notifications.seen', 0));
  useEffect(() => {
    const newest = Math.max(0, ...(rows || []).map((row) => Number(row.timestamp) || 0));
    if (newest > storage.local('notifications.seen', 0)) storage.setLocal('notifications.seen', newest);
  }, [rows]);
  // Newest first, whichever way they arrived (the history read, then live).
  const list = (rows || []).filter((row) => !hidden.has(row.id))
    .sort((a, b) => (Number(b.timestamp) || 0) - (Number(a.timestamp) || 0));
  const unread = list.filter((row) => Number(row.timestamp) > seen).length;
  // LITE-1b: opened from a push about one notification - show which.
  useEffect(() => {
    if (!highlight) return;
    const node = document.querySelector('[data-testid="notification-' + CSS.escape(highlight) + '"]');
    if (node && node.scrollIntoView) node.scrollIntoView({block: 'center'});
  }, [highlight, rows]);
  const [open, setOpen] = useState(null);
  const hide = (ids) => setHidden((current) => {
    const next = new Set(current);
    for (const id of [].concat(ids)) next.add(id);
    storage.setLocal('notifications.hidden', [...next].slice(-500));
    return next;
  });
  const toggle = (
    <button type="button" className={'dnd' + (dnd ? ' on' : '')} aria-pressed={dnd} aria-label={N.dnd}
      data-testid="notifications-dnd" onClick={() => onDnd(!dnd)}>
      <Icon name="moon" size={13} />
      {N.dnd}
    </button>
  );
  return (
    <PanelArea title={t.panels.notifications} accessory={toggle} testid="panel-notifications" label={t.panels.notifications}>
      {dnd ? <p className="snote">{N.dndOn}</p> : null}
      {rows && list.length === 0 ? (
        <EmptyState title={N.empty} detail={N.emptyDetail} testid="notifications-empty" />
      ) : (
        <>
          <div className="nhead">
            <GroupLabel>{unread ? fill(N.unread, unread) : N.all}</GroupLabel>
            {list.length ? (
              <button type="button" className="nhead__clear" data-testid="notifications-clear"
                onClick={() => hide(list.map((row) => row.id))}>{N.clear}</button>
            ) : null}
          </div>
          {list.map((row) => {
            const expanded = open === row.id;
            const note = expanded ? requestNote(row, N) : null;
            return (
              <div key={row.id} className={'nrow' + (row.active ? '' : ' past') + (row.id === highlight ? ' hl' : '')
                + (expanded ? ' open' : '')}
                data-testid={'notification-' + row.id} data-active={row.active ? '1' : '0'} data-kind={row.kind || ''}>
                <i className="nrow__rule" style={{background: RULE[row.urgency] || RULE.normal}} />
                <button type="button" className="nrow__tx" aria-expanded={expanded}
                  data-testid={'notification-open-' + row.id} onClick={() => setOpen(expanded ? null : row.id)}>
                  <span className="nrow__meta">
                    <span>{row.app}</span>
                    <span className="nrow__at">{when(row.timestamp, lang)}</span>
                    <span className="grow" />
                    {row.active ? null : <span className="nrow__at">{N.history}</span>}
                  </span>
                  <span className="nrow__sum">{row.summary}</span>
                  {row.body ? <span className="nrow__body">{row.body}</span> : null}
                  {note ? <span className="nrow__why" data-testid={'notification-why-' + row.id}>{note}</span> : null}
                </button>
                <button type="button" className="nrow__x" aria-label={N.hide} data-testid={'notification-hide-' + row.id}
                  onClick={() => hide(row.id)}>
                  <Icon name="close" size={13} />
                </button>
              </div>
            );
          })}
        </>
      )}
      <p className="snote">{N.live}</p>
    </PanelArea>
  );
}
