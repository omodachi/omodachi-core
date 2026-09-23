"""Safe semantic projection of host-owned Omarchy v4.0.3 shell.json.

Verified tagged source (public source only, not a live host observation):
- config/omarchy/shell.json: version 1; bar.layout.{left,center,right}.
- shell/Commons/Util.qml:127-153: ID strings or {id, ...inline settings};
  absent sections normalize to empty arrays, without a defaults deep merge.
- shell/plugins/bar/Bar.qml:564-569 and BarModel.js: pin omarchy.tray to
  right's start or left/center's end; retain only the last tray per section.
- shell/plugins/bar/widgets/ActiveWindow.qml:9: omarchy.active-window.
- manual/05-the-top-bar.md:90-145: bar position is top/bottom/left/right and
  the canonical config stores it under `bar.position` in shell.json.
- shell/plugins/bar/README.md:51-83: reviewed built-in widget semantics;
  omarchy.agents is an AI usage widget, not Herdr task status telemetry.

This module does not read files, run commands, import QML, or expose settings.
The host supplies already parsed JSON and the source status. Unknown IDs remain
visible as unsupported slots. Limits and malformed IDs fail closed; settings
cannot replace a reviewed role. The Omodachi stream ID is a project-defined
semantic slot used by the explicitly synthetic demo, not an upstream claim.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from .bar_modules import STATUS_ROLES

_SECTIONS = ("left", "center", "right")
_POSITIONS = ("top", "bottom", "left", "right")
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}")
_MAX_SECTION_ENTRIES = 64
_REVIEWED_ROLES = {
    "omarchy.menu": "logo",
    "omarchy.workspaces": "workspaces",
    "omarchy.active-window": "focused_window",
    "omarchy.clock": "clock",
    "omarchy.tray": "system_tray",
    "omarchy.agents": "agent_usage",
    "omarchy.audio": "audio",
    "omarchy.power": "power",
    "omarchy.network": "network",
    "com.omodachi.host": "panel",
    "omodachi.stream": "stream",
}
# One status value is a small flat object of JSON scalars. Anything else is a
# reader bug, and a bar draws nothing rather than something made up.
_MAX_STATUS_FIELDS = 8
_STATUS_KEY = re.compile(r"[a-z][a-z0-9_]{0,31}")


def _status(role: str, statuses: Any) -> dict[str, Any] | None:
    if role not in STATUS_ROLES or not isinstance(statuses, dict):
        return None
    value = statuses.get(role)
    if value is None:
        return None
    if not isinstance(value, dict) or not value or len(value) > _MAX_STATUS_FIELDS:
        return None
    for key, item in value.items():
        if not isinstance(key, str) or not _STATUS_KEY.fullmatch(key):
            return None
        if item is None or type(item) is bool:
            continue
        if type(item) is int and -2 ** 31 < item < 2 ** 31:
            continue
        if type(item) is float and math.isfinite(item):
            continue
        if isinstance(item, str) and len(item) <= 64 and not any(ord(c) < 32 for c in item):
            continue
        return None
    return dict(value)


def _modules(sections: dict[str, list[dict[str, str]]], statuses: Any) -> list[dict[str, Any]]:
    """The status-bearing modules the host's own bar carries, in layout order.

    A client joins these to the layout by `id`. A module the host does not have
    is simply absent; one core cannot read carries `status: null`. Unknown is
    never published as a zero.
    """
    rows, seen = [], set()
    for region in _SECTIONS:
        for entry in sections[region]:
            if entry["role"] not in STATUS_ROLES or entry["id"] in seen:
                continue
            seen.add(entry["id"])
            rows.append({"id": entry["id"], "role": entry["role"], "status": _status(entry["role"], statuses)})
    return rows


def _result(status: str, sections: dict[str, list[dict[str, str]]] | None = None,
            position: str | None = None, statuses: Any = None, geometry: Any = None) -> dict[str, Any]:
    sections = sections or {region: [] for region in _SECTIONS}
    # MENU-2: `geometry` is where that bar actually is on the screen a Remote
    # session is looking at (`bar_geometry.compose`), or None when there is no
    # session, no bar layer, or nothing to measure. It is part of the hashed
    # projection so one `revision` still answers "has anything the client draws
    # or acts on moved" - a bar that slid sideways is exactly such a change.
    result = {"source": "shell.json", "source_status": status, "position": position,
              "modules": _modules(sections, statuses), "geometry": geometry, **sections}
    # Hash only the public projection. Private settings do not affect a public
    # revision, so changes to credentials or QML do not leak through a hash.
    result["revision"] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    return result


def _entry(value: Any) -> dict[str, str] | None:
    if isinstance(value, str):
        identifier, custom = value, False
    elif isinstance(value, dict):
        identifier = value.get("id")
        # BarModel.customModuleType chooses custom content from these fields;
        # a known ID is not sufficient to assert the normal built-in widget.
        custom = bool(value.get("type") or value.get("exec") or value.get("source"))
    else:
        return None
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
        return None
    return {"id": identifier, "role": "unsupported" if custom else _REVIEWED_ROLES.get(identifier, "unsupported")}


def _pin_tray(entries: list[dict[str, str]], region: str) -> list[dict[str, str]]:
    tray = None
    output = []
    for entry in entries:
        if entry["id"] == "omarchy.tray":
            tray = entry
        else:
            output.append(entry)
    if tray is not None:
        if region == "right":
            output.insert(0, tray)
        else:
            output.append(tray)
    return output


def parse_bar_layout(document: Any, *, source_status: str = "available",
                     statuses: Any = None, geometry: Any = None) -> dict[str, Any]:
    """Project parsed shell.json into safe slots; never interpret inline content.

    Result: {source, source_status, position, revision, modules, geometry, left, center, right},
    with sections containing only {id, role} and `modules` carrying the
    status-bearing subset as {id, role, status}. `statuses` is the host's own
    reading per role (see `bar_modules.BarModules`); a role it has nothing for
    stays null. source_status is a host-owned enum: fixture,
    available, or unavailable. Unknown but safe IDs have role=unsupported.
    Invalid version/layout/entry/size returns unavailable empty sections, not a
    guessed default layout. Missing sections are empty as in the tagged source.
    """
    if source_status not in ("fixture", "available", "unavailable"):
        raise ValueError("source_status must be fixture, available, or unavailable")
    if source_status == "unavailable" or not isinstance(document, dict):
        return _result("unavailable")
    if type(document.get("version")) is not int or document["version"] != 1:
        return _result("unavailable")
    bar = document.get("bar")
    if not isinstance(bar, dict) or bar.get("id", "omarchy.bar") != "omarchy.bar":
        return _result("unavailable")
    position = bar.get("position")
    if position is not None and (not isinstance(position, str) or position not in _POSITIONS):
        return _result("unavailable")
    layout = bar.get("layout")
    if not isinstance(layout, dict):
        return _result("unavailable")
    sections = {}
    for region in _SECTIONS:
        values = layout.get(region, [])
        if not isinstance(values, list) or len(values) > _MAX_SECTION_ENTRIES:
            return _result("unavailable")
        entries = []
        for value in values:
            entry = _entry(value)
            if entry is None:
                return _result("unavailable")
            entries.append(entry)
        sections[region] = _pin_tray(entries, region)
    return _result(source_status, sections, position, statuses, geometry)
