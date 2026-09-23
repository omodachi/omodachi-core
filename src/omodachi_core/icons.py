"""The host's icon theme: the one lookup a phone cannot do for itself.

A catalog row compiled from a `.desktop` file carries what `Icon=` said — an
**XDG icon name** (`org.gnome.Nautilus`, `google-chrome`, `x`) or, rarely, an
absolute path. A name is not a picture: resolving it needs the machine's icon
theme, its inheritance chain and the file tree under every icon base directory.
That is all on the host, so the host answers with the bytes.

The rule obeyed here is not "the XDG specification" but **whatever
`AppLibrary.iconSource()` draws in Omarchy's own menu**, because a row on the
phone and the same row on the laptop have to be the same picture. UX-2 §5 is
that complaint and nothing else: "Apps 的 icon 和系统图标不一样."

`/usr/share/omarchy/shell/services/AppLibrary.qml:58-70` consults, in order:

1. an empty value → `application-x-executable`;
2. a `file://` or `image://` value → as it stands;
3. a leading `/` → that file;
4. **`root.iconIndex[value]`** — a flat, unthemed index built by
   `iconIndexScanCommand()` (`:139-152`): every `*/apps/*` and `*/devices/*`
   file under `$HOME/.icons`, `$HOME/.local/share/icons` and each
   `$XDG_DATA_DIRS/icons`, plus `/usr/share/pixmaps`, **every SVG before every
   PNG**, the first hit for a name winning (`indexIconLine()`, `:154-162`);
5. `Quickshell.iconPath(value, true)` — Qt's own themed XDG lookup;
6. `application-x-executable` again.

Step 4 is the one that decides, and it runs **before** the theme. On this host
all 47 applications the menu shows resolve at step 4 and not one reaches step
5, so a core that ran the themed chain first disagreed with the menu on 13 of
them: `org.gnome.Nautilus` was `hicolor/scalable/apps/*.svg` — a grey GNOME
folder — in the menu and `Yaru-blue/256x256/apps/*.png` — a blue Yaru folder —
on the phone. So the index is primary here too, and the themed chain is what
answers the names it misses.

The index is size-blind, and which of an icon's several files it keeps depends
on the order `readdir` hands each directory over. So does ours, by
construction and on purpose: being the same picture as the menu is the
requirement, and any tidier rule would be a different picture.

Two host behaviours shape the themed fallback, both read off the running
Omarchy:

* the theme is `org.gnome.desktop.interface icon-theme` — Omarchy sets it
  itself (`/usr/share/omarchy/install/user/first-run/gnome-theme.sh:3`:
  `gsettings set org.gnome.desktop.interface icon-theme "Yaru-blue"`), and
  there is no `~/.config/gtk-[34].0/settings.ini` on the machine;
* the fallback keeps the `Applications` and `Devices` contexts that
  `AppLibrary.qml:62-63` warns about — "an unconstrained themed lookup can
  resolve an app name such as `zoom` to an action icon instead". The index
  already enforces that guard for every name it carries; the context filter is
  what is left for the names it does not. `application-x-executable` is the
  one exception, because the host's own blank-`Icon=` fallback is a `MimeTypes`
  icon and Qt does not context-filter when it draws it.

Nothing here executes a row's command, reads an arbitrary path a client names,
or takes a size a client did not bound.
"""
from __future__ import annotations

import configparser
import fnmatch
import hashlib
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

#: `Icon=` keys are names or absolute paths; this is the name form. `@` and `+`
#: appear in real themed names, `/` and `..` never do.
ICON_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+@-]{0,127}")
#: The contexts an application row may legitimately resolve into.
CONTEXTS = ("applications", "devices")
#: Extension preference inside one directory, the XDG order.
EXTENSIONS = (".png", ".svg", ".xpm")
CONTENT_TYPES = {".png": "image/png", ".svg": "image/svg+xml", ".xpm": "image/x-xpixmap"}
DEFAULT_THEME = "hicolor"
#: What the host's own menu falls back to (`AppLibrary.qml:57-69`). It is a
#: `MimeTypes` icon, so the themed lookup drops its context filter for it.
GENERIC_ICON = "application-x-executable"
PIXMAPS = Path("/usr/share/pixmaps")
GSETTINGS = "/usr/bin/gsettings"
RSVG_CONVERT = "/usr/bin/rsvg-convert"
MAX_ICON_BYTES = 4_194_304
MIN_SIZE, MAX_SIZE = 8, 512
#: Depth guard for a theme that inherits in a cycle.
MAX_THEME_DEPTH = 8
#: `iconIndexScanCommand()`'s two passes, in its order: every SVG, then every PNG.
INDEX_EXTENSIONS = (".svg", ".png")
#: `iconIndexScanCommand()`'s `-path` predicate, verbatim.
INDEX_PATHS = ("*/apps/*", "*/devices/*")
#: How long an index may be reused before the tree is walked again. The host
#: rescans on every desktop-entry change, debounced to 750ms; core has no such
#: signal, so it re-stats the base directories on every lookup and re-walks on
#: this clock. A package that lands a new icon shows up within a minute.
INDEX_TTL = 60.0


