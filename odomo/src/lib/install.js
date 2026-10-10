// LITE-1e: where Odomo is running, and whether to ask for it to be installed.
//
// Three answers, from the platform and the display mode:
//
//   none   it is installed (a Home Screen app, an installed window): never a guide.
//   block  iPhone / iPad, not installed - a Safari tab, another iOS browser, or
//          the Omodachi app's in-app Safari view (SFSafariViewController, which
//          is not standalone and cannot be told apart from a Safari tab). Odomo
//          shows only the install screen and never offers to pair: on iOS a Home
//          Screen app keeps its own storage (measured on iOS 26.5 and 27,
//          LITE-1e report §1), so a pairing made in the tab would not come along,
//          and push only exists in the Home Screen app.
//   offer  Android and desktop browsers, not installed. Push works in the tab and
//          an installed app shares the browser's storage, so pairing stays in
//          the tab and installing is only offered.
//
// This is a UX gate in the page, not a security control: core accepts a pairing
// request from a browser tab exactly as before.

const DISPLAY_MODES = ['standalone', 'fullscreen', 'minimal-ui', 'window-controls-overlay'];

// 'ios' (iPhone, iPad, iPod - every browser there is WebKit and has no
// `navigator.userAgentData`), 'android' or 'desktop'.
export function platformOf(nav) {
  if (!nav) return 'desktop';
  const ua = String(nav.userAgent || '');
  const data = nav.userAgentData;
  if (data && /android/i.test(String(data.platform || ''))) return 'android';
  if (/\bAndroid\b/.test(ua)) return 'android';
  if (!data) {
    if (/\b(iPhone|iPad|iPod)\b/.test(ua)) return 'ios';
    // iPadOS Safari asks for the desktop site and calls itself a Mac; a Mac has no touch screen.
    if (/\bMacintosh\b/.test(ua) && (nav.maxTouchPoints || 0) > 1) return 'ios';
  }
  return 'desktop';
}

// The word for the device in a sentence, on iOS.
export function appleDevice(nav) {
  const ua = String((nav && nav.userAgent) || '');
  if (/\biPad\b/.test(ua) || /\bMacintosh\b/.test(ua)) return 'iPad';
  return 'iPhone';
}

export function installed(env) {
  if (!env) return false;
  const nav = env.navigator || {};
  if (nav.standalone === true) return true;
  if (typeof env.matchMedia !== 'function') return false;
  return DISPLAY_MODES.some((mode) => {
    try {
      return env.matchMedia('(display-mode: ' + mode + ')').matches;
    } catch {
      return false;
    }
  });
}

export function installGate({platform, isInstalled}) {
  if (isInstalled) return 'none';
  return platform === 'ios' ? 'block' : 'offer';
}

export function readInstall(env = globalThis) {
  const nav = (env && env.navigator) || null;
  const platform = platformOf(nav);
  const isInstalled = installed(env);
  return {platform, installed: isInstalled, gate: installGate({platform, isInstalled}),
    device: platform === 'ios' ? appleDevice(nav) : null};
}

// Chromium's install prompt (Android Chrome, desktop Chrome and Edge). It can
// fire before React has drawn anything, so main.jsx starts listening at once;
// whoever draws an "Install" button asks here whether there is a prompt to call.
export function promptStore() {
  let deferred = null;
  let done = false;
  const listeners = new Set();
  const tell = () => listeners.forEach((listener) => listener());
  return {
    listen(target) {
      if (!target || typeof target.addEventListener !== 'function') return;
      target.addEventListener('beforeinstallprompt', (event) => {
        // Keep Chrome's own mini-infobar away: the offer is Odomo's, and quiet.
        event.preventDefault();
        deferred = event;
        tell();
      });
      target.addEventListener('appinstalled', () => {
        deferred = null;
        done = true;
        tell();
      });
    },
    state: () => ({available: !!deferred, installedNow: done}),
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    // Inside the tap: Chrome shows its own dialog. The event can be used once.
    async prompt() {
      const event = deferred;
      if (!event) return 'unavailable';
      deferred = null;
      tell();
      await event.prompt();
      const choice = await event.userChoice.catch(() => null);
      const outcome = (choice && choice.outcome) || 'dismissed';
      if (outcome === 'accepted') done = true;
      tell();
      return outcome;
    },
  };
}

export const installPrompt = promptStore();
