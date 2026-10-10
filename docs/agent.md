# The default agent

Omodachi's Agent surface is one Codex conversation, owned by the host daemon and
reached from the phone over the same authenticated HTTPS/WSS the rest of core
uses. There is no terminal in the path: core speaks codex's own app-server
protocol, and the phone speaks core's `/v1/agent/default/chat*` routes.

## The transport is codex's own remote surface

`codex app-server` (0.154.0) listens on `stdio://`, `unix://PATH` or
`ws://IP:PORT`. Core starts the third one:

```
codex app-server --listen ws://127.0.0.1:<port> \
  --ws-auth capability-token --ws-token-file ~/.config/omodachi/agent/ws-token
```

- The listener is **loopback only**. codex refuses to bind anything else here
  and says so (`note: binds localhost only`). Nothing about the agent reaches
  the LAN except through core's own TLS listener.
- The capability token is 32 random bytes as hex in
  `~/.config/omodachi/agent/ws-token`, mode `0600` inside a `0700` directory.
  Core presents it as `Authorization: Bearer <token>`. A connection without it
  is rejected at the HTTP upgrade.
- The chosen port is recorded in `~/.config/omodachi/agent/endpoint.json`
  (`{"host","port","unit","token_file"}`, mode `0600`) so a restarted daemon
  reconnects to the listener that is already running instead of starting a
  second one.
- `codex` is looked up in core's own PATH, then the user manager's
  (`systemctl --user show-environment`), then the usual per-user bin
  directories (`~/.local/bin`, `~/.local/share/mise/shims`, `~/.cargo/bin`,
  …), and the unit is started with that same PATH. AGENT-NEW-1: after a reboot
  omodachid can start before the session imports its PATH and saw only
  `/usr/local/bin:/usr/bin`, so a mise-installed codex was "missing" and the
  Agent was gone until omodachid restarted.
- The process is a transient systemd user unit named **`omodachi-agent.service`**
  (`systemd-run --user --unit=omodachi-agent.service --collect
  --property=Type=exec`). It lives outside core's cgroup on purpose: restarting
  `omodachid` does not take the agent with it.

The wire format is one JSON object per line, responses omit `jsonrpc`, and the
handshake is `initialize` → `initialized`.

## The thread is never replaced

`~/.config/omodachi/structured-default/owner.json` is the manager's record of
the one owned thread:

```json
{"host_id": "omarchy", "provider": "codex",
 "cwd": "/home/alex/.local/share/omodachi/agent-workspace",
 "transport": "ws",
 "endpoint_file": "/home/alex/.config/omodachi/agent/endpoint.json",
 "token_file": "/home/alex/.config/omodachi/agent/ws-token",
 "thread_id": "01a0aeb1-…", "daemon_unit": "omodachi-agent.service"}
```

`cwd`, `transport`, `endpoint_file`, `token_file` and `daemon_unit` describe how
the manager reaches the daemon and are **rewritten in place** when any of them
changes — a rename, a move, or the switch from the Unix socket to the WebSocket.
`thread_id` is not: a record whose transport has changed is migrated, never
discarded, and the previous values are kept under `migrated_from` for provenance.
Creating a thread still requires a proven-missing default, and a crash between
"intent" and "identity" leaves `creation_pending` behind rather than a second
conversation.

The workspace is also pushed **into the thread**. Codex keeps the cwd a thread
was created with; the default thread on the first host was created as
`~/.local/share/omadochi/agent-workspace`, a directory the rename removed, and
every approved command then failed with "Failed to create unified exec process:
No such file or directory (os error 2)" while `view_image` could not find files
that existed. Rewriting `owner.json` alone did not reach codex. Core therefore
recreates the workspace directory if it is missing, sends `cwd` on every
`thread/resume` (`ThreadResumeParams.cwd`; if a provider refuses the override,
the plain resume is used) and on every `turn/start` (`TurnStartParams.cwd`,
"for this turn and subsequent turns"). The thread id and its history stay the
same; no replacement thread is started.

