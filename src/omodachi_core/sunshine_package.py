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
here writes outside `~/.local/share/omodachi/sunshine` and
`~/.config/systemd/user`, and `remove()` takes back exactly those.

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
    """`sunshine_package` from the core version table: {version, url, sha256, satisfied_by}."""
    try:
        entry = json.loads(path.read_text())["sunshine_package"]
        version, url, sha256 = entry["version"], entry["url"], entry["sha256"]
        url = url.replace("{repository}", SUNSHINE_REPOSITORY) if isinstance(url, str) else url
        satisfied = entry.get("satisfied_by", [])
        if (not _COMMIT.fullmatch(version) or not isinstance(url, str) or "://" not in url
                or "/releases/latest/" in url or not re.fullmatch(r"[0-9a-f]{64}", sha256)
                or not isinstance(satisfied, list) or not all(isinstance(v, str) and _COMMIT.fullmatch(v)
                                                               for v in satisfied)):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise SunshinePackageError("sunshine_package_pin_invalid",
                                   f"{path} has no usable sunshine_package pin") from None
    return {"version": version, "url": url, "sha256": sha256, "satisfied_by": list(satisfied)}


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
                "version": pin["version"], "satisfied_by": pin["satisfied_by"]}
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
    try:
        written_by_us = MARKER in dropin.read_text() or MARKER in (home / UNIT_DIR / SUNSHINE_UNIT).read_text()
    except OSError:
        written_by_us = False
    enabled = _systemctl(["is-enabled", SUNSHINE_UNIT], runner)
    active = _systemctl(["is-active", SUNSHINE_UNIT], runner)
    return {"version": version, "directory": str(directory), "binary": str(binary),
            "written_by_installer": written_by_us,
            "enabled": (enabled.stdout or "").strip() or "unknown",
            "active": (active.stdout or "").strip() or "unknown"}


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
    previous = None
    if target.exists():
        previous = target.with_name(target.name + ".previous")
        shutil.rmtree(previous, ignore_errors=True)
        target.rename(previous)
    unpacked.rename(target)
    shutil.rmtree(staging, ignore_errors=True)
    if previous is not None:
        shutil.rmtree(previous, ignore_errors=True)
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


def _body(directory: Path, arguments) -> str:
    # WorkingDirectory is load-bearing, not tidiness: the packaged binary has
    # the *relative* path "assets" compiled in as SUNSHINE_ASSETS_DIR, which is
    # the only way one build can serve every user. Without this line the fork
    # finds no shaders, logs five compile errors and streams on the CPU.
    return "\n".join([
        f"WorkingDirectory={directory}",
        "ExecStart=" + " ".join([str(directory / "sunshine"), *arguments]),
        "Environment=SUNSHINE_MANAGED_LOCAL_PAIRING=1",
    ])


def unit_text(directory: Path, arguments) -> str:
    return UNIT_TEMPLATE.format(marker=MARKER, body=_body(directory, arguments))


def dropin_text(directory: Path, arguments) -> str:
    return DROPIN_TEMPLATE.format(marker=MARKER, body=_body(directory, arguments))


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
    """True when something outside this installer already provides the unit.

    On a machine with the distribution's `sunshine` package installed the unit
    exists in /usr/lib/systemd/user and the user's own copy would shadow it.
    There we write only a drop-in, exactly as a developer host has had since
    SPEC-B1; on a machine that has never had Sunshine we write the whole unit.
    """
    result = _systemctl(["cat", SUNSHINE_UNIT], runner)
    if result.returncode != 0:
        return False
    for line in (result.stdout or "").splitlines():
        if line.startswith("# /") and UNIT_DIR not in line:
            return True
    return False


def ensure_unit(home: Path, directory: Path, *, arguments=None, runner=None) -> dict:
    """Write the unit (or just its drop-in), reload, enable, and start it.

    The 2026-09-20 host incident was this step missing its middle word: the
    unit file existed and the binary was there, but nothing had ever run
    `enable`, so the fork was not in graphical-session.target.wants and never
    came back after a reboot. Remote then cached "unavailable" for the life of
    the daemon.
    """
    arguments = encoder_arguments() if arguments is None else arguments
    unit_dir = home / UNIT_DIR
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / SUNSHINE_UNIT
    dropin = unit_dir / (SUNSHINE_UNIT + ".d") / DROPIN_NAME
    packaged = unit_is_packaged(runner=runner)
    if packaged:
        # Our own full unit, if an earlier run wrote one, has to go first or it
        # keeps shadowing the packaged unit the drop-in is meant to amend.
        if unit_path.is_file() and MARKER in unit_path.read_text():
            unit_path.unlink()
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text(dropin_text(directory, arguments))
        dropin.chmod(0o644)
        written = str(dropin)
    else:
        shutil.rmtree(dropin.parent, ignore_errors=True)
        unit_path.write_text(unit_text(directory, arguments))
        unit_path.chmod(0o644)
        written = str(unit_path)
    _systemctl(["daemon-reload"], runner)
    enabled = _systemctl(["enable", SUNSHINE_UNIT], runner)
    started = _systemctl(["restart", SUNSHINE_UNIT], runner)
    state = _systemctl(["is-active", SUNSHINE_UNIT], runner)
    return {"unit": written, "packaged_unit": packaged, "arguments": arguments,
            "enabled": enabled.returncode == 0,
            "enable_detail": (enabled.stderr or "").strip()[:200] or None,
            "started": started.returncode == 0,
            "start_detail": (started.stderr or "").strip()[:200] or None,
            "active": (state.stdout or "").strip() or "unknown"}


def install(spec: str, home: Path, *, sha256=None, cache=None, runner=None,
            opener=urllib.request.urlopen, adapter=None) -> dict:
    """The whole path: fetch, verify, unpack, dependencies, unit, enable.

    `sha256` is required: an archive is never unpacked unless it hashes to a
    value the caller - the version table or the user - supplied.
    """
    expected = (sha256 or "").strip().lower()
    if not _SHA256.fullmatch(expected):
        raise SunshinePackageError("sunshine_package_sha256_required",
                                   f"no sha256 to check {spec} against; nothing was downloaded")
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
    unit = ensure_unit(home, directory, arguments=encoder_arguments(adapter=adapter), runner=runner)
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


def remove(home: Path, *, runner=None) -> dict:
    """Stop and disable the fork, and take back only what install() wrote."""
    _systemctl(["disable", "--now", SUNSHINE_UNIT], runner)
    removed = {"unit": False, "dropin": False, "installs": []}
    unit_path = home / UNIT_DIR / SUNSHINE_UNIT
    try:
        if MARKER in unit_path.read_text():
            unit_path.unlink()
            removed["unit"] = True
    except OSError:
        pass
    dropin = home / UNIT_DIR / (SUNSHINE_UNIT + ".d") / DROPIN_NAME
    try:
        if MARKER in dropin.read_text():
            dropin.unlink()
            removed["dropin"] = True
            dropin.parent.rmdir()
    except OSError:
        pass
    root = home / INSTALL_ROOT
    if root.is_dir():
        for child in sorted(root.iterdir()):
            # Only directories this installer could have made: a per-commit
            # tree with our SOURCE file in it. A developer's hand-built
            # directory has no SOURCE and is left where it is.
            if child.is_dir() and (child / "SOURCE").is_file():
                shutil.rmtree(child, ignore_errors=True)
                removed["installs"].append(child.name)
        try:
            root.rmdir()
        except OSError:
            pass
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
