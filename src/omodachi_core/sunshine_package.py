"""Install the managed Sunshine fork on this host, from a built archive.

Until INSTALL-1 the installer only *reported* on the fork: it printed where
the assets should be and left the rest to a developer with an rsync script.
A real user has no fork at all, so Remote had no Sunshine backend and the
panel's one button could not give them one.

The archive is what `scripts/package_release.sh` in the fork produces:

    omodachi-sunshine-<short sha>-x86_64/
        sunshine              the linked binary, assets path compiled in as
                              the *relative* "assets", so it is resolved
                              against the unit's WorkingDirectory
        assets/               shaders and images, beside the binary
        lib/                  any shared object no distribution package owns
        LICENSE NOTICE        GPL-3.0, verbatim
        SOURCE                repository URL and the exact commit
        DEPENDS               one pacman package per line
        BUNDLED               name and build-host path of each lib/ entry
        MANIFEST.sha256       every other file in the archive

Nothing here runs as root except the one `pacman -S --needed` for packages the
host does not already have, and that only when the list is non-empty. Nothing
here writes outside `~/.local/share/omodachi/sunshine`, `~/.config/systemd/user`
(the unit, or its one drop-in), `~/.config/omodachi/sunshine-web-credentials.json`
and `~/.local/state/omodachi/sunshine-unit.json`, and `remove()` takes back
exactly those.

RELEASE-9. Only what this installer can show it made is written over, stopped,
disabled or deleted:

* the unit or drop-in is written only where there is none, or where the one
  there carries MARKER; a Sunshine that something else set up (a unit file
  without the marker, the distribution's `sunshine` package, drop-ins nobody
  here wrote) is refused and left exactly as it is, and Remote uses VNC;
* the unit's enabled state before the first install is recorded, and
  `remove()` disables only a unit this installer enabled;
* a `<sha>/` directory is replaced or deleted only when it is byte for byte a
  release archive's contents (`pristine`), so a hand-built tree is kept;
* the fork's web admin UI (47990) accepts connections from this computer only
  (`origin_web_ui_allowed=pc`) and has a login from the moment it starts: a
  random user name and a random password hash that no password is known to
  match, in a 0600 file of ours, never printed. Upstream leaves that page
  unclaimed until somebody opens it and sets a password - the first caller
  wins - and a managed fork has nobody to open it.

Standard library only: this module is imported by the installer script running
under the system interpreter, before the virtualenv exists.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

# Every public URL in this project hangs off one owner. It is a single name so
# that moving the project between accounts or organisations is one edit here
# and one in the plugin, not a grep across four repositories.
GITHUB_OWNER = "omodachi"
SUNSHINE_REPOSITORY = f"https://github.com/{GITHUB_OWNER}/omodachi-sunshine"
# The fork is GPL-3.0 and therefore public; its releases are the source.
#
# CORE-2 §4: the default is no longer `releases/latest`. A core release pins
# the one fork build it was tested against - a versioned release asset, its
# sha256 and the fork commit - in `data/versions.json`. `--sunshine-package`
# overrides the pin, OMODACHI_SUNSHINE_PACKAGE overrides it from the
# environment (how a staging host points at a private build), and the word
# `latest` is the explicit choice of the newest release.
#
# RELEASE-6: every one of those overrides needs the sha256 it must hash to,
# given by the person asking for it (`--sunshine-sha256` or
# OMODACHI_SUNSHINE_SHA256). A `.sha256` published beside the archive is never
# trusted - it comes from the same place as the archive, so it authenticates
# nothing - and nothing is ever installed unchecked.
LATEST_PACKAGE = f"{SUNSHINE_REPOSITORY}/releases/latest/download/omodachi-sunshine-x86_64.tar.zst"
LATEST = "latest"
PACKAGE_ENV = "OMODACHI_SUNSHINE_PACKAGE"
SHA256_ENV = "OMODACHI_SUNSHINE_SHA256"
VERSIONS = Path(__file__).resolve().parent / "data" / "versions.json"
_COMMIT = re.compile(r"[0-9a-f]{7,40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def pinned(path: Path = VERSIONS) -> dict:
    """`sunshine_package` from the core version table:
    {version, url, sha256, manifest_sha256, satisfied_by}."""
    try:
        entry = json.loads(path.read_text())["sunshine_package"]
        version, url, sha256 = entry["version"], entry["url"], entry["sha256"]
        manifest = entry["manifest_sha256"]
        url = url.replace("{repository}", SUNSHINE_REPOSITORY) if isinstance(url, str) else url
        satisfied = entry.get("satisfied_by", [])
        if (not _COMMIT.fullmatch(version) or not isinstance(url, str) or "://" not in url
                or "/releases/latest/" in url or not re.fullmatch(r"[0-9a-f]{64}", sha256)
                or not re.fullmatch(r"[0-9a-f]{64}", manifest)
                or not isinstance(satisfied, list) or not all(isinstance(v, str) and _COMMIT.fullmatch(v)
                                                               for v in satisfied)):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise SunshinePackageError("sunshine_package_pin_invalid",
                                   f"{path} has no usable sunshine_package pin") from None
    return {"version": version, "url": url, "sha256": sha256, "manifest_sha256": manifest,
            "satisfied_by": list(satisfied)}


def choose(spec: str | None = None, sha256: str | None = None, *, environ=None,
           pin: dict | None = None) -> dict:
    """Which archive to install, and against which checksum.

    `source` says why: `pin` (the core version table), `latest` (asked for by
    name), or `explicit` (a URL or path from the flag or the environment).
    Only a `pin` choice may be skipped for an installed fork that satisfies it.
    """
    environ = os.environ if environ is None else environ
    spec = spec or environ.get(PACKAGE_ENV) or None
    sha256 = (sha256 or environ.get(SHA256_ENV) or "").strip().lower() or None
    if sha256 is not None and not _SHA256.fullmatch(sha256):
        raise SunshinePackageError("sunshine_package_sha256_invalid",
                                   f"{sha256!r} is not a sha256 (64 hex characters)")
    if spec is None:
        pin = pin or pinned()
        if sha256 is not None and sha256 != pin["sha256"]:
            # The pin is its own checksum; a second one cannot loosen it.
            raise SunshinePackageError(
                "sunshine_package_sha256_conflict",
                f"the pinned archive must hash to {pin['sha256']}, not {sha256}; "
                f"drop --sunshine-sha256/${SHA256_ENV} or name the archive it belongs to "
                f"with --sunshine-package")
        return {"source": "pin", "spec": pin["url"], "sha256": pin["sha256"],
                "version": pin["version"], "satisfied_by": pin["satisfied_by"],
                "manifest_sha256": pin.get("manifest_sha256")}
    if sha256 is None:
        raise SunshinePackageError(
            "sunshine_package_sha256_required",
            f"{spec!r} is not the archive this core pins, so it needs the sha256 it must "
            f"hash to: add --sunshine-sha256 <64 hex> (or set ${SHA256_ENV}). A .sha256 "
            f"published beside an archive is not accepted, and nothing is installed "
            f"unchecked. Without --sunshine-package the pinned archive is used")
    if spec == LATEST:
        return {"source": "latest", "spec": LATEST_PACKAGE, "sha256": sha256, "version": None,
                "satisfied_by": []}
    return {"source": "explicit", "spec": spec, "sha256": sha256, "version": None, "satisfied_by": []}


def installed_fork(home: Path, *, runner=None) -> dict | None:
    """The managed fork the Sunshine unit starts right now, or None.

    Read from what systemd will actually run (`ExecStart` after every drop-in),
    so a fork a developer's hand-written drop-in points at counts exactly like
    one this installer wrote. It is "managed" when that binary lives under
    ~/.local/share/omodachi/sunshine/<dir>/; its version is the commit in the
    SOURCE file the release archive carries, or the directory's name.
    """
    try:
        result = _systemctl(["show", "-p", "ExecStart", "--value", SUNSHINE_UNIT], runner)
    except OSError:
        return None     # no systemd here at all
    match = re.search(r"\bpath=(\S+)", result.stdout or "") if result.returncode == 0 else None
    if not match:
        return None
    binary = Path(match.group(1))
    root = home / INSTALL_ROOT
    try:
        relative = binary.relative_to(root)
    except ValueError:
        return None
    if len(relative.parts) != 2 or relative.parts[1] != "sunshine" or not binary.is_file():
        return None
    directory = binary.parent
    version = relative.parts[0]
    try:
        for line in (directory / "SOURCE").read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() in ("commit", "sha") and _COMMIT.fullmatch(value.strip()[:40]):
                version = value.strip()
                break
    except OSError:
        pass
    dropin = home / UNIT_DIR / (SUNSHINE_UNIT + ".d") / DROPIN_NAME
    # Each file on its own: a missing drop-in must not hide our marked unit
    # (RELEASE-9 found the one read raising for the other).
    written_by_us = _marked(dropin) or _marked(home / UNIT_DIR / SUNSHINE_UNIT)
    enabled = _systemctl(["is-enabled", SUNSHINE_UNIT], runner)
    active = _systemctl(["is-active", SUNSHINE_UNIT], runner)
    argv = re.search(r"\bargv\[\]=(.*?)(?: ;|$)", result.stdout or "")
    arguments = argv.group(1).split()[1:] if argv else []
    return {"version": version, "directory": str(directory), "binary": str(binary),
            "written_by_installer": written_by_us, "arguments": arguments,
            "enabled": (enabled.stdout or "").strip() or "unknown",
            "active": (active.stdout or "").strip() or "unknown"}


def pristine(directory: Path, manifest_sha256: str | None = None) -> str:
    """"" when `directory` is exactly a release archive's contents, else why not.

    RELEASE-9. A real directory (not a link) holding a MANIFEST.sha256 - which,
    when `manifest_sha256` is given, must itself hash to it - where every file
    the manifest names hashes to its line, and nothing else: no file, link or
    other entry the manifest does not list. Such a tree holds nothing that is
    not in a published archive, so it is safe to trust as that build, to replace
    and to delete; anything else is somebody's and is kept.
    """
    try:
        if directory.is_symlink() or not directory.is_dir():
            return f"{directory} is not a directory"
        manifest = directory / "MANIFEST.sha256"
        if manifest.is_symlink() or not manifest.is_file():
            return "it has no MANIFEST.sha256"
        if manifest_sha256 is not None and digest(manifest) != manifest_sha256:
            return "its MANIFEST.sha256 is not the one the pinned archive carries"
        listed = set()
        for line in manifest.read_text().splitlines():
            expected, _, name = line.partition("  ")
            if not name:
                continue
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                return f"its manifest names {name!r}, outside the directory"
            member = directory / relative
            if member.is_symlink() or not member.is_file() or digest(member) != expected:
                return f"{relative} does not match its manifest line"
            listed.add(relative)
        for base, directories, files in os.walk(directory):
            for name in files + [entry for entry in directories
                                 if os.path.islink(os.path.join(base, entry))]:
                relative = Path(os.path.relpath(os.path.join(base, name), directory))
                if relative != Path("MANIFEST.sha256") and relative not in listed:
                    return f"it holds {relative}, which no release archive has"
    except (OSError, ValueError) as error:
        return f"it could not be read ({error})"
    return ""


def satisfies(installed_version: str, choice: dict) -> bool:
    """Whether an installed fork commit is the pinned build, or one the pin accepts."""
    if not installed_version or choice.get("source") != "pin":
        return False
    wanted = [choice["version"], *choice.get("satisfied_by", [])]
    have = installed_version.removesuffix("-dirty")
    if have != installed_version:
        return False    # a dirty tree is never "the same build"
    return any(have[:len(want)] == want or want[:len(have)] == have
               for want in wanted if min(len(want), len(have)) >= 7)

SUNSHINE_UNIT = "app-dev.lizardbyte.app.Sunshine.service"
INSTALL_ROOT = ".local/share/omodachi/sunshine"
UNIT_DIR = ".config/systemd/user"
DROPIN_NAME = "60-omodachi.conf"
ARCHIVE_NAME = re.compile(r"^omodachi-sunshine-(?P<sha>[0-9a-f]{7,40}(?:-dirty)?)-x86_64$")
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MARKER = "# Written by omodachi-core (scripts/install_host.py). Safe to delete."


class SunshinePackageError(Exception):
    """Anything that stops the fork being installed, with one plain sentence."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code, self.detail = code, detail


