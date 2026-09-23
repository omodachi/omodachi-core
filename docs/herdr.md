# The Herdr bridge

Contract revision: `omodachi.v1`. Schemas:
[`contracts/herdr-layout.schema.json`](../contracts/herdr-layout.schema.json),
[`contracts/herdr-sessions.schema.json`](../contracts/herdr-sessions.schema.json).
Examples: [`contracts/fixtures/herdr-layout.json`](../contracts/fixtures/herdr-layout.json),
[`contracts/fixtures/herdr-sessions.json`](../contracts/fixtures/herdr-sessions.json).

Herdr 0.8.2 has no first-party phone client and says so; what it does have is
`herdr terminal session observe|control`, a documented bridge for third-party
front ends. Omodachi draws its own touch grid from the session snapshot and puts
those two streams on the WebSocket the device already authenticated. Going with
the official design, not around it.

`omodachi`, run by `omodachi-herdr.service`, is the session core owns and the
one every device starts on. HERDR-2 adds the rest: a device can select any
session this host is running, because the user's own work lives in *their*
session and a panel that can only see core's own shell is a panel with nothing
in it. The Herdr socket still has no authentication of its own — whoever can
reach it owns the session — so it is never exposed to the LAN: the authenticated
WSS and the same-user Unix socket are the only ways in.

## Sessions

`herdr session list --json` is the whole enumeration herdr 0.8.2 publishes.
Verbatim, on this host:

```
$ herdr session list --json
{"sessions":[{"default":true,"name":"default","running":true,
  "session_dir":"/home/alex/.config/herdr",
  "socket_path":"/home/alex/.config/herdr/herdr.sock"},
 {"default":false,"name":"omodachi","running":true,
  "session_dir":"/home/alex/.config/herdr/sessions/omodachi",
  "socket_path":"/home/alex/.config/herdr/sessions/omodachi/herdr.sock"}]}
```

There is **no runtime directory**: `ls $XDG_RUNTIME_DIR | grep -i herdr` is
empty, and every socket lives under `~/.config/herdr`. The default session's
socket is at the root of that directory rather than under `sessions/<name>/`, so
a socket path is read from this listing rather than constructed — and a path the
listing reports outside `~/.config/herdr` is refused rather than connected to.

| Method and path | Behavior |
| --- | --- |
| `GET /v1/herdr/sessions` | every session, its shape, and the one this device is on |
| `POST /v1/herdr/sessions/{name}/select` | move this device to that session |

```json
{"contract_revision": "omodachi.v1", "selected": "omodachi", "owned": "omodachi",
 "sessions": [{"name": "omodachi", "running": true, "owned": true, "herdr_default": false,
               "readable": true, "workspaces": 1, "tabs": 1, "panes": 2, "agents": 1,
               "protocol": 20, "version": "0.8.2"},
              {"name": "default", "running": true, "owned": false, "herdr_default": true,
               "readable": true, "workspaces": 4, "tabs": 4, "panes": 4, "agents": 0,
               "protocol": 20, "version": "0.8.2"}]}
```

The counts come from each session's own `herdr --session <name> api snapshot`.
A session that is running but stopped answering is listed with `readable:
false` and null counts rather than dropped: a name the user knows is on the
machine must not silently vanish from the list. `owned` is core's own session,
`herdr_default` is the one plain `herdr` attaches to.

**A selection is remembered per device.** Two iPads are looking at two different
things, so the choice is keyed by the device the credential belongs to;
`omodachi-host herdr select <name>` with no `--device` moves the host-wide
fallback under every device that has not chosen for itself, and the owned
session is the fallback under that. A name is accepted only because
`herdr session list` just returned it as running, so `invalid_session` covers
both "there is no such session" and "that one is stopped", and a client can
never invent a name that becomes an argv element or a socket path.

After a selection, **`layout`, `observe`, `control` and every action route mean
that session** for that device. The one-controller rule and the layout revision
are per session, so two devices in two sessions do not collide.

## Layout

`GET /v1/herdr/layout` projects one `herdr --session <selected> api snapshot`
into workspaces → tabs → panes:

```json
{
  "contract_revision": "omodachi.v1",
  "session": "omodachi", "protocol": 20, "version": "0.8.2",
  "focused": {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p2"},
  "workspaces": [{"id": "w1", "label": "~", "number": 1, "focused": true,
    "active_tab_id": "w1:t1", "agent_status": "unknown",
    "tabs": [{"id": "w1:t1", "label": "1", "number": 1, "focused": true, "zoomed": false,
      "agent_status": "unknown",
      "panes": [{"id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1",
                 "title": "alex@omarchy:~", "cwd": "/home/alex",
                 "focused": false, "zoomed": false, "agent_status": "unknown",
                 "agent": null, "size": {"cols": 47, "rows": 20}, "revision": 1}]}]}],
  "revision": 2
}
```

`size` is the pane's rectangle in the tab layout, so a client can lay out the
grid in the same proportions Herdr uses. Zoom belongs to the **tab**; a pane
reads as `zoomed` only when its tab is zoomed and that pane is the tab's focused
one. `agent` is filled from the snapshot's agent list when one lives in the
pane, and `agent_status` is Herdr's own `idle|working|blocked|done|unknown` —
where `unknown` does not mean finished, and where Herdr's authority over
codex/claude panes is a screen heuristic. Approvals and token usage belong to
the codex app-server, not here.

