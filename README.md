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

Devices reach it over authenticated HTTPS and WSS on the LAN or over Tailscale,
on `0.0.0.0:8099` behind a self-signed certificate the companion pins when
pairing succeeds. The Omarchy plugin reaches it over a user-owned Unix socket.

## Install

The normal way is the plugin:

```sh
omarchy plugin add https://github.com/omodachi/omodachi-plugin.git --enable
```

then press Install on its panel. The plugin clones this repository at the tag
it pins (`v0.1.0` for plugin 0.1.0) and runs this installer for you, in a
visible terminal.

By hand, on the computer itself, the same thing the plugin does:

```sh
git clone https://github.com/omodachi/omodachi-core.git ~/.local/share/omodachi/src
python3 ~/.local/share/omodachi/src/scripts/install_host.py --local
```

(`install_host.py --host user@omarchy` from a development machine syncs `src/`,
`pyproject.toml`, `requirements/host.lock` and the two scripts there first.)
The installer runs on the system `python3` with the standard library only. It
builds `~/.local/share/omodachi/venv` from those sources, generates a host certificate under
`~/.config/omodachi/tls/` if there is none, writes `omodachid.service` and
`omodachi-herdr.service` into `~/.config/systemd/user/`, installs
`~/.local/bin/omodachi-panel` and the desktop entry, opens the LAN firewall
rules and enables the units. It is idempotent, writes no credentials or device
state, and never replaces an existing certificate.

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

The firewall step copies `omarchy-install-service-sunshine`. It opens
`8099/tcp` for `10.0.0.0/8`, `172.16.0.0/12` and `192.168.0.0/16` plus
`tailscale0` with the comment `omodachi-core`, and the managed Sunshine fork's
`47984,47989,48010/tcp` and `47998:48000/udp` the same way with the comment
`omodachi-sunshine`. It needs sudo, a failure only warns, `--no-firewall` skips
it, and `--remove-firewall` takes exactly those two comments back out.

### The remote screen

Remote runs on a managed fork of Sunshine,
[`omodachi-sunshine`](https://github.com/omodachi/omodachi-sunshine). **Nobody
has to build it.** The installer takes the prebuilt package from that
repository's own Releases and puts it under
`~/.local/share/omodachi/sunshine/`, then writes and enables the Sunshine user
unit. Building from source is the fallback, and that fork's `FORK.md` carries
the recipe, the upstream base and the locked submodule commits.

The package is pinned, not "latest": `src/omodachi_core/data/versions.json`
names the fork commit this core was tested against and the archive's sha256,
and for this release it resolves to

```
https://github.com/omodachi/omodachi-sunshine/releases/download/sunshine-328d231/omodachi-sunshine-328d231-x86_64.tar.zst
```

`--sunshine-package <url|path>` overrides it and `--sunshine-package latest`
asks for the newest release by name; either one needs `--sunshine-sha256 <hex>`
(a `.sha256` published beside the archive is not accepted). `--sunshine-build
<git-url> --sunshine-build-commit <sha>` builds the fork at exactly that commit;
`--sunshine-build <path>` builds your own local tree as it stands.

The fork is GPL-3.0-only, inherited from upstream, and stays a separate
repository for that reason: it is somebody else's program with our patches on
it, not part of this one.

WayVNC is the alternative backend and needs no fork; `scripts/install_wayvnc.py`
installs it.

`omodachid --demo` serves bundled synthetic menu and agent data and performs no
host mutations, for a local daemon with no real Omarchy host behind it.

## Screenshots

None here. The interface this daemon feeds lives on the device, and the site
draws it: **omodachi.app**.

## Layout

| Path | Contents |
| --- | --- |
| `src/omodachi_core/` | the package: hub, catalog, routes, agent, audio, discovery |
| `src/omodachi_core/remote/` | the remote screen: one session, two modes, two backends |
| `contracts/` | JSON Schema documents and generated wire fixtures |
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
- [notification sync](docs/notifications.md)
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
