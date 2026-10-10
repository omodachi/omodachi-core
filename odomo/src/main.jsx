import React from 'react';
import {createRoot} from 'react-dom/client';
import App from './App.jsx';
import {installPrompt} from './lib/install.js';
import {followKeyboard} from './lib/viewport.js';
import './styles.css';

// LITE-1e: Chromium can offer its install prompt before React has drawn
// anything; keep it for the "Install Odomo" button.
installPrompt.listen(window);
// POLISH-U: while the on-screen keyboard is up, the page is the part of the
// screen above it, so focusing a box never scrolls the page away.
followKeyboard(window);

createRoot(document.getElementById('root')).render(<App />);

// The worker that makes the Home Screen app an installed app (and, in
// LITE-1b, receives push). Its scope is /odomo/; it caches nothing.
if ('serviceWorker' in navigator && window.isSecureContext) {
  navigator.serviceWorker.register(new URL('./sw.js', document.baseURI).href, {scope: './'}).catch(() => {});
}