`ensure` semantics are unchanged: it resumes the recorded thread, and it refuses
to start one when an independent TUI already owns the default agent.

## A new conversation, when the person asks for one (AGENT-NEW-1)

"The thread is never replaced" holds for everything core does on its own. The
person can still ask for a fresh start: `POST /v1/agent/default/chat:new
{"confirmed": true, "expected_provider_session_id": "<the conversation on the
screen>"}` (App and Odomo: "New conversation" in the Agent panel's header).
Under the owner lock core

1. refuses while that conversation has a turn running (`409 agent_turn_active`)
   or a question waiting for the person (`409 agent_approval_pending`) - from
   the attached chat, or from codex's own `thread/read` status when none is
   attached - and a request for a conversation that is no longer the default
   (`409 agent_identity_changed`, so two phones pressing at once make one);
2. archives the current `owner.json` to `owner-before-new-conversation-<ns>.json`
   and saves the **intent** - `creation_pending`, `previous_thread_id`,
   `recovery_reason: "user_new_conversation"`, `previous_metadata`,
   `new_conversation_at` - before any RPC, exactly as the first creation does;
3. `thread/start {cwd: <workspace>}` and saves the new `thread_id`.

A core that dies between 2 and 3 finishes the intent on the next ensure (or a
retried request) rather than leaving `agent_thread_creation_unconfirmed`; the
plain first creation keeps its "never retried" rule. The old thread is **not**
archived, deleted or unsubscribed: codex keeps it in `~/.codex/sessions` and it
can be resumed by id (there is no UI to reopen it yet). Core then follows the new
thread on the same app-server connection (a fresh thread has no rollout until
its first message, so the connection is not dropped), clears the per-thread
delivery journal, tells every open event stream `{"type": "resync_required"}`
and publishes `agent.changed` with an idle face. Chat routes answer
`409 agent_conversation_changing` for the moment it takes. One journal line per
request: `{"omodachi":"agent","event":"agent.conversation.new","device","role",
"previous_thread_id","outcome","thread_id"?}` - never any words.

## The transcript has no stuck or empty rows

- A tool whose `item/completed` never came (codex sends none for a command that
  could not start) is closed with its turn: `interrupted` when the person
  stopped the turn, `failed` otherwise, as a `tool` event before
  `turnFinished` / `turnFailed`. History read back from a finished turn is
  closed the same way. Tool statuses on the wire: `running`, `succeeded`,
  `failed`, `cancelled` (declined), `interrupted`, `unknown`.
- codex brackets tool calls with commentary `agentMessage` items whose text is a
  single space. A message whose text is only whitespace never leaves core (not
  in a snapshot, not as an event); one that is still empty because it has just
  started goes out with its first visible delta.

## The conversation stays while a tool runs (AGENT-FIX-2)

Leo: "agent 在工具调用的时候，聊天记录会消失".

- **A snapshot is core's own rows, never a re-read.** Every App open, every
  `chat/events` connect and every `default:ensure` used to call `thread/read`
  and replace core's rows with the answer. codex 0.160 stores an item when it
  *finishes* (a paginated thread), so the read had no running command and no
  half-written reply, and ordered a turn by finish time: each phone that opened
  the panel mid-tool took those rows off every screen, and their later deltas
  had no row left to land in. While attached, core has applied every
  notification of the thread since `attach()` read the provider's record, so
  `snapshot()` now returns those rows as they are.
- **Re-attaching joins, it does not replace.** When the app-server connection
  is lost and `ensure` attaches again, `merge_rows` joins codex's record with
  the rows already shown: turns in codex's order; inside a turn the rows a
  phone has seen keep their places; a row of the turn still running that codex
  has not stored yet stays. A finished turn is codex's to say.
- **Output streams as increments.** `item/commandExecution/outputDelta` used to
  re-send the whole tool row - the whole output so far - per delta, which for
  a command that prints a lot flooded the stream until the subscriber queue
  overflowed (`resync_required`) or a frame passed the App's 1 MB limit. Now
  the deltas of one item are gathered for 0.1 s and go out as one
  `{"type":"tool.output","value":{"id","turnID","delta","detail_truncated"?}}`;
  any output still waiting goes out before the next other event, so an item's
  completion never overtakes its words. A client appends `delta` to the row's
  `detail` and keeps its newest 65536 characters.
- **A row keeps the newest 64 K characters of its output** and says how many it
  dropped (`detail_truncated`). A snapshot spends at most 512 K characters on
  tool output, newest rows first; an older row past that keeps its newest 4 K.
  Core's own rows are not cut by the snapshot budget.
- The App and Odomo reconnect by themselves when the stream breaks (0.5 s, then
  1, 2, 4, 8 s), and draw an answered approval as one line under the command
  it was about (`details.item_id`), not after the whole conversation.

## Edits you can read, steps in a few words (AGENT-COLLIE-1)

Checked against codex 0.160's own schema (`codex app-server
generate-json-schema`) and a real capture (`tests/fixtures/codex-0160-agent-collie1.json`).

- **A fileChange row carries the change.** codex's `fileChange` item has
  `changes: [{path, kind: {type: add|delete|update, move_path?}, diff}]` on
  `item/started`, and 0.160 sends that *before* `item/fileChange/requestApproval`,
  whose params name only the `itemId` (no diff, `reason` and `grantRoot`
  usually null). Core keeps the old fields (`name: "File changes"`, the JSON
  `detail`, `paths`) and adds `changes: [{path, kind, move_path?, added,
  removed, diff, diff_omitted?}]`. `diff` is unified-diff lines: an update is
  codex's hunks as they are; an added or deleted file arrives from codex as its
  whole text and gets `+` / `-` signs here; the `Moved to: …` codex appends to
  a moved file's diff becomes `move_path`. At most 16 files per row (the rest
  counted in `changes_omitted`), 120 lines per file, 400 per row, 400
  characters per line; a snapshot spends at most 256 K characters on diffs,
  newest rows first, and an older row keeps its files and counts with an empty
  `diff`. Only what codex put in its item ever travels: this is not a file
  viewer, and a path still opens on the computer (LINKS-1).
- **A fileChange approval names what it allows.** Its `details` add `files`
  (the row's paths, at most 8), `added` and `removed`, from the row core holds
  for `details.item_id`. A client draws the change from that row.
- **A command reads as a step.** Tool rows from `commandExecution` add
  `command` (the words without the shell codex wraps them in, when they
  differ from `name`), `actions` (codex's `commandActions`: `read {name}`,
  `listFiles {path?}`, `search {query?, path?}`, `unknown`; at most 4),
  `exit_code` and `duration_ms` once finished.
- Not used: `item/fileChange/patchUpdated` (in the 0.160 schema; not sent in
  the capture, even with `apply_patch_streaming_events` on) and
  `turn/diff/updated` (the turn's whole diff, unbounded).

## Approvals and input requests

codex asks the *client* for four things, as real JSON-RPC requests that stall the
turn until they are answered:

| Server request | Chat kind |
| --- | --- |
| `item/commandExecution/requestApproval` | `commandExecution` |
| `item/fileChange/requestApproval` | `fileChange` |
| `item/permissions/requestApproval` | `permissions` |
| `item/tool/requestUserInput` | `userInput` |

Each becomes one chat event, using the provider's own request id as the handle:

```json
{"identity": {"…"}, "sequence": 1,
 "event": {"type": "agent.approval.requested", "request_id": "7",
           "kind": "commandExecution", "summary": "ls -la",
           "details": {"item_id": "item-1", "turn_id": "turn-1",
                       "command": "ls -la", "cwd": "/home/alex/…",
                       "command_kind": "command"},
           "decisions": ["accept", "acceptForSession", "decline", "cancel"]}}
```

`details` is a bounded projection: scalars and short strings only, never a nested
provider structure and never an unbounded command output.

Answering is `POST /v1/agent/default/chat/approvals/{request_id}`:

- `{"decision": "accept" | "acceptForSession" | "decline" | "cancel"}` for the
  three approval kinds. For `permissions`, accepting echoes the requested
  profile back as the granted one (`scope` is `session` for `acceptForSession`,
  otherwise `turn`); declining grants nothing.
- `{"input": {"q1": ["main"]}}` for `userInput`, which becomes codex's
  `{"answers": {"q1": {"answers": ["main"]}}}`.

A `userInput` question is `{id, header, question, secret, options, choices,
other}`. `options` is the list of labels; `choices` (AGENT-COLLIE-1) is the same
list with the one-sentence `description` codex sends beside each label, and
`other` is codex's `isOther` - the person may answer in their own words (codex
0.160 sets it on every question a model asks). `secret` is codex's `isSecret`:
a client hides what is typed and never shows the answer afterwards.

Either way the reply is
`{"request_id": "7", "kind": "commandExecution", "decision": "accept", "resolved": true}`
and the stream carries `agent.approval.resolved` with `"source": "client"`.
When the same prompt is answered somewhere else, codex sends
`serverRequest/resolved`; the stale row disappears with `"source": "elsewhere"`.

`GET /v1/agent/default/chat/approvals` lists what is still waiting, and the chat
snapshot carries the same rows under `pendingApprovals`, so a phone that
reconnects mid-prompt still sees it.

Two invariants: this adapter never approves anything by itself, and a server
request it does not understand is **declined** (`-32601`) rather than ignored —
an unanswered request hangs the turn forever.

## Credential refresh

`account/chatgptAuthTokens/refresh` is a server→client request too. Core answers
it by re-reading `~/.codex/auth.json`, the credential store the Codex CLI itself
maintains, and returning `{accessToken, chatgptAccountId, chatgptPlanType?}`.
Core mints nothing and writes nothing under `~/.codex`. An installation
authenticated with an API key has no ChatGPT tokens; there the request is
declined with `-32001`, which is still an answer. The token never appears in an
event, a log or a client response — only `{"type": "agent.auth.refreshed",
"refreshed": true|false}` does.

## Status, models, usage, steering

- **Status.** `thread/status/changed` is the provider's own answer to "is it
  waiting on me": `state.agent.status` becomes `waiting_on_approval` or
  `waiting_on_user_input` and outranks Herdr's pane heuristic, which cannot see
  an approval prompt. The structured value lives at `state.agent.chat.status`.
- **Activity (MOTION-1).** `state.agent.chat.activity` is `{phase, turn_id}`,
  read from the provider's own turn and item notifications, for the animated
  face on the phone and in Odomo: `thinking` (`turn/started`, no item yet, or a
  `reasoning` item), `working` (an `agentMessage` started or streaming), `tool`
  (a `commandExecution`, `fileChange` or `mcpToolCall` in progress — it wins
  over a message that starts beside it), `done` (`turn/completed` completed),
  `error` (failed), `stopped` (interrupted by the user — INTEGRATE-2: a calm
  face with closed eyes, never the red error), `offline` (the provider
  connection is gone) and `idle` (no turn since attach). `done`, `error` and
  `stopped` stay until the next
  `turn/started`; whether anybody has seen them is the client's to remember by
  `turn_id`. `agent.changed` is published only when the phase moves, so a
  streamed delta publishes nothing. Waiting stays where it was: `status.waiting`.
- **Models.** `GET /v1/agent/default/models` pages `model/list` and returns
  `{"models": [{"id", "model", "display_name", "description", "hidden",
  "is_default", "default_effort", "efforts"}], "default": "<id>"}`.
- **Per-turn overrides.** codex has no model setter; model and reasoning effort
  ride the turn. `POST …/chat/messages` therefore accepts optional `model` and
  `effort`, and `thread/settings/updated` confirms what landed. Until it does,
  `usage.model` / `usage.effort` start from what `thread/resume` or
  `thread/start` say the thread runs with, so the model control shows the
  current choice from the first frame.
- **Usage.** `thread/tokenUsage/updated` and `account/rateLimits/updated` are
  merged into `state.agent.usage` and served by
  `GET /v1/agent/default/chat/usage`. Rate-limit updates are sparse rolling
  updates, so they merge and never clear a value they omit.
- **Interrupt and steer.** `POST …/chat/interrupt` is `turn/interrupt`.
  `POST …/chat/steer` is `turn/steer`: it adds to the turn in flight without
  stopping it, and fails with `agent_not_working` when there is no active turn.

## Routes

| Route | Meaning |
| --- | --- |
| `POST /v1/agent/default:ensure` `{"surface":"chat"}` | resume the owned thread, attach, return the snapshot |
| `GET /v1/agent/default/chat` | snapshot |
| `GET /v1/agent/default/chat/events` | WSS: `snapshot` then one event per change |
| `POST /v1/agent/default/chat/messages` | `{request_id, text, model?, effort?, attachments?}` |
| `POST /v1/agent/default/chat/interrupt` | `{turn_id}` |
| `POST /v1/agent/default/chat/steer` | `{request_id, text}` |
| `GET /v1/agent/default/chat/approvals` | pending server requests |
| `POST /v1/agent/default/chat/approvals/{request_id}` | `{decision}` or `{input}`; a decline may carry `reason` (AGENT-QUEUE-1) |
| `GET /v1/agent/default/chat/usage` | context and quota |
| `GET /v1/agent/default/models` | models and reasoning efforts |
| `GET /v1/agent/default/chat/commands` · `POST …/{command}:execute` | the reviewed slash commands |
| `POST /v1/agent/default/chat/handoff:prepare` · `:confirm` | take over an independent TUI thread |
| `POST /v1/agent/default/chat/recover-empty` | replace a confirmed-lost empty thread |
| `POST /v1/agent/default/chat:new` | `{confirmed: true, expected_provider_session_id}`: a fresh default conversation, the old thread kept |
| `POST /v1/agent/default/chat/mode` | `{mode: "plan" \| "default"}`: codex's collaboration mode for the turns that follow (the Plan chip) |
| `POST /v1/agent/default/chat/queue` | `{request_id, text}`: wait in codex's queue for the running turn to end (AGENT-QUEUE-1) |
| `POST /v1/agent/default/chat/queue/{id}:send` · `:edit` · `:delete` | a waiting message: Send now, new words (`{text}`), off the queue |
| `GET /v1/agent/default/chat/history?before=<row id>&limit=<n>` | earlier rows, a page at a time |

ODOMO-2: a web (Odomo) credential reaches every route above except
`recover-empty` (the App's own recovery, not a panel control; AGENT-NEW-1's
`chat:new` is a panel control and is allowed) - the same chat,
from the same panel shape. Each POST a web device makes is one journal line,
`{"omodachi":"web","event":"agent.write","device","route","status"}`, never the
words. The WebSocket opens with the Odomo ticket (`POST /v1/ws-ticket`).

## What is typed goes through (AGENT-PLAN-1)

Leo (2026-10-07): "甚至是 / 的优化整个都可以砍掉 只透传". The App and Odomo have no
slash list, no `/` button and no per-command answers any more: whatever the
person types is a message, sent to codex as typed - `/vim`, `/model gpt-x`,
`/new` included (the header has the model picker and New conversation).
`POST …/chat/messages` no longer refuses text that starts with `/`.

Four basics cannot work as words (Leo: "如果 基础 plan compact review diff 都记不了的话
别用了"), because each needs the app-server or core's own record rather than the
model, so core handles them quietly in the message route (and refuses them in
`…/chat/steer` with `409 agent_command_while_working`, as the TUI does while a
task runs):

| Typed | What core does | Answer |
| --- | --- | --- |
| `/plan` | codex's Plan mode on, or off when it is on (`thread/settings/update`, below) | `{accepted, delivery: "command", command: "plan", mode}` - no turn starts |
| `/plan <words>` | Plan mode on and the words (only) sent in the same turn | the usual send answer plus `command: "plan", mode: "plan"` |
| `/compact` | `thread/compact/start` (codex runs it as a turn with a `contextCompaction` item) | `{accepted, delivery: "command", command: "compact"}` |
| `/review` | `review/start {target: {type: "uncommittedChanges"}}`, inline - the TUI's first choice | the usual send answer plus `command: "review"` |
| `/review <words>` | `review/start {target: {type: "custom", instructions: <words>}}` - the TUI's `/review <words>` | the same |
| `/diff` | the conversation's file changes from core's own fileChange rows (AGENT-COLLIE-1's change cards; at most 16, newest last, a quarter of the snapshot diff budget) - never a file read, never `git diff` | `{accepted, delivery: "command", command: "diff", changes: [{path, kind, added, removed, diff, move_path?, diff_omitted?, turnID}], changes_omitted?}` |

`/compact <words>` and `/diff <words>` are the person's words. A codex that
refuses `review/start` gets `/review …` as typed. **Review turns:** codex 0.160
announces a review's turn with `turn/started` under another id than the one
`review/start` answers with and the review's items and `turn/completed` carry
(captured: `tests/fixtures/codex-0160-agent-plan1.json`, `review`). Core
follows it as one turn under the answered id - also for a review another
client of the thread started - or it would never end. `enteredReviewMode` /
`exitedReviewMode` items are not shown: the review's request is its user
message and its findings are the following agent message (same text).

A retried request id gets the same answer and does nothing again. An older
App never sends these (it still uses the command routes below, which stay).

## Slash commands (AGENT-NEW-1; kept for older Apps)

`GET …/chat/commands` lists codex 0.154's own commands (less `debug-m-drop`,
`debug-m-update` and `test-approval`, which codex marks developer-only).
Every one does one of three things, and none answers with a provider
structure for a phone to print (Leo saw `/model` print the raw model list):

| Command | What happens |
| --- | --- |
| `/model` | opens the header's model picker (`native_request.action: choose_model`); `/model <id> [effort]` validates against `model/list` and preselects that choice for the next message (it rides the turn, as before; core's connection now has the experimental API for Plan mode, but the model choice was left as it was) |
| `/new`, `/clear` | open the New conversation confirmation (`new_conversation`) |
| `/plan`, `/plan <words>` | codex's Plan mode (below): on, or off when it is on; with words, on and the words sent in the same turn |
| `/copy`, `/export` | the last reply / the whole conversation as Markdown, to the phone's clipboard |
| `/status`, `/pwd`, `/skills`, `/mcp [verbose]`, `/usage`, `/rename <name>`, `/compact` | run here; the answer is `display {key, args}` (scalars) that the App and Odomo word in the reader's language, and `result.summary` in plain English |
| everything else | not available, with `reason_code`: `command_terminal_only`, `command_settings_on_computer`, `command_sessions_on_computer`, `command_not_on_phone_yet`, each one sentence in both clients |

`tests/test_agent_commands_display.py` executes every catalog command against
codex's real answer shapes and fails on anything that looks like JSON.

## Plan mode (AGENT-PLAN-1)

codex 0.160 has two collaboration modes, `default` and `plan` (`ModeKind`;
`collaborationMode/list` returns the presets `Plan` - effort `medium` - and
`Default`). In Plan mode codex gives the model its built-in Plan instructions:
work out a decision-complete plan with the person, ask with
`request_user_input` (the question cards; in Default mode the tool answers
"unavailable"), change nothing, and end with a `<proposed_plan>` block. codex
lifts that block out of the reply into a `plan` item (`id: "<turn>-plan"`,
streamed by `item/plan/delta`, the whole text again on `item/completed`).

- **The mode is the thread's.** codex keeps it in the rollout
  (`ThreadSettingsApplied`, `TurnContext.collaboration_mode`) and says it on
  `thread/resume` (`collaborationMode`) and on `thread/settings/updated`
  (`threadSettings.collaborationMode`). Core reads both into `usage.mode`
  (`default` | `plan` | null until codex has said), so the phone's chip survives
  re-attach, a core restart and an app-server restart. Nothing on the phone
  holds it.
- **Changing it.** `POST …/chat/mode {mode}` and bare `/plan` send
  `thread/settings/update {threadId, collaborationMode: {mode, settings: {model,
  reasoning_effort, developer_instructions: null}}}` - what the TUI's `/plan`
  and Shift+Tab send. `model` and `reasoning_effort` are the conversation's
  current ones (codex requires a model, and the mode's settings replace the
  thread's model and effort); `developer_instructions: null` means codex's
  built-in words for the mode. `/plan <words>` puts the same `collaborationMode`
  on the `turn/start` that carries the words (one step, as the TUI does). A
  later turn's `model` / `effort` override keeps the mode. Refused while a turn
  runs (`agent_busy`; the TUI's `/plan` is not available during a task either).
  Core sends no `approvalPolicy` or `sandboxPolicy` for any of it.
- **Why `experimentalApi: true`.** Every way to set a mode is experimental in
  0.160: on a stable connection `turn/start.collaborationMode`,
  `thread/settings/update` and `collaborationMode/list` are refused with
  "… requires experimentalApi capability", and `thread/settings/updated` is
  only delivered to connections that opted in. Core's connection opts in.
  It reads only the fields it names, so an experimental field it does not know
  changes nothing. A codex that still refuses the change answers
  `agent_mode_unavailable`.
- **The plan on the phone.** A `plan` item is an assistant message row with
  `plan: true` (a client that does not know the mark shows it as a reply).
  codex's history lists the plan before the words that introduce it (items in
  the order they finished); core puts it after them, as it streamed.
- **Leaving.** The chip (`mode: default`) or `/plan` again. After a plan, the
  App and Odomo offer what the TUI's "Implement this plan?" offers: switch to
  Default and send "Implement the plan." (codex's own words for it).

## A question asked again is answered again (AGENT-QUEUE-1)

Leo: "askuserquestion 的卡片 会循环" - a three-question card came back at
question 1 after he answered, several times. codex re-sends a server request
it is still waiting on - identical: same id, item, turn and questions - to a
connection that resumes the thread (real 0.160 frames, `ask_replay`). If
core's answer went down with its connection to the app-server, codex is
still waiting, and every re-attach put the same questions back on the
phone. Core now keeps what it answered (`request_fingerprint` of the
request, and the answer) across re-attaches to the same thread; an
identical request it already answered is answered again at once and never
reaches the phone. A different request that reuses the id (codex numbers per
connection) is asked as usual. The App and Odomo also keep a question card's
progress outside the card, so a card drawn again carries on where the person
was - after an answer, at "Check your answers"; a secret answer is never kept.

## Queue, checklist, a reason, earlier messages (AGENT-QUEUE-1)

Four things the phone does while codex works, each on codex 0.160's own
protocol (`tests/fixtures/codex-0160-agent-queue1.json`, real frames):

- **Queue.** While a turn runs, the composer's Send puts the words in codex's
  own durable queue (`thread/queue/add`, experimental): codex keeps it on
  disk (`queue_1.sqlite` in `CODEX_HOME`), across a phone being killed or
  offline and across core and app-server restarts, and starts each message
  as a turn of its own, in order, when the running turn *completes*. After a
  Stop it waits (codex does not drain an interrupted thread) until Send now.
  Every client of the thread - the App, Odomo, codex's TUI - sees the same
  queue: core lists it on `thread/queue/changed` and sends
  `agent.queue.changed {queue}`. Send now steers it into the running turn
  (off the queue first, so codex cannot start it too; back first in line if
  the turn ended in between) or, between turns, starts it
  (`thread/queue/start`). Edit is `thread/queue/update`, in place. Words core
  steered (Send now, a decline's reason) that codex had not yet taken when
  the turn ended - codex drops steered input a turn never reached, e.g. a
  Stop while it waits on an approval - go back on the queue, first in line.
  An approval is never queued. A codex without the queue (`thread/queue/list` refused)
  makes `queue: null`; clients then steer while a turn runs, as before. A
  queued message takes the thread's model and effort, not a pending
  per-turn choice (codex's queued submission carries words only).
- **Checklist.** Clients pin it while its turn runs, or while a step is open
  and no later turn has started (a plan the model left behind goes).
  codex's `turn/plan/updated {turnId, explanation, plan}` (the
  model's `update_plan` tool) becomes `checklist` in the snapshot and
  `agent.checklist.updated`. 0.160 ships `update_plan` **off** (`[tools.update_plan]
  enabled = true` turns it on), so on a stock config there is no checklist and
  clients show nothing. codex's record does not hold plan updates; core keeps
  the newest in `structured-default/checklist.json` for its thread.
- **A reason.** `POST …/approvals/{id} {"decision": "decline", "reason": "…"}`.
  codex's decisions carry no words, so the reason is the person's message:
  `turn/steer` into the turn *before* the decline is answered (the model's
  next request then holds the refusal and the reason together), else codex's
  queue (the next message). The reply's `delivery` says `steered`, `queued` or
  `unsent` (the client keeps the words). Only a plain decline takes one; no
  reason is today's decline.
- **Earlier messages.** Attach resumes with `excludeTurns` and
  `initialTurnsPage` (newest 10 turns; experimental) instead of the whole
  thread; a snapshot carries the newest 150 rows and `earlier`; `GET
  …/chat/history?before=` returns older rows from core's copy, then codex's
  record a page at a time (`thread/turns/list`). A codex that will not page is
  resumed whole, as before, and still sent newest-first from core's copy. A
  message longer than 65536 characters travels cut, with `text_truncated`.

## Attachments (ATTACH-1)

A message may carry `attachments`: upload ids from `POST /v1/uploads`
(`uploads.py`, `contracts/upload.schema.json`), in chip order, each a string
or `{"id", "marker"}` where the marker is the `[Image #N]` / `[File #N]` the
phone put in the words. At most ten; an id that is no longer on the host is
`409 attachment_gone` and nothing is sent.

- **An image** goes to codex as the app-server's own image input,
  `{"type": "localImage", "path": <the upload's path>}`, after the text item
  in `turn/start`. codex 0.154's `UserInput` (`codex app-server
  generate-json-schema`, `v2/TurnStartParams.json`) has `text`, `image {url}`,
  `localImage {path}`, `audio`, `localAudio`, `skill` and `mention`; both
  image items take an optional `detail` (`auto|low|high|original`), which is
  left to codex. codex numbers the pictures `[Image #1]`, `[Image #2]`, … in
  that order, so an image's marker is renumbered to match (a draft numbers
  files and pictures together). PNG, JPEG, GIF and WebP are read; **HEIC is
  dropped by codex without a word** (it answered "NO IMAGE" on the host), so
  core converts HEIC to JPEG when it is uploaded.
- **A text file or a PDF** becomes its absolute path where its marker stood;
  one whose marker is gone from the words is listed on a last line,
  `Attached: <path>, …`, for the agent to open with its own tools.
- **If codex refuses the image input** (`provider_request_rejected` on
  `turn/start`), the same words go again with the image paths on an
  `Attached:` line, and the reply says `"images": "path"` instead of
  `"native"`, so the phone can say the agent got the path, not the picture.

The user row the provider echoes back carries `images: <count>` when pictures
rode with it - a count only, never a path.

Contract documents: `contracts/agent-chat-snapshot.schema.json`,
`agent-chat-event.schema.json`, `agent-chat-approvals.schema.json`,
`agent-chat-usage.schema.json`, `agent-chat-models.schema.json`, with generated
examples under `contracts/fixtures/agent-chat-*.json`.

## Configuration status

The probe (`agent.py`, `ReadOnlyAgentProbe`) still reports what Omarchy has
configured, what Herdr supports and whether a default agent exists; that model is
`contracts/schemas/default-agent-capabilities.schema.json` and is published as
`state.agent.default_agent`. It is read-only: core does not write
`~/.config/omarchy` agent settings and does not install agent kinds. A host whose
configured kind is not `codex` gets `structured_agent_kind_unsupported` from the
chat surface rather than a silently different provider.
