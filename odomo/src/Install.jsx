import React, {useEffect, useState} from 'react';
import {useCopy} from './copy.js';
import Icon, {Symbol} from './Icon.jsx';
import {FlowButton} from './ds.jsx';
import {fill} from './lib/i18n.js';
import {installPrompt} from './lib/install.js';

// LITE-1e. The install screen (iPhone and iPad, before anything else), the
// three steps it draws (also under Settings › Notifications when a browser
// cannot have push), and the quiet offer on Android and desktop.
//
// The steps are drawn with Odomo's own glyphs on the App's grid (18 view box,
// 1.5 stroke, butt caps, zero radius) - never Apple's artwork.

// Odomo's own: a box with an arrow out of its top (Share), and a box with a
// plus in it (Add to Home Screen).
const GLYPHS = {
  share: 'M6.5 6.5H3.5v10h11v-10h-3 M9 1.5v9.5 M5.75 4.75L9 1.5l3.25 3.25',
  add: 'M2 2h14v14H2z M9 5.5v7M5.5 9h7',
};

function Glyph({name, size = 22}) {
  return (
    <svg viewBox="0 0 18 18" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.5"
      strokeLinecap="butt" strokeLinejoin="miter" aria-hidden="true" focusable="false"
      style={{display: 'block', flex: 'none'}}>
      <path d={GLYPHS[name]} />
    </svg>
  );
}

export function InstallSteps({testid = 'install-steps'}) {
  const {t} = useCopy();
  const I = t.install;
  const steps = [
    [<Glyph key="g" name="share" />, I.step1, I.step1Detail],
    [<Glyph key="g" name="add" />, I.step2, I.step2Detail],
    [<Symbol key="g" size={22} />, I.step3, I.step3Detail],
  ];
  return (
    <ol className="isteps" data-testid={testid}>
      {steps.map(([glyph, title, detail], index) => (
        <li className="istep" key={index} data-step={index + 1}>
          <span className="istep__n" aria-hidden="true">{index + 1}</span>
          <span className="istep__g">{glyph}</span>
          <span className="istep__tx">
            <b>{title}</b>
            <span>{detail}</span>
          </span>
        </li>
      ))}
    </ol>
  );
}

function hostLabel() {
  const name = (typeof location !== 'undefined' && location.hostname) || '';
  return /^[0-9.:[\]]+$/.test(name) ? name : name.split('.')[0] || name;
}

// iPhone and iPad, not installed: this is the whole page. No "pair" here and
// no way around it (Leo, 2026-10-02): the Home Screen app pairs on its own.
export default function InstallScreen({device, hadPairing}) {
  const {t} = useCopy();
  const I = t.install;
  const why = [
    ['bell', fill(I.whyNotify, device)],
    ['apps', I.whyOpen],
    ['plug', I.whyStays],
    ['display', I.whyStore],
  ];
  return (
    <div className="pair inst" data-testid="install" data-device={device}>
      <div className="pair__col">
        <span className="pair__mark"><Symbol size={44} /></span>
        <h1 className="pair__title">{I.title}</h1>
        <p className="pair__lead">{fill(I.lead, hostLabel(), device)}</p>

        <h2 className="inst__h">{I.whyTitle}</h2>
        <ul className="iwhy" data-testid="install-why">
          {why.map(([icon, text]) => (
            <li key={icon}><Icon name={icon} size={18} /><span>{text}</span></li>
          ))}
        </ul>

        <h2 className="inst__h">{I.howTitle}</h2>
        <InstallSteps />
        <p className="pair__note" data-testid="install-first">{fill(I.whyFirst, device)}</p>
        {hadPairing ? <p className="pair__note" data-testid="install-old-pairing">{I.oldPairing}</p> : null}
        <p className="pair__note">{I.otherApp}</p>
      </div>
    </div>
  );
}

// Chromium's prompt, as React state.
export function usePrompt() {
  const [state, setState] = useState(() => installPrompt.state());
  useEffect(() => installPrompt.subscribe(() => setState(installPrompt.state())), []);
  return state;
}

// Android and desktop browsers, not installed: a quiet card that never stands
// in the way. Android always says how (the button when Chrome offers a prompt,
// the menu steps when it does not); a desktop browser only speaks up when it
// can actually install.
export function InstallOffer({platform, testid = 'install-offer', compact = false}) {
  const {t} = useCopy();
  const I = t.install;
  const prompt = usePrompt();
  if (prompt.installedNow) return <p className="snote" data-testid={testid + '-done'}>{I.installedNow}</p>;
  if (platform !== 'android' && !prompt.available) return null;
  return (
    <div className={'ioffer' + (compact ? ' ioffer--compact' : '')} data-testid={testid} data-platform={platform}>
      {compact ? <b className="ioffer__t">{I.offerTitle}</b> : null}
      <p className="ioffer__b">{platform === 'android' ? I.offerAndroid : I.offerDesktop}</p>
      {prompt.available ? (
        <div className="ioffer__act">
          <FlowButton kind="secondary" onClick={() => installPrompt.prompt().catch(() => null)}
            data-testid={testid + '-button'}>{I.installButton}</FlowButton>
        </div>
      ) : (
        <p className="ioffer__b" data-testid={testid + '-steps'}>{I.androidSteps}</p>
      )}
    </div>
  );
}
