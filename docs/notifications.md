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

No D-Bus server, no APNs, no background push. A phone sees notifications while
it holds the event stream; waking a backgrounded app is a later piece of work.

Contracts: `contracts/notifications.schema.json`,
`contracts/notification-action.schema.json`,
`contracts/notifications-dnd.schema.json`, and the `notification.posted` branch
of `contracts/events.schema.json`.
