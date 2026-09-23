# Local integration and the request surface

Contract revision: `omodachi.v1`.

## Running a daemon locally

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

`omodachid --demo` serves bundled synthetic menu and default-agent data and one
in-memory notification toggle; it never executes a host command. Without
`--demo` the daemon reads the installed Omarchy menu sources and the read-only
Herdr CLI. A missing Herdr or Sunshine is reported as unavailable and does not
prevent the hub from starting. A bounded read-only agent/Herdr probe runs
periodically in the background; probe calls never overlap, and a timeout keeps
the last confirmed identity while lowering readiness.

`--default-menu`, `--user-menu`, `--omodachi-menu`, `--secret-file`, the socket
path and the TLS paths are local administrator inputs, not API parameters.
`--socket` defaults to `/run/omodachi/<uid>/omodachid.sock` when the host's
opt-in root step has made that directory and `$XDG_RUNTIME_DIR/omodachi/`
otherwise; see [hub.md](hub.md) for why, and for what keeps the old path
working.

Credentials are issued and revoked by a separate process while the daemon runs;
the daemon reloads the credential registry on every check. Store the output in a
`0600` file. `omodachi-host` reads its credential from `OMODACHI_TOKEN`. Two subcommands
need none: `omodachi-host theme-changed` and `omodachi-host font-changed` are
what the installed Omarchy hooks run, and their authority is the verified Unix
peer, the same as pairing.

```sh
omodachid --secret-file <private-dir>/device.secret --issue-token ipad
omodachid --demo --socket <private-dir>/hub.sock \
  --secret-file <private-dir>/device.secret \
  --listen 127.0.0.1 --port 8099 \
  --tls-cert <private-dir>/server.pem --tls-key <private-dir>/server.key
```

HTTPS/WSS requires a certificate and key. On a host install these are the
self-signed pair `install_host.py` generates under `~/.config/omodachi/tls/`,
and the unit listens on `0.0.0.0:8099`; clients pin its fingerprint at pairing
time (see [pairing.md](pairing.md)). Hostname and IP checks are never bypassed. The only plaintext option is `--allow-loopback-http`
on a literal loopback address; it cannot bind plaintext on `0.0.0.0` or a LAN
address. Access logging is off so credentials and input are not written to logs.

`scripts/manage_simulator_backend.py` owns a detached development instance for
iOS Simulator work. Its endpoint, CA path, device-credential file paths, PID and
log path are recorded in the ignored `.runtime/simulator-backend.json` (0600);
token and key material are never written there. Stop uses the recorded PID and
waits before a forced cleanup; a live socket is never deleted to take over an
instance.

## HTTP surface

Every `/v1/*` request needs `Authorization: Bearer <device credential>`. The
network never issues credentials. Device identity is always taken from the
credential, never from request JSON. Unknown JSON fields are rejected at
operation boundaries.

