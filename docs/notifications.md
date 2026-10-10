# Notification sync

`org.freedesktop.Notifications` has exactly one owner on the bus and the Omarchy
shell is it (`shell/plugins/notifications/Service.qml` declares a full
`NotificationServer`). Omodachi cannot start a second one and does not
eavesdrop on the first. It reads what the shell already writes down.

## Where the notifications are

Every toast the shell shows is mirrored to a file:

- `~/.local/state/omarchy/notifications/<ms>-<id>.json` while it is on screen;
- `~/.local/state/omarchy/notifications/history/<ms>-<id>.json` after it goes
  away, including notifications that DND silenced;
- referenced images are copied into `notifications/images/`, so they outlive the
  toast;
- DND itself is `~/.local/state/omarchy/notifications.json`.

The file is the shell's own snapshot: `{id, originalId, app, appIcon, summary,
body, image, glyph, execArgv, urgency, expireTimeout, timestamp}`.

**The shell keeps ten.** `historyLimit` is 10, and evicting a file deletes the
icons it referenced. Anything that wants a longer history has to catch a
notification while it is still there, which is why core keeps its own bounded
copy (500 rows) in memory rather than reading the directory on request.

## The watcher

`NotificationMirror` watches both directories and rescans on every wakeup. The
wakeup comes from inotify when it can be set up — reached through `ctypes`,
because core has no watcher dependency — and from a 2 s timer otherwise. Either
way the rescan is the source of truth, so a missed inotify event costs latency
and nothing else. A file that disappears from the active directory marks its row
inactive.

Each new file becomes one broadcast event:

```json
{"seq": 3, "event_id": "evt_00000003", "type": "notification.posted",
 "device_id": null, "ts": 1789711988.81,
 "payload": {"id": "1789711988810-1", "app": "Omodachi",
             "summary": "“QA companion” wants to connect",
             "body": "Open Omodachi to approve or reject it.",
             "glyph": "", "urgency": "critical", "timestamp": 1789711988810,
             "has_action": true, "active": true}}
