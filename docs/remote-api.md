# The Remote screen

Contract revision: `omodachi.v1`.

Remote turns an iPad into a screen of the Omarchy desktop. There are two
dimensions, and they are independent:

| | |
| --- | --- |
| **mode** `extend` | the iPad is an extra monitor beside the physical one; the local screen keeps working |
| **mode** `takeover` | the iPad takes over the desktop the user was already on; the local screen goes dark and everything comes back at the end |
| **backend** `sunshine` | the managed Sunshine fork — the main path, hardware encoded, adaptive |
| **backend** `vnc` | WayVNC 0.10.1 bridged onto the same authenticated WSS — the low-overhead alternative |

A host has **at most one session**. Everything about it lives in one
`RemoteSession` with a single monotonic `revision`; there are no lease epochs,
geometry epochs or connection generations. A request that names a stale
`expected_revision` is refused with 409 and the client re-reads the session.

## Operations

Every `/v1/*` request carries `Authorization: Bearer <device credential>`, and
the device that created a session is the only one that can read or change it.

| Method and path | Behavior |
| --- | --- |
| `GET /v1/remote/capabilities` | backends and why each is or is not available, `default_backend`, modes, placements, `lock_local_input_supported`, `bar_occlusion`, encoder limits |
| `POST /v1/remote/sessions` | create; `201` with the session, or `409 remote_session_exists` naming who holds it |
| `GET /v1/remote/sessions/{id}` | the whole session |
| `POST /v1/remote/sessions/{id}/resize` | new geometry for the same output; returns the session with a new `connection` and `revision` |
| `POST /v1/remote/sessions/{id}/backend` | change backend through the same path |
| `POST /v1/remote/sessions/{id}/heartbeat` | `{revision, state}` |
| `POST /v1/remote/sessions/{id}/presented` | optional telemetry; the response records whether the presented rectangle was a correct aspect fit |
| `DELETE /v1/remote/sessions/{id}` | idempotent release |
| `GET /v1/remote/sessions/{id}/audio` (WSS) | the microphone uplink, see [audio-uplink-contract.md](audio-uplink-contract.md) |
| `GET /v1/remote/sessions/{id}/vnc` (WSS) | the VNC byte bridge, see [wayvnc.md](wayvnc.md) |
| WSS event `remote.session.changed` | `{id, revision, state, reason?}` |

`POST /v1/remote/sessions` takes `{backend, mode, viewport_points, orientation,
logical_long_edge, quality, decoder, placement?, lock_local_input?,
ttl_seconds?, bar_occlusion_points?}`. The geometry fields are the planner's request; see
[desktop-profile.md](desktop-profile.md). `placement` (`right`, `left`, `above`,
`below`; default `right`) positions the owned output relative to the physical
layout. `lock_local_input` defaults to **false**.

`backend` is optional. Left out, the session gets `capabilities.default_backend`:
the host's `remote_backend` preference while that backend reports itself
available, otherwise the first one that does, in the host's order. The host's
answer is `sunshine`; a client that wants the other one names it, and one that
has no opinion should show `default_backend` rather than inventing a constant of
its own.

`GET /v1/state` carries `remote: {session_id, state, mode, backend, revision}`
and the `remote_bar` projection Panel anchors on.

### The device's corners (REMOTE-SAFE-1)

In Extend mode the owned output is the device's exact shape and the picture is
edge to edge, so the ends of the host's bar sit under the display's rounded
corners. When `GET /v1/remote/capabilities` says `bar_occlusion: true`, create
and resize also take

```json
"bar_occlusion_points": {"top": 46.4, "bottom": 46.4, "left": 46.4, "right": 46.4}
```

— how far, in the device's points, its corners reach along each edge into a bar
of the host bar's thickness (`top`/`bottom` are the two ends of a vertical bar,
`left`/`right` those of a horizontal one). Optional; all four keys, each
`0…256`. A resize without it keeps the session's value. It never changes the
plan. `remote/corners.py` converts it into the owned output's logical px
(aspect-fit, letterbox subtracted, rounded up, at most a quarter of the edge)
and `state.remote_bar.bar_insets` publishes that for the session, in either
mode — a takeover's output is the same device-shaped output, with the whole
desktop and the bar (on the edge `official_bar_position` picked) on it
(REMOTE-SAFE-1b). It is `null` for a client that sent nothing and with no
session. The Omodachi plugin's bar widget on that one output moves the bar's
end sections inward by it (`omodachi-plugin` `BarWidget.qml`), and reports
back through `omarchy-shell omodachi barGeometry` where that bar's Omarchy logo
now is (`bar_geometry.py`), so `state.bar.geometry.logo` follows it.

