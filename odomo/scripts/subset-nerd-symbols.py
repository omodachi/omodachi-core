#!/usr/bin/env python3
"""Cut the Herdr mirror's icon fallback from Symbols Nerd Font Mono.

AGENT-UI-2: prompts, lualine, neo-tree and the agents' TUIs draw Nerd Font
code points (powerline separators, file and git icons). JetBrains Mono Web is
a 123-glyph subset and no phone has these glyphs, so they were drawn as empty
boxes. The App bundles the same face whole (omodachi-ios
`Omodachi/Resources/Fonts/SymbolsNerdFontMono-Regular.ttf`, Nerd Fonts v3.5.1,
MIT, `docs/THIRD-PARTY.md`); Odomo carries it as two WOFF2 files split on the
Basic and Supplementary Private Use Areas, each behind a `unicode-range`, so a
browser downloads one only when a pane shows a glyph from it.

The files are committed under src/font/ and are inputs to the build, not
outputs of it; this is how they were made (fontTools + brotli), and running it
again on the same input writes the same bytes.

    python3 scripts/subset-nerd-symbols.py <SymbolsNerdFontMono-Regular.ttf>
"""
from hashlib import sha256
from pathlib import Path
import sys

from fontTools import subset
from fontTools.ttLib import TTFont

SOURCE_SHA256 = "fe471e538392f51910faab985fa8e192a39dd3426125edd15b71b3680df0e749"
OUT = Path(__file__).resolve().parents[1] / "src" / "font"
# The power symbols Nerd Fonts adds outside the PUA (IEC 5009 and 60417).
EXTRA = [0x23FB, 0x23FC, 0x23FD, 0x23FE, 0x2B58]
PARTS = {
    "symbols-nerd-font-mono-bmp.woff2": lambda cp: 0xE000 <= cp <= 0xF8FF or cp in EXTRA,
    "symbols-nerd-font-mono-spua.woff2": lambda cp: cp >= 0xF0000,
}


def main(path):
    data = Path(path).read_bytes()
    if sha256(data).hexdigest() != SOURCE_SHA256:
        sys.exit(f"{path} is not Symbols Nerd Font Mono v3.5.1 (sha256 {SOURCE_SHA256})")
    for name, wanted in PARTS.items():
        font = TTFont(path, recalcTimestamp=False)
        unicodes = [cp for cp in font.getBestCmap() if wanted(cp)]
        options = subset.Options()
        options.flavor = "woff2"
        options.layout_features = []
        options.name_IDs = ["*"]
        options.name_languages = ["*"]
        options.hinting = False
        options.notdef_outline = True
        subsetter = subset.Subsetter(options)
        subsetter.populate(unicodes=unicodes)
        subsetter.subset(font)
        font.flavor = "woff2"
        font.save(OUT / name)
        print(f"{name}: {len(unicodes)} glyphs, {(OUT / name).stat().st_size} bytes")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