```

**`execArgv` is read to answer one question — is there an action — and is then
dropped.** It is not stored, not published and never executed from a remote
request. `urgency` is mapped to `low` / `normal` / `critical`. The id is the
shell's own file name, which is also the dedupe key and the `since` cursor.

## Routes

| Route | Meaning |
| --- | --- |
| `GET /v1/notifications?since=&limit=` | the mirrored history, oldest first, with a `cursor` |
| `POST /v1/notifications/{id}:invoke` | fire the default action |
| `POST /v1/notifications/{id}:dismiss` | take it off screen |
| `GET /v1/notifications/dnd` | `{"dnd": true|false}` |
| `POST /v1/notifications/dnd` | `{"enabled": true|false}`, or an empty body to toggle |

Actions map onto the shell's own IPC and nothing else —
`omarchy-shell notifications {invokeLast,dismissOne,dismiss,dndState,setDnd,toggleDnd}`,
the same five verbs `default/hypr/bindings/utilities.lua` already binds. The
shell only offers "act on the newest popup", so:

- `:invoke` is accepted **only** for the newest active notification, and only
  when it has an action. Anything else is `notification_not_actionable`. An
  id-addressed invoke would mean running the stored `execArgv` here, which this
  module refuses to keep at all.
- `:dismiss` uses `dismissOne` for the newest and `dismiss <summary>` otherwise.

DND is **written** through the same target, so it stays consistent with the
bar indicator and the `SUPER+ALT+.` binding. It is **read** from the file the
shell keeps it in (CORE-2 §3): the notifications plugin hydrates
`doNotDisturb` from `~/.local/state/omarchy/notifications.json` at start and
writes `{"version": 3, "dnd": <bool>}` back 200 ms after every change, so the
file is the state and reading it spawns nothing. Before CORE-2 every read - and
the menu's checked-state refresh made one roughly every 16 s - was an
`omarchy-shell … isDnd` process, i.e. one `qs` under `LANG=C`, whose Qt locale
warning put four lines into the user journal each time (G17). The shell is
asked only when the file cannot say (DND never toggled on this install, or an
unreadable file), then at most every 30 s, and every `omarchy-shell` call now
runs with `LANG=LC_ALL=C.UTF-8`. A toggle's read-back is the shell's own answer
to `toggleDnd`, because the file lags it by the save timer.

## What this is not

## Do Not Disturb is state, not a client's memory

`state.notifications.dnd` carries the shell's own reading (ARCH-1, Study 04
N-36). It starts as `null` — "nobody has asked the shell yet" — and becomes a
boolean once the mirror has read it. The mirror re-reads whenever
`~/.local/state/omarchy/notifications.json` moves and after a `POST`, and
publishes a `notifications.changed` event when the value is different from what
state already says. Nothing else writes it.

That field exists because DND has two owners: the desktop's own bar indicator
and the client's switch. A client that remembered its own boolean would go on
saying "off" after the user turned it on at the machine, so a client reads the
state and never caches a boolean of its own; the switch moves when the state
does, not when the finger lifts.

No D-Bus server and no APNs key of Omodachi's. The App sees notifications
while it holds the event stream. A locked phone is reached through Omodachi
Odomo's Web Push, below.

## Leaving the screen is news too

A toast that leaves the desktop's screen - dismissed there, dismissed by a
client (`POST /v1/notifications/{id}:dismiss`), or timed out into history - is
published as `notification.updated` with the whole row, now `active: false`
(LITE-1b; NEXT-1 found Odomo still showing rows the App had dismissed). It is
not a second `notification.posted`: the App toasts on that. The mirror's watch
thread hands every event to the daemon's event loop
(`call_soon_threadsafe`); nothing touches the hub from that thread.

## A decided request is withdrawn (NOTIFY-3)

The Omodachi plugin posts one notification per pairing request ("“iPad” via
Tailscale wants to connect") and per SSH-key request ("… wants an SSH login"):
app name `Omodachi`, urgency critical, and a click that summons the plugin's
Devices view (`omarchy-shell shell summon com.omodachi.host
{"view":"devices","request_id":…}`). The shell never times out a critical toast,
and before NOTIFY-3 nothing took these down once the request was decided: Leo's
desktop held five of them, from requests approved, denied or revoked long
before, and every client listed them for good.

- **Ours, provably.** A row is one of these only when the app name is
  `Omodachi` *and* the stored click command is exactly that summon
  (`request_marker`). It carries `kind: "pairing_request" | "ssh_request"` on
  the wire. Nothing else is ever matched.
- **Still waiting?** On every pass that sees one of ours, the mirror asks the
  service (`pending_request_markers`): pairing requests whose status is still
  `pending` and not expired (`PairingStore.pending_request_ids`, a lock-free
  read of `pairing.json`), and SSH keys still waiting. Approved, rejected,
  expired, replaced by the same device asking again, the device revoked, or the
  daemon restarted (an SSH request lives in memory) - all of these are "not
  waiting". When the answer cannot be had, nothing is withdrawn.
- **Withdrawn from every list.** The row leaves the mirror and is published as
  `notification.updated` with `active: false, withdrawn: true`; clients drop it
  (it is not history), and Web Push retracts its tag. A request that was decided
  before its notification was ever seen is never announced at all - this is
  also the sweep at daemon start.
- **Off the desktop.** On the watch thread, the file is read again and must
  still be ours, then: `omarchy-shell notifications dismiss <title>` (with
  `OMARCHY_PATH`; the wrapper finds the session itself). The shell matches
  titles by substring, so the call is only made when no other toast on screen
  contains the title. If another *of ours, still waiting* does (the same device
  asked again, same title), this one waits and goes with it. If anybody else's
  does, the shell is not asked.
- **Fallback: our own file.** When the shell does not answer, says it has no
  such toast, has not moved the file within 4 s, or a stranger shares the
  title, the file `<ms>-<id>.json` is removed. Safe for the shell (the file is
  only its restart backstop; its later archive is `mv … || exit 0`), but not
  complete: a toast still drawn stays drawn until clicked or the shell restarts.
- **Out of history.** Our decided ones are removed from `history/` as well; a
  history replay would otherwise put them back on screen as critical toasts
  that never expire.

Each withdrawal is one journal line, `{"omodachi":"notifications","event":
"notification.withdrawn" | "notification.withdraw", ...}`.

## Web Push to Odomo (LITE-1b)

The host's only way to a locked phone, with no relay and no APNs key: a
browser running Odomo subscribes with its own push service against
this host's VAPID key, and the daemon sends to it.

- **Who.** Web devices only (`push_web_only` otherwise). One subscription per
  device: `POST /v1/push/subscriptions` with the browser's
  `PushSubscription.toJSON()`, `DELETE` to drop it; `GET
  /v1/push/vapid-public-key` first. The endpoint must be https on Apple's,
  Google's, Mozilla's or Microsoft's push service. Revoke and purge delete it;
  so does the push service answering 404 or 410. The Devices page (and
  `omodachi-host devices push <device> off`) switches one browser off.
- **What.** `GET/PUT /v1/notifications/prefs` (a revision, 409 when stale):
  five kinds - `agent_approval`, `agent_needs_input`, `agent_done`,
  `herdr_blocked`, `host_notification` - each on or off; `snooze_until`;
  `quiet_when_native_connected` (NOTIFY-2: a push is held back only while a
  native App of this host is in front - a live event stream and its last
  `PUT /v1/presence` said `foreground` - *and* that presence listed the kind
  among the ones the App alerts by itself; a kind the App does not alert, such
  as `agent_done`, always goes, and an App from before NOTIFY-2 says nothing
  and so holds nothing back); the language the titles are written in (`en`,
  `zh-Hans`).
- **When.** From hub events: the default agent's status moving to
  `waiting_on_approval`, to waiting for input (`waiting_on_user_input` or
  Herdr's `blocked`), or from working to `done`; a Herdr pane other than the
  default agent's becoming blocked (`herdr.layout.changed` carries the blocked
  pane ids; NOTIFY-2: every *running* session is watched from daemon start,
  the list re-read every ten seconds inside the two-second snapshot poll, not
  only the sessions a device opened - and a session that stops says so once,
  with nothing blocked, which withdraws what was waiting in it); a new active
  desktop notification. A push held for its 30-second window is checked
  against the App's presence again when the window ends. The first push of a source
  goes at once; whatever the same source says in the next 30 seconds becomes
  one push ("and 2 more"). The default agent's three kinds share one tag, `agent`,
  so each newer state replaces the last one on screen - "done" replaces "needs
  your approval" - which is the only way to update an iPhone. When the thing
  is over and nothing replaces it (the approval answered, the pane
  unblocked), a retraction closes the notification by tag, except on Apple's
  push service, where every push must show something and none is sent: there
  the stale notification stays until the next agent push replaces it. A desktop notification is never retracted: its leaving the screen
  looks the same whether it was dismissed or timed out, and the phone of
  someone away from the computer should keep it. A push the browser on this same computer
  shows lands in the desktop's notification list; anything titled like a push
  sent in the last ten minutes is that echo and is not pushed back.
- **Shape.** A declarative Web Push message, at most 2.7 KB
  (`contracts/push-payload.schema.json`): `{"web_push": 8030, "notification":
  {title, body, navigate, tag, lang, dir}}`. Safari (iOS/iPadOS 18.4+, Home
  Screen apps) shows it without waking any script; other browsers hand it to
  Odomo's service worker, which always shows it. `navigate` is
  `https://<host>/odomo/?open=<agent|herdr|notifications>&<ids>` - ids only
  (ODOMO-2: `herdr`, the panel's real name; Odomo still reads the `herd` an
  older push or installed worker carries); NOTIFY-2 adds `host=<host_id>`,
  which Odomo's Agent and Herdr panels use for
  their "Open in Omodachi" link (`https://omodachi.app/open/agent?host=`,
  `/open/herdr?host=&session=&pane=` - a real tap on a real link, which is
  what lets iOS open the App). HERDR-4: a Herdr push also carries `agent=<name>`
  when the blocked pane's agent has a plain name (it is the push title's own
  word), so the Herdr card says "It is claude, in the work session." instead of
  Herdr's pane id; the id stays in the link only.
  The `Topic` header (replace-if-undelivered) is 16 hex characters of the
  tag's SHA-256: Apple reads Topic as base64url and answers `400
  BadWebPushTopic` to a 21-character one (measured). A refusal's own reason
  (`{"reason": ...}`) is logged with the status, never the endpoint or keys.
  Pushes to one browser go out in order, so a retraction never overtakes what
  it retracts.
  Encryption is RFC 8291 (`aes128gcm`), the JWT is RFC 8292 (ES256), reused for
  at least an hour per push service, `sub` = the host's own
  `https://<name>.ts.net` (Apple refuses `mailto:` at localhost). Sending is on
  worker threads with at most two retries (429/5xx/no answer); nothing waits on
  the event loop.
