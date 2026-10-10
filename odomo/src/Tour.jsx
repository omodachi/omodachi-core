import React, {useEffect, useLayoutEffect, useRef, useState} from 'react';
import {useCopy} from './copy.js';
import {fill} from './lib/i18n.js';
import {calloutFrame} from './lib/tour.js';

// TUTORIAL-1: one stop of the first-steps tour, drawn over the whole page -
// a dimmed page with a window cut where the bar (or one of its entries) is,
// an accent outline, and a callout beside it. Like the App's
// `TourSpotlight` (omodachi-ios `DesignSystem/Primitives/Tour.swift`).
//
// The dimming and the outline take no pointer: a tap on the bar still opens
// that panel, and App.jsx ends the tour when it does. Only the callout is
// interactive. Focus moves to the callout's heading on every stop, so a
// screen reader reads it; Escape skips. Reduce Motion drops the movement.

function targetOf(step) {
  if (typeof document === 'undefined') return null;
  const id = step === 'bar' ? 'app-bar' : 'bar-' + step;
  const el = document.querySelector('[data-testid="' + id + '"]');
  if (!el) return null;
  const box = el.getBoundingClientRect();
  return box.width && box.height ? {x: box.left, y: box.top, w: box.width, h: box.height} : null;
}

export default function Tour({state, onNext, onSkip}) {
  const {t} = useCopy();
  const T = t.tour;
  const step = state.steps[state.index];
  const last = state.index === state.steps.length - 1;
  const card = useRef(null);
  const heading = useRef(null);
  const [view, setView] = useState(() => ({
    width: typeof window === 'undefined' ? 1280 : window.innerWidth,
    height: typeof window === 'undefined' ? 800 : window.innerHeight,
    rect: targetOf(step),
  }));
  const [cardHeight, setCardHeight] = useState(200);

  // Where the thing is: now, and again when the window or the bar moves.
  useLayoutEffect(() => {
    const measure = () => setView({width: window.innerWidth, height: window.innerHeight, rect: targetOf(step)});
    measure();
    window.addEventListener('resize', measure);
    window.addEventListener('orientationchange', measure);
    const bar = document.querySelector('[data-testid="app-bar"]');
    if (bar) bar.addEventListener('scroll', measure, {passive: true});
    return () => {
      window.removeEventListener('resize', measure);
      window.removeEventListener('orientationchange', measure);
      if (bar) bar.removeEventListener('scroll', measure);
    };
  }, [step]);

  // How tall the callout wants to be: its own height with the words at
  // their full height (they shrink and scroll when the window is shorter).
  // Measured again whenever the words change size (a web font arriving,
  // a narrower window, another language).
  const wordsBox = useRef(null);
  const wordsInner = useRef(null);
  useLayoutEffect(() => {
    const measure = () => {
      const el = card.current;
      if (!el || !wordsBox.current || !wordsInner.current) return;
      setCardHeight(Math.ceil(el.offsetHeight - wordsBox.current.clientHeight + wordsInner.current.offsetHeight));
    };
    measure();
    if (typeof ResizeObserver === 'undefined' || !wordsInner.current) return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(wordsInner.current);
    return () => observer.disconnect();
  }, [step, view.width, view.height, t]);

  useEffect(() => {
    if (heading.current) heading.current.focus({preventScroll: true});
  }, [step]);

  useEffect(() => {
    const key = (event) => {
      if (event.key === 'Escape') onSkip();
    };
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  }, [onSkip]);

  const frame = calloutFrame({width: view.width, height: view.height, rect: view.rect, cardHeight});
  const r = view.rect;
  const words = T.steps[step];
  const progress = fill(T.progress, state.index + 1, state.steps.length);
  return (
    <div className="tour" data-testid="tour" data-step={step}>
      {r ? (
        <div className="tour__cut" aria-hidden="true"
          style={{left: r.x - 3 + 'px', top: r.y - 3 + 'px', width: r.w + 6 + 'px', height: r.h + 6 + 'px'}} />
      ) : <div className="tour__dim" aria-hidden="true" />}
      <section ref={card} className="tour__card" role="dialog" aria-modal="false" aria-label={T.label}
        aria-labelledby="tour-title" aria-describedby="tour-body" data-testid="tour-card"
        style={{left: frame.x + 'px', top: frame.y + 'px', width: frame.w + 'px', maxHeight: frame.h + 'px'}}>
        <p className="tour__n" aria-hidden="true" data-testid="tour-progress">{(state.index + 1) + ' / ' + state.steps.length}</p>
        <div className="tour__words" ref={wordsBox}>
          <div ref={wordsInner}>
            <h2 id="tour-title" ref={heading} tabIndex={-1} data-testid={'tour-step-' + step}>
              {words.title}<span className="sr">{' · ' + progress}</span>
            </h2>
            <p id="tour-body">{words.body}</p>
          </div>
        </div>
        <div className="tour__ctl">
          {last ? <span /> : (
            <button type="button" className="fbtn fbtn--ghost" data-testid="tour-skip" onClick={onSkip}>{T.skip}</button>
          )}
          <button type="button" className="fbtn fbtn--primary" data-testid={last ? 'tour-done' : 'tour-next'}
            onClick={onNext}>{last ? T.done : T.next}</button>
        </div>
      </section>
    </div>
  );
}
