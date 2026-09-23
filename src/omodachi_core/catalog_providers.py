"""Reviewed Gio Apps and Omarchy fonts providers, with optional action handlers.

The system-Python scanner is a fresh process: GLib owns Desktop Entry parsing,
XDG precedence, visibility, TryExec, locale and duplicate desktop IDs. Exec is
never serialized. This file also runs standalone without importing core/GI from
the daemon venv: /usr/bin/python3 <this file> --scan-apps.

The non-demo bootstrap installs reviewed actions explicitly; standalone
install_catalog_providers defaults to read-only. Registration never launches an
application or changes a font. Actual handlers only accept an
ID+revision from the current server provider index, and re-read before dispatch.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import stat
import subprocess
import time
from typing import Callable

MAX_ROWS = 512
MAX_OUTPUT = 2 * 1024 * 1024
MAX_DESKTOP_FILE = 256 * 1024
SOURCE_KEYS = ("action", "target", "when", "checked", "provider", "surface")
FONT_LIST = "/usr/share/omarchy/bin/omarchy-font-list"
FONT_CURRENT = "/usr/share/omarchy/bin/omarchy-font-current"
FONT_SET = "/usr/share/omarchy/bin/omarchy-font-set"
HIDES = "/usr/share/omarchy/default/omarchy/launcher.hides"


class ProviderUnavailable(ValueError):
    """Stable non-private errors; subprocess stderr and source text stay local."""


def _plain(value, *, maximum=256, allow_empty=False):
    if (not isinstance(value, str) or (not allow_empty and not value)
            or len(value.encode("utf-8")) > maximum
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ProviderUnavailable("provider_metadata_invalid")
    return value


def valid_desktop_id(value):
    _plain(value, maximum=255)
    if value.startswith(("-", ".")) or "/" in value or "\\" in value:
        raise ProviderUnavailable("desktop_id_invalid")
    return value


def _read_bounded(path: Path, maximum: int, *, trusted_owner=False):
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ProviderUnavailable("provider_source_invalid")
        if trusted_owner and (info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022):
            raise ProviderUnavailable("provider_source_owner_invalid")
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise ProviderUnavailable("provider_source_limit")
    return value


def scan_gio_apps(*, hides_path=HIDES):
    """System-Python only. Return safe metadata and opaque source revisions.

    No launch/get_commandline/get_executable call is made. The selected file is
    hashed internally to invalidate an action even when only Exec changes.
    """
    import gi
    gi.require_version("Gio", "2.0")
    gi.require_version("GioUnix", "2.0")
    from gi.repository import Gio, GioUnix
    hides_file = Path(hides_path)
    hidden = set()
    if hides_file.is_file():
        for line in _read_bounded(hides_file, 65536).decode("utf-8").splitlines():
            line = line.strip()
            if line.endswith(".desktop"):
                line = line[:-8]
            if line:
                hidden.add(line)
    apps, ids = [], set()
    stats = {"gio_entries": 0, "visibility_filtered": 0, "omarchy_hidden": 0,
             "invalid_metadata": 0, "duplicate_ids": 0}
    for info in Gio.AppInfo.get_all():
        stats["gio_entries"] += 1
        if not isinstance(info, GioUnix.DesktopAppInfo) or not info.should_show() or info.get_is_hidden():
            stats["visibility_filtered"] += 1
            continue
        try:
            full_id = info.get_id()
            if not isinstance(full_id, str) or not full_id.endswith(".desktop"):
                raise ProviderUnavailable("desktop_id_invalid")
            app_id = valid_desktop_id(full_id[:-8])
            if app_id in ids:
                stats["duplicate_ids"] += 1
                continue
            ids.add(app_id)
            if app_id in hidden:
                stats["omarchy_hidden"] += 1
                continue
            name = _plain(info.get_name(), maximum=512)
            icon_object = info.get_icon()
            # GIcon serialization preserves themed/file semantics as metadata;
            # it does not authorize reading an arbitrary path on the phone.
            icon = _plain(icon_object.to_string() if icon_object else "", maximum=1024, allow_empty=True)
            filename = info.get_filename()
            if not filename:
                raise ProviderUnavailable("desktop_source_missing")
            source = _read_bounded(Path(filename), MAX_DESKTOP_FILE, trusted_owner=True)
            apps.append({"appId": app_id, "label": name, "icon": icon,
                         "appRevision": hashlib.sha256(source).hexdigest()})
        except (OSError, UnicodeError, ValueError, TypeError):
            stats["invalid_metadata"] += 1
            continue
        if len(apps) > MAX_ROWS:
            raise ProviderUnavailable("apps_capacity")
    apps.sort(key=lambda row: (row["label"].casefold(), row["appId"]))
    return {"schema": 1, "apps": apps, "stats": stats}


def bounded_process(argv: tuple[str, ...], environment: dict[str, str], *, timeout=4.0):
    """Finite argv, bounded output/deadline. Never expose stderr on failure."""
    process = subprocess.Popen(argv, env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    result = bytearray()
    deadline = time.monotonic() + timeout
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderUnavailable("provider_timeout")
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, min(65536, MAX_OUTPUT + 1 - len(result)))
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    result.extend(chunk)
                    if len(result) > MAX_OUTPUT:
                        raise ProviderUnavailable("provider_output_limit")
        process.wait(timeout=max(.001, deadline - time.monotonic()))
        if process.returncode:
            raise ProviderUnavailable("provider_command_failed")
        return result.decode("utf-8")
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise ProviderUnavailable("provider_command_failed") from None
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)
        if process.stdout:
            process.stdout.close()


#: The presentation/search fields a desktop-entry or font listing needs. No
#: title, agent content, credential file or environment value is logged.
PROVIDER_ENVIRONMENT_FIELDS = frozenset({
    "XDG_DATA_HOME", "XDG_DATA_DIRS", "XDG_CONFIG_HOME", "XDG_CURRENT_DESKTOP",
    "XDG_SESSION_DESKTOP", "DESKTOP_SESSION", "LANG", "LANGUAGE", "LC_ALL",
    "LC_MESSAGES", "PATH", "DBUS_SESSION_BUS_ADDRESS"})


def _presentation(process: Path) -> dict[str, str] | None:
    """One process's allowlisted environment, or None if it cannot be read."""
    try:
        raw = (process / "environ").read_bytes()
        if len(raw) > 131072:
            return None
        selected = {}
        for pair in raw.split(b"\0"):
            key, equal, value = pair.partition(b"=")
            if equal and key.decode(errors="replace") in PROVIDER_ENVIRONMENT_FIELDS:
                selected[key.decode()] = value.decode()
        return selected or None
    except (OSError, UnicodeError):
        return None


