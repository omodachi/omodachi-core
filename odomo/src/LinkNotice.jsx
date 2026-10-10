import React from 'react';
import Icon from './Icon.jsx';
import {fill} from './lib/i18n.js';
import {appLinks} from './lib/applink.js';

// LINKS-1: the one line after a link was tapped - what happened, in words,
// and the next step when there is one ("Watch in Omodachi Remote" after a file
// opened on the computer; "Open on the computer" beside a rewritten address
// that might only be served to the computer itself).
export function linkNoticeText(t, notice) {
  if (!notice) return '';
  switch (notice.kind) {
    case 'opening': return t.links.opening;
    case 'opened': return notice.line ? fill(t.links.openedAt, notice.name || '', notice.line) : fill(t.links.opened, notice.name || '');
    case 'here': return fill(t.links.here, notice.address) + ' ' + t.links.hereHint;
    case 'refused': return t.links.refused[notice.code] || fill(t.links.refusedUnknown, notice.code || '?');
    default: return '';
  }
}

export default function LinkNotice({notice, t, hostId, onDismiss, onComputer, testid}) {
  if (!notice) return null;
  return (
    <div className={'anotice lnotice' + (notice.kind === 'refused' ? ' anotice--warn' : '')} data-testid={testid}
      role="status">
      <p data-testid={testid + '-text'}>{linkNoticeText(t, notice)}</p>
      {notice.kind === 'opened' ? (
        <a className="tlink lnotice__a" href={appLinks('remote', hostId).universal} target="_blank" rel="noopener noreferrer"
          data-testid={testid + '-remote'}>{t.links.watch}</a>
      ) : null}
      {notice.kind === 'here' ? (
        <button type="button" className="tlink lnotice__a" data-testid={testid + '-computer'}
          onClick={() => onComputer(notice.source, notice.original)}>{t.links.onComputer}</button>
      ) : null}
      <button type="button" className="pushed__x" aria-label={t.links.dismiss} onClick={onDismiss}>
        <Icon name="close" size={12} />
      </button>
    </div>
  );
}
