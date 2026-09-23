"""One remote session: extend or takeover, over sunshine or vnc.

At most one session exists per host. It carries a single monotonic ``revision``
instead of a family of epochs and generations; a request that names a stale one
is refused with 409 and the client re-reads the session.

Every host mutation is journaled before it is issued, so the same
``restore()`` finishes the job for all four exits: normal release, heartbeat
timeout, a daemon restart that finds a leftover journal, and an operator
running ``omodachi-host remote recover``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import math
from pathlib import Path
import time
import uuid

from ..preferences import CUSTOM_BITRATE_KBPS, CUSTOM_FPS, QUALITIES
from ..protocol import IDLE_REMOTE_BAR as IDLE_BAR
from .errors import RemoteError
from .hyprland import (DPMS_WAKE_OPTIONS, Hyprland, OUTPUT_BOUND_DEVICES, OutputState, OWNED_NAME,
                        focus_workspace_lua, guard_lua, new_output_name, place)
from .journal import Journal
from .profile import (DesktopProfile, DesktopProfileError, EncoderLimits, PixelSize, PointRect,
                      PointSize, ProfileRequest, QualityBudget, ViewportProfilePlanner,
                      negotiate_codec, validate_presented_geometry)

MODES = ("extend", "takeover")
BACKENDS = ("sunshine", "vnc")
PLACEMENTS = ("right", "left", "above", "below")
# STREAM-1. Where a session's frame rate and bitrate come from. `host` is the
# host's own `quality` preference (a rate ceiling on what the client asked
# for, as before). The three names are the same table the host preference
# uses, chosen by the device instead, and `custom` is the device's own numbers
# inside `preferences.CUSTOM_*`. Every choice other than `host` overrides the
# host's default; none of them touches the pixel budget.
QUALITY_PRESETS = ("host",) + tuple(QUALITIES) + ("custom",)
LIVE = {"creating", "ready", "resizing", "stopping"}
KIND = "omodachi.remote.session"
DEFAULT_TTL = 30.0
# How many consecutive reconcile passes may fail to put the owned output back
# before the session gives up. A `hyprctl reload` settles in well under a
# second and the watchdog runs four times that often, so this is seconds of
# patience for something that is normally over before the first pass finishes.
RECONFIGURE_ATTEMPTS = 8


def _print_journal(entry):
    """systemd captures the daemon's stdout, so this *is* the journal line."""
    try:
        print(json.dumps({"omodachi": "remote", **entry}, separators=(",", ":"), default=str), flush=True)
    except (OSError, ValueError):
        pass


def _orientation_of(viewport) -> str:
    return "portrait" if viewport["height"] > viewport["width"] else "landscape"


@dataclass
class RemoteSession:
    id: str
    device_id: str
    backend: str
    mode: str
    state: str
    revision: int
    output_name: str
    journal_path: str
    created_at: float
    ttl_seconds: float
    placement: str = "right"
    lock_local_input: bool = False
    profile: DesktopProfile | None = None
    connection: dict | None = None
    request: dict | None = None
    position: tuple[int, int] = (0, 0)
    last_heartbeat: float = 0.0
    reason: str | None = None
    quality_preset: str = "host"
    adaptive: bool = False

    def to_dict(self):
        output = None
        if self.profile is not None:
            output = {"name": self.output_name, "mode_pixels": self.profile.output_mode_pixels.to_dict(),
                      "scale": self.profile.output_scale, "position": {"x": self.position[0], "y": self.position[1]}}
        return {"id": self.id, "device_id": self.device_id, "backend": self.backend, "mode": self.mode,
                "state": self.state, "revision": self.revision,
                "profile": self.profile.to_dict() if self.profile else None, "output": output,
                "connection": self.connection, "created_at": self.created_at,
                "last_heartbeat": self.last_heartbeat, "ttl_seconds": self.ttl_seconds,
                "placement": self.placement, "lock_local_input": self.lock_local_input,
                "journal_path": self.journal_path, "reason": self.reason,
                "quality": {"preset": self.quality_preset, "adaptive": self.adaptive}}


