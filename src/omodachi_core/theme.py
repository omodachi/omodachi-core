"""The host's current Omarchy theme, read from the files Omarchy itself writes.

Nothing here derives, mixes or guesses a colour. The palette is rendered by
`omarchy-theme-set-templates` from our own template under
`~/.config/omarchy/themed/`, so the values are the ones Omarchy computed for
every other themed application; the design tokens are the generated
`shell.toml`, backfilled only from the defaults in Omarchy's own
`default/themed/shell.toml.tpl`. A client that hardcodes a Tokyo Night hex is
wrong by construction.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tomllib
from typing import Any

STATE_DIR = Path.home() / ".local/state/omarchy/current"
DEFAULT_SHELL_TEMPLATE = Path("/usr/share/omarchy/default/themed/shell.toml.tpl")
TEMPLATE_NAME = "omodachi-theme.json.tpl"
TEMPLATE_SOURCE = Path(__file__).parent / "data" / TEMPLATE_NAME
USER_TEMPLATE_DIR = Path.home() / ".config/omarchy/themed"

# The roles the template publishes: mode, accent/selection/muted, four
# backgrounds, four foregrounds, the eight base colours and the six bright
# ones. Exactly the keys `colors.toml` declares, which is why the fallback can
# read that file directly when a template render has not happened yet.
COLOR_KEYS = (
    "accent", "selection", "muted",
    "background", "dark_background", "darker_background", "lighter_background",
    "foreground", "bright_foreground", "light_foreground", "dark_foreground",
    "red", "yellow", "orange", "green", "cyan", "blue", "magenta", "brown",
    "bright_red", "bright_yellow", "bright_green", "bright_cyan", "bright_blue", "bright_magenta",
)
# The seven sections SPEC-F1 promises are always present; the reader publishes
# every other section the host wrote as well.
REQUIRED_SECTIONS = ("bar", "controls", "spacing", "font", "menu", "popups", "hyprland")
MODES = ("dark", "light")
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_KEY = re.compile(r"[a-z][a-z0-9-]*")
_SECTION = re.compile(r"\[([A-Za-z0-9_-]+)\]")
_REFERENCE = re.compile(r"[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*")
_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
MAX_JSON_BYTES = 262_144
MAX_TOML_BYTES = 1_048_576
MAX_BACKGROUND_BYTES = 33_554_432


class ThemeUnavailable(ValueError):
    """The host has no readable current theme; the boundary answers 503."""


def _read(path: Path, limit: int) -> str:
    with path.open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ThemeUnavailable("theme_source_too_large")
    return raw.decode("utf-8")


def parse_shell_defaults(text: str) -> dict[str, dict[str, Any]]:
    """Read Omarchy's own shell template as the default value set.

    The generated `shell.toml` leaves most of `[spacing]` and `[font]` commented
    out, so the numbers a native client needs live only in those comments. A
    commented default is `# <key> = <toml scalar>`; prose never matches both the
    key shape and a parsable TOML value, and any line still holding a `{{ }}`
    placeholder is a colour the live file always carries, never a default.
    """
    defaults: dict[str, dict[str, Any]] = {}
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        if not stripped or "{{" in stripped:
            continue
        match = _SECTION.fullmatch(stripped)
        if match:
            section = match.group(1)
            defaults.setdefault(section, {})
            continue
        if section is None or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key, value = key.strip(), value.strip()
        if not _KEY.fullmatch(key) or not value:
            continue
        try:
            parsed = tomllib.loads("value = " + value)["value"]
        except (tomllib.TOMLDecodeError, ValueError):
            continue
        if isinstance(parsed, (str, int, float, bool)):
            defaults[section][key] = parsed
    return defaults


def _resolve_references(shell: dict[str, dict[str, Any]]) -> None:
    """`border = "hyprland.active-border"` is Omarchy's own indirection.

    Resolving it here means every surface section answers with a colour, and no
    client has to reimplement the lookup the shell does in `Style.qml`.
    """
    for values in shell.values():
        for key, value in list(values.items()):
            if not isinstance(value, str) or not _REFERENCE.fullmatch(value):
                continue
            section, _, name = value.partition(".")
            target = shell.get(section, {}).get(name)
            if isinstance(target, str):
                values[key] = target


def sha256_of(path: Path, limit: int = MAX_BACKGROUND_BYTES) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(262_144):
            size += len(chunk)
            if size > limit:
                raise ThemeUnavailable("theme_background_too_large")
            digest.update(chunk)
    return digest.hexdigest(), size


class HostTheme:
    """Reads the current theme; `revision` only moves when the payload moves."""

    def __init__(self, state_dir: Path | None = None, *,
                 shell_template: Path | None = None) -> None:
        self.state_dir = Path(state_dir) if state_dir is not None else STATE_DIR
        self.shell_template = Path(shell_template) if shell_template is not None else DEFAULT_SHELL_TEMPLATE
        self._revision = 0
        self._payload: dict[str, Any] | None = None
        self._signature: str | None = None

    # --- individual sources -------------------------------------------------
    @property
    def theme_dir(self) -> Path:
        return self.state_dir / "theme"

    def name(self) -> str:
        try:
            value = _read(self.state_dir / "theme.name", 256).strip()
        except (OSError, ThemeUnavailable, UnicodeError):
            raise ThemeUnavailable("theme_name_unavailable") from None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,127}", value):
            raise ThemeUnavailable("theme_name_unavailable")
        return value

    def colors(self) -> dict[str, str]:
        """The rendered template, or `colors.toml` when it has not rendered yet."""
        rendered = self.theme_dir / "omodachi-theme.json"
        try:
            value = json.loads(_read(rendered, MAX_JSON_BYTES))
        except (OSError, ValueError, UnicodeError, ThemeUnavailable):
            value = self._colors_from_toml()
        if not isinstance(value, dict):
            raise ThemeUnavailable("theme_colors_unavailable")
        mode = value.get("mode")
        colors = {}
        for key in COLOR_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and _COLOR.fullmatch(candidate.strip()):
                colors[key] = candidate.strip().lower()
        if mode not in MODES or len(colors) != len(COLOR_KEYS):
            raise ThemeUnavailable("theme_colors_unavailable")
        return {"mode": mode, **colors}

    def _colors_from_toml(self) -> dict[str, Any]:
        try:
            return tomllib.loads(_read(self.theme_dir / "colors.toml", MAX_TOML_BYTES))
        except (OSError, ValueError, UnicodeError) as error:
            raise ThemeUnavailable("theme_colors_unavailable") from error

    def shell(self) -> dict[str, dict[str, Any]]:
        """Generated design tokens, backfilled from Omarchy's own defaults.

        A theme may replace `shell.toml` wholesale or override one section with
        `shell.<section>.toml`, so a missing section or key is expected and is
        filled from the template rather than from a number written here.
        """
        try:
            live = tomllib.loads(_read(self.theme_dir / "shell.toml", MAX_TOML_BYTES))
        except (OSError, ValueError, UnicodeError):
            live = {}
        try:
            defaults = parse_shell_defaults(_read(self.shell_template, MAX_TOML_BYTES))
        except (OSError, UnicodeError, ThemeUnavailable):
            defaults = {}
        shell: dict[str, dict[str, Any]] = {}
        for section in sorted(set(defaults) | {k for k, v in live.items() if isinstance(v, dict)}):
            values = dict(defaults.get(section, {}))
            for key, value in (live.get(section) or {}).items():
                if isinstance(value, (str, int, float, bool)) and _KEY.fullmatch(str(key)):
                    values[key] = value
            shell[section] = values
        missing = [section for section in REQUIRED_SECTIONS if not shell.get(section)]
        if missing:
            raise ThemeUnavailable("theme_shell_unavailable")
        _resolve_references(shell)
        return shell

    def background(self) -> dict[str, Any]:
        path = self.background_path()
        sha256, size = sha256_of(path)
        return {"sha256": sha256, "bytes": size,
                "content_type": _IMAGE_TYPES.get(path.suffix.lower(), "application/octet-stream")}

    def background_path(self) -> Path:
        """The symlink target, resolved once, and only inside the theme state."""
        link = self.state_dir / "background"
        try:
            path = link.resolve(strict=True)
            if not path.is_file():
                raise OSError("background is not a file")
        except (OSError, RuntimeError) as error:
            raise ThemeUnavailable("theme_background_unavailable") from error
        return path

    # --- the published snapshot --------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        name, colors, shell = self.name(), self.colors(), self.shell()
        try:
            background = self.background()
        except ThemeUnavailable:
            # A theme without a wallpaper is a theme, not a broken host.
            background = None
        mode = colors.pop("mode")
        payload = {"name": name, "mode": mode, "colors": colors, "shell": shell,
                   "background": background}
        signature = json.dumps(payload, sort_keys=True)
        if signature != self._signature:
            self._signature = signature
            self._revision += 1
            self._payload = payload
        return {**self._payload, "revision": self._revision}

    @property
    def revision(self) -> int:
        return self._revision
