import React, {useEffect, useRef, useState} from 'react';
import Icon from '../Icon.jsx';
import {fill} from '../lib/i18n.js';
import * as AT from '../lib/attachments.js';

// ATTACH-1: the paperclip, the chips and what went wrong - the App's
// `AttachButton` / `AttachmentChipStrip` (omodachi-ios `Surfaces/Attachments`).
// The paperclip sits inside the composer's box, before the words, so a
// phone-width field keeps its width (`.composer__box`).
//
// The paperclip asks Photos or Files, because a phone shows its photo library
// only for an image-only `accept` (Collie ADR 0060). A chip is a 40x40
// thumbnail for a picture and a tile with the name for a file, with its `#N`
// and an x; while it uploads it shows how far it got.

export function AttachButton({att, t, disabled, testid}) {
  const [open, setOpen] = useState(false);
  const photos = useRef(null);
  const files = useRef(null);
  const box = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const away = (event) => {
      if (box.current && !box.current.contains(event.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', away);
    return () => document.removeEventListener('pointerdown', away);
  }, [open]);
  const take = (event) => {
    att.add(event.target.files);
    event.target.value = '';
    setOpen(false);
  };
  return (
    <span className="attach" ref={box}>
      <button type="button" className={'attach__b' + (open ? ' on' : '')} aria-label={t.attach.button}
        aria-haspopup="menu" aria-expanded={open} disabled={disabled || undefined} data-testid={testid}
        onClick={() => setOpen(!open)}>
        <Icon name="attach" size={18} />
      </button>
      {open ? (
        <span className="attach__menu" role="menu">
          <button type="button" role="menuitem" className="attach__row" data-testid={testid + '-photos'}
            onClick={() => photos.current && photos.current.click()}>
            <Icon name="image" size={16} /><span>{t.attach.photos}</span>
          </button>
          <button type="button" role="menuitem" className="attach__row" data-testid={testid + '-files'}
            onClick={() => files.current && files.current.click()}>
            <Icon name="file" size={16} /><span>{t.attach.files}</span>
          </button>
        </span>
      ) : null}
      <input ref={photos} type="file" accept={AT.PHOTO_ACCEPT} multiple hidden onChange={take}
        data-testid={testid + '-photo-input'} />
      <input ref={files} type="file" accept={AT.FILE_ACCEPT} multiple hidden onChange={take}
        data-testid={testid + '-file-input'} />
    </span>
  );
}

export function AttachmentChips({att, draft, t, testid}) {
  if (!att.chips.length) return null;
  return (
    <div className="chips" data-testid={testid}>
      {att.chips.map((chip) => {
        const missing = chip.status === 'ready' && AT.markerMissing(draft, chip);
        const label = fill(chip.kind === 'image' ? t.attach.imageN : t.attach.fileN, chip.n);
        return (
          <span key={chip.n} className={'chip' + (missing ? ' chip--loose' : '') + (chip.status === 'uploading' ? ' chip--up' : '')}
            data-testid={testid + '-' + chip.n} title={missing ? t.attach.markerGone : chip.name}>
            {/* A picture this browser cannot draw (Chrome and HEIC) is a tile. */}
            {chip.thumb ? <img className="chip__thumb" src={chip.thumb} alt="" onError={() => att.dropThumb(chip.n)} />
              : <span className="chip__tile"><Icon name={chip.kind === 'image' ? 'image' : 'file'} size={16} /></span>}
            <span className="chip__text">
              <b>{label}{missing ? ' ←' : ''}</b>
              <span>{chip.status === 'uploading' ? fill(t.attach.uploading, Math.round((chip.progress || 0) * 100) + '%')
                : AT.shortName(chip.name)}</span>
            </span>
            {chip.status === 'uploading' ? (
              <i className="chip__bar" style={{width: Math.round((chip.progress || 0) * 100) + '%'}} aria-hidden="true" />
            ) : null}
            <button type="button" className="chip__x" aria-label={fill(t.attach.remove, chip.name)}
              data-testid={testid + '-remove-' + chip.n} onClick={() => att.remove(chip.n)}>
              <Icon name="close" size={12} />
            </button>
            {missing ? <span className="sr">{t.attach.markerGone}</span> : null}
          </span>
        );
      })}
    </div>
  );
}

export function AttachNotice({att, t, testid}) {
  if (!att.notice) return null;
  const text = fill(t.attach[att.notice.key] || t.attach.failed, att.notice.name || '');
  return (
    <div className="anotice anotice--warn" role="status" data-testid={testid}>
      <p>{text}</p>
      <button type="button" className="pushed__x" aria-label={t.attach.dismiss} onClick={att.dismissNotice}>
        <Icon name="close" size={12} />
      </button>
    </div>
  );
}

// A desktop drops files on the panel: they are attached like a pick.
export function useDropTarget(att, enabled) {
  const [over, setOver] = useState(false);
  const depth = useRef(0);
  if (!enabled) return {over: false, handlers: {}};
  const hasFiles = (event) => !!event.dataTransfer && Array.from(event.dataTransfer.types || []).includes('Files');
  return {
    over,
    handlers: {
      onDragEnter: (event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        depth.current += 1;
        setOver(true);
      },
      onDragOver: (event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = 'copy';
      },
      onDragLeave: (event) => {
        if (!hasFiles(event)) return;
        depth.current = Math.max(0, depth.current - 1);
        if (!depth.current) setOver(false);
      },
      onDrop: (event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        depth.current = 0;
        setOver(false);
        att.add(event.dataTransfer.files);
      },
    },
  };
}

export function DropVeil({over, t}) {
  return over ? <div className="dropveil" data-testid="attach-drop"><Icon name="attach" size={22} /><span>{t.attach.drop}</span></div> : null;
}
