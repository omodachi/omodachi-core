"""REMOTE-SAFE-1. How much of each end of the host bar the device's corners hide.

In both modes (Extend, and since REMOTE-SAFE-1b Take over) the owned output is
the device's exact shape and the picture is edge to edge (A-58), so the host bar runs into the display's rounded corners:
on a real iPhone `omarchy.power` sat 7 logical px from the bottom edge and was
visibly cut. The device knows its own corners; the host knows its own pixels.

* The App measures, per model and orientation, how far *along* each edge the
  corners reach into a bar of the host bar's thickness, and reports it in its
  own points as ``bar_occlusion_points = {top, bottom, left, right}`` with the
  viewport it already reports (create and resize).
* This module turns that into the owned output's logical pixels, which is the
  only unit the Omarchy plugin can act on, and ``RemoteManager.bar_projection``
  publishes it on ``state.remote_bar.bar_insets`` - the stream
  ``omodachi-host plugin-watch`` already forwards.

``top``/``bottom`` are the two ends of a *vertical* bar, ``left``/``right`` the
two ends of a *horizontal* one. All four are sent because the host, not the
App, decides where its bar is.
"""
from __future__ import annotations

import math

from .errors import RemoteError

EDGES = ("top", "bottom", "left", "right")
#: No display corner is anywhere near this, in points; a larger number is a
#: client bug, not a device.
MAX_POINTS = 256.0
#: No end inset may take more than this fraction of the bar's length. A bar
#: whose two ends were pushed past each other would draw nothing at all.
MAX_FRACTION = 0.25


def parse_occlusion(value):
    """`bar_occlusion_points` as the App sent it, or `RemoteError(400)`.

    `None` (the field absent, or an explicit null) is "this device did not say",
    which is every client from before REMOTE-SAFE-1 and every home-button
    device; it is not an error.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(EDGES):
        raise RemoteError("invalid_request", 400)
    result = {}
    for edge in EDGES:
        number = value[edge]
        if type(number) not in (int, float) or isinstance(number, bool) or not math.isfinite(number) \
                or not 0 <= number <= MAX_POINTS:
            raise RemoteError("invalid_request", 400)
        result[edge] = float(number)
    return result


def to_output_pixels(occlusion, viewport, logical_size):
    """Points on the device -> logical px of the owned output, per end.

    The picture is the whole output aspect-fitted into the viewport (A-58: the
    output is planned to the viewport's own aspect, within 1 %). So one point is
    `1/s` logical px with `s = min(vw/lw, vh/lh)`, and whatever letterbox the
    1 % leaves on an axis is already that far away from the corner, so it is
    subtracted before converting. Rounded *up*: half a pixel under a corner is
    still under the corner.

    `None` whenever there is nothing honest to publish - no occlusion, or a
    geometry that cannot be a picture.
    """
    if not occlusion or not isinstance(viewport, dict) or not isinstance(logical_size, dict):
        return None
    try:
        vw, vh = float(viewport["width"]), float(viewport["height"])
        lw, lh = float(logical_size["width"]), float(logical_size["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(x) and x > 0 for x in (vw, vh, lw, lh)):
        return None
    scale = min(vw / lw, vh / lh)
    gap_x = max(0.0, (vw - lw * scale) / 2)
    gap_y = max(0.0, (vh - lh * scale) / 2)
    result = {}
    for edge in EDGES:
        gap, length = (gap_y, lh) if edge in ("top", "bottom") else (gap_x, lw)
        pixels = math.ceil(max(0.0, float(occlusion.get(edge, 0.0)) - gap) / scale - 1e-9)
        result[edge] = int(min(max(0, pixels), math.floor(length * MAX_FRACTION)))
    return result
