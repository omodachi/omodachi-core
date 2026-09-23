"""The host's fonts: the monospace family fontconfig actually resolves, the
families it falls back to for the code points that family does not carry, and
Omarchy's private icon font.

All of them are read from the host, never packaged. `omarchy-font-current` is
`fc-match monospace`, so it answers with whatever the user chose with
`omarchy font set` — the client renders the host's font, not a guess. The icon
font grows a private-use glyph every time Omarchy learns a new agent, which is
exactly why a client must fetch it instead of embedding a copy.

The matched family is only the *first* link. Everything on the host that draws
with the `monospace` alias — kitty, Quickshell's menu, Qt — asks fontconfig again
per character, so a prompt drawn in a family with no Nerd Font block still shows
its icons. `fallback_chain` is that second question, asked with the same tool
(`fc-match "monospace:charset=…"`) for the code points a terminal actually meets:
the Nerd Font private-use blocks, CJK, and emoji.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any

ICON_FONT = Path("/usr/share/omarchy/default/fonts/omarchy/omarchy.ttf")
FONT_CURRENT = "omarchy-font-current"
FC_LIST = "/usr/bin/fc-list"
FC_MATCH = "/usr/bin/fc-match"
MAX_FONT_BYTES = 33_554_432
_STYLES = {"mono-regular": "Regular", "mono-bold": "Bold"}
_FONT_TYPES = {".ttf": "font/ttf", ".otf": "font/otf", ".ttc": "font/collection",
               ".woff": "font/woff", ".woff2": "font/woff2"}
_FAMILY = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,127}")

#: What a terminal line actually asks for beyond the matched family, in the
#: order a client should register. Each probe is one code point handed to
#: `fc-match "monospace:charset=<hex>"`, which is how Qt and kitty end up on the
#: face they draw with.
#:
#: * ``symbols`` — the Nerd Font blocks. They do not resolve as one family:
#:   `U+F835` is a pre-3.0 Material Design point current Nerd Fonts dropped, and
#:   on a host with Font Awesome installed fontconfig answers it from there. So
#:   the group is probed point by point and every distinct answer becomes a link.
#: * ``cjk`` — a Han ideograph and a kana. `:lang=zh` is *not* used: fontconfig
#:   scores a language tag weakly enough that a monospace family with no CJK at
#:   all still wins it (on Leo's host `fc-match "monospace:lang=zh"` answers
#:   Nimbus Mono PS, which has no Han glyph). A charset probe cannot lie.
#: * ``emoji`` — one emoji-presentation point and one that is commonly drawn
#:   monochrome.
FALLBACK_PROBES: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("symbols", (0xE0B0, 0xE615, 0xF00C, 0xF07B, 0xF835, 0xF0249)),
    ("cjk", (0x4E2D, 0x3042)),
    ("emoji", (0x1F600,)),
)
COVERAGES = tuple(name for name, _ in FALLBACK_PROBES)


class FontsUnavailable(ValueError):
    """No readable host font of that role; the boundary answers 503 or 404."""


def _run(argv: tuple[str, ...]) -> str:
    from .agent import ReadOnlyAgentProbe
    value = ReadOnlyAgentProbe._run_process(argv, timeout_seconds=5.0, max_bytes=1_048_576)
    if value.returncode or value.error:
        raise FontsUnavailable("font_probe_unavailable")
    return value.stdout


def sha256_of(path: Path, limit: int = MAX_FONT_BYTES) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(262_144):
            size += len(chunk)
            if size > limit:
                raise FontsUnavailable("font_too_large")
            digest.update(chunk)
    return digest.hexdigest(), size


def parse_fc_list(text: str, family: str) -> dict[str, Path]:
    """`<path>: <family list>:style=<style list>` into {style: path}.

    A Nerd Font publishes two family names (`JetBrainsMono Nerd Font` and
    `JetBrainsMono NF`) and several comma-separated styles, so both lists are
    matched member-wise rather than compared as strings.
    """
    found: dict[str, Path] = {}
    wanted = family.strip().casefold()
    for line in text.splitlines():
        path_text, separator, rest = line.partition(": ")
        if not separator:
            continue
        families, _, styles = rest.partition(":style=")
        if wanted not in {item.strip().casefold() for item in families.split(",")}:
            continue
        for style in styles.split(","):
            style = style.strip()
            if style and style not in found:
                found[style] = Path(path_text)
    return found


def probe_label(code: int) -> str:
    """`U+F07B`, the way a report and a schema both spell a code point."""
    return f"U+{code:04X}"


class HostFonts:
    """Publishes the roles SPEC-F1 defines plus TERM-1's fallback chain; each
    row points at a real file the host can serve."""

    def __init__(self, *, icon_font: Path | None = None, runner=None) -> None:
        self.icon_font = Path(icon_font) if icon_font is not None else ICON_FONT
        self.runner = runner or _run
        self._revision = 0
        self._signature: str | None = None
        self._chain: list[dict[str, Any]] | None = None
        self._digests: dict[tuple[str, int, int], tuple[str, int]] = {}

    def refresh(self) -> None:
        """What the `font-set` hook means: forget what fontconfig said.

        The chain costs two `fc-*` processes per probe, and `/v1/fonts` is read
        on every client reconnect, so it is remembered. The hook — and nothing
        else — is allowed to say it is stale. File *contents* need no hook: the
        digest cache is keyed on the file's own mtime and size.
        """
        self._chain = None

    def family(self) -> str:
        value = self.runner((FONT_CURRENT,)).strip().splitlines()
        name = value[0].strip() if value else ""
        if not _FAMILY.fullmatch(name):
            raise FontsUnavailable("font_family_unavailable")
        return name

    def _digest(self, path: Path) -> tuple[str, int]:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        value = self._digests.get(key)
        if value is None:
            value = sha256_of(path)
            if len(self._digests) > 64:
                self._digests.clear()
            self._digests[key] = value
        return value

    def _fc_match(self, pattern: str) -> tuple[str, Path] | None:
        text = self.runner((FC_MATCH, pattern, "-f", "%{family[0]}\t%{file}\n"))
        name, tab, file_text = text.strip().partition("\t")
        name, file_text = name.strip(), file_text.strip()
        if not tab or not file_text or not _FAMILY.fullmatch(name):
            return None
        return name, Path(file_text)

    def _covers(self, path: Path, code: int) -> bool:
        """`fc-match` always answers something — it is a *match*, not a lookup.

        Asked for a code point no installed font carries it hands back the
        matched monospace family anyway, so publishing its answer unchecked
        would publish a family that draws tofu. `fc-list :charset=` is the
        lookup: it lists only the files that really carry the point.
        """
        text = self.runner((FC_LIST, f":charset={code:x}", "file"))
        wanted = str(path)
        for line in text.splitlines():
            if line.strip().rstrip().removesuffix(":").strip() == wanted:
                return True
        return False

    def _links(self) -> list[dict[str, Any]]:
        """`[{coverage, family, path, probes}]`, in probe order, one per file."""
        if self._chain is not None:
            return self._chain
        links: list[dict[str, Any]] = []
        by_path: dict[str, dict[str, Any]] = {}
        for coverage, codes in FALLBACK_PROBES:
            for code in codes:
                try:
                    answer = self._fc_match(f"monospace:charset={code:x}")
                    if answer is None or not self._covers(answer[1], code):
                        continue
                except (FontsUnavailable, OSError):
                    continue
                name, path = answer
                link = by_path.get(str(path))
                if link is None:
                    link = {"coverage": coverage, "family": name, "path": path, "probes": []}
                    by_path[str(path)] = link
                    links.append(link)
                link["probes"].append(probe_label(code))
        self._chain = links
        return links

    def document(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The rows and the chain, built together so a link can point at a row
        the mono role already published rather than at a second copy of it."""
        rows: list[dict[str, Any]] = []
        ids_by_path: dict[str, str] = {}

        def publish(identifier: str, role: str, name: str | None, path: Path) -> bool:
            try:
                sha256, size = self._digest(path)
            except (OSError, FontsUnavailable):
                return False
            rows.append({"id": identifier, "role": role, "family": name, "path": str(path),
                         "sha256": sha256, "bytes": size,
                         "content_type": _FONT_TYPES.get(path.suffix.lower(), "font/sfnt")})
            ids_by_path[str(path)] = identifier
            return True

        try:
            family = self.family()
            styles = parse_fc_list(self.runner((FC_LIST, family)), family)
        except FontsUnavailable:
            family, styles = None, {}
        for identifier, style in _STYLES.items():
            path = styles.get(style)
            if path is not None:
                publish(identifier, "mono", family, path)
        publish("icons", "icons", "omarchy", self.icon_font)

        chain: list[dict[str, Any]] = []
        taken: set[str] = set()
        for link in self._links():
            path, name = link["path"], link["family"]
            identifier = ids_by_path.get(str(path))
            if identifier is None:
                slug, index = link["coverage"], 1
                while slug in taken:
                    index += 1
                    slug = f"{link['coverage']}-{index}"
                if publish(f"fallback-{slug}", "fallback", name, path):
                    taken.add(slug)
                    identifier = f"fallback-{slug}"
            # A family whose file this host will not serve — unreadable, or
            # past MAX_FONT_BYTES — is still the truth about what draws that
            # code point here, so it is reported without a download.
            chain.append({"coverage": link["coverage"], "family": name,
                          "font": identifier, "probes": list(link["probes"])})
        if not rows:
            raise FontsUnavailable("fonts_unavailable")
        return rows, chain

    def listing(self) -> list[dict[str, Any]]:
        return self.document()[0]

    def snapshot(self) -> dict[str, Any]:
        rows, chain = self.document()
        import json
        signature = json.dumps([rows, chain], sort_keys=True)
        if signature != self._signature:
            self._signature = signature
            self._revision += 1
        return {"revision": self._revision, "fonts": rows, "fallback_chain": chain}

    def resolve(self, identifier: str) -> dict[str, Any]:
        """The one row a `/v1/fonts/{id}` download refers to; no path from a client."""
        for row in self.listing():
            if row["id"] == identifier:
                return row
        raise FontsUnavailable("font_not_found")

    @property
    def revision(self) -> int:
        return self._revision
