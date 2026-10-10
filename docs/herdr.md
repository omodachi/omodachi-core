# The Herdr bridge

Contract revision: `omodachi.v1`. Schemas:
[`contracts/herdr-layout.schema.json`](../contracts/herdr-layout.schema.json),
[`contracts/herdr-sessions.schema.json`](../contracts/herdr-sessions.schema.json),
[`contracts/herdr-keys.schema.json`](../contracts/herdr-keys.schema.json),
[`contracts/herdr-text.schema.json`](../contracts/herdr-text.schema.json).
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
| `POST /v1/herdr/sessions/{name}/start` | HERDR-5: start a stopped session with no window, then select it |
| `POST /v1/herdr/sessions {name}` | HERDR-5: make a new session in `$HOME`, started the same way, then select it |
| `POST /v1/herdr/sessions/{name}/workspaces {cwd, name?}` | HERDR-6: a new workspace in that running session, in a folder the device picked, without moving the computer; then select the session |
| `GET /v1/herdr/folders[?under=&hidden=1]` | HERDR-6: the folders a new workspace is offered in: recent and common, or one folder's folders |

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

### Starting a stopped session, making a new one (HERDR-5)

A stopped session used to be listed and greyed out. `start` brings it up the
way core already runs its own: **herdr 0.8.2's headless server,
`herdr --session <name> server`**, the exact command `omodachi-herdr.service`
runs for `omodachi`. It runs as a transient **user unit**:

```
systemd-run --user --unit=omodachi-herdr-session-<name> --collect \
  --property=Type=exec --working-directory=$HOME \
  --description="Herdr session <name> (started from Omodachi)" \
  herdr --session <name> server
```

Why not a short-lived client in a PTY that detaches: a server a client spawns
is `herdr server` with `HERDR_SESSION=<name>` in **that client's cgroup** (on
the host, `new`'s server sits in the `foot` terminal's scope). One spawned from
core would sit in `omodachid.service`'s cgroup and die with it on every core
restart or reinstall. A unit of its own needs no client, outlives core, and
ends when the session is stopped (`herdr session stop <name>`); `--collect`
lets systemd forget it then. `omodachi` itself is started with
`systemctl --user start omodachi-herdr.service`, as the Agent path always has.
Measured on herdr 0.8.2: a second `server` for a running session exits with
`error: herdr server is already running`; `--session default` is the default
session (the root of `~/.config/herdr`, not `sessions/default`).

Herdr restores a stopped session's workspaces from its own `session.json` when
its server starts; a session with none (a new one) gets one workspace in
`$HOME` (`herdr --session <name> workspace create --cwd $HOME --no-focus`),
which is what a desktop client makes on first attach. Core waits (up to ten
seconds) until `herdr session list` says the session runs and its
`api snapshot` answers, then **selects it for this device**, so one call lands
the phone in it:

```json
{"contract_revision": "omodachi.v1", "selected": "default", "owned": "omodachi",
 "scope": "device", "started": true, "created": false}
```

