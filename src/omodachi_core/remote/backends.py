"""The two frame sources behind one four-call interface.

    status()                  -> {available, reason}
    prepare(session, profile) -> connection dict for the client
    stop(session)             -> to a real stop, not to a request being sent
    release(session)          -> give the reservation back

Neither adapter observes frames. The fork's own `media.get` counters and
WayVNC's `output-list` are read only to confirm that the source is live.
"""
from __future__ import annotations

from pathlib import Path
import re
import socket
import subprocess
import uuid

from .errors import RemoteError

SUNSHINE_HTTPS_PORT = 47984
SUNSHINE_APP_NAME = "Omodachi Desktop"
# The one authenticated, fingerprint-pinned route a client reaches WayVNC over.
VNC_BRIDGE_PATH = "/v1/remote/sessions/{session_id}/vnc"

# The fork's asset root is a *build* input: `SUNSHINE_ASSETS_DIR` is compiled in
# at configure time, so the tree has to sit beside the binary the unit actually
# starts, in the same per-commit directory it was configured for. When it is
# not there the fork logs a shader compile error per file and silently falls
# back to software encoding - a working stream that costs the CPU instead of the
# GPU (PERF-3). The shader directory is what `graphics.cpp` opens, so that is
# what this probes.
SUNSHINE_UNIT = "app-dev.lizardbyte.app.Sunshine.service"
SUNSHINE_INSTALL_ROOT = ".local/share/omodachi/sunshine"
SUNSHINE_SHADERS = "assets/shaders/opengl"
_EXEC_PATH = re.compile(r"(?:^|[{;]\s*)path=(\S+)")
# STREAM-1. What the managed fork may say it encodes, in `desktop.status`.
KNOWN_CODECS = ("h264", "hevc", "av1")


def _systemctl(unit):
    result = subprocess.run(["systemctl", "--user", "show", "--property=ExecStart",
                             "--value", unit], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=5, check=False)
    return result.stdout if result.returncode == 0 else ""


def managed_sunshine_assets(*, runner=_systemctl, unit=SUNSHINE_UNIT) -> dict:
    """Where the managed fork's assets have to be, and whether they are there.

    The unit's resolved `ExecStart` is the only authority on which build is
    running - the drop-in that points at it is the user's file and is never
    written here. Result: `{executable, assets, present, reason}`, with
    `present` left `None` when the unit cannot be read, because "we could not
    look" is not "it is missing".
    """
    try:
        value = runner(unit)
    except (OSError, ValueError, subprocess.SubprocessError):
        value = ""
    match = _EXEC_PATH.search(value or "")
    if not match:
        return {"executable": None, "assets": None, "present": None,
                "reason": "sunshine_unit_unreadable"}
    executable = Path(match.group(1))
    shaders = executable.parent / SUNSHINE_SHADERS
    try:
        present = shaders.is_dir() and any(shaders.iterdir())
    except OSError:
        present = False
    return {"executable": str(executable), "assets": str(executable.parent / "assets"),
            "present": present, "reason": None if present else "sunshine_assets_missing"}


def opening_pixels(profile) -> dict:
    """The size WayVNC 0.10.1 announces in ServerInit: the logical size.

    It is an opening state, not the steady one. One update in, WayVNC sends a
    NewFBSize rect for the output's buffer pixels and serves those for the rest
    of the session (`docs/wayvnc.md`). With scale 2 the two are different
    numbers for the same screen; with scale 1 they are the same number.
    """
    return {"width": round(profile.logical_size.width), "height": round(profile.logical_size.height)}


def served_pixels(profile) -> dict:
    """The framebuffer WayVNC settles on: the owned output's buffer pixels."""
    return profile.output_mode_pixels.to_dict()