# --- fetching ---------------------------------------------------------------

def _download(url: str, destination: Path, *, opener=urllib.request.urlopen) -> Path:
    try:
        with opener(url, timeout=120) as response, destination.open("wb") as sink:
            copied = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > MAX_ARCHIVE_BYTES:
                    raise SunshinePackageError(
                        "sunshine_package_too_large",
                        f"{url} is larger than {MAX_ARCHIVE_BYTES // (1024 * 1024)} MiB")
                sink.write(chunk)
    except SunshinePackageError:
        raise
    except Exception as error:  # urllib raises a wide family; the user needs one line
        raise SunshinePackageError("sunshine_package_unreachable",
                                   f"could not download {url}: {error}") from None
    return destination


def fetch(spec: str, cache: Path, *, opener=urllib.request.urlopen) -> Path:
    """Return a local archive path for a URL or a path, downloading if needed."""
    if "://" not in spec:
        path = Path(spec).expanduser()
        if not path.is_file():
            raise SunshinePackageError("sunshine_package_missing", f"no such file: {path}")
        return path
    cache.mkdir(parents=True, exist_ok=True)
    return _download(spec, cache / (spec.rsplit("/", 1)[-1] or "sunshine.tar.zst"), opener=opener)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            value.update(chunk)
    return value.hexdigest()


