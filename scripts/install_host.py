#!/usr/bin/env python3
"""Install omodachi-core on an Omarchy host, from this checkout.

    install_host.py --host alex@omarchy   # rsync this checkout, then install
    install_host.py --local                 # install from an already synced tree

The install is idempotent: it syncs the sources, reinstalls them into the
host virtualenv, generates the host certificate once, writes the user units,
wrappers and desktop entry, opens the LAN firewall rules, then reloads and
enables systemd. It never writes credentials or device state, never replaces an
existing certificate, and never touches ~/.config/omarchy, ~/.config/hypr or the
Sunshine unit. The one Sunshine file it does edit is apps.json, additively: the
fork has to publish the desktop entry core advertises or no stream can launch.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REMOTE_SOURCE = ".local/share/omodachi/src"

# The daemon listens on every interface so a companion on the LAN or over
# Tailscale reaches it directly. aiohttp binds one address family per site, so
# this is IPv4; IPv6 on the LAN is a known limitation of this revision.
DAEMON_UNIT = """[Unit]
Description=Omodachi host daemon
After=graphical-session.target

[Service]
Type=simple
# RELEASE-7b. The daemon's interpreter is started with -I, so nothing the user
# manager's environment carries (environment.d, `systemctl --user
# set-environment`) reaches it: PYTHONPATH, PYTHONHOME, PYTHONSTARTUP,
# PYTHONWARNINGS, PYTHONPYCACHEPREFIX and every other PYTHON* are ignored,
# neither venv/bin nor the working directory goes on sys.path, and no user
# site-packages or .pth file is read. The same variables are also taken out of
# the unit's environment, so no process the daemon starts inherits them.
UnsetEnvironment=PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE PYTHONPLATLIBDIR \\
  PYTHONPYCACHEPREFIX PYTHONWARNINGS PYTHONBREAKPOINT PYTHONINSPECT PYTHONEXECUTABLE
ExecStart=%h/.local/share/omodachi/venv/bin/python -I %h/.local/share/omodachi/venv/bin/omodachid \\
  --secret-file %h/.config/omodachi/device.secret \\
  --listen 0.0.0.0 --port 8099 \\
  --tls-cert %h/.config/omodachi/tls/server.pem \\
  --tls-key %h/.config/omodachi/tls/server.key
# AUTH-2: no --socket. The daemon decides, because two copies of a path is
# exactly how AUTH-1 ended up with a PAM helper looking in the wrong place:
# /run/omodachi/<uid> when the root step made it (the only kind of directory
# polkit's ProtectHome=yes sandbox can be given), and $XDG_RUNTIME_DIR/omodachi
# otherwise. RuntimeDirectory= makes the second one, at 0700, before ExecStart.
RuntimeDirectory=omodachi
RuntimeDirectoryMode=0700
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
"""

HERDR_UNIT = """[Unit]
Description=Omodachi owned persistent Herdr session