`started: false` when it was already running (a second phone pressing Start:
one start at a time, the second finds it up). `create` takes `{"name": …}`
only: `[A-Za-z0-9][A-Za-z0-9._-]{0,31}` — Herdr's alphabet with a letter or
digit first, so `.`/`..` (`sessions/..` is the default session's directory)
and `-x` (a flag) can never be one — and refuses a name the host already has,
running or stopped, with `409 herdr_session_exists`: create never restarts or
reuses a session. Neither route stops or deletes anything; that stays on the
computer (`herdr session stop|delete`). Both are native-only (not in
`browser_gate.WEB_ALLOWED`) and write one line each to `herdr-writes.jsonl`
(`kind: session_start|session_create`, `pane: "-"`, the outcome; no content).
Schema: [`contracts/herdr-session-start.schema.json`](../contracts/herdr-session-start.schema.json).

### A new workspace in a chosen folder (HERDR-6)

`POST /v1/herdr/sessions/{name}/workspaces {"cwd": "~/Work/omodachi", "name": "omodachi"}`
runs, in a session the host lists as running:

```
herdr --session <name> workspace create --cwd <folder> [--label <name>] --no-focus
```

**The folder is checked by core first** (`host_paths.folder()`), because Herdr
does not check it: probed on herdr 0.8.2, `--cwd` with a missing folder, a file
or a relative path is accepted without a word and the shell opens in `$HOME`.
`cwd` is read the way a person types it (`~`, `~/x`, an absolute path, or one
relative to the home folder), resolved with `realpath` (links followed), and
must then be the home folder or inside it (`folder_outside_home` - a link out of
`$HOME` is outside it), not a private place (`folder_private`: `~/.ssh`,
`~/.gnupg`, `~/.password-store`, cloud and container credentials, `~/.codex`,
`~/.claude`, browser profiles, `~/.config/herdr`, Omodachi's own state - ATTACH-2's
design denylist, directories only, with its `uploads/` and `agent-workspace/`
exceptions), and an existing folder (`404 folder_not_found`). Private is decided
before existence, so a refusal never says whether something private is there.
`name` is optional, a label like a tab's (1-64 characters, no control
characters; `invalid_workspace_name`); without one Herdr names the workspace
after its folder (`~` for the home folder), as on the desktop.

**`--no-focus` keeps the computer where it is.** Measured on the host: the
session's `focused_workspace_id` (what a desktop client shows) stays put, and
Hyprland's active window does not change. Herdr's one exception is its own: in
a session with **no** workspace at all, the first one is focused whatever is
asked, since there was nothing else to look at. The session becomes this
device's selection, and the reply names the new workspace and its first pane,
which the client then shows in its **own** view (it never calls `workspace
focus`):

```json
{"contract_revision": "omodachi.v1", "selected": "work", "owned": "omodachi", "scope": "device",
 "workspace": {"id": "w3", "label": "omodachi", "number": 3, "focused": false, "pane": "w3:p1",
               "cwd": "/home/user/Work/omodachi", "display": "~/Work/omodachi"}}
```

`workspace.cwd` is where Herdr opened the shell (the root pane's own `cwd`). It
is audited like the other writes: one `herdr-writes.jsonl` line, `kind:
workspace_create`, `pane` = the new workspace id, the outcome; the folder is
not written there. Native and web credentials (Odomo's card is the App's).
Schema: [`contracts/herdr-workspace-create.schema.json`](../contracts/herdr-workspace-create.schema.json).

`GET /v1/herdr/folders` is what the card offers:

- `recent`: the folders the running sessions' panes are in, the device's own
  session first, **newest first** - herdr 0.8.2's snapshot carries no times, so
  a folder counts from when core first saw a pane in it (the two-second layout
  poll and this read both look; a folder a workspace was just made in is the
  newest); ties keep Herdr's own order, newest pane first. At most eight, never
  the home folder itself, only folders `host_paths` allows.
  POLISH-U: merged with core's own list of the folders new workspaces were
  made in, so one stays offered after its pane closed, its session stopped or
  core restarted. The list is `~/.config/omodachi/herdr-folders.json` (beside
  `herdr-sessions.json`; 0600, rewritten atomically, `{"version": 1,
  "folders": [{"path", "at"}]}`): this host's, at most 16, newest first, never
  the home folder. A remembered folder is offered only while `host_paths` still
  allows it (it exists, is a folder inside `$HOME`, is not private) and is
  dropped from the file the next time it is written; the order is the later of
  "used" and "first seen in a pane". `--remove --purge` deletes it (and
  `herdr-writes.jsonl` with its `.1`), in core's and the plugin's rule alike.
- `common`: those of `~`, `~/Work`, `~/Projects`, `~/code`, `~/src`, `~/dev`
  that exist.
- `?under=<folder>` instead answers `browse`: that folder (checked the same
  way), its parent while still inside `$HOME`, and the folders directly in it -
  one level, sorted by name, names starting with `.` only with `&hidden=1`,
  never a private one or a link out of `$HOME`, at most 100 (`truncated`).
  `recent` and `common` are null then, so opening a folder reads no session.

Schema: [`contracts/herdr-folders.schema.json`](../contracts/herdr-folders.schema.json)
(`herdr-folders.json`, `herdr-folders-browse.json`).

### Starting an agent with it, favourite folders (HERDR-11)

The same `POST …/workspaces` may name **`launch`**, and may leave `cwd` out or
empty (that is the home folder). `launch` must equal, character for character,
one of the **launchers** the folders document lists; anything else is `400
herdr_launch_not_allowed` before anything is made. The launchers are the
operator's own `launchers.toml` (Collie's format: `[[launchers]] command
label?`, its `cwd` ignored - the folder is the card's), read from
`~/.config/omodachi/` else `~/.config/collie/` with HERDR-4's rules (re-read on
an mtime change, a bad row dropped, a file that stops parsing keeps its last
good rows, ours wins, at most eight; an empty file means none). Without a file
the launchers are `codex` and `claude`, each only when it is installed where a
login shell would find it (`$PATH`, then the per-user bin folders the Agent's
own lookup searches).