def _shell_presentation(proc: Path = Path("/proc")):
    """The Omarchy shell's environment, found by its argv - the old way.

    Kept as the fallback for a session whose compositor cannot be identified
    from its runtime directory. It is not the first choice any more because
    the match is on a command line that does not survive the shell restarting
    itself: Quickshell relaunches from its own signal handlers and comes back
    as a bare `/usr/bin/quickshell`, at which point this finds nothing at all.
    """
    candidates = []
    for process in proc.iterdir():
        try:
            if not process.name.isdigit() or process.stat().st_uid != os.getuid():
                continue
            argv = (process / "cmdline").read_bytes().split(b"\0")
            if not argv or argv[0].rsplit(b"/", 1)[-1] not in (b"quickshell", b"qs") or b"/usr/share/omarchy/shell" not in argv:
                continue
        except (OSError, UnicodeError):
            continue
        selected = _presentation(process)
        if selected is not None:
            candidates.append(selected)
    return candidates[0] if len(candidates) == 1 else None


def provider_environment(proc: Path = Path("/proc"), runtime: Path | None = None):
    """Use the actual graphical user's XDG/locale/PATH, never SSH defaults.

    PERF-5 follow-up. Ask the compositor first, the same order
    `graphical_environment` already uses and for the same reason: Hyprland is
    identified from its own runtime directory, so the answer does not depend on
    any process's command line. Its environment carries every field below with
    the same values the shell has - it is the session both of them were started
    in - and it is there for as long as the session is.

    Before this, the only source was a `quickshell`/`qs` process whose argv
    contained the shell path. On a host where Quickshell had relaunched itself
    that match found nothing, so `apps` and `style.font` were both permanently
    unavailable and the Apps submenu was empty - while a transient `qs` could
    make them briefly work again, which is what made the same tap sometimes
    cost two subprocesses and sometimes none (PERF-5 report §1).
    """
    from .graphical import compositor_process, graphical_environment
    env = graphical_environment(proc, runtime=runtime)
    found = compositor_process(proc, runtime=runtime)
    selected = _presentation(found[0]) if found is not None else None
    if selected is None:
        selected = _shell_presentation(proc)
    if selected is None:
        raise ProviderUnavailable("graphical_session_ambiguous")
    env.update(selected)
    # The official AppLibrary hidden scanner unions these three desktop names.
    desktop_names = []
    for key in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION"):
        for name in env.get(key, "").split(":"):
            if name and name not in desktop_names:
                desktop_names.append(name)
    env["XDG_CURRENT_DESKTOP"] = ":".join(desktop_names)
    # Omarchy app visibility also scans the user's Nix profile after XDG roots.
    dirs = env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    nix = str(Path.home() / ".nix-profile/share")
    if Path(nix).is_dir() and nix not in dirs.split(":"):
        dirs += ":" + nix
    env["XDG_DATA_DIRS"] = dirs
    env["OMARCHY_PATH"] = "/usr/share/omarchy"
    return env