### Connection shapes

```json
{"backend":"sunshine","host":"192.168.1.11","https_port":47984,
 "app_name":"Omodachi Desktop","output_id":"OMODACHI-…",
 "stream_pixels":{"width":2560,"height":1440},"fps":60}
```

```json
{"backend":"vnc","transport":"wss","path":"/v1/remote/sessions/rs_…/vnc",
 "output_id":"OMODACHI-…",
 "initial_framebuffer_pixels":{"width":1280,"height":894},
 "framebuffer_pixels":{"width":2560,"height":1788}}
```

For Sunshine the client launches with its existing Moonlight pairing identity;
core has already bound the owned output to the fork's lease. For VNC the client
opens `path` as a WebSocket on the same host origin it is already talking to,
with the same Bearer credential and the same pinned certificate; core dials the
owned WayVNC listener on loopback and passes bytes through. The port is never
part of any response, and nothing is exposed on the LAN by either backend.

WayVNC shows its **first** client two framebuffer sizes, so core settles it
before a session gets a bridge and the document names both (see
[wayvnc.md](wayvnc.md)): `framebuffer_pixels` is the owned output's buffer
pixels, which is what WayVNC serves for the rest of the session, and
`initial_framebuffer_pixels` is what this client's own `ServerInit` will
announce — the same number when the prime worked, the compositor's logical size
when it did not, in which case one `NewFBSize` correction is coming.
For a vnc session `framebuffer_pixels == output_mode_pixels == stream_pixels`,
because WayVNC serves the framebuffer itself rather than encoding to a budget.
The two are equal only when the owned output happens to be planned at scale 1.

**Both backends are planned at the host's render density** (`render_density`,
default 2.0). The owned output's mode is the device's pixels and its scale is
the device's scale, so the desktop is laid out in the same logical units
whichever backend carries it and switching backends mid-session does not change
the desktop's geometry. For Sunshine, `stream_pixels` remains encoder pixels
inside the client's quality budget.

The client's `decoder` limits describe its **H.264** decoder. They bound
Sunshine's `stream_pixels`; they do not bound a vnc session's framebuffer,
because the RFB leg does not decode H.264 at all. A vnc framebuffer is bounded
by the host's `encoder_limits` and by the planner's own 320–4096 logical range.

## The host's quality preference

`quality` in the create/resize request is the **client's** ceiling. The host has
one of its own, the `quality` preference in
`~/.config/omodachi/preferences/state.json`, and the planner caps the client's
rates with it. It is a rate ceiling and nothing else — the pixel budget,
viewport, orientation and decoder limits stay the client's:

| Host `quality` | fps | bitrate_kbps |
| --- | --- | --- |
| `performance` | 30 | 8000 |
| `balanced` (default) | 60 | 12000 |
| `quality` | 60 | 20000 |

The planned `fps` and `bitrate_kbps` are therefore
`min(output refresh, host encoder, client decoder, client request, host preference)`.
The session keeps the client's request verbatim, so changing the preference and
resizing re-applies it.

`GET /v1/preferences` publishes the same numbers as
`profile_defaults.quality = {fps, bitrate_kbps}`, so a client can show what it
is going to get without reimplementing the mapping. A client must not recompute
the three tiers itself: the names are the host's, and only the host resolves
them. The values are `preferences.QUALITIES`.

### The device's own preset (STREAM-1)

A create or resize request may carry `quality_preset` and `adaptive`:

| `quality_preset` | fps / bitrate_kbps the session is planned at |
| --- | --- |
| absent or `host` | the table above, used as a ceiling on the client's `quality` (unchanged behaviour) |
| `performance` / `balanced` / `quality` | that row of the **host's** table, whatever rate the client sent. The host default is overridden |
| `custom` | the client's `quality.fps` / `quality.bitrate_kbps`, which must be one of `CUSTOM_FPS` (30, 60) and inside `CUSTOM_BITRATE_KBPS` (4000–40000); anything else is `400 invalid_request` before the host is touched |