[Service]
Type=simple
WorkingDirectory=%h/.local/share/omodachi/agent-workspace
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/share/omarchy/bin
Environment=SHELL=/usr/bin/bash
ExecStart=/usr/bin/herdr --session omodachi server
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
"""

UNITS = {"omodachid.service": DAEMON_UNIT, "omodachi-herdr.service": HERDR_UNIT}
# The ~/.local/bin commands start the venv's console scripts the way the unit
# does: under -I, whatever the calling shell or hook has in its PYTHON*.
WRAPPER = ('#!/bin/sh\nexec "$HOME/.local/share/omodachi/venv/bin/python" -I '
           '"$HOME/.local/share/omodachi/venv/bin/%s" "$@"\n')

# The firewall rules copy omarchy-install-service-sunshine exactly: private
# CIDRs and tailscale0 only, one comment per subsystem so the rules can be
# found and removed again. Omodachi's network assumption is the same as
# Omarchy's - same LAN or Tailscale, never a relay.
CORE_PORT = "8099"
PRIVATE_CIDRS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
CORE_COMMENT = "omodachi-core"
SUNSHINE_COMMENT = "omodachi-sunshine"
SUNSHINE_TCP = "47984,47989,48010"
SUNSHINE_UDP = "47998:48000"
# Two hand-written rules scoped to one Mac, left behind by an earlier session.
LEGACY_COMMENTS = ("omadochi-mac-pair-check",)
MANAGED_COMMENTS = (CORE_COMMENT, SUNSHINE_COMMENT)
_NUMBERED = re.compile(r"^\[\s*(\d+)\]\s.*#\s*(\S+)\s*$")


def ufw(*arguments, check=True):
    return run(["sudo", "ufw", *arguments], check=check, capture_output=True, text=True)


def _tailscale_present() -> bool:
    return subprocess.run(["ip", "link", "show", "tailscale0"],
                          capture_output=True).returncode == 0


def _allow(proto: str, port: str, comment: str, *, tailscale: bool) -> None:
    for cidr in PRIVATE_CIDRS:
        ufw("allow", "in", "proto", proto, "from", cidr, "to", "any",
            "port", port, "comment", comment)
    if tailscale:
        ufw("allow", "in", "on", "tailscale0", "to", "any", "port", port,
            "proto", proto, "comment", comment)


def _delete_by_comment(comments) -> int:
    """Delete only rules carrying one of these comments, highest number first.

    Rule numbers shift on every delete, so they are collected once and spent in
    descending order. Anything with another comment - omodachi-dev-mac, the
    Omarchy sshd rule, the docker DNS rules - is never touched.
    """
    listing = ufw("status", "numbered").stdout
    targets = []
    for line in listing.splitlines():
        match = _NUMBERED.match(line.strip())
        if match and match.group(2) in comments:
            targets.append(int(match.group(1)))
    for number in sorted(targets, reverse=True):
        ufw("--force", "delete", str(number))
    return len(targets)


def configure_firewall() -> int:
    if shutil.which("ufw") is None:
        print("ufw is not installed; skipping Omodachi firewall rules.", file=sys.stderr)
        return 0
    tailscale = _tailscale_present()
    try:
        removed = _delete_by_comment(LEGACY_COMMENTS)
        if removed:
            print(f"removed {removed} legacy {LEGACY_COMMENTS[0]} rule(s)", flush=True)
        _allow("tcp", CORE_PORT, CORE_COMMENT, tailscale=tailscale)
        # The managed Sunshine fork gets the official port set, scoped the
        # official way rather than to one developer machine.
        _allow("tcp", SUNSHINE_TCP, SUNSHINE_COMMENT, tailscale=tailscale)
        _allow("udp", SUNSHINE_UDP, SUNSHINE_COMMENT, tailscale=tailscale)
        ufw("reload")
    except (OSError, subprocess.SubprocessError) as error:
        # A closed firewall is a reachability problem, not a broken install.
        print(f"firewall step failed, leaving the current rules alone: {error}", file=sys.stderr)
        return 0
    print("firewall rules for " + ", ".join(MANAGED_COMMENTS) + " are in place"
          + ("" if tailscale else " (no tailscale0 on this host)"), flush=True)
    return 0


def remove_firewall() -> int:
    if shutil.which("ufw") is None:
        print("ufw is not installed; nothing to remove.", file=sys.stderr)
        return 0
    try:
        removed = _delete_by_comment(MANAGED_COMMENTS)
        ufw("reload")
    except (OSError, subprocess.SubprocessError) as error:
        print(f"firewall removal failed: {error}", file=sys.stderr)
        return 1
    print(f"removed {removed} Omodachi firewall rule(s)", flush=True)
    return 0


def run(argv, **kwargs):
    print("+ " + " ".join(argv), flush=True)
    kwargs.setdefault("check", True)
    return subprocess.run(argv, **kwargs)


# RELEASE-7b. Every Python this installer starts runs under -I where it is
# started by name (venv creation, pip) and, either way, with an environment
# the installer chose rather than the caller's: no PYTHON* from outside
# (PYTHONPATH, PYTHONSTARTUP, PYTHONHOME, PYTHONWARNINGS, somebody else's
# PYTHONPYCACHEPREFIX...), no user site-packages and so no user .pth file, no
# bytecode written beside a source. The one value carried over is this
# interpreter's own bytecode prefix: the plugin bootstrap starts this file as
# `python3 -I -B -X pycache_prefix=<new empty private directory>`, and whatever
# it starts should read and write bytecode there too, not beside the sources.
def python_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("PYTHON")}
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if sys.pycache_prefix:
        environment["PYTHONPYCACHEPREFIX"] = sys.pycache_prefix
    return environment


def write(path: Path, text: str, mode=0o644) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == text and path.stat().st_mode & 0o777 == mode:
        return False
    path.write_text(text)
    path.chmod(mode)
    return True


# desktop-runtime.json outlived the SPEC-A desktop subsystem: recovery_output
# and devices belong to code that no longer exists, and journal_dir still
# pointed at the old per-session directory. The daemon already reads past those
# keys; the installer takes them out so the file says what is true.
RUNTIME_CONFIG = ".config/omodachi/desktop-runtime.json"
RUNTIME_DROP = ("recovery_output", "devices", "profiles")
OLD_JOURNAL_DIR = ".local/state/omodachi/desktop"
NEW_JOURNAL_DIR = ".local/state/omodachi/remote"


def migrate_runtime_config(home: Path) -> dict:
    """Rewrite the host runtime file in place. Never invents one that is absent."""
    path = home / RUNTIME_CONFIG
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"migrated": False, "reason": "absent_or_unreadable"}
    if not isinstance(value, dict):
        return {"migrated": False, "reason": "not_an_object"}
    before = json.dumps(value, sort_keys=True)
    for key in RUNTIME_DROP:
        value.pop(key, None)
    if str(value.get("journal_dir", "")).rstrip("/").endswith(OLD_JOURNAL_DIR):
        value["journal_dir"] = str(home / NEW_JOURNAL_DIR)
    changed = json.dumps(value, sort_keys=True) != before
    if changed:
        path.write_text(json.dumps(value, indent=2) + "\n")
        path.chmod(0o600)
    # Only an empty legacy directory is removed; real journals are left alone
    # for the user to look at or delete.
    old = home / OLD_JOURNAL_DIR
    removed = False
    if old.is_dir() and not any(old.iterdir()):
        old.rmdir()
        removed = True
    return {"migrated": changed, "legacy_journal_dir_removed": removed,
            "legacy_journal_dir_present": old.is_dir()}


# `~/.config/omodachi/omodachi-menu.jsonc` is the user's own third menu layer,
# and core loads it on top of the packaged one. SPEC-A's rename copied the
# codex-era test file `omadochi-menu.jsonc` to the new name instead of retiring
# it, so every host that went through that rename now publishes a second
# "Omadochi" branch of dead IDs next to the real one. The file is only ever
# retired when it is *entirely* that file - every top-level key in the
# `omadochi` namespace - so a host where somebody wrote their own entries keeps
# them untouched.
USER_MENU = ".config/omodachi/omodachi-menu.jsonc"
USER_MENU_BACKUP = ".config/omodachi/omodachi-menu.jsonc.codex-bak"
LEGACY_MENU_PREFIX = "omadochi"


def migrate_user_menu(home: Path, source: Path = ROOT) -> dict:
    """Rename the stale codex menu layer aside. Never deletes, never rewrites."""
    path = home / USER_MENU
    if not path.is_file():
        return {"renamed": False, "reason": "absent"}
    sys.path.insert(0, str(source / "src"))
    from omodachi_core.catalog import load_jsonc
    try:
        value = load_jsonc(path)
    except (OSError, ValueError):
        return {"renamed": False, "reason": "unreadable"}
    if not isinstance(value, dict) or not value:
        return {"renamed": False, "reason": "not_an_object"}
    if not all(key == LEGACY_MENU_PREFIX or key.startswith(LEGACY_MENU_PREFIX + ".")
               for key in value):
        return {"renamed": False, "reason": "not_the_codex_file"}
    backup = home / USER_MENU_BACKUP
    if backup.exists():
        # A previous install already kept one; this file is still not ours to
        # delete, so it is set aside under a numbered name instead.
        index = 2
        while (home / (USER_MENU_BACKUP + f".{index}")).exists():
            index += 1
        backup = home / (USER_MENU_BACKUP + f".{index}")
    path.rename(backup)
    return {"renamed": True, "reason": "codex_test_menu", "entries": len(value),
            "backup": str(backup)}


# The fork's app catalogue is the contract between the desktop entry core
# advertises and the one a client can actually launch: `OMRemoteClient` looks up
# `connection.app_name` in the fork's applist, and core fills that field from
# `SUNSHINE_APP_NAME`. A host whose apps.json never publishes that name strands
# every Sunshine session at `launching` (SPEC-E3 §7). The name is imported from
# that one constant rather than repeated here, so the two cannot drift.
SUNSHINE_APPS = ".config/sunshine/apps.json"
SUNSHINE_APPS_BACKUP = ".config/sunshine/apps.json.omodachi-bak"
SUNSHINE_APP_IMAGE = "desktop.png"
SUNSHINE_UNIT = "app-dev.lizardbyte.app.Sunshine.service"


# INSTALL-1. The panel talks to the daemon over a Unix socket as a device, with
# a device credential, exactly like an iPhone does - and nothing had ever issued
# it. On this developer host the file was made by hand a long time ago; on a
# clean machine `omarchy plugin add` put the panel there, Install gave it a
# daemon, and the panel still said "This panel needs permission from the host
# service" for ever, because there was no step anywhere that could give it one.
PLUGIN_TOKEN = ".config/omodachi/plugin.token"


def ensure_plugin_credential(source: Path, home: Path | None = None) -> dict:
    """Issue the panel's own device credential, once, and never replace it.

    An existing file is left exactly as it is: re-issuing would hand the panel
    a second identity and leave the first one in `devices list` for ever. The
    file is 0600 and is written through a temporary file, because
    `plugin_bridge.plugin_credential` refuses anything a group could read.
    """
    home = home or Path.home()
    path = home / PLUGIN_TOKEN
    if path.exists():
        return {"issued": False, "reason": "already_present", "path": str(path)}
    sys.path.insert(0, str(source / "src"))
    from omodachi_core.protocol import PLUGIN_DEVICE_ID
    from omodachi_core.auth import DeviceAuthenticator
    secret = home / ".config/omodachi/device.secret"
    try:
        token = DeviceAuthenticator.from_file(str(secret)).issue(PLUGIN_DEVICE_ID).token
    except Exception as error:
        return {"issued": False, "reason": type(error).__name__, "path": str(path)}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(token + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    return {"issued": True, "reason": "issued", "device_id": PLUGIN_DEVICE_ID, "path": str(path)}


def _sunshine_package(source: Path = ROOT):
    """The packaged Sunshine installer, imported from the synced sources."""
    sys.path.insert(0, str(source / "src"))
    from omodachi_core import sunshine_package
    return sunshine_package


def install_sunshine(source: Path, *, spec=None, sha256=None, adapter=None) -> dict:
    """Put the managed fork on this host and make systemd keep it running.

    INSTALL-1. Before this, the installer only *looked* at the fork: a user who
    had never built it got a Remote with no Sunshine backend and an installer
    that said nothing about why. The archive is the fork's own
    `scripts/package_release.sh` output, pinned by sha256; `--sunshine-build`
    is the documented fallback for a host with no published build for it.
    """
    package = _sunshine_package(source)
    try:
        choice = package.choose(spec, sha256)
    except package.SunshinePackageError as error:
        print(f"Sunshine was not installed ({error.code}): {error.detail}", file=sys.stderr)
        return {"installed": False, "reason": error.code, "detail": error.detail}
    # CORE-2 §4. A host that already runs a managed fork satisfying the pin is
    # left exactly as it is: nothing downloaded, the unit and its drop-in not
    # rewritten, the fork not restarted (a restart drops any stream in
    # flight). Before this every install on a developer host went to GitHub
    # for `releases/latest`, got a 404 and printed a failure.
    if choice["source"] == "pin":
        present = package.installed_fork(Path.home())
        if present is not None and package.satisfies(present["version"], choice):
            same = present["version"][:7] == choice["version"][:7]
            print(f"the managed Sunshine fork {present['version'][:12]} is already what "
                  f"{package.SUNSHINE_UNIT} runs ({present['binary']}, "
                  f"{'written by this installer' if present['written_by_installer'] else 'a drop-in this installer did not write'}, "
                  f"active={present['active']}, enabled={present['enabled']}); "
                  + ("it is the pinned build" if same else
                     f"it satisfies the pinned {choice['version']} (same runtime sources)")
                  + ", so nothing was downloaded and the unit was left alone", flush=True)
            if present["enabled"] != "enabled":
                enabled = package._systemctl(["enable", package.SUNSHINE_UNIT])
                print(f"enabled {package.SUNSHINE_UNIT} so it comes back after a reboot"
                      if enabled.returncode == 0 else
                      f"could not enable {package.SUNSHINE_UNIT}: {(enabled.stderr or '').strip()[:200]}",
                      flush=True)
            return {"installed": False, "reason": "already_installed", "version": present["version"],
                    "pinned": choice["version"], "binary": present["binary"]}
    spec, sha256 = choice["spec"], choice["sha256"]
    print(f"+ installing the managed Sunshine fork from {spec}"
          + (f" (pinned {choice['version']}, sha256 {sha256[:12]}…)" if choice["source"] == "pin" else
             f" (the newest release, asked for by name, sha256 {sha256[:12]}…)" if choice["source"] == "latest" else
             f" (sha256 {sha256[:12]}…, given with --sunshine-sha256)"),
          flush=True)
    try:
        result = package.install(spec, Path.home(), sha256=sha256, adapter=adapter)
    except package.SunshinePackageError as error:
        print(f"Sunshine was not installed ({error.code}): {error.detail}", file=sys.stderr)
        print("Remote's VNC mode still works. Re-run with --sunshine-package <url|path> once you "
              "have an archive, or with --sunshine-build to build the fork from source here.",
              file=sys.stderr)
        return {"installed": False, "reason": error.code, "detail": error.detail}
    unit = result["unit"]
    print(f"installed {result['sha']} into {result['directory']} (sha256 {result['sha256'][:12]}… checked)",
          flush=True)
    if result["packages"]["installed"]:
        print("installed host packages: " + ", ".join(result["packages"]["installed"]), flush=True)
    elif result["packages"]["reason"] == "package_install_failed":
        print("could not install " + ", ".join(result["packages"]["missing"]) + ": "
              + result["packages"]["detail"] + f"\nrun `{result['packages']['command']}` yourself, "
              "then restart " + package.SUNSHINE_UNIT, file=sys.stderr)
    print(f"{'drop-in' if unit['packaged_unit'] else 'unit'} {unit['unit']}, "
          f"enabled={unit['enabled']}, active={unit['active']}, "
          f"arguments={' '.join(unit['arguments'])}", flush=True)
    if "encoder=vaapi" not in unit["arguments"]:
        # A machine with no VAAPI render node - a VM, or a GPU whose driver is
        # not loaded - streams on the CPU. That is a working stream, and saying
        # so here is the difference between a known limit and a mystery.
        print("no render node answered for VAAPI, so the fork will encode in software.",
              flush=True)
    if not unit["enabled"]:
        print("the unit could not be enabled: " + str(unit["enable_detail"])
              + "\nRemote will stop working at the next reboot until it is.", file=sys.stderr)
    result["installed"] = True
    return result


SUNSHINE_BUILD_CACHE = ".cache/omodachi/sunshine-src"
# The same guard the plugin bootstrap uses (RELEASE-5): a checkout's own hooks,
# fsmonitor and replace refs get no say in what this installer asks git.
GIT_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "--no-replace-objects")
_FULL_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def is_git_url(checkout: str) -> bool:
    return "://" in checkout or bool(re.match(r"^[\w.-]+@[\w.-]+:", checkout))


def sunshine_build_refusal(checkout, commit) -> str:
    """Why a --sunshine-build request is refused before anything runs, or ""."""
    if checkout is None:
        return "--sunshine-build-commit only goes with --sunshine-build <git-url>" if commit else ""
    if is_git_url(checkout):
        if not commit:
            return (f"--sunshine-build {checkout} is a git URL, so it needs the exact commit to "
                    f"build: add --sunshine-build-commit <40-hex sha>. A branch or HEAD is "
                    f"never built")
        if not _FULL_COMMIT.fullmatch(commit):
            return f"--sunshine-build-commit {commit!r} is not a full 40-character commit sha"
        return ""
    if commit:
        return ("--sunshine-build-commit only goes with a git URL; a local --sunshine-build "
                "path is built as it stands, as your own tree")
    return ""


def _git(root: Path, *arguments, check=False):
    return run(["git", *GIT_SAFE, "-C", str(root), *arguments], check=check,
               capture_output=True, text=True)


def fetch_sunshine_commit(url: str, commit: str, root: Path) -> tuple[bool, str]:
    """Put exactly `commit` of `url`, detached and clean, at `root`.

    `root` is this installer's own cache, so whatever is there is thrown away
    rather than trusted. Returns (ok, detail); ok only when HEAD is the commit,
    its tree is the commit's tree and there is nothing else in the work tree.
    """
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    if run(["git", "init", "--quiet", str(root)], check=False).returncode != 0:
        return False, "git init failed"
    fetched = _git(root, "fetch", "--depth", "1", url, commit)
    if fetched.returncode != 0:
        return False, (fetched.stderr or "").strip()[:300] or f"could not fetch {commit} from {url}"
    got = (_git(root, "rev-parse", "FETCH_HEAD^{commit}").stdout or "").strip()
    if got != commit:
        return False, f"{url} answered {got or 'nothing'} for {commit}"
    if _git(root, "checkout", "--quiet", "--force", "--detach", commit).returncode != 0:
        return False, f"could not check out {commit}"
    _git(root, "submodule", "update", "--init", "--recursive", "--depth", "1")
    return verify_sunshine_checkout(root, commit)


def verify_sunshine_checkout(root: Path, commit: str) -> tuple[bool, str]:
    head = (_git(root, "rev-parse", "--verify", "HEAD^{commit}").stdout or "").strip()
    if head != commit:
        return False, f"HEAD is {head or 'unknown'}, not the requested {commit}"
    trees = [(_git(root, "rev-parse", "--verify", f"{name}^{{tree}}").stdout or "").strip()
             for name in ("HEAD", commit)]
    if not trees[0] or trees[0] != trees[1]:
        return False, "the checked-out tree is not the commit's tree"
    dirty = (_git(root, "status", "--porcelain", "--untracked-files=all").stdout or "").strip()
    if dirty:
        return False, "the checkout is not clean: " + "; ".join(dirty.splitlines()[:5])
    return True, ""


def build_sunshine(checkout: str, *, commit=None, jobs=4) -> dict:
    """Fallback: build the fork here with its own release script.

    `checkout` is a local tree or a git URL. A git URL is built only at the
    exact `commit` asked for - fetched detached into this installer's cache and
    checked (HEAD, tree, clean) immediately before its script runs - never a
    branch head. A local path is the user's own tree and is built as it stands;
    the fork's script itself refuses a dirty tree for a release. This needs the
    fork's build dependencies (FORK.md); it is the documented answer for an
    architecture or a host with no published archive, not the ordinary path.
    """
    refusal = sunshine_build_refusal(checkout, commit)
    if refusal:
        print(refusal, file=sys.stderr)
        return {"built": False, "reason": "sunshine_build_refused"}
    if is_git_url(checkout):
        source = Path.home() / SUNSHINE_BUILD_CACHE
        ok, detail = fetch_sunshine_commit(checkout, commit, source)
        if ok:
            # Last check before the checkout's own script runs.
            ok, detail = verify_sunshine_checkout(source, commit)
        if not ok:
            print(f"not building {checkout} at {commit}: {detail}", file=sys.stderr)
            return {"built": False, "reason": "sunshine_build_unverified"}
    else:
        source = Path(checkout).expanduser().resolve()
    script = source / "scripts/package_release.sh"
    if not script.is_file():
        print(f"{script} is missing; that checkout is not the Omodachi Sunshine fork",
              file=sys.stderr)
        return {"built": False, "reason": "no_package_script"}
    result = run(["bash", str(script)], check=False,
                 env=dict(os.environ, OMODACHI_BUILD_JOBS=str(jobs)))
    if result.returncode != 0:
        return {"built": False, "reason": "build_failed"}
    dist = Path(os.environ.get("OMODACHI_PACKAGE_OUT",
                               str(Path.home() / ".cache/omodachi-sunshine-build/dist")))
    archives = sorted(dist.glob("omodachi-sunshine-*-x86_64.tar.zst"),
                      key=lambda path: path.stat().st_mtime)
    if not archives:
        return {"built": False, "reason": "no_archive"}
    return {"built": True, "archive": str(archives[-1])}


def install_vnc(source: Path) -> dict:
    """Install the optional WayVNC backend, the same way the fork is installed.

    INSTALL-1: `scripts/install_wayvnc.py` has been in this repository since
    SPEC-E2 and nothing ever ran it, so a clean machine answered
    `wayvnc_0_10_1_required` for the one Remote backend that needs no GPU -
    which is exactly the backend a machine whose GPU cannot capture falls back
    to. That script's own installer uses `sudo -n`, which is right for an ssh
    deploy and wrong here: the panel runs this in a *visible terminal*, where
    a password prompt is a thing the user can answer. So its version gate is
    used as a probe and the install itself goes through the same helper the
    Sunshine dependencies use. Failure is never fatal and always names the
    command to run by hand.
    """
    script = source / "scripts/install_wayvnc.py"
    if not script.is_file():
        return {"available": False, "reason": "install_wayvnc_missing"}
    import importlib.util
    spec = importlib.util.spec_from_file_location("install_wayvnc", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def probe():
        try:
            return module.install_dependency(install=False)
        except module.WayVNCInstallError as error:
            return {"available": False, "reason": error.code}

    result = probe()
    if result.get("available") and result.get("installed"):
        print("kept wayvnc " + str(result.get("version")), flush=True)
        return result
    if result.get("available"):
        packages = _sunshine_package(source).install_packages(["wayvnc"])
        if packages["reason"] == "package_install_failed":
            print("could not install wayvnc: " + packages["detail"]
                  + "\nrun `" + packages["command"] + "` yourself and press Install again.",
                  file=sys.stderr)
            return {"available": False, "reason": "wayvnc_package_install_failed"}
        result = probe()
        if result.get("installed"):
            print("installed wayvnc " + str(result.get("version")), flush=True)
            return result
    print(f"the VNC backend is not installed ({result.get('reason', 'not_installed')}). "
          f"Remote's Sunshine mode is unaffected; to add VNC run "
          f"`sudo pacman -S --needed wayvnc`.", file=sys.stderr)
    return result


def check_sunshine_assets(source: Path = ROOT) -> dict:
    """Report where the managed fork is and whether its assets are beside it.

    This installer does not build the fork and does not write its unit; the
    build and the drop-in belong to the release spec and to the user. What it
    can do is say out loud when the running build has no asset tree, because
    the fork does not: it logs five shader compile errors at startup and then
    streams on the CPU as if nothing happened (PERF-3).
    """
    sys.path.insert(0, str(source / "src"))
    from omodachi_core.remote.backends import (managed_sunshine_assets, SUNSHINE_INSTALL_ROOT,
                                               SUNSHINE_SHADERS)
    result = managed_sunshine_assets()
    result["expected_root"] = str(Path.home() / SUNSHINE_INSTALL_ROOT / "<sha>")
    result["probed"] = (str(Path(result["executable"]).parent / SUNSHINE_SHADERS)
                        if result["executable"] else None)
    return result


def _sunshine_app_name(source: Path = ROOT) -> str:
    """Read the advertised entry name out of the packaged sources."""
    sys.path.insert(0, str(source / "src"))
    from omodachi_core.remote.backends import SUNSHINE_APP_NAME
    return SUNSHINE_APP_NAME


def ensure_sunshine_app(home: Path, app_name: str) -> dict:
    """Publish core's desktop entry in the host apps.json, additively.

    Every existing app, the `env` block and every other key are carried over
    unchanged; the entry is appended only when no app already answers to this
    name. A file that is missing is created holding just this app. A file that
    exists but cannot be read or is not the shape Sunshine writes is left
    completely alone - it is the user's Sunshine configuration, and a broken
    one is a thing to report, not to overwrite. The replacement is atomic, and
    the first write that changes anything leaves the original behind as
    apps.json.omodachi-bak.
    """
    path = home / SUNSHINE_APPS
    entry = {"name": app_name, "image-path": SUNSHINE_APP_IMAGE}
    existed = path.exists()
    if existed:
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError):
            return {"changed": False, "reason": "unreadable", "backup": False}
        if not isinstance(document, dict) or not isinstance(document.get("apps"), list):
            return {"changed": False, "reason": "unexpected_shape", "backup": False}
        apps = document["apps"]
        if any(isinstance(app, dict) and app.get("name") == app_name for app in apps):
            return {"changed": False, "reason": "already_published", "backup": False}
        apps.append(entry)
    else:
        document = {"apps": [entry]}

    # 4-space indent with sorted keys is byte-for-byte what the fork's own
    # writer (`confighttp.cpp`, nlohmann `dump(4)`) produces, so a later save
    # from its web UI does not reshuffle the whole file.
    text = json.dumps(document, indent=4, sort_keys=True, ensure_ascii=False) + "\n"
    backup = home / SUNSHINE_APPS_BACKUP
    wrote_backup = False
    path.parent.mkdir(parents=True, exist_ok=True)
    if existed and not backup.exists():
        shutil.copy2(path, backup)
        wrote_backup = True
    temporary = path.with_name(path.name + ".omodachi-new")
    temporary.write_text(text)
    temporary.chmod(0o644)
    os.replace(temporary, path)
    return {"changed": True, "reason": "created" if not existed else "appended",
            "backup": wrote_backup}


def remove_sunshine_app(home: Path, app_name: str) -> dict:
    """Take our one entry back out of the user's apps.json, and nothing else.

    `ensure_sunshine_app` appends one app; removal has to be exactly that,
    reversed. Every other app, the `env` block and every other key are carried
    over unchanged, and a file that is not the shape the fork writes is left
    completely alone - it is the user's configuration and a broken one is a
    thing to report, not to rewrite.
    """
    path = home / SUNSHINE_APPS
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"changed": False, "reason": "absent_or_unreadable"}
    if not isinstance(document, dict) or not isinstance(document.get("apps"), list):
        return {"changed": False, "reason": "unexpected_shape"}
    kept = [app for app in document["apps"]
            if not (isinstance(app, dict) and app.get("name") == app_name)]
    if len(kept) == len(document["apps"]):
        return {"changed": False, "reason": "not_published"}
    document["apps"] = kept
    text = json.dumps(document, indent=4, sort_keys=True, ensure_ascii=False) + "\n"
    temporary = path.with_name(path.name + ".omodachi-new")
    temporary.write_text(text)
    temporary.chmod(0o644)
    os.replace(temporary, path)
    # The backup only existed to undo this one append. Once it is undone, a
    # stale copy of the user's Sunshine configuration is just clutter - but
    # only if it is still identical to what we have now.
    backup = home / SUNSHINE_APPS_BACKUP
    removed_backup = False
    try:
        if backup.is_file() and json.loads(backup.read_text()) == document:
            backup.unlink()
            removed_backup = True
    except (OSError, ValueError):
        pass
    return {"changed": True, "reason": "removed", "backup_removed": removed_backup}


# --- the Omarchy surfaces Omodachi owns -------------------------------------
# Three files, all of them ours: one theme template, two hook scripts. Nothing
# else under ~/.config/omarchy is read-modify-written, and --remove takes back
# exactly these three.
ENV_BOOTSTRAP = "/usr/share/omarchy/default/bash/env-bootstrap"
THEMED_DIR = ".config/omarchy/themed"
THEME_TEMPLATE = "omodachi-theme.json.tpl"
OMARCHY_STATE = ".local/state/omarchy/current"
HOOK_SOURCE_DIR = ".local/share/omodachi/hooks"
HOOK_NAME = "omodachi"
HOOK_COMMANDS = {"theme-set": "theme-changed", "font-set": "font-changed"}
HOOK_MARKER = "# Installed by omodachi-core (scripts/install_host.py)."
HOOK_SCRIPT = """#!/bin/bash
{marker}
# Omarchy runs this after {hook}. It only tells the daemon to re-read the host;
# the daemon owns what that means. It must never block the hook chain.
exec timeout 10 "$HOME/.local/bin/omodachi-host" {command} >/dev/null 2>&1
"""


def hook_script(hook: str) -> str:
    return HOOK_SCRIPT.format(marker=HOOK_MARKER, hook=hook, command=HOOK_COMMANDS[hook])


def ensure_theme_template(home: Path, source: Path) -> dict:
    """Publish our template where omarchy-theme-set-templates already looks.

    User templates under ~/.config/omarchy/themed are the documented extension
    point for "theming apps Omarchy doesn't cover", and they are rendered before
    the packaged ones. The file is ours alone, so it is written whole.
    """
    body = (source / "src/omodachi_core/data" / THEME_TEMPLATE).read_text()
    target = home / THEMED_DIR / THEME_TEMPLATE
    return {"path": str(target), "changed": write(target, body)}


def render_current_theme(home: Path, *, force: bool = False) -> dict:
    """Make the current theme render our template too, without changing it.

    omarchy-theme-set-templates renders out of the `next-theme` staging
    directory a theme switch builds, so the supported way to regenerate for the
    theme already in use is to re-apply that same theme. Headless mode skips the
    wallpaper, the shell IPC, the sixteen app restarts and the hooks: the only
    effect is that current/theme is rebuilt, with our file in it.
    """
    rendered = home / OMARCHY_STATE / "theme" / THEME_TEMPLATE.removesuffix(".tpl")
    if rendered.is_file() and not force:
        return {"rendered": True, "reason": "already_present", "path": str(rendered)}
    if shutil.which("omarchy-theme-set") is None:
        return {"rendered": False, "reason": "omarchy_theme_set_missing"}
    try:
        name = (home / OMARCHY_STATE / "theme.name").read_text().strip()
    except OSError:
        return {"rendered": False, "reason": "no_current_theme"}
    if not name:
        return {"rendered": False, "reason": "no_current_theme"}
    environment = dict(os.environ, OMARCHY_THEME_HEADLESS="1", OMARCHY_THEME_SKIP_BACKGROUND="1")
    # omarchy-theme-set resolves the theme directory through OMARCHY_PATH, and
    # an ssh command shell has not necessarily been through an rc file. Omarchy
    # names env-bootstrap as the single source of truth for that variable, so
    # the command is run the way Omarchy's own non-login shells run it.
    result = run(["bash", "-c",
                  '[ -r ' + ENV_BOOTSTRAP + ' ] && . ' + ENV_BOOTSTRAP + '; exec omarchy-theme-set "$1"',
                  "omodachi-install", name],
                 check=False, env=environment, capture_output=True, text=True)
    if result.returncode != 0:
        return {"rendered": False, "reason": "omarchy_theme_set_failed",
                "detail": ((result.stderr or "") + (result.stdout or "")).strip()[:200]}
    return {"rendered": rendered.is_file(), "reason": "regenerated", "theme": name,
            "path": str(rendered)}


def ensure_hooks(home: Path) -> dict:
    """Install both hooks through `omarchy hook install`, never by hand.

    The official installer is the one that decides where a hook lives and what
    mode it carries; Omodachi only supplies the file and the name.
    """
    installer = shutil.which("omarchy-hook-install")
    installed = {}
    for hook in sorted(HOOK_COMMANDS):
        master = home / HOOK_SOURCE_DIR / hook / HOOK_NAME
        write(master, hook_script(hook), 0o755)
        target = home / ".config/omarchy/hooks" / (hook + ".d") / HOOK_NAME
        if installer is None:
            installed[hook] = "omarchy_hook_install_missing"
            continue
        result = run([installer, hook, str(master)], check=False, capture_output=True, text=True)
        installed[hook] = ("installed" if result.returncode == 0 and target.is_file()
                           else (result.stderr or "failed").strip()[:120])
    return installed


def remove_omarchy_surfaces(home: Path) -> dict:
    """Take back exactly what ensure_* put there, and nothing a user wrote."""
    removed = {"template": False, "hooks": {}, "rendered": False}
    template = home / THEMED_DIR / THEME_TEMPLATE
    if template.is_file():
        template.unlink()
        removed["template"] = True
    rendered = home / OMARCHY_STATE / "theme" / THEME_TEMPLATE.removesuffix(".tpl")
    if rendered.is_file():
        # Generated state, regenerated on the next theme switch; leaving it
        # behind would keep answering for a daemon that is gone.
        rendered.unlink()
        removed["rendered"] = True
    for hook in sorted(HOOK_COMMANDS):
        target = home / ".config/omarchy/hooks" / (hook + ".d") / HOOK_NAME
        try:
            owned = HOOK_MARKER in target.read_text()
        except OSError:
            removed["hooks"][hook] = "absent"
            continue
        if not owned:
            # Somebody else's hook happens to share the name: not ours to delete.
            removed["hooks"][hook] = "not_ours"
            continue
        target.unlink()
        removed["hooks"][hook] = "removed"
    master = home / HOOK_SOURCE_DIR
    if master.is_dir():
        shutil.rmtree(master)
    return removed


# INSTALL-1 §1.4. `omarchy plugin remove` takes the panel away and leaves
# everything the panel installed: a daemon, a Herdr session, a venv, two
# wrappers, a desktop entry, the Sunshine fork, the firewall rules and the
# Omarchy surfaces. This is the matching entry point, and `--purge` is the only
# thing that touches ~/.config/omodachi, because that directory holds the
# device secret, the host certificate and every pairing - a user who removes
# the host to reinstall it should not have to pair every device again.
REMOVED_UNITS = ("omodachid.service", "omodachi-herdr.service", "omodachi-agent.service")
CONFIG_DIR = ".config/omodachi"


def remove_local(*, purge=False, sunshine=True) -> int:
    home = Path.home()
    removed = {"units": {}, "wrappers": [], "venv": False, "sources": False,
               "desktop_entry": [], "sunshine": None, "purged": False}
    for unit in REMOVED_UNITS:
        state = run(["systemctl", "--user", "is-active", unit], check=False,
                    capture_output=True, text=True).stdout.strip()
        run(["systemctl", "--user", "disable", "--now", unit], check=False,
            capture_output=True, text=True)
        path = home / ".config/systemd/user" / unit
        existed = path.is_file()
        if existed:
            path.unlink()
        removed["units"][unit] = {"was": state or "unknown", "unit_file_removed": existed}

    if sunshine:
        removed["sunshine"] = _sunshine_package(ROOT).remove(home)
        try:
            removed["sunshine_app"] = remove_sunshine_app(home, _sunshine_app_name(
                home / REMOTE_SOURCE if (home / REMOTE_SOURCE / "src").is_dir() else ROOT))
        except Exception as error:
            removed["sunshine_app"] = {"changed": False, "reason": type(error).__name__}

    # The desktop entry, its icon and the canonical wrapper are the launcher's
    # own three files; it knows which they are, so it is asked rather than
    # guessed at.
    sys.path.insert(0, str(home / REMOTE_SOURCE / "src"))
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from omodachi_core import desktop_launcher
        for row in desktop_launcher.inspect(home):
            path = Path(row["path"])
            if row["status"] == "installed" and path.is_file():
                path.unlink()
                removed["desktop_entry"].append(row["path"])
    except Exception as error:  # a half-removed install must still finish
        removed["desktop_entry"] = f"skipped: {type(error).__name__}"

    for name in ("omodachid", "omodachi-host"):
        wrapper = home / ".local/bin" / name
        try:
            if wrapper.is_file() and "share/omodachi/venv" in wrapper.read_text():
                wrapper.unlink()
                removed["wrappers"].append(str(wrapper))
        except OSError:
            pass

    share = home / ".local/share/omodachi"
    # AUTH-1's PAM entry is root-owned and only `--remove-pam` can take it
    # back - and `--remove-pam` runs the packaged pam_install.py out of these
    # very sources. Deleting them while the entry is still installed would
    # leave a PAM line pointing at a helper nobody can uninstall any more, and
    # the closing advice would name a path that had just been deleted. So the
    # sources stay until the PAM entry is gone.
    pam_installed = Path("/etc/omodachi/pam.conf").exists()
    removed["pam_entry_present"] = pam_installed
    shutil.rmtree(share / "venv.previous", ignore_errors=True)
    for child, key in ((share / "venv", "venv"), (share / "src", "sources")):
        if key == "sources" and pam_installed:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
            removed[key] = True
    if pam_installed:
        print("the device-approval PAM entry is still installed, so the sources were kept.\n"
              f"  python3 {share / 'src/scripts/install_host.py'} --local --remove-pam\n"
              "then run this uninstall again to finish.", flush=True)

    removed["omarchy"] = remove_omarchy_surfaces(home)
    remove_firewall()
    run(["systemctl", "--user", "daemon-reload"], check=False)

    if purge:
        for directory in (home / CONFIG_DIR, home / ".cache/omodachi",
                          home / ".local/state/omodachi", share):
            if directory.is_dir():
                shutil.rmtree(directory, ignore_errors=True)
        removed["purged"] = True
    else:
        # agent-workspace is the user's own files; the rest of the tree is ours.
        for leftover in ("hooks",):
            shutil.rmtree(share / leftover, ignore_errors=True)
        print(f"kept {home / CONFIG_DIR} (device secret, certificate, pairings). "
              f"Add --purge to remove it too.", flush=True)
    print(json.dumps(removed, sort_keys=True), flush=True)
    return 0


def _host_identity(source: Path):
    """Import the packaged identity helper straight from the synced sources."""
    sys.path.insert(0, str(source / "src"))
    from omodachi_core import host_identity
    return host_identity


# AUTH-1. The PAM entry is the one thing this installer writes outside the
# user's own home, and it is the one thing that can make a machine harder to
# get into, so it is opt-in (`--pam`), it always keeps a byte-exact backup, and
# `--remove-pam` puts every file back. `sudo -n` on purpose: an installer that
# could sit at a password prompt inside an ssh session is how a host ends up
# half-configured.
#
# RELEASE-3b. That reasoning is about the ssh/agent path, and `--pam` /
# `--remove-pam` are also run by hand in a terminal the user is looking at -
# `--remove` tells them to. A fresh terminal has no sudo timestamp, so `sudo -n`
# failed there every time. With a terminal on stdin the password is asked for
# once, up front, by `sudo -v`; the step itself still runs under `sudo -n`, and
# without a terminal nothing changes.
PAM_SERVICES = "sudo,polkit-1"


def _visible_terminal() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return False


# RELEASE-7b. What root runs for --pam / --remove-pam. It is the whole program
# given to `/usr/bin/python3 -I -B -c`, so the root interpreter never opens a
# file under the user's home to find its code: -I puts no script directory,
# working directory, PYTHON* variable or user site-packages on the path (only
# the system's own library is importable), -B writes no bytecode anywhere.
# The two files it needs - pam_install.py and pam_helper.py, the program PAM
# will run - arrive as bytes on stdin, read once by this installer from the
# tree it is running from, right before the step. Root writes them into a new directory of its own
# (mkdtemp under the sticky /tmp: 0700, root's, a name nobody else can predict
# or replace), runs pam_install.py from there, and deletes it. So the helper
# that ends up in /usr/local/bin is a copy root made from root's own file, and
# no path a user can rename, relink or rewrite is ever followed as root.
PAM_FILES = ("pam_install.py", "pam_helper.py")
PAM_LOADER = """\
import json, os, runpy, shutil, sys, tempfile
files = json.load(sys.stdin)
directory = tempfile.mkdtemp(prefix="omodachi-pam-", dir="/tmp")
try:
    for name in %r:
        with open(os.path.join(directory, name), "x", encoding="utf-8", newline="") as handle:
            handle.write(files[name])
    sys.argv[0] = os.path.join(directory, "pam_install.py")
    runpy.run_path(sys.argv[0], run_name="__main__")
