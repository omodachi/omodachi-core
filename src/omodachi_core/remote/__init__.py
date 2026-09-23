"""The remote-desktop subsystem: one session, two modes, two backends."""
from __future__ import annotations

import os
from pathlib import Path

from .backends import SunshineBackend, VncBackend
from .errors import RemoteError
from .hyprland import Hyprland, OWNED_NAME
from .idle import OmarchyIdle
from .profile import EncoderLimits
from .session import RemoteManager, RemoteSession
from .shell import OmarchyShell
from .sunshine import SunshineDesktopIPC, read_private_json
from .vnc import HostBackendPreference, ManagedWayVNC

SUNSHINE_PAIRING_SOCKET = "omodachi-sunshine/pairing.sock"
JOURNAL_DIR = ".local/state/omodachi/remote"
CONFIG_PATH = ".config/omodachi/desktop-runtime.json"
# SPEC-A-era keys that this subsystem no longer has anything to do with. An
# installed host keeps its file; these are read and ignored rather than making
# the daemon refuse to start Remote at all.
LEGACY_FIELDS = ("recovery_output", "devices", "profiles")
DEFAULT_ENCODER = {"max_width": 4096, "max_height": 4096, "max_pixels": 16777216, "max_fps": 60,
                   "max_bitrate_kbps": 40000, "width_alignment": 2, "height_alignment": 2, "codecs": ["h264"]}


def host_settings(home: Path | None = None) -> dict:
    """Installed defaults, optionally overridden by one private local file."""
    home = Path(home) if home is not None else Path.home()
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    value = {"hyprland_instance": "auto", "sunshine_socket": str(runtime_dir / SUNSHINE_PAIRING_SOCKET),
             "journal_dir": str(home / JOURNAL_DIR), "encoder_limits": dict(DEFAULT_ENCODER),
             "render_density": 2.0}
    path = Path(os.environ.get("OMODACHI_REMOTE_CONFIG", str(home / CONFIG_PATH)))
    try:
        stored = read_private_json(path)
    except (RemoteError, OSError):
        return value
    stored = {key: item for key, item in stored.items() if key not in LEGACY_FIELDS}
    if stored.get("version") != 1 or set(stored) - (set(value) | {"version"}):
        raise RemoteError("remote_config_invalid")
    value.update(stored)
    return value


def build_manager(*, home: Path | None = None, certificate_resolver=None, events=None,
                  bar_position=None, idle=None, shell=None, environment=None) -> RemoteManager:
    """Assemble the host manager. Raises when there is no graphical session."""
    from ..graphical import graphical_environment
    from ..official_bar_position import OfficialBarPosition

    settings = host_settings(home)
    environment = environment or graphical_environment
    instance = settings["hyprland_instance"]
    if instance == "auto":
        instance = environment()["HYPRLAND_INSTANCE_SIGNATURE"]
    limits = dict(settings["encoder_limits"])
    limits["codecs"] = tuple(limits.get("codecs", ("h264",)))
    journal_dir = Path(settings["journal_dir"])

    def vnc_factory(session_id):
        return ManagedWayVNC(journal_dir / "vnc" / session_id, environment=environment)

    return RemoteManager(
        hyprland=Hyprland(instance), journal_dir=journal_dir, encoder=EncoderLimits(**limits),
        sunshine=SunshineBackend(SunshineDesktopIPC(Path(settings["sunshine_socket"])),
                                 certificate_resolver=certificate_resolver),
        vnc=VncBackend(vnc_factory), bar_position=bar_position or OfficialBarPosition(),
        idle=idle or OmarchyIdle(environment=environment),
        shell=shell or OmarchyShell(environment=environment, home=home),
        render_density=settings["render_density"], events=events)