- **Where from.** `src/omodachi_core/webpush.py` (the sender, with RFC 8291's
  own test vector in `tests/test_push.py`), `push.py` (store, kinds, sender),
  `odomo/public/sw.js` (the worker), `odomo/src/panels/PushSettings.jsx`.

An iOS Simulator cannot subscribe to Web Push; desktop Chrome can
(`odomo/tests/e2e/push.mjs`), and an iPhone is checked by hand.

Contracts: `contracts/notifications.schema.json`,
`contracts/notification-action.schema.json`,
`contracts/notifications-dnd.schema.json`, the `notification.posted` and
`notification.updated` branches of `contracts/events.schema.json`, and for push
`push-vapid-key`, `push-subscription`, `push-unsubscribe`,
`notifications-prefs` and `push-payload`.

## The App's own alerts (NOTIFY-2)

The native App raises local notifications from the same `/v1/events` stream,
whichever panel is on screen: the default agent waiting on an approval or on an
answer (`agent.changed`), and a Herdr pane turning `blocked`
(`herdr.layout.changed`, the first sighting of each session being a baseline,
not news). It debounces and replaces them by thread the way `push.py` does and
withdraws them when the thing is over. A tap lands on the place: the Agent
panel, or Herdr on that session with that pane selected, on that computer.