`adaptive: true` records that the client's own automatic mode picked the preset
(it steps between the three named rows and says so); core does not act on it.
Both are reported back as `session.quality = {preset, adaptive}` and journaled.
A resize that does not name `quality_preset` keeps the session's — a rotation is
not a change of mind about quality. The encoder and decoder ceilings still
apply to every preset, and no preset changes the pixel budget.

Changing the preset mid-session is a resize: the backend is stopped, re-planned
and re-prepared on the same output, the session id does not change, the
revision goes up and the client re-dials with the new connection. It is **not**
a geometry change, so `allow_dynamic_resolution: false` does not refuse it.
GameStream has no in-band way to change a running stream's frame rate or
bitrate — the fork admits a launch only at exactly the prepared `fps` and at
most the prepared `bitrate_kbps` — so the re-dial is the price of a switch
(about one to two seconds of held last frame on the client).

`GET /v1/preferences` also publishes `profile_defaults.preset` (the host's own
row name), `profile_defaults.presets` (all three rows) and
`profile_defaults.custom` (`{fps, min_bitrate_kbps, max_bitrate_kbps}`), so a
client can draw the picker without a copy of either table.

### Codec

`profile.codec` is `hevc` or `h264`. The Sunshine leg negotiates it per plan:
HEVC when the client's `decoder.codecs` names `hevc` **and** the managed fork's
`desktop.status` lists it in `encoders`; otherwise H.264. The fork's list is
also published as `capabilities.backends.sunshine.codecs`. A fork that does not
send `encoders` is treated as H.264-only, because that fork refuses any other
codec at `desktop.prepare` and at the RTSP launch — its encoder probe finding
`hevc_vaapi` does not change that. The installed `encoder_limits.codecs` is
not consulted for this: it states the baseline every encoder here must meet
(H.264), not what the running one serves. The VNC leg encodes nothing and is
always planned as `h264`. A client must declare to Moonlight exactly the codec
in `profile.codec` — the fork compares it at launch.

## What creating a session does to the host

Both modes first record `omarchy-shell idle status` in the journal and, if the
idle cycle was enabled, turn it off: a screen being watched from an iPad looks
idle to the host, and Omarchy would otherwise blank and lock the machine
underneath the stream. A host that already had it off keeps it off, and a host
without `omarchy-shell` runs the session anyway.

**extend**

1. plan the profile from the viewport, orientation, desired logical long edge,
   quality budget and the client's decoder limits;
2. `hyprctl output create headless OMODACHI-<16 hex>`;
3. set its mode, scale and position with one guarded `hl.monitor` evaluation and
   read the result back. An explicitly positioned output makes Hyprland re-pack
   the remaining auto-positioned monitors, so any physical output that moved is
   pinned back to its snapshot: the physical layout is byte-identical before and
   after;
4. point the fork's touch and pen devices at the owned output (see below);
5. prepare the backend.

**takeover**

1. snapshot every physical output's full configuration and the whole
   workspace→monitor map into the journal;
2. plan and create the headless output as above;
3. move every workspace onto it one at a time, reading each move back, and
   follow the focus to the workspace the user was on;
4. disable the physical outputs from their snapshot — the local screen goes dark;
5. optionally (`lock_local_input`, default false) disable each physical input
   device, skipping Sunshine's and any other emulated input;
6. put the official Omarchy bar on the long edge for the device orientation
   (landscape `top`, portrait `left`, changeable in
   `~/.config/omarchy/omodachi-remote-bar.json`), recording the original;
7. record `layout:single_window_aspect_ratio` and, if it is not already off,
   set it to `0 0` (see below);
8. record `misc:mouse_move_enables_dpms` and `misc:key_press_enables_dpms` and
   turn off whichever were on, so the forwarded input does not light the
   screens step 4 just darkened (see below);
9. point the fork's touch and pen devices at the owned output (see below);
10. prepare the backend.

### Touch and pen belong to the output; the pointer belongs to the desktop

The fork normalizes **touch and pen** coordinates to the output it captures and
hands the backend 0..1, so the compositor is what places that box. Without a
per-device binding Hyprland falls back to `input:touchdevice:output`, which is
`[[Auto]]` on this host and cannot know which of several screens the stream is.
So a Sunshine session — extend and takeover alike — sets