# --- unpacking --------------------------------------------------------------

def _safe_members(archive: tarfile.TarFile, prefix: str):
    for member in archive.getmembers():
        name = member.name
        if name != prefix and not name.startswith(prefix + "/"):
            raise SunshinePackageError("sunshine_package_unexpected_member",
                                       f"{name} is outside {prefix}/")
        if member.issym() or member.islnk() or member.isdev():
            raise SunshinePackageError("sunshine_package_unexpected_member",
                                       f"{name} is a link or device node")
        if not (member.isfile() or member.isdir()):
            raise SunshinePackageError("sunshine_package_unexpected_member", f"{name} is not a file")
        yield member


def archive_prefix(archive: tarfile.TarFile) -> str:
    names = {name.split("/", 1)[0] for name in archive.getnames()}
    if len(names) != 1:
        raise SunshinePackageError("sunshine_package_shape",
                                   "the archive must hold exactly one top-level directory")
    prefix = names.pop()
    if not ARCHIVE_NAME.match(prefix):
        raise SunshinePackageError("sunshine_package_shape",
                                   f"{prefix} is not omodachi-sunshine-<sha>-x86_64")
    return prefix


@contextlib.contextmanager
def open_archive(path: Path):
    """tarfile if this interpreter reads zstd, the `zstd` binary if it does not.

    CPython grew `r:zst` in 3.14, which is what Omarchy ships, but the
    installer runs under whatever `python3` the host has and an installer that
    dies on `ReadError` tells the user nothing. `zstd` itself is not optional
    on Arch - pacman depends on it.
    """
    try:
        with tarfile.open(path, "r:*") as archive:
            yield archive
            return
    except (tarfile.ReadError, tarfile.CompressionError):
        pass
    if shutil.which("zstd") is None:
        raise SunshinePackageError("sunshine_package_unreadable",
                                   "this python cannot read .tar.zst and zstd is not installed")
    with tempfile.TemporaryDirectory(prefix="omodachi-sunshine-") as scratch:
        plain = Path(scratch) / "package.tar"
        result = _run(["zstd", "-dqf", str(path), "-o", str(plain)])
        if result.returncode != 0:
            raise SunshinePackageError("sunshine_package_unreadable",
                                       (result.stderr or "zstd could not decompress it").strip()[:200])
        with tarfile.open(plain, "r:") as archive:
            yield archive


