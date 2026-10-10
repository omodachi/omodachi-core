# Odomo

The web app `omodachid` serves at `https://<host>/odomo/` (LITE-1a). It is the
host's own page: pair a browser once (the person at the computer approves it
on the Omodachi panel) and it has the Omarchy menu, the keybindings and the
host's notifications, live, and (ODOMO-2) the App's Herdr and Agent panels:
the default agent's conversation with its approvals and `/` commands, and
Herdr's sessions, panes, a live mirror of the pane on screen, keys, the
composer, Quick and Type. No screen and no SSH, ever: Remote is a link into
the app, and SSH is not here (a browser cannot open it).

## Build

The built app is committed to `src/omodachi_core/data/odomo/`, so the host
never runs a JavaScript toolchain and the plugin's tree check sees the same
bytes as the repository. `scripts/verify_odomo.py` proves those bytes are what
this directory builds to.

```sh
cd odomo
npm ci                      # hash-locked; .npmrc forbids install scripts
npm test                    # node:test, no browser
cd .. && .venv/bin/python scripts/verify_odomo.py --write   # rebuild data/odomo
.venv/bin/python scripts/verify_odomo.py                    # clean rebuild + byte compare
```

Pinned toolchain: Node `.node-version` (major), every package in
`package-lock.json` with its sha512. `npm run e2e -- <odomo-url> <out-dir>`
drives the system Chrome in a throwaway context (`tests/e2e/chrome.mjs`).

## Provenance

`src/ds.jsx`, `src/Icon.jsx`, `src/layout.js`, the English strings in
`src/lib/i18n.js`, most of `src/styles.css` and the shape of `src/Sidebar.jsx`
and the panels are copied from omodachi-web (main `0da28c9`, same author, MIT)
and say so in their headers. They are kept in step by hand. The site's own
build - which makes no requests and stores nothing - stays separate and is not
touched by anything here. The Chinese strings are omodachi-ios's own
`zh-Hans` (`Localizable.xcstrings`) wherever the App has the string.
`src/font/` is the JetBrains Mono subset the site ships (OFL 1.1, licence
beside it and in `public/`), and AGENT-UI-2's Nerd Font symbols for the Herdr mirror: Symbols Nerd Font
Mono from Nerd Fonts v3.5.1 (MIT, the face omodachi-ios bundles whole), cut by
`scripts/subset-nerd-symbols.py` into one WOFF2 per Private Use Area, each behind a
`unicode-range` so it is fetched only when a pane shows one of its glyphs
(`SymbolsNerdFont-LICENSE.txt` beside them and in `public/`). The Herdr mirror's terminal core is
`@xterm/headless` 6.0.0 (MIT, hash-locked like everything else; its licence
is `public/xterm-LICENSE.txt`), drawn on a canvas by `src/lib/term.js`
because xterm.js's own renderer injects style elements the CSP refuses. It is
in its own chunk (`HerdrPanel-*.js`), fetched the first time Herdr opens.
HERDR-9's choices strip reads a pane's dialog with `src/lib/choices.js`, the
same rules as the App's `HerdrChoices.swift`, both ported from ColliePWA
(AltanS/collie `25468b0`, MIT; its licence is `public/collie-LICENSE.txt`;
`tests/fixtures/herdr9/collie--*.txt` are its pane captures, used as test
fixtures). The name and logo are ColliePWA's and appear nowhere in Odomo. The icons are drawn from the Omodachi symbol by
`scripts/make-icons.py`.

AGENT-COLLIE-1: the Agent panel's run folding (`groupRuns`, the six-step live
tail), its step lines and find's stepping and highlighting
(`src/lib/agentSteps.js`) are ported from Collie (https://github.com/AltanS/collie,
`web/src/components/chat-cards.tsx`, `web/src/lib/transcript-search.ts`), under
its MIT licence:

> MIT License. Copyright (c) 2026 Altan Sarisin. Permission is hereby granted,
> free of charge, to any person obtaining a copy of this software and
> associated documentation files (the "Software"), to deal in the Software
> without restriction, including without limitation the rights to use, copy,
> modify, merge, publish, distribute, sublicense, and/or sell copies of the
> Software, and to permit persons to whom the Software is furnished to do so,
> subject to the following conditions: The above copyright notice and this
> permission notice shall be included in all copies or substantial portions of
> the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
> EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
> MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO
> EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES
> OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
> ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
> DEALINGS IN THE SOFTWARE.

The data is core's own structured rows (codex's items), never a parsed
screen, and nothing of Collie's name, branding or artwork is in the page.

The agent's animated face (MOTION-1) is the brand study's own model,
omodachi-brand `agent-motion/study.html` at `8318df5`, ported once into
`src/lib/agentFace.js` and drawn by `src/AgentFace.jsx` as inline SVG cells.
Every number comes from `src/lib/agent-motion-tokens.json`, a byte-for-byte
copy of the brand's `tokens.json` that `../scripts/check_motion_tokens.py`
holds to the pinned commit; `tests/motion1.test.mjs` checks the port against
444 golden frames that `scripts/agent-face-golden.mjs` exports from the study
itself. A face runs a 12 fps clock only while it is on screen, the page is
visible, its surface and state call for motion and Reduce Motion is off, and
at most three move at once; otherwise it is its state's static pose, drawn
once. Odomo has no Low Power signal (browsers do not expose one).

## What it keeps

In IndexedDB, on this origin only: a device id, the credential the host
issued, and a pairing request in flight. In localStorage: pins, the language
choice, which notifications were seen or hidden. Nothing else, nowhere else.
