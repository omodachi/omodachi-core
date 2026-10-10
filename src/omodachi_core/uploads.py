"""ATTACH-1: a file from the phone, kept on the host for an agent to read.

`POST /v1/uploads` writes one file into `~/.local/state/omodachi/uploads/`
(directory 0700, file 0600) and answers with its id and its absolute path. The
Agent turns an image into codex's own image input (`localImage {path}`) and a
text file or a PDF into a path in the message; Herdr types the path into the
pane where the chip stood. A terminal cannot take a pasted file, so a path the
agent reads with its own tools is the whole mechanism (Collie's
`bridge/uploads.ts`, ADR 0060).

What is accepted is decided by what the file *is*, not by what it is called:

- **images** by their signature bytes: PNG, JPEG, GIF, WebP. HEIC (an iPhone's
  camera default) is converted to JPEG here, because codex 0.154 drops a HEIC
  `localImage` without a word (ATTACH-1 report §1). SVG is refused on purpose:
  it is script-bearing markup, not a picture.
- **PDF** by `%PDF-` at the start.
- **text and code** by the extension (`TEXT_EXTS`), with the bytes as a veto:
  a NUL or a stray control byte anywhere refuses it, and so does anything that
  is not UTF-8.

Everything else is refused with a reason a phone can put in words. The cap
(25 MB) is enforced while the body streams in, so an oversize upload never
lands. A file is kept seven days and then swept - on start and every hour.

Nothing here ever logs the bytes or the name; the journal line says which
device, what kind and how many bytes.
"""
from __future__ import annotations

import codecs
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import uuid

from .service import ServiceError

UPLOAD_DIR = ".local/state/omodachi/uploads"
MAX_BYTES = 25 * 1024 * 1024
TTL_SECONDS = 7 * 24 * 60 * 60
SWEEP_INTERVAL_SECONDS = 60 * 60
# A `.part` older than this is a stream that died with the connection.
PART_TTL_SECONDS = 60 * 60
MAX_ATTACHMENTS = 10
TARGETS = ("agent", "herdr", "file")

IMAGE_EXTS = ("png", "jpg", "gif", "webp")
# The text formats an agent reads off a path. Bare lowercase extensions.
TEXT_EXTS = (
    "md", "markdown", "txt", "text", "rst", "org", "adoc", "tex",
    "json", "jsonl", "ndjson", "yaml", "yml", "toml", "ini", "cfg", "conf",
    "csv", "tsv", "log", "xml", "html", "htm", "css", "scss", "sass", "less",
    "js", "jsx", "mjs", "cjs", "ts", "tsx", "vue", "svelte",
    "py", "pyi", "ipynb", "go", "rs", "c", "h", "cc", "cpp", "cxx", "hpp", "hh",
    "java", "kt", "kts", "swift", "m", "mm", "rb", "php", "pl", "lua", "r", "jl",
    "scala", "dart", "zig", "nim", "ex", "exs", "erl", "hs", "ml", "clj",
    "sh", "bash", "zsh", "fish", "sql", "graphql", "gql", "proto", "diff", "patch",
)
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif",
               "webp": "image/webp", "pdf": "application/pdf"}
# HEIF brands an iPhone (or anything else writing HEIC) puts in `ftyp`.
HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
SNIFF_BYTES = 4096

ID = re.compile(r"[0-9a-f]{32}")
STORED = re.compile(r"(agent|herdr|file)-[0-9]{8}T[0-9]{6}Z-([0-9a-f]{32})\.([a-z0-9]{1,10})")
PART = re.compile(r"\.upload-[0-9a-f]{32}\.(part|jpg)")
MARKER = re.compile(r"\[(Image|File) #([0-9]{1,3})\]")
# Bytes a real text file does contain: tab, LF, VT, FF, CR, and ESC (a log
# with ANSI colour is still a log). Everything else below 0x20, and DEL, is a
# binary wearing a text name.
_CONTROL = re.compile(rb"[\x00-\x08\x0e-\x1a\x1c-\x1f\x7f]")


REFUSALS = {
    "upload_too_large": "the file is larger than 25 MB",
    "upload_svg_refused": "SVG is not accepted: it is markup that can carry scripts, not a picture",
    "upload_type_refused": "only images, PDFs and text or code files are accepted",
    "upload_not_text": "a text file has to be UTF-8 text with no control bytes",
    "upload_heic_unconvertible": "this HEIC photo could not be turned into a JPEG on the host",
    "upload_empty": "the file is empty",
}