A launch is typed **only into the root pane `workspace create` just returned**
- nobody has typed there - as one `pane.send_text` and an `Enter`, after the
shell drew its prompt (`pane.read visible` not blank; up to four seconds, then
anyway - the terminal queues what is typed before the shell reads it). Nothing
is resized, attached or focused. The reply's `launch` says what happened:

```json
"launch": {"command": "codex", "pane": "w3:p1", "sent": true, "drawn": true, "reason": null}
```

A launch that could not be typed (`sent: false`, `reason` a Herdr code) leaves
the workspace standing with its shell. The audit line is `kind: launch`, the
command's length, never the command.

`GET /v1/herdr/folders` (without `under`) also carries `launchers` and
**`favourites`**: folders a person starred, shared by every device of the host,
in the order they were starred, at most 12, only while `host_paths` allows
them. `POST /v1/herdr/folders/favourites {"path": "~/Work/notes", "favourite":
true|false}` stars or un-stars one (the folder checked like `cwd`) and answers
with the folders document. They are kept in `herdr-folders.json` beside the
used folders (`"favourites": [{"path", "at"}]`). Both fields are null with
`under`. Fixtures: `herdr-folders.json`, `herdr-workspace-create-launch.json`,
`http-error-herdr-launch-not-allowed.json`.

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
published from a poll of every running session — `session` is what tells a
device whether the move was its own, because the revision alone cannot. The
snapshot is still the reading that cannot be wrong.

**HERDR-11: Herdr's events poke the poll.** On a Herdr speaking protocol 20 or
later (0.8.2 is 20), core keeps one `events.subscribe` connection per running
session (`herdr_events.py`), subscribed to the session's structure
(`workspace.*`, `tab.*`, `pane.created|closed|focused|moved|exited|agent_detected`,
`layout.updated`) and to `pane.agent_status_changed` for each of its panes
(0.8.2 requires a `pane_id` for that one, so the subscription is remade when
the panes change; at most 64). `pane.updated` is not subscribed: it fires on
every redraw of a busy pane. An event only marks its session as moved; the
session is then read within 150 ms (bursts gathered into one read). How often a
session is read otherwise:

| Session | Read every |
| --- | --- |
| watched (the owned one, any device's selection, a pane of it on a screen), events down | 2 s |
| watched, events up | 10 s (the reconciliation; events do the rest) |
| nobody is looking at it | 10 s |
| any session Herdr's events just said moved | at once |

A Herdr that refuses the subscription is left to the poll; a dropped one is
retried after 1, 2, 5, 15, then 30 seconds.

`GET /v1/herdr/layout` answers from the projection the poll just read while it
is still the newest revision and at most two seconds old (a device that reads
the layout because of `herdr.layout.changed` gets exactly that reading, without
a second `api snapshot`), with `ETag: "<session>:<revision>"`; `If-None-Match`
with that tag is `304`.

## A pane's latest reply (HERDR-11)

`GET /v1/herdr/panes/{pane}/reply` gives a codex or claude pane's latest reply
as plain text, from **the agent's own session log** - codex's rollout
(`~/.codex/sessions/YYYY/MM/DD/rollout-*-<thread>.jsonl`), Claude Code's
project log (`~/.claude/projects/<folder>/<session>.jsonl`). It is read-only,
the last 512 KiB (4 MiB once, if the last reply is further back), parsed only
when the file's size or time changed; it never touches the PTY and never asks
the agent anything. `herdr_reply.py`.

Which log is the pane's is decided only through the pane's processes:
`pane.process_info` (a read-only socket call, added to the allowlist) names the
pane's foreground processes, then `/proc` (core is the same user):

- **codex** - the rollout open in that process tree. codex 0.160 keeps it open
  in its `app-server-daemon` child (probed on the host, file names only). Two
  open rollouts are `ambiguous`; none, with exactly one
  `~/.codex/thread-writer-locks/<thread>.lock` open, is the rollout with that
  thread in its name (the last 62 day folders searched).
- **claude** - a log it holds open; else `--resume`/`--session-id <id>` on its
  command line while that log is the newest in its project folder; else the
  newest log in the project folder of its working directory written since the
  process started, **only when it is the one claude on the host in that
  folder** (two are `ambiguous`). Subagent logs (`subagents/`, `isSidechain`)
  are never the reply.
- A Herdr that reports the agent's session itself (`agent_session`, kind `path`
  or `id`) is believed first. 0.8.2 does not.

A path is followed only to a regular file inside the agent's own log folder.
The reply is the last assistant message: codex's `response_item` message
(`output_text` blocks; `phase` is codex's own `commentary`/`final_answer`;
`complete` is a `task_complete` after it), Claude's `assistant` lines of one
`message.id` (text blocks only - no `tool_use`, no thinking; `complete` is
`stop_reason: end_turn`). At most 32 KiB (`truncated`).

```json
{"contract_revision": "omodachi.v1", "session": "h11-test", "pane": "w1:p1",
 "available": true, "reason": null, "agent": "codex", "text": "Done. …",
 "at": "2026-10-07T10:00:04.000Z", "complete": true, "phase": "final_answer", "truncated": false}
```

Anything that cannot be matched reliably is `available: false` with a `reason`
- `not_an_agent`, `no_match`, `ambiguous`, `no_reply` - that clients never
show: **they show no reply at all**, only the mirror. `404 herdr_pane_gone` for
a pane the session no longer has. `ETag` + `If-None-Match` → `304`, so a client
can ask whenever the layout moves. Native and web credentials. Schema:
[`contracts/herdr-reply.schema.json`](../contracts/herdr-reply.schema.json)
(`herdr-reply.json`, `herdr-reply-claude.json`, `herdr-reply-unavailable.json`).

## Streams

HERDR-5: the App no longer opens `control` (its "Take keyboard" item is gone;
typing is `/keys` and `/text`, and `observe` at the pane's own size shows a
full-screen program as it is). `control`, `takeover=true` and the
`pane.focus` action stay here, unused by any client, for a later front end.

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

## Typing without taking the pane (HERDR-3)

| Method and path | Body | Reply |
| --- | --- | --- |
| `POST /v1/herdr/panes/{pane}/keys` | `{"keys": ["Escape", "ctrl+c", …]}` (1–32) | `{"action": "keys", "pane", "sent"}` |
| `POST /v1/herdr/panes/{pane}/text` | `{"text": string, "submit"?: bool}` | `{"action": "text", "pane", "delivered", "submitted", "reason", "proof"}` |

