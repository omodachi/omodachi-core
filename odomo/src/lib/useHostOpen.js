// LINKS-1: what a tap on a link in the Agent or the Herdr mirror does.
//
// - A web address opens in a new tab. When it points at the computer itself
//   (`localhost`, `0.0.0.0`, the host's own names) it is first rewritten to
//   the road this browser came in on, and a quiet line says that a server
//   listening only on the computer cannot be reached from here, with "Open on
//   the computer" beside it.
// - A file opens on the computer (`POST /v1/host/open`). Odomo has no Remote,
//   so the line that confirms it carries "Watch in Omodachi Remote", the
//   universal link into the App's Remote.
//
// `source` is what core checks the link against: `{kind: 'agent', item}` or
// `{kind: 'herdr', pane, session}`.

import {useCallback, useEffect, useRef, useState} from 'react';
import {hostLocal, rewrite, roadOf} from './links.js';

export function openHere(url, open = (...args) => globalThis.open && globalThis.open(...args)) {
  open(url, '_blank', 'noopener,noreferrer');
}

// The decision, apart from React: `here` (let the anchor navigate), `rewritten`
// (open this address instead), or `computer` (ask core).
export function decide(link, roads, hostname) {
  if (link.kind === 'path') return {action: 'computer'};
  const self = hostname ? [hostname] : [];
  if (!hostLocal(link.target, roads, self)) return {action: 'here'};
  return {action: 'rewritten', address: rewrite(link.target, roadOf(hostname, roads), roads, self)};
}

export function useHostOpen({client, location = globalThis.location}) {
  const [notice, setNotice] = useState(null);
  // Loaded when the panel opens, so a tap can decide at once: a new tab has
  // to be opened inside the tap, or Safari blocks it.
  const roads = useRef({endpoints: []});
  useEffect(() => {
    let live = true;
    client.get('/v1/host/roads').then((value) => {
      if (live && value && Array.isArray(value.endpoints)) roads.current = value;
    }).catch(() => {});
    return () => { live = false; };
  }, [client]);

  const toComputer = useCallback(async (source, body) => {
    setNotice({kind: 'opening'});
    try {
      const result = await client.post('/v1/host/open', {source, ...body});
      setNotice({kind: 'opened', name: result && result.name, line: result && result.at_line ? result.line : null});
    } catch (error) {
      setNotice({kind: 'refused', code: error && error.code ? error.code : 'host_unreachable'});
    }
  }, [client]);

  // True: the caller lets its anchor navigate. False: handled here.
  const openLink = useCallback((source, link) => {
    const hostname = location && location.hostname;
    const choice = decide(link, roads.current, hostname);
    if (choice.action === 'computer') {
      toComputer(source, {path: link.target});
      return false;
    }
    if (choice.action === 'here') return true;
    openHere(choice.address);
    setNotice({kind: 'here', address: choice.address, original: link.target, source});
    return false;
  }, [location, toComputer]);

  return {
    notice,
    dismiss: () => setNotice(null),
    openLink,
    openURLOnComputer: (source, url) => toComputer(source, {url}),
  };
}
