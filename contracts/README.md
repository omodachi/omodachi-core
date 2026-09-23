# Omodachi core contracts

The implementation defines contract revision **`omodachi.v1`**. Schema `$id`s
are `https://omodachi.dev/contracts/v1/<name>.schema.json`; they are identifiers,
not URLs the verifier fetches.

`fixtures/` contains deterministic, synthetic examples for web, iOS, and
plugin work. The `theme/`, `fonts/` and `herdr/` subdirectories are recorded host *inputs*
the readers consume, not wire examples; the generated documents beside them are
what the registry validates. They include no live host identifiers, pane content, real window
titles, credentials, or LLM keys. IPC request examples contain the explicitly
invalid marker `fixture-invalid-token-not-a-credential`. Menu action/condition
strings are synthetic host-side catalog data, not instructions for a client to
execute. Fixture timestamps describe the frozen sample scenario.

The capability wire resource has one shape:

```json
{
  "contract_revision": "omodachi.v1",
  "sunshine": false,
  "terminal": true,
  "desktop": false,
  "native": []
}
```

The terminal flag describes core routing capability. Real SSH reachability,
keys, Herdr attachment and client PTY support still need their own checks.
The separate default-agent model preserves configured/actual kind, pane probe,
status and readiness. The snapshot may report `working`, `blocked` or `done`
without changing kind or target to make the UI appear ready.

| Schema | Example family and meaning |
| --- | --- |
| `capabilities.schema.json` | Device-level capability flags |
| `state.schema.json` | Host/workspace/focus, agent/Herdr summaries, Remote projection, capabilities, catalog, event cursor and daemon instance ID |
| `schemas/default-agent-capabilities.schema.json` | Read-only default-agent model; includes working/blocked/done/missing/kind-mismatch samples |
| `schemas/herdr-status.schema.json` / `herdr-resource.schema.json` | Detailed Herdr model / device resource, including nullable unknown server flags and schema probe |
| `catalog.schema.json` | Actual three-layer JSONC compiler/runtime snapshot; a condition is available (bool), unknown (null, host could not answer; row stays usable) or unavailable (null, no evaluator) |
| `agent-chat-snapshot.schema.json` | Default-agent chat snapshot: rows, active turn, pending approvals, provider status and usage |
| `agent-chat-event.schema.json` | One event on `/v1/agent/default/chat/events`, including `agent.approval.requested` / `agent.approval.resolved` |
| `agent-chat-approvals.schema.json` / `agent-chat-usage.schema.json` / `agent-chat-models.schema.json` | Pending approvals, context/quota usage, and the advertised models with their reasoning efforts |
| `voice-capabilities.schema.json` / `voice-dictation.schema.json` | Voice uplink, Voxtype dictation and level capabilities; one dictation's result |
| `notifications.schema.json` / `notification-action.schema.json` / `notifications-dnd.schema.json` | Mirrored Omarchy notifications (never `execArgv`), one action's result, do-not-disturb |
| `route-descriptor.schema.json` / `route-descriptors.schema.json` | One route / route collection; all four route values are represented |
| `remote-session.schema.json` | The one Remote session: mode, backend, state, one monotonic revision, planned profile, owned output and connection |
| `remote-capabilities.schema.json` | Backends and why each is available, modes, placements, `lock_local_input_supported`, encoder limits |
| `remote-connection.schema.json` | How the client reaches the screen: the Sunshine GameStream identity or the VNC loopback port |
| `remote-bar.schema.json` | Live owned output and host workspaces for the Panel bar |
| `theme.schema.json` | The host's current Omarchy theme: palette, `shell.toml` design tokens, wallpaper digest. Generated from `fixtures/theme/`, a synthetic palette plus Omarchy's own `shell.toml.tpl`, so no machine's colours are pinned here |
| `fonts.schema.json` | The host monospace family, the families fontconfig falls back to for the code points it does not carry, and Omarchy's icon font, each with the digest that is its download `ETag`. Generated from the synthetic files in `fixtures/fonts/` and the fake fontconfig answers in `fixtures/fonts/fc-fallback.json` |
| `herdr-layout.schema.json` | The owned `omodachi` Herdr session as workspaces, tabs and panes. Generated from `fixtures/herdr/snapshot.json`, one recorded `api snapshot` envelope with synthetic ids |
| `events.schema.json` / `wss-envelope.schema.json` | Typed catalog-change/resync payloads and WSS snapshot/ready/event messages |
| `shortcuts.schema.json` | Host keybinding rows: one per record `omarchy-menu-keybindings` publishes, each with the `execution` kind core would replay it with, or the reason it cannot |
| `route-descriptors-menu-actions.json` (fixture) | MENU-4: every route the menu-action adapter gives Omarchy 4.0.3's default menu; `confirm: true` on the A-68 rows |
| `action-result.schema.json` | Prepared/accepted/failed action result; a prepared surface has not executed a command. A shortcut's or a menu action's receipt carries `observed`: the compositor before/after, for `exec` the process core started, and `redirected_output` when a Remote session made a workspace row act on its own output |
| `ipc-request.schema.json` | Known local operations with explicit token; no client actor/shell/Lua/URL/path/argv fields |
| `ipc-envelope.schema.json` | Local response `{ok,result}` / `{ok:false,error,message}` or `{event}` |
| `http-error.schema.json` | HTTPS error `{contract_revision,error:{code,message}}` |