```
hl.device({ name = "touch-passthrough", output = "OMODACHI-<16 hex>" })
hl.device({ name = "pen-passthrough",   output = "OMODACHI-<16 hex>" })
```

before the backend is prepared, and `restore()` writes `output = ""` — Hyprland's
own default — on every exit path. Hyprland keeps device configuration by name, so
the mapping is accepted before the fork has created the devices for a connecting
client and survives a `hyprctl reload`.

The **absolute pointer** is deliberately not bound (INPUT-1). Its coordinates are
relative to the whole virtual desktop, and Hyprland resolves an absolute pointer
against the bounding box of its enabled monitors — which is exactly what the fork
targets. Measured on Hyprland 0.56.2:
`hyprctl getoption "device:mouse-passthrough-(absolute):output"` answers `no such
option`, setting it through `hl.device` changes nothing, and feeding the fork's
own absolute values through a copy of its uinput device puts the cursor within
one logical pixel of the target on the owned output, in both modes. Binding it
would be wrong even if it worked: desktop-wide coordinates squeezed into one
output. The relative pointer and the keyboard follow focus and are never mapped
either, and no physical input device is ever remapped.

A VNC session maps nothing — WayVNC drives the compositor's own virtual pointer
and there is no uinput device to place. A session that switches from VNC to
Sunshine maps them at the switch.

### `layout:single_window_aspect_ratio`, for the length of a takeover

Omarchy's `trigger.toggle.one-window-ratio` sets this Hyprland **global** to
`1 1`, which coerces a lone window into a square. On the owned output that
leaves the remote client looking at a square with a margin on either side.
Hyprland has no per-monitor, per-workspace or window-rule form of this option
(PERF-2 §8.2), so the only honest scope is the session:

- the current value goes into the journal before anything is written, and
  `restore()` puts it back on every exit path;
- a host that never turned it on is left completely alone, `set` flag included;
- `hyprctl reload` re-sources the user's own toggle directory and turns it back
  on, so the `configreloaded` reconcile re-asserts `0 0` while the session runs;
- a value somebody sets during the session is theirs: the restore sees a number
  that is not the one it wrote and leaves it;
- **extend** never touches it. The user is looking at the same screens.

The user's toggle file under `~/.local/state/omarchy/toggles/` is never read or
written; only the live compositor value is.

### `misc:*_enables_dpms`, for the length of a takeover

Hyprland wakes **every** DPMS-off monitor on any input event while
`misc:mouse_move_enables_dpms` or `misc:key_press_enables_dpms` is on, and
Omarchy sets both to true in `/usr/share/omarchy/default/hypr/input.lua`. A
takeover feeds the remote client's pointer, touchpad and keyboard to that same
compositor as real input, so with them on the screen in the room lit up on
every single interaction and went dark again on the next reconcile — one flash
per tap. They are Hyprland globals with no per-monitor or per-device form, so
the scope is the session, on the same terms as the aspect ratio:

- both values go into the journal, with their `set` flags, before either is
  written, and `restore()` puts back exactly the ones it turned off;
- an option the host already had off is not this session's and is never turned
  on;
- `hyprctl reload` re-sources Omarchy's own `input.lua` and turns both back on,
  so the `configreloaded` reconcile re-asserts them — before it re-darkens the
  screens in the same pass, or the next forwarded event would undo the repair;
- a compositor that will not answer `getoption` costs the session nothing: the
  step is journaled `unavailable` and nothing is written or restored;
- **extend** never touches them. The user is looking at the same screens.

Read with `hyprctl -j getoption`, written with `hl.config({ misc = { … } })`
and read back — `hyprctl keyword` cannot reach this parser, and `hyprctl
descriptions` reports the compiled-in default (`false`) as `current` rather
than the live value.

## When the host changes its displays mid-session

Changing the display configuration during a session is a legitimate user
action, not an error. In takeover mode the official Omarchy Display panel is
the UI the remote user is looking at, and its SCALE buttons run
`omarchy-hyprland-monitor-scaling`, which acts on **whichever monitor is
focused** — during a session, the session's own headless output. The host's
`omarchy-hyprland-monitor-watch` independently issues `hyprctl reload` on
`monitoradded` / `monitorremoved`, and a reload re-applies the catch-all
`hl.monitor({output = "", mode = "preferred", position = "auto", scale = …})`
rule in `~/.config/hypr/monitors.lua` to *every* output, the session's
included.