def refused(code: str, status: int = 415) -> ServiceError:
    return ServiceError(code, REFUSALS.get(code, code), status)


# ── the accept decision, pure ──────────────────────────────────────────────

def image_ext(head: bytes) -> str | None:
    """The image format the leading bytes are, or None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def is_heic(head: bytes) -> bool:
    return len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in HEIC_BRANDS


def is_pdf(head: bytes) -> bool:
    return head.startswith(b"%PDF-")


def looks_like_svg(name_ext: str | None, head: bytes) -> bool:
    if name_ext in ("svg", "svgz"):
        return True
    sample = head[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return sample.startswith(b"<svg") or (sample.startswith(b"<?xml") and b"<svg" in head.lower())


def ext_from_name(name: str) -> str | None:
    """The bare lowercase extension a name claims. A leading dot is not one."""
    dot = name.rfind(".")
    if dot <= 0 or dot == len(name) - 1:
        return None
    ext = name[dot + 1:].lower()
    return ext if re.fullmatch(r"[a-z0-9]{1,10}", ext) else None


def classify(name: str, head: bytes, *, text_ok: bool) -> tuple[str, str]:
    """`(kind, ext)` to store this file under, or a refusal.

    Bytes first: an image is what its signature says whatever it is called,
    a PDF is `%PDF-`. Then the name, against the text list, with the bytes as
    a veto (`text_ok` is the whole file's verdict: UTF-8, no control bytes).
    """
    image = image_ext(head)
    if image is not None:
        return "image", image
    if is_heic(head):
        return "image", "heic"  # converted to JPEG before it is kept
    if is_pdf(head):
        return "pdf", "pdf"
    claimed = ext_from_name(name)
    if looks_like_svg(claimed, head):
        raise refused("upload_svg_refused")
    if claimed not in TEXT_EXTS:
        raise refused("upload_type_refused")
    if not text_ok:
        raise refused("upload_not_text")
    return "text", claimed


def clean_name(name) -> str:
    """The name to show for a file: no directories, no control characters,
    NFC, at most 120 characters. It is never part of a path on disk."""
    if not isinstance(name, str):
        return "file"
    value = unicodedata.normalize("NFC", name.replace("\\", "/").rsplit("/", 1)[-1])
    value = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C" and ch not in '<>:"|?*')
    value = value.strip().strip(".") or "file"
    if len(value) > 120:
        stem, dot, ext = value.rpartition(".")
        value = (stem[:110] + "…." + ext) if dot and len(ext) <= 10 else value[:119] + "…"
    return value


# ── markers and paths, pure ────────────────────────────────────────────────

def compose_herdr(text: str, attachments) -> str:
    """The line typed into a pane (Collie ADR 0060). Each `[Image #N]` /
    `[File #N]` marker that has an attachment behind it becomes that file's
    path where it stands; an attachment whose marker is gone from the text is
    not dropped - its path goes in front, in order. A marker with nothing
    behind it is left exactly as typed. `attachments` is `[(marker, path)]`."""
    by_marker, orphans = {}, []
    for marker, path in attachments:
        if marker and marker in text:
            by_marker[marker] = path
        else:
            orphans.append(path)
    line = MARKER.sub(lambda m: by_marker.get(m.group(0), m.group(0)), text)
    words = line.strip()
    return " ".join([*orphans, *([words] if words else [])])


def compose_agent(text: str, attachments) -> tuple[str, list[str]]:
    """What the default agent is sent: `(text, image paths)`.

    Images go to codex as its own image input, in order, and codex calls them
    `[Image #1]`, `[Image #2]`, … by that order - so an image's marker is
    renumbered to match (a draft numbers files and images together). A text
    file or a PDF becomes its path where its marker stood; one without a marker
    is listed on a last line, `Attached: <path>`, for the agent to open with its
    tools. `attachments` is `[(marker, upload)]`."""
    images, replace, unplaced = [], {}, []
    for marker, upload in attachments:
        if upload.kind == "image":
            images.append(upload.path)
            if marker:
                replace[marker] = f"[Image #{len(images)}]"
        elif marker and marker in text:
            replace[marker] = upload.path
        else:
            unplaced.append(upload.path)
    body = MARKER.sub(lambda m: replace.get(m.group(0), m.group(0)), text).strip()
    if unplaced:
        body = (body + "\n\n" if body else "") + "Attached: " + ", ".join(unplaced)
    if not body and images:
        body = " ".join(f"[Image #{n}]" for n in range(1, len(images) + 1))
    return body, images


def attachment_refs(value) -> list[tuple[str, str | None]]:
    """`attachments` as a send carries it: upload ids, or `{id, marker}`."""
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
        raise ServiceError("invalid_request", "attachments must be a list of at most ten")
    refs, seen = [], set()
    for item in value:
        marker = None
        if isinstance(item, dict):
            if set(item) - {"id", "marker"} or "id" not in item:
                raise ServiceError("invalid_request")
            marker = item.get("marker")
            if marker is not None and not (isinstance(marker, str) and MARKER.fullmatch(marker)):
                raise ServiceError("invalid_request", "invalid attachment marker")
            item = item["id"]
        if not isinstance(item, str) or not ID.fullmatch(item) or item in seen:
            raise ServiceError("invalid_request", "invalid attachment id")
        seen.add(item)
        refs.append((item, marker))
    return refs


# ── the store ──────────────────────────────────────────────────────────────

@dataclass
class Upload:
    id: str
    name: str
    kind: str
    bytes: int
    path: str
    created_at: float
    device: str | None = None
    target: dict = field(default_factory=dict)

    def document(self, ttl=TTL_SECONDS) -> dict:
        ext = ext_from_name(self.path) or ""
        return {"id": self.id, "name": self.name, "kind": self.kind, "bytes": self.bytes,
                "path": self.path, "media_type": MEDIA_TYPES.get(ext, "text/plain"),
                "expires_at": datetime.fromtimestamp(self.created_at + ttl, timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")}


def journal(entry: dict) -> None:
    """One JSON line on the daemon's journal: device, kind, bytes - never the
    name and never the content."""
    try:
        print(json.dumps({"omodachi": "upload", **entry}, separators=(",", ":"), default=str), flush=True)
    except (OSError, ValueError):
        pass


def heic_to_jpeg(source: Path, target: Path) -> bool:
    """libheif's `heif-dec`, else ImageMagick. False when neither could."""
    for argv in (("heif-dec", "-q", "90", str(source), "-o", str(target)),
                 ("magick", str(source), "-quality", "90", "jpg:" + str(target))):
        if shutil.which(argv[0]) is None:
            continue
        try:
            subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60, check=True)
        except (OSError, subprocess.SubprocessError):
            target.unlink(missing_ok=True)
            continue
        try:
            with open(target, "rb") as handle:
                if image_ext(handle.read(16)) == "jpg":
                    return True
        except OSError:
            pass
        target.unlink(missing_ok=True)
    return False