The App tells core what it does with `PUT /v1/presence {foreground, alerts}`
(`contracts/presence.schema.json`, native credentials only): on every
connection, when it goes to the background (it keeps the stream for the few
seconds iOS allows, so a background alert can still fire), and when its alert
permission changes. `alerts` is empty unless iOS lets it show banners, so a
quiet (provisional) or refused permission never mutes Odomo. Presence lives in
memory and only counts while that device's event stream is live.

## Act on the alert, quiet at the desk, done but not seen (NOTIFY-1)

**Deciding from an alert.** Every pending Agent approval carries a
`fingerprint`: 16 hex characters of SHA-256 over what it shows (request id,
kind, summary, details, decisions). `state.agent.chat.approval` publishes the
oldest pending one as ids only - `{request_id, kind, fingerprint, decidable}`,
never what it asks - so the App's alert can offer Allow / Deny on exactly that
request. `POST /v1/agent/default/chat/approvals/{id}` takes an optional
`fingerprint`; core sends the decision to codex only if that same request is
still pending **and** still reads the same. Otherwise nothing is sent:
`409 agent_approval_unknown` (answered already, at the computer or elsewhere) or
`409 agent_approval_changed`. A question (`decidable: false`) is answered in the
App. Herdr panes get no Allow / Deny: core does not know a pane's choices (the
App reads them off the screen, HERDR-9), so their alert's only action opens the
App on the pane. Odomo's declarative Web Push cannot carry actions on Apple.

