// AGENT-UI-2 (Leo: the Agent composer's "Message your agent…" was too big -
// it broke onto a second line and was cut off). A placeholder is one line at
// the box's own text size, or it is the shorter wording; it is never shrunk
// and never wrapped. On a phone with a turn running, Send/Steer and Interrupt
// leave the box about 100 px - room for "Message…", not for the whole line.
import {useLayoutEffect, useState} from 'react';

/** The first of `candidates` (longest first) whose width fits `room`; the last one otherwise. */
export function pickPlaceholder(room, candidates, measure) {
  for (const text of candidates) if (measure(text) <= room) return text;
  return candidates[candidates.length - 1];
}

let canvas = null;
function measurer(element) {
  const style = getComputedStyle(element);
  canvas = canvas || document.createElement('canvas');
  const context = canvas.getContext('2d');
  if (!context) return null;
  context.font = style.fontStyle + ' ' + style.fontWeight + ' ' + style.fontSize + ' ' + style.fontFamily;
  return {room: element.clientWidth - parseFloat(style.paddingLeft || '0') - parseFloat(style.paddingRight || '0'),
    measure: (text) => context.measureText(text).width};
}

/** The placeholder that fits the box `ref` points at, kept right as the box resizes. */
export function useFittingPlaceholder(ref, candidates) {
  const key = candidates.join('\u0000');
  const [text, setText] = useState(candidates[0]);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element || typeof getComputedStyle !== 'function' || typeof document === 'undefined') return undefined;
    const update = () => {
      const fit = measurer(element);
      if (fit && fit.room > 0) setText(pickPlaceholder(fit.room, candidates, fit.measure));
    };
    update();
    if (typeof ResizeObserver !== 'function') return undefined;
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref, key]); // eslint-disable-line react-hooks/exhaustive-deps
  return text;
}