class Receiving:
    """One upload while its body streams in: written to a `.part` file, the
    cap checked on every chunk, the text verdict kept as it goes."""

    def __init__(self, store: "UploadStore") -> None:
        self.store = store
        self.id = uuid.uuid4().hex
        self.part = store.directory / f".upload-{self.id}.part"
        self.size = 0
        self.head = b""
        self.text_ok = True
        self._utf8 = codecs.getincrementaldecoder("utf-8")()
        store.ensure_directory()
        self._fd = os.open(self.part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.size += len(chunk)
        if self.size > self.store.max_bytes:
            self.abort()
            raise refused("upload_too_large", 413)
        if len(self.head) < SNIFF_BYTES:
            self.head += chunk[:SNIFF_BYTES - len(self.head)]
        if self.text_ok:
            if _CONTROL.search(chunk):
                self.text_ok = False
            else:
                try:
                    self._utf8.decode(chunk)
                except UnicodeDecodeError:
                    self.text_ok = False
        view = memoryview(chunk)
        while view:
            written = os.write(self._fd, view)
            view = view[written:]

    def abort(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.part.unlink(missing_ok=True)

    def finish(self, name, *, device=None, target=None) -> Upload:
        os.close(self._fd)
        self._fd = None
        try:
            if self.size == 0:
                raise refused("upload_empty", 400)
            if self.text_ok:
                try:
                    self._utf8.decode(b"", final=True)
                except UnicodeDecodeError:
                    self.text_ok = False
            kind, ext = classify(name, self.head, text_ok=self.text_ok)
            shown = clean_name(name)
            source = self.part
            if ext == "heic":
                converted = self.store.directory / f".upload-{self.id}.jpg"
                if not self.store.converter(self.part, converted):
                    raise refused("upload_heic_unconvertible")
                self.part.unlink(missing_ok=True)
                source, ext = converted, "jpg"
                stem = shown.rsplit(".", 1)[0] if "." in shown else shown
                shown = stem + ".jpg"
            return self.store.keep(self.id, source, ext, kind, shown, device=device, target=target or {})
        except BaseException:
            self.part.unlink(missing_ok=True)
            (self.store.directory / f".upload-{self.id}.jpg").unlink(missing_ok=True)
            raise


class UploadStore:
    def __init__(self, directory=None, *, clock=time.time, max_bytes=MAX_BYTES, ttl=TTL_SECONDS,
                 converter=heic_to_jpeg) -> None:
        self.directory = Path(directory) if directory is not None else Path.home() / UPLOAD_DIR
        self.clock, self.max_bytes, self.ttl, self.converter = clock, max_bytes, ttl, converter
        self._known: dict[str, Upload] = {}
        self._lock = threading.Lock()

    def ensure_directory(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise ServiceError("upload_unavailable", "the uploads directory is not ours", 503)
        if self.directory.stat().st_mode & 0o777 != 0o700:
            os.chmod(self.directory, 0o700)

    def receive(self) -> Receiving:
        return Receiving(self)

    def keep(self, upload_id, source: Path, ext, kind, name, *, device=None, target=None) -> Upload:
        now = self.clock()
        stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        prefix = (target or {}).get("kind") if (target or {}).get("kind") in TARGETS else "file"
        final = self.directory / f"{prefix}-{stamp}-{upload_id}.{ext}"
        os.chmod(source, 0o600)
        os.replace(source, final)
        size = final.stat().st_size
        upload = Upload(upload_id, name, kind, size, str(final.resolve()), now, device, dict(target or {}))
        with self._lock:
            self._known[upload_id] = upload
        return upload

    def resolve(self, upload_id) -> Upload:
        """The upload an id names, or `409 attachment_gone` (removed, swept,
        or never here). After a restart the name shown is the stored one."""
        if not isinstance(upload_id, str) or not ID.fullmatch(upload_id):
            raise ServiceError("invalid_request", "invalid attachment id")
        with self._lock:
            known = self._known.get(upload_id)
        if known is not None and os.path.isfile(known.path):
            return known
        for path in self._stored():
            match = STORED.fullmatch(path.name)
            if match and match.group(2) == upload_id:
                ext = match.group(3)
                kind = "image" if ext in IMAGE_EXTS else "pdf" if ext == "pdf" else "text"
                info = path.stat()
                upload = Upload(upload_id, path.name, kind, info.st_size, str(path.resolve()), info.st_mtime)
                with self._lock:
                    self._known[upload_id] = upload
                return upload
        with self._lock:
            self._known.pop(upload_id, None)
        raise ServiceError("attachment_gone", "that attachment is no longer on the host", 409)

    def delete(self, upload_id) -> dict:
        try:
            upload = self.resolve(upload_id)
        except ServiceError as error:
            if error.code == "attachment_gone":
                raise ServiceError("upload_unknown", "no such upload", 404) from None
            raise
        Path(upload.path).unlink(missing_ok=True)
        with self._lock:
            self._known.pop(upload_id, None)
        return {"id": upload_id, "removed": True}

    def _stored(self):
        try:
            return [path for path in self.directory.iterdir()
                    if not path.is_symlink() and path.is_file()]
        except OSError:
            return []

    def sweep(self, now=None) -> list[str]:
        """Remove every upload older than the TTL and every `.part` a dead
        stream left; never a file this module did not name."""
        now = self.clock() if now is None else now
        removed = []
        for path in self._stored():
            ours = STORED.fullmatch(path.name)
            part = PART.fullmatch(path.name)
            if not ours and not part:
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age > (self.ttl if ours else PART_TTL_SECONDS):
                try:
                    path.unlink()
                except OSError:
                    continue
                removed.append(path.name)
                if ours:
                    with self._lock:
                        self._known.pop(ours.group(2), None)
        return removed