So the daemon subscribes to the compositor's own event stream,
`$XDG_RUNTIME_DIR/hypr/<instance>/.socket2.sock`, and reconciles on
`monitoradded`, `monitorremoved`, `configreloaded` and `monitorlayoutchanged`.
A reconcile also runs every two seconds, so a host whose event socket cannot be
reached still converges. Reconciling never fights the user:

| What the host did | What the session does |
| --- | --- |
| changed a **physical** output | records the new geometry as the snapshot, so the restore puts back what the user last chose. Nothing about the session changes; the backend is not touched |
| moved, re-moded or re-scaled the **owned** output | re-asserts the planned mode, scale and position, and stops nothing. The session's output geometry was planned from the client's viewport and stays the session's for its whole life; a compositor default baked into it would put permanent letterboxing inside the encoded stream. Reported as `owned_output_repinned` |
| **removed** the owned output | re-creates it, re-applies the profile and prepares the backend again. The only reconcile that touches the backend, and the session id does not change |
| is still reloading, so the write did not land | says `host_reconfigure_retry` and tries again on the next pass. Only `RECONFIGURE_ATTEMPTS` (8) passes in a row that all fail end the session |
| re-sourced its config (`configreloaded`) and turned `layout:single_window_aspect_ratio` back on during a takeover | re-asserts `0 0`; reported as `single_window_aspect_reapplied` |
| re-sourced its config and turned `misc:mouse_move_enables_dpms` / `misc:key_press_enables_dpms` back on during a takeover | turns off again whichever ones this session owns, first in the pass; reported as `dpms_wake_reapplied` when nothing louder happened |

A pass that had to put something right writes one line on the daemon's
journal, `{"omodachi":"remote","event":"reconciled","reason":…}`. A quiet pass
says nothing, so the record of what a session kept having to repair is
readable afterwards.

Only the removed-output case publishes anything: `remote.session.changed` with
`reason: "host_reconfigured"` and a new `revision`, on the **same session id**.
The client re-reads that session and dials it again; it does not start a new
one and it does not leave the picture. A host-initiated change is never refused
by `allow_dynamic_resolution` — that preference governs what a *client* may ask
for.

