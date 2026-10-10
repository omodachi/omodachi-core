// POLISH-U: the on-screen keyboard.
//
// On an iPhone, focusing a text box in Odomo (the Agent's composer, Herdr's
// composer, the search) made iOS scroll the whole page up by the keyboard's
// height: the panel header went off the top of the screen and the page only
// came back when the keyboard went away. iOS keeps the *layout* viewport the
// size of the screen and only shrinks the *visual* one, and Odomo's root is a
// screen-tall box (100lvh on the Home Screen, `position: fixed; inset: 0` in a
// tab), so the only way iOS can show the focused box is to move the page.
//
// So while a keyboard is up the root box follows the visual viewport: it is
// exactly as tall as what is left above the keyboard (and its accessory bar),
// and it starts where that visible part starts. The panels are flex columns,
// so the composer sits right above the keyboard and the header stays at the
// top; nothing is scrolled, so nothing jumps. `data-keyboard` on <html> and two
// custom properties carry this to styles.css; nothing here sets a style that
// CSP would refuse (CSSOM custom properties are not inline style markup).

// Less than this between the layout and the visual viewport is browser chrome
// (a bar that came or went), not a keyboard.
export const KEYBOARD_MIN = 120;

/** What the viewport says, as numbers: `null` when there is no visualViewport. */
export function readViewport(view) {
  const vv = view && view.visualViewport;
  if (!vv) return null;
  const layout = Math.max(view.document.documentElement.clientHeight || 0, view.innerHeight || 0);
  const height = Math.round(vv.height);
  const keyboard = layout - vv.height * vv.scale >= KEYBOARD_MIN && vv.scale <= 1.01;
  return {
    keyboard,
    height,
    // Where the visible part begins: in the document (a Home Screen app's root
    // is absolute) and in the layout viewport (a tab's root is fixed).
    pageTop: Math.round(vv.pageTop !== undefined ? vv.pageTop : (view.scrollY || 0) + (vv.offsetTop || 0)),
    offsetTop: Math.round(vv.offsetTop || 0),
  };
}

/** Puts `reading` on <html>; returns whether the keyboard state changed. */
export function applyViewport(root, reading) {
  const was = root.dataset.keyboard === 'up';
  if (reading && reading.keyboard) {
    root.style.setProperty('--om-vv-height', reading.height + 'px');
    root.style.setProperty('--om-vv-page-top', reading.pageTop + 'px');
    root.style.setProperty('--om-vv-offset-top', reading.offsetTop + 'px');
    root.dataset.keyboard = 'up';
  } else {
    root.style.removeProperty('--om-vv-height');
    root.style.removeProperty('--om-vv-page-top');
    root.style.removeProperty('--om-vv-offset-top');
    delete root.dataset.keyboard;
  }
  return was !== (root.dataset.keyboard === 'up');
}

/** Follows the visual viewport until the returned function is called. */
export function followKeyboard(view = typeof window === 'undefined' ? null : window) {
  const vv = view && view.visualViewport;
  if (!vv) return () => {};
  const root = view.document.documentElement;
  let frame = 0;
  const update = () => {
    frame = 0;
    const changed = applyViewport(root, readViewport(view));
    // The keyboard went away: whatever iOS scrolled to show the box goes too.
    if (changed && root.dataset.keyboard !== 'up' && (view.scrollY || view.scrollX)) view.scrollTo(0, 0);
  };
  const schedule = () => {
    if (!frame) frame = view.requestAnimationFrame(update);
  };
  vv.addEventListener('resize', schedule);
  vv.addEventListener('scroll', schedule);
  view.addEventListener('focusin', schedule);
  view.addEventListener('focusout', schedule);
  update();
  return () => {
    if (frame) view.cancelAnimationFrame(frame);
    vv.removeEventListener('resize', schedule);
    vv.removeEventListener('scroll', schedule);
    view.removeEventListener('focusin', schedule);
    view.removeEventListener('focusout', schedule);
    applyViewport(root, null);
  };
}