**By default the phone never takes the pane.** Both routes are one-shot calls on
the session socket - `pane.send_keys` and `pane.send_text` - so no controller is
held, nothing is resized, and the person at the desk keeps their pane as it was.
`control` is still there for the rare full-screen program (vim and friends) and
is what the App calls "Take keyboard".

Keys use Herdr's own grammar, probed on 0.8.2 (protocol 20): `Escape` (`esc`),
`Tab`, `Enter`, `Space`, `Backspace`, `Up`, `Down`, `Left`, `Right`, `F1`–`F12`,
any one printable character (`1`, `y`, `/`, `é`), and `ctrl+` / `alt+` /
`shift+` in front of one of those (`shift+tab` is ⇧Tab, `ctrl+c` is ^C). Names
are case-insensitive and are passed on in one canonical spelling. `PageUp`,
`PageDown`, `Home`, `End`, `Insert` and `Delete` do not exist in Herdr and are
`400 herdr_key_unsupported`; `cmd+`/`super+` (Herdr drops the modifier) and
tmux's `C-c` are `400 invalid_key`. More than 32 keys is `413 herdr_too_many_keys`.

Text is one `pane.send_text`, **never chunked** - Herdr adds no bracketed paste,
so a chunk boundary on a newline would submit half a message - and at most
16 KiB of UTF-8 (`413 herdr_text_too_long`). `\n` and `\t` are allowed; `\r` and
every other control character are `400 invalid_text`, because Enter is this
route's decision, not something the text carries.

With `"submit": true` the route reads the pane's **visible** screen before the
send and up to eight times after it (the first at once, then every 350 ms), and
presses Enter only when the screen shows the end of the text - its last 32
non-space characters, or all of it when shorter - more often than it did
before, or a new `[Pasted text #N +M lines]` token whose line count fits (Claude
Code collapses long pastes into one). Herdr's ack only means it took the bytes;
a dialog with focus swallows the text, and a blind Enter would then answer the
dialog. So without that proof nothing is submitted and the reply says so:

```json
{"contract_revision": "omodachi.v1", "action": "text", "pane": "w1:p2",
 "delivered": true, "submitted": false, "reason": "not_seen"}
```

`reason` is `null` when submitted, `not_requested` without `submit`, `not_seen`
when the text never showed up, `submit_failed` when the Enter itself was
refused.

**A dialog has the keyboard (VERIFY-1).** On the real host, a Quick
`continue` sent into codex's approval dialog never got its Enter - the proof
held - but the dialog read the letters as its own shortcuts and cancelled the
request. So with `submit`, core first asks Herdr's snapshot for the pane's
`agent_status`; while it is `blocked`, nothing is typed and the reply is
`"delivered": false, "submitted": false, "reason": "herdr_agent_blocked"`. The keys
(`1`, `↓ ↩`, `esc`) answer the dialog. A snapshot that cannot be read says
nothing, and the send goes ahead with the screen proof as before.

**Short replies (HERDR-4).** A reply under eight characters - `yes`, `y`,
`/model` - is a word that can turn up in new output anywhere on the screen, so
counting it proves nothing. For those, Enter follows only when a line that
changed between the two reads now *ends* with the reply and did not before:
the input line, `> ` becoming `> yes`, or Claude's grey hint becoming `> yes`.
`proof` says which reading it was: `tail`, `paste` or `line` (`null` when
nothing was submitted). `line` is the weaker one, and the App says "Sent"
without the ✓ for it. Reads are always `source: "visible"`: `recent` asks for rows above the
screen, and for an idle agent Herdr fetches those by scrolling the desktop pane
up and back.

Every write is one line in `~/.config/omodachi/herdr-writes.jsonl` (0600, rolled
once at 1 MiB): time, device, session, pane, `keys` or `text`, how many keys or
characters, the outcome and whether it was submitted - **never the keys or the
text**, because Type mode sends every keystroke here.

