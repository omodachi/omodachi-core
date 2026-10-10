# omodachi-core

The host daemon of Omodachi: the part that owns the real data and the real
actions on an [Omarchy](https://omarchy.org) computer.

<p>
  <img src="https://omodachi.app/img/shots/vm-02-install.webp" width="720" alt="The panel's Install button running the host installer in a terminal on a fresh Omarchy install">
</p>

<sub>The installer fetching this repository at its pinned commit on a fresh Omarchy VM. More at <a href="https://omodachi.app">omodachi.app</a>.</sub>

## Where this sits

Omodachi turns an iPhone or iPad into an extension of an Omarchy desktop. It
ships as four repositories, plus the site.

| Repository | What it is |
| --- | --- |
| [`omodachi-plugin`](https://github.com/omodachi/omodachi-plugin) | the Omarchy plugin: the bar icon, the panel and one-step pairing |
| **`omodachi-core`** | **this repository: the host daemon, where the weight of the system sits** |
| [`omodachi-ios`](https://github.com/omodachi/omodachi-ios) | the native iPhone and iPad app |
| [`omodachi-sunshine`](https://github.com/omodachi/omodachi-sunshine) | the Sunshine fork that drives the remote screen. Its Releases carry the prebuilt package |
| the site | **omodachi.app**. Its source is not published |

Core provides:

- the **control panel** data: the merged Omarchy menu, keybinding actions and
  workspaces, plus the routing that actually executes them;
- the **remote screen**: an owned headless Hyprland output in either `extend`
  or `takeover` mode, driven through the managed Sunshine fork, with WayVNC as a
  low-overhead alternative backend;
- the **theme and fonts** a native client renders with: the palette Omarchy
  itself rendered for the current theme, its `shell.toml` design tokens, the
  wallpaper, the host monospace family and Omarchy's private icon font;
- **bridges** for the default agent, Herdr, SSH, two-way audio, the voice
  uplink into the host's own Voxtype, and the notifications the Omarchy shell
  has already shown;
- a thin **Bonjour** advertisement and the local IPC surface the Omarchy plugin
  talks to.

Devices reach it over authenticated HTTPS and WSS on `0.0.0.0:8099`, behind a
self-signed certificate the companion pins when pairing succeeds, on the LAN or
over Tailscale. The Omarchy plugin reaches it over a user-owned Unix socket.

**Tailscale** works from core 0.2.0 with iOS app 0.2.0 (TAIL-2).
The app reaches the computer by its MagicDNS name (`name.<tailnet>.ts.net`; a
typed `100.x` address cannot pass iOS App Transport Security and the app says
so), recognises a computer it paired with at home by its `host_id` and pinned
certificate when it meets it on the tailnet, keeps one credential for both
roads, and its terminal and Remote follow the road in use. App 0.1.0 can pair on
the LAN only. Over Tailscale, who can reach this computer is decided by your
tailnet's access rules (ACLs), not by ufw: Tailscale accepts everything on
`tailscale0` before ufw sees it. If Tailscale SSH is on, port 22 over the tailnet
is Tailscale's, and Omodachi's SSH approval and revoke do not apply to it.
`omodachi-host tailnet` prints what Tailscale on this computer means for
Omodachi, and the panel shows the same warnings.

## Install

The normal way is the plugin, which is listed on the
[Omarchy plugin marketplace](https://plugins.omarchy.org/plugin.html?id=com.omodachi.host):

```sh
omarchy plugin add https://github.com/omodachi/omodachi-plugin.git --enable
```

then press Install on its panel. The plugin fetches this repository at the one
full commit its `omodachi.json` pins, checks the checkout byte for byte, and
runs this installer for you in a visible terminal.

Without the panel, run the same bootstrap yourself; it does exactly what the
button does, pin and checks included:

```sh
git clone https://github.com/omodachi/omodachi-plugin.git
python3 -I -B omodachi-plugin/tools/install_host.py            # install
python3 -I -B omodachi-plugin/tools/install_host.py --remove   # uninstall, keeping pairings
```

(`install_host.py --host user@omarchy` from a development machine syncs `src/`,
`pyproject.toml`, `requirements/host.lock` and the two scripts there first.)
The installer runs on the system `python3` with the standard library only.

### What Install changes on this computer

| Where | What |
| --- | --- |
| `~/.local/share/omodachi/` | `src/` (the checked checkout, put there by the plugin's bootstrap), `venv/` built from it, `sunshine/<commit>/` (the managed fork), `hooks/` (the sources of the two Omarchy hooks), `agent-workspace/` (the default agent's working directory; yours, never deleted) |
| `~/.config/omodachi/` | `device.secret`, `tls/` (a self-signed certificate, never replaced), `plugin.token` (the panel's own device credential, 0600, issued once), `sunshine-web-credentials.json` (see below); the daemon keeps its pairings and settings here too |
| `~/.config/systemd/user/` | `omodachid.service` and `omodachi-herdr.service`, enabled and started; the managed Sunshine's `app-dev.lizardbyte.app.Sunshine.service` |
| `~/.local/bin/`, `~/.local/share/applications/`, icons | the `omodachid`, `omodachi-host` and `omodachi-panel` commands, the desktop entry and its icon |
| `~/.config/omarchy/` | `themed/omodachi-theme.json.tpl` and the `theme-set` and `font-set` hooks (installed with `omarchy hook install`); the current theme is re-applied headless so Omarchy renders the template. Nothing else there, and never `~/.config/hypr` |
| `~/.config/sunshine/apps.json` | one app entry added (the original kept as `apps.json.omodachi-bak`) |
| ufw (sudo) | allow rules for `8099/tcp` and the Sunshine ports from the private ranges (and removal of the no-op `tailscale0` rules versions before 0.2.0 added); the installer then says whether ufw is actually active and filtering, and that it does not filter Tailscale |
| pacman | the fork's runtime libraries and `wayvnc`, only those missing |
| `~/.local/state/omodachi/`, `~/.cache/omodachi/` | the installer's records (which venv and Sunshine unit it made), Remote's session journals, the downloaded Sunshine archive |
| `tailscale serve` (when Tailscale runs) | one mapping, `https://<this computer>.<tailnet>.ts.net` → `https+insecure://127.0.0.1:8099`, on port 443 if nothing else uses it there, otherwise 8443, otherwise none (the command is printed instead). It is Odomo's address: a real certificate a phone already trusts, which a Home Screen web app needs for push. Never a path under somebody else's port, never `tailscale serve reset`. Recorded in `~/.local/state/omodachi/tailscale-serve.json`. Tailscale puts the name in public certificate-transparency logs, as it does for any `tailscale serve`. If you are not Tailscale's operator the installer prints `sudo tailscale set --operator=$USER` and does not run it. `omodachi-host odomo status`, `odomo enable` and `odomo disable` read, add and remove it later |
| `/etc` (only with `--pam`) | the opt-in device-approval PAM helper, its lines in `/etc/pam.d/sudo` and `polkit-1`, and the root-owned store of the device keys it accepts (`/etc/omodachi/pam/`, filled only by `--pam`/`--pam-enroll` after you see each key and type your password); see `src/omodachi_core/pam_install.py` |

While it runs, the daemon also adds one line per device you grant SSH to
`~/.ssh/authorized_keys`, marked `# omodachi:<device>` and written as
`restrict,pty,expiry-time=…` so it gives a terminal only and lapses with the
device's credential (a device approved without SSH needs a new approval on this
computer before it can add a key); points Voxtype's audio
device at its own source while a device dictates, keeping the original as
`config.toml.omodachi-dictation-bak` until it puts it back; moves the Omarchy
bar with `omarchy bar position` during a Remote session and back after it; and
runs the Omarchy menu's own `when` conditions to decide which rows to show.
For Odomo's push it creates `~/.config/omodachi/vapid.json` (this
host's VAPID key pair, 0600) the first time a browser asks for it, and keeps
each web device's push subscription in `push-subscriptions.json` beside it.

### Removing it

`--remove` takes back what Install made and nothing else: the units, commands,
desktop entry, venv, the managed Sunshine (its unit disabled only if Install
enabled it; the fork's own state, including the clients it paired, only if
`~/.config/sunshine` did not exist before Omodachi - otherwise it says that
state is kept), the `tailscale serve` mapping (only one Install made, and only
while it still points where Install pointed it; a mapping that was already
there, or that somebody changed, stays), the `apps.json` entry (and the file, if Install created it), the Omarchy template and hooks, the ufw
rules, every `authorized_keys` line marked as Omodachi's, a Voxtype config left
mid-dictation, and, if you used `--pam`, the PAM entry (it asks for your
password in the terminal). If something that grants access cannot be taken
back, it says the host was only partly removed and what to run, and exits 3.
Pairings, the certificate and the device secret stay, so a reinstall does not
have to pair again. `--remove --purge` deletes those too, but only the files
Omodachi itself creates; anything else you put in `~/.config/omodachi`,
`~/.cache/omodachi` or `~/.local/state/omodachi`, and `agent-workspace`, is
kept and listed. A checkout it made (`src`, the Sunshine build cache) that you
changed is kept and listed as well: it is deleted only while it is exactly the
commit it was made to hold.

A venv, a Sunshine unit or install directory, a `src` checkout, or a unit file,
command, theme template or hook of Omodachi's name that the installer cannot
show it made is never replaced or deleted: Install stops and says how to move
it aside, and `--remove` keeps it and says so. Every file the installer writes
goes through a new file and a rename, so it never writes through a link.

If a device-approval PAM entry from before this release is on the computer,
Install says the host is only partly installed and gives the `--pam` command
that replaces it (exit 3), rather than reporting success.

### How the install is verified

The plugin fetches this repository at one pinned full commit and checks the
checkout byte for byte before running anything in it. This installer then
installs only what `requirements/host.lock` names: exact versions, each with
the sha256 of every wheel a host may use (CPython 3.11-3.14, x86_64), covering
the build backend (setuptools) and every runtime and transitive dependency.
The venv is recreated on every install (the old one is put back if anything
fails) and filled with
`pip install --isolated --require-hashes --no-deps --only-binary=:all: -r requirements/host.lock`,
so pip refuses any file whose hash is not in the lock, resolves nothing and
builds no sdist. omodachi-core itself is then built from the checkout with
`--no-index --no-deps --no-build-isolation --check-build-dependencies`: pip
cannot reach an index, and the build backend is the locked setuptools, the
exact version `pyproject.toml` requires. pip itself is the interpreter's
bundled one from `python3 -m venv` and is never upgraded. The Sunshine
archive is checked against the sha256 pinned in
`src/omodachi_core/data/versions.json`; the optional overrides need an
explicit one too (`--sunshine-package` only with `--sunshine-sha256`, a git
`--sunshine-build` only at an exact `--sunshine-build-commit`), and are
refused otherwise. Every other package (WayVNC, the
fork's runtime libraries) comes from pacman, whose repositories are signed.
`scripts/update_host_lock.py` regenerates the lock from
`requirements/host.in`; `tests/test_host_lock.py` fails if any pip call in the
installer is not hash-checked or offline, or if the lock and `pyproject.toml`
disagree.

Under `~/.config/omarchy` it writes exactly three files of its own: the theme
template `themed/omodachi-theme.json.tpl` and the `theme-set` and `font-set`
hook scripts, installed with `omarchy hook install`. `--remove` takes back
those three and nothing else. It never touches `~/.config/hypr`.

The firewall step opens `8099/tcp` for `10.0.0.0/8`, `172.16.0.0/12` and
`192.168.0.0/16` with the comment `omodachi-core`, and the managed Sunshine
fork's `47984,47989,48010/tcp` and `47998:48000/udp` the same way with the
comment `omodachi-sunshine`. It needs sudo, a failure only warns,
`--no-firewall` skips it, and `--remove-firewall` takes exactly those two
comments back out. Rules are only a filter when ufw is enabled with incoming
traffic denied by default, so the step reads `ufw status verbose` afterwards and
says which it is: when ufw is missing or inactive it says plainly that the
daemon's and Sunshine's ports are reachable from every network the computer is
on (`sudo ufw enable` turns the rules on). The daemon listens on `0.0.0.0:8099`
either way, behind TLS and device credentials.

ufw does not filter Tailscale. Tailscale's own `ts-input` rule sits before ufw
in `INPUT` and accepts everything arriving on `tailscale0`, so over the tailnet
every listening port - 8099, 22, the Sunshine ports, the Sunshine admin page on
47990 - is open to whoever the tailnet's access rules allow (measured in
TAIL-1). Versions before 0.2.0 added `tailscale0` rules that never decided
anything; Install now removes those (only rules carrying the two comments above)
and the summary says the tailnet's ACL is the filter.

### The remote screen

Remote runs on a managed fork of Sunshine,
[`omodachi-sunshine`](https://github.com/omodachi/omodachi-sunshine). **Nobody
has to build it.** The installer takes the prebuilt package from that
repository's own Releases and puts it under
`~/.local/share/omodachi/sunshine/`, then writes and enables the Sunshine user
unit. Building from source is the fallback, and that fork's `FORK.md` carries
the recipe, the upstream base and the locked submodule commits.

It never takes over a Sunshine it did not set up: if the unit already exists
without this installer's marker, comes from a package (the distribution's
`sunshine`), or, with no unit of ours, has drop-ins nobody here wrote, the fork
is not installed, the existing one and its `apps.json` are left exactly as they
are, and Remote uses WayVNC. Drop-ins someone adds beside our own unit are kept
as their customisation of it; if one replaces its `ExecStart`, the installer
says the web page lockdown below is not in effect. A fork already
on disk is trusted as the pinned build without downloading only when its
`MANIFEST.sha256` hashes to the `manifest_sha256` pinned in
`src/omodachi_core/data/versions.json`, every file matches it and nothing else
is in the directory. Sunshine's web admin page (47990) is started with
`origin_web_ui_allowed=pc` and with its own credentials file already holding a
random user name and a password hash no password is known to match, so it
answers this computer only and nobody - on this computer or the network - can
claim it by being first to set a password, which upstream otherwise allows
until someone opens it. The installer reads the lockdown back from the unit
systemd resolved before saying so. It uses that credentials file of its own
even where `~/.config/sunshine/sunshine_state.json` already holds a login:
before this release that page could be claimed by anyone, so a login found
there is not taken as the user's. Nothing in that file is changed; to use the
fork's web page yourself, set a login with
`<fork>/sunshine credentials_file=$HOME/.config/omodachi/sunshine-web-credentials.json --creds <user> <password>`.

The package is pinned, not "latest": `src/omodachi_core/data/versions.json`
names the fork commit this core was tested against and the archive's sha256,
and for this release it resolves to

```
https://github.com/omodachi/omodachi-sunshine/releases/download/sunshine-e9f1b08/omodachi-sunshine-e9f1b08-x86_64.tar.zst
```

That archive was built from the public commit
`e9f1b089255e235b5e6aa13ed89b7dd3f569de87` of
[`omodachi-sunshine`](https://github.com/omodachi/omodachi-sunshine), on a
clean build machine and compiled so that none of that machine's paths is
left in the binary; its release notes give the recipe and the sources of the
libraries it links.

`--sunshine-package <url|path>` overrides it and `--sunshine-package latest`
asks for the newest release by name; either one needs `--sunshine-sha256 <hex>`
(a `.sha256` published beside the archive is not accepted). To build the fork
from source instead, `--sunshine-build` with no argument fetches that same
public commit from GitHub and builds it with the fork's own
`scripts/package_release.sh` (it needs the build dependencies in the fork's
`FORK.md`):

```sh
python3 -I -B ~/.local/share/omodachi/src/scripts/install_host.py --local --sunshine-build
# the same, spelled out
python3 -I -B ~/.local/share/omodachi/src/scripts/install_host.py --local \
  --sunshine-build https://github.com/omodachi/omodachi-sunshine \
  --sunshine-build-commit e9f1b089255e235b5e6aa13ed89b7dd3f569de87
```

and any other commit of that repository is built the same way, only ever at an
exact 40-character `--sunshine-build-commit`, never a branch head.
`--sunshine-build <path>` builds your own local tree as it stands. The git-URL
checkout lives in `~/.cache/omodachi/sunshine-src`; the next build or `--purge`
deletes it only while it is still exactly that commit (plus the build output
the fork's `.gitignore` names). If you changed it - an edit, a new file, a
commit of your own - a build moves it to `~/.local/share/omodachi-kept/` and
says so, and `--purge` keeps it and lists it.

The fork is GPL-3.0-only, inherited from upstream, and stays a separate
repository for that reason: it is somebody else's program with our patches on
it, not part of this one.

WayVNC is the alternative backend and needs no fork; `scripts/install_wayvnc.py`
installs it.

`omodachid --demo` serves bundled synthetic menu and agent data and performs no
host mutations, for a local daemon with no real Omarchy host behind it.

## Security model

Who can do what to this computer, and which code decides it. What Install
writes, and what `--remove` and `--remove --purge` take back, is above under
[What Install changes on this computer](#what-install-changes-on-this-computer)
and [Removing it](#removing-it). How the plugin fetches and checks this
repository before running it is in the plugin's README,
[What Install does](https://github.com/omodachi/omodachi-plugin#what-install-does).
To report a vulnerability, see [SECURITY.md](SECURITY.md).

- **The network can only ask.** Over HTTPS every path except `/`, `/health`
  and the two pairing calls needs a device credential
  (`src/omodachi_core/network.py`). A pairing request becomes a card on the
  plugin's Devices page and an Omarchy notification; one per device and two
  per source address wait at a time.
- **Approve happens on the computer.** Approve and Reject are operations on
  the daemon's Unix socket, which serves only the user the daemon runs as, so
  no request from the network can approve itself (`pairing.py`, `ipc.py`).
  `omodachi-host preferences set --revision <n> --pairing-mode invite` (the
  current `<n>` is in `omodachi-host preferences get`) makes a request need a
  one-time invitation as well.
- **One Approve, three grants.** It issues the device credential, the managed
  Sunshine's certificate for that device and, when the request carried an SSH
  key, one line in `~/.ssh/authorized_keys`. A credential lasts 30 days; in
  its last 7 the device may trade it for a new one, and the old one keeps
  working 24 hours after that. The daemon stores a hash of each credential,
  not the credential (`auth.py`).
- **Remove takes it all back in one call:** the credential, the Sunshine
  certificate, the `authorized_keys` line and, if the device had one, its
  password-approval key (`omodachi-host devices revoke <device>`, or Remove on
  the Devices page; `service.py`).
- **Rows that change the computer run on the second request.** Which rows
  those are is decided in `menu_actions.py`: the session and power rows, the
  factory reset, everything under Remove and Update except two that change
  nothing by themselves, and any row whose command powers off or ends the
  session, removes packages or deletes recursively, updates, sets a password,
  changes boot or security settings, is one of Omarchy's `remove` or `refresh`
  scripts, runs through `sudo`, `pkexec` or `doas`, or restarts audio, Wi-Fi,
  Bluetooth, the trackpad or the shell. The first request for such a row is
  refused with a one-use `confirm_token`; only a second one for the same row
  from the same device within 30 seconds runs it (`service.py`). The plugin's
  panel is the person at the computer and is not asked twice.
- **Over Tailscale, the tailnet decides who reaches the ports.** Tailscale
  accepts everything on `tailscale0` before ufw sees it, so the tailnet's access
  rules are the filter there; credentials and Approve still guard 8099 as
  everywhere. With Tailscale SSH on, port 22 over the tailnet is answered by
  Tailscale, not sshd, and Remove here does not end that access - the panel says
  so (`tailnet.py`). A request from the tailnet shows the requesting node's
  name and owner as this computer's own `tailscale whois` reports them, never
  as proof of anything; a device that already holds a credential may ask
  again, and approving it retires the old credential (`pairing.py`).
- **The SSH lines are narrow.** Each one is
  `restrict,pty,expiry-time="…" <key> # omodachi:<device>`: no port, agent or
  X11 forwarding and no `~/.ssh/rc`, a terminal only, lapsing one day after the
  credential. Your own lines are copied through byte for byte. A device
  approved without SSH that offers a key later is answered
  `409 ssh_approval_required`, and its key waits ten minutes for
  `omodachi-host ssh approve <device>` on the computer (`ssh_keys.py`,
  `service.py`).
- **You can always end a Remote session.** A device ends only its own; the
  person at the computer ends anyone's, whether or not that device still
  answers: `omodachi-host remote stop --all` from a terminal, a text console
  or SSH (a Take over has the screen off), or End Remote session on the
  panel's Overview. The authority is the Unix socket's own check that the
  caller is you, the same as Approve; no device credential can do it over the
  network, the panel's included (`remote/service.py`, `service.py`).
- **Odomo is the computer's own page, and a smaller key.** `GET /odomo/*`
  (and the two icons Safari asks the root for) needs no credential: it is a
  web app, fixed bytes read once into memory from this package and served with
  a strict CSP, and a page has to load before it can pair. It is built from
  `odomo/` and committed; `scripts/verify_odomo.py` rebuilds it with the
  hash-locked toolchain and compares byte for byte. A browser pairs through
  the same card and Approve as the App, but gets a `web` credential that
  reaches only the routes `browser_gate.WEB_ALLOWED` lists - the menu,
  keybindings, notifications, its own push subscription and (ODOMO-2) what the
  App's Herdr and Agent panels do: watch a pane (`observe`, never `control`),
  keys and text into it, split / zoom / close, tabs, start or make a session;
  the default agent's chat, its approvals and `/` commands - never Remote, SSH,
  the clipboard, the microphone, approval keys, or the two Herdr calls that
  move the computer's own screen (`pane.focus`, `workspace focus`). Its writes
  are audited with its device id like the App's (`herdr-writes.jsonl`; agent
  writes as `"omodachi":"web"` journal lines, never their words). ATTACH-1:
  it may also send files - `POST /v1/uploads` and `DELETE /v1/uploads/{id}`,
  the same as the App - and nothing reads one back. Anything carrying `Origin`
  is held to that list too, must name this host in `Host` and come from
  exactly `https://<that host>`, and opens a WebSocket only with a 30-second,
  single-use ticket (`browser_gate.py`). Behind `tailscale serve` every request
  comes from 127.0.0.1; only then are the forwarded address and
  `Tailscale-User-Login` read, for the card and nothing else.
- **A file from the phone is kept seven days, privately (ATTACH-1).**
  `POST /v1/uploads` writes one file into `~/.local/state/omodachi/uploads/`
  (0700; the file 0600) as `<agent|herdr|file>-<UTC>-<id>.<ext>`, so an agent
  can read it by path. What is kept is decided by what the bytes are: PNG,
  JPEG, GIF and WebP by their signature (HEIC is converted to JPEG with
  libheif's `heif-dec`, else ImageMagick), PDF by `%PDF-`, text and code by
  extension with every byte checked (UTF-8, no NUL or other control byte). SVG
  is refused (markup that can carry script), and so is everything else, with
  the reason. 25 MB at most, counted while the body streams in; nothing past
  that is written. Files are swept seven days after they arrive, at start and
  hourly; `DELETE /v1/uploads/{id}` takes one back before a send. No route
  serves an upload back. The journal gets one line per upload -
  `{"omodachi":"upload","event","device","kind","bytes"}` - never the name or
  the content.
- **Push goes out, and says little.** For a web device that turned
  notifications on, the daemon makes outbound HTTPS requests to that
  browser's push service - `*.push.apple.com`, `fcm.googleapis.com`,
  `updates.push.services.mozilla.com` (or `*.notify.windows.com` for Edge) -
  and to nothing else: a subscription for any other address is refused. Each
  message is encrypted for that one browser (RFC 8291) and signed with the
  host's VAPID key (`~/.config/omodachi/vapid.json`, 0600). It says which of
  five things happened (the agent needs approval, is waiting, is done; an
  agent in Herdr is blocked; a desktop notification) and links to this host's
  own Odomo with ids only - no credential, no question text. Revoke, purge and
  the push service's own "gone" (404/410) delete the subscription; the Devices
  page can switch push off for one browser (`push.py`, `webpush.py`). The
  sender is the one new dependency, `cryptography`, hash-locked like the rest
  in `requirements/host.lock`.
- **VNC has no port.** WayVNC listens only on a Unix socket in a 0700
  directory of its own, and the daemon's authenticated WSS connection is what
  dials it (`remote/vnc.py`).
- **Sunshine's web page is closed.** The managed fork starts with
  `origin_web_ui_allowed=pc`, so its admin page on 47990 answers this computer
  only, and with a 0600 credentials file of ours holding a random user name
  and a password hash no password is known to match, so nobody can claim the
  page by setting the first password (`sunshine_package.py`).
- **Password prompts stay yours unless you opt in.** Only
  `install_host.py --pam` installs the PAM entry, for `sudo` and `polkit-1`,
  and the `biometric_auth` preference that the daemon also checks ships off
  (`biometric.py`). With both on, the helper PAM runs as root makes a fresh
  nonce for each prompt and accepts only an approval signed with
  a P-256 device key from the root-owned `/etc/omodachi/pam/keys.json`, which
  only a root step you type your password for can fill. Every other outcome,
  a timeout included, falls through to the password prompt (`pam_helper.py`,
  `pam_enroll.py`).

## Screenshots

None here. The interface this daemon feeds lives on the device, and the site
draws it: **omodachi.app**.

## Layout

| Path | Contents |
| --- | --- |
| `src/omodachi_core/` | the package: hub, catalog, routes, agent, audio, discovery |
| `src/omodachi_core/remote/` | the remote screen: one session, two modes, two backends |
| `contracts/` | JSON Schema documents and generated wire fixtures |
| `odomo/` | Odomo's source; the committed build is `src/omodachi_core/data/odomo/` |
| `scripts/` | host installer, contract verifier, host-side probes |
| `tests/` | the unit, IPC, HTTPS/WSS and real-subprocess suites |
| `docs/` | how the subsystems work |

## Tests

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'   # a development venv, not the host's
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
.venv/bin/python scripts/verify_contracts.py
```

`verify_contracts.py --write` regenerates the fixtures under
`contracts/fixtures/` from the current serializers. Review that diff together
with the implementation change that caused it.

## Documentation

- [local integration, the HTTP/IPC surface and the owned SSH keys](docs/local-integration.md)
- [the host theme](docs/theme.md)
- [the host fonts](docs/fonts.md)
- [the default agent: app-server, approvals, models, usage](docs/agent.md)
- [the Herdr bridge](docs/herdr.md)
- [pairing and the pinned host certificate](docs/pairing.md)
- [device hub, transport and events](docs/hub.md)
- [catalog compilation and route policy](docs/catalog-routes.md)
- [the remote screen: API, modes and recovery](docs/remote-api.md)
- [desktop profile planning](docs/desktop-profile.md)
- [managed Sunshine control IPC](docs/sunshine-ipc.md)
- [WayVNC backend](docs/wayvnc.md)
- [microphone uplink](docs/audio-uplink-contract.md)
- [voice: the uplink, Voxtype and the transcript](docs/voice.md)
- [notification sync, and Web Push to Odomo](docs/notifications.md)
- [LAN discovery](docs/lan-discovery.md)
- [contract registry](contracts/README.md)

## Contributing

Issues and pull requests are welcome. Run the two commands under **Tests**
before you open one, and say which Omarchy version you ran against. A change to
a wire shape belongs in `contracts/` in the same commit as the code that
produces it, because the plugin and the App both drive their models from those
fixtures. Keep the subject line of a commit a sentence about behaviour.

## Licence

**MIT.** See [LICENSE](LICENSE). The Omarchy plugin is MIT as well, so a
client of any kind can be written against this daemon. The iPhone and iPad app
is **GPL-3.0**, which two vendored streaming components decide for it, and the
managed Sunshine fork keeps upstream's **GPL-3.0-only**.

---

Omodachi is an independent project with no tie to Omarchy upstream.