def lan_address() -> str | None:
    """The address this host would use to reach the LAN, without sending anything."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        value = probe.getsockname()[0]
        return value if isinstance(value, str) and not value.startswith("127.") else None
    except OSError:
        return None
    finally:
        probe.close()


class SunshineBackend:
    """The managed fork, driven over its private JSONL control socket.

    The wire shape is the fork's frozen IPC v3 contract (`docs/IPC-v3.md`,
    `src/managed_desktop_control.cpp`): it validates the exact field sets of
    `lease` and `identity`. Core has one monotonic session revision, so both of
    the fork's generation counters are that revision and the lease epoch is 1;
    they are wire fields here and nowhere else in core.
    """

    name = "sunshine"

    def __init__(self, ipc, *, certificate_resolver=None, address=lan_address, assets=managed_sunshine_assets):
        self.ipc, self.certificate_resolver, self.address = ipc, certificate_resolver, address
        self.assets = assets
        # What the fork said it encodes the last time it was asked. `status()`
        # runs right before every plan (`RemoteManager._backend`), so this is
        # never older than the session being planned.
        self.codecs: tuple[str, ...] = ("h264",)

    @staticmethod
    def served_codecs(result) -> tuple[str, ...]:
        """The codecs a `desktop.status` answer says this fork serves.

        STREAM-1 asks the fork for `encoders: ["h264", "hevc"]`. A fork that
        does not carry the field is the one that validates `codec == "h264"`
        at `desktop.prepare` and at the RTSP launch, so its answer is H.264 -
        not "whatever the encoder probe found": the fork's log saying
        `Found HEVC encoder: hevc_vaapi` does not make the fork accept an
        HEVC profile. Unknown names are dropped; H.264 is always the baseline.
        """
        value = result.get("encoders") if isinstance(result, dict) else None
        if not isinstance(value, list):
            return ("h264",)
        named = [codec for codec in KNOWN_CODECS if codec in value]
        return tuple(named) if "h264" in named else ("h264",)

    def status(self):
        try:
            result = self.ipc.request("desktop.status")
        except (RemoteError, OSError):
            self.codecs = ("h264",)
            return {"available": False, "reason": "sunshine_control_unavailable"}
        self.codecs = self.served_codecs(result)
        if result.get("available") is not True:
            return {"available": False, "reason": "sunshine_desktop_unavailable"}
        # The fork answers for its own capture path; whether it will do it on
        # the GPU is decided by a directory next to the binary, which the fork
        # never complains about at this level. Say so here or nobody finds out
        # until the encoder in this same result reads `libx264`.
        try:
            assets = self.assets()
        except Exception:
            assets = {"present": None}
        # A unit we could not read is not a missing asset tree, and this field
        # is not the place to guess.
        reason = "sunshine_assets_missing" if assets.get("present") is False else None
        return {"available": True, "reason": reason, "backend": result.get("backend"),
                "encoder": result.get("encoder"), "codecs": list(self.codecs)}

    def preflight(self, device_id):
        """Refuse before any host mutation when this device never paired."""
        self._fingerprint(device_id)

    def _fingerprint(self, device_id):
        value = self.certificate_resolver(device_id) if self.certificate_resolver else None
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise RemoteError("media_pairing_required", 409)
        return value

    def _lease(self, session):
        return {"lease_id": session.id, "lease_epoch": 1, "owner_device_id": session.device_id,
                "client_cert_sha256": self._fingerprint(session.device_id)}

    def _call(self, session, operation, **fields):
        lease = self._lease(session)
        result = self.ipc.request(operation, lease=lease, **fields)
        if result.get("lease") != lease:
            raise RemoteError("sunshine_binding_mismatch")
        return result

    def prepare(self, session, profile):
        # claim is a same-UID reservation: it refuses unrelated active streams
        # and never stops another client's session or reconfigures a monitor.
        self._call(session, "desktop.claim", output_id=session.output_name)
        identity = {"lease_id": session.id, "lease_epoch": 1,
                    "transition_id": "t" + uuid.uuid4().hex,
                    "geometry_epoch": session.revision, "connection_generation": session.revision}
        result = self._call(session, "desktop.prepare", identity=identity,
                            output_id=session.output_name, profile=profile.to_dict())
        if (result.get("configured_output_id") != session.output_name
                or result.get("session_count") != 0 or result.get("prepared") is not True):
            raise RemoteError("sunshine_prepare_unconfirmed")
        return {"backend": "sunshine", "host": self.address(), "https_port": SUNSHINE_HTTPS_PORT,
                "app_name": SUNSHINE_APP_NAME, "output_id": session.output_name,
                "stream_pixels": profile.stream_pixels.to_dict(), "fps": profile.fps}

    def stop(self, session):
        """True when the fork held our reservation and it is now really stopped."""
        # The fork's reservation lives in memory. Reconfirm this exact lease
        # first so a fork restart or a lost response cannot strand the output,
        # then stop only the reserved session; there is no global stop.
        try:
            self._call(session, "desktop.claim", output_id=session.output_name)
        except RemoteError as error:
            if error.code != "desktop_busy":
                raise
            # An unrelated owner holds the fork, so it holds nothing of ours and
            # there is nothing to stop. Never stop another client's session.
            return False
        result = self._call(session, "desktop.stop")
        if result.get("stopped") is not True or result.get("session_count") != 0:
            raise RemoteError("sunshine_stop_incomplete")
        after = self._call(session, "desktop.session")
        if after.get("stopped") is not True or after.get("session_count") != 0:
            raise RemoteError("sunshine_stop_incomplete")
        return True

    def release(self, session):
        result = self._call(session, "desktop.release")
        if result.get("released") is not True:
            raise RemoteError("sunshine_release_unconfirmed")


class VncBackend:
    """One owned WayVNC instance on a pre-bound loopback fd."""

    name = "vnc"

    def __init__(self, factory):
        self.factory, self._instances, self._ports = factory, {}, {}

    def _instance(self, session):
        if session.id not in self._instances:
            self._instances[session.id] = self.factory(session.id)
        return self._instances[session.id]

    def status(self):
        try:
            available = self.factory("probe").available()
        except (RemoteError, OSError):
            available = False
        return {"available": available, "reason": None if available else "wayvnc_0_10_1_required",
                "transport": "wss", "audio": False}

    def prepare(self, session, profile):
        instance = self._instance(session)
        value = instance.start(session.output_name, profile.output_mode_pixels.to_dict(),
                               profile.logical_size.to_dict())
        self._ports[session.id] = value["port"]
        # REMOTE-6: take WayVNC's one mid-stream resize here, so the session's
        # real client is served the settled size from its own ServerInit. It is
        # best effort: a prime that did not finish is not a failed session, and
        # the document still names both sizes for a client that meets one.
        settled = instance.settle()
        # WayVNC shows a client two framebuffer sizes, so the document names
        # both: `initial_framebuffer_pixels` is what ServerInit announces and
        # `framebuffer_pixels` is what it serves from the first NewFBSize rect
        # onwards. A client that expects only one of them is wrong for part of
        # every scale-2 session (docs/wayvnc.md). They are equal exactly when
        # the owned output is planned at scale 1.
        return {"backend": "vnc", "transport": "wss",
                "path": VNC_BRIDGE_PATH.format(session_id=session.id),
                "output_id": session.output_name,
                "initial_framebuffer_pixels": settled or opening_pixels(profile),
                "framebuffer_pixels": served_pixels(profile)}

    def loopback_port(self, session):
        """The owned WayVNC listener the authenticated WSS bridge connects to.

        It is never sent to a client: nothing outside this process learns the
        port, and nothing outside the host can reach it.
        """
        return self._ports.get(session.id)

    def stop(self, session):
        if not self._instance(session).stop():
            raise RemoteError("vnc_stop_incomplete")
        return True

    def release(self, session):
        self.stop(session)
        self._instances.pop(session.id, None)
        self._ports.pop(session.id, None)
