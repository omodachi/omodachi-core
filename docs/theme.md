# The host theme

Contract revision: `omodachi.v1`. Schema: [`contracts/theme.schema.json`](../contracts/theme.schema.json).
Example: [`contracts/fixtures/theme.json`](../contracts/fixtures/theme.json).

A client renders the host's current Omarchy theme. Not an approximation of it,
and not a palette derived from three of its colours: the same values Omarchy
computed for every other themed application on that machine. **No colour,
alpha, size or font step is ever hardcoded in a client.**

## How the values get there

Omarchy renders every theme file from a template when the theme changes.
`~/.config/omarchy/themed/*.tpl` is the documented place for "theming apps
Omarchy doesn't cover", and user templates are rendered before the packaged
ones. The installer puts one file there:

```
~/.config/omarchy/themed/omodachi-theme.json.tpl
```

It is a JSON document whose values are `{{ key }}` placeholders for the roles in
`colors.toml`: `mode`, `accent`, `selection`, `muted`, the four backgrounds, the
four foregrounds, the eight base colours and the six bright ones. After a theme
switch Omarchy has written the rendered result to

```
~/.local/state/omarchy/current/theme/omodachi-theme.json
```

`omarchy-theme-set-templates` renders out of the `next-theme` staging directory
a switch builds, so it cannot be run on its own to catch up an already-current
theme. The installer therefore re-applies the theme that is already set, with
`OMARCHY_THEME_HEADLESS=1 OMARCHY_THEME_SKIP_BACKGROUND=1`: that skips the
wallpaper, the shell IPC, the sixteen application restarts and the hooks, and
leaves only the regeneration of `current/theme`. The theme itself does not
change. `OMARCHY_PATH` comes from Omarchy's own
`/usr/share/omarchy/default/bash/env-bootstrap`, because an ssh command shell
has not necessarily been through an rc file.

If that render has not happened yet, the daemon falls back to parsing
`current/theme/colors.toml`, whose keys are the same. A palette that is missing
a role is refused rather than half-served.

## The design tokens

`current/theme/shell.toml` is Omarchy's design system: bar sizes, the control
state alphas, the spacing scale, the type scale, and every surface's
background/text/border/scrim. The generated file leaves most of `[spacing]` and
all of `[font]`'s steps **commented out**, and a theme may replace the whole
file or override one section with `shell.<section>.toml`. So the daemon
backfills every missing key from Omarchy's own
`/usr/share/omarchy/default/themed/shell.toml.tpl`, reading its commented
`# key = value` lines as the defaults they are. Lines still holding a `{{ }}`
placeholder are colours the live file always carries and are never defaults.

Sections that reference another, `border = "hyprland.active-border"`, are
resolved before publication, the way the shell resolves them in `Style.qml`, so
every surface answers with a colour. The seven sections `bar`, `controls`,
`spacing`, `font`, `menu`, `popups` and `hyprland` are always present; every
other section the host wrote is published too.

## Staying current

Both hooks are installed with the official installer, `omarchy hook install`:

| Hook | File | What it does |
| --- | --- | --- |
| `theme-set` | `~/.config/omarchy/hooks/theme-set.d/omodachi` | `omodachi-host theme-changed` |
| `font-set` | `~/.config/omarchy/hooks/font-set.d/omodachi` | `omodachi-host font-changed` (see [fonts.md](fonts.md)) |

The script says only that something changed; the daemon re-reads the host and
decides whether that is news. Authority is the Unix peer UID on
the daemon's local socket (see [hub.md](hub.md)), the same as
pairing — no device credential exists at hook time. The daemon also reads once at startup.

`install_host.py --remove` takes back exactly the template, the two hook
scripts and the rendered JSON, and nothing else: a hook of the same name that
is not ours is left alone.

## API

| Method and path | Behavior |
| --- | --- |
| `GET /v1/theme` | the whole theme |
| `GET /v1/theme/background` | the wallpaper bytes; `ETag` is its sha256 |
| WSS event `theme.changed` | `{revision, name}` |

```json
{
  "contract_revision": "omodachi.v1",
  "name": "nord",
  "mode": "dark",
  "colors": {"accent": "#81a1c1", "background": "#2e3440", "…": "…"},
  "shell": {
    "bar": {"size-horizontal": 26, "size-vertical": 28, "background": "#2e3440", "…": "…"},
    "controls": {"selected-fill-alpha": 0.18, "…": "…"},
    "spacing": {"control-height": 28, "row-padding-x": 12, "…": "…"},
    "font": {"base-size": 12, "heading": 16, "…": "…"},
    "menu": {"scrim-alpha": 0.5, "…": "…"},
    "popups": {"…": "…"}, "hyprland": {"active-border": "#81a1c1", "…": "…"}
  },
  "background": {"sha256": "6873fbf7…", "bytes": 1477329, "content_type": "image/jpeg"},
  "revision": 1
}
```

`revision` is a monotonic integer that only moves when the published payload
moves, so a client can cache on it. The wallpaper is fetched once: the digest
in `background.sha256` is the `ETag` of `GET /v1/theme/background`, and a
client that already has it sends `If-None-Match` and gets `304`. A theme with
no wallpaper has `background: null`, which is a theme, not a broken host.

A host with no readable theme answers `503` with `theme_unavailable`,
`theme_colors_unavailable` or `theme_shell_unavailable`. It never answers with
a guess. `--demo` has no host theme and reports `theme_unavailable`.
