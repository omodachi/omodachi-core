from __future__ import annotations

"""Omarchy JSONC menu compiler.

The catalog is deliberately sourced from menu JSONC only.  Script annotations
are optional metadata used by :func:`parse_script_annotations` and
:func:`validate_action_params`; they never create catalog entries.
"""

from dataclasses import dataclass, field
import json
import re
import shlex
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class CatalogError(ValueError):
    pass


class JsoncError(CatalogError):
    pass


def loads_jsonc(text: str) -> Any:
    """Parse JSON with ``//``/``/* */`` comments and trailing commas.

    A tiny scanner is used instead of regex so comment markers inside strings
    remain intact. This accepts the syntax used by Omarchy's menu files.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    escaped = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            if end < 0:
                raise JsoncError("unterminated block comment")
            i = end + 2
            continue
        out.append(c)
        i += 1
    cleaned = "".join(out)
    # Remove commas immediately before a closing delimiter, outside strings.
    out = []
    in_string = False
    escaped = False
    for i, c in enumerate(cleaned):
        if in_string:
            out.append(c)
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
            out.append(c)
            continue
        if c == ",":
            j = i + 1
            while j < len(cleaned) and cleaned[j].isspace():
                j += 1
            if j < len(cleaned) and cleaned[j] in "]}":
                continue
        out.append(c)
    try:
        return json.loads("".join(out))
    except json.JSONDecodeError as exc:
        raise JsoncError(f"invalid JSONC at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc


def load_jsonc(path: str | Path) -> Any:
    return loads_jsonc(Path(path).read_text(encoding="utf-8"))


def app_entry_id(app_id: str) -> str:
    """Stable catalog ID for desktop IDs, including spaces/non-ASCII/long IDs.

    The reserved xdg- namespace prevents a real desktop name from colliding with
    an encoded one. The original appId remains metadata, never an Exec body.
    """
    import hashlib
    import re
    if (not isinstance(app_id, str) or not app_id or len(app_id.encode("utf-8")) > 255
            or app_id.startswith(("-", ".")) or "/" in app_id or "\\" in app_id
            or any(ord(c) < 32 or ord(c) == 127 for c in app_id)):
        raise CatalogError("invalid desktop app id")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,122}", app_id) and not app_id.startswith("xdg-"):
        return "apps." + app_id
    return "apps.xdg-" + hashlib.sha256(app_id.encode("utf-8")).hexdigest()


def _app_kind(entry_id: str, row: Mapping[str, Any]) -> bool:
    if row.get("kind") != "app":
        return False
    if (entry_id != app_entry_id(row.get("appId")) or row.get("parent", "apps") != "apps"
            or any(row.get(key) for key in ("action", "target", "provider", "surface", "when", "checked"))):
        raise CatalogError("invalid desktop app row")
    # Apps use a closed metadata schema; arbitrary Desktop Entry fields (most
    # critically Exec) are not inherited from the generic static-row contract.
    allowed = {"id", "parent", "parent_id", "kind", "appId", "appRevision", "label", "icon",
               "action", "target", "provider", "surface", "when", "checked", "iconFont",
               "icon_kind", "title", "description", "aliases"}
    if set(row) - allowed:
        raise CatalogError("desktop execution source is private")
    for key, maximum in (("label", 512), ("icon", 1024)):
        value = row.get(key, "")
        if (not isinstance(value, str) or len(value.encode("utf-8")) > maximum
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise CatalogError("invalid desktop metadata")
    if row.get("parent_id") not in {None, "apps"}:
        raise CatalogError("invalid desktop parent")
    revision = row.get("appRevision")
    if revision is not None:
        import re
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
            raise CatalogError("invalid desktop revision")
    return True


def _normalize_row(entry_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """Match Omarchy MenuModel.normalizeItem defaults per source layer."""
    row = dict(value)
    parent = row.get("parent")
    if parent is None:
        parent = _parent_id(entry_id) or "root"
    if entry_id == "root":
        parent = ""
    aliases = row.get("aliases", ())
    if isinstance(aliases, str):
        aliases = [aliases] if aliases else []
    elif isinstance(aliases, Sequence):
        aliases = [x for x in aliases if x]
    else:
        aliases = []
    # Omarchy's Apps provider has a special kind, beyond static source inference.
    is_app = _app_kind(entry_id, row)
    row.update({
        "id": entry_id,
        "parent": parent,
        "kind": "app" if is_app else ("action" if row.get("action") else ("link" if row.get("target") else "menu")),
        "icon": row.get("icon") or "",
        "iconFont": row.get("iconFont") or "",
        "label": row.get("label") or entry_id,
        "title": row.get("title") or "",
        "target": row.get("target") or "",
        "description": row.get("description") or "",
        "action": row.get("action") or "",
        "provider": row.get("provider") or "",
        "aliases": aliases,
        "when": row.get("when") or "",
        "checked": row.get("checked") or "",
    })
    # ICON-1. Every row carries what kind of thing its `icon` is, static rows
    # and provider rows alike — the provider path (`CatalogRuntime`) normalizes
    # here and never builds a `CatalogEntry`, so this is the one place that
    # sees both.
    from .icons import classify_icon
    row["icon_kind"] = classify_icon(row["icon"], row["iconFont"])
    return row


def _entries(source: Any) -> list[dict[str, Any]]:
    """Normalize MenuModel object/list shapes into ordered rows."""
    if source is None:
        return []
    if isinstance(source, list):
        rows = source
    elif isinstance(source, dict):
        for key in ("items", "routes", "menu", "entries"):
            if isinstance(source.get(key), list):
                rows = source[key]
                break
            if isinstance(source.get(key), dict):
                source = source[key]
                rows = []
                for k, v in source.items():
                    if isinstance(v, dict):
                        rows.append(_normalize_row(str(k), v))
                break
        else:
            rows = []
            for key, value in source.items():
                if isinstance(value, dict):
                    rows.append(_normalize_row(str(key), value))
    else:
        raise CatalogError("menu source must be an object or array")
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            raise CatalogError("every menu entry must be an object with a non-empty id")
        # List rows may already have id; normalize each independently so an
        # override layer receives the same defaults as MenuModel.js.
        result.append(_normalize_row(str(row["id"]), row))
    return result

def _merge_rows(sources: Iterable[Any]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for source in sources:
        for row in _entries(source):
            entry_id = str(row["id"])
            if entry_id in positions:
                # MenuModel's override is a shallow field merge after each
                # source row receives explicit defaults.
                merged[positions[entry_id]].update(row)
            else:
                positions[entry_id] = len(merged)
                merged.append(row)
    return merged


def _infer_kind(row: Mapping[str, Any]) -> str:
    if _app_kind(str(row.get("id", "")), row):
        return "app"
    # Static Omarchy MenuModel rows infer action/link/menu; provider is metadata.
    if row.get("action"):
        return "action"
    if row.get("target"):
        return "link"
    return "menu"


def _parent_id(entry_id: str) -> str | None:
    return entry_id.rsplit(".", 1)[0] if "." in entry_id else None


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    kind: str
    parent_id: str | None
    label: Any = None
    icon: Any = None
    icon_font: Any = None
    aliases: tuple[str, ...] = ()
    action: Any = None
    target: Any = None
    provider: Any = None
    when: Any = None
    checked: Any = None
    surface: str | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        result = dict(self.fields)
        result.update({"id": self.id, "kind": self.kind, "parent_id": self.parent_id})
        # Keep canonical fields present when present in source; this avoids
        # silently dropping labels/icons/conditions during serialization.
        for name, value in (("label", self.label), ("icon", self.icon), ("iconFont", self.icon_font),
                            ("aliases", list(self.aliases)), ("action", self.action), ("target", self.target),
                            ("provider", self.provider), ("when", self.when), ("checked", self.checked),
                            ("surface", self.surface)):
            if value is not None and (name in self.fields or name in {"label", "icon", "iconFont", "aliases", "action", "target", "provider", "when", "checked", "surface"}):
                result[name] = value
        # ICON-1. `icon` stays exactly what the host wrote; `icon_kind` says
        # what kind of thing that is, so a client stops having to guess from
        # the string's shape whether it is a code point it can draw or a name
        # only `GET /v1/icons/{name}` can turn into a picture.
        from .icons import classify_icon
        result["icon_kind"] = classify_icon(result.get("icon", ""), result.get("iconFont", ""))
        return result


@dataclass(frozen=True)
class Catalog:
    entries: tuple[CatalogEntry, ...]
    revision: str
    sources: tuple[str, ...] = ()

    def by_id(self, entry_id: str) -> CatalogEntry | None:
        return next((entry for entry in self.entries if entry.id == entry_id), None)

    def as_dict(self) -> dict[str, Any]:
        return {"revision": self.revision, "entries": [e.as_dict() for e in self.entries]}


def compile_catalog(*sources: Any, source_names: Sequence[str] = ()) -> Catalog:
    """Merge default, user and Omodachi menu sources into one stable catalog."""
    rows = _merge_rows(sources)
    if not any(str(row.get("id")) == "root" for row in rows):
        rows.insert(0, _normalize_row("root", {"label": "Go"}))
    entries: list[CatalogEntry] = []
    for row in rows:
        aliases = row.get("aliases", ())
        if isinstance(aliases, str):
            aliases = (aliases,)
        elif isinstance(aliases, Sequence):
            aliases = tuple(str(x) for x in aliases)
        else:
            aliases = ()
        fields = dict(row)
        # id/kind/parent are represented by typed fields; retain all other
        # unknown fields (including iconFont, nested provider metadata, etc.).
        fields.pop("id", None)
        fields.pop("kind", None)
        entry = CatalogEntry(
            id=str(row["id"]), kind=_infer_kind(row), parent_id=(str(row.get("parent")) if row.get("parent") is not None else _parent_id(str(row["id"]))),
            label=row.get("label"), icon=row.get("icon"), icon_font=row.get("iconFont"), aliases=aliases,
            action=row.get("action"), target=row.get("target"), provider=row.get("provider"),
            when=row.get("when"), checked=row.get("checked"), surface=row.get("surface"), fields=fields,
        )
        entries.append(entry)
    # Deterministic revision over canonical JSON, independent of dict ordering.
    import hashlib
    payload = json.dumps([e.as_dict() for e in entries], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    revision = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return Catalog(tuple(entries), revision, tuple(source_names))


def compile_catalog_from_jsonc(*paths: str | Path) -> Catalog:
    return compile_catalog(*(load_jsonc(path) for path in paths), source_names=tuple(str(p) for p in paths))


_ANNOTATION_RE = re.compile(r"^\s*#\s*omarchy:(?P<key>[A-Za-z0-9_-]+)=(?P<value>.*)\s*$")


def parse_script_annotations(text: str) -> dict[str, Any]:
    """Read Omarchy ``# omarchy:*`` metadata for help/validation only."""
    result: dict[str, Any] = {}
    for line in text.splitlines()[:80]:
        match = _ANNOTATION_RE.match(line)
        if not match:
            continue
        key, value = match.group("key"), match.group("value").strip()
        if value.lower() in {"true", "false"}:
            parsed: Any = value.lower() == "true"
        elif value.startswith(("[", "{")):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = value
        else:
            parsed = value
        result[key] = parsed
    return result