class IconsUnavailable(ValueError):
    """No icon of that name on this host; the boundary answers 404 or 503."""


def classify_icon(value: Any, icon_font: Any = "") -> str:
    """What a catalog row's `icon` field actually holds.

    The four answers are the four things the host publishes there, and the
    order is `HostGlyph.resolve`'s on the client, so the two agree row for row:

    * `"none"` — empty (`omodachi`, and every row Omarchy left blank);
    * `"path"` — an absolute path, which `Icon=` is also allowed to be;
    * `"glyph"` — one scalar the client can draw: a Nerd Font code point, a
      private-use code point from `omarchy.ttf`, an emoji, or a literal `✓`;
    * `"xdg"` — an icon *name*, which is everything else, including the
      one-letter names (`apps.X` publishes `"x"`) that are a name precisely
      because a bare ASCII letter is never a code point.
    """
    if not isinstance(value, str) or not value:
        return "none"
    if value.startswith("/"):
        return "path"
    if isinstance(icon_font, str) and icon_font:
        return "glyph"
    scalars = list(value)
    if len(scalars) != 1:
        return "xdg"
    single = scalars[0]
    if single.isascii() and single.isalnum():
        return "xdg"
    return "glyph"


def _run(argv: tuple[str, ...], *, timeout: float = 5.0) -> str:
    from .agent import ReadOnlyAgentProbe
    value = ReadOnlyAgentProbe._run_process(argv, timeout_seconds=timeout, max_bytes=65_536)
    if value.returncode or value.error:
        raise IconsUnavailable("icon_theme_unavailable")
    return value.stdout


class _Theme:
    """One parsed `index.theme`, for one theme name, under one base directory."""

    __slots__ = ("name", "root", "inherits", "directories")

    def __init__(self, name: str, root: Path, inherits: tuple[str, ...],
                 directories: tuple[dict[str, Any], ...]) -> None:
        self.name, self.root = name, root
        self.inherits, self.directories = inherits, directories


def _find(root: str) -> Iterable[str]:
    """`find <root>`, in GNU find's own order: readdir, depth first, pre-order.

    `iconIndexScanCommand()` keeps the *first* line `find` prints for a name,
    so the order is the answer and not an implementation detail. `find` hands
    back the starting point, then each entry in the order `readdir` gives it,
    descending into a directory the moment it reaches it rather than after its
    siblings — which is why `os.walk` is the wrong tool here: it drains a
    directory before descending and would pick a different file. `os.scandir`
    is the same `readdir`, unsorted, and `-P` (the default) does not follow a
    symlinked directory, so neither do we.
    """
    yield root
    yield from _descend(root)


def _descend(directory: str) -> Iterable[str]:
    try:
        with os.scandir(directory) as reader:
            entries = list(reader)
    except OSError:
        return
    for entry in entries:
        path = directory + "/" + entry.name
        yield path
        try:
            descend = entry.is_dir(follow_symlinks=False)
        except OSError:
            descend = False
        if descend:
            yield from _descend(path)


