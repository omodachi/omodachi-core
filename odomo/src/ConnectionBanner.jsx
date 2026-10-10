import React from 'react';
import {useCopy} from './copy.js';
import {healthWords, isLost} from './lib/herdr11.js';

// HERDR-11 §3 (the App's `ConnectionBanner`): the one connection clock in one
// line over the top of the panel. Amber after ~4 s without a word from the
// computer, red after ~15 s with the real cause and Try again. Nothing while
// the clock says live.
export default function ConnectionBanner({health, host, everLive = true, onRetry, inline = false}) {
  const {t} = useCopy();
  const words = healthWords(health, t, host, everLive);
  if (!words) return null;
  const lost = isLost(health);
  return (
    <div className={'conn' + (lost ? ' conn--lost' : '') + (inline ? ' conn--inline' : '')} role="status"
      data-testid={lost ? 'connection-banner-lost' : 'connection-banner-trouble'}>
      <span className="conn__dot" aria-hidden="true" />
      <span className="conn__words">{words}</span>
      {lost && onRetry ? <button type="button" className="conn__retry" onClick={onRetry}>{t.bar.tryAgain}</button> : null}
    </div>
  );
}