The session's output scale is deliberately **not** adopted from the host
mid-session (REMOTE-4; this replaces the earlier behaviour, which re-planned the
profile and restarted the backend whenever the owned output's scale moved).
Changing the display scale while a session is up is something a user does by
mis-clicking the official Display panel — which, in a takeover, acts on
whichever monitor is focused, and that is ours. The only promise that has to
hold is that it costs them nothing: the picture does not blink, the session
does not end, and the scale they picked is on the screen in the room the moment
the session is over, because the *physical* snapshot does follow the user.

Only the session's own output is written. Nothing under `~/.config/hypr/`,
`~/.local/state/omarchy/` or any Omarchy toggle is read back or edited.

## The desktop shell, and how a takeover darkens a screen

Taking an output away kills Quickshell 0.3.1. Qt 6.11's
`QWaylandWindow::setGeometry` moves a toplevel to
`screen()->geometry().topLeft()` without checking that `screen()` is still
there (`qwaylandwindow.cpp:491`), so an `xdg_toplevel.configure` delivered
after the output is gone is a segfault. It is not a race this side can win:
with a five-second pause and no workspace moves at all, disabling the physical
output still crashed the shell 22 times out of 22. See
`docs/issues/2026-09-21-quickshell-crash-on-takeover.md`.

So a takeover **turns the physical screens off rather than removing them**:
`hl.dsp.dpms` with the monitor object, read back on `dpmsStatus`. The screen
in the room is dark and the output stays in the layout, which is what the
compositor and the shell need. Each screen goes into the journal as
`takeover.blanked: [{name, method}]` before it is touched, and the restore
turns exactly those back on. A compositor whose DPMS dispatcher will not answer
falls back to disabling the output — the old behaviour, and the reason the
crash fallback below still exists. A screen the user had already turned off or
disabled is not this session's and is neither darkened nor restored.

Forwarded input is stopped from waking those screens at all — see
`misc:*_enables_dpms` above. A screen that comes back on anyway while the
session is live is turned off again by `reconcile()`, reported as
`physical_blanking_reapplied`. That is the same
undertaking as re-asserting the owned output's mode: the session owns the
physical screens for its lifetime and gives them back as it found them.

The same pass re-pins the workspaces, reported as `workspaces_repinned`.
`hyprctl reload` — which Omarchy's monitor watcher issues whenever an output
appears, so at least once per session — re-applies the user's `workspace_rule`
bindings, and a host that pins its workspaces to the laptop panel would pull
them back onto the screen that is now merely dark rather than gone. Only the
workspaces this session moved are moved again, and only off a screen this
session darkened.

Destroying the session's *own* headless output at release does not crash
anything (0 crashes in 30 observed removals): it is removing the physical
screen the shell started on that is fatal.

If the shell dies anyway, its own crash handler reloads the configuration
inside the crashed process: it answers `omarchy-shell shell ping` again while
every IpcHandler is registered twice, the menu's Apps list is empty and the
polkit agent is unreachable. The session notices the new entry under
`~/.cache/quickshell/crashes`, runs `omarchy-restart-shell` **once**, and
publishes `remote.session.changed` with `reason: "shell_restarted"`. The
`revision` does not move — nothing about the session changed — so no client
has to re-read or reconnect; the event is there to explain why the host
blinked. The daemon also writes one journal line:
`{"omodachi":"remote","event":"shell_restarted",…}`.

## Recovery

Every mutation is journaled to `~/.local/state/omodachi/remote/OMODACHI-*.json`
**before** it is issued. All four exits run the same `restore()`:

- a normal `DELETE`;
- a missed heartbeat — no beat within `ttl_seconds` (default 30, the CLI uses 60);
- daemon start, which finishes every journal it finds;
- `omodachi-host remote recover`.

`restore()` walks back in order — stop and release the backend, put **every**
physical output back to its snapshot, move each workspace home, re-enable the
input devices, unbind the touch devices from the output, put the
`misc:*_enables_dpms` options and `layout:single_window_aspect_ratio` back,
re-enable the idle cycle if it was on
before, restore the bar and destroy the headless output — and **every step runs even if an earlier one
failed**, so a stuck backend can never leave the
screen dark. Failures are collected and reported; the journal is deleted only
when there are none, so the next `recover` can finish the job.

Every physical output is restored, not only the ones a takeover turned off: an
extend session has no disabled list, and the host still moves the screens
underneath it — `hyprctl reload` re-applies the user's catch-all monitor rule,
and an explicitly positioned output makes Hyprland re-pack the auto-positioned
ones. An output that is no longer attached is skipped, and one that already
matches its snapshot is not written at all.

The bar is only restored to the value core itself wrote. If the user moved the
bar during the session, that is reported as an override and left alone.

`recover` finishes only the journals in **this daemon's own** journal directory.
An `OMODACHI-*` output that none of them accounts for is **reported and left
alone** (`unowned_outputs`, and a journal line `unowned_output_left`): another
RemoteManager on the same compositor - a side-by-side test daemon, or the
installed daemon seen from one - may own it, and before CORE-2 a second daemon
starting up destroyed the first one's live session this way (HOST-1 §7.3).
`omodachi-host remote recover --orphans` is the explicit operator step that
removes them, after moving their occupied workspaces to a physical output, and
reports them as `orphan_outputs`. Daemon start never passes it.

## Resize, and how a client should drive it

Resize is server-driven and is one call. The client stops its own media and
input first; the host does the rest and answers with the new connection.

```
iPad                                   host
 |                                      |
 | stop decoding, release input         |
 |                                      |
 |--- POST /resize {expected_revision,  |
 |      viewport_points, orientation}   |
 |                                      |--- backend.stop() to the real fence
 |                                      |--- re-plan the profile
 |                                      |--- hl.monitor: new mode/scale, same
 |                                      |    output, so windows stay put
 |                                      |--- read the compositor back
 |                                      |--- backend.prepare() -> connection
 |<-- 200 {session: {revision, connection, profile, ...}}
 |                                      |
 | reconnect with the new connection    |
 | keep the old image until first frame |
 |                                      |
 |--- POST /presented (optional) ------>| validate_presented_geometry, recorded
```

The output is reconfigured, never destroyed, so the windows and the workspace on
it survive a rotation. A fast reverse rotation is safe: the second request still
carries the old `expected_revision` and is refused with `409 stale_revision`;
the client re-reads the session and sends again. `presented` is telemetry and
blocks nothing.

If the host preference `allow_dynamic_resolution` is false, a resize that would
change output geometry is refused with `403 dynamic_resolution_policy_denied`;
an encoder-only change (pixels, fps, bitrate) is still allowed.

## Error codes

| Code | Status | Meaning |
| --- | --- | --- |
| `remote_session_exists` | 409 | another session owns the host. `error.detail` carries `session_id`, `owner_device_id`, `owner_device_name`, `mode`, `backend` and `started_at`, so the screen can say *who* rather than "somebody" |
| `stale_revision` | 409 | `expected_revision` is not the current one |
| `session_not_found` | 404 | no session with that ID |
| `session_not_ready` | 409 | the session is mid-transition |
| `permission_denied` | 403 | another device owns the session |
| `media_pairing_required` | 409 | the Sunshine backend has no paired certificate for this device |
| `wayvnc_0_10_1_required` | 503 | the VNC backend is not installed |
| `sunshine_desktop_unavailable` | 503 | the fork is not running the managed desktop control |
| `sunshine_assets_missing` | - | not an error: a capabilities `reason` reported next to `available: true`. The managed fork's `assets/shaders/opengl` is not beside the binary its unit starts, so it is encoding in software. See [sunshine-ipc.md](sunshine-ipc.md) |
| `remote_runtime_unavailable` | 503 | there is no graphical session, so there is no manager |
| `dynamic_resolution_policy_denied` | 403 | the host preference forbids this geometry change |
| `host_waking` | 409 | the host screen was asleep or the screensaver was active; `POST /v1/control/wake` first, then retry |
| `vnc_bridge_unavailable` | 409 / 503 | this session is not a ready VNC session (409), or its WayVNC listener is not accepting connections (503) |
| `vnc_bridge_exists` | 409 | one VNC bridge is already open on this session |

A 401 `permission_denied` for a refused device credential carries
`error.reason`: `credential_expired`, `credential_revoked`, `device_purged` or
`unknown_credential` (CORE-2; see [pairing.md](pairing.md)). The credential
routes add two codes of their own:

| Code | Status | Meaning |
| --- | --- | --- |
| `credential_renewal_not_due` | 409 | `POST /v1/pairing/renew` before the credential's last week. `error.detail` carries `expires_at` and `renewable_at` |
| `plugin_credential` | 409 | the host panel's own credential: it is not renewed and not revoked without `--force` |

Errors carry `{"error":{"code","message"}}` and, for the codes above that say
so, a bounded `error.detail` object of strings, integers and booleans.

### The clipboard's codes (CLIP-1)

`GET` / `PUT /v1/clipboard` are not Remote session routes, but this is where
this host enumerates its HTTP error vocabulary, so they are listed here too.
The routes themselves are documented in [clipboard.md](clipboard.md).

| Code | Status | Meaning |
| --- | --- | --- |
| `clipboard_sync_disabled` | 403 | the host preference `clipboard_sync` is `off`. It is a refusal and not an empty clipboard, so a screen can tell the two apart |
| `clipboard_write_disabled` | 403 | `clipboard_sync` is `host_to_device`: this host is shared one way only |
| `clipboard_too_large` | 413 | more than 64 KiB of UTF-8, in either direction. Refused rather than truncated |
| `clipboard_not_text` | 409 / 415 | the host clipboard holds something that is not text (409), or the body sent is not UTF-8 `text/plain` (415) |
| `clipboard_invalid` | 400 | an empty write, or a body that is not text at all |
| `clipboard_unavailable` | 503 | there is no clipboard bridge: no graphical session, or `wl-copy`/`wl-paste` did not answer |

## Command line

```sh
omodachi-host remote status
omodachi-host remote start --mode takeover --backend vnc --viewport 1194x834 \
                           --orientation landscape_left --ttl 60
omodachi-host remote resize --viewport 834x1194 --orientation portrait
omodachi-host remote resize --quality-preset balanced            # STREAM-1
omodachi-host remote resize --quality-preset custom --fps 30 --bitrate-kbps 30000
omodachi-host remote stop
omodachi-host remote recover
omodachi-host remote recover --orphans   # also remove OMODACHI-* outputs no journal here owns
```

`start` defaults to a 60-second TTL so a forgotten command-line session restores
itself. The other commands resolve the session ID themselves, because there is
only one.