finally:
    shutil.rmtree(directory)
""" % (PAM_FILES,)


def pam_command(arguments: list[str]) -> list[str]:
    return ["sudo", "-n", "/usr/bin/python3", "-I", "-B", "-c", PAM_LOADER, *arguments]


def pam_payload(source: Path) -> str:
    package = source / "src/omodachi_core"
    return json.dumps({name: (package / name).read_bytes().decode("utf-8") for name in PAM_FILES})


def _pam(source: Path, arguments: list[str]) -> int:
    """Run the packaged PAM installer as root, from the synced sources."""
    if shutil.which("sudo") is None:
        print("sudo is required to write /etc/pam.d", file=sys.stderr)
        return 2
    if _visible_terminal():
        print("+ sudo -v", flush=True)
        if subprocess.run(["sudo", "-v"]).returncode != 0:
            print("sudo -v failed; nothing was changed", file=sys.stderr)
            return 1
    # Not `-m`: `sudo` resets the environment and -I ignores what is left, so
    # the package is not importable as root. pam_install.py imports nothing but
    # the standard library precisely so it can run as a plain file under the
    # system interpreter; PAM_LOADER hands it over.
    command = pam_command(arguments)
    result = subprocess.run(command, input=pam_payload(source), capture_output=True, text=True)
    print("+ " + " ".join([*command[:command.index("-c") + 1], "<PAM_LOADER>", *arguments])
          + " < " + " ".join(PAM_FILES), flush=True)
    if result.stdout:
        print(result.stdout.strip(), flush=True)
    if result.returncode != 0:
        print(result.stderr.strip() or "pam step failed", file=sys.stderr)
    return result.returncode


def _runtime_paths(source: Path):
    """The packaged helper that knows where the socket is. Standard library only."""
    sys.path.insert(0, str(source / "src"))
    from omodachi_core import runtime_paths
    return runtime_paths


def install_pam(source: Path, *, services=PAM_SERVICES, timeout=45) -> int:
    home = Path.home()
    paths = _runtime_paths(source)
    owner = os.environ.get("USER") or home.name
    # AUTH-2. The socket the PAM helper will be told to use is the one in
    # /run/omodachi/<uid> - the only place a `ProtectHome=yes` sandbox can be
    # given - whether or not it exists yet, because the same run creates it.
    socket = str(paths.shared_socket_dir() / paths.SOCKET_NAME)
    code = _pam(source, ["install", "--owner", owner, "--socket", socket,
                         "--services", services, "--timeout", str(timeout)])
    if code != 0:
        return code
    # The daemon chose its socket when it started, which was before that
    # directory existed. Restart it so it binds the path the helper now has.
    run(["systemctl", "--user", "restart", "omodachid.service"], check=False)
    print(f"daemon restarted onto {socket}", flush=True)
    print("PAM entry installed. It does nothing until `omodachi-host preferences set "
          "--revision N --biometric-auth true` and a device has enrolled a key.", flush=True)
    print("Every failure path falls through to the password prompt; "
          "`install_host.py --local --remove-pam` restores the originals byte for byte.", flush=True)
    return code


def remove_pam(source: Path) -> int:
    code = _pam(source, ["remove"])
    if code == 0:
        # `/run/omodachi/<uid>` survives until the next boot on purpose (pulling
        # it away would take the socket out from under a running daemon), so the
        # daemon keeps the path it has and moves back on its own next restart.
        print("the tmpfiles fragment is gone; /run/omodachi/<uid> goes at the next boot, "
              "and the daemon falls back to $XDG_RUNTIME_DIR/omodachi when it does", flush=True)
    return code


# RELEASE-6. The host venv holds exactly requirements/host.lock plus
# omodachi-core itself, and nothing in it comes from a package index unless
# its sha256 is in that committed lock.
#
# * The venv is rebuilt from nothing on every install. An existing venv holds
#   whatever an older installer (or a hand) put there - other versions, extra
#   packages, and bytes that were never checked against any hash, which an
#   `--upgrade` over it would not replace when the version happens to match.
#   Recreating is the only way the result is exactly the lock whatever was
#   there before, and it costs a few seconds of wheel downloads. The old venv
#   is moved aside first and put back if anything fails, so a failed update
#   (no network, a hash mismatch) leaves the previous install running.
# * `python3 -m venv` gets pip from the interpreter's own bundled wheel
#   (ensurepip - part of the distribution's python package), offline. pip is
#   never upgraded, and its version check is off.
# * The lock goes in with --require-hashes --no-deps --only-binary=:all:: every
#   file pip downloads must hash to a line in the lock, the resolver adds
#   nothing, and there is no sdist to build - so no build backend is ever
#   fetched for one. --isolated keeps PIP_* variables and user pip.conf out,
#   so nothing in the environment can add a requirement, an index flag, or a
#   --user/--target that sends the install elsewhere.
# * omodachi-core itself is a local directory, so it has no hash to check;
#   it goes in with --no-index (pip cannot reach an index at all),
#   --no-deps, and --no-build-isolation, so the build backend is the locked
#   setuptools already in the venv, and --check-build-dependencies makes pip
#   refuse if that is not the exact version [build-system] requires.
HOST_LOCK = "requirements/host.lock"
PIP = ("-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "--no-input")


def pip_commands(source: Path, venv: Path) -> list[list[str]]:
    """Every pip invocation the installer makes, in order."""
    python = str(venv / "bin/python")
    return [
        [python, *PIP, "install", "--quiet", "--require-hashes", "--no-deps",
         "--only-binary=:all:", "-r", str(source / HOST_LOCK)],
        [python, *PIP, "install", "--quiet", "--no-index", "--no-deps",
         "--no-build-isolation", "--check-build-dependencies", str(source)],
    ]


def install_venv(source: Path, venv: Path) -> None:
    lock = source / HOST_LOCK
    if not lock.is_file():
        raise SystemExit(f"{lock} is missing; these sources cannot be installed "
                         f"without their dependency lock")
    previous = venv.with_name(venv.name + ".previous")
    shutil.rmtree(previous, ignore_errors=True)
    if venv.exists():
        os.rename(venv, previous)
    try:
        run(["python3", "-I", "-m", "venv", str(venv)], env=python_environment())
        for argv in pip_commands(source, venv):
            run(argv, env=python_environment())
    except BaseException:
        shutil.rmtree(venv, ignore_errors=True)
        if previous.exists():
            os.rename(previous, venv)
            print(f"the install failed; {venv} is back to what it was before", file=sys.stderr)
        raise
    shutil.rmtree(previous, ignore_errors=True)


def sunshine_override_refusal(source: Path, *, spec=None, sha256=None, build=None,
                              build_commit=None) -> str:
    """RELEASE-6: an override the installer cannot verify stops the install before it starts.

    An archive other than the pinned one needs the sha256 it must hash to; a
    git URL to build needs the exact commit. Refusing up front, rather than
    warning and carrying on, means a user who asked for something specific is
    never handed something else - or something unchecked - with a success line.
    """
    refusal = sunshine_build_refusal(build, build_commit)
    if refusal:
        return refusal
    if build is not None and spec is None:
        return ""  # the fallback, if the build fails, is the pin
    package = _sunshine_package(source)
    try:
        package.choose(spec, sha256)
    except package.SunshinePackageError as error:
        if error.code in ("sunshine_package_sha256_required", "sunshine_package_sha256_invalid",
                          "sunshine_package_sha256_conflict"):
            return error.detail
    return ""


def install_local(*, firewall=True, sunshine=True, sunshine_package=None,
                  sunshine_sha256=None, sunshine_adapter=None, sunshine_build=None,
                  sunshine_build_commit=None, vnc=True) -> int:
    home = Path.home()
    share = home / ".local/share/omodachi"
    source, venv = share / "src", share / "venv"
    if not (source / "pyproject.toml").is_file():
        print(f"missing synced sources at {source}; run with --host first", file=sys.stderr)
        return 2
    if not (source / HOST_LOCK).is_file():
        print(f"{source / HOST_LOCK} is missing; these sources cannot be installed without "
              f"their dependency lock", file=sys.stderr)
        return 2
    if sunshine:
        refusal = sunshine_override_refusal(source, spec=sunshine_package, sha256=sunshine_sha256,
                                            build=sunshine_build, build_commit=sunshine_build_commit)
        if refusal:
            print("refusing to install: " + refusal, file=sys.stderr)
            return 2
    for directory in (home / ".config/omodachi", home / ".cache/omodachi",
                      share / "agent-workspace", home / ".local/state/omodachi/remote"):
        directory.mkdir(parents=True, exist_ok=True)
    # The owned app-server's capability token lives here; only this user reads it.
    (home / ".config/omodachi/agent").mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home / ".config/omodachi/agent", 0o700)

    migration = migrate_runtime_config(home)
    if migration.get("migrated"):
        print("migrated " + RUNTIME_CONFIG, flush=True)
    if migration.get("legacy_journal_dir_present"):
        print(f"note: {OLD_JOURNAL_DIR} still holds old journals; nothing there is read any more",
              flush=True)

    menu = migrate_user_menu(home, source)
    if menu["renamed"]:
        print(f"retired the stale codex menu layer: {USER_MENU} -> {menu['backup']} "
              f"({menu['entries']} omadochi.* entries); core now uses its packaged one",
              flush=True)

    app_name = _sunshine_app_name(source)
    apps = ensure_sunshine_app(home, app_name)
    if apps["changed"]:
        print(f"published {app_name!r} in {SUNSHINE_APPS}"
              + (f" (original kept as {SUNSHINE_APPS_BACKUP})" if apps["backup"] else ""),
              flush=True)
        # The fork parses apps.json in `main` and again on its own web-UI saves
        # (`proc::refresh`); nothing watches the file. Restarting it is the
        # user's call because it drops any stream in flight, so the installer
        # says so and leaves the Sunshine unit alone.
        print(f"restart the managed Sunshine once no stream is running so it re-reads it: "
              f"systemctl --user restart {SUNSHINE_UNIT}", flush=True)
    elif apps["reason"] in ("unreadable", "unexpected_shape"):
        print(f"{SUNSHINE_APPS} is {apps['reason']}; leaving it alone. Sunshine sessions will "
              f"stop at `launching` until it publishes an app named {app_name!r}.", file=sys.stderr)

    if sunshine:
        if sunshine_build is not None:
            built = build_sunshine(sunshine_build, commit=sunshine_build_commit)
            if built["built"]:
                # The archive this run just built from the verified tree: its
                # own digest is the checksum, taken before anything else can
                # touch the file.
                sunshine_package = built["archive"]
                sunshine_sha256 = _sunshine_package(source).digest(Path(sunshine_package))
            else:
                print(f"building the fork failed ({built['reason']}); "
                      f"falling back to the packaged archive", file=sys.stderr)
        install_sunshine(source, spec=sunshine_package, sha256=sunshine_sha256,
                         adapter=sunshine_adapter)
    else:
        print("skipping the managed Sunshine fork (--no-sunshine); Remote will offer VNC only",
              flush=True)

    if vnc:
        install_vnc(source)

    assets = check_sunshine_assets(source)
    if assets["present"] is False:
        print(f"managed Sunshine has no asset tree: {assets['probed']} is missing. The fork "
              f"compiles its asset root in at configure time, so the assets must sit beside "
              f"the binary the unit starts ({assets['executable']}) and that binary must have "
              f"been configured for that path. Without them it falls back to software "
              f"encoding. Expected layout: {assets['expected_root']}/{{sunshine,assets/}}.",
              file=sys.stderr)
    elif assets["present"]:
        print(f"managed Sunshine assets present beside {assets['executable']}", flush=True)
    else:
        print(f"could not read {SUNSHINE_UNIT}; not checking the managed Sunshine assets",
              flush=True)

    credential = ensure_plugin_credential(source, home)
    if credential["issued"]:
        print(f"issued the panel's device credential as {credential['device_id']} "
              f"-> {credential['path']} (0600)", flush=True)
    elif credential["reason"] != "already_present":
        print(f"could not issue the panel's device credential ({credential['reason']}); "
              f"the panel will keep saying it needs permission", file=sys.stderr)

    identity = _host_identity(source)
    tls = identity.ensure_certificate(home / ".config/omodachi/tls")
    print(("generated" if tls["created"] else "kept") + " certificate "
          + tls["certificate"] + " fingerprint " + str(tls["tls_fingerprint_sha256"]), flush=True)

    # setuptools' `build/lib` is a copy tree it refreshes only when the source
    # is *newer* than the copy - and `rsync -a` preserves this checkout's
    # mtimes. So an edit written before the host's last build is silently
    # skipped and the venv keeps serving the previous release, with every
    # sign of a successful install. PAIR-3 lost a deploy to exactly this:
    # `media-pairing pending` answered the old shape from freshly synced
    # sources that plainly contained the new one. The build tree is a cache;
    # it is rebuilt, never trusted.
    for stale in [source / "build", *source.glob("src/*.egg-info")]:
        shutil.rmtree(stale, ignore_errors=True)
    try:
        install_venv(source, venv)
    except subprocess.CalledProcessError as error:
        # pip has already said why (a hash that does not match the lock, no
        # network); the traceback under it would only bury that line.
        print(f"installing the locked dependencies failed (exit {error.returncode}); "
              f"pip's own message above says why", file=sys.stderr)
        return 1

    for name in ("omodachid", "omodachi-host"):
        write(home / ".local/bin" / name, WRAPPER % name, 0o755)
    for name, body in UNITS.items():
        write(home / ".config/systemd/user" / name, body)
    # The owned default agent is codex's own app-server on a loopback
    # WebSocket, started as the transient unit omodachi-agent.service by
    # omodachi_core.agent_chat_owner (AGENT_UNIT), so it needs no unit file.

    run([str(venv / "bin/python"), "-I", str(venv / "bin/omodachi-host"), "desktop-entry", "install"],
        env=python_environment())

    template = ensure_theme_template(home, source)
    print(("wrote " if template["changed"] else "kept ") + template["path"], flush=True)
    render = render_current_theme(home, force=template["changed"])
    print("theme template render: " + json.dumps(render), flush=True)
    print("omarchy hooks: " + json.dumps(ensure_hooks(home)), flush=True)

    if firewall:
        configure_firewall()
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", *UNITS])
    run(["systemctl", "--user", "restart", *UNITS])
    return 0


def install_remote(target: str, *, firewall=True, remove=False, remove_all=False,
                   pam=False, pam_services=PAM_SERVICES, pam_timeout=45, remove_pam=False,
                   purge=False, sunshine=True, sunshine_package=None, sunshine_sha256=None,
                   sunshine_adapter=None, sunshine_build=None, sunshine_build_commit=None,
                   vnc=True) -> int:
    if shutil.which("rsync") is None:
        print("rsync is required on this machine", file=sys.stderr)
        return 2
    run(["ssh", target, f"mkdir -p {REMOTE_SOURCE}/scripts"])
    run(["rsync", "-a", "--delete", "--exclude", "__pycache__", "--exclude", "*.egg-info",
         str(ROOT / "src") + "/", f"{target}:{REMOTE_SOURCE}/src/"])
    run(["rsync", "-a", str(ROOT / "pyproject.toml"), f"{target}:{REMOTE_SOURCE}/"])
    run(["ssh", target, f"mkdir -p {REMOTE_SOURCE}/requirements"])
    run(["rsync", "-a", str(ROOT / HOST_LOCK), f"{target}:{REMOTE_SOURCE}/requirements/"])
    run(["rsync", "-a", str(ROOT / "scripts/install_wayvnc.py"), str(Path(__file__).resolve()),
         f"{target}:{REMOTE_SOURCE}/scripts/"])
    remote = f"python3 -I -B {REMOTE_SOURCE}/scripts/install_host.py --local"
    if remove_pam:
        remote += " --remove-pam"
    elif remove_all:
        remote += " --remove" + (" --purge" if purge else "") + ("" if sunshine else " --no-sunshine")
    elif remove:
        remote += " --remove-firewall"
    else:
        if not firewall:
            remote += " --no-firewall"
        if not sunshine:
            remote += " --no-sunshine"
        if not vnc:
            remote += " --no-vnc"
        if sunshine_package:
            remote += f" --sunshine-package {shlex.quote(sunshine_package)}"
        if sunshine_sha256:
            remote += f" --sunshine-sha256 {shlex.quote(sunshine_sha256)}"
        if sunshine_adapter:
            remote += f" --sunshine-adapter {shlex.quote(sunshine_adapter)}"
        if sunshine_build is not None:
            remote += f" --sunshine-build {shlex.quote(sunshine_build)}" if sunshine_build else " --sunshine-build"
        if sunshine_build_commit:
            remote += f" --sunshine-build-commit {shlex.quote(sunshine_build_commit)}"
        if pam:
            remote += f" --pam --pam-services {pam_services} --pam-timeout {int(pam_timeout)}"
    run(["ssh", target, remote])
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--host", metavar="USER@HOST", help="rsync this checkout and install there")
    group.add_argument("--local", action="store_true", help="install from the synced tree here")
    parser.add_argument("--firewall", action=argparse.BooleanOptionalAction, default=True,
                        help="open port 8099 and the Sunshine ports for private LANs and "
                             "tailscale0; needs sudo, and a failure only warns")
    parser.add_argument("--remove-firewall", action="store_true",
                        help="remove the Omodachi ufw rules and do nothing else")
    parser.add_argument("--remove", action="store_true",
                        help="uninstall: stop and remove the units, the venv, the synced "
                             "sources, the wrappers, the desktop entry, the managed Sunshine "
                             "fork, the firewall rules and the Omarchy surfaces")
    parser.add_argument("--purge", action="store_true",
                        help="with --remove, also delete ~/.config/omodachi - the device "
                             "secret, the host certificate and every pairing")
    parser.add_argument("--vnc", action=argparse.BooleanOptionalAction, default=True,
                        help="install WayVNC, Remote's second backend and the one that needs "
                             "no GPU (default: %(default)s)")
    parser.add_argument("--sunshine", action=argparse.BooleanOptionalAction, default=True,
                        help="install the managed Sunshine fork, which is what Remote's "
                             "picture mode streams through (default: %(default)s)")
    parser.add_argument("--sunshine-package", metavar="URL|PATH|latest",
                        help="the release archive to install, overriding the build pinned in "
                             "core's data/versions.json (so does $OMODACHI_SUNSHINE_PACKAGE); "
                             "`latest` is the newest release. Requires --sunshine-sha256: an "
                             "override is never installed unchecked")
    parser.add_argument("--sunshine-sha256", metavar="HEX",
                        help="the sha256 the --sunshine-package archive must have (or "
                             "$OMODACHI_SUNSHINE_SHA256); a .sha256 published beside the "
                             "archive is not accepted")
    parser.add_argument("--sunshine-adapter", metavar="/dev/dri/renderDN",
                        help="force this VAAPI render node instead of probing for one")
    parser.add_argument("--sunshine-build", metavar="PATH|GIT-URL", nargs="?", const="",
                        help="build the fork here with its own scripts/package_release.sh "
                             "instead of downloading an archive; needs the build dependencies "
                             "in the fork's FORK.md. A git URL also needs "
                             "--sunshine-build-commit. A local PATH is your own tree and is "
                             "built as it stands, unverified by this installer")
    parser.add_argument("--sunshine-build-commit", metavar="SHA",
                        help="with --sunshine-build <git-url>: the full 40-character commit to "
                             "build; it is fetched detached and checked (HEAD, tree, clean) "
                             "right before its build script runs")
    parser.add_argument("--pam", action="store_true",
                        help="AUTH-1: also install the PAM entry that lets a paired device "
                             "answer a host password prompt; off by default, needs sudo -n, "
                             "and keeps a byte-exact backup of every file it touches")
    parser.add_argument("--pam-services", default=PAM_SERVICES,
                        help="which /etc/pam.d services get the entry (default: %(default)s)")
    parser.add_argument("--pam-timeout", type=int, default=45,
                        help="seconds a host prompt waits for the device before falling "
                             "back to the password (default: %(default)s)")
    parser.add_argument("--remove-pam", action="store_true",
                        help="restore every PAM file AUTH-1 touched and remove its helper")
    args = parser.parse_args(argv)
    if args.local:
        if sys.platform != "linux":
            print("--local writes user units and only runs on the Linux host", file=sys.stderr)
            return 2
        source = Path.home() / REMOTE_SOURCE
        if args.remove_pam:
            return remove_pam(source if (source / "src").is_dir() else ROOT)
        if args.remove:
            return remove_local(purge=args.purge, sunshine=args.sunshine)
        if args.remove_firewall:
            return remove_firewall()
        build = args.sunshine_build
        if build == "":
            build = str(ROOT.parent / "omodachi-sunshine")
        code = install_local(firewall=args.firewall, sunshine=args.sunshine, vnc=args.vnc,
                             sunshine_package=args.sunshine_package,
                             sunshine_sha256=args.sunshine_sha256,
                             sunshine_adapter=args.sunshine_adapter,
                             sunshine_build=build,
                             sunshine_build_commit=args.sunshine_build_commit)
        if code == 0 and args.pam:
            code = install_pam(source if (source / "src").is_dir() else ROOT,
                               services=args.pam_services, timeout=args.pam_timeout)
        return code
    return install_remote(args.host, firewall=args.firewall, remove=args.remove_firewall,
                          remove_all=args.remove, pam=args.pam, pam_services=args.pam_services,
                          pam_timeout=args.pam_timeout, remove_pam=args.remove_pam,
                          purge=args.purge, sunshine=args.sunshine,
                          sunshine_package=args.sunshine_package,
                          sunshine_sha256=args.sunshine_sha256,
                          sunshine_adapter=args.sunshine_adapter,
                          sunshine_build=args.sunshine_build,
                          sunshine_build_commit=args.sunshine_build_commit, vnc=args.vnc)


if __name__ == "__main__":
    raise SystemExit(main())
