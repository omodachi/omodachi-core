"""Finite host defaults, separate from config plans and device controls.

Mostly Remote defaults, plus PAIR-2's `pairing_mode`, which is a host policy
rather than a session setting: it decides only whether a pairing request may
arrive without an invitation, never whether one is approved.

No host command, stream restart, credential, or capture authorization is stored
or applied here. A lease captures one revision; explicit client choices win.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import threading

from .clipboard import CLIPBOARD_MODES
from .protocol import PAIRING_MODES

DEFAULTS = {"allow_dynamic_resolution": True, "quality": "balanced", "host_audio_playback": False,
            "voice_uplink": False, "remote_backend": "sunshine", "pairing_mode": "open",
            # AUTH-1. Whether a paired device may satisfy a host password
            # prompt. Off is the shipped value and the only safe default: the
            # PAM entry may be installed on a host whose owner has not decided
            # yet, and until they do, the helper refuses before it publishes
            # anything at all.
            "biometric_auth": False,
            # CLIP-1. Whether the host's clipboard is shared with a paired
            # device, and in which direction. Off is the shipped value: a
            # clipboard carries passwords and one-time codes, so it travels
            # only after somebody has said it may. `host_to_device` publishes
            # `clipboard.changed` and answers `GET /v1/clipboard`; `both` also
            # lets a device write this host's clipboard.
            "clipboard_sync": "off"}
LEGACY_FIELDS = {"follow_orientation", "resolution", "quality", "host_audio_playback"}
QUALITIES = {"performance": (30, 8_000), "balanced": (60, 12_000), "quality": (60, 20_000)}
# STREAM-1. A device may pick its own point on the same table (速度优先 /
# 平衡 / 画质优先), or its own numbers inside this range (自定义). The host's
# `quality` preference stays what a device that picks nothing gets. The table
# and the range are published in `profile_defaults`, so no client carries a
# copy of either.
CUSTOM_FPS = (30, 60)
CUSTOM_BITRATE_KBPS = (4_000, 40_000)
# Which backend a session gets when the client does not name one. The host says
# `sunshine`; `remote/session.py` falls through to whatever is actually
# available, and reports the result as `capabilities.default_backend`.
BACKENDS = ("sunshine", "vnc")
# PAIR-2. `open` is the shipped default: the handshake's boundary is the local
# Approve, so a request needs no invitation to reach it. `invite` is the lock
# for a host that wants the 43-character one-shot back.


class PreferencesError(ValueError):
    def __init__(self, code="preferences_invalid", status=400):
        self.code, self.status = code, status
        super().__init__(code)


def validate_changes(changes, *, complete=False):
    if (not isinstance(changes, dict) or not changes or set(changes) - set(DEFAULTS)
            or complete and set(changes) != set(DEFAULTS)):
        raise PreferencesError()
    for key, value in changes.items():
        if key in {"allow_dynamic_resolution", "host_audio_playback", "voice_uplink", "biometric_auth"}:
            valid = type(value) is bool
        elif key == "remote_backend":
            valid = value in BACKENDS
        elif key == "pairing_mode":
            valid = value in PAIRING_MODES
        elif key == "clipboard_sync":
            valid = value in CLIPBOARD_MODES
        else:
            valid = isinstance(value, str) and value in QUALITIES
        if not valid: raise PreferencesError()


def quality_presets():
    """The three named rates, in the order a picker shows them (slow link first)."""
    return {name: {"fps": fps, "bitrate_kbps": bitrate} for name, (fps, bitrate) in QUALITIES.items()}


def profile_defaults(values):
    """Host quality defaults affect FPS/bitrate only; Remote owns pixel budget.

    `quality` is what a device that chose nothing streams at; `preset` names
    it. `presets` and `custom` are what a device may choose instead (STREAM-1).
    """
    validate_changes(values, complete=True)
    fps, bitrate = QUALITIES[values["quality"]]
    return {"quality": {"fps": fps, "bitrate_kbps": bitrate}, "preset": values["quality"],
            "presets": quality_presets(),
            "custom": {"fps": list(CUSTOM_FPS), "min_bitrate_kbps": CUSTOM_BITRATE_KBPS[0],
                       "max_bitrate_kbps": CUSTOM_BITRATE_KBPS[1]}}


class HostPreferencesStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        self._mutex = threading.RLock()
        self._memory = {"version": 2, "revision": 0, "values": dict(DEFAULTS)}
        if self.path is not None and not self.path.is_absolute():
            raise PreferencesError("preferences_path_invalid")

    @contextmanager
    def _locked(self):
        with self._mutex:
            if self.path is None:
                yield
                return
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            parent = self.path.parent.lstat()
            if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
                raise PreferencesError("preferences_store_unsafe", 503)
            lock = self.path.with_name(self.path.name + '.lock')
            fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise PreferencesError("preferences_store_unsafe", 503)
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _read(self):
        if self.path is None: return deepcopy(self._memory)
        try: fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError: return {"version": 2, "revision": 0, "values": dict(DEFAULTS)}
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PreferencesError("preferences_store_unsafe", 503)
            raw = stream.read(16385)
        if len(raw) > 16384: raise PreferencesError("preferences_store_invalid", 503)
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError()
                result[key] = value
            return result
        try:
            state = json.loads(raw, object_pairs_hook=unique)
            if (not isinstance(state, dict) or set(state) != {"version", "revision", "values"}
                    or type(state["version"]) is not int or state["version"] not in {1, 2}
                    or type(state["revision"]) is not int or not 0 <= state["revision"] < 2**53):
                raise ValueError()
            if state["version"] == 1:
                old = state["values"]
                if (not isinstance(old, dict) or set(old) != LEGACY_FIELDS
                        or type(old["follow_orientation"]) is not bool
                        or old["resolution"] not in {"auto", "720p", "1080p"}
                        or old["quality"] not in QUALITIES or type(old["host_audio_playback"]) is not bool):
                    raise ValueError()
                # Old controls described preferences, not a host prohibition.
                # Preserve previous permission and remove every host pixel cap.
                state = {"version": 2, "revision": state["revision"], "values": {
                    "allow_dynamic_resolution": True, "quality": old["quality"],
                    "host_audio_playback": old["host_audio_playback"]}}
                self._write(state)
            # A store written before a control existed is not an invalid store:
            # fill the new key with its default rather than refusing to start.
            #
            # ICON-1. The same is true in the other direction, and it was not
            # handled: a store written *after* a control existed carried a key
            # this build had never heard of, `validate_changes` refused it, and
            # the daemon died at startup with `preferences_store_invalid` and
            # restarted for ever. That is exactly what happened on `omarchy` at
            # 22:27 — a build that knows `biometric_auth` had written the key,
            # and a build that does not could no longer start. A preference
            # nobody here understands is not a corrupt store: it is somebody
            # else's control, so it is kept exactly as written (a later build
            # that does know it still finds it) and left out of everything this
            # build validates or reports.
            unknown = {key: value for key, value in state["values"].items() if key not in DEFAULTS}
            known = {key: value for key, value in state["values"].items() if key in DEFAULTS}
            missing = set(DEFAULTS) - set(known)
            if missing:
                known = {**{key: DEFAULTS[key] for key in missing}, **known}
                state = {**state, "values": {**unknown, **known}}
                self._write(state)
            validate_changes(known, complete=True)
        except (ValueError, TypeError):
            raise PreferencesError("preferences_store_invalid", 503) from None
        return state

    def _write(self, state):
        if self.path is None:
            self._memory = deepcopy(state)
            return
        fd, temporary = tempfile.mkstemp(prefix='.preferences-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(state, stream, separators=(',', ':'), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            parent = os.open(self.path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try: os.fsync(parent)
            finally: os.close(parent)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)

    @staticmethod
    def _view(state):
        # ICON-1: a preference this build does not know stays in the file and
        # out of the wire. Reporting it would mean publishing a control no
        # client here can read, validate or change.
        values = {key: value for key, value in state["values"].items() if key in DEFAULTS}
        return {"revision": state["revision"], "values": deepcopy(values),
                "scope": "host_remote", "applies_to": "next_session",
                "permission_effect": "subsequent_output_change_requests",
                "profile_defaults": profile_defaults(values)}

    def get(self):
        try:
            with self._locked(): return self._view(self._read())
        except OSError:
            raise PreferencesError("preferences_store_unavailable", 503) from None

    def set(self, *, expected_revision, changes):
        if type(expected_revision) is not int or not 0 <= expected_revision < 2**53:
            raise PreferencesError()
        validate_changes(changes)
        try:
            with self._locked():
                state = self._read()
                if state['revision'] != expected_revision:
                    raise PreferencesError('preferences_revision_conflict', 409)
                values = {**state['values'], **changes}
                if values != state['values']:
                    if state['revision'] == 2**53 - 1: raise PreferencesError('preferences_revision_exhausted', 409)
                    state.update(revision=state['revision'] + 1, values=values)
                    self._write(state)
                return self._view(state)
        except OSError:
            raise PreferencesError("preferences_store_unavailable", 503) from None
