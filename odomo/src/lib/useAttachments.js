// ATTACH-1: the chips above a composer, as the App's `AttachmentTray` keeps them.
//
// A picked file becomes a chip and a marker in the draft at once, then
// uploads (one at a time, in pick order) with its progress on the chip. A file
// the host would refuse anyway is refused here, in words, before a byte goes.
// Removing a chip takes its marker out of the draft and its file off the host;
// a send that lands clears the chips (their files stay for the agent to read,
// and core sweeps them after seven days).

import {useCallback, useEffect, useRef, useState} from 'react';
import * as AT from './attachments.js';

export function useAttachments({client, target, getDraft, setDraft, inputRef}) {
  const [chips, setChipsState] = useState([]);
  const [notice, setNotice] = useState(null);
  const ref = useRef([]);
  const next = useRef(1);
  const queue = useRef(Promise.resolve());
  const uploads = useRef(new Map());

  const setChips = useCallback((update) => {
    const value = typeof update === 'function' ? update(ref.current) : update;
    ref.current = value;
    setChipsState(value);
  }, []);
  const patch = useCallback((n, change) => setChips((all) => all.map((chip) => (chip.n === n ? {...chip, ...change} : chip))), [setChips]);

  const release = (chip) => {
    if (chip && chip.thumb && typeof URL !== 'undefined' && URL.revokeObjectURL) URL.revokeObjectURL(chip.thumb);
  };

  const add = useCallback((files) => {
    const list = Array.from(files || []);
    if (!list.length) return;
    let draft = getDraft();
    const input = inputRef && inputRef.current;
    let caret = input && typeof input.selectionStart === 'number' && document.activeElement === input
      ? input.selectionStart : null;
    const added = [];
    for (const file of list) {
      if (ref.current.length + added.length >= AT.MAX_CHIPS) {
        setNotice({key: 'tooMany', name: ''});
        break;
      }
      const refusal = AT.rejectAttachment(file);
      if (refusal) {
        setNotice({key: refusal, name: file.name || ''});
        continue;
      }
      const kind = AT.attachmentKind(file);
      const chip = {n: next.current++, kind, name: file.name || (kind === 'image' ? 'image' : 'file'), size: file.size,
        status: 'uploading', progress: 0, id: null, thumb: null};
      if (kind === 'image' && typeof URL !== 'undefined' && URL.createObjectURL) {
        try { chip.thumb = URL.createObjectURL(file); } catch { chip.thumb = null; }
      }
      const placed = AT.insertMarker(draft, caret, AT.markerFor(chip));
      draft = placed.text;
      caret = placed.caret;
      added.push({chip, file});
    }
    if (!added.length) return;
    setChips((all) => all.concat(added.map((item) => item.chip)));
    setDraft(draft);
    for (const {chip, file} of added) {
      queue.current = queue.current.then(async () => {
        if (!ref.current.some((c) => c.n === chip.n)) return; // removed before its turn
        const pending = client.upload(file, AT.uploadQuery(chip.name, target()), (fraction) => patch(chip.n, {progress: fraction}));
        uploads.current.set(chip.n, pending);
        try {
          const done = await pending;
          if (!ref.current.some((c) => c.n === chip.n)) {
            client.deleteUpload(done.id).catch(() => {});
            return;
          }
          patch(chip.n, {status: 'ready', progress: 1, id: done.id, name: done.name || chip.name});
        } catch (error) {
          if (error && error.code === 'upload_cancelled') return;
          const gone = ref.current.find((c) => c.n === chip.n);
          if (!gone) return;
          release(gone);
          setChips((all) => all.filter((c) => c.n !== chip.n));
          setDraft(AT.removeMarker(getDraft(), AT.markerFor(gone)));
          setNotice({key: AT.refusalKey(error && error.code), name: chip.name});
        } finally {
          uploads.current.delete(chip.n);
        }
      });
    }
  }, [client, getDraft, inputRef, patch, setChips, setDraft, target]);

  const remove = useCallback((n) => {
    const chip = ref.current.find((c) => c.n === n);
    if (!chip) return;
    const pending = uploads.current.get(n);
    if (pending && pending.abort) pending.abort();
    if (chip.id) client.deleteUpload(chip.id).catch(() => {});
    release(chip);
    setChips((all) => all.filter((c) => c.n !== n));
    setDraft(AT.removeMarker(getDraft(), AT.markerFor(chip)));
  }, [client, getDraft, setChips, setDraft]);

  // A send landed: the files are the agent's now; numbering starts again.
  const clear = useCallback(() => {
    for (const chip of ref.current) release(chip);
    setChips([]);
    next.current = 1;
  }, [setChips]);

  // Walking away from a draft (another pane): its uploads go too.
  const discard = useCallback(() => {
    for (const chip of ref.current) {
      const pending = uploads.current.get(chip.n);
      if (pending && pending.abort) pending.abort();
      if (chip.id) client.deleteUpload(chip.id).catch(() => {});
      release(chip);
    }
    setChips([]);
    next.current = 1;
  }, [client, setChips]);

  // The panel goes away with chips nobody sent: their files go too.
  const discardRef = useRef(discard);
  discardRef.current = discard;
  useEffect(() => () => discardRef.current(), []);

  // Paste an image (or a file) into the composer: it becomes a chip.
  const onPaste = useCallback((event) => {
    const data = event.clipboardData;
    if (!data) return;
    const files = Array.from(data.files || []);
    if (!files.length && data.items) {
      for (const item of Array.from(data.items)) {
        if (item.kind === 'file') {
          const file = item.getAsFile();
          if (file) files.push(file);
        }
      }
    }
    if (!files.length) return;
    event.preventDefault();
    add(files.map((file, index) => (file.name ? file
      : new File([file], 'pasted-' + (index + 1) + '.' + (String(file.type).split('/')[1] || 'png'), {type: file.type}))));
  }, [add]);

  return {chips, notice, add, remove, clear, discard, onPaste, dismissNotice: () => setNotice(null),
    say: (key, name = '') => setNotice({key, name}),
    dropThumb: (n) => setChips((all) => all.map((chip) => {
      if (chip.n !== n || !chip.thumb) return chip;
      release(chip);
      return {...chip, thumb: null};
    })),
    refs: () => AT.sendRefs(ref.current), uploading: AT.uploading(chips), ready: chips.some((c) => c.status === 'ready')};
}
