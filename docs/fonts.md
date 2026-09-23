# The host fonts

Contract revision: `omodachi.v1`. Schema: [`contracts/fonts.schema.json`](../contracts/fonts.schema.json).
Example: [`contracts/fixtures/fonts.json`](../contracts/fixtures/fonts.json).

Three roles, all taken from the host, none packaged into a client.

## `mono`

`omarchy-font-current` is `fc-match monospace` with the first family of the
alias list, so it answers with whatever the user chose with `omarchy font set`.
That family is then looked up with `fc-list`, and its `Regular` and `Bold` files
become `mono-regular` and `mono-bold`. A Nerd Font publishes two family names
(`JetBrainsMono Nerd Font` and `JetBrainsMono NF`) and several comma-separated
styles, so both lists are matched member-wise rather than compared as strings.

The family is whatever fontconfig resolves, which is **not necessarily a Nerd
Font** — on the maintainer's host `fc-match monospace` answers Nimbus Mono PS,
which carries no Nerd Font code point at all.

That family is only the first link. **The host itself does not draw tofu**, and
this document used to claim it did. Everything on an Omarchy desktop that draws
with the `monospace` alias — kitty, Qt, Quickshell's menu — asks fontconfig again
for each character the matched family cannot draw, and lands on whatever else is
installed. A client that registers only the matched family and stops there is the
one that shows tofu; the host is not misconfigured, the client was
under-informed. That second question is `fallback`, below.

## `fallback`

What fontconfig answers for the code points the matched family does not carry,
in the order a client should register them. It is asked exactly the way the
desktop asks it, one code point at a time:

```
$ fc-match "monospace:charset=f07b" -f "%{family[0]}\t%{file}\n"
JetBrainsMono Nerd Font   /usr/share/fonts/TTF/JetBrainsMonoNerdFont-Regular.ttf
```

Three groups of probes, chosen because they are what a terminal line meets:

| `coverage` | probes | why |
| --- | --- | --- |
| `symbols` | `U+E0B0` `U+E615` `U+F00C` `U+F07B` `U+F835` `U+F0249` | Powerline separators, the Seti block, Font Awesome, and the Material Design plane current Nerd Fonts moved to `U+F0000`. A prompt and an icon-ful `ls` draw from all of them |
| `cjk` | `U+4E2D` `U+3042` | a Han ideograph and a kana |
| `emoji` | `U+1F600` | one emoji-presentation point |

Two things the probing has to get right:

* **`:lang=zh` is not used.** fontconfig scores a language tag weakly enough that
  a monospace family with no CJK at all still wins it — on the maintainer's host
  `fc-match "monospace:lang=zh"` answers Nimbus Mono PS, which has no Han glyph.
  A charset probe cannot be fooled that way.
* **`fc-match` never fails.** It is a *match*, not a lookup: asked for a code
  point nothing on the machine carries it hands back the matched monospace
  family anyway. So every answer is confirmed with `fc-list ":charset=<hex>"`,
  which lists only the files that really carry the point, and an unconfirmed
  answer is dropped rather than published.

A group does not have to resolve to one family. `U+F835` is a pre-3.0 Material
Design point that current Nerd Fonts dropped, and a host with Font Awesome
installed answers it from `fa-brands-400.woff2` — so `symbols` can be two links,
and each one is published in probe order.

Each link names the row that carries its file. When the file is one the `mono`
role already published, `font` points at that row instead of a second copy of
the same bytes; when the host will not serve the file at all — unreadable, or
past 32 MiB — `font` is `null` and the `family` is still reported, because it is
still the truth about what draws that code point here.

The chain is remembered between requests: it costs two `fc-*` processes per
probe and `/v1/fonts` is read on every reconnect. The `font-set` hook is the one
event allowed to say it is stale.

## `icons`

`/usr/share/omarchy/default/fonts/omarchy/omarchy.ttf` is Omarchy's private icon
font. Its private-use code points are the AI agent marks the menu uses for rows
tagged `"iconFont": "omarchy"`, they are monochrome so the active theme's
foreground recolours them, and **the set grows with every Omarchy release**.
Embedding a copy in a client means missing glyphs after the next `omarchy
update`, so the file is fetched from the host and re-fetched when its digest
changes.

## API

| Method and path | Behavior |
| --- | --- |
| `GET /v1/fonts` | the rows |
| `GET /v1/fonts/{id}` | that file; `ETag` is its sha256 |
| WSS event `fonts.changed` | `{revision}` |

```json
{
  "contract_revision": "omodachi.v1",
  "revision": 1,
  "fonts": [
    {"id": "mono-regular", "role": "mono", "family": "Nimbus Mono PS",
     "path": "/usr/share/fonts/gsfonts/NimbusMonoPS-Regular.otf",
     "sha256": "4f225ca8…", "bytes": 77936, "content_type": "font/otf"},
    {"id": "mono-bold", "role": "mono", "family": "Nimbus Mono PS", "…": "…"},
    {"id": "icons", "role": "icons", "family": "omarchy",
     "path": "/usr/share/omarchy/default/fonts/omarchy/omarchy.ttf",
     "sha256": "de860dce…", "bytes": 5412, "content_type": "font/ttf"},
    {"id": "fallback-symbols", "role": "fallback", "family": "JetBrainsMono Nerd Font",
     "path": "/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Regular.ttf",
     "sha256": "1c680e8c…", "bytes": 2571596, "content_type": "font/ttf"},
    {"id": "fallback-symbols-2", "role": "fallback", "family": "Font Awesome 7 Brands", "…": "…"},
    {"id": "fallback-cjk", "role": "fallback", "family": "Noto Sans Mono CJK KR", "…": "…"},
    {"id": "fallback-emoji", "role": "fallback", "family": "Noto Color Emoji", "…": "…"}
  ],
  "fallback_chain": [
    {"coverage": "symbols", "family": "JetBrainsMono Nerd Font", "font": "fallback-symbols",
     "probes": ["U+E0B0", "U+E615", "U+F00C", "U+F07B", "U+F0249"]},
    {"coverage": "symbols", "family": "Font Awesome 7 Brands", "font": "fallback-symbols-2",
     "probes": ["U+F835"]},
    {"coverage": "cjk", "family": "Noto Sans Mono CJK KR", "font": "fallback-cjk",
     "probes": ["U+4E2D", "U+3042"]},
    {"coverage": "emoji", "family": "Noto Color Emoji", "font": "fallback-emoji",
     "probes": ["U+1F600"]}
  ]
}
```

`id` is `mono-regular`, `mono-bold`, `icons`, or `fallback-<coverage>` with a
`-2`, `-3`… when one coverage needs more than one family; a client never names a
path, and an unknown id is `404 font_not_found`. A fallback file can be large —
`NotoSansCJK-Regular.ttc` is 19 MB on the maintainer's host — so a client is
expected to take only the links it cannot answer itself, which for a platform
with its own CJK and emoji faces is `symbols` alone. `path` is published so a report
can say where the bytes came from, never so a client can ask for another file.
The digest in the listing is the `ETag` of the download, so a client that
already has a font sends `If-None-Match` and gets `304`.

The `font-set` hook (installed with `omarchy hook install`, see
[theme.md](theme.md)) calls `omodachi-host font-changed`; the daemon re-reads
and publishes `fonts.changed` only when a row actually moved. If fontconfig
cannot be read at all, the icon font is still published on its own — that is the
one a client cannot do without. A host with neither answers `503
fonts_unavailable`.