ODOMO-2: a web (Odomo) credential reaches these two routes as well - Odomo's
Herdr panel replies like the App's - and its lines carry its own device id.
Splits, zooms, closes and tab changes are audited the same way since ODOMO-2
(`kind` `split` / `zoom` / `close` / `tab_create` / `tab_close`, `length` 0).
What a browser never gets: the `control` stream (and so `takeover`),
`pane.focus` and `workspace focus` - the calls that move the computer's own
screen (`browser_gate.WEB_ALLOWED`, `tests/test_odomo2.py`).

**Attachments (ATTACH-1).** `/text` may carry `attachments`, the same upload
ids the Agent takes (`POST /v1/uploads`, `contracts/upload.schema.json`), each
a string or `{"id", "marker"}`. The pane only ever receives text: each
`[Image #N]` / `[File #N]` marker that has an attachment behind it becomes that
file's absolute host path where it stands, and an attachment whose marker was
edited away has its path put in front, in order (Collie ADR 0060). A marker
with nothing behind it is typed as written. codex reads an image path with its
own `view_image` tool ("Viewed Image" on the host, ATTACH-1 report §3) and
Claude Code takes image paths in a prompt. The audit line gets
`"attachments": <count>` - never a path.

## The operator's rows (HERDR-4)

| Method and path | Reply |
| --- | --- |
| `GET /v1/herdr/operator-rows[?agent=<herdr agent name>]` | `{agent, family, quick, keys, harness, commands, sources}` (`contracts/herdr-operator-rows.schema.json`) |

What one pane's **Quick** dock, **key presets** and **harness segment** show.
No `agent` is a shell pane. Every quick item goes out as `/text submit:true`,
every key row as one `/keys`, every harness button as its bare command with
`/text submit:true` - so the agent's own picker opens in the pane and no list of
model names is kept here.

What ships (Collie's): agent panes get `yes`/`no` and `continue`/`commit and
push`/`retry`/`skip`; a shell gets `y`/`n`; key presets are Ctrl C/D/U/R/L/Z
(D and Z ask for a second tap); Claude's harness is `/model /effort /compact
/resume`, Codex's `/model /compact /resume`. Shipped rows carry an `id` the App
words itself; the operator's carry their own words.

The operator's rows are Collie's files, in Collie's format
(`quick-replies.toml`, `keys.toml`, `commands.toml`; see Collie's
`docs/configure.md` and `*.toml.example`), read from `~/.config/omodachi/` - or,
per file, from `~/.config/collie/` when ours is absent - behind an mtime check
on every request, so an edit shows up on the next read with no restart.

- **Replace, never merge** (Collie ADR 0018, 0043): on a pane your rows address,
  they are that surface's whole list; other panes keep what ships. `bar = true`
  command rows replace the harness segment only.
- **Narrowest scope wins**: an exact agent name, then its family (`claude`
  reaches `claude-code`), then unscoped; `scope = "shell"` reaches shells.
- **A bad row is dropped** (`sources[].warning.code = operator_rows_dropped`);
  key chords use the `/keys` grammar above, so a row `/keys` would refuse never
  becomes a button. **A file that does not parse keeps its last good rows**
  (`operator_file_unparsed`, with the parser's line and column).
- A row naming a shipped dangerous command (`/clear`, `/logout`…) keeps its
  second tap whatever `confirm` says.

Read by the App and (ODOMO-2) by Odomo's Herdr panel, for its Quick dock and keys.

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
`terminal session control` and takes the pane away from whoever holds it. Since
HERDR-3 that includes another device on this bridge: its stream is closed with
the reason `taken_over`, and the pane is this one's.

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
`404 herdr_pane_gone` when Herdr no longer has the pane, the key and text codes
above,
`400 invalid_session` for a name that is not a running session on this host
(for `start`: not a session this host lists), `400 invalid_session_name` and
`409 herdr_session_exists` for `create`, `503 herdr_session_start_failed` when
the server did not come up,
`400` for an invalid pane, geometry, payload or control command, `409
herdr_control_in_use`, `409 herdr_request_failed` when Herdr itself refused.
`--demo` has no owned session and reports `herdr_unavailable`.