class RemoteManager:
    """Blocking host owner. The async service serializes every call into a thread."""

    def __init__(self, *, hyprland: Hyprland, journal_dir, encoder: EncoderLimits,
                 sunshine=None, vnc=None, bar_position=None, idle=None, shell=None, render_density=2.0,
                 clock=time.time, monotonic=time.monotonic, events=None, ttl_seconds=DEFAULT_TTL,
                 allow_resize=None, host_quality=None, host_backend=None, device_names=None):
        self.hyprland = hyprland
        self.journal_dir = Path(journal_dir)
        if not self.journal_dir.is_absolute():
            raise RemoteError("journal_path_invalid")
        self.encoder = encoder
        self.backends = {name: value for name, value in (("sunshine", sunshine), ("vnc", vnc)) if value is not None}
        self.bar_position = bar_position
        self.idle = idle
        # The Omarchy shell, only so a session can repair it after the crash a
        # takeover provokes in it. Nothing here depends on the shell existing.
        self.shell = shell
        self.render_density = render_density
        self.clock, self.monotonic, self.events = clock, monotonic, events
        self.default_ttl = ttl_seconds
        # Host policy: with dynamic resolution off, a session keeps the output
        # geometry it started with. Encoder-only changes stay allowed.
        self.allow_resize = allow_resize
        # Host policy: the `quality` preference is the host's own rate ceiling.
        # It answers `{fps, bitrate_kbps}`; the pixel budget stays the client's.
        self.host_quality = host_quality
        # Host policy: which backend a client gets when it does not name one.
        # The host says `sunshine`; a client that disagreed with that default
        # was the whole of Study 03's open question 5.
        self.host_backend = host_backend
        # Display names for the single session's owner. It is never an identity
        # and nothing authorizes on it: a second device is told *who* holds the
        # host rather than an opaque device_id it cannot act on.
        self.device_names = device_names
        self.session: RemoteSession | None = None
        # Consecutive reconcile passes that could not put the owned output back.
        self._reconfigure_failures = 0
        self._record: dict | None = None
        self._journal: Journal | None = None

    # --- helpers -----------------------------------------------------------

    def _publish(self, reason=None):
        if self.events is not None and self.session is not None:
            self.events(self.session.to_dict(), reason)

    def _advance(self, state, *, reason=None, publish=True):
        self.session.state = state
        self.session.revision += 1
        self.session.reason = reason
        if state in LIVE:
            self._record.update(state=state, revision=self.session.revision)
            self._journal.write(self._record)
        if publish:
            self._publish(reason)

    def _backend(self, name):
        backend = self.backends.get(name)
        if backend is None:
            raise RemoteError("backend_unsupported", 400)
        status = backend.status()
        if not status.get("available"):
            raise RemoteError(status.get("reason") or "backend_unavailable", 503)
        return backend

    def _physical(self):
        return [row for row in self.hyprland.monitors() if not OWNED_NAME.fullmatch(row.name)]

    def _plan(self, backend, name, request: ProfileRequest, *, density=None, preset="host") -> DesktopProfile:
        # Both backends are planned at the host's render density (REMOTE-6).
        # The owned output's mode is the device's pixels and its scale is the
        # device's scale, so the desktop on it is laid out in the same logical
        # units whichever backend is carrying it: a window, the Omarchy bar and
        # a point of type are the same size in both, and switching backends
        # mid-session changes nothing about the desktop.
        #
        # SPEC-E3 planned the vnc leg at scale 1 to dodge WayVNC 0.10.1's two
        # framebuffer sizes: it opens a client at the compositor's LOGICAL size
        # and corrects itself to the output's buffer pixels with a NewFBSize
        # rect one update in, and a client that cannot follow that is
        # disconnected by it. Paying for that with half the pixels made the
        # picture soft on a retina client, so the resize is now followed on the
        # client instead of being designed around here; the connection document
        # names both sizes (`initial_framebuffer_pixels` and
        # `framebuffer_pixels`) so the client knows the flip is coming.
        request = self._rated(request, preset)
        if density is None:
            density = self.render_density
        planner = ViewportProfilePlanner(name, encoder=self.encoder, render_density=density)
        profile = planner.plan(request, codec=self._codec(backend, request))
        if backend == "vnc":
            # WayVNC serves the framebuffer itself; its stream is the full mode.
            profile = replace(profile, stream_pixels=profile.output_mode_pixels)
        return profile

    def _codec(self, backend, request: ProfileRequest) -> str:
        """HEVC when the client decodes it and the live encoder serves it.

        Only the Sunshine leg encodes. Its codec list is the managed fork's own
        answer from the `desktop.status` that `_backend()` just read - never
        the installed `encoder_limits.codecs`, which describes what every
        encoder here must at least do (H.264), not what this one does today.
        A fork that does not say is an H.264 fork: it refuses any other codec
        at `desktop.prepare`.
        """
        if backend != "sunshine":
            return "h264"
        source = self.backends.get("sunshine")
        return negotiate_codec(request.decoder.codecs, getattr(source, "codecs", ("h264",)))

    @staticmethod
    def _preset(value, *, fallback="host"):
        if value is None:
            return fallback
        if not isinstance(value, str) or value not in QUALITY_PRESETS:
            raise RemoteError("invalid_request", 400)
        return value

    @staticmethod
    def _adaptive(value, *, fallback=False):
        if value is None:
            return fallback
        if type(value) is not bool:
            raise RemoteError("invalid_request", 400)
        return value

    def _rated(self, request: ProfileRequest, preset: str) -> ProfileRequest:
        """The frame rate and bitrate this session streams at (STREAM-1).

        `host` keeps the host's preference as a ceiling on what the client
        asked for. A named preset is the host's own table entry, whatever rate
        the client happened to send. `custom` is the client's numbers, which
        have to sit inside the published range. The pixel budget is the
        client's in every case, and the planner still clamps everything to the
        encoder's and the decoder's limits.
        """
        quality = request.quality
        if preset == "host":
            return self._host_limited(request)
        if preset == "custom":
            if quality.fps not in CUSTOM_FPS or not CUSTOM_BITRATE_KBPS[0] <= quality.bitrate_kbps <= CUSTOM_BITRATE_KBPS[1]:
                raise RemoteError("invalid_request", 400)
            return request
        fps, bitrate = QUALITIES[preset]
        return replace(request, quality=QualityBudget(quality.max_pixels, fps, bitrate))

    def _host_limited(self, request: ProfileRequest) -> ProfileRequest:
        """Cap the client's requested rates by the host's `quality` preference.

        The host's preference is a rate ceiling and nothing else: the pixel
        budget, viewport, orientation and decoder limits stay the client's, the
        same split the lease API had. `performance` is 30 fps / 8000 kbps,
        `balanced` 60 / 12000, `quality` 60 / 20000 (`preferences.QUALITIES`),
        and the client is told the same numbers by
        `GET /v1/preferences.profile_defaults.quality`, so it never has to
        recompute the mapping.
        """
        if self.host_quality is None:
            return request
        try:
            value = self.host_quality()
        except Exception:  # a preference store that cannot answer is not a session failure
            return request
        if not isinstance(value, dict):
            return request
        fps, bitrate = value.get("fps"), value.get("bitrate_kbps")
        if type(fps) is not int or type(bitrate) is not int or fps < 1 or bitrate < 1:
            return request
        quality = request.quality
        if fps >= quality.fps and bitrate >= quality.bitrate_kbps:
            return request
        return replace(request, quality=QualityBudget(quality.max_pixels, min(quality.fps, fps),
                                                      min(quality.bitrate_kbps, bitrate)))

    @staticmethod
    def _request(value) -> ProfileRequest:
        try:
            return ProfileRequest.from_dict(value)
        except (DesktopProfileError, TypeError, KeyError, AttributeError) as error:
            raise RemoteError(getattr(error, "code", "invalid_request"), 400) from None

    def _configure(self, profile, position, *, guards):
        owned, _ = self.hyprland.configure_output(self.session.output_name, profile.output_mode_pixels,
                                                  profile.output_scale, position, guards=guards)
        # An explicitly positioned output makes Hyprland re-pack the remaining
        # auto-positioned monitors. Pin any that moved back to their snapshot so
        # the user's physical layout is byte-identical before and after.
        baseline = [OutputState.from_dict(row) for row in self._record["baseline"]]
        for row in self._physical():
            original = next((value for value in baseline if value.name == row.name), None)
            if original is not None and not original.disabled and not row.disabled and (row.x, row.y) != (original.x, original.y):
                self.hyprland.apply_output(original)
        return owned

    # --- capabilities ------------------------------------------------------

    def capabilities(self):
        backends = {}
        for name in BACKENDS:
            backend = self.backends.get(name)
            backends[name] = backend.status() if backend is not None else {"available": False, "reason": "backend_not_installed"}
        return {"backends": backends, "modes": list(MODES), "placement_options": list(PLACEMENTS),
                "lock_local_input_supported": True, "default_backend": self.default_backend(backends),
                "encoder_limits": {**asdict(self.encoder), "codecs": list(self.encoder.codecs)}}

    def default_backend(self, backends=None):
        """What `POST /sessions` without a `backend` will use, said out loud.

        The host preference wins while it is usable; otherwise the first backend
        that reports itself available, in the order this host prefers them. When
        nothing is available the answer is still `sunshine` - a name for the
        client to show, not a promise that it will connect.
        """
        preferred = self.host_backend() if self.host_backend is not None else BACKENDS[0]
        if preferred not in BACKENDS:
            preferred = BACKENDS[0]
        order = (preferred,) + tuple(name for name in BACKENDS if name != preferred)
        if backends is None:
            backends = {}
            for name in order:
                backend = self.backends.get(name)
                backends[name] = backend.status() if backend is not None else {"available": False}
        for name in order:
            if backends.get(name, {}).get("available"):
                return name
        return preferred

    def _owner_detail(self, session):
        name = None
        if self.device_names is not None:
            try:
                name = self.device_names(session.device_id)
            except Exception:
                name = None
        return {"session_id": session.id, "owner_device_id": session.device_id,
                "owner_device_name": name if isinstance(name, str) and name else session.device_id,
                "mode": session.mode, "backend": session.backend,
                "started_at": int(session.created_at)}

    # --- lifecycle ---------------------------------------------------------

    def current(self):
        return self.session if self.session is not None and self.session.state in LIVE else None

    def get(self, session_id):
        if self.session is None or self.session.id != session_id:
            raise RemoteError("session_not_found", 404)
        return self.session

    def _check(self, session_id, expected_revision, *, states=("ready",)):
        session = self.get(session_id)
        if session.state not in states:
            raise RemoteError("session_not_ready", 409, state=session.state)
        if type(expected_revision) is not int or expected_revision != session.revision:
            raise RemoteError("stale_revision", 409, revision=session.revision)
        return session

    def create(self, device_id, payload):
        live = self.current()
        if live is not None:
            raise RemoteError("remote_session_exists", 409, **self._owner_detail(live))
        allowed = {"backend", "mode", "viewport_points", "orientation", "logical_long_edge",
                   "quality", "decoder", "placement", "lock_local_input", "ttl_seconds",
                   "quality_preset", "adaptive"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise RemoteError("invalid_request", 400)
        preset = self._preset(payload.get("quality_preset"))
        adaptive = self._adaptive(payload.get("adaptive"))
        backend_name = payload["backend"] if "backend" in payload else self.default_backend()
        mode = payload.get("mode", "extend")
        placement = payload.get("placement", "right")
        lock_input = payload.get("lock_local_input", False)
        ttl = payload.get("ttl_seconds", self.default_ttl)
        if backend_name not in BACKENDS or mode not in MODES or placement not in PLACEMENTS:
            raise RemoteError("invalid_request", 400)
        if type(lock_input) is not bool or type(ttl) not in {int, float} or isinstance(ttl, bool) or not 5 <= ttl <= 3600:
            raise RemoteError("invalid_request", 400)
        request = self._request({key: payload[key] for key in
                                 ("viewport_points", "orientation", "logical_long_edge", "quality", "decoder")
                                 if key in payload})
        backend = self._backend(backend_name)
        preflight = getattr(backend, "preflight", None)
        if preflight is not None:
            preflight(device_id)
        name = new_output_name()
        profile = self._plan(backend_name, name, request, preset=preset)
        journal = Journal(self.journal_dir / (name + ".json"))
        now = self.clock()
        self.session = RemoteSession(id="rs_" + uuid.uuid4().hex, device_id=device_id, backend=backend_name,
                                     mode=mode, state="creating", revision=1, output_name=name,
                                     journal_path=str(journal.path), created_at=now, ttl_seconds=float(ttl),
                                     placement=placement, lock_local_input=lock_input,
                                     request=request.to_dict(), last_heartbeat=self.monotonic(),
                                     quality_preset=preset, adaptive=adaptive)
        self._journal = journal
        self._reconfigure_failures = 0
        physical = self._physical()
        self._record = {"journal_version": 1, "kind": KIND, "instance_id": self.hyprland.instance_id,
                        "id": self.session.id, "device_id": device_id, "backend": backend_name, "mode": mode,
                        "state": "creating", "revision": 1, "created_at": now, "ttl_seconds": float(ttl),
                        "output_name": name, "placement": placement, "lock_local_input": lock_input,
                        "request": request.to_dict(), "profile": None, "position": None,
                        "quality_preset": preset, "adaptive": adaptive,
                        "baseline": [row.to_dict() for row in physical], "takeover": None, "prepared": False,
                        "device_outputs": [],
                        "idle_was_enabled": self.idle.enabled() if self.idle is not None else None}
        if mode == "takeover":
            self._record["takeover"] = {
                "workspaces": [{"id": row["id"], "monitor_id": row["monitor_id"], "windows": row["windows"],
                                "monitor_name": next((m.name for m in physical if m.monitor_id == row["monitor_id"]), None)}
                               for row in self.hyprland.workspaces()],
                "active": list(self.hyprland.active_workspace() or ()) or None,
                "moved": [], "disabled": [], "blanked": [], "inputs": []}
        if self.shell is not None:
            # Which crashes Quickshell had already recorded before this session
            # touched anything; a new one is this session's doing (HOST-1).
            self._record["shell_crashes"] = self.shell.crashes()
        journal.write(self._record)
        try:
            if self._record["idle_was_enabled"]:
                # A watched screen looks idle to the host; do not let Omarchy
                # blank and lock the machine underneath the stream.
                self.idle.set(False)
            self.hyprland.create_output(name)
            position = place(placement, physical, math.ceil(profile.logical_size.width),
                             math.ceil(profile.logical_size.height))
            self._record.update(position=list(position), profile=profile.to_dict())
            journal.write(self._record)
            self.session.position, self.session.profile = position, profile
            self._configure(profile, position, guards=[row for row in physical if not row.disabled])
            if mode == "takeover":
                self._enter_takeover(physical)
            self._map_absolute_inputs(backend_name)
            self._record["prepared"] = True
            journal.write(self._record)
            self.session.connection = backend.prepare(self.session, profile)
        except BaseException as error:
            self._advance("failed", reason=getattr(error, "code", "create_failed"), publish=False)
            self.restore(self._record, "create_failed")
            self._publish("create_failed")
            raise
        self._advance("ready", reason="created")
        return self.session

    def _enter_takeover(self, physical):
        record, takeover = self._record, self._record["takeover"]
        self._take_single_window_aspect()
        # Before anything goes dark: with the wake options on, the first
        # forwarded pointer move would light the screen back up between the
        # DPMS call below and the next reconcile.
        self._take_dpms_wake()
        active = takeover["active"]
        for row in takeover["workspaces"]:
            focus = active is not None and row["id"] == active[0]
            takeover["moved"].append(row["id"])
            self._journal.write(record)
            if not self.hyprland.move_workspace(row["id"], self.session.output_name, focus=focus) and focus:
                # The user's own workspace was empty and the compositor dropped
                # it; land the focus on the remote screen anyway.
                self.hyprland.eval(focus_workspace_lua(
                    next(iter(value["id"] for value in self.hyprland.workspaces()
                              if value["monitor_id"] == self._owned_monitor_id()), row["id"])))
        # HOST-1. The screens go dark by DPMS, not by being taken out of the
        # layout: removing an output kills Quickshell 0.3.1 outright, every
        # time, and no ordering on this side avoids it (see
        # `docs/issues/2026-09-21-quickshell-crash-on-takeover.md`). A screen
        # that is off but still there looks the same to the person in the room
        # and costs the compositor nothing it was not already paying.
        for row in physical:
            if row.disabled or not row.dpms:
                continue  # already dark, and not this session's doing
            entry = {"name": row.name, "method": None}
            takeover["blanked"].append(entry)
            self._journal.write(record)
            entry["method"] = self.hyprland.blank_output(row.name)
            if entry["method"] == "disabled":
                # A host without the DPMS dispatcher: the old way, and the
                # shell-crash fallback is what covers the consequences.
                takeover["disabled"].append(row.name)
            self._journal.write(record)
        if self.session.lock_local_input:
            for name in self.hyprland.input_devices():
                takeover["inputs"].append(name)
                self._journal.write(record)
                self.hyprland.set_device_enabled(name, False)
        if self.bar_position is not None:
            self.bar_position.committed(self._journal, self.session.request["viewport_points"])
            self._record = self._journal.read()

    def _map_absolute_inputs(self, backend_name):
        """Point the fork's touch and pen devices at the owned output.

        The fork normalizes touch and pen coordinates to the output it captures
        and hands the backend 0..1, so the compositor is what places that box.
        Without a per-device `output` Hyprland falls back to
        `input:touchdevice:output`, which is `[[Auto]]` on this host and does not
        know which of several screens the stream belongs to.

        The absolute pointer is deliberately left alone (INPUT-1): its
        coordinates are desktop-relative, and Hyprland resolves an absolute
        pointer against the bounding box of the enabled monitors, which is
        exactly what the fork targets.

        Both modes need it and it is written before the backend is prepared, so
        the mapping is already in Hyprland's config when the fork creates the
        devices for a connecting client. VNC has no emulated input devices, so a
        VNC session maps nothing; a session that switches to Sunshine later maps
        them then, and one that switches away keeps the mapping until it is
        restored — it names devices that only the fork creates.
        """
        if backend_name != "sunshine":
            return
        mapped = self._record.setdefault("device_outputs", [])
        for name in OUTPUT_BOUND_DEVICES:
            if name in mapped:
                continue
            mapped.append(name)
            self._journal.write(self._record)
            self.hyprland.set_device_output(name, self.session.output_name)

    # --- the one global config option a takeover owns ----------------------

    def _take_single_window_aspect(self):
        """Turn `layout:single_window_aspect_ratio` off for this takeover only.

        It is a Hyprland global with no per-output or per-workspace form
        (PERF-2 §8.2): on the owned output it squeezes the single remote window
        into a square and leaves the client staring at two margins. The current
        value goes into the journal before anything is written, so every restore
        path - release, heartbeat timeout, daemon restart, `remote recover` -
        puts back exactly what the user had. An extend session shares the
        physical screens with the user and does not touch it.
        """
        takeover = self._record["takeover"]
        try:
            x, y, explicit = self.hyprland.single_window_aspect_ratio()
        except RemoteError as error:
            takeover["single_window_aspect"] = {"state": "unavailable", "code": error.code}
            self._journal.write(self._record)
            return
        if (x, y) == (0.0, 0.0):
            takeover["single_window_aspect"] = {"state": "already_off", "original": [x, y], "was_set": explicit}
            self._journal.write(self._record)
            return
        takeover["single_window_aspect"] = {"state": "owned", "original": [x, y], "was_set": explicit}
        self._journal.write(self._record)
        # A comfort setting must not cost the session: a refusal here is
        # journaled as owned, and the restore finds the value already at the
        # user's own number and leaves it alone.
        try:
            self.hyprland.set_single_window_aspect_ratio(0.0, 0.0)
        except RemoteError:
            pass

    def _enforce_single_window_aspect(self):
        """Re-assert it after the host re-sourced its own configuration.

        `hyprctl reload` - which the host's monitor watcher issues on every
        output change - re-runs `hyprland.lua`, which sources the user's
        toggle directory and puts the option back. `configreloaded` brings the
        session here through `reconcile()`.
        """
        session = self.current()
        owned = ((self._record or {}).get("takeover") or {}).get("single_window_aspect") or {}
        if session is None or session.mode != "takeover" or owned.get("state") != "owned":
            return False
        try:
            x, y, _ = self.hyprland.single_window_aspect_ratio()
            if (x, y) == (0.0, 0.0):
                return False
            self.hyprland.set_single_window_aspect_ratio(0.0, 0.0)
        except RemoteError:
            return False
        return True

    def _restore_single_window_aspect(self, record, takeover):
        owned = takeover.get("single_window_aspect") or {}
        if owned.get("state") != "owned":
            return
        x, y = (float(value) for value in owned["original"])
        current = self.hyprland.single_window_aspect_ratio()
        if current[:2] != (0.0, 0.0):
            # Somebody put a value back while the session ran; it is theirs.
            return
        self.hyprland.set_single_window_aspect_ratio(x, y)
        if self.hyprland.single_window_aspect_ratio()[:2] != (x, y):
            raise RemoteError("single_window_aspect_restore_unconfirmed")

    # --- and the two that decide whether input wakes a dark screen ---------

    def _take_dpms_wake(self):
        """Stop the client's own input from lighting the screen in the room.

        `misc:mouse_move_enables_dpms` and `misc:key_press_enables_dpms` are
        both on in Omarchy's own input configuration, and a takeover feeds the
        remote client's pointer and keys to this compositor as real input. With
        them on, every tap, every touchpad move and every keystroke wakes all
        the screens Hyprland has, including the one this takeover just turned
        off; `reconcile()` turns it back off a moment later, so the panel in
        the room flashes once per interaction (HOST-1 §8.4).

        Both values go into the journal before either is written, so every
        restore path - release, heartbeat timeout, daemon restart, `remote
        recover` - puts back exactly what the user had. An extend session
        shares the screens with the user and leaves their wake behaviour alone.
        """
        takeover = self._record["takeover"]
        try:
            original = {name: list(self.hyprland.bool_option(name)) for name in DPMS_WAKE_OPTIONS}
        except RemoteError as error:
            takeover["dpms_wake"] = {"state": "unavailable", "code": error.code}
            self._journal.write(self._record)
            return
        if not any(value for value, _ in original.values()):
            takeover["dpms_wake"] = {"state": "already_off", "original": original}
            self._journal.write(self._record)
            return
        takeover["dpms_wake"] = {"state": "owned", "original": original}
        self._journal.write(self._record)
        for name, (value, _) in original.items():
            if not value:
                continue  # already off, and not this session's to put back on
            # A refusal must not cost the session: it is journaled as owned,
            # and the restore finds the value already at the user's own and
            # leaves it alone.
            try:
                self.hyprland.set_bool_option(name, False)
            except RemoteError:
                pass

    def _enforce_dpms_wake(self):
        """Re-assert them after the host re-sourced its own configuration.

        `hyprctl reload` - which the host's monitor watcher issues on every
        output change, so at least once per session - re-runs `hyprland.lua`
        and with it `/usr/share/omarchy/default/hypr/input.lua`, which sets
        both back to true. `configreloaded` brings the session here through
        `reconcile()`.
        """
        session = self.current()
        owned = ((self._record or {}).get("takeover") or {}).get("dpms_wake") or {}
        if session is None or session.mode != "takeover" or owned.get("state") != "owned":
            return []
        again = []
        for name, value in (owned.get("original") or {}).items():
            if name not in DPMS_WAKE_OPTIONS or not value[0]:
                continue
            try:
                if not self.hyprland.bool_option(name)[0]:
                    continue
                self.hyprland.set_bool_option(name, False)
            except RemoteError:
                continue  # reported by the next pass rather than failing a session
            again.append(name)
        return again

    def _restore_dpms_wake(self, record, takeover):
        owned = takeover.get("dpms_wake") or {}
        if owned.get("state") != "owned":
            return
        failures = []
        for name, value in (owned.get("original") or {}).items():
            if name not in DPMS_WAKE_OPTIONS or not value[0]:
                continue
            try:
                if self.hyprland.bool_option(name)[0]:
                    continue
                self.hyprland.set_bool_option(name, True)
            except RemoteError as error:
                failures.append(error.code)
        if failures:
            raise RemoteError(failures[0])

    def _owned_monitor_id(self):
        owned = next((row for row in self.hyprland.monitors() if row.name == self.session.output_name), None)
        if owned is None:
            raise RemoteError("owned_output_unavailable")
        return owned.monitor_id

    def resize(self, session_id, payload):
        allowed = {"expected_revision", "viewport_points", "orientation", "logical_long_edge", "quality", "decoder",
                   "quality_preset", "adaptive"}
        if not isinstance(payload, dict) or set(payload) - allowed or "expected_revision" not in payload:
            raise RemoteError("invalid_request", 400)
        session = self._check(session_id, payload["expected_revision"])
        # A resize that does not name a preset keeps the session's: a rotation
        # is not a change of mind about the picture's quality.
        preset = self._preset(payload.get("quality_preset"), fallback=session.quality_preset)
        adaptive = self._adaptive(payload.get("adaptive"), fallback=session.adaptive)
        merged = dict(session.request)
        merged.update({key: value for key, value in payload.items()
                       if key not in {"expected_revision", "quality_preset", "adaptive"}})
        request = self._request(merged)
        return self._reapply(session, request, session.backend, "resized", preset=preset, adaptive=adaptive)

    def switch_backend(self, session_id, payload):
        if not isinstance(payload, dict) or set(payload) != {"expected_revision", "backend"}:
            raise RemoteError("invalid_request", 400)
        session = self._check(session_id, payload["expected_revision"])
        if payload["backend"] not in BACKENDS:
            raise RemoteError("invalid_request", 400)
        if payload["backend"] == session.backend:
            return session
        return self._reapply(session, self._request(session.request), payload["backend"], "backend_changed")

    @staticmethod
    def _geometry(profile):
        return (profile.output_mode_pixels, profile.output_scale, profile.logical_size)

    def _reapply(self, session, request: ProfileRequest, backend_name, reason, *,
                 density=None, policy=True, recoverable=False, preset=None, adaptive=None):
        target = self._backend(backend_name)
        previous = self.backends.get(session.backend)
        preset = session.quality_preset if preset is None else preset
        adaptive = session.adaptive if adaptive is None else adaptive
        profile = self._plan(backend_name, session.output_name, request, density=density, preset=preset)
        if (policy and self.allow_resize is not None and session.profile is not None
                and self._geometry(profile) != self._geometry(session.profile) and not self.allow_resize()):
            raise RemoteError("dynamic_resolution_policy_denied", 403)
        self._advance("resizing", reason=reason)
        try:
            if previous is not None:
                previous.stop(session)
                if backend_name != session.backend:
                    previous.release(session)
            if not any(row.name == session.output_name for row in self.hyprland.monitors()):
                # The host removed our output from under the session; the same
                # path that re-plans it also puts it back.
                self.hyprland.create_output(session.output_name)
            physical = [row for row in self._physical() if not row.disabled]
            # The output is reconfigured, never destroyed: the windows on it and
            # their workspace stay exactly where they are across a rotation.
            self._configure(profile, session.position, guards=physical)
            session.profile, session.backend, session.request = profile, backend_name, request.to_dict()
            session.quality_preset, session.adaptive = preset, adaptive
            self._record.update(profile=profile.to_dict(), backend=backend_name, request=request.to_dict(),
                                quality_preset=preset, adaptive=adaptive)
            self._journal.write(self._record)
            self._map_absolute_inputs(backend_name)
            if session.mode == "takeover" and self.bar_position is not None:
                self.bar_position.committed(self._journal, request.viewport_points.to_dict())
                self._record = self._journal.read()
            self._record["prepared"] = True
            self._journal.write(self._record)
            session.connection = target.prepare(session, profile)
        except BaseException as error:
            if recoverable and isinstance(error, RemoteError):
                # A host-initiated rebuild that did not land is not a failed
                # session. Put it back to `ready` on the profile it already had
                # and let the caller decide whether to try again; the backend is
                # re-prepared by the next attempt. This *is* published: the
                # client was told `resizing` on the way in, and a client left
                # holding that while the session is alive and ready again is
                # the shape REMOTE-2 spent a whole item on.
                self._advance("ready", reason=reason)
                raise
            self._advance("failed", reason=getattr(error, "code", "resize_failed"), publish=False)
            self.restore(self._record, reason="resize_failed")
            self._publish("resize_failed")
            raise
        self._advance("ready", reason=reason)
        return session

    def heartbeat(self, session_id):
        session = self.get(session_id)
        if session.state not in LIVE:
            raise RemoteError("session_not_ready", 409, state=session.state)
        session.last_heartbeat = self.monotonic()
        return {"revision": session.revision, "state": session.state}

    def presented(self, session_id, payload):
        if not isinstance(payload, dict) or set(payload) != {"revision", "video_rect_points", "decoded_pixels"}:
            raise RemoteError("invalid_request", 400)
        session = self.get(session_id)
        if payload["revision"] != session.revision:
            return {"accepted": False, "reason": "stale_revision", "revision": session.revision}
        try:
            validate_presented_geometry(PointSize.from_dict(session.request["viewport_points"]),
                                        PointRect.from_dict(payload["video_rect_points"]),
                                        PixelSize.from_dict(payload["decoded_pixels"]))
        except (DesktopProfileError, TypeError, KeyError) as error:
            return {"accepted": False, "reason": getattr(error, "code", "invalid_request"), "revision": session.revision}
        return {"accepted": True, "reason": None, "revision": session.revision}

    # --- host-initiated display changes ------------------------------------

    def reconcile(self):
        """Adopt a display change the host made while a session was running.

        A pass that had to put something right says so on the daemon's journal.
        The quiet answer is `None` and is not logged, so this is one line per
        thing the host actually did, and the record of whether a session had to
        keep re-darkening a screen is readable after the fact.

        Changing the display configuration during a session is a legitimate
        user action, not an error: in takeover mode the official Omarchy
        Display panel is literally the UI the remote user is looking at, and
        `omarchy-hyprland-monitor-scaling` acts on whichever monitor is
        focused — which is ours. The host's own monitor watcher also issues
        `hyprctl reload`, which re-applies the catch-all
        `hl.monitor({output="", mode="preferred", position="auto", scale=...})`
        rule in `~/.config/hypr/monitors.lua` to *every* output, ours included.

        Two outcomes, and only the first touches the backend:

        * the owned output is *gone* -> re-create it and re-prepare the
          backend. That is the only change the stream cannot be carried
          through, and even then the session keeps its id;
        * anything else about it moved - mode, scale or position -> re-assert
          the planned values. Nothing is stopped, the revision does not move,
          and the client never notices.

        The owned output's geometry is the session's, planned from the
        client's viewport, and it is not adopted from the host mid-session
        (REMOTE-4; this reverses PERF-2 §1.4's second row, see
        `docs/specs/REMOTE-4-report.md`). Changing the display scale while a
        session is running is a thing a user can do by mis-clicking the
        official Display panel, and the only promise that matters is that it
        costs them nothing: the picture does not blink, the session does not
        end, and the scale they chose is on the screen in the room the moment
        the session does.

        Physical outputs are never fought with. Whatever the user left them at
        becomes the new snapshot, so the restore puts back what they last
        chose rather than what they had before the session.
        """
        # Named before the pass rather than after it: a pass that gave up ends
        # the session, and a journal line that then says `null` is the one line
        # that had to name it.
        before = self.session
        reason = self._reconcile()
        if reason is not None:
            _print_journal({"event": "reconciled", "reason": reason,
                            "session": before.id if before is not None else None})
        return reason

    def _reconcile(self):
        session = self.current()
        if session is None or session.state != "ready" or session.profile is None or self._record is None:
            return None
        reapplied = self._enforce_single_window_aspect()
        # Before the screens are darkened again, or the next forwarded event
        # undoes it on the way out of this pass.
        rewoken = self._enforce_dpms_wake()
        rows = self.hyprland.monitors()
        relit = self._enforce_blanking(rows)
        repinned = self._enforce_workspaces(rows)
        adopted = self._adopt_physical(rows)
        owned = next((row for row in rows if row.name == session.output_name), None)
        planned, position = session.profile, tuple(session.position)
        if owned is None:
            return self._host_reconfigured(session)
        if ((owned.width, owned.height) != (planned.output_mode_pixels.width, planned.output_mode_pixels.height)
                or abs(owned.scale - planned.output_scale) > 1e-6
                or (owned.x, owned.y) != position):
            return self._repin(session, planned, position)
        self._reconfigure_failures = 0
        if adopted:
            return "baseline_updated"
        if repinned:
            return "workspaces_repinned"
        if relit:
            return "physical_blanking_reapplied"
        if reapplied:
            return "single_window_aspect_reapplied"
        return "dpms_wake_reapplied" if rewoken else None

    def _enforce_blanking(self, rows):
        """A screen this takeover turned off that came back on is turned off again.

        Hyprland brings a DPMS-off monitor back for anything that counts as
        activity, and a takeover's whole promise is that the screen in the room
        stays dark. This is the same undertaking as re-asserting the owned
        output's mode: the session owns the physical screens for its lifetime,
        and gives them back exactly as it found them when it ends.
        """
        takeover = (self._record or {}).get("takeover") or {}
        names = [row["name"] for row in (takeover.get("blanked") or []) if row.get("method") == "dpms"]
        relit = []
        for row in rows:
            if row.name in names and row.dpms:
                try:
                    self.hyprland.set_output_dpms(row.name, False)
                    relit.append(row.name)
                except RemoteError:
                    pass  # reported by the next pass rather than failing a session
        return relit

    def _enforce_workspaces(self, rows):
        """Put back any workspace the host's own rules pulled onto a dark screen.

        `hyprctl reload` - which Omarchy's monitor watcher issues whenever an
        output appears or disappears, so once per session at least - re-applies
        the user's `workspace_rule` bindings, and on this host every workspace
        is pinned to the laptop panel (`~/.config/hypr/control-panel.lua`).
        While a takeover merely darkens that panel instead of removing it, the
        pull now succeeds and the user would find their windows on the one
        screen nobody can see. Only the workspaces this session moved are moved
        again, and only off a screen this session darkened.
        """
        takeover = (self._record or {}).get("takeover") or {}
        blanked = {row["name"] for row in (takeover.get("blanked") or []) if isinstance(row.get("name"), str)}
        if not blanked or self.session is None:
            return []
        dark = {row.monitor_id for row in rows if row.name in blanked}
        moved = set(takeover.get("moved") or [])
        back = []
        for row in self.hyprland.workspaces():
            if row["id"] not in moved or row["monitor_id"] not in dark:
                continue
            try:
                if self.hyprland.move_workspace(row["id"], self.session.output_name):
                    back.append(row["id"])
            except RemoteError:
                pass  # the next pass tries again rather than failing a session
        return back

    def _repin(self, session, planned, position):
        """Put the session's own output back the way the session planned it.

        A `hyprctl reload` is in flight for as long as the compositor takes to
        re-read the user's files, and a write issued into the middle of one is
        simply lost. That is not a failed session, it is a pass that has to run
        again: the reason is journaled, the next tick tries again, and only
        `RECONFIGURE_ATTEMPTS` passes in a row that all fail end anything.
        """
        try:
            self._configure(planned, position, guards=())
        except RemoteError as error:
            return self._reconfigure_failed(session, error)
        self._reconfigure_failures = 0
        return "owned_output_repinned"

    def _host_reconfigured(self, session):
        """The owned output itself is gone: rebuild it and re-prepare the backend.

        The one host change a stream cannot be carried through. The session id
        does not move - the client re-dials the same session rather than
        landing back on the entry screen - and `host_reconfigured` is what
        tells it to.
        """
        try:
            self._reapply(session, self._request(session.request), session.backend,
                          "host_reconfigured", density=session.profile.output_scale,
                          policy=False, recoverable=True)
        except RemoteError as error:
            return self._reconfigure_failed(session, error)
        self._reconfigure_failures = 0
        return "host_reconfigured"

    def _reconfigure_failed(self, session, error):
        """One failed pass, or the last one this session is going to get."""
        self._reconfigure_failures += 1
        if self._reconfigure_failures < RECONFIGURE_ATTEMPTS:
            return "host_reconfigure_retry"
        self._advance("failed", reason=error.code, publish=False)
        self.restore(self._record, reason=error.code)
        self._publish(error.code)
        return error.code

    # Position is compositor layout bookkeeping — our own placement re-packs it
    # — so it is never adopted; the restore puts the snapshot's coordinates
    # back. Everything the user can actually choose is.
    ADOPTED = ("width", "height", "refresh_hz", "scale", "transform")

    def _adopt_physical(self, rows):
        """Record the user's current physical geometry as the new snapshot."""
        baseline = self._record.get("baseline") or []
        changed = False
        for row in rows:
            if OWNED_NAME.fullmatch(row.name):
                continue
            snapshot = next((value for value in baseline if value["name"] == row.name), None)
            # A disabled output keeps reporting its geometry on this Hyprland;
            # a 0x0 or scale-0 readback is the compositor having nothing to say,
            # and is never written over a good snapshot.
            if snapshot is None or not row.width or not row.height or not row.scale:
                continue
            live = {key: getattr(row, key) for key in self.ADOPTED}
            if all(snapshot[key] == value for key, value in live.items()):
                continue
            snapshot.update(live)
            changed = True
        if changed:
            self._journal.write(self._record)
        return changed

    def release(self, session_id, reason="released"):
        if self.session is None or self.session.id != session_id:
            return {"released": True, "errors": []}
        if self.session.state == "released":
            return {"released": True, "errors": []}
        self._advance("stopping", reason=reason, publish=False)
        errors = self.restore(self._record, reason)
        self._advance("failed" if errors else "released", reason=reason)
        return {"released": not errors, "errors": errors}

    def maintain(self):
        """Expire a session whose client stopped sending heartbeats."""
        session = self.current()
        if session is None or session.state != "ready":
            return None
        if self.monotonic() - session.last_heartbeat <= session.ttl_seconds:
            return None
        return self.release(session.id, "heartbeat_timeout")

    # --- the one restore path ---------------------------------------------

    def restore(self, record, reason="released"):
        """Undo every journaled mutation, in reverse order, best effort.

        Each step reports its own failure and the next step still runs, so a
        stuck backend cannot leave the physical screen dark.
        """
        errors = []
        takeover = record.get("takeover") or {}
        steps = [("backend", lambda: self._restore_backend(record)),
                 ("dpms", lambda: self._restore_dpms(record, takeover)),
                 ("physical", lambda: self._restore_physical(record, takeover)),
                 ("workspaces", lambda: self._restore_workspaces(record, takeover)),
                 ("input", lambda: self._restore_input(record, takeover)),
                 ("device_output", lambda: self._restore_device_outputs(record)),
                 ("dpms_wake", lambda: self._restore_dpms_wake(record, takeover)),
                 ("single_window_aspect", lambda: self._restore_single_window_aspect(record, takeover)),
                 ("idle", lambda: self._restore_idle(record)),
                 ("bar", lambda: self._restore_bar(record)),
                 ("output", lambda: self._restore_output(record))]
        for name, step in steps:
            try:
                step()
            except Exception as error:  # every step is independent
                errors.append({"step": name, "code": getattr(error, "code", type(error).__name__)})
        if not errors:
            self._journal_for(record).unlink()
        return errors

    def _journal_for(self, record):
        return Journal(self.journal_dir / (record["output_name"] + ".json"))

    def _restore_backend(self, record):
        backend = self.backends.get(record.get("backend"))
        if backend is None or not record.get("prepared"):
            return
        session = self.session if self.session is not None and self.session.id == record["id"] else RemoteSession(
            id=record["id"], device_id=record["device_id"], backend=record["backend"], mode=record["mode"],
            state="stopping", revision=record.get("revision", 1), output_name=record["output_name"],
            journal_path=str(self._journal_for(record).path), created_at=record.get("created_at", 0.0), ttl_seconds=record.get("ttl_seconds", self.default_ttl))
        if backend.stop(session):
            backend.release(session)

    def _restore_dpms(self, record, takeover):
        """Turn back on every screen this session turned off with DPMS.

        The output was never taken out of the layout, so there is nothing to
        re-apply: `_restore_physical` still puts back any geometry the host
        changed underneath, and this is the light.
        """
        failures = []
        for row in takeover.get("blanked") or []:
            if row.get("method") != "dpms" or not isinstance(row.get("name"), str):
                continue
            try:
                self.hyprland.set_output_dpms(row["name"], True)
            except RemoteError as error:
                failures.append(error.code)
        if failures:
            raise RemoteError(failures[0])

    def _restore_physical(self, record, takeover):
        """Put every physical output back to its snapshot, not only the ones we disabled.

        Turning the screens off in takeover is not the only way a session moves
        them. `hyprctl reload` — which the host's own monitor watcher issues
        whenever an output appears or disappears, and creating the session's
        output does exactly that — re-applies the user's catch-all monitor rule
        to every screen, and an explicitly positioned output makes Hyprland
        re-pack the auto-positioned ones. Extend sessions have no `disabled`
        list at all, so those changes used to be left on the host for good.

        The snapshot is what the user last chose: `reconcile()` adopts a
        deliberate change into it while the session runs, so this restores their
        intent rather than a stale copy of it.
        """
        live = {row.name: row for row in self.hyprland.monitors()}
        failures = []
        for value in record["baseline"]:
            snapshot = OutputState.from_dict(value)
            row = live.get(snapshot.name)
            # An output that is no longer attached is not ours to put back.
            if row is None or row == replace(snapshot, monitor_id=row.monitor_id, dpms=row.dpms):
                continue
            try:
                self.hyprland.apply_output(snapshot)
            except RemoteError as error:
                failures.append(error.code)
        if failures:
            raise RemoteError(failures[0])

    def _restore_workspaces(self, record, takeover):
        moved = set(takeover.get("moved", []))
        if not moved:
            return
        failures = []
        active = takeover.get("active")
        for row in takeover.get("workspaces", []):
            if row["id"] not in moved or not row.get("monitor_name"):
                continue
            try:
                self.hyprland.move_workspace(row["id"], row["monitor_name"],
                                             focus=bool(active) and row["id"] == active[0])
            except RemoteError as error:
                failures.append(error.code)
        if failures:
            raise RemoteError(failures[0])

    def _restore_input(self, record, takeover):
        for name in takeover.get("inputs", []):
            self.hyprland.set_device_enabled(name, True)

    def _restore_device_outputs(self, record):
        """Unbind every device this session pointed at its output.

        An empty `output` is Hyprland's own default — the mapping is dropped and
        the device goes back to the whole layout. Journals written before
        INPUT-1 have no list and nothing to undo.
        """
        for name in record.get("device_outputs") or []:
            self.hyprland.set_device_output(name, None)

    def _restore_idle(self, record):
        if self.idle is not None and record.get("idle_was_enabled"):
            self.idle.set(True)

    def _restore_bar(self, record):
        if self.bar_position is None or record.get("official_bar_position") is None:
            return
        journal = self._journal_for(record)
        # The helper compares the live position with the value it wrote; a user
        # change during the session is reported as an override and kept.
        self.bar_position.restore(journal)
        self._record = journal.read() or record

    def _restore_output(self, record):
        name = record["output_name"]
        if not any(row.name == name for row in self.hyprland.monitors()):
            return
        self._evacuate(name)
        # What must be confirmed is that removing our output took nothing else
        # with it. A screen the user unplugged during the session was already
        # gone before this ran and is not ours to answer for.
        before = {row.name for row in self.hyprland.monitors() if row.name != name}
        self.hyprland.destroy_output(name)
        remaining = {row.name for row in self.hyprland.monitors()}
        if before - remaining:
            raise RemoteError("owned_output_removal_unconfirmed")

    def _evacuate(self, name):
        """Move whole occupied workspaces off the owned output before removing it."""
        monitors = self.hyprland.monitors()
        owned = next((row for row in monitors if row.name == name), None)
        destination = next((row for row in monitors if row.name != name and not row.disabled), None)
        if owned is None or destination is None:
            return
        for row in self.hyprland.workspaces():
            if row["monitor_id"] == owned.monitor_id and row["windows"]:
                self.hyprland.move_workspace(row["id"], destination.name)

    # --- startup and operator recovery ------------------------------------

    def recover(self, *, orphans: bool = False):
        """Finish every leftover journal in *this* manager's directory.

        CORE-2 §2. An `OMODACHI-*` output this manager has no journal for is
        not an orphan by default: it is just as likely to belong to another
        RemoteManager on the same compositor - a side-by-side test daemon with
        its own journal directory starting up while the installed daemon holds
        a live session (HOST-1 §7.3 ended somebody's session exactly that way;
        REMOTE-4 §3.0 found the same trap). So an unknown output is reported
        (`unowned_outputs`, and one journal line) and left where it is.
        `orphans=True` - `omodachi-host remote recover --orphans`, an operator
        who has looked - is the old behaviour: move its workspaces off and
        destroy it.
        """
        results = []
        for path in sorted(self.journal_dir.glob("OMODACHI-*.json")):
            record = Journal(path).read()
            if not record or record.get("kind") != KIND:
                continue
            record.setdefault("journal_path", str(path))
            errors = self.restore(record, "daemon_restarted")
            results.append({"session_id": record.get("id"), "output": record.get("output_name"), "errors": errors})
        if self.session is not None and self.session.state in LIVE:
            self.session.state, self.session.reason = "released", "recovered"
            self._publish("recovered")
        self.session = None
        known = {row.get("output_name") for row in results}
        unknown = [row.name for row in self.hyprland.monitors()
                   if OWNED_NAME.fullmatch(row.name) and row.name not in known]
        if not orphans:
            if unknown:
                _print_journal({"event": "unowned_output_left", "outputs": unknown,
                                "journal_dir": str(self.journal_dir),
                                "hint": "omodachi-host remote recover --orphans"})
            return {"recovered": results, "orphan_outputs": [], "unowned_outputs": unknown}
        for name in unknown:
            self._evacuate(name)
            self.hyprland.destroy_output(name)
        if unknown:
            _print_journal({"event": "orphan_outputs_removed", "outputs": unknown})
            # An owned output without a journal means an earlier process died
            # mid-takeover. Undo the only other state it could have left.
            for row in self.hyprland.monitors():
                if row.disabled and not OWNED_NAME.fullmatch(row.name):
                    results.append({"session_id": None, "output": row.name, "errors": [{"step": "physical",
                                    "code": "disabled_output_left_for_operator"}]})
        return {"recovered": results, "orphan_outputs": unknown, "unowned_outputs": []}

    # --- projections -------------------------------------------------------

    def watch_shell(self):
        """Repair the Omarchy shell once, if it died under this session.

        Quickshell's own crash handler restarts the configuration inside the
        crashed process, so the shell answers again while being unusable: every
        IpcHandler is registered twice, the menu's Apps list is empty and the
        polkit agent is unreachable (HOST-1). The user cannot see any of that -
        their screen is off and they are on the iPad - so the session repairs it
        instead of leaving them a broken desktop to come back to. Once per
        session: a shell that keeps crashing is not something to keep restarting.
        """
        session, record = self.session, self._record
        if self.shell is None or session is None or session.state not in LIVE or record is None:
            return None
        baseline = record.get("shell_crashes")
        if baseline is None or record.get("shell_restarted") is not None:
            return None
        crashes = [name for name in self.shell.crashes() if name not in baseline]
        if not crashes:
            return None
        restarted = self.shell.restart()
        record["shell_restarted"] = {"crashes": crashes, "restarted": restarted, "at": self.clock()}
        self._journal.write(record)
        _print_journal({"event": "shell_restarted", "session": session.id, "mode": session.mode,
                        "crashes": crashes, "restarted": restarted})
        # The session itself did not change, so the revision does not move; the
        # event is there to tell the client why the host blinked.
        self._publish("shell_restarted")
        return record["shell_restarted"]

    def state_projection(self):
        session = self.current()
        if session is None:
            return {"session_id": None, "state": "offline", "mode": None, "backend": None, "revision": 0}
        return {"session_id": session.id, "state": session.state, "mode": session.mode,
                "backend": session.backend, "revision": session.revision}

    def bar_projection(self):
        session = self.current()
        if session is None or session.profile is None:
            return dict(IDLE_BAR)
        monitors = {row.monitor_id: row for row in self.hyprland.monitors()}
        owned = next((row for row in monitors.values() if row.name == session.output_name), None)
        if owned is None:
            return dict(IDLE_BAR)
        active = self.hyprland.active_workspace()
        viewport = session.request["viewport_points"]
        rows = [{"id": row["id"], "monitor": monitors[row["monitor_id"]].name if row["monitor_id"] in monitors else None,
                 "windows": row["windows"], "active": bool(active) and row["id"] == active[0],
                 "remote": row["monitor_id"] == owned.monitor_id}
                for row in self.hyprland.workspaces() if row["id"] > 0]
        return {"active": True, "session_id": session.id, "output_name": session.output_name,
                "viewport": viewport, "orientation": _orientation_of(viewport),
                "logical_size": {"width": owned.width / owned.scale, "height": owned.height / owned.scale},
                "revision": str(session.revision), "workspaces": rows}