| Method and path | Behavior |
| --- | --- |
| `GET /` | unauthenticated boundary hint; this is the host API, not a web UI |
| `GET /health` | service and revision health, plus `host_id` and `tls_fingerprint_sha256`; no device data |
| `GET /v1/state` | host, bar, workspace, focus, agent, Herdr, catalog and Remote snapshot with state revision, event cursor and instance ID |
| `GET /v1/capabilities` | reported capability flags |
| `GET /v1/catalog` | three-source merged catalog with condition/provider state, route policy and readiness; `?q=` searches the same catalog |
| `GET /v1/herdr` | sanitized summary plus the detailed read-only snapshot |
| `GET /v1/theme`, `GET /v1/theme/background` | the host's current Omarchy theme and its wallpaper (see [theme.md](theme.md)) |
| `GET /v1/fonts`, `GET /v1/fonts/{id}` | the host monospace family, its fontconfig fallback chain and Omarchy's icon font (see [fonts.md](fonts.md)) |
| `GET /v1/icons/{name}?size=64` | one application icon from the host's icon theme (see [icons.md](icons.md)) |
| `GET /v1/herdr/layout` | the owned `omodachi` Herdr session as workspaces, tabs and panes (see [herdr.md](herdr.md)) |
| `GET /v1/shortcuts` | host keybinding rows from the shortcut provider (see [Shortcuts](#shortcuts)) |
| `GET /v1/preferences` | host preference values and profile defaults |
| `GET /v1/events` (WebSocket) | device events with full replay or an explicit snapshot/resync |
| `POST /v1/actions/{id}:invoke` | catalog lookup, current source and condition, revision, finite params and registered fixed argv in one boundary |
| `POST /v1/workspaces/{id}/select` | switch the host workspace through the catalog entry |
| `POST /v1/control/wake` | wake the host display through the reviewed host adapter |
| `POST /v1/pairing/requests`, `POST /v1/pairing/requests/{id}/claim` | local pairing handshake; claim also returns the pinned host identity and endpoints (see [pairing.md](pairing.md)) |
| `POST /v1/media/pairing/*`, `GET`/`DELETE /v1/media/pairing/requests/{id}` | Sunshine-fork pairing bridge |
| `POST /v1/agent/tasks`, `POST /v1/agent/default:ensure` | default-agent lifecycle |
| `GET`/`POST /v1/agent/default/chat*` | agent chat snapshot, events, messages, interrupt, commands, handoff and empty-thread recovery |
| `GET /v1/herdr/panes/{pane}/{observe,control}` (WebSocket) | the official Herdr terminal streams, one NDJSON line per message |
| `POST /v1/herdr/panes/{pane}/{split,zoom,focus,close}`, `POST /v1/herdr/workspaces/{id}/select` | Herdr pane and workspace controls |
| `POST /v1/workspaces/{id}/select`, `POST /v1/workspaces/relative/{e+1\|e-1}/select` | select a host workspace by number, or by direction |
| `GET /v1/audio/input`, `GET /v1/remote/sessions/{id}/audio` (WebSocket) | microphone uplink capability and channel |
| `GET /v1/remote/capabilities` | backends and why each is available, modes, placements, encoder limits |
| `POST /v1/remote/sessions` | create the one Remote session; 409 when one already exists |
| `GET`/`DELETE /v1/remote/sessions/{id}` | read the session; idempotent release |
| `POST /v1/remote/sessions/{id}/{resize,backend,heartbeat,presented}` | server-driven geometry or backend change, heartbeat, presentation telemetry |

`panel.summon` prepares a local view without asserting that QML opened, or
targets the device that owns the current Remote session. `omodachi-host
panel-summon [--view overview|keybindings|settings]` names the destination and
the answer echoes it; with a session, the recall published to the owning device
carries the same `view`, so a host shortcut bound to the keybindings overlay
lands there on the iPad instead of on its root panel. An unknown view is
`invalid_request`, never a silent downgrade.

`settings` is the third destination (ARCH-1, Study 04 A-59). The Omodachi
plugin's own bar widget is what a user clicks to reach the app's preferences,
and while a session is up that click belongs on the device holding the picture
rather than on a QML panel behind it — so the plugin sends
`panel-summon --view settings` from a session and keeps its local panel on the
right button. The recall is still one event to one device
(`contracts/fixtures/event-panel-summon.json`); the client decides what to do
with a repeat of the same view, because the hub does not remember what is open.

`workspace.select` takes either a `workspace_id` or a `relative` of `e+1` /
`e-1`, never both. The relative form is resolved here, against the collection
this host publishes in `state.workspace.items` (the fixed five plus whatever
exists, `Workspaces.qml`'s own rule), and it wraps. It exists so a touch client
binding a swipe to "next workspace" does not compute a neighbour from a snapshot
that has already moved (ARCH-1, Study 04 A-64): the only reading that cannot be
wrong is the one the host just took.

## State shape

`state.bar` is a semantic projection of the host `shell.json`: `position`
(`top`, `bottom`, `left`, `right` or `null`), source status, revision and
reviewed widget IDs and roles. Panel and native consumers anchor on the same
position; core does not prescribe animation direction.

`state.bar.modules` is the status-bearing subset of that same layout, in layout
order, as `{id, role, status}`; a client joins it to `left`/`center`/`right` by
`id`. Only modules the host's own bar actually carries appear, and `status` is
`null` whenever core has no reading - a bar draws nothing rather than a zero it
made up. The readings are taken read-only on the daemon's two-second source
refresh and throttled to that same period.

| role | widget | source |
| --- | --- | --- |
| `audio` | `omarchy.audio` | `wpctl get-volume @DEFAULT_AUDIO_SINK@` → `{volume, muted}` |
| `power` | `omarchy.power` | `/sys/class/power_supply/*` → `{percent, state, charging}`; a host with no battery publishes `null` |
| `network` | `omarchy.network` | `omarchy-network-status`, the command the shell's own network panel samples → `{kind, name, signal}` |

`state.bar.geometry` is where that bar physically is, on the output a Remote
session owns, so the client can put its own mark over the host's Omarchy logo
instead of asking a plugin to intercept it (MENU-2, Study 04 A-67). It is
`null` whenever there is no session, no `omarchy-bar` layer on that output, or
nothing measurable; otherwise:

| field | meaning |
| --- | --- |
| `output` | the output the session owns, so a geometry for a session that has been replaced is recognisable |
| `logical_size` | that output's logical size, so every rectangle below is a *fraction* of the picture and survives letterboxing, rotation and any encoder resolution |
| `position` | `top` / `bottom` / `left` / `right`. `bar.position` is optional in `shell.json`; when it is absent the edge is **measured** off the layer rectangle rather than assumed |
| `bar` | the `omarchy-bar` layer surface, in the output's own logical coordinates |
| `logo` | the leading square, and only when the first entry of the leading section really is `omarchy.menu`; a logo the user moved, replaced or removed is `null` |

The layer box comes from `hyprctl -j layers`, which reports **layout**
coordinates — the session's output at `x=2304` reports its bar at `x=2304` — so
the session's own origin is subtracted. The reading is a subprocess, throttled
to the same two seconds as the bar modules and taken on a worker; the
compositor's `openlayer` / `closelayer omarchy-bar` marks it stale so a bar that
appeared, went away or followed a new output is re-read on the next tick.

**The Omodachi plugin's own bar slot is deliberately not located.** It is one
widget among a dozen third-party ones and its position moves whenever any of
them changes, so a rectangle for it would be a mark of ours landing on somebody
else's widget. That icon behaves from the device exactly as it does for a person
sitting at the machine, and the one thing a takeover changes about it — with the
screens blanked there is nothing useful for it to open — is decided inside
`com.omodachi.host`, which already reads the session out of this state. Core
publishes nothing for it and accepts no report about it.

**Notifications are deliberately not published.** The only unread count lives in
the shell's in-memory popup model; `~/.local/state/omarchy/notifications/history/`
holds *recorded history*, which is a different number, and the `notifications`
IPC target only exposes the Do-Not-Disturb flag and needs a live `quickshell` -
the dependency PERF-1 §6.3 took off the Remote path precisely because the shell
restarts under it. The notification widget on Leo's host
(`jankeesvw.notification-center`) is third-party besides. Bluetooth, weather,
display, fans, keyboard layout and system updates are in the same position: no
cheap read-only source, or not first-party. They stay layout-only, with role
`unsupported` where they are not reviewed at all.

`state.workspace.items` holds the workspace rows with nullable occupancy,
`persistent` and catalog-backed `select_entry_id` / `move_entry_id`. The rows
are the set the official bar draws: 1-5 always, plus any other live workspace
(`shell/plugins/bar/widgets/Workspaces.qml:21-31`). `persistent` marks the fixed
five, so a self-drawn bar can show the same collection; a persistent row the
compositor did not list is unoccupied, and with no compositor reading at all its
occupancy is `null` rather than a guess. `state.focus` holds only an
app summary and an opaque target token. Moving a window uses the current state
revision and that token; there is no focused-pane fallback.

Panel workspace selection uses the catalog `select_entry_id`. Every row in that
collection can be selected, including a persistent row the compositor has not
materialised: Hyprland switches to an empty numbered workspace the same way
`SUPER+3` does on the host, and the compositor drops it again when it is left
empty. A workspace outside the published collection is still refused with
`workspace_unavailable`. With Remote
closed it switches the host workspace and starts neither Remote nor a terminal.
With a Remote session open the selection acts on the session's own output:
`focus_workspace_on_output()` moves the workspace to `session.output_name` and
focuses it there, so the tablet's screen changes and the laptop panel keeps the
workspace it was on. `workspace.select`'s relative form (`e+1`/`e-1`, resolved
against the published `state.workspace.items`) goes the same way. `state.workspace`
and its WSS updates are the authority for the result.

`state.remote` is `{session_id, state, mode, backend, revision}` and
`state.remote_bar` is the live output and workspace projection Panel anchors on.
See [remote-api.md](remote-api.md).

The desktop geometry concepts stay distinct: stream pixels, output mode pixels,
output scale and logical size, and client point geometry. See
[desktop-profile.md](desktop-profile.md).

## Shortcuts

`GET /v1/shortcuts` lists one row per record the host's own
`omarchy-menu-keybindings` publishes, in the host's order. Core does not read
`hyprctl binds -j` for this: on Omarchy 4 / Hyprland 0.56 the compositor reports
every Lua bind as `{"dispatcher": "__lua", "arg": "<callback index>"}`, which is
an index into the config's closure table and carries no replayable expression.
`output_binding_records` rebuilds the missing half from the Lua config source,
so that is the record core keys on.

Executing a row is what pressing the key on the host does. Each row says how, in
`execution`:

| `execution.kind` | record dispatcher | how core replays it |
| --- | --- | --- |
| `exec` | `exec` | runs `detail` through a shell in the graphical session, as the user, detached — the same thing Hyprland's own exec does, except core keeps the pid |
| `eval` | `lua` | `hyprctl dispatch "<hl.* expression>"`, which on a Lua config evaluates that expression |
| `sendshortcut` | `sendshortcut` | the host's own `hl.dsp.send_key_state` down/up pair |
| `dispatch` | anything else | `hyprctl dispatch <dispatcher> <arg>`, the classic form, for a host that is not on a Lua config |

A row whose record carries no dispatcher at all — Omarchy publishes five, the
`Universal copy`/`paste`/`cut` and zoom rows the shell implements elsewhere —
is the only kind that stays unusable. It has `enabled: false`,
`disabled_reason: "binding_adapter_unavailable"`, and
`disabled_reason_detail` repeating the host's own dispatcher and argument.

`requires_target` means the binding acts on the focused window. It is a hint a
client may show; it never greys the row out, because a shortcut is defined by
what the physical key does, which is "act on whatever has focus now". Nothing a
client holds identifies a window: focus is read at execution time, and a
binding invoked with nothing focused answers `status: "failed"` with code
`no_focused_window` rather than an HTTP error a client cannot tell apart from
`stale_catalog_revision`.

Two rows keep a native route so this client's own panel answers them:
`omarchy-menu toggle` (`SUPER + SPACE`) and `omarchy-menu-keybindings`
(`SUPER + K`). Every other row, including `omarchy-menu toggle system` and the
tmux and Herdr keybinding menus, runs on the host.

`POST /v1/actions/{action_ref}:invoke` answers with the usual action result plus
`observed`: `activeworkspace` and `activewindow` before and after (no window
titles), whether anything changed, and for an `exec` row the `{pid, exited,
exit_code}` of the process core started. A client shows success or failure from
that, rather than only "sent".

### Workspace rows during a Remote session

`Switch to workspace N`, `Next workspace` and `Previous workspace` are
`hl.dsp.focus({ workspace = ... })`, and Hyprland reads that as "go to whichever
monitor already shows that workspace". Pressing the key on the host is meant to
do exactly that, so with Remote closed nothing is rewritten.

With a Remote session open the user is looking at the session's own output, so
core replays those rows there instead: workspace N is moved to the owned output
and then focused, which is what Hyprland's own
`focusworkspaceoncurrentmonitor` does internally. The output is named from the
session rather than resolved as "current", so the result does not depend on
where focus happened to be. The receipt says so in `observed.redirected_output`.
`e+1`/`e-1` step through the workspaces on the owned output. `Move window to
workspace N` is never rewritten — carrying the focused window to the physical
screen is the point of it — and neither are `previous`, the special workspace
or the monitor-relative forms, none of which name a workspace to pull.

## SSH keys the host owns

SPEC-F3's companion terminal connects over SSH with a key pair the app
generates. `omodachi-host ssh` owns the lines that key is installed as, and
nothing else in `~/.ssh`:

```sh
omodachi-host ssh authorize "ssh-ed25519 AAAA... alex@ipad" --device omodachi-ipad
omodachi-host ssh list
omodachi-host ssh revoke --device omodachi-ipad
```

The written line is `<type> <body> # omodachi:<device>`. Everything after the
key body is an OpenSSH comment, so the marker is legal, greppable and
unambiguous about who wrote it, and `revoke` deletes exactly the lines carrying
one device's marker. `authorize` is idempotent on the key body; a key already
in the file that is not marked as ours is refused (`public_key_not_owned`)
rather than duplicated, because a second copy would let a revoke look like it
worked while the key still opens the door. Every other line - the user's own
keys, their `command=` options, their comments - is copied through byte for
byte, the file is replaced atomically at `0600` under a `0700` directory, and
a symlinked `authorized_keys` is refused rather than followed.

These are local administrator commands like `tls`: the Unix owner of the file
is the only authority involved, and they need neither a device credential nor a
running daemon. `--home` exists for tests.

`omodachi-host devices revoke <device_id>` calls the same revoke, so a device
loses its credential, its Sunshine certificate and its SSH line in one step;
the result carries `ssh: {revoked, removed, device}`, or an `error` code when
the file could not be rewritten. A failure there never hides the credential
revocation that did happen.

Nothing here reads or writes `sshd_config`, and no key without the marker is
ever touched.

## Wire fixtures

State, catalog and event fixtures plus their JSON Schemas live under
`contracts/`. They are generated from the packaged demo bootstrap, so they track
the serializers rather than a hand-written document. See
[contracts/README.md](../contracts/README.md).