def _arg_tokens(spec: str) -> list[str]:
    try:
        return shlex.split(spec)
    except ValueError:
        return spec.split()


def validate_action_params(annotations: Mapping[str, Any] | None, params: Mapping[str, Any] | None) -> tuple[bool, str | None]:
    """Validate bounded params against an ``omarchy:args`` annotation.

    This helper intentionally supports only a small grammar: literal flags,
    ``<name>``, ``<a|b>`` enums and optional ``[--flag]`` tokens. It never
    executes or constructs a shell command.
    """
    params = dict(params or {})
    spec = (annotations or {}).get("args")
    if not spec:
        return (not params, "unexpected parameters" if params else None)
    tokens = _arg_tokens(str(spec))
    allowed: set[str] = set()
    required: set[str] = set()
    enums: dict[str, set[str]] = {}
    for token in tokens:
        optional = token.startswith("[") and token.endswith("]")
        token = token[1:-1] if optional else token
        if token.startswith("<") and token.endswith(">"):
            name = token[1:-1]
            parts = name.split("|")
            key = parts[0]
            allowed.add(key)
            if not optional:
                required.add(key)
            if len(parts) > 1:
                enums[key] = set(parts)
        elif token.startswith("--"):
            key = token.lstrip("-").replace("-", "_")
            allowed.add(key)
            if not optional:
                required.add(key)
    unknown = set(params) - allowed
    missing = required - set(params)
    if unknown:
        return False, f"unknown parameters: {', '.join(sorted(unknown))}"
    if missing:
        return False, f"missing parameters: {', '.join(sorted(missing))}"
    for key, values in enums.items():
        if key in params and str(params[key]) not in values:
            return False, f"invalid value for {key}: expected one of {sorted(values)}"
    return True, None
