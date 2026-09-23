"""Packaged user desktop-entry integration; not a full Host setup installer.

Resources are included in wheels/releases. Import/status/build creates nothing;
explicit install reconciles the canonical launcher, icon and desktop entry.
No daemon/media or credential lifecycle is changed.
"""
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import stat

CANONICAL_WRAPPER = "omodachi-panel"


def asset(name):
    return resources.files("omodachi_core").joinpath("data", "desktop_launcher", name).read_bytes()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def desktop_exec(path):
    text = str(path)
    if any(c in text for c in "\0\n\r"):
        raise ValueError("invalid_home_path")
    # Desktop Exec quoting is not shell evaluation; the executable has no args.
    quoted = '"' + "".join(("\\" + c if c in '\\"`$' else "%%" if c == "%" else c) for c in text) + '"'
    return quoted.replace("\\", "\\\\")


def payload(home):
    wrapper = home / ".local/bin" / CANONICAL_WRAPPER
    entry = ("""[Desktop Entry]
Type=Application
Version=1.0
Name=Omodachi
GenericName=Host control panel
Comment=Open the local Omodachi control panel
Comment[zh_CN]=打开本机 Omodachi 控制面板
Exec=""" + desktop_exec(wrapper) + """
Icon=com.omodachi.host
Terminal=false
Categories=Utility;System;
Keywords=Omodachi;Remote;Host;
StartupNotify=false
X-Omodachi-Managed=true
""").encode()
    return {
        wrapper: (asset(CANONICAL_WRAPPER), 0o755),
        home / ".local/share/icons/hicolor/scalable/apps/com.omodachi.host.svg": (asset("com.omodachi.host.svg"), 0o644),
        home / ".local/share/applications/com.omodachi.host.desktop": (entry, 0o644),
    }


def _inspect(expected):
    rows = []
    for path, (data, mode) in expected.items():
        if path.is_symlink():
            state = "conflict"
        elif not path.exists():
            state = "missing"
        elif path.is_file() and path.read_bytes() == data and stat.S_IMODE(path.stat().st_mode) == mode:
            state = "installed"
        else:
            state = "conflict"
        rows.append({"path": str(path), "status": state, "sha256": sha(data), "mode": oct(mode)})
    return rows


def inspect(home):
    return _inspect(payload(home))


def _create_missing(expected):
    for path, (data, mode) in expected.items():
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents a race from overwriting a new user file.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())


def _install(home):
    expected = payload(home)
    before = inspect(home)
    if any(row["status"] == "conflict" for row in before):
        return {"installed": False, "error": "existing_file_conflict", "files": before}
    _create_missing(expected)
    after = inspect(home)
    return {
        "installed": all(row["status"] == "installed" for row in after),
        "files": after,
        "surface": "local_plugin_panel",
        "existing_files_overwritten": False,
        "media_started": False,
        "host_services_restarted": False,
    }


def status(home=None):
    home = Path.home() if home is None else Path(home)
    rows = inspect(home)
    return {
        "installed": all(row["status"] == "installed" for row in rows),
        "files": rows,
        "scope": "user_desktop_entry",
        "read_only": True,
    }


def install(home=None):
    home = Path.home() if home is None else Path(home)
    try:
        return {"scope": "user_desktop_entry", **_install(home)}
    except (OSError, ValueError):
        return {
            "scope": "user_desktop_entry",
            "installed": False,
            "error": "desktop_entry_install_failed",
            "existing_files_overwritten": False,
        }