def validate_action_context(environment, owner_uid, owner_home):
    """Do not cross users or accept an SSH/client-selected graphical session."""
    if os.getuid() != owner_uid or os.geteuid() != owner_uid or owner_uid == 0:
        raise ProviderUnavailable("action_uid_mismatch")
    if environment.get("HOME") != str(owner_home):
        raise ProviderUnavailable("action_home_mismatch")
    if environment.get("XDG_RUNTIME_DIR") != f"/run/user/{owner_uid}":
        raise ProviderUnavailable("action_runtime_mismatch")
    if not re.fullmatch(r"wayland-[0-9]+", environment.get("WAYLAND_DISPLAY", "")):
        raise ProviderUnavailable("action_display_invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", environment.get("HYPRLAND_INSTANCE_SIGNATURE", "")):
        raise ProviderUnavailable("action_session_invalid")


class ReviewedProviderActionRunner:
    """Actual same-user launcher/font runner; only called after user invocation.

    stdout/stderr are discarded, so spawned apps cannot keep a capture pipe open
    or leak content. A launcher's exit zero means submitted, not window-visible.
    Font success additionally needs the provider's independent current-font read.
    """
    def __init__(self, *, owner_uid=None, owner_home=None, popen=None):
        self.owner_uid = os.getuid() if owner_uid is None else owner_uid
        self.owner_home = Path.home() if owner_home is None else Path(owner_home)
        self.popen = subprocess.Popen if popen is None else popen

    def preflight(self, argv, environment):
        validate_action_context(environment, self.owner_uid, self.owner_home)
        if (len(argv) == 4 and tuple(argv[:3]) == ("/usr/bin/uwsm-app", "--", "/usr/bin/gtk-launch")
                and isinstance(argv[3], str) and argv[3].endswith(".desktop")):
            valid_desktop_id(argv[3][:-8])
            executables = (argv[0], argv[2])
        elif len(argv) == 2 and argv[0] == FONT_SET:
            # Packaged font-set interpolates names into sed/XML. Do not admit
            # metacharacters that its implementation cannot safely represent.
            if not isinstance(argv[1], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,127}", argv[1]):
                raise ProviderUnavailable("font_name_not_safe_for_packaged_setter")
            executables = (argv[0],)
        else:
            raise ProviderUnavailable("provider_action_not_reviewed")
        for executable in executables:
            try:
                info = Path(executable).stat()
            except OSError:
                raise ProviderUnavailable("provider_action_missing") from None
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                    or not os.access(executable, os.X_OK)):
                raise ProviderUnavailable("provider_action_not_trusted")
        return True

    def __call__(self, argv, environment):
        argv = tuple(argv)
        self.preflight(argv, environment)
        process = self.popen(argv, env=dict(environment), shell=False, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        # PERF-4. A font set is a mutation whose success the provider reads back
        # afterwards, so that one is still waited for. An application launch is
        # not: `uwsm-app -- gtk-launch` registers a scope and hands the desktop
        # entry to the session, and waiting for it to exit was up to five
        # seconds of a client request spent watching somebody else's launcher.
        # "Exit zero" never meant the window was on screen anyway - the comment
        # on this class has always said so - and the window that does appear is
        # reported by the workspace snapshot, asynchronously, like every other
        # thing that happens on the host.
        if argv[0] != FONT_SET:
            return {"status": "submitted"}
        try:
            code = process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            # Never kill the process group: it may already contain the user's
            # launched app. Outcome is unknown and is not automatically retried.
            process.kill()
            process.wait(timeout=1)
            raise ProviderUnavailable("provider_action_outcome_unknown") from None
        if code:
            raise ProviderUnavailable("provider_action_failed")
        return {"status": "submitted"}


@dataclass(frozen=True)
class PreparedProviderAction:
    entry_id: str
    provider_revision: str
    argv: tuple[str, ...]


class CatalogProviders:
    def __init__(self, service, *, runner: Callable = bounded_process,
                 environment: Callable = provider_environment, app_reader=None,
                 actions=False, action_runner=None):
        self.service, self.runner, self.environment = service, runner, environment
        self.app_reader = app_reader
        if actions and action_runner is None:
            raise ValueError("actions require an explicit reviewed action_runner")
        self.actions = actions
        self.action_runner = action_runner
        self.apps_index = {}
        self.fonts_index = {}
        self.apps_revision = ""
        self.fonts_revision = ""
        self.last_app_stats = {}
        self.last_action_result = None
        self.last_action_errors = {}
        self._owned = {"apps": {}, "fonts": {}}
        self._expected_sources = {
            "apps": dict.fromkeys(SOURCE_KEYS, "") | {"provider": "apps"},
            "style.font": dict.fromkeys(SOURCE_KEYS, "") | {"provider": "fonts"},
        }

    def _source_ok(self, entry_id):
        row = self.service.runtime.catalog.by_id(entry_id)
        if row is None:
            return False
        current = row.as_dict()
        return (current.get("kind") == "menu"
                and all((current.get(k) or "") == value for k, value in self._expected_sources[entry_id].items()))

    def _scan_apps(self):
        raw = self.app_reader() if self.app_reader else json.loads(self.runner(
            ("/usr/bin/python3", str(Path(__file__).resolve()), "--scan-apps"), self.environment()))
        if not isinstance(raw, dict) or raw.get("schema") != 1 or not isinstance(raw.get("apps"), list) or len(raw["apps"]) > MAX_ROWS:
            raise ProviderUnavailable("apps_schema_invalid")
        from .catalog import app_entry_id
        rows, seen = [], set()
        for app in raw["apps"]:
            if not isinstance(app, dict) or set(app) != {"appId", "label", "icon", "appRevision"}:
                raise ProviderUnavailable("apps_schema_invalid")
            app_id = valid_desktop_id(app["appId"])
            entry_id = app_entry_id(app_id)
            if entry_id in seen:
                raise ProviderUnavailable("apps_duplicate_id")
            seen.add(entry_id)
            label = _plain(app["label"], maximum=512)
            icon = _plain(app["icon"], maximum=1024, allow_empty=True)
            if not isinstance(app["appRevision"], str) or not re.fullmatch(r"[0-9a-f]{64}", app["appRevision"]):
                raise ProviderUnavailable("apps_revision_invalid")
            # The static source always wins; never grant a dynamic launch route
            # to a user-created row that happens to collide with this ID.
            if self.service.runtime.catalog.by_id(entry_id):
                continue
            rows.append({"id": entry_id, "parent": "apps", "kind": "app", "appId": app_id,
                         "label": label, "icon": icon, "appRevision": app["appRevision"],
                         "action": "", "target": "", "provider": "", "when": "", "checked": ""})
        rows.sort(key=lambda row: (row["label"].casefold(), row["id"]))
        self.last_app_stats = {k: v for k, v in raw.get("stats", {}).items() if type(v) is int and 0 <= v <= 100000}
        return rows

    def _revision(self, rows):
        # Any source/catalog edit invalidates a prepared provider reference,
        # even if it leaves this particular app's label and desktop file alone.
        payload = {"sourceRevision": self.service.runtime.catalog.revision, "rows": rows}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

    def _retire(self, provider):
        # RoutePolicy currently has no public unregister API. Remove only the
        # exact objects this instance installed, never another owner's adapter.
        for entry_id, (descriptor, callback) in self._owned[provider].items():
            policy = self.service.policy
            policy.unregister(entry_id, expected=descriptor)
            if self.service._executors.get(entry_id, (None,))[0] is callback:
                self.service._executors.pop(entry_id, None)
        self._owned[provider] = {}

    def _register_actions(self, provider, rows, revision):
        self._retire(provider)
        if not self.actions:
            return
        if self.action_runner is None:
            raise ProviderUnavailable("provider_action_runner_missing")
        from .routes import RouteDescriptor
        self.last_action_errors = {key: value for key, value in self.last_action_errors.items() if key not in {row["id"] for row in rows}}
        environment = self.environment()
        for row in rows:
            entry_id = row["id"]
            prepared = self._prepare(provider, entry_id, revision, rescan=False)
            if hasattr(self.action_runner, "preflight"):
                try:
                    self.action_runner.preflight(prepared.argv, environment)
                except (OSError, ValueError):
                    self.last_action_errors[entry_id] = "provider_action_unavailable"
                    continue
            def execute(argv, ident=entry_id, expected=revision, category=provider):
                fresh = self._prepare(category, ident, expected, rescan=True)
                if tuple(argv) != fresh.argv:
                    raise ProviderUnavailable("provider_action_mismatch")
                result = self.action_runner(fresh.argv, self.environment())
                if isinstance(result, dict) and result.get("status") not in {None, "submitted"}:
                    raise ProviderUnavailable("provider_action_failed")
                if category == "fonts":
                    current = self.runner((FONT_CURRENT,), self.environment()).strip()
                    if current != self.fonts_index[ident]["label"]:
                        self.last_action_result = {"entry_id": ident, "status": "readback_failed"}
                        raise ProviderUnavailable("font_readback_failed")
                    self.last_action_result = {"entry_id": ident, "status": "readback_confirmed"}
                else:
                    # PERF-4. The identity re-read that guards this launch is
                    # the `rescan=True` above, taken *before* the desktop entry
                    # was handed to the session. Repeating it afterwards proved
                    # nothing the first read had not already proved and cost a
                    # second half-second Gio scan inside the client's request.
                    self.last_action_result = {"entry_id": ident, "status": "submitted_identity_confirmed"}
            self.service.policy.register(entry_id, RouteDescriptor("host", True, argv=prepared.argv),
                                         source_action=row.get("action", ""))
            self.service.register_executor(entry_id, execute)
            self._owned[provider][entry_id] = (self.service.policy._adapters[entry_id], execute)

    def apps(self):
        try:
            if not self._source_ok("apps"):
                raise ProviderUnavailable("apps_source_changed")
            rows = self._scan_apps()
            revision = self._revision(rows)
            self.apps_index = {row["id"]: row for row in rows}
            self.apps_revision = revision
            self._register_actions("apps", rows, revision)
            return rows
        except Exception:
            self.apps_index, self.apps_revision = {}, ""
            self._retire("apps")
            raise

    def _scan_fonts(self):
        env = self.environment()
        raw = self.runner((FONT_LIST,), env)
        current = self.runner((FONT_CURRENT,), env).strip()
        current = _plain(current, maximum=256, allow_empty=True)
        fonts = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            value = _plain(line.strip(), maximum=256)
            if value not in fonts:
                fonts.append(value)
        if len(fonts) > MAX_ROWS:
            raise ProviderUnavailable("fonts_capacity")
        # Official MenuModel: source order, slugify and append '-' on collision.
        taken, rows = set(), []
        for font in fonts:
            slug = re.sub(r"[^a-z0-9]+", "-", font.lower()).strip("-") or "item"
            entry_id = "style.font." + slug
            while entry_id in taken:
                entry_id += "-"
            taken.add(entry_id)
            if len(entry_id) > 128 or self.service.runtime.catalog.by_id(entry_id):
                continue
            rows.append({"id": entry_id, "parent": "style.font", "kind": "action", "label": font,
                         "icon": "✓" if current == font else "",
                         "action": "omarchy-font-set " + shlex.quote(font), "provider": "", "target": "",
                         "when": "", "checked": ""})
        return rows

    def fonts(self):
        try:
            if not self._source_ok("style.font"):
                raise ProviderUnavailable("fonts_source_changed")
            rows = self._scan_fonts()
            revision = self._revision(rows)
            self.fonts_index = {row["id"]: row for row in rows}
            self.fonts_revision = revision
            self._register_actions("fonts", rows, revision)
            return rows
        except Exception:
            self.fonts_index, self.fonts_revision = {}, ""
            self._retire("fonts")
            raise

    def _prepare(self, provider, entry_id, expected_revision, *, rescan=True):
        from .service import identifier
        identifier(entry_id)
        source = "apps" if provider == "apps" else "style.font"
        if not self._source_ok(source):
            raise ProviderUnavailable("provider_source_changed")
        old_index = self.apps_index if provider == "apps" else self.fonts_index
        revision = self.apps_revision if provider == "apps" else self.fonts_revision
        if not expected_revision or expected_revision != revision or entry_id not in old_index:
            raise ProviderUnavailable("provider_reference_stale")
        rows = self._scan_apps() if provider == "apps" and rescan else self._scan_fonts() if rescan else list(old_index.values())
        current = {row["id"]: row for row in rows}
        if self._revision(rows) != expected_revision or entry_id not in current:
            raise ProviderUnavailable("provider_reference_stale")
        row = current[entry_id]
        if provider == "apps":
            argv = ("/usr/bin/uwsm-app", "--", "/usr/bin/gtk-launch", row["appId"] + ".desktop")
        else:
            argv = (FONT_SET, row["label"])
        return PreparedProviderAction(entry_id, expected_revision, argv)

    def prepare_app(self, entry_id, expected_revision):
        return self._prepare("apps", entry_id, expected_revision)

    def prepare_font(self, entry_id, expected_revision):
        return self._prepare("fonts", entry_id, expected_revision)


def install_catalog_providers(service, **kwargs):
    """Register on the existing directory; read-only unless actions=True.

    Intended bootstrap integration after reviewed state adapters are installed:
        service.catalog_providers = install_catalog_providers(service)
    Optional action activation is a separate, explicit integration decision.
    """
    providers = CatalogProviders(service, **kwargs)
    service.runtime.register_provider("apps", providers.apps)
    service.runtime.register_provider("fonts", providers.fonts)
    return providers


if __name__ == "__main__":
    import sys
    if sys.argv[1:] != ["--scan-apps"]:
        raise SystemExit(2)
    try:
        print(json.dumps(scan_gio_apps(), ensure_ascii=False, separators=(",", ":")))
    except Exception:
        print(json.dumps({"error": "apps_reader_unavailable"}))
        raise SystemExit(1)