The HTTP API returns the resource itself on success. The local IPC transport
wraps that resource in its response envelope. Consumers must not substitute an
HTTP error for an IPC error or assume that an accepted route has executed.
Data models such as `herdr.json` describe the detailed model embedded by a
resource; they are not a second transport protocol.

The catalog preserves additional upstream/extension metadata fields so that
valid menu metadata is not discarded. Its standard fields and condition/route
shapes are checked. This does not grant extra execution authority: runtime
`RoutePolicy` registration, exact source-action matching and finite parameter
validation remain mandatory. Unknown/native/provider entries stay visible as
unavailable where appropriate. Schema validation alone is not authorization.

From the `omodachi-core` checkout, after installing the project's test
requirements into `.venv`, run:

```sh
.venv/bin/python scripts/verify_contracts.py
.venv/bin/python -m unittest tests.test_contracts -v
```

The verifier uses **jsonschema Draft 2020-12** and a local reference registry;
it validates every JSON fixture, all schema documents and timestamp formats.
It then regenerates each fixture in memory from the actual core serializers,
the JSONC compiler, the catalog runtime and `CoreService`/`Hub`, rejecting drift.
Only synthetic inputs, a fixed clock, and redacted runtime IDs are used. The
working/approved/failed outcomes come from the same model methods as the app.
Schema tests also reject invalid capability shapes, incomplete state and client
execution fields.

To deliberately update examples after an implementation change:

```sh
.venv/bin/python scripts/verify_contracts.py --write
.venv/bin/python -m unittest tests.test_contracts -v
```

`--write` only regenerates known files in `contracts/fixtures/`. Review those
changes together with the changed implementation and schemas. The verifier does
not probe or mutate the user's Omarchy system and does not fetch schema URLs.

The canonical state shape is **flat**: `state.bar` is the semantic `shell.json` layout itself
(`source`, `source_status`, `revision`, `left`, `center`, `right`),
`state.workspace.items` holds workspace state, and `state.focus` holds
sanitized app identity and the opaque focus token. There is no additional
`bar.layout`, `bar.workspaces` or `bar.focus` wrapper. The workspace move
reference is named `move_entry_id`.

Workspace selection and movement remain in the single catalog. The third
Omodachi JSONC layer supplies `omodachi.workspace.select.<n>` and
`omodachi.workspace.move.<n>`; `state.workspace.items` only references those
IDs. A client must not create its own executable workspace action table.
Movement uses the current `state_revision` and `focus.target_token`; it cannot
infer a focused pane or turn a missing token into a target. Unknown occupancy
can remain null. Search responses retain the catalog shape and revision and
may add `query`; the alias `3` resolves through the Workspace 3 catalog row.

`catalog/omarchy-default-v4.0.3.jsonc` is a byte-preserved copy of the locked
Omarchy menu source in `omodachi-web/docs/design-research/source-evidence/omarchy/`;
its adjacent `.meta.json` records `omarchy-v4.0.3` and the source SHA-256. It is a
source fixture: it is never executed and is not the demo catalog.

The primary `catalog.json`/`state.json` examples are generated from the actual
packaged demo bootstrap. `catalog-layered.json` separately exercises the
three-layer override edge case, so its input rows are not mistaken for the full
panel catalog or for a second workspace registry.

Workspace selection does not need an additional context field. Panel topbar
clicks invoke the existing catalog `select_entry_id` with the current catalog
revision and `params:{}`. When Remote is closed, this changes the computer's
workspace without opening Remote or a terminal. When a Remote session is open
the same tap acts on the session's own output: the workspace is pulled to
`session.output_name` and focused there, the way SHORTCUT-1 replays `SUPER+N`,
so the screen the user is looking at is the screen that changes and the physical
one keeps its workspace. (Before ARCH-1 this was refused with
`remote_session_required`, which left the squares dead for the whole session.)
The `state.workspace` snapshot and events remain authoritative
for the selected workspace. This describes user behavior; no separate remote
workspace scope or resolution negotiation schema is introduced.

Selection uses the bounded workspace ID in the catalog and catalog revision.
Moving a focused window additionally requires the current `state_revision` and
`focus.target_token`. Existing stale-source/condition/target checks are retained.
A host action result does not instruct the client to open a terminal or claim
that a media stream was verified. Client navigation is the iOS owner's
responsibility.
