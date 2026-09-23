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

`ensure` semantics are unchanged: it resumes the recorded thread, and it refuses
to start one when an independent TUI already owns the default agent.

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
- **Models.** `GET /v1/agent/default/models` pages `model/list` and returns
  `{"models": [{"id", "model", "display_name", "description", "hidden",
  "is_default", "default_effort", "efforts"}], "default": "<id>"}`.
- **Per-turn overrides.** codex has no model setter; model and reasoning effort
  ride the turn. `POST …/chat/messages` therefore accepts optional `model` and
  `effort`, and `thread/settings/updated` confirms what landed.
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
| `POST /v1/agent/default/chat/messages` | `{request_id, text, model?, effort?}` |
| `POST /v1/agent/default/chat/interrupt` | `{turn_id}` |
| `POST /v1/agent/default/chat/steer` | `{request_id, text}` |
| `GET /v1/agent/default/chat/approvals` | pending server requests |
| `POST /v1/agent/default/chat/approvals/{request_id}` | `{decision}` or `{input}` |
| `GET /v1/agent/default/chat/usage` | context and quota |
| `GET /v1/agent/default/models` | models and reasoning efforts |
| `GET /v1/agent/default/chat/commands` · `POST …/{command}:execute` | the reviewed slash commands |
| `POST /v1/agent/default/chat/handoff:prepare` · `:confirm` | take over an independent TUI thread |
| `POST /v1/agent/default/chat/recover-empty` | replace a confirmed-lost empty thread |

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
