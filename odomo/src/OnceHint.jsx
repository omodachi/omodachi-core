import React, {useEffect, useState} from 'react';
import Icon from './Icon.jsx';
import {dismissHint, hintSeen} from './lib/hints.js';

// HERDR-4 / ODOMO-2: a one-time tip, until it is put away in this browser.
// TUTORIAL-1: one component and one registry (lib/hints.js) for both panels,
// so "Show the tour again" brings every tip back.
// HERDR-8: `once` - shown on this visit only: marked seen the moment it shows
// (the phone's Herdr tip, where every row counts).
export default function OnceHint({id, text, once = false}) {
  const [shown, setShown] = useState(() => !hintSeen(id));
  useEffect(() => {
    if (once && shown) dismissHint(id);
  }, [once, shown, id]);
  if (!shown) return null;
  return (
    <div className="ohint" data-testid={'hint-' + id}>
      <span>{text}</span>
      <button type="button" className="ohint__x" aria-label="×" onClick={() => {
        dismissHint(id);
        setShown(false);
      }}><Icon name="close" size={12} /></button>
    </div>
  );
}
