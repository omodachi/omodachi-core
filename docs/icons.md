# The host's icon theme

Contract revision: `omodachi.v1`.
Schema: [`contracts/catalog.schema.json`](../contracts/catalog.schema.json) (`icon_kind`),
[`contracts/state.schema.json`](../contracts/state.schema.json) (`focus.icon`),
[`contracts/http-error.schema.json`](../contracts/http-error.schema.json) (the miss).
Example of a miss: [`contracts/fixtures/http-error-icon-not-found.json`](../contracts/fixtures/http-error-icon-not-found.json).

A catalog row's `icon` is not always a glyph. Four things arrive on a real
Omarchy install, and `icon_kind` now says which, so a client stops inferring it
from the string's shape:

| `icon_kind` | `icon` holds | what a client does |
| --- | --- | --- |
| `glyph` | one code point — a Nerd Font one (`U+F003B`), one of `omarchy.ttf`'s private-use agent marks, an emoji, a literal `✓` | draws it in the face that carries it (see [fonts.md](fonts.md)) |
| `xdg` | an XDG icon **name**: `org.gnome.Nautilus`, `google-chrome`, `docker`, `x` | `GET /v1/icons/{name}` |
| `path` | an absolute path, which `Icon=` in a `.desktop` file is also allowed to be | `GET /v1/icons/{path}`, percent-encoded |
| `none` | nothing | draws its own fallback |

`icon` itself is untouched — `icon_kind` is a second field, never a rewrite.

## Which theme

Omarchy sets the icon theme itself and ships no GTK `settings.ini`:

```
/usr/share/omarchy/install/user/first-run/gnome-theme.sh:3
  gsettings set org.gnome.desktop.interface icon-theme "Yaru-blue"
```

So `gsettings get org.gnome.desktop.interface icon-theme` is asked first,
`~/.config/gtk-4.0/settings.ini` then `gtk-3.0/settings.ini`'s
`gtk-icon-theme-name` second, and `hicolor` — which every theme inherits — when
a machine says neither. A name that is not an icon-theme name is refused rather
than walked.

## How a name becomes a file

Not by the XDG specification, but by whatever Omarchy's own menu draws. The
menu is `AppLibrary.iconSource()`
(`/usr/share/omarchy/shell/services/AppLibrary.qml:58-70`), and a row on the
phone that is not the same picture as the same row on the laptop is the bug
UX-2 §5 reported. Its order, and ours:

1. an empty value — the host draws `application-x-executable`; core leaves it
   to `icon_kind: "none"` and the client's own fallback;
2. a `file://` value — the host passes the URL to QML, core sends the file it
   names, percent-decoded and admitted under the same roots as an absolute
   `Icon=`. An `image://` value is a Quickshell image provider inside the shell
   process, so it is a miss;
3. a leading `/` — that file, under the restrictions below;
4. **the host's flat index**, which is what actually answers;
5. the themed XDG lookup, for the names the index misses;
6. `/usr/share/pixmaps/<name>.xpm`, and then a miss.

### The index (step 4)

`AppLibrary.iconIndexScanCommand()` (`:139-152`) shells out to:

```bash
for ext in svg png; do
  for base in $HOME/.icons $HOME/.local/share/icons $XDG_DATA_DIRS.../icons; do
    find "$base" \( -path "*/apps/*" -o -path "*/devices/*" \) -name "*.$ext"
  done
  find /usr/share/pixmaps -maxdepth 1 -name "*.$ext"
done
```

and `indexIconLine()` (`:154-162`) keeps the **first** line per basename. So
the index is flat, unthemed and size-blind; every SVG anywhere beats every PNG
anywhere, including across themes; `/usr/share/pixmaps` is part of it rather
than a separate last resort; and which of an icon's several files wins depends
on the order `readdir` hands each directory over.