`revision` is monotonic **per session** and only moves when that session's
projection moves. The WSS event `herdr.layout.changed {revision, session}` is
published from a **two-second poll** of the owned session plus every other
session some device has selected — `session` is what tells a device whether the
move was its own, because the revision alone cannot: `events.subscribe` streams only on the connection that asked for
it, and whether 0.8.2 replays history on subscribe is unverified, so the
snapshot is the reading that cannot be wrong.

## Streams

| Method and path | Behavior |
| --- | --- |
| `GET /v1/herdr/panes/{pane}/observe?cols=N&rows=N` (WSS) | read-only; many viewers |
| `GET /v1/herdr/panes/{pane}/control?cols=N&rows=N[&takeover=true]` (WSS) | read-write; one at a time |

Both wrap the official command. **Every NDJSON line Herdr writes becomes one
WebSocket TEXT message, byte for byte** — `terminal.frame`, `terminal.closed`
and anything a later Herdr adds travel unchanged, because this is a pipe, not a
second protocol:

```json
{"type":"terminal.frame","seq":1,"encoding":"ansi","full":true,
 "width":80,"height":10,"bytes":"<base64 ANSI>"}
```

`control` forwards the client's commands to Herdr's stdin. Only Herdr's own four
are accepted, and their fields are Herdr's, forwarded as written:
`terminal.input` (`text` or `bytes`, not both), `terminal.resize` (`cols`,
`rows`), `terminal.scroll` (`lines`), `terminal.release`. Anything else closes
the socket with `invalid_control_command`; the check exists so this bridge can
never be used to speak a different protocol at the session socket.

`observe` on 0.8.2 **reads no stdin at all**, so a viewer that wants a different
size sends `{"type":"resize","cols":N,"rows":N}` and the bridge restarts the
stream at the new size; the next frame is a full one. `control` takes
`terminal.resize` in band and the same process keeps running.

Herdr allows one controller per pane, and so does the bridge: a second `control`
of the same pane is refused with `409 herdr_control_in_use` **before** the
WebSocket upgrade, so a client learns about it as an HTTP status. Observers are
unlimited. The credential is rechecked every second on both streams, and the
subprocess is killed when either end goes away.

## Actions

| Method and path | Body |
| --- | --- |
| `POST /v1/herdr/panes/{pane}/split` | `{"direction": "right"\|"down", "ratio"?: 0.05–0.95}` |
| `POST /v1/herdr/panes/{pane}/zoom` | `{"mode": "on"\|"off"\|"toggle"}` |
| `POST /v1/herdr/panes/{pane}/focus` | `{}` or `{"direction": "left"\|"right"\|"up"\|"down"}` |
| `POST /v1/herdr/panes/{pane}/close` | `{}` |
| `POST /v1/herdr/workspaces/{id}/select` | `{}` |
| `POST /v1/herdr/workspaces/{id}/tabs` | `{"label"?: string, "focus"?: bool}` |
| `DELETE /v1/herdr/workspaces/{id}/tabs/{tab}` | none |

Each is a fixed argv of the official CLI — `herdr --session <selected> pane
split --pane w1:p1 --direction down`, and so on. Pane ids must match `wN:pN`,
workspace ids `wN` and a session name `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`, so a
client value can never become a flag. The reply carries Herdr's own result.

`focus` is the exception worth knowing about: **herdr 0.8.2's CLI only focuses a
neighbour** (`pane focus --direction`), and a touch client tapping a pane needs
absolute focus. With no `direction` the bridge therefore calls the protocol's
own `pane.focus {pane_id}` on the session socket — same session, same official
API, one allowlisted method name, newline-delimited JSON. If a later Herdr
exposes it on the CLI, that call should move back to argv.

Tabs are `herdr tab create --workspace wN [--label L] [--focus|--no-focus]` and
`herdr tab close wN:tM`. There is deliberately **no `--cwd`**: a client-chosen
working directory is a path this bridge would hand to a spawned shell, and the
owned session already starts where core put it. A label is passed as one argv
element with control characters refused, and a tab id must both match `wN:tM`
and belong to the workspace in the path, so `DELETE …/workspaces/w1/tabs/w2:t1`
is a `400 invalid_tab` rather than a cross-workspace close.

`control` also accepts `?takeover=1`, which adds Herdr's own `--takeover` to
`terminal session control` and takes the pane away from whoever holds it. The
bridge's one-controller rule still applies to its own sockets.

The earlier `GET`/`POST /v1/herdr/sessions/{s}/panes/{p}[/zoom]` surface is
gone for good: a session is chosen once, per device, through
`/v1/herdr/sessions/{name}/select`, and after that there is nothing for a pane
request to name but the pane.

## The host side

```
omodachi-host herdr                      the agent/Herdr snapshot, as before
omodachi-host herdr sessions [--device D]  every session, and where D is looking
omodachi-host herdr select <name> [--device D]
```

Errors: `503 herdr_unavailable` when the selected session does not answer,
`400 invalid_session` for a name that is not a running session on this host,
`400` for an invalid pane, geometry, payload or control command, `409
herdr_control_in_use`, `409 herdr_request_failed` when Herdr itself refused.
`--demo` has no owned session and reports `herdr_unavailable`.
