#!/usr/bin/env python3
"""Install omodachi-core on an Omarchy host, from this checkout.

    install_host.py --local                 # install from ~/.local/share/omodachi/src
    install_host.py --local --remove        # uninstall, keeping pairings
    install_host.py --local --remove --purge
    install_host.py --host alex@omarchy   # development: rsync this checkout, then install

The Omodachi plugin's Install button fetches this repository at a pinned commit
into ~/.local/share/omodachi/src, checks it, and runs `--local`. Install is
idempotent. On the computer it:

  - builds ~/.local/share/omodachi/venv from requirements/host.lock (hashes
    required) and this checkout, replacing only a venv it can show it made;
  - creates ~/.config/omodachi/device.secret and tls/ (a self-signed
    certificate) if absent, and issues the panel's device credential,
    ~/.config/omodachi/plugin.token (0600), once;
  - writes ~/.config/systemd/user/omodachid.service and omodachi-herdr.service,
    the ~/.local/bin/omodachid and omodachi-host wrappers, the desktop entry
    (~/.local/share/applications/com.omodachi.host.desktop, its icon, and
    ~/.local/bin/omodachi-panel), and enables and starts the two units;
  - installs the managed Sunshine fork: the pinned archive (sha256-checked) in
    ~/.local/share/omodachi/sunshine/<commit>/, its user unit
    app-dev.lizardbyte.app.Sunshine.service - only where there is no Sunshine
    unit or it is this installer's own; a Sunshine something else set up is
    left alone - with the web admin page limited to this computer and given a
    random login (~/.config/omodachi/sunshine-web-credentials.json, 0600,
    never printed), read back from systemd before it says so, and - only for
    a Sunshine it manages - one app entry added to ~/.config/sunshine/apps.json
    (the original kept as apps.json.omodachi-bak);
  - installs missing packages with pacman through omarchy-pkg-add or sudo:
    the fork's runtime libraries and wayvnc;
  - under ~/.config/omarchy writes themed/omodachi-theme.json.tpl and, through
    `omarchy hook install`, hooks/theme-set.d/omodachi and font-set.d/omodachi
    (their sources in ~/.local/share/omodachi/hooks), then re-applies the
    current theme headless so ~/.local/state/omarchy/current/theme gains the
    rendered omodachi-theme.json;
  - with sudo, adds ufw rules for 8099/tcp and the Sunshine ports from the
    private ranges and tailscale0, and says whether ufw is actually filtering;
  - records what it made in ~/.local/state/omodachi (venv-ids.json,
    sunshine-unit.json).

`--pam` (opt-in, root, asks for the password) additionally installs the
device-approval PAM helper and its lines in /etc/pam.d, described in
pam_install.py.

The daemon, once running, also: adds one line per device granted SSH to
~/.ssh/authorized_keys (marked `# omodachi:<device>`); points Voxtype's
audio device at its own source while a device dictates and puts the original
back afterwards (~/.config/voxtype/config.toml.omodachi-dictation-bak meanwhile);
moves the Omarchy bar with `omarchy bar position` during a Remote session and
back at its end; and runs the Omarchy menu's own `when` conditions to decide
which rows to show.

Every file it writes goes through a new file and a rename (never through a
link), and a unit, command, template or hook source of its name that it did not
write stops the install. An outdated PAM entry (from before RELEASE-9) makes
Install report a partial install and exit 3.

`--remove` takes back what Install made and nothing else: the units, wrappers,
desktop entry, venvs, the Sunshine fork, unit and login (disabling the unit only
if Install enabled it; the fork's own state only if ~/.config/sunshine was
created for it), the apps.json entry, the Omarchy template and hooks, the
ufw rules, every authorized_keys line marked as Omodachi's, a Voxtype config
left mid-dictation, the PAM entry if --pam was used (with your password, in the
terminal), and src when the plugin's bootstrap made it. `--purge` also deletes
the device secret, certificate, pairings and the daemon's other state - only
the files it creates (PURGE_RULES); anything else in ~/.config/omodachi,
~/.cache/omodachi or ~/.local/state/omodachi, and agent-workspace, is kept and
listed. If the PAM entry or an authorized_keys line cannot be removed, it
says the host was only partly removed and exits 3.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REMOTE_SOURCE = ".local/share/omodachi/src"

# The daemon listens on every interface so a companion on the LAN or over
# Tailscale reaches it directly. aiohttp binds one address family per site, so
# this is IPv4; IPv6 on the LAN is a known limitation of this revision.
UNIT_MARKER = "# Written by omodachi-core (scripts/install_host.py)."
DAEMON_UNIT = """[Unit]
# Written by omodachi-core (scripts/install_host.py).
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
# Written by omodachi-core (scripts/install_host.py).
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
# RELEASE-9: what makes a unit file there the installer's. Units written before
# the marker are recognised by the one line only this installer writes.
UNIT_SIGNATURES = {"omodachid.service": "/.local/share/omodachi/venv/bin/omodachid",
                   "omodachi-herdr.service": "/usr/bin/herdr --session omodachi server"}


def unit_is_ours(name: str, text: str) -> bool:
    return UNIT_MARKER in text or UNIT_SIGNATURES[name] in text


def wrapper_is_ours(text: str) -> bool:
    return "share/omodachi/venv" in text
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
MANAGED_COMMENTS = (CORE_COMMENT, SUNSHINE_COMMENT)
# `ufw show added`: "ufw allow from 10.0.0.0/8 to any port 8099 proto tcp comment 'omodachi-core'"
_ADDED = re.compile(r"^ufw (.+?) comment '(.*)'$")


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
    """Delete only rules carrying one of these comments.

    RELEASE-9 integration: the rules are read from `ufw show added`, not from
    `ufw status numbered`. With ufw installed but not enabled - the default on
    a fresh Omarchy - `status` lists no rules at all, so the numbered listing
    found nothing, the removal said "removed 0" and every allow rule Install
    added stayed in user.rules, to open 8099 and the Sunshine ports the day
    somebody runs `ufw enable`. `show added` lists the rules either way, and
    `ufw delete <rule>` deletes by the rule itself, so there are no numbers to
    shift. Anything with another comment - omodachi-dev-mac, the Omarchy sshd
    rule, the docker DNS rules - is never touched.
    """
    listing = ufw("show", "added").stdout or ""
    targets = []
    for line in listing.splitlines():
        match = _ADDED.match(line.strip())
        if match and match.group(2) in comments:
            targets.append(shlex.split(match.group(1)))
    for rule in targets:
        ufw("--force", "delete", *rule)
    return len(targets)


# RELEASE-9: what the firewall step says is what ufw does. The rules below are
# only a filter when ufw is enabled with incoming traffic denied by default;
# otherwise every port the daemon and the fork listen on is reachable from
# every network this computer is on, and the summary says exactly that.
LISTENING = (f"{CORE_PORT}/tcp (omodachid)",
             f"{SUNSHINE_TCP}/tcp and {SUNSHINE_UDP}/udp (the managed Sunshine)",
             "47990/tcp (the managed Sunshine's admin page, which itself refuses other computers)")


