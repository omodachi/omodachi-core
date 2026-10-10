# Links in what agents send (LINKS-1)

Leo, 2026-10-04: the phone never views the computer's files. A **web address**
opens on the phone; a **file** opens on the computer, and the App jumps to
Remote to look at it.

## `POST /v1/host/open`

```json
{"source": {"kind": "agent", "item": "<agent row id>"}, "path": "/home/u/l1test/report.md:12"}
{"source": {"kind": "herdr", "pane": "w1:p2", "session": "work"}, "path": "tui-out.csv", "check": true}
{"source": {"kind": "agent", "item": "<row>"}, "url": "http://localhost:8000/"}
```

Native and web credentials (`browser_gate.WEB_ALLOWED`). Reply:
`host-open.schema.json`; refusals and every rule are in that schema's
description and in `src/omodachi_core/host_open.py`. In short:

1. **Mentioned**: the text must appear in the source - an assistant message or
   a tool row's `paths` (Agent), or the pane's visible screen now, line breaks
   taken out (Herdr, `pane.read source=visible`). A command's output never
   counts.
2. **Roots**: real path inside `$HOME`, the thread's / pane's folder, or `/tmp`;
   outside `$HOME` only the user's own files.
3. **Private first**: `~/.ssh`, `~/.gnupg`, `~/.codex`, `~/.claude`, Omodachi's
   config and state (uploads and the agent workspace excepted), key and
   credential names, `.env*`.
4. Regular files and folders only. 5. **Never run**: exec bit, ELF, `.desktop`,
   AppImage and other launchable kinds are refused.

It opens with `xdg-open` in the graphical session's environment
(`catalog_providers.provider_environment`). With a Remote session running, the
session's own output is focused first (`hl.dsp.focus({ monitor = <output> })`,
read back from `hyprctl -j monitors`), so a new window lands on the screen the
phone shows: `where: "remote"`. Without one it is the focused screen
(`where: "screen"`). The App therefore starts Remote (extend) *before* it asks
for the open, after a `check: true` that opens nothing.

`:12`, `:12:4` and `#L12` open the editor at that line when the file's default
handler is Omarchy's terminal editor (`nvim.desktop`, the default for text):
`omarchy-launch-editor +12 <path>` (helix: `<path>:12`). Other handlers
(Omawrite for markdown, imv, Evince, Nautilus, the browser) open the file as
usual and `at_line` is false. An app that is already running may put its new
window or tab where it likes (a browser tab in its existing window).

One journal line per request: `{"omodachi":"host_open","event":"host.open",
device, role, source, check, kind, target, outcome, where}`.

## Rows

Tool rows may carry `paths` (fileChange: the files it touched; codex's
`imageView`, now a `Viewed image` row: the picture it looked at). It is an
optional field on an existing kind, so older clients ignore it.
Whitespace-only assistant messages never leave core (AGENT-NEW-1).

## Web addresses

Clients classify and rewrite them themselves with the shared vectors in
`contracts/fixtures/links-detect.json`: a `localhost` / `127.x` / `0.0.0.0` /
`::1` / host-own-name address is rewritten to the road the device uses; for
`http` an IP road is preferred (no name lookup and nothing for a browser to
upgrade to https; measured on iOS 27, `http://<name>.ts.net:<port>` loads in the
Safari view too, so this is a preference, not a workaround). A server
that listens only on loopback cannot be reached that way; the clients say so
and offer "Open on the computer" (`url`). No prompt or skill is injected into
agents (Leo): binding to `0.0.0.0` is the user's call.