def _parse_index(root: Path, contexts: tuple[str, ...] | None = CONTEXTS) -> _Theme | None:
    index = root / "index.theme"
    try:
        if index.stat().st_size > 1_048_576:
            return None
        text = index.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    parser = configparser.RawConfigParser(strict=False, delimiters=("=",), interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    if not parser.has_section("Icon Theme"):
        return None
    header = parser["Icon Theme"]
    inherits = tuple(value.strip() for value in header.get("Inherits", "").split(",") if value.strip())
    names = [value.strip() for value in header.get("Directories", "").split(",") if value.strip()]
    names += [value.strip() for value in header.get("ScaledDirectories", "").split(",") if value.strip()]
    directories: list[dict[str, Any]] = []
    for name in names:
        if not parser.has_section(name):
            continue
        section = parser[name]
        context = section.get("Context", "").strip().casefold()
        if contexts is not None and context not in contexts:
            continue

        def number(key: str, default: int) -> int:
            try:
                return int(section.get(key, "").strip() or default)
            except ValueError:
                return default

        size = number("Size", 0)
        if size <= 0:
            continue
        kind = (section.get("Type", "Threshold").strip() or "Threshold").casefold()
        directories.append({"name": name, "size": size, "scale": max(1, number("Scale", 1)),
                            "type": kind if kind in {"fixed", "scalable", "threshold"} else "threshold",
                            "min": number("MinSize", size), "max": number("MaxSize", size),
                            "threshold": number("Threshold", 2), "context": context})
    if not directories:
        return None
    return _Theme(root.name, root, inherits, tuple(directories))


def _matches(directory: dict[str, Any], size: int, scale: int) -> bool:
    """`DirectoryMatchesSize`, XDG icon theme specification."""
    if directory["scale"] != scale:
        return False
    if directory["type"] == "fixed":
        return directory["size"] == size
    if directory["type"] == "scalable":
        return directory["min"] <= size <= directory["max"]
    return directory["size"] - directory["threshold"] <= size <= directory["size"] + directory["threshold"]


def _distance(directory: dict[str, Any], size: int, scale: int) -> int:
    """`DirectorySizeDistance`, XDG icon theme specification."""
    nominal, directory_scale = directory["size"], directory["scale"]
    if directory["type"] == "fixed":
        return abs(nominal * directory_scale - size * scale)
    if directory["type"] == "scalable":
        if size * scale < directory["min"] * directory_scale:
            return directory["min"] * directory_scale - size * scale
        if size * scale > directory["max"] * directory_scale:
            return size * scale - directory["max"] * directory_scale
        return 0
    low, high = nominal * directory_scale - directory["threshold"], nominal * directory_scale + directory["threshold"]
    if size * scale < low:
        return low - size * scale
    if size * scale > high:
        return size * scale - high
    return 0


def _rank(directory: dict[str, Any], extension: str, size: int, scale: int) -> tuple[int, int]:
    """Among the directories that *match* the size, the cheapest good one.

    The XDG rule is "the first matching directory in `Directories` order", and
    on a real Yaru that is `256x256/apps` — declared `Type=Scalable MinSize=64`
    — for anything from 64 up. A desktop can afford that; sending a 512×512 PNG
    over the LAN for a 36pt row cannot. So a match is ranked instead: a vector
    is always perfect, and among bitmaps the smallest one that is still at
    least as large as what was asked for wins, falling back to the closest.
    Nothing here can pick a *different picture* — only a different
    representation of the same icon.

    A vector scores below every bitmap rather than level with the exact-size
    one. It used to score `(0, 0)`, which an exactly-sized PNG also scores, and
    `min()` broke that tie by `Directories=` position with `EXTENSIONS` putting
    `.png` first — so "a vector is always perfect" was true of the docstring
    and not of the code whenever both files existed at the requested size.
    """
    if extension == ".svg":
        return (-1, 0)
    nominal = directory["size"] * directory["scale"]
    wanted = size * scale
    return (0 if nominal >= wanted else 1, abs(nominal - wanted))


class HostIcons:
    """`GET /v1/icons/{name}` — one icon, resolved the way the host resolves it."""

    #: What a client should draw when this host has no picture for the name.
    FALLBACK = "application"

    def __init__(self, *, runner=None, environ: dict[str, str] | None = None,
                 home: Path | None = None, pixmaps: Path | None = None,
                 rsvg_convert: str | None = RSVG_CONVERT, theme: str | None = None) -> None:
        self.runner = runner or _run
        self.environ = dict(os.environ if environ is None else environ)
        self.home = Path(home) if home is not None else Path(self.environ.get("HOME", "~")).expanduser()
        self.pixmaps = Path(pixmaps) if pixmaps is not None else PIXMAPS
        self.rsvg_convert = rsvg_convert
        self._forced_theme = theme
        self._themes: dict[tuple[Path, tuple[str, ...] | None], _Theme | None] = {}
        self._rendered: dict[tuple[str, int, int], dict[str, Any]] = {}
        self._index: dict[str, str] | None = None
        self._index_stamp: tuple[int | None, ...] = ()
        self._index_at = 0.0

    # --- where the host keeps icons -------------------------------------

    def base_dirs(self) -> list[Path]:
        """`$XDG_DATA_HOME/icons`, `~/.icons`, then every `$XDG_DATA_DIRS/icons`."""
        roots: list[Path] = []
        data_home = self.environ.get("XDG_DATA_HOME", "").strip()
        roots.append(Path(data_home) / "icons" if data_home.startswith("/") else self.home / ".local/share/icons")
        roots.append(self.home / ".icons")
        dirs = self.environ.get("XDG_DATA_DIRS", "").strip() or "/usr/local/share:/usr/share"
        for entry in dirs.split(":"):
            entry = entry.strip()
            if entry.startswith("/"):
                roots.append(Path(entry) / "icons")
        seen, ordered = set(), []
        for root in roots:
            if root not in seen:
                seen.add(root)
                ordered.append(root)
        return ordered

    def index_base_dirs(self) -> list[Path]:
        """Where the *host's own scan* looks, in the order it looks — not XDG's.

        `iconIndexScanCommand()` opens with the literal
        `dirs="$HOME/.icons $HOME/.local/share/icons"` and only then appends
        `$d/icons` for each `$XDG_DATA_DIRS` entry. That is `~/.icons` first,
        which is the reverse of `base_dirs()`, and `$XDG_DATA_HOME` is never
        consulted even when it is set elsewhere. Both are differences that
        decide which file a duplicated name resolves to, so they are copied
        rather than corrected. Nothing is de-duplicated either: `bash` does not,
        and a second walk of the same tree cannot change a first-wins index.
        """
        roots = [self.home / ".icons", self.home / ".local/share/icons"]
        dirs = self.environ.get("XDG_DATA_DIRS", "").strip() or "/usr/local/share:/usr/share"
        for entry in dirs.split(":"):
            entry = entry.strip()
            if entry.startswith("/"):
                roots.append(Path(entry) / "icons")
        return roots

    def theme_name(self) -> str:
        """The theme the desktop is actually set to, and where that was read.

        Omarchy sets it through gsettings and ships no GTK `settings.ini`, so
        gsettings is asked first; a machine that does keep a `settings.ini`
        still answers, and a machine with neither falls to `hicolor`, which
        every theme inherits anyway.
        """
        return self.theme_source()["theme"]

    def theme_source(self) -> dict[str, str]:
        if self._forced_theme:
            return {"theme": self._forced_theme, "source": "configured"}
        try:
            value = self.runner((GSETTINGS, "get", "org.gnome.desktop.interface", "icon-theme")).strip()
        except IconsUnavailable:
            value = ""
        value = value.strip().strip("'\"")
        if ICON_NAME.fullmatch(value):
            return {"theme": value, "source": "gsettings"}
        for version in ("gtk-4.0", "gtk-3.0"):
            settings = self.home / ".config" / version / "settings.ini"
            try:
                text = settings.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            found = re.search(r"^\s*gtk-icon-theme-name\s*=\s*(\S[^\r\n]*)$", text, re.MULTILINE)
            name = found.group(1).strip().strip("'\"") if found else ""
            if ICON_NAME.fullmatch(name):
                return {"theme": name, "source": version + "/settings.ini"}
        return {"theme": DEFAULT_THEME, "source": "default"}

    def _theme(self, root: Path, contexts: tuple[str, ...] | None) -> _Theme | None:
        key = (root, contexts)
        if key not in self._themes:
            self._themes[key] = _parse_index(root, contexts)
        return self._themes[key]

    def theme_chain(self) -> list[str]:
        """The theme, everything it inherits, and `hicolor` last — never a cycle."""
        start = self.theme_name()
        chain: list[str] = []
        pending = [(start, 0)]
        while pending:
            name, depth = pending.pop(0)
            if name in chain or depth > MAX_THEME_DEPTH or not ICON_NAME.fullmatch(name):
                continue
            chain.append(name)
            # Walked without the context filter: a theme whose `Directories=`
            # holds no `Applications` or `Devices` entry still has an
            # `Inherits=`, and dropping it would cut the chain short.
            theme = self._index_for(name, None)
            inherits = list(theme.inherits) if theme is not None else []
            pending.extend((value, depth + 1) for value in inherits)
        if DEFAULT_THEME not in chain:
            chain.append(DEFAULT_THEME)
        return chain

    # --- the lookup -----------------------------------------------------

    def _index_for(self, theme_name: str, contexts: tuple[str, ...] | None = CONTEXTS) -> _Theme | None:
        """One theme's `index.theme`, from whichever base directory carries it.

        A theme is one theme even when its files are spread over several base
        directories, and only one of them needs the index: a user's
        `~/.local/share/icons/hicolor` never has one, and dropping it would
        hide every icon an app installed for this user alone — which is exactly
        where Omodachi's own `com.omodachi.host.svg` lives on this host.
        """
        for base in self.base_dirs():
            theme = self._theme(base / theme_name, contexts)
            if theme is not None:
                return theme
        return None

    def _in_theme(self, theme_name: str, name: str, size: int, scale: int,
                  contexts: tuple[str, ...] | None = CONTEXTS) -> tuple[Path, int] | None:
        """`FindIconHelper` for one theme: an exact size match, else the closest."""
        index = self._index_for(theme_name, contexts)
        if index is None:
            return None
        best: tuple[int, Path, int] | None = None
        matched: list[tuple[tuple[int, int], Path, int]] = []
        for base in self.base_dirs():
            root = base / theme_name
            if not root.is_dir():
                continue
            for directory in index.directories:
                for extension in EXTENSIONS:
                    candidate = root / directory["name"] / (name + extension)
                    if not candidate.is_file():
                        continue
                    if _matches(directory, size, scale):
                        matched.append((_rank(directory, extension, size, scale), candidate, directory["size"]))
                        continue
                    distance = _distance(directory, size, scale)
                    if best is None or distance < best[0]:
                        best = (distance, candidate, directory["size"])
        if matched:
            chosen = min(matched, key=lambda row: row[0])
            return chosen[1], chosen[2]
        return (best[1], best[2]) if best is not None else None

    def _in_pixmaps(self, name: str) -> Path | None:
        for extension in EXTENSIONS:
            candidate = self.pixmaps / (name + extension)
            if candidate.is_file():
                return candidate
        return None

    def icon_index(self) -> dict[str, str]:
        """`AppLibrary.iconIndex` — the host's flat, unthemed, size-blind index.

        Cached, because building it costs a full walk of every icon tree on the
        machine (1355 names and some tens of thousands of `readdir` entries on
        this host). It is rebuilt when a base directory's own mtime moves — a
        theme appearing or vanishing — and otherwise once `INDEX_TTL` has
        passed, which is what catches a package dropping a file deep inside a
        tree the way the host's 750ms rescan does.
        """
        stamp: list[int | None] = []
        for base in self.index_base_dirs() + [self.pixmaps]:
            try:
                stamp.append(base.stat().st_mtime_ns)
            except OSError:
                stamp.append(None)
        signature = tuple(stamp)
        now = time.monotonic()
        if (self._index is not None and signature == self._index_stamp
                and now - self._index_at < INDEX_TTL):
            return self._index
        self._index = self._scan_index()
        self._index_stamp, self._index_at = signature, now
        return self._index

    def _scan_index(self) -> dict[str, str]:
        """`iconIndexScanCommand()` and `indexIconLine()`, run here instead of bash.

        The shell command is::

            for ext in svg png; do
              for base in $dirs; do
                [[ -d $base ]] && find "$base" \\( -path "*/apps/*" -o -path
                  "*/devices/*" \\) -name "*.$ext" 2>/dev/null;
              done;
              find /usr/share/pixmaps -maxdepth 1 -name "*.$ext" 2>/dev/null;
            done

        so every SVG anywhere is emitted before any PNG anywhere, `pixmaps`
        comes after the base directories **within** each extension pass, and
        `indexIconLine()` keeps the first line per name. `-path` and `-name`
        are `fnmatch` without `FNM_PATHNAME`, which is `fnmatch.fnmatchcase`
        exactly — `*` crosses `/` in both. `find` tests directories as well as
        files, so a directory called `foo.svg` would enter the index on the
        host and enters it here too.

        One walk serves both passes: the traversal is the same and only the
        extension differs, so each entry is filed into its extension's bucket
        and the buckets are drained in the host's order afterwards. The host
        reads `/usr/share/pixmaps` by that literal path; `self.pixmaps` is the
        same directory, redirectable only so the tests can build a tree.
        """
        buckets: dict[str, list[str]] = {extension: [] for extension in INDEX_EXTENSIONS}
        for base in self.index_base_dirs():
            if not base.is_dir():
                continue
            for path in _find(str(base)):
                if not any(fnmatch.fnmatchcase(path, rule) for rule in INDEX_PATHS):
                    continue
                stem = path[path.rfind("/") + 1:]
                for extension in INDEX_EXTENSIONS:
                    if fnmatch.fnmatchcase(stem, "*" + extension):
                        buckets[extension].append(path)
        for extension in INDEX_EXTENSIONS:
            try:
                with os.scandir(self.pixmaps) as reader:
                    entries = list(reader)
            except OSError:
                entries = []
            for entry in entries:
                if fnmatch.fnmatchcase(entry.name, "*" + extension):
                    buckets[extension].append(str(self.pixmaps) + "/" + entry.name)
        index: dict[str, str] = {}
        for extension in INDEX_EXTENSIONS:
            for path in buckets[extension]:
                stem = path[path.rfind("/") + 1:]
                dot = stem.rfind(".")
                name = stem[:dot] if dot > 0 else stem
                if name and name not in index:
                    index[name] = path
        return index

    def _allowed_roots(self) -> list[Path]:
        """Where an absolute `Icon=` may point. Everything else is a 404."""
        roots = list(self.base_dirs()) + [self.pixmaps]
        dirs = self.environ.get("XDG_DATA_DIRS", "").strip() or "/usr/local/share:/usr/share"
        for entry in dirs.split(":"):
            entry = entry.strip()
            if entry.startswith("/"):
                roots.append(Path(entry))
        roots.append(self.home / ".local/share")
        roots.append(Path("/opt"))
        return roots

    def _resolve_path(self, value: str) -> Path:
        """An absolute `Icon=` value, admitted only under a known data root.

        A client names this path, so the file must really live under one of the
        directories a `.desktop` file is allowed to point at — after symlinks,
        not before — and must be an image by extension. This is not a file
        server.
        """
        candidate = Path(value)
        if not candidate.is_absolute() or candidate.suffix.lower() not in CONTENT_TYPES:
            raise IconsUnavailable("icon_not_found")
        try:
            real = candidate.resolve(strict=True)
            if not real.is_file():
                raise IconsUnavailable("icon_not_found")
        except (OSError, RuntimeError):
            raise IconsUnavailable("icon_not_found") from None
        for root in self._allowed_roots():
            try:
                real.relative_to(root.resolve(strict=False))
            except ValueError:
                continue
            return real
        raise IconsUnavailable("icon_not_found")

    def _resolve_url(self, value: str) -> Path:
        """`iconSource()`'s second branch, turned back into bytes.

        The host hands a `file://` or `image://` value straight to QML, which
        can load either; core has to send the picture down a wire. A `file://`
        URL is the path it names — percent-decoded, because `Util.fileUrl()`
        percent-*encodes* each segment on the way out — admitted under the same
        data roots as an absolute `Icon=`. An `image://` value names a
        Quickshell image provider living inside the shell process, which
        nothing outside it can read, so it is a miss rather than a guess.
        """
        if not value.startswith("file://"):
            raise IconsUnavailable("icon_not_found")
        split = urlsplit(value)
        if split.netloc not in ("", "localhost") or split.query or split.fragment:
            raise IconsUnavailable("icon_not_found")
        return self._resolve_path(unquote(split.path))

    def lookup(self, name: str, *, size: int = 64, scale: int = 1) -> dict[str, Any]:
        """The file this host would draw for `name`, in `iconSource()`'s order.

        Empty is the one branch that is not mirrored: the host draws
        `application-x-executable` itself, while `icon_kind` already told the
        client the row has no icon and `FALLBACK` already told it what to draw
        — so an empty name stays a miss here rather than becoming a picture
        nobody asked for. The name itself is still resolvable on request.
        """
        if not isinstance(name, str) or not name:
            raise IconsUnavailable("icon_not_found")
        if name.startswith("file://") or name.startswith("image://"):
            return {"path": self._resolve_url(name), "theme": None, "source": "path", "nominal": None}
        if name.startswith("/"):
            return {"path": self._resolve_path(name), "theme": None, "source": "path", "nominal": None}
        if not ICON_NAME.fullmatch(name):
            raise IconsUnavailable("icon_not_found")
        indexed = self.icon_index().get(name)
        # The index may be up to `INDEX_TTL` stale; a name whose file has since
        # been removed falls through to the theme rather than promising bytes
        # that are not there. While the file exists — the case parity is about
        # — this is the menu's own answer.
        if indexed is not None and Path(indexed).is_file():
            return {"path": Path(indexed), "theme": None, "source": "index", "nominal": None}
        contexts = None if name == GENERIC_ICON else CONTEXTS
        for theme_name in self.theme_chain():
            found = self._in_theme(theme_name, name, size, scale, contexts)
            if found is not None:
                return {"path": found[0], "theme": theme_name, "source": "theme", "nominal": found[1]}
        # `/usr/share/pixmaps` again, for the `.xpm` the host's own scan — two
        # passes over `svg` and `png` — never looks at.
        pixmap = self._in_pixmaps(name)
        if pixmap is not None:
            return {"path": pixmap, "theme": None, "source": "pixmaps", "nominal": None}
        raise IconsUnavailable("icon_not_found")

    # --- the bytes ------------------------------------------------------

    def _rasterize(self, path: Path, size: int) -> bytes | None:
        """An SVG at the size that was asked for, when `rsvg-convert` is here.

        Without it the SVG travels as an SVG and the client rasterizes: a
        vector at the wrong size is still the right icon, a missing icon is
        not.
        """
        if not self.rsvg_convert or not Path(self.rsvg_convert).is_file():
            return None
        with tempfile.TemporaryDirectory(prefix="omodachi-icon-") as directory:
            out = Path(directory) / "icon.png"
            argv = (self.rsvg_convert, "--width", str(size), "--height", str(size),
                    "--keep-aspect-ratio", "--format", "png", "--output", str(out), str(path))
            from .agent import ReadOnlyAgentProbe
            result = ReadOnlyAgentProbe._run_process(argv, timeout_seconds=5.0, max_bytes=65_536)
            if result.returncode or result.error:
                return None
            try:
                if out.stat().st_size > MAX_ICON_BYTES:
                    return None
                return out.read_bytes()
            except OSError:
                return None

    def render(self, name: str, *, size: int = 64) -> dict[str, Any]:
        """The response body for one icon: bytes, type, and the ETag for them.

        The ETag is the sha256 of the **file**, as the fonts and the wallpaper
        already do, with the rasterized size appended when the bytes are a PNG
        this host produced from an SVG — the same file at 72 and at 108 is two
        representations and must not share a validator.
        """
        if type(size) is not int or not MIN_SIZE <= size <= MAX_SIZE:
            raise IconsUnavailable("invalid_size")
        found = self.lookup(name, size=size)
        path: Path = found["path"]
        try:
            stat = path.stat()
            if stat.st_size > MAX_ICON_BYTES:
                raise IconsUnavailable("icon_too_large")
            key = (str(path), int(stat.st_mtime_ns), size)
            cached = self._rendered.get(key)
            if cached is not None:
                return cached
            data = path.read_bytes()
        except OSError:
            raise IconsUnavailable("icon_not_found") from None
        digest = hashlib.sha256(data).hexdigest()
        content_type = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        if path.suffix.lower() == ".svg":
            raster = self._rasterize(path, size)
            if raster is not None:
                data, content_type, digest = raster, "image/png", digest + f"-png{size}"
        row = {"name": name, "path": str(path), "theme": found["theme"], "source": found["source"],
               "size": size, "bytes": data, "content_type": content_type, "sha256": digest}
        if len(self._rendered) > 128:
            self._rendered.clear()
        self._rendered[key] = row
        return row

    def resolvable(self, names: Iterable[str], *, size: int = 64) -> dict[str, bool]:
        """Which of these names this host can draw. Used by tests and probes."""
        answer: dict[str, bool] = {}
        for name in names:
            try:
                self.lookup(name, size=size)
                answer[name] = True
            except IconsUnavailable:
                answer[name] = False
        return answer