**At the desk** (`desk.py`). Core reads three things without privileges and
without any hook on the input devices: the compositor's idle timer through
Wayland `ext-idle-notify-v1` (v2's input-idle notification when offered, 2 min),
whether a screen locker of this user runs (`/proc`), and whether a Remote
session is driving the desktop from a phone. `state.desk = {present, state,
source}` and `desk.changed` carry the answer; `state` is `using`, `idle`,
`locked`, `remote` or `unknown` (the idle timer could not be read: nothing is
ever held then). While `present`, pushes to a web device with `quiet_at_desk`
(default true) are held, newest per source; when the desk is left or locked,
what is still waiting goes and what was answered at the computer is dropped.

**Herdr** waits 30 s before it says anything (`HERDR_DELAY_SECONDS`); a pane
answered inside that time was answered by someone at the computer, and nothing
is sent. What does go is **one digest per host** (tag `herdr`): "codex needs
you" for one pane, "3 agents need you" for several. When every pane it named is
answered it is withdrawn where the platform allows it. Retractions now live as
long as the push (TTL 3600, urgency high, the push's own topic). A snooze, or a
kind switched off, also stops what was already waiting in a coalescing window.

**Done, not yet seen** (`seen.py`, `~/.config/omodachi/seen.json`). Per Herdr
pane and for the Agent conversation: the last status, when its agent last
finished and when a device last had it open. Finished is the transition
`working|blocked -> idle|done` (Herdr 0.9 reports a finished agent as `idle`;
the Agent's `thinking|working|tool -> done|error`); a first sighting is a
baseline. Seen is a device opening the pane's stream or sending it keys or
text, or holding the Agent chat's stream. `state.unseen = {agent, herdr:
[{session, pane_id, name}]}` and `unseen.changed` expose it. The `herdr_done`
push kind ("codex finished") is off by default and is held at the desk like the
rest; one already seen when the desk is left is dropped.

## One "Needs you" list (OVERVIEW-1)

`overview.py` keeps one projection of everything that wants the person:
`state.overview = {rows, counts, sessions}` and `overview.changed` (published
only when it moved; nothing to show is never published - `state()` carries the
empty list then). Rows are the Agent conversation and every pane with an agent
in every running Herdr session, each `needs_you`, `working` or `done`
(finished and not seen, from the ledger above), in position order: the Agent,
then sessions by name and their panes in layout order. The default agent's own
pane in the owned session is the Agent row.

`ask` says what is asked, in a few words a client translates:

| source | state | ask | from |
| --- | --- | --- | --- |
| Agent | needs_you | `approve_command` / `approve_files` / `approve_permissions` / `answer` / `approve` | the pending approval's kind (`state.agent.chat.approval`), never what it asks |
| Agent | working | `working` | the turn's activity phase |
| Agent | done | `finished` / `failed` | `state.unseen.agent`, the last phase |
| Herdr | needs_you | `pick` / `yes_no` + `question`, else `waiting` | the pane's visible screen (`pane.read visible`, once per pane revision): a run of options numbered one apart with exactly one marked (`›` / `❯`), or a `(y/n)` question; the question is the nearest line above the options ending in `?` |
| Herdr | working | `working` | Herdr's status |
| Herdr | done | `your_move` + `question`, else `finished` | the agent's own latest reply (HERDR-11's log match) when it ends in a question |

A screen that does not read as one of those two shapes is `waiting` (a client
says "Waiting for you"): a guess is never shown as the question. Questions are
cut to 120 characters. `counts` and `sessions` (per-session counts, Herdr panes
only) are taken before the rows are cut to 64.