def firewall_state() -> dict:
    """{"present", "active", "default_incoming"} from `sudo ufw status verbose`."""
    if shutil.which("ufw") is None:
        return {"present": False, "active": False, "default_incoming": None}
    try:
        text = ufw("status", "verbose", check=False).stdout or ""
    except (OSError, subprocess.SubprocessError):
        text = ""
    active = bool(re.search(r"^Status:\s*active\b", text, re.MULTILINE))
    incoming = re.search(r"^Default:\s*(\w+)\s*\(incoming\)", text, re.MULTILINE)
    return {"present": True, "active": active, "known": bool(text.strip()),
            "default_incoming": incoming.group(1) if incoming else None}


def firewall_summary(state: dict, *, opened: bool, tailscale: bool) -> str:
    ranges = ", ".join(PRIVATE_CIDRS) + (" and tailscale0" if tailscale else "")
    exposed = "; ".join(LISTENING)
    if not state["present"]:
        return (f"ufw is not installed, so nothing filters incoming connections: {exposed} "
                f"are reachable from every network this computer is on")
    if not state.get("known", True):
        return (f"could not read `ufw status`, so whether anything filters {exposed} is unknown")
    if not state["active"]:
        return (f"ufw is installed but NOT active, so nothing filters incoming connections: "
                f"{exposed} are reachable from every network this computer is on"
                + (f". The Omodachi rules ({CORE_PORT}/tcp, {SUNSHINE_TCP}/tcp, {SUNSHINE_UDP}/udp "
                   f"from {ranges}) are added and take effect with `sudo ufw enable`"
                   if opened else ""))
    if state["default_incoming"] not in ("deny", "reject"):
        return (f"ufw is active but its default for incoming traffic is "
                f"{state['default_incoming'] or 'unknown'}, so {exposed} are reachable from "
                f"every network this computer is on")
    if not opened:
        return (f"ufw is active and denies incoming traffic by default; the Omodachi rules "
                f"could not be added, so devices on the network cannot reach {exposed}")
    return (f"ufw is active and denies incoming traffic by default: {CORE_PORT}/tcp, "
            f"{SUNSHINE_TCP}/tcp and {SUNSHINE_UDP}/udp are open to {ranges} only "
            f"(comments {', '.join(MANAGED_COMMENTS)}); 47990 is not opened")


def configure_firewall() -> int:
    if shutil.which("ufw") is None:
        print(firewall_summary(firewall_state(), opened=False, tailscale=False), file=sys.stderr)
        return 0
    tailscale = _tailscale_present()
    opened = True
    try:
        # RELEASE-9: Install adds rules and deletes none; a rule someone wrote
        # by hand is theirs, whatever its comment says.
        _allow("tcp", CORE_PORT, CORE_COMMENT, tailscale=tailscale)
        # The managed Sunshine fork gets the official port set, scoped the
        # official way rather than to one developer machine.
        _allow("tcp", SUNSHINE_TCP, SUNSHINE_COMMENT, tailscale=tailscale)
        _allow("udp", SUNSHINE_UDP, SUNSHINE_COMMENT, tailscale=tailscale)
        ufw("reload")
    except (OSError, subprocess.SubprocessError) as error:
        # A closed firewall is a reachability problem, not a broken install.
        print(f"firewall step failed, leaving the current rules alone: {error}", file=sys.stderr)
        opened = False
    state = firewall_state()
    print("firewall: " + firewall_summary(state, opened=opened, tailscale=tailscale),
          file=sys.stdout if state["active"] else sys.stderr, flush=True)
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


class NotOurs(Exception):
    """A file where the installer writes one of its own holds something else."""


def write(path: Path, text: str, mode=0o644, *, ours=None) -> bool:
    """Write one of the installer's files: through a new file and a rename, so
    a link at `path` is replaced rather than followed. RELEASE-9: an existing
    file is replaced only when `ours(current text)` says it is the installer's;
    otherwise NotOurs is raised and nothing is written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(path):
        try:
            current = None if path.is_symlink() or not path.is_file() else path.read_text()
        except (OSError, UnicodeDecodeError):
            current = None
        if current == text and path.stat().st_mode & 0o777 == mode:
            return False
        if ours is not None and (current is None or not ours(current)):
            raise NotOurs(f"{path} is there and is not the installer's; it was left as it is")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.omodachi-new")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


# RELEASE-9: the installer no longer rewrites ~/.config/omodachi/desktop-runtime.json
# or renames ~/.config/omodachi/omodachi-menu.jsonc aside (two migrations for
# one developer host's leftovers): both are files a person writes, and the
# installer touches only what it made.


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


def report_web_lock(package, unit: dict) -> None:
    """Say what the resolved unit does about the fork's web admin page (RELEASE-9)."""
    if unit.get("strangers"):
        print("kept drop-ins this installer did not write, which also apply to the unit: "
              + ", ".join(unit["strangers"]), flush=True)
    if unit.get("web_locked") is False:
        print("WARNING: the Sunshine unit systemd resolves does not carry the web page lockdown "
              "(a drop-in above replaces its ExecStart), so Sunshine's web admin page (47990) "
              "may still be claimed by the first caller. Remove that drop-in, or its ExecStart "
              "line, and install again.", file=sys.stderr)
    elif unit.get("web_locked"):
        print(f"Sunshine's web admin page (47990) answers this computer only and has a random "
              f"login nobody knows ({package.WEB_CREDENTIALS}, 0600), so it cannot be claimed",
              flush=True)
    else:
        print("could not read back the Sunshine unit from systemd to confirm the web page "
              "lockdown", file=sys.stderr)


def lock_existing_fork(source: Path) -> None:
    """--no-sunshine: a managed fork this installer set up earlier still runs,
    so its unit still gets the web page lockdown (RELEASE-9); nothing is
    downloaded and the fork restarts only if its unit changed."""
    package = _sunshine_package(source)
    if package.unit_owner(Path.home())["state"] != "ours":
        return
    present = package.installed_fork(Path.home())
    if present is None:
        return
    try:
        unit = package.ensure_unit(Path.home(), Path(present["directory"]),
                                   arguments=present.get("arguments") or None, restart=False)
    except package.SunshinePackageError as error:
        print(f"could not update the managed Sunshine unit ({error.code}): {error.detail}",
              file=sys.stderr)
        return
    report_web_lock(package, unit)