def unpack(archive_path: Path, home: Path) -> dict:
    """Extract into ~/.local/share/omodachi/sunshine/<sha>/, atomically."""
    root = home / INSTALL_ROOT
    root.mkdir(parents=True, exist_ok=True)
    with open_archive(archive_path) as archive:
        prefix = archive_prefix(archive)
        sha = ARCHIVE_NAME.match(prefix).group("sha")
        staging = Path(tempfile.mkdtemp(prefix=".unpack-", dir=root))
        try:
            archive.extractall(staging, members=_safe_members(archive, prefix))
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    unpacked = staging / prefix
    target = root / sha
    # A reinstall of the same build replaces the directory rather than merging
    # into it: a stale shader from an older tree next to a newer binary is the
    # kind of thing that only shows up as a silent software-encoder fallback.
    # RELEASE-9: but only a directory that is exactly an archive's contents
    # (`pristine`). A tree somebody built or changed by hand under the same
    # name is theirs: nothing is replaced and the install stops.
    aside = None
    if os.path.lexists(target):
        why = pristine(target)
        if why:
            shutil.rmtree(staging, ignore_errors=True)
            raise SunshinePackageError(
                "sunshine_directory_not_ours",
                f"{target} is already there and is not a release archive this installer "
                f"unpacked ({why}); it was left as it is. Move it aside and install again")
        aside = Path(tempfile.mkdtemp(prefix=".replaced-", dir=root))
        target.rename(aside / "old")
    unpacked.rename(target)
    shutil.rmtree(staging, ignore_errors=True)
    if aside is not None:
        shutil.rmtree(aside, ignore_errors=True)
    (target / "sunshine").chmod(0o755)
    return {"sha": sha, "directory": str(target)}


def verify_manifest(directory: Path) -> list[str]:
    """Every file in MANIFEST.sha256 must be present and hash to its line."""
    manifest = directory / "MANIFEST.sha256"
    if not manifest.is_file():
        raise SunshinePackageError("sunshine_package_unverifiable", "the archive has no MANIFEST.sha256")
    bad = []
    for line in manifest.read_text().splitlines():
        expected, _, name = line.partition("  ")
        if not name:
            continue
        member = directory / name
        if not member.is_file() or digest(member) != expected:
            bad.append(name)
    return bad


# --- host packages ----------------------------------------------------------

def _run(argv, runner=None):
    runner = runner or subprocess.run
    return runner(argv, capture_output=True, text=True, check=False)


def missing_packages(names, *, runner=None) -> list[str]:
    absent = []
    for name in names:
        if _run(["pacman", "-Qq", name], runner).returncode != 0:
            absent.append(name)
    return absent


def install_packages(names, *, runner=None) -> dict:
    """Install only what is absent, through Omarchy's helper when it exists.

    An empty list is the overwhelmingly common case on a real Omarchy host -
    every one of the fork's shared libraries is already there - so a plain
    install never reaches for sudo at all.
    """
    absent = missing_packages(names, runner=runner)
    if not absent:
        return {"installed": [], "reason": "already_present"}
    helper = shutil.which("omarchy-pkg-add")
    command = ([helper, *absent] if helper
               else ["sudo", "pacman", "-S", "--needed", "--noconfirm", *absent])
    result = _run(command, runner)
    if result.returncode != 0:
        return {"installed": [], "reason": "package_install_failed", "missing": absent,
                "detail": (result.stderr or result.stdout or "").strip()[:200],
                "command": " ".join(command)}
    return {"installed": absent, "reason": "installed"}


def package_depends(directory: Path) -> list[str]:
    path = directory / "DEPENDS"
    if not path.is_file():
        return []
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


# --- the unit ---------------------------------------------------------------

UNIT_TEMPLATE = """[Unit]
{marker}
Description=Omodachi managed Sunshine
StartLimitIntervalSec=500
StartLimitBurst=5
After=graphical-session.target xdg-desktop-autostart.target xdg-desktop-portal.service

[Service]
# Upstream's own delay: the fork enumerates outputs at startup and a compositor
# that is still bringing them up answers with a display list it then keeps.
ExecStartPre=/bin/sleep 5
{body}
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=graphical-session.target
"""

DROPIN_TEMPLATE = """[Service]
{marker}
ExecStart=
{body}
"""


