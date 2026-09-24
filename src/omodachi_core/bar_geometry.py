"""Where the host's own bar is, on the screen a Remote session is looking at.

MENU-2. The iPad needs one rectangle: the Omarchy logo at the leading end of the
bar. With it the App draws its *own* mark on top, above the stream, so that one
square answers on the device (panel ①) and nothing on the host has to intercept
its own bar for us. That is what retires `com.omodachi.menu`: the clone existed
only so a tap on the logo could be turned into a recall, and it paid for that by
owning the `menu` kind, by rewriting the user's `shell.json` on install, and by
having to track upstream.

Two sources:

* `omarchy-shell omodachi barGeometry` (REMOTE-SAFE-1): the Omodachi plugin's
  own bar widget, which lives inside every screen's bar, reports where that
  bar's `omarchy.menu` slot actually is, in output-local logical px. It is the
  only reading that knows a logo the user moved into `center` (Leo's layout -
  a centred group's position depends on every other widget's width) and the
  one that follows the plugin moving the bar's end sections inward to clear
  the device's corners. When it answers for the session's output, its logo is
  the answer, including "no logo on this bar".
* `hyprctl layers -j` for the `omarchy-bar` layer surface. The document is
  keyed by output name and each entry is `{x, y, w, h, namespace}` in the
  compositor's **layout** coordinates (logical pixels, the same space
  `hyprctl monitors` reports `x`/`y` in), so a session's own output is read by
  subtracting that output's origin. The bar not being there - because the user
  hid it, or because the shell is restarting - is an answer, not an error:
  geometry is then `None` and the App falls back to its corner handle (A-67).

Without the plugin's answer (plugin not in the bar, older plugin, shell busy)
the logo is modelled from the layer as before: first entry of the leading
section only. That fallback is also right about the corners: without the
plugin nothing moved them.

**Only the logo.** The Omodachi plugin's own bar slot is deliberately *not*
located: it is one widget among a dozen third-party ones, its position moves
whenever any of them changes, and covering the wrong square would put a mark of
ours on somebody else's widget. That icon keeps working exactly as it does for
somebody sitting at the machine, and the one thing a takeover has to change
about it — there is nothing useful for it to open while the screens are off —
is decided inside the plugin, where a local mouse click is answered the same way
(Study 04 A-67).

Nothing here runs a command on the event loop: `BarGeometry` throttles itself
the way `BarModules` does, the daemon warms it on a worker, and the compositor's
own `openlayer`/`closelayer omarchy-bar` events mark it dirty so a bar that
moves is re-read on the next tick rather than two seconds later.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import subprocess
import time
import tomllib

from .graphical import bounded_hyprctl, graphical_environment

#: The Omarchy shell's own layer namespace for the bar.
BAR_NAMESPACE = "omarchy-bar"
#: The bar positions `shell.json` allows; the leading end differs per axis.
HORIZONTAL = ("top", "bottom")
VERTICAL = ("left", "right")
#: The layout entry that draws the Omarchy logo. `bar.parse_bar_layout` gives
#: it role `logo`; the id is repeated here because the *first* entry of the
#: leading section has to be that widget for the square to be where we say.
LOGO_ID = "omarchy.menu"
#: A bar wider or taller than this is not a bar.
MAX_EDGE = 16384
#: REMOTE-SAFE-1. The plugin's read-only report (`Service.qml` IpcHandler
#: `omodachi.barGeometry`). A getter on our own target - never a shell setter.
PLUGIN_COMMAND = ("/usr/share/omarchy/bin/omarchy-shell", "omodachi", "barGeometry")

# ---------------------------------------------------------------------------
# The two numbers that decide where the logo actually is (UX-3 §3)
#
# MENU-2 assumed the logo was "a square of the bar's thickness at the bar's
# leading edge". Both halves of that are wrong, and Leo saw it immediately: the
# mark sat to the left of the icon it was meant to cover.
#
# `Bar.qml` anchors the leading section with `anchors.leftMargin: Style.space(8)`
# (`anchors.topMargin` on a vertical bar), and a bar widget is a
# `BarIconButton`, whose extent *along* the bar is `Style.bar.iconSlot` - not
# the bar's thickness. On Leo's host those are 9 and 32 against a 30-thick bar,
# so the modelled square was 9 px too far leading and 2 px too narrow: its
# centre was 10 px out. That is exactly "偏左".
#
# Both numbers are theme-derived, so they are computed the way `Style.qml`
# computes them rather than written down here. The one constant is the
# unscaled icon slot, which `Style.qml` does not let a theme override
# (`barToken` is only consulted for `size-horizontal` / `size-vertical`).
# ---------------------------------------------------------------------------

#: `Style.bar.iconSlot`'s fallback, before the font scale is applied.
ICON_SLOT_BASE = 27
#: `Bar.qml`'s `LeftModules` margin, in `Style.space` units.
LEADING_SPACE_UNITS = 8
#: Omarchy's own rem root. `Style.qml`: `fontScale = max(1/12, base-size / 12)`.
FONT_BASE_DEFAULT = 12
#: Where the user's own overrides live. `Style.qml` reads the theme's tokens and
#: this file on top of them, and this file is where Leo's `base-size = 14` is;
#: reading only the theme would model a 12 px shell on a 14 px host.
USER_SHELL_TOML = ".config/omarchy/shell.toml"


def _round_half_up(value: float) -> int:
    """QML's `Math.round`, which is half-up. Python's `round` is half-even.

    `round(31.5)` is 32 in QML and 32 in this function; it is **31** in Python.
    The icon slot on Leo's host is exactly that value, so the difference is not
    academic.
    """
    return math.floor(value + 0.5)


def _number(value, fallback):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def _flag(value, fallback):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower() if value is not None else ""
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return fallback


@dataclass(frozen=True)
class BarStyle:
    """The leading margin and the icon slot, as `Style.qml` derives them.

    This is a port, not an interpretation: each field below names the QML it
    comes from, and the defaults are QML's defaults.
    """

    #: `Style.space(8)` - the leading section's margin inside the bar.
    leading_space: int = 8
    #: `Style.bar.iconSlot` - one widget's extent *along* the bar.
    icon_slot: int = ICON_SLOT_BASE

    @classmethod
    def from_tokens(cls, shell) -> "BarStyle":
        """`Style.qml` §`applyTheme` + `space()` + `barToken()`, verbatim."""
        if not isinstance(shell, dict):
            shell = {}
        font = shell.get("font") if isinstance(shell.get("font"), dict) else {}
        spacing = shell.get("spacing") if isinstance(shell.get("spacing"), dict) else {}
        bar = shell.get("bar") if isinstance(shell.get("bar"), dict) else {}

        base = _number(font.get("base-size"), FONT_BASE_DEFAULT)
        base = FONT_BASE_DEFAULT if base < 1 else base
        font_scale = max(1 / 12, base / 12)

        spacing_scale = _number(spacing.get("scale"), 1.0)
        if spacing_scale < 0:
            spacing_scale = 1.0
        if _flag(spacing.get("scale-with-font"), True):
            spacing_scale *= font_scale
        leading = max(1, _round_half_up(LEADING_SPACE_UNITS * spacing_scale)) if spacing_scale > 0 else 0

        slot_scale = font_scale if _flag(bar.get("scale-with-font"), True) else 1.0
        slot = max(1, _round_half_up(ICON_SLOT_BASE * slot_scale))
        return cls(leading_space=leading, icon_slot=slot)


def user_shell_tokens(home: Path | None = None) -> dict:
    """The user's own `shell.toml`, or `{}`. A missing file is not an error.

    It is read here rather than through `theme.Theme.shell()` because that
    composes the *theme's* tokens with Omarchy's defaults and never looks at
    the user's file - which is the one that actually carries `base-size` on
    this host.
    """
    path = (home or Path.home()) / USER_SHELL_TOML
    try:
        with path.open("rb") as source:
            raw = source.read(65_537)
        if len(raw) > 65_536:
            return {}
        value = tomllib.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _finite(value, lower, upper):
    return type(value) in (int, float) and math.isfinite(value) and lower <= value <= upper


def _rect(x, y, width, height):
    return {"x": float(x), "y": float(y), "width": float(width), "height": float(height)}


def _valid(rect) -> bool:
    return (isinstance(rect, dict) and set(rect) == {"x", "y", "width", "height"}
            and _finite(rect["x"], -MAX_EDGE, MAX_EDGE) and _finite(rect["y"], -MAX_EDGE, MAX_EDGE)
            and _finite(rect["width"], 1, MAX_EDGE) and _finite(rect["height"], 1, MAX_EDGE))


def bar_layer(document, output_name, *, origin=(0.0, 0.0)):
    """The `omarchy-bar` rectangle on one output, in that output's coordinates.

    `None` whenever the answer would have to be guessed: a document that is not
    the shape `hyprctl layers -j` produces, an output that is not in it, no bar
    layer on that output, or more than one (two shells are not a bar position
    we can name).
    """
    if not isinstance(document, dict) or not isinstance(output_name, str) or not output_name:
        return None
    entry = document.get(output_name)
    if not isinstance(entry, dict):
        return None
    levels = entry.get("levels")
    if not isinstance(levels, dict) or len(levels) > 16:
        return None
    found = []
    for surfaces in levels.values():
        if not isinstance(surfaces, list) or len(surfaces) > 256:
            return None
        for surface in surfaces:
            if not isinstance(surface, dict) or surface.get("namespace") != BAR_NAMESPACE:
                continue
            if not all(_finite(surface.get(key), -MAX_EDGE, MAX_EDGE) for key in ("x", "y")):
                return None
            if not all(_finite(surface.get(key), 1, MAX_EDGE) for key in ("w", "h")):
                return None
            found.append(_rect(surface["x"] - origin[0], surface["y"] - origin[1],
                               surface["w"], surface["h"]))
    if len(found) != 1:
        return None
    return found[0]


def logo_slot(rect, position, sections, style: BarStyle | None = None):
    """Where the shell's own logo is drawn, when it is the leading widget.

    `Bar.qml` puts the leading section `Style.space(8)` inside the bar's
    leading edge and centres it on the other axis; a bar widget is a
    `BarIconButton`, which is `Style.bar.iconSlot` long and as thick as the bar.
    So the rectangle is *not* a square at the origin, which is what MENU-2
    assumed and what UX-3 §3 is about.

    A user who moved the logo, replaced it, or took it out gets `None` - there
    is then no logo on the picture to cover, which is the truth.
    """
    if not _valid(rect) or position not in HORIZONTAL + VERTICAL:
        return None
    if not isinstance(sections, dict):
        return None
    leading = sections.get("left")
    if not isinstance(leading, list) or not leading:
        return None
    first = leading[0]
    if not isinstance(first, dict) or first.get("id") != LOGO_ID:
        return None
    style = style or BarStyle()
    thickness = rect["height"] if position in HORIZONTAL else rect["width"]
    if thickness < 1 or thickness > min(rect["width"], rect["height"]) * 64:
        return None
    # The margin plus one whole slot has to fit along the bar. A bar too short
    # for that is not a bar this model describes, and half a rectangle is worse
    # than none: the App would draw a mark over something we cannot name.
    along = rect["width"] if position in HORIZONTAL else rect["height"]
    if along < style.leading_space + style.icon_slot:
        return None
    if position in HORIZONTAL:
        return _rect(rect["x"] + style.leading_space, rect["y"], style.icon_slot, thickness)
    return _rect(rect["x"], rect["y"] + style.leading_space, thickness, style.icon_slot)


def measured_position(rect, logical_size):
    """Which edge this bar is on, read off the rectangle rather than guessed.

    `bar.position` is optional in `shell.json` and `parse_bar_layout` publishes
    `null` for it rather than assuming Omarchy's default - which is right for a
    layout projection, and useless here: a user who never wrote the key would
    get no marks at all. The bar itself answers it. A layer that spans the
    output's width is horizontal and its `y` says top or bottom; one that spans
    its height is vertical and its `x` says left or right. A rectangle that
    spans neither is not a bar this can name, and nothing is returned.
    """
    if not _valid(rect):
        return None
    width, height = logical_size["width"], logical_size["height"]
    if abs(rect["width"] - width) <= 1 and rect["height"] < height / 2:
        return "top" if rect["y"] <= height - rect["y"] - rect["height"] else "bottom"
    if abs(rect["height"] - height) <= 1 and rect["width"] < width / 2:
        return "left" if rect["x"] <= width - rect["x"] - rect["width"] else "right"
    return None


def plugin_rows(text) -> list | None:
    """The plugin's report, or None when it could not be read as one.

    `omarchy-shell` exits 0 even when the target or function is unknown and
    prints qs's complaint instead, so anything that is not a JSON list of
    objects is simply "no answer".
    """
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, list) or len(value) > 64:
        return None
    return [row for row in value if isinstance(row, dict)]


def plugin_logo(rows, output_name, logical_size):
    """`(answered, rect)` for one output from the plugin's report.

    `answered` is False when the plugin said nothing about this output (then
    the layer model decides). When it did answer, `rect` is its logo - or None
    when that bar has no visible `omarchy.menu`, or the rectangle is not inside
    the output, which is again the truth rather than a guess.
    """
    if not rows or not isinstance(output_name, str) or not output_name:
        return False, None
    row = next((row for row in rows if row.get("output") == output_name), None)
    if row is None:
        return False, None
    logo = row.get("logo")
    if not isinstance(logo, dict):
        return True, None
    try:
        rect = _rect(logo["x"], logo["y"], logo["width"], logo["height"])
    except (KeyError, TypeError, ValueError):
        return True, None
    if not _valid(rect):
        return True, None
    width, height = logical_size["width"], logical_size["height"]
    if rect["x"] < -1 or rect["y"] < -1 or rect["x"] + rect["width"] > width + 1 \
            or rect["y"] + rect["height"] > height + 1:
        return True, None
    return True, rect


def compose(*, output, logical_size, position, layer, sections, style: BarStyle | None = None):
    """The published `state.bar.geometry`, or `None` when there is no bar."""
    if layer is None or not isinstance(output, str) or not output:
        return None
    if not isinstance(logical_size, dict) or not _finite(logical_size.get("width"), 1, MAX_EDGE) \
            or not _finite(logical_size.get("height"), 1, MAX_EDGE):
        return None
    size = {"width": float(logical_size["width"]), "height": float(logical_size["height"])}
    where = position if position in HORIZONTAL + VERTICAL else measured_position(layer, size)
    if where is None:
        return None
    return {"output": output, "logical_size": size, "position": where, "bar": layer,
            "logo": logo_slot(layer, where, sections, style)}


def session_output(session):
    """The output a live session owns, from the session itself.

    Deliberately not a second `hyprctl monitors` call: the manager already
    holds the mode, the scale and the position it configured, and this runs on
    the daemon's own tick.
    """
    if session is None or getattr(session, "profile", None) is None:
        return None
    name = getattr(session, "output_name", None)
    if not isinstance(name, str) or not name:
        return None
    profile = session.profile
    scale = getattr(profile, "output_scale", None)
    mode = getattr(profile, "output_mode_pixels", None)
    width = getattr(mode, "width", None)
    height = getattr(mode, "height", None)
    if not _finite(scale, 0.1, 8) or not _finite(width, 1, MAX_EDGE) or not _finite(height, 1, MAX_EDGE):
        return None
    position = getattr(session, "position", (0, 0))
    if not isinstance(position, tuple) or len(position) != 2:
        position = (0, 0)
    if not all(_finite(value, -MAX_EDGE, MAX_EDGE) for value in position):
        position = (0, 0)
    return {"name": name, "origin": (float(position[0]), float(position[1])),
            "logical_size": {"width": math.ceil(width / scale), "height": math.ceil(height / scale)}}


class BarGeometry:
    """One throttled reading of the bar layer.

    The throttle is the daemon's two-second source refresh, the same as
    `BarModules`. `mark_dirty()` is what the compositor's `openlayer` /
    `closelayer omarchy-bar` events call: a bar that appeared, went away or
    moved is re-read on the next tick instead of up to two seconds later.
    """

    def __init__(self, *, runner=bounded_hyprctl, environment=graphical_environment,
                 clock=time.monotonic, throttle=2.0, home=None):
        self.runner = runner
        self.environment = environment
        self.clock = clock
        self.throttle = float(throttle)
        self.home = home
        self._layers = None
        self._read_at = None
        self._style = None
        self._style_at = None
        self._plugin = None
        self._plugin_at = None

    def mark_dirty(self) -> None:
        self._read_at = None
        self._plugin_at = None

    def plugin(self) -> list | None:
        """The plugin's report, on the same throttle as the layer.

        Only ever called for a live session (`snapshot` returns before it
        otherwise), so an idle host never spawns a `qs` for this.
        """
        now = self.clock()
        if self._plugin_at is not None and now - self._plugin_at < self.throttle:
            return self._plugin
        self._plugin_at = now
        try:
            environment = dict(self.environment())
            # `qs` is Qt: under LANG=C it logs its locale fallback on every run
            # (CORE-2 §3). `omarchy-shell` refuses to run without OMARCHY_PATH.
            environment.update({"OMARCHY_PATH": "/usr/share/omarchy", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
            value = plugin_rows(self.runner(PLUGIN_COMMAND, environment))
        except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
            value = None
        self._plugin = value
        return value

    def layers(self) -> dict | None:
        now = self.clock()
        if self._read_at is not None and now - self._read_at < self.throttle:
            return self._layers
        self._read_at = now
        try:
            value = json.loads(self.runner(("/usr/bin/hyprctl", "-j", "layers"), self.environment()))
        except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
            value = None
        self._layers = value if isinstance(value, dict) else None
        return self._layers

    def style(self) -> BarStyle:
        """The theme-derived leading margin and icon slot, on the same throttle.

        A theme change rewrites `shell.toml`, so this is re-read with the same
        cadence as the layer rather than once at start-up; a bar whose font
        scale changed moves its logo, and a mark that kept the old numbers
        would be wrong in exactly the way UX-3 §3 is about.
        """
        now = self.clock()
        if self._style is not None and self._style_at is not None \
                and now - self._style_at < self.throttle:
            return self._style
        self._style_at = now
        self._style = BarStyle.from_tokens(user_shell_tokens(self.home))
        return self._style

    def snapshot(self, session, position, sections) -> dict | None:
        """`state.bar.geometry` for the session that is live right now."""
        output = session_output(session)
        if output is None:
            return None
        document = self.layers()
        if document is None:
            return None
        layer = bar_layer(document, output["name"], origin=output["origin"])
        geometry = compose(output=output["name"], logical_size=output["logical_size"], position=position,
                           layer=layer, sections=sections, style=self.style())
        if geometry is None:
            return None
        answered, logo = plugin_logo(self.plugin(), output["name"], geometry["logical_size"])
        if answered:
            geometry["logo"] = logo
        return geometry