def install_sunshine(source: Path, *, spec=None, sha256=None, adapter=None,
                     config_created=None) -> dict:
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
    # not downloaded again and the fork is not restarted for nothing (a restart
    # drops any stream in flight). Before this every install on a developer
    # host went to GitHub for `releases/latest`, got a 404 and printed a failure.
    #
    # RELEASE-9: "satisfies" is no longer a name in a SOURCE file. The tree the
    # unit starts must be byte for byte the pinned archive's contents - its
    # MANIFEST.sha256 hashes to the manifest_sha256 pinned in versions.json,
    # every file it lists matches, nothing else is there - and the unit or
    # drop-in that starts it must carry this installer's marker. Otherwise the
    # pinned archive is downloaded and checked as on a fresh machine. The unit
    # itself is still brought to what this core writes (the web UI lockdown),
    # and the fork restarted only if that changed it.
    if choice["source"] == "pin":
        present = package.installed_fork(Path.home())
        if present is not None and package.satisfies(present["version"], choice):
            why = (package.pristine(Path(present["directory"]), choice.get("manifest_sha256"))
                   or ("" if present["written_by_installer"] else
                       "the unit that starts it was not written by this installer"))
            if why:
                print(f"the Sunshine fork at {present['directory']} names {present['version'][:12]}, "
                      f"but is not trusted as the pinned build: {why}. Installing the pinned "
                      f"archive instead", flush=True)
            else:
                try:
                    unit = package.ensure_unit(Path.home(), Path(present["directory"]),
                                               arguments=present.get("arguments") or None,
                                               restart=False, config_created=config_created)
                except package.SunshinePackageError as error:
                    print(f"Sunshine was not installed ({error.code}): {error.detail}", file=sys.stderr)
                    return {"installed": False, "reason": error.code, "detail": error.detail}
                print(f"the managed Sunshine fork {present['version'][:12]} at {present['directory']} "
                      f"is the pinned build (its MANIFEST.sha256 is the pinned one and every file "
                      f"matches), so nothing was downloaded; unit "
                      + ("updated and the fork restarted" if unit["restarted"] else "unchanged")
                      + f", enabled={unit['enabled']}, active={unit['active']}", flush=True)
                report_web_lock(package, unit)
                return {"installed": False, "reason": "already_installed", "version": present["version"],
                        "pinned": choice["version"], "binary": present["binary"], "unit": unit}
    spec, sha256 = choice["spec"], choice["sha256"]
    print(f"+ installing the managed Sunshine fork from {spec}"
          + (f" (pinned {choice['version']}, sha256 {sha256[:12]}…)" if choice["source"] == "pin" else
             f" (the newest release, asked for by name, sha256 {sha256[:12]}…)" if choice["source"] == "latest" else
             f" (sha256 {sha256[:12]}…, given with --sunshine-sha256)"),
          flush=True)
    try:
        result = package.install(spec, Path.home(), sha256=sha256, adapter=adapter,
                                 config_created=config_created)
    except package.SunshinePackageError as error:
        print(f"Sunshine was not installed ({error.code}): {error.detail}", file=sys.stderr)
        if error.code in ("sunshine_not_ours", "sunshine_directory_not_ours",
                          "sunshine_web_credentials_unusable"):
            print("Remote's VNC mode still works.", file=sys.stderr)
        else:
            print("Remote's VNC mode still works. Re-run with --sunshine-package <url|path> once "
                  "you have an archive, or with --sunshine-build to build the fork from source "
                  "here.", file=sys.stderr)
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
    report_web_lock(package, unit)
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

    `root` is this installer's own cache, so what it made there before is
    thrown away rather than trusted - and only that: RELEASE-9, a directory
    without the marker this function writes into its .git is somebody else's
    and is left alone. Returns (ok, detail); ok only when HEAD is the commit,
    its tree is the commit's tree and there is nothing else in the work tree.
    """
    marker = root / SUNSHINE_BUILD_MARKER
    if os.path.lexists(root):
        if root.is_symlink() or not root.is_dir() or marker.is_symlink() or not marker.is_file():
            return False, (f"{root} is there and is not this installer's build cache; it was "
                           f"left as it is - move it aside and try again")
        shutil.rmtree(root)
    root.mkdir(parents=True)
    if run(["git", "init", "--quiet", "--template=", str(root)], check=False).returncode != 0:
        return False, "git init failed"
    marker.write_text("omodachi-core install_host.py --sunshine-build cache\n")
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
    backup = home / SUNSHINE_APPS_BACKUP
    if document == {"apps": []} and not os.path.lexists(backup):
        # No original was kept, so ensure_sunshine_app created this file for
        # our one entry; with it gone, so is the file.
        path.unlink()
        return {"changed": True, "reason": "removed_file_we_created", "backup_removed": False}
    text = json.dumps(document, indent=4, sort_keys=True, ensure_ascii=False) + "\n"
    temporary = path.with_name(path.name + ".omodachi-new")
    temporary.write_text(text)
    temporary.chmod(0o644)
    os.replace(temporary, path)
    # The backup only existed to undo this one append. Once it is undone, a
    # stale copy of the user's Sunshine configuration is just clutter - but
    # only if it is still identical to what we have now.
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


# RELEASE-9: a template file there is the installer's when it is byte for byte
# one this installer has shipped (every published core carries the same one) or
# the one it ships now; anything else is somebody's edit and is kept.
PUBLISHED_TEMPLATES = frozenset({"8c937b35ad65cdbeee77c3b4d9b9a20234c1cb696da08bec0f630517ba1b210e"})


def template_is_ours(text: str, source: Path = ROOT) -> bool:
    import hashlib
    digest = hashlib.sha256(text.encode()).hexdigest()
    try:
        current = (source / "src/omodachi_core/data" / THEME_TEMPLATE).read_text()
    except OSError:
        current = None
    return digest in PUBLISHED_TEMPLATES or text == current


def ensure_theme_template(home: Path, source: Path) -> dict:
    """Publish our template where omarchy-theme-set-templates already looks.

    User templates under ~/.config/omarchy/themed are the documented extension
    point for "theming apps Omarchy doesn't cover", and they are rendered before
    the packaged ones. Written whole, over nothing or over a copy of ours only.
    """
    body = (source / "src/omodachi_core/data" / THEME_TEMPLATE).read_text()
    target = home / THEMED_DIR / THEME_TEMPLATE
    return {"path": str(target),
            "changed": write(target, body, ours=lambda text: template_is_ours(text, source))}


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
        write(master, hook_script(hook), 0o755, ours=lambda text: HOOK_MARKER in text)
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
    try:
        ours = not template.is_symlink() and template.is_file() and template_is_ours(template.read_text())
    except (OSError, UnicodeDecodeError):
        ours = False
    if ours:
        template.unlink()
        removed["template"] = True
    elif os.path.lexists(template):
        removed["template"] = "not_ours"
    rendered = home / OMARCHY_STATE / "theme" / THEME_TEMPLATE.removesuffix(".tpl")
    if ours and rendered.is_file():
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
    # RELEASE-8: the master copies too - only files carrying our marker, then
    # whatever directories that leaves empty.
    master = home / HOOK_SOURCE_DIR
    for hook in sorted(HOOK_COMMANDS):
        copy = master / hook / HOOK_NAME
        try:
            if not copy.is_symlink() and HOOK_MARKER in copy.read_text():
                copy.unlink()
        except OSError:
            pass
        _remove_empty(master / hook)
    _remove_empty(master)
    return removed


def _remove_empty(directory: Path) -> None:
    try:
        if directory.is_dir() and not directory.is_symlink():
            directory.rmdir()
    except OSError:
        pass  # not empty: something in it is not ours


# INSTALL-1 §1.4. `omarchy plugin remove` takes the panel away and leaves
# everything the panel installed: a daemon, a Herdr session, a venv, two
# wrappers, a desktop entry, the Sunshine fork, the firewall rules and the
# Omarchy surfaces. This is the matching entry point, and `--purge` is the only
# thing that touches ~/.config/omodachi, because that directory holds the
# device secret, the host certificate and every pairing - a user who removes
# the host to reinstall it should not have to pair every device again.
REMOVED_UNITS = ("omodachid.service", "omodachi-herdr.service", "omodachi-agent.service")
CONFIG_DIR = ".config/omodachi"

# RELEASE-8. What --remove and --purge may delete is what the installer (or
# the daemon it installed) made, never the user's own files. ~/.local/share/
# omodachi: agent-workspace is the agent's working directory - the user's
# files - and is always kept, as is anything else nobody here made; src goes
# only when the plugin's bootstrap can show it made it (the same id it keeps in
# its .git and in ~/.local/state/omodachi/core-source.json); the venvs only
# when this installer can (RELEASE-9, VENV_RECORD).
SHARE_DIR = ".local/share/omodachi"
SOURCE_RECORD = ".local/state/omodachi/core-source.json"
SOURCE_ID_FILE = "omodachi-install-id"
SUNSHINE_BUILD_MARKER = ".git/omodachi-sunshine-build-cache"

# RELEASE-9. `--purge` deletes, in the three directories below, exactly the
# files and directories Omodachi's installer and daemon create - each listed
# here by its name, or by the exact pattern of a name they generate (tempfile's
# random part is eight of [a-z0-9_]) - and nothing else. Anything a person put
# there, and anything this list does not name, is kept and printed. A value of
# None is a file (or socket, or link - never followed); a dict is a directory
# of ours, judged the same way inside, and removed only if that leaves it
# empty. The same list is in the plugin's tools/install_host.py, for a purge
# after core is gone; tests/test_install_host.py compares the two.
_TMP = "[a-z0-9_]{8}"
PURGE_RULES = {
    ".config/omodachi": {
        r"device\.secret": None,
        r"device\.credentials\.json": None,
        r"device\.credentials\.json\.lock": None,
        r"\.device\.credentials\.json\." + _TMP: None,
        r"pairing\.json": None,
        r"pairing\.json\.lock": None,
        r"\.pairing-" + _TMP: None,
        r"host-id": None,
        r"herdr-sessions\.json": None,
        r"herdr-sessions\.json\.lock": None,
        r"\.herdr-sessions" + _TMP: None,
        r"biometric-keys\.json": None,
        r"biometric-keys\.json\.lock": None,
        r"\.biometric-" + _TMP: None,
        r"plugin\.token": None,
        r"plugin\.token\.new": None,
        r"owned-herdr-pane\.json": None,
        r"agent-requests\.json": None,
        r"agent-lifecycle\.lock": None,
        r"\.agent-" + _TMP: None,
        r"sunshine-web-credentials\.json": None,
        r"media-pairing": {r"state\.json": None, r"state\.lock": None,
                           r"\.media-pairing-" + _TMP: None},
        r"preferences": {r"state\.json": None, r"state\.json\.lock": None,
                         r"\.preferences-" + _TMP: None},
        r"tls": {r"server\.pem": None, r"server\.key": None,
                 r"\.server-" + _TMP + r"\.(pem|key)": None},
        r"structured-default": {r"owner\.json": None, r"owner-" + _TMP: None,
                                r"owner-before-empty-recovery-\d+\.json": None,
                                r"delivery\.json": None, r"sequence\.json": None,
                                r"\.agent-" + _TMP: None},
        r"agent-handoff": {r"handoff_[0-9a-f]{32}\.json": None, r"\.agent-" + _TMP: None},
        r"agent": {r"ws-token": None, r"ws-token-" + _TMP: None,
                   r"endpoint\.json": None, r"endpoint-" + _TMP: None},
    },
    ".cache/omodachi": {
        r"install-status\.json": None,
        r"\.install-status\.json\.\d+\.tmp": None,
        r"omodachid\.sock": None,
        r"omodachid\.sock\.omodachi-new": None,
        r"voice": {r"transcript-[0-9a-f]{16}\.txt(\.done)?": None},
        r"sunshine": {r"omodachi-sunshine-[0-9a-f]{7,40}(-dirty)?-x86_64\.tar\.zst": None,
                      r"omodachi-sunshine-x86_64\.tar\.zst": None, r"sunshine\.tar\.zst": None},
        # --sunshine-build <git-url>'s checkout: whole, but only with our marker.
        r"sunshine-src": SUNSHINE_BUILD_MARKER,
    },
    ".local/state/omodachi": {
        r"remote": {
            r"OMODACHI-[0-9a-f]{16}\.json": None,
            r"\.remote-session-" + _TMP: None,
            # rfb.sock: RELEASE-9 B3's private RFB listener, left behind only
            # if the daemon died with a VNC session open.
            r"vnc": {r"rs_[0-9a-f]{32}": {r"instance\.json": None, r"control\.sock": None,
                                          r"rfb\.sock": None, r"last-error\.txt": None}},
        },
        r"desktop": {},
        r"core-source\.json": None,
        r"\.core-source\.json\.\d+\.tmp": None,
        r"venv-ids\.json": None,
        r"\.venv-ids\.json\.\d+\.tmp": None,
        r"sunshine-unit\.json": None,
        r"\.sunshine-unit\.json\." + _TMP: None,
    },
}


def _rule_for(name: str, rules: dict):
    for pattern, rule in rules.items():
        if re.fullmatch(pattern, name):
            return True, rule
    return False, None


def purge_directory(directory: Path, rules: dict, kept: list, *, spare=()) -> None:
    """Delete what `rules` names under `directory`; list everything else in `kept`."""
    if directory.is_symlink() or not directory.is_dir():
        if os.path.lexists(directory):
            kept.append(directory)
        return
    for child in sorted(directory.iterdir()):
        known, rule = _rule_for(child.name, rules)
        real_dir = child.is_dir() and not child.is_symlink()
        if not known or child in spare:
            kept.append(child)
        elif rule is None:
            if real_dir:
                kept.append(child)  # a directory where the program writes a file
            else:
                child.unlink(missing_ok=True)
        elif isinstance(rule, str):
            marker = child / rule
            if real_dir and not marker.is_symlink() and marker.is_file():
                shutil.rmtree(child, ignore_errors=True)
            else:
                kept.append(child)
        elif real_dir:
            purge_directory(child, rule, kept, spare=spare)
            _remove_empty(child)
        else:
            kept.append(child)


PAM_CONF = "/etc/omodachi/pam.conf"


def pam_outdated(conf: str = PAM_CONF) -> bool:
    """RELEASE-9: a PAM entry installed before the root helper verified
    approvals itself (its config has no `protocol=2`) is not one to keep
    running on this computer; Install says so instead of succeeding."""
    try:
        lines = Path(conf).read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return "protocol=2" not in (line.strip() for line in lines)


def pam_present() -> list[str]:
    """The files that show the --pam integration is on this computer, if any.

    All readable without root: the helper, its config, the tmpfiles fragment,
    the polkit drop-in, and the PAM service files carrying its marker.
    """
    found = [path for path in ("/etc/omodachi/pam.conf", "/usr/local/bin/omodachi-pam",
                               "/etc/tmpfiles.d/omodachi.conf",
                               "/etc/systemd/system/polkit-agent-helper@.service.d/60-omodachi.conf")
             if os.path.lexists(path)]
    for service in ("sudo", "polkit-1", "hyprlock", "omarchy-lock-password", "su"):
        try:
            text = Path("/etc/pam.d", service).read_text(errors="replace")
        except OSError:
            continue
        if "omodachi-auth" in text or "omodachi-pam" in text:
            found.append(f"/etc/pam.d/{service}")
    return found


def remove_ssh_lines(home: Path, source: Path = ROOT) -> dict:
    """RELEASE-9: take back the authorized_keys lines Omodachi wrote."""
    sys.path.insert(0, str(source / "src"))
    from omodachi_core import ssh_keys
    try:
        removed = ssh_keys.remove_marked_lines(home)
    except ssh_keys.SshKeyError as error:
        return {"ok": False, "error": error.code, "removed": []}
    return {"ok": True, "removed": removed}


def source_is_the_bootstraps(home: Path) -> bool:
    """Whether ~/.local/share/omodachi/src is the checkout the plugin's
    bootstrap made: a real directory with a real .git whose id file names the
    id its record holds (the record's "pending" ids included)."""
    source = home / REMOTE_SOURCE
    identifier = source / ".git" / SOURCE_ID_FILE
    try:
        if (source.is_symlink() or not source.is_dir() or (source / ".git").is_symlink()
                or identifier.is_symlink() or not identifier.is_file()):
            return False
        with identifier.open("rb") as handle:
            found = handle.read(64).decode("ascii", "replace").strip()
        record = json.loads((home / SOURCE_RECORD).read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict) or record.get("path") != str(source) or len(found) != 32:
        return False
    return found == record.get("id") or found in (record.get("pending") or [])


def _delete(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path, ignore_errors=True)


VOXTYPE_CONFIG = ".config/voxtype/config.toml"
VOXTYPE_BACKUP = VOXTYPE_CONFIG + ".omodachi-dictation-bak"   # voice.BACKUP_SUFFIX


def restore_voxtype(home: Path) -> dict:
    """Put the pre-dictation Voxtype config back - only if the current one is
    exactly what the daemon made of it (the one device line changed), so an
    edit made since is never overwritten; otherwise both are kept and named."""
    backup, config = home / VOXTYPE_BACKUP, home / VOXTYPE_CONFIG
    try:
        if backup.is_symlink() or not backup.is_file() or config.is_symlink():
            return {"restored": False}
        original = backup.read_bytes()
        sys.path.insert(0, str(ROOT / "src"))
        from omodachi_core import voice
        if config.read_bytes() != voice.set_audio_device(original, voice.SOURCE_NAME):
            return {"restored": False, "kept": [str(config), str(backup)],
                    "reason": "the config was changed since dictation began"}
        temporary = config.with_name(f".{config.name}.{os.getpid()}.omodachi-restore")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             config.stat().st_mode & 0o777 if config.exists() else 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(original)
        os.replace(temporary, config)
        backup.unlink()
    except Exception as error:  # an unreadable or unsupported config: keep both
        return {"restored": False, "error": str(error)}
    return {"restored": True}


# The exit status of a removal that could not take back everything that
# grants access to this computer (the PAM entry, an authorized_keys line).
PARTIAL = 3


def remove_local(*, purge=False, sunshine=True) -> int:
    home = Path.home()
    removed = {"units": {}, "wrappers": [], "venv": False, "sources": False,
               "desktop_entry": [], "sunshine": None, "purged": False}
    left = []   # what still grants access, if anything: the result is then partial
    share = home / SHARE_DIR

    # RELEASE-9: the PAM entry is part of removing Omodachi, not advice
    # printed after it. Its root step runs first, from this verified tree,
    # asking for the password in the terminal the user is looking at. If it
    # cannot run or does not finish, the removal is partial and says so - the
    # sources stay, because that step runs out of them.
    pam = pam_present()
    if pam:
        print("the device-approval PAM entry is installed (" + ", ".join(pam)
              + "); removing it needs your password once", flush=True)
        pam_code = remove_pam(ROOT)
        pam = pam_present()
        removed["pam_entry_present"] = bool(pam)
        if pam and pam_code == 3:
            left.append("a PAM file was changed around the lines --pam added, so it was left as it "
                        "is (the lines are shown above; the helper they name was removed, so they "
                        "can only fall through to the password). Delete them with `sudoedit` in "
                        + ", ".join(path for path in pam if path.startswith("/etc/pam.d/"))
                        + ", then remove again")
        elif pam:
            left.append("the device-approval PAM entry is still installed (" + ", ".join(pam)
                        + "). Run\n    python3 -I -B "
                        + str(share / "src/scripts/install_host.py") + " --local --remove-pam\n"
                        "  in a terminal (it asks for your password), then remove again")
    for unit in REMOVED_UNITS:
        path = home / ".config/systemd/user" / unit
        try:
            text = path.read_text() if path.is_file() and not path.is_symlink() else None
        except (OSError, UnicodeDecodeError):
            text = None
        if os.path.lexists(path) and unit in UNITS and (text is None or not unit_is_ours(unit, text)):
            # RELEASE-9: a file of that name the installer did not write.
            removed["units"][unit] = {"kept": str(path)}
            print(f"kept {path}: it is not a unit this installer wrote", flush=True)
            continue
        # omodachi-agent.service is transient (the daemon starts it with
        # systemd-run), so it has no file; the other two are ours or absent.
        state = run(["systemctl", "--user", "is-active", unit], check=False,
                    capture_output=True, text=True).stdout.strip()
        run(["systemctl", "--user", "disable", "--now", unit], check=False,
            capture_output=True, text=True)
        existed = os.path.lexists(path)
        if existed:
            path.unlink()
        removed["units"][unit] = {"was": state or "unknown", "unit_file_removed": existed}

    # RELEASE-9: while a device dictates, the daemon points Voxtype's one
    # audio-device line at its own source and keeps the original bytes beside
    # it; it puts them back when dictation ends. A daemon stopped in between
    # (a crash, or this removal) would leave Voxtype listening to a source
    # that no longer exists, so the saved original goes back here.
    removed["voxtype"] = restore_voxtype(home)
    if removed["voxtype"]["restored"]:
        print(f"put {home / VOXTYPE_CONFIG} back the way it was before dictation", flush=True)
    elif removed["voxtype"].get("kept"):
        print(f"kept {home / VOXTYPE_CONFIG} as it is: it was changed after dictation pointed it "
              f"at Omodachi; the pre-dictation copy is {home / VOXTYPE_BACKUP}", flush=True)

    if sunshine:
        removed["sunshine"] = _sunshine_package(ROOT).remove(home)
        for line in (removed["sunshine"].get("left_enabled"), removed["sunshine"].get("unit_not_ours")):
            if line:
                print(line, flush=True)
        try:
            removed["sunshine_app"] = remove_sunshine_app(home, _sunshine_app_name(ROOT))
        except Exception as error:
            removed["sunshine_app"] = {"changed": False, "reason": type(error).__name__}
        if removed["sunshine"].get("state_kept"):
            print(removed["sunshine"]["state_kept"], flush=True)
        _remove_empty(home / ".config/sunshine")

    # The desktop entry, its icon and the canonical wrapper are the launcher's
    # own three files; it knows which they are, so it is asked rather than
    # guessed at.
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

    # RELEASE-9: every authorized_keys line Omodachi wrote - one per device
    # that ever got SSH - goes with it; the user's own lines are copied
    # through untouched. Without this a removed (even purged) host kept
    # letting every paired device log in, with nothing left to revoke it.
    ssh = remove_ssh_lines(home)
    removed["authorized_keys"] = ssh
    if not ssh["ok"]:
        left.append(f"{home / '.ssh/authorized_keys'} could not be cleaned ({ssh['error']}); "
                    f"delete every line ending in '# omodachi:<device>' from it yourself")
    elif ssh["removed"]:
        print(f"removed {len(ssh['removed'])} Omodachi line(s) from {home / '.ssh/authorized_keys'}: "
              + ", ".join(ssh["removed"]), flush=True)

    removed["venvs"] = remove_venvs(home)
    removed["venv"] = "venv" in removed["venvs"]["removed"]
    source = share / "src"
    if os.path.lexists(source) and not pam:
        if source_is_the_bootstraps(home):
            shutil.rmtree(source, ignore_errors=True)
            removed["sources"] = True
        else:
            removed["sources_kept"] = str(source)
            print(f"kept {source}: nothing shows the Omodachi plugin's installer made it, "
                  f"so it is not this uninstaller's to delete.", flush=True)

    removed["omarchy"] = remove_omarchy_surfaces(home)
    if remove_firewall() != 0:
        # RELEASE-9: allow rules left behind are not "removed".
        left.append(f"the ufw rules commented {' and '.join(MANAGED_COMMENTS)} could not be "
                    f"deleted; list them with `sudo ufw show added` and delete each with "
                    f"`sudo ufw delete <the rule as listed, without its comment>`")
    run(["systemctl", "--user", "daemon-reload"], check=False)

    kept = [path for path in removed["venvs"]["kept"]]
    if purge:
        # The plugin bootstrap's lock, held while this runs; it removes it after.
        spare = [home / ".local/state/omodachi/install.lock"]
        package = _sunshine_package(ROOT)
        unit_dir = home / package.UNIT_DIR
        if not sunshine and (package._marked(unit_dir / package.SUNSHINE_UNIT) or package._marked(
                unit_dir / (package.SUNSHINE_UNIT + ".d") / package.DROPIN_NAME)):
            # --no-sunshine left the managed fork installed: its web login and
            # the record of what its unit was stay with it.
            spare += [home / package.WEB_CREDENTIALS, home / package.UNIT_RECORD]
        if os.path.lexists(source):
            # The plugin's ownership record outlives a checkout it still
            # names, so that checkout stays provably the bootstrap's.
            spare.append(home / SOURCE_RECORD)
        for relative, rules in PURGE_RULES.items():
            directory = home / relative
            before = len(kept)
            purge_directory(directory, rules, kept, spare=spare)
            _remove_empty(directory)
            # The record we deliberately spared is not "the user's".
            kept[before:] = [path for path in kept[before:] if path not in spare]
        removed["purged"] = True
    else:
        print(f"kept {home / CONFIG_DIR} (device secret, certificate, pairings). "
              f"Add --purge to remove it too.", flush=True)
    # ~/.local/share/omodachi: what the installer made is gone (sunshine/,
    # hooks/, the venvs and src were each judged above); the rest -
    # agent-workspace, and anything nobody here made - stays, with or
    # without --purge.
    if share.is_dir() and not share.is_symlink():
        kept += [child for child in sorted(share.iterdir())
                 if child.name != "src" and str(child) not in removed["venvs"]["kept"]]
        _remove_empty(share)
    if kept:
        removed["kept"] = [str(path) for path in kept]
        print("kept, because they are yours rather than the installer's:\n  "
              + "\n  ".join(str(path) for path in kept), flush=True)
    print(json.dumps(removed, sort_keys=True, default=str), flush=True)
    if left:
        removed["partial"] = left
        print("\nOmodachi Host was only PARTLY removed:\n  - " + "\n  - ".join(left), flush=True)
        return PARTIAL
    return 0


def _host_identity(source: Path):
    """Import the packaged identity helper straight from the synced sources."""
    sys.path.insert(0, str(source / "src"))
    from omodachi_core import host_identity
    return host_identity


# AUTH-1. The PAM entry is the one thing this installer writes outside the
# user's own home, and it is the one thing that can make a machine harder to
# get into, so it is opt-in (`--pam`), it always keeps a byte-exact backup, and
# `--remove-pam` takes back exactly what it added (RELEASE-9: out of the files
# as they are then, never by writing the old backup over later changes).
# `sudo -n` on purpose: an installer that could sit at a password prompt inside
# an ssh session is how a host ends up half-configured.
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
PAM_FILES = ("pam_install.py", "pam_helper.py", "pam_enroll.py")
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
    # RELEASE-9 B1: the helper only accepts keys root holds. Enrol the ones the
    # owner's devices have registered, in the same sudo step, after showing them.
    enroll_pam(source)
    print("PAM entry installed. It does nothing until `omodachi-host preferences set "
          "--revision N --biometric-auth true` and a device key is enrolled for it "
          "(`install_host.py --local --pam-enroll`, which asks for your password).", flush=True)
    print("Every failure path falls through to the password prompt; "
          "`install_host.py --local --remove-pam` takes exactly those lines back out.", flush=True)
    return code


def _pam_enroll(source: Path):
    """The key-store module from the synced sources, by path (standard library only)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "omodachi_pam_enroll", source / "src/omodachi_core/pam_enroll.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def enroll_pam(source: Path, devices=None) -> int:
    """RELEASE-9 B1: copy device approval keys into root's store, with sudo.

    The keys come from the owner's own files, so they are printed first - the
    person typing the password is the check that these are their devices - and
    root validates every one again before writing it.
    """
    module = _pam_enroll(source)
    owner = os.environ.get("USER") or Path.home().name
    try:
        request = module.collect_request(Path.home() / ".config/omodachi", owner, devices)
        lines = module.describe(request)
    except module.EnrollError as error:
        print(f"no device key enrolled for password prompts: {error}", file=sys.stderr)
        return 1
    if not lines:
        print("no paired device has turned on 'approve host password prompts' yet; nothing to "
              "enrol. Turn it on in the app, then run install_host.py --local --pam-enroll.", flush=True)
        return 0
    print("These device keys will be able to answer this computer's password prompts:", flush=True)
    for line in lines:
        print(line, flush=True)
    if _visible_terminal():
        answer = input("Enrol them? This asks for your password. [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("nothing was enrolled", flush=True)
            return 1
    return _pam(source, ["enroll", "--keys-json", json.dumps(request, separators=(",", ":"))])


def unenroll_pam(source: Path, devices) -> int:
    arguments = ["unenroll", "--all"] if devices == ["all"] else ["unenroll"] + [
        item for device in devices for item in ("--device", device)]
    return _pam(source, arguments)


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


# RELEASE-9. The venv and venv.previous are replaced or deleted only when this
# installer can show it made them - the rule RELEASE-8 gave the plugin's src.
# Every venv it creates gets a random id in VENV_ID_FILE inside it, and the
# same id in VENV_RECORD (0600) outside it; a directory is ours when both
# agree. A venv made before this record existed is adopted only if it is
# unmistakably an earlier install's: a Python venv whose omodachi-core was
# installed from ~/.local/share/omodachi/src (pip's direct_url.json says so)
# and holding nothing but that, pip, and packages at exactly the versions in
# requirements/host.lock. Everything else - a venv somebody made, a link, a
# file - is left exactly where it is, Install stops before changing anything,
# and --remove keeps it and says so.
VENV_ID_FILE = "omodachi-venv-id"
VENV_RECORD = ".local/state/omodachi/venv-ids.json"
_ID = re.compile(r"[0-9a-f]{32}\Z")


def _read_venv_ids(home: Path) -> list[str]:
    try:
        value = json.loads((home / VENV_RECORD).read_text())
    except (OSError, ValueError):
        return []
    ids = value.get("ids") if isinstance(value, dict) else None
    return [item for item in ids if isinstance(item, str) and _ID.fullmatch(item)] \
        if isinstance(ids, list) else []


def _write_venv_ids(home: Path, ids) -> None:
    path = home / VENV_RECORD
    ids = sorted(set(ids))
    if not ids:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(json.dumps({"schema": 1, "ids": ids}) + "\n")
    os.replace(temporary, path)


def _venv_id(venv: Path) -> str:
    marker = venv / VENV_ID_FILE
    try:
        if marker.is_symlink() or not marker.is_file():
            return ""
        with marker.open("rb") as handle:
            value = handle.read(64).decode("ascii", "replace").strip()
    except OSError:
        return ""
    return value if _ID.fullmatch(value) else ""


def _lock_pins(source: Path) -> dict[str, str]:
    """{normalised name: version} for every `name==version` in the lock."""
    pins = {}
    try:
        for line in (source / HOST_LOCK).read_text().splitlines():
            match = re.match(r"^([A-Za-z0-9._-]+)==([^\s;\\]+)", line.strip())
            if match:
                pins[re.sub(r"[-_.]+", "_", match.group(1)).lower()] = match.group(2)
    except OSError:
        pass
    return pins


def earlier_venv(home: Path, venv: Path, source: Path = ROOT) -> str:
    """"" when `venv` is unmistakably one an installer before RELEASE-9 made,
    else why not. Reads files only; nothing in the venv is run."""
    try:
        config = venv / "pyvenv.cfg"
        if config.is_symlink() or not config.is_file() or "home" not in config.read_text():
            return "it has no pyvenv.cfg, so it is not a Python venv"
        sites = [path for path in venv.glob("lib/python3.*/site-packages")
                 if path.is_dir() and not path.is_symlink()]
        if len(sites) != 1:
            return "it does not have exactly one site-packages"
        found = {}
        for info in sites[0].glob("*.dist-info"):
            name, _, version = info.name[:-len(".dist-info")].rpartition("-")
            found[re.sub(r"[-_.]+", "_", name).lower()] = (version, info)
        core = found.pop("omodachi_core", None)
        if core is None:
            return "omodachi-core is not installed in it"
        direct = json.loads((core[1] / "direct_url.json").read_text())
        wanted = (home / REMOTE_SOURCE).as_uri()
        if not isinstance(direct, dict) or direct.get("url") not in (wanted, wanted + "/"):
            return f"its omodachi-core was not installed from {home / REMOTE_SOURCE}"
        found.pop(PIP[2], None)   # pip itself: the interpreter's bundled one
        pins = _lock_pins(source)
        extra = sorted(f"{name}=={version}" for name, (version, _) in found.items()
                       if pins.get(name) != version)
        if extra:
            return "it holds packages the lock does not pin: " + ", ".join(extra[:5])
        if not {"aiohttp", "zeroconf", "setuptools"} <= set(found):
            return "it is missing packages every install puts there"
    except (OSError, ValueError) as error:
        return f"it could not be read ({error})"
    return ""


def venv_ownership(home: Path, venv: Path, source: Path = ROOT) -> tuple[str, str]:
    """("absent" | "ours" | "earlier" | "foreign", why) for a venv path."""
    if not os.path.lexists(venv):
        return "absent", ""
    if venv.is_symlink() or not venv.is_dir():
        return "foreign", "it is a link or a file, not a directory this installer made"
    identifier = _venv_id(venv)
    if identifier and identifier in _read_venv_ids(home):
        return "ours", ""
    why = earlier_venv(home, venv, source)
    if not why:
        return "earlier", ""
    return "foreign", ("its id does not match " + str(home / VENV_RECORD) if identifier
                       else "there is no record of this installer making it") + ", and " + why


def venv_refusal(home: Path, source: Path = ROOT) -> str:
    """Why Install must not touch the venvs, or "". Checked before any change."""
    share = home / SHARE_DIR
    for name in ("venv", "venv.previous"):
        kind, why = venv_ownership(home, share / name, source)
        if kind == "foreign":
            path = share / name
            return (f"{path} is not a virtualenv this installer made ({why}). It was left exactly "
                    f"as it is and nothing was installed. If it is yours, move it away, e.g.\n"
                    f"    mv {shlex.quote(str(path))} {shlex.quote(str(home / ('omodachi-' + name + '-moved-aside')))}\n"
                    f"and press Install again.")
    return ""


def files_refusal(home: Path, source: Path = ROOT) -> str:
    """Why Install must not write its units, commands, template or hook sources, or ""."""
    checks = [(home / ".config/systemd/user" / name, lambda text, name=name: unit_is_ours(name, text))
              for name in UNITS]
    checks += [(home / ".local/bin" / name, wrapper_is_ours) for name in ("omodachid", "omodachi-host")]
    checks += [(home / THEMED_DIR / THEME_TEMPLATE, lambda text: template_is_ours(text, source))]
    checks += [(home / HOOK_SOURCE_DIR / hook / HOOK_NAME, lambda text: HOOK_MARKER in text)
               for hook in HOOK_COMMANDS]
    for path, ours in checks:
        if not os.path.lexists(path):
            continue
        try:
            text = None if path.is_symlink() or not path.is_file() else path.read_text()
        except (OSError, UnicodeDecodeError):
            text = None
        if text is None or not ours(text):
            return (f"{path} is there and is not the one this installer writes; it was left as it "
                    f"is and nothing was installed. Move it aside and press Install again.")
    return ""


def install_venv(source: Path, venv: Path, home: Path | None = None) -> None:
    home = home or Path.home()
    lock = source / HOST_LOCK
    if not lock.is_file():
        raise SystemExit(f"{lock} is missing; these sources cannot be installed "
                         f"without their dependency lock")
    previous = venv.with_name(venv.name + ".previous")
    # Asked again right here, where it counts (install_local asked first).
    for path in (previous, venv):
        kind, why = venv_ownership(home, path, source)
        if kind == "foreign":
            raise SystemExit(f"{path} is not a virtualenv this installer made ({why}); "
                             f"it was left as it is")
    ids = _read_venv_ids(home)
    for path in (venv, previous):
        if venv_ownership(home, path, source)[0] == "earlier":
            print(f"{path} is the virtualenv an earlier Omodachi installer built (requirements/"
                  f"host.lock plus omodachi-core from {home / REMOTE_SOURCE}); replacing it", flush=True)
    if os.path.lexists(previous):
        ids = [item for item in ids if item != _venv_id(previous)]
        shutil.rmtree(previous)
    moved = False
    if os.path.lexists(venv):
        os.rename(venv, previous)
        moved = True
    identifier = secrets.token_hex(16)
    # Recorded before the directory exists, so a run killed half-way still
    # leaves a venv that is provably ours.
    _write_venv_ids(home, [*ids, identifier])
    try:
        run(["python3", "-I", "-m", "venv", str(venv)], env=python_environment())
        descriptor = os.open(venv / VENV_ID_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(identifier + "\n")
        for argv in pip_commands(source, venv):
            run(argv, env=python_environment())
    except BaseException:
        # `venv` did not exist when this run began (it was moved aside or was
        # absent), so whatever is there now is what this run made.
        shutil.rmtree(venv, ignore_errors=True)
        if moved:
            os.rename(previous, venv)
            print(f"the install failed; {venv} is back to what it was before", file=sys.stderr)
        _write_venv_ids(home, ids)
        raise
    if moved:
        shutil.rmtree(previous, ignore_errors=True)
    _write_venv_ids(home, [item for item in ids if os.path.lexists(previous)
                           and item == _venv_id(previous)] + [identifier])


def remove_venvs(home: Path, source: Path = ROOT) -> dict:
    """--remove: delete venv and venv.previous only when they are ours."""
    share = home / SHARE_DIR
    result = {"removed": [], "kept": {}}
    ids = _read_venv_ids(home)
    for name in ("venv.previous", "venv"):
        path = share / name
        kind, why = venv_ownership(home, path, source)
        if kind in ("ours", "earlier"):
            identifier = _venv_id(path)
            shutil.rmtree(path, ignore_errors=True)
            ids = [item for item in ids if item != identifier]
            result["removed"].append(name)
        elif kind == "foreign":
            result["kept"][str(path)] = why
            print(f"kept {path}: it is not a virtualenv this installer made ({why})", flush=True)
    _write_venv_ids(home, ids)
    return result


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
    # RELEASE-9: before anything is changed, the venvs and the files this run
    # would replace have to be this installer's own.
    refusal = venv_refusal(home, source) or files_refusal(home, source)
    if refusal:
        print("refusing to install: " + refusal, file=sys.stderr)
        return 2
    for directory in (home / ".config/omodachi", home / ".cache/omodachi",
                      share / "agent-workspace", home / ".local/state/omodachi/remote"):
        directory.mkdir(parents=True, exist_ok=True)
    # The owned app-server's capability token lives here; only this user reads it.
    (home / ".config/omodachi/agent").mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home / ".config/omodachi/agent", 0o700)

    # RELEASE-9: apps.json is Sunshine's configuration; one entry goes in only
    # for a Sunshine this installer is about to manage (none that something
    # else set up, nothing with --no-sunshine).
    app_name = _sunshine_app_name(source)
    config_created = not os.path.lexists(home / ".config/sunshine")
    managing = sunshine and _sunshine_package(source).unit_owner(home)["state"] != "foreign"
    apps = ensure_sunshine_app(home, app_name) if managing else {"changed": False, "reason": "skipped"}
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
                         adapter=sunshine_adapter, config_created=config_created)
    else:
        print("not installing the managed Sunshine fork (--no-sunshine)", flush=True)
        lock_existing_fork(source)

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
        install_venv(source, venv, home)
    except subprocess.CalledProcessError as error:
        # pip has already said why (a hash that does not match the lock, no
        # network); the traceback under it would only bury that line.
        print(f"installing the locked dependencies failed (exit {error.returncode}); "
              f"pip's own message above says why", file=sys.stderr)
        return 1

    for name in ("omodachid", "omodachi-host"):
        write(home / ".local/bin" / name, WRAPPER % name, 0o755, ours=wrapper_is_ours)
    for name, body in UNITS.items():
        write(home / ".config/systemd/user" / name, body,
              ours=lambda text, name=name: unit_is_ours(name, text))
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
                        help="uninstall what Install made: the units, the venvs, the synced "
                             "sources, the wrappers, the desktop entry, the managed Sunshine "
                             "fork, the firewall rules, the Omarchy surfaces, the "
                             "authorized_keys lines Omodachi wrote and, if installed, the PAM "
                             "entry (asks for your password); exits 3 if something that grants "
                             "access could not be removed")
    parser.add_argument("--purge", action="store_true",
                        help="with --remove, also delete the device secret, the host "
                             "certificate, every pairing and the rest of the host's state - "
                             "only the files Omodachi creates; anything else in "
                             "~/.config/omodachi, ~/.cache/omodachi and ~/.local/state/omodachi, "
                             "and agent-workspace, is kept and listed")
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
                             "answer a host password prompt; off by default, needs sudo (asked "
                             "for once in a terminal), writes under /etc (see pam_install.py); "
                             "--remove and --remove-pam take it back")
    parser.add_argument("--pam-services", default=PAM_SERVICES,
                        help="which /etc/pam.d services get the entry (default: %(default)s)")
    parser.add_argument("--pam-timeout", type=int, default=45,
                        help="seconds a host prompt waits for the device before falling "
                             "back to the password (default: %(default)s)")
    parser.add_argument("--remove-pam", action="store_true",
                        help="take the lines --pam added back out of the PAM files as they are "
                             "now (a file changed around them is kept and reported), and "
                             "remove its helper, config and drop-ins")
    parser.add_argument("--pam-enroll", nargs="*", metavar="DEVICE",
                        help="RELEASE-9: let these devices' approval keys (default: every device "
                             "with the switch on) answer password prompts; asks for your password")
    parser.add_argument("--pam-unenroll", nargs="+", metavar="DEVICE",
                        help="RELEASE-9: stop accepting these devices' keys ('all' for every one)")
    args = parser.parse_args(argv)
    if args.local:
        if sys.platform != "linux":
            print("--local writes user units and only runs on the Linux host", file=sys.stderr)
            return 2
        source = Path.home() / REMOTE_SOURCE
        if args.remove_pam:
            return remove_pam(source if (source / "src").is_dir() else ROOT)
        if args.pam_enroll is not None:
            return enroll_pam(source if (source / "src").is_dir() else ROOT, args.pam_enroll or None)
        if args.pam_unenroll:
            return unenroll_pam(source if (source / "src").is_dir() else ROOT, args.pam_unenroll)
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
        if code == 0 and pam_outdated():
            print(f"\nOmodachi Host was only PARTLY installed: the device-approval PAM helper on "
                  f"this computer is from before RELEASE-9 (no protocol=2 in {PAM_CONF}) and must "
                  f"be reinstalled. Run\n    python3 -I -B {source / 'scripts/install_host.py'} "
                  f"--local --pam\nin this terminal (it asks for your password), or take it off "
                  f"with --remove-pam.", flush=True)
            code = PARTIAL
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