def _word(value: str) -> str:
    """One ExecStart word: `%` is systemd's specifier character, and a path
    with a space in it has to be quoted to stay one argument."""
    value = value.replace("%", "%%")
    if any(character.isspace() or character in "\"'\\;" for character in value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _body(directory: Path, arguments) -> str:
    # WorkingDirectory is load-bearing, not tidiness: the packaged binary has
    # the *relative* path "assets" compiled in as SUNSHINE_ASSETS_DIR, which is
    # the only way one build can serve every user. Without this line the fork
    # finds no shaders, logs five compile errors and streams on the CPU.
    return "\n".join([
        f"WorkingDirectory={_word(str(directory))}",
        "ExecStart=" + " ".join(_word(str(part)) for part in [directory / "sunshine", *arguments]),
        "Environment=SUNSHINE_MANAGED_LOCAL_PAIRING=1",
    ])


def unit_text(directory: Path, arguments) -> str:
    return UNIT_TEMPLATE.format(marker=MARKER, body=_body(directory, arguments))


def dropin_text(directory: Path, arguments) -> str:
    return DROPIN_TEMPLATE.format(marker=MARKER, body=_body(directory, arguments))


# --- the web admin UI (RELEASE-9) --------------------------------------------
# The fork, like upstream, always serves its admin UI on 47990, and until that
# UI has a login the first caller to POST /api/password sets one - no CSRF
# token is needed without an Origin header, and the address check is skipped
# too. Upstream expects the person who installed it to open the welcome page;
# a managed fork has nobody who will. So the unit points the fork at a
# credentials file of our own, made before the fork ever starts: a random user
# name, a random salt and a random value where the password hash goes, which
# no password is known to hash to. Nobody can log in, so nobody can claim it,
# and nothing secret is written anywhere or printed. `origin_web_ui_allowed=pc`
# on top keeps the page to this computer. The user's own Sunshine state
# (~/.config/sunshine/sunshine_state.json) is not touched.
WEB_CREDENTIALS = ".config/omodachi/sunshine-web-credentials.json"
WEB_ONLY_THIS_COMPUTER = "origin_web_ui_allowed=pc"
WEB_ARGUMENT_KEYS = ("origin_web_ui_allowed", "credentials_file")
_WEB_KEYS = ("username", "salt", "password")


def ensure_web_credentials(home: Path) -> dict:
    """Our credentials file for the fork's web UI: kept if it is there, else made."""
    path = home / WEB_CREDENTIALS
    if os.path.lexists(path):
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("not a regular file")
            value = json.loads(path.read_text())
            if not (isinstance(value, dict) and all(isinstance(value.get(key), str) and value[key]
                                                    for key in _WEB_KEYS)):
                raise ValueError("not a login")
        except (OSError, ValueError) as error:
            raise SunshinePackageError(
                "sunshine_web_credentials_unusable",
                f"{path} is there but is not a login this installer wrote ({error}); it was "
                f"left as it is. Delete it and install again") from None
        return {"path": str(path), "created": False}
    body = {"username": "omodachi-" + secrets.token_hex(8), "salt": secrets.token_hex(8),
            "password": secrets.token_hex(32).upper()}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(json.dumps(body, indent=4) + "\n")
    return {"path": str(path), "created": True}


def web_arguments(home: Path) -> list[str]:
    """The two fork arguments that close the web UI, creating the login if needed."""
    credentials = ensure_web_credentials(home)
    return [WEB_ONLY_THIS_COMPUTER, "credentials_file=" + credentials["path"]]


def remove_web_credentials(home: Path) -> bool:
    path = home / WEB_CREDENTIALS
    try:
        if path.is_symlink() or not path.is_file():
            return False
        value = json.loads(path.read_text())
        if not (isinstance(value, dict) and str(value.get("username", "")).startswith("omodachi-")):
            return False
        path.unlink()
        return True
    except (OSError, ValueError):
        return False


def render_nodes(dri: Path = Path("/dev/dri")) -> list[Path]:
    try:
        return sorted(node for node in dri.iterdir() if node.name.startswith("renderD"))
    except OSError:
        return []


def encoder_arguments(*, adapter=None, nodes=None, probe=None) -> list[str]:
    """`capture=wlr`, plus VAAPI only when a render node really answers for it.

    A virtual machine with virtio-gpu has no render node at all, and a laptop
    has two. Guessing wrong is not a visible failure - the fork accepts the
    adapter, fails to build the VAAPI pipeline and streams on the CPU - so the
    adapter is only named when `vainfo` says that node decodes H.264, and is
    left out entirely otherwise so the fork chooses for itself.
    """
    if adapter:
        return ["capture=wlr", "encoder=vaapi", f"adapter_name={adapter}"]
    nodes = render_nodes() if nodes is None else [Path(node) for node in nodes]
    if not nodes:
        return ["capture=wlr"]
    probe = probe or _vainfo
    for node in nodes:
        if probe(node):
            return ["capture=wlr", "encoder=vaapi", f"adapter_name={node}"]
    return ["capture=wlr"]


def _vainfo(node: Path) -> bool:
    if shutil.which("vainfo") is None:
        return False
    result = _run(["vainfo", "--display", "drm", "--device", str(node)])
    return result.returncode == 0 and "VAProfileH264" in (result.stdout or "")


def _systemctl(arguments, runner=None):
    return _run(["systemctl", "--user", *arguments], runner)


def unit_is_packaged(*, runner=None) -> bool:
    """True when something outside this user's unit directory provides the unit.

    On a machine with the distribution's `sunshine` package installed the unit
    exists in /usr/lib/systemd/user. That is a Sunshine somebody else set up,
    and RELEASE-9 no longer takes it over with a drop-in (see `unit_owner`).
    """
    result = _systemctl(["cat", SUNSHINE_UNIT], runner)
    if result.returncode != 0:
        return False
    for line in (result.stdout or "").splitlines():
        if line.startswith("# /") and UNIT_DIR not in line:
            return True
    return False


# RELEASE-9. What the unit was before this installer first wrote it, so that
# --remove disables only what Install enabled.
UNIT_RECORD = ".local/state/omodachi/sunshine-unit.json"
# `systemctl is-enabled` answers for which "enable" was this installer's doing.
_NOT_ENABLED = ("not-found", "disabled", "")


def _marked(path: Path) -> bool:
    """A regular file (never a link) that carries MARKER."""
    try:
        return not path.is_symlink() and path.is_file() and MARKER in path.read_text()
    except (OSError, UnicodeDecodeError):
        return False


def unit_owner(home: Path, *, runner=None) -> dict:
    """Whose Sunshine unit this is, deciding nothing and changing nothing.

    {"state": "absent" | "ours" | "foreign", "shape": "unit" | "dropin",
     "reason": why foreign, "strangers": drop-ins nobody here wrote}.
    """
    unit_path = home / UNIT_DIR / SUNSHINE_UNIT
    dropin_dir = home / UNIT_DIR / (SUNSHINE_UNIT + ".d")
    dropin = dropin_dir / DROPIN_NAME
    ours_dropin = _marked(dropin)
    strangers = []
    if dropin_dir.is_dir() and not dropin_dir.is_symlink():
        strangers = [str(child) for child in sorted(dropin_dir.iterdir())
                     if not (child == dropin and ours_dropin)]
    elif os.path.lexists(dropin_dir):
        strangers = [str(dropin_dir)]
    if os.path.lexists(unit_path):
        if _marked(unit_path):
            # Drop-ins beside our own unit are the user's customisation of it,
            # the way systemd intends; they are kept and never edited.
            return {"state": "ours", "shape": "unit", "reason": "", "strangers": strangers}
        return {"state": "foreign", "shape": "unit", "strangers": strangers,
                "reason": f"{unit_path} is a Sunshine unit this installer did not write"}
    if unit_is_packaged(runner=runner):
        if ours_dropin:
            return {"state": "ours", "shape": "dropin", "reason": "", "strangers": strangers}
        return {"state": "foreign", "shape": "dropin", "strangers": strangers,
                "reason": (f"Sunshine is already installed on this computer by something other "
                           f"than Omodachi ({SUNSHINE_UNIT} comes from outside {UNIT_DIR}, "
                           f"for example the distribution's sunshine package), and Omodachi "
                           f"does not take over a Sunshine it did not set up")}
    if strangers:
        return {"state": "foreign", "shape": "unit", "strangers": strangers,
                "reason": (f"{dropin_dir} holds drop-ins this installer did not write "
                           f"({', '.join(Path(name).name for name in strangers)}), which would "
                           f"change what the unit it writes runs")}
    return {"state": "absent", "shape": "unit", "reason": "", "strangers": [],
            "stale_dropin": ours_dropin}


def _write_text(path: Path, text: str, mode: int = 0o644) -> bool:
    """Write `text` through a new file and a rename; False when already so."""
    try:
        if not path.is_symlink() and path.is_file() and path.read_text() == text:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(handle, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(text)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return True


def _read_record(home: Path) -> dict:
    try:
        value = json.loads((home / UNIT_RECORD).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def web_locked(home: Path, *, runner=None) -> bool | None:
    """Whether the ExecStart systemd will run carries both web UI arguments.

    RELEASE-9: a drop-in beside our unit that sets its own ExecStart would drop
    them, so the lockdown is read back from what systemd resolved rather than
    assumed from what was written. None when systemd could not be asked.
    """
    result = _systemctl(["show", "-p", "ExecStart", "--value", SUNSHINE_UNIT], runner)
    text = result.stdout or "" if result.returncode == 0 else ""
    if "argv[]=" not in text:
        return None
    return (WEB_ONLY_THIS_COMPUTER in text
            and ("credentials_file=" + str(home / WEB_CREDENTIALS)) in text)


# RELEASE-9: the files the fork itself writes into its configuration directory
# (config.cpp: sunshine.conf, sunshine_state.json - its paired clients -,
# sunshine.log, credentials/). They are deleted by remove() only when that
# directory did not exist before this installer's first install, which
# ensure_unit records; otherwise they belong to a Sunshine that was here before.
SUNSHINE_CONFIG = ".config/sunshine"
FORK_FILES = ("sunshine_state.json", "sunshine.log", "credentials/cacert.pem",
              "credentials/cakey.pem")


def ensure_unit(home: Path, directory: Path, *, arguments=None, runner=None,
                restart: bool = True, config_created: bool | None = None) -> dict:
    """Write our unit (or our drop-in), reload, enable, and start it.

    The 2026-09-20 host incident was this step missing its middle word: the
    unit file existed and the binary was there, but nothing had ever run
    `enable`, so the fork was not in graphical-session.target.wants and never
    came back after a reboot. Remote then cached "unavailable" for the life of
    the daemon.

    RELEASE-9: only over a unit or drop-in that is ours or absent
    (`unit_owner`); a foreign one raises `sunshine_not_ours` and nothing is
    written. The first time, the unit's enabled/active state is recorded for
    `remove()`. `restart=False` restarts only if the unit text changed or the
    fork is not running.
    """
    owner = unit_owner(home, runner=runner)
    if owner["state"] == "foreign":
        raise SunshinePackageError("sunshine_not_ours", owner["reason"] + "; it was left as it is")
    arguments = encoder_arguments() if arguments is None else list(arguments)
    arguments = [argument for argument in arguments
                 if argument.partition("=")[0] not in WEB_ARGUMENT_KEYS] + web_arguments(home)
    unit_dir = home / UNIT_DIR
    unit_path = unit_dir / SUNSHINE_UNIT
    dropin = unit_dir / (SUNSHINE_UNIT + ".d") / DROPIN_NAME
    packaged = owner["shape"] == "dropin"
    if not _read_record(home):
        before = {"enabled": (_systemctl(["is-enabled", SUNSHINE_UNIT], runner).stdout or "").strip(),
                  "active": (_systemctl(["is-active", SUNSHINE_UNIT], runner).stdout or "").strip()}
        if owner["state"] == "ours":
            # An install from before RELEASE-9: a whole unit of ours did not
            # exist before we wrote it; a drop-in on somebody's unit, unknown.
            before = {"enabled": "not-found" if owner["shape"] == "unit" else "unknown",
                      "active": "unknown"}
        if config_created is None:
            config_created = not os.path.lexists(home / SUNSHINE_CONFIG)
        _write_text(home / UNIT_RECORD, json.dumps(
            {"schema": 1, "unit": SUNSHINE_UNIT, "shape": owner["shape"],
             "enabled_before": before["enabled"] or "not-found",
             "active_before": before["active"] or "unknown",
             "config_created": bool(config_created) and owner["state"] == "absent"},
            indent=1) + "\n", 0o600)
    if packaged:
        written = dropin
        changed = _write_text(dropin, dropin_text(directory, arguments))
    else:
        written = unit_path
        changed = _write_text(unit_path, unit_text(directory, arguments))
        if _marked(dropin):
            # Ours, from when a packaged unit was here; it would override the
            # ExecStart just written. Only that one file goes.
            dropin.unlink()
            _remove_empty(dropin.parent)
            changed = True
    if changed:
        _systemctl(["daemon-reload"], runner)
    enabled = _systemctl(["enable", SUNSHINE_UNIT], runner)
    state = (_systemctl(["is-active", SUNSHINE_UNIT], runner).stdout or "").strip()
    started = None
    if restart or changed or state != "active":
        started = _systemctl(["restart", SUNSHINE_UNIT], runner)
        state = (_systemctl(["is-active", SUNSHINE_UNIT], runner).stdout or "").strip()
    return {"unit": str(written), "packaged_unit": packaged, "arguments": arguments,
            "changed": changed, "strangers": owner["strangers"],
            "web_locked": web_locked(home, runner=runner),
            "enabled": enabled.returncode == 0,
            "enable_detail": (enabled.stderr or "").strip()[:200] or None,
            "started": started is None or started.returncode == 0,
            "restarted": started is not None,
            "start_detail": ((started.stderr or "").strip()[:200] or None) if started else None,
            "active": state or "unknown"}


def _remove_empty(directory: Path) -> None:
    try:
        if directory.is_dir() and not directory.is_symlink():
            directory.rmdir()
    except OSError:
        pass  # not empty: something in it is not ours


def install(spec: str, home: Path, *, sha256=None, cache=None, runner=None,
            opener=urllib.request.urlopen, adapter=None, config_created=None) -> dict:
    """The whole path: fetch, verify, unpack, dependencies, unit, enable.

    `sha256` is required: an archive is never unpacked unless it hashes to a
    value the caller - the version table or the user - supplied. A Sunshine
    unit somebody else set up stops it before anything is downloaded.
    """
    expected = (sha256 or "").strip().lower()
    if not _SHA256.fullmatch(expected):
        raise SunshinePackageError("sunshine_package_sha256_required",
                                   f"no sha256 to check {spec} against; nothing was downloaded")
    owner = unit_owner(home, runner=runner)
    if owner["state"] == "foreign":
        raise SunshinePackageError("sunshine_not_ours", owner["reason"]
                                   + "; it was left as it is and nothing was downloaded")
    cache = cache or (home / ".cache/omodachi/sunshine")
    archive = fetch(spec, cache, opener=opener)
    actual = digest(archive)
    if expected != actual:
        raise SunshinePackageError(
            "sunshine_package_checksum_mismatch",
            f"{spec} hashes to {actual}, not the {expected} it was pinned to")
    unpacked = unpack(archive, home)
    directory = Path(unpacked["directory"])
    bad = verify_manifest(directory)
    if bad:
        raise SunshinePackageError("sunshine_package_manifest_mismatch",
                                   f"{len(bad)} file(s) do not match MANIFEST.sha256: {bad[:3]}")
    packages = install_packages(package_depends(directory), runner=runner)
    unit = ensure_unit(home, directory, arguments=encoder_arguments(adapter=adapter), runner=runner,
                       config_created=config_created)
    source = {}
    try:
        for line in (directory / "SOURCE").read_text().splitlines():
            key, _, value = line.partition("=")
            if value:
                source[key] = value
    except OSError:
        pass
    return {"archive": str(archive), "sha256": actual, "checksum_pinned": True,
            "sha": unpacked["sha"], "directory": str(directory), "packages": packages,
            "unit": unit, "source": source}


# Names this module gives the transient directories it makes under
# INSTALL_ROOT (tempfile.mkdtemp: the prefix and eight of [a-z0-9_]).
_TRANSIENT = re.compile(r"\.(unpack|replaced)-[a-z0-9_]{8}\Z")


def _transient_pristine(directory: Path) -> bool:
    """RELEASE-10: one of those goes only when everything in it is exactly a
    release archive's contents (unpack()'s extracted tree, or the `old` tree
    it had checked before moving it aside) - or it is empty. A half-extracted
    tree, or anything somebody put there, is kept and listed."""
    try:
        return (directory.is_dir() and not directory.is_symlink()
                and all(entry.is_dir() and not entry.is_symlink() and not pristine(entry)
                        for entry in directory.iterdir()))
    except OSError:
        return False


def remove(home: Path, *, runner=None) -> dict:
    """Take back what install() made, and only that.

    RELEASE-9: the unit is stopped only when the unit or drop-in that starts
    it is ours, and disabled only when this installer enabled it (the record
    ensure_unit keeps, or, for an install from before that record, a whole
    unit of ours - which did not exist before it was written). Only our own
    unit file or drop-in is deleted, never the drop-in directory with anything
    else in it; only a `<sha>/` that is exactly an archive's contents goes.
    """
    unit_path = home / UNIT_DIR / SUNSHINE_UNIT
    dropin = home / UNIT_DIR / (SUNSHINE_UNIT + ".d") / DROPIN_NAME
    ours_unit, ours_dropin = _marked(unit_path), _marked(dropin)
    record = _read_record(home)
    removed = {"unit": False, "dropin": False, "installs": [], "kept": [],
               "stopped": False, "disabled": False, "web_credentials": False}
    if ours_unit or ours_dropin:
        _systemctl(["stop", SUNSHINE_UNIT], runner)
        removed["stopped"] = True
        before = record.get("enabled_before") if record else ("not-found" if ours_unit else "unknown")
        if before in _NOT_ENABLED:
            _systemctl(["disable", SUNSHINE_UNIT], runner)
            removed["disabled"] = True
        else:
            removed["left_enabled"] = (f"{SUNSHINE_UNIT} was {before} before Omodachi's install, "
                                       f"so it was not disabled")
        if ours_unit:
            unit_path.unlink()
            removed["unit"] = True
        if ours_dropin:
            dropin.unlink()
            removed["dropin"] = True
            _remove_empty(dropin.parent)
    elif os.path.lexists(unit_path) or os.path.lexists(dropin.parent):
        removed["unit_not_ours"] = ("the Sunshine unit here was not written by this installer, "
                                    "so it was not stopped, disabled or edited")
    root = home / INSTALL_ROOT
    if root.is_dir() and not root.is_symlink():
        for child in sorted(root.iterdir()):
            if (_TRANSIENT.fullmatch(child.name) and _transient_pristine(child)) \
                    or (ARCHIVE_NAME.match("omodachi-sunshine-" + child.name + "-x86_64")
                        and not pristine(child)):
                shutil.rmtree(child, ignore_errors=True)
                removed["installs"].append(child.name)
            else:
                removed["kept"].append(str(child))
        _remove_empty(root)
    removed["web_credentials"] = remove_web_credentials(home)
    config = home / SUNSHINE_CONFIG
    if record.get("config_created") and (ours_unit or ours_dropin):
        # ~/.config/sunshine did not exist before Omodachi's first install, so
        # what the fork wrote there - above all sunshine_state.json, the
        # clients it trusts - was Omodachi's Sunshine's, and goes with it.
        for name in FORK_FILES:
            path = config / name
            if not path.is_symlink() and path.is_file():
                path.unlink()
                removed.setdefault("fork_files", []).append(name)
        conf = config / "sunshine.conf"
        if not conf.is_symlink() and conf.is_file() and conf.stat().st_size == 0:
            conf.unlink()   # the fork creates it empty; one with settings is kept
        _remove_empty(config / "credentials")
    elif (ours_unit or ours_dropin) and (config / "sunshine_state.json").is_file():
        removed["state_kept"] = (f"{config / 'sunshine_state.json'} was there before Omodachi's "
                                 f"install, so it is kept; devices paired through Omodachi's "
                                 f"Sunshine stay in it until you delete it")
    try:
        (home / UNIT_RECORD).unlink()
    except OSError:
        pass
    if removed["unit"] or removed["dropin"]:
        _systemctl(["daemon-reload"], runner)
    return removed


def status(home: Path, *, runner=None) -> dict:
    """What is installed right now, for the report and for --remove to check."""
    root = home / INSTALL_ROOT
    installs = sorted(child.name for child in root.iterdir()
                      if child.is_dir()) if root.is_dir() else []
    state = _systemctl(["is-active", SUNSHINE_UNIT], runner)
    enabled = _systemctl(["is-enabled", SUNSHINE_UNIT], runner)
    return {"installs": installs, "active": (state.stdout or "").strip() or "unknown",
            "enabled": (enabled.stdout or "").strip() or "unknown"}


def describe(result: dict) -> str:
    return json.dumps(result, sort_keys=True)
