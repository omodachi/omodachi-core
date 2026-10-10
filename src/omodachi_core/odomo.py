"""LITE-1a C1: Odomo, served from this package's own data.

Odomo is a web app built from `omodachi-core/odomo/` and committed, built, to
`src/omodachi_core/data/odomo/` (so the plugin's tree check passes and the host
never runs a JavaScript toolchain; `scripts/verify_odomo.py` proves the
committed bytes are what the source builds to). This module reads that
directory once into memory and serves exactly those paths:

- no `add_static`, no file system access per request, so no traversal, no
  symlink following and no mtime-derived validator;
- a path is a dictionary key or a 404;
- the ETag is the file's sha256;
- hashed assets (`assets/<name>-<8>.<ext>`) are immutable for a year,
  everything else - `index.html`, `sw.js`, `manifest.webmanifest`, the
  icons - is `no-cache` and revalidated by ETag, so an update is one reload;
- every response carries the CSP LITE-0 §5.2 ran the spike under (no inline
  script, no inline style element, nothing from another origin), `nosniff`,
  `no-referrer` and the build id.

The routes are unauthenticated (a page has to load before it can pair) and
are exempted in `network.boundary`, which still holds them to an allowed
`Host` (`browser_gate.HostNames`).
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re

ODOMO_DIR = Path(__file__).resolve().parent / "data" / "odomo"

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}
_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}(/[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}){0,4}\Z")
_HASHED = re.compile(r"assets/[A-Za-z0-9_.-]+-[A-Za-z0-9_-]{8}\.(js|css|woff2|png|svg)\Z")
MAX_FILES = 256
MAX_BYTES = 16 * 1024 * 1024

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
       "connect-src 'self'; worker-src 'self'; manifest-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'none'")
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"
BUILD_HEADER = "X-Omodachi-Odomo-Build"


@dataclass(frozen=True)
class Asset:
    body: bytes
    sha256: str
    content_type: str
    immutable: bool


def load(root: Path = ODOMO_DIR) -> dict[str, Asset]:
    """Every servable file under `root`, keyed by its URL path below /odomo/."""
    root = Path(root)
    assets: dict[str, Asset] = {}
    total = 0
    if not root.is_dir():
        return assets
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = sorted(name for name in folders if not name.startswith("."))
        for name in sorted(files):
            full = Path(directory) / name
            relative = full.relative_to(root).as_posix()
            if name.startswith(".") or full.is_symlink() or not full.is_file():
                continue
            content_type = TYPES.get(full.suffix.lower())
            if content_type is None or not _PATH.fullmatch(relative):
                continue
            body = full.read_bytes()
            total += len(body)
            if len(assets) >= MAX_FILES or total > MAX_BYTES:
                raise ValueError("odomo bundle too large")
            assets[relative] = Asset(body, hashlib.sha256(body).hexdigest(), content_type,
                                     bool(_HASHED.fullmatch(relative)))
    return assets


def build_id(assets: dict[str, Asset]) -> str:
    """One short digest over every path and its bytes: what the page is."""
    digest = hashlib.sha256()
    for path in sorted(assets):
        digest.update(path.encode() + b"\0" + assets[path].sha256.encode() + b"\n")
    return digest.hexdigest()[:16]


class OdomoBundle:
    def __init__(self, root: Path = ODOMO_DIR):
        self.assets = load(root)
        self.build = build_id(self.assets) if self.assets else None

    def __bool__(self):
        return "index.html" in self.assets

    def get(self, path: str):
        if not isinstance(path, str) or path == "":
            path = "index.html"
        return self.assets.get(path)


def security_headers(build: str | None) -> dict[str, str]:
    headers = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
               "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"}
    if build:
        headers[BUILD_HEADER] = build
    return headers