Core reproduces that scan rather than approximating it: the same base
directories in the same order (`~/.icons` first, which is *not* the XDG order,
and `$XDG_DATA_HOME` deliberately not consulted), the same two passes, the same
`fnmatch` predicates, and a depth-first pre-order walk that descends into a
subdirectory the moment it reaches it — `find`'s order, which `os.walk` does
not have. The result is a dependence on readdir order, on purpose: being the
same file as the menu is the requirement, and a tidier rule would be a
different picture. On the host, core's index and the shell's agree on all 1355
names, and all 47 applications the menu shows resolve here.

The index is cached and rebuilt when a base directory's mtime moves or after
60 seconds, which stands in for the host's own rescan on every desktop-entry
change.

### The themed lookup (step 5)

The XDG icon theme specification's lookup, with the theme's `Inherits` chain
and `hicolor` last, `DirectoryMatchesSize` / `DirectorySizeDistance` for the
size, and every base directory (`$XDG_DATA_HOME/icons`, `~/.icons`, each
`$XDG_DATA_DIRS/icons`) searched for the **same** theme. Among the directories
that match, a vector always wins and otherwise the smallest bitmap at least as
large as the request does, so a 36pt row does not pull a 512×512 PNG over the
LAN.

It keeps the `Applications` and `Devices` contexts that Omarchy's shell warns
about in its own source (`AppLibrary.qml:62-63`: *"An unconstrained themed
lookup can resolve an app name such as `zoom` to an action icon instead"*) —
the index already enforces that guard for every name it carries, and the filter
is what is left for the names it does not. `application-x-executable` is the
one exception: the host's own blank-`Icon=` fallback is a `MimeTypes` icon and
Qt does not context-filter when it draws it, so neither does core, and the one
name the 404 below names is now a name core can serve.

In practice this branch is nearly unreachable for an application: any name with
a file under `*/apps/*` or `*/devices/*` was already answered at step 4. It
still matters for the generic icon, for a `$XDG_DATA_HOME` pointed somewhere
the host's scan does not look, and for a name whose file appeared inside the
index's cache window.

An **absolute** `Icon=` path is served only when the path, after symlinks are
resolved, is a regular file with a `.png` / `.svg` / `.xpm` suffix under one of
the icon base directories, `/usr/share/pixmaps`, a `$XDG_DATA_DIRS` entry,
`$HOME/.local/share` or `/opt`. This is not a file server.

## API

| Method and path | Behavior |
| --- | --- |
| `GET /v1/icons/{name}?size=64` | the bytes; `size` is 8–512 and defaults to 64 |

`Content-Type` is `image/png` or `image/svg+xml`. An SVG is rasterized with
`rsvg-convert` when the host has it and travels as an SVG when it does not — a
vector at the wrong nominal size is still the right icon.

`ETag` is the source file's sha256, with `-png<size>` appended when the bytes
are a PNG this host produced from an SVG: the same file at 72 and at 108 is two
representations and must not share a validator. `If-None-Match` is answered
`304`, exactly as for a font or the wallpaper. The daemon sends
`Cache-Control: no-store` on every response, including this one, so a client
revalidates with the digest it holds rather than with an HTTP cache.

A miss is:

```json
{
  "contract_revision": "omodachi.v1",
  "error": {
    "code": "icon_not_found",
    "message": "icon_not_found",
    "detail": {"fallback": "application"}
  }
}
```

`fallback` is what the client should draw instead, and `application` is the
same answer Omarchy gives itself (`AppLibrary.qml:57-69` ends at
`application-x-executable`). A host with no icon reader at all answers `503
icons_unavailable`; a size outside 8–512, or a query key other than `size`, is
`400 invalid_request`.

## The focused window

`state.focus` carries `icon` and `icon_kind` beside `app_id`. The compositor
only gives a Wayland `app_id`, so the desktop database already behind the Apps
submenu is what turns it into an `Icon=` value: a case-insensitive match on the
app rows' `appId`. It reads the catalog snapshot that already exists and never
builds one — focus changes every time a window is raised, and PERF-4's point
was that a window event must not re-run the menu's conditions. An `app_id` that
matches no installed entry keeps `icon_kind: "none"`.
