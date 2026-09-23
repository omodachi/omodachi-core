# Catalog and route contract

Contract revision: `omodachi.v1`.

`omodachi_core.catalog.compile_catalog` is the single menu compiler. It reads
three JSONC layers in priority order: Omarchy default, the user's Omarchy
extension, and Omodachi's extension. Each layer is normalized with the same
field defaults as Omarchy's `MenuModel.js`; rows with an existing ID are
shallow-merged in place, while new IDs append in source order. A synthetic
`root` row is added when absent. The same rule applies to all three layers:
an Omodachi row containing only a `surface` therefore clears omitted standard
menu fields to defaults, exactly as a user-layer replacement does. Include the
complete desired standard fields in a surface override. `surface` is Omodachi
metadata; it is not an official upstream field.

`provider` remains data for host evaluation: `CatalogRuntime` resolves it only
through an explicitly registered host callback, replaces provider rows without
overwriting static IDs, and invokes `on_change` for device-event integration.

`when`, `checked` and the optional `disabled` are answered the way Omarchy's
own menu answers them (MENU-3, `conditions.py`). A registered reviewed
adapter answers first, without a subprocess. On a real host (never the demo)
a `ConditionEngine` is attached, and an expression written in one of the three
menu **source** files that no adapter knows is run as `bash -c <expression>`
in the graphical session's environment, 2 s each, at most 8 at once: exit 0 is
`{"status": "available", "value": true}`, any other exit `false`, and a timeout
or a shell that did not start `{"status": "unknown", "value": null, "reason":
"condition_timeout" | "condition_spawn_failed"}`. `unknown` is not
`unavailable`: the row is drawn and can be invoked, and the host decides what
the action does. An expression carried by a provider row is never given to
bash; without an engine (demo, unit tests) an expression with no adapter stays
`unavailable` / `condition_adapter_unavailable`.

Readings are not taken on a clock. Each expression is classified once:
*file* (`[[ -f/-d/-x … ]]`, `compgen -G`, `grep … <files>`, and helpers that
are a file test: the paths are watched with inotify), *package*
(`omarchy-pkg-present`, `omarchy-cmd-present`, `pacman`: the package database
and the command's PATH entries are watched), *demand* (anything else: re-read
when a menu opens - `panel.summon`, `GET /v1/catalog` - if older than 10 s, and
for the invoked row's group after an action), *static* (hardware probes, the
root filesystem type). Idle, the condition subsystem spawns nothing;
`/health.resources.condition_shells_5m` and the journal's `conditions pass=…`
lines count what it does spawn. A true `disabled` makes the route
`ready: false` with `readiness_reason: condition_disabled`.

`surface` is a routing hint. It does not grant execution authority.
`RoutePolicy` must contain a registered adapter before an action can be
invoked (on a real host, the menu-action adapter below is one). Registrations pin the source action and fixed argv and may declare
finite parameter enums. An override that changes a registered action is
rejected as `menu_action_changed`. `register_terminal` accepts reviewed simple
launcher forms, removes the local-window wrapper, and retains fixed argv.
Compound shell syntax is unsupported. The built-in `omodachi.desktop`,
`omodachi.agent`, and `omodachi.herdr` descriptors identify product surfaces;
Agent explicitly targets `default`. A descriptor is not evidence of a live
SSH, Sunshine, or native adapter.

## Menu actions (MENU-4)

On a real host (not the demo), every **menu source** row with an `action` that
no reviewed adapter owns gets a host route from `menu_actions.py`, registered
last so a reviewed adapter keeps its row. It runs the row exactly the way
Omarchy's own menu does - `Menu.qml` `runAction` → `Util.execDetached` →
`Quickshell.execDetached(["bash", "-lc", action])` - with the row's own text,
`bash -lc`, and the Omarchy shell's own environment (the whole of
`/proc/<quickshell>/environ`, minus its unit's variables, with the live
compositor's session keys laid over it; the compositor's environment when no
shell is running). It adds no `uwsm-app`: the rows that want one say so in
their text. The one addition is a transient `systemd-run --user --scope`, so the
row's process does not live in `omodachid.service`'s cgroup and die with it.

* The route is `{"route": "host", "supported": true, "argv":
  ["omodachi-menu-action", "<entry_id>"]}`: a marker, like
  `omodachi-keybinding`. The route has no parameter enums, so an invocation
  with any `params` is refused; the text that runs is re-read from the source
  catalog at execution time and must still be the text the route was
  registered for. Provider rows (apps, fonts, keybinding records) are never
  registered.
* The receipt carries `observed` in the SHORTCUT-1 `exec` shape:
  `activeworkspace`/`activewindow` before and after, and the process
  (`pid`, `exited`, `exit_code`) after a 0.1 s settle. Exit 126/127 is a failed
  receipt with `executable_missing`; no graphical session is
  `graphical_session_unavailable`.
* Each invocation is one journal line, `{"omodachi":"menu_action",
  "entry_id", "device", "at", "confirm", "status", "pid", "exited",
  "exit_code", "ms"}`. The action text is never logged.
* `route.confirm: true` (Study 04 A-68) marks a row that changes the machine:
  `system.{lock,suspend,hibernate,logout,reboot,shutdown}`, `setup.reset`,
  every `remove.*`, every `update.*` except the host picker
  (`omarchy-menu-timezone`) and the night-light restart
  (`omarchy-restart-hyprsunset`), and any row whose action runs a power or
  session verb (`systemctl suspend`, `loginctl terminate-*` …), `pacman -R`,
  `rm -r…`, `sudo`/`pkexec`, an `omarchy-remove-*` / `omarchy-refresh-*` /
  `omarchy-setup-security-*` script, a service restart that takes audio,
  network or input away, an update, a credential or boot change. The client
  asks for a second tap; core does not enforce it.
* A row that cannot be run stays `ready: false` with a reason: an empty action
  is `menu_action_empty`; a bare command that reads a terminal (`passwd`,
  `gum`, an editor …) with no terminal wrapper around it is
  `menu_action_needs_terminal`. A submenu row is `menu_row_not_invocable`, and
  a keybinding row the host publishes without a dispatcher repeats its own
  `binding_adapter_unavailable`. `route adapter is not registered` now only
  means nothing has looked at the row (the demo, unit tests).

`route-descriptors-menu-actions.json` is every route the adapter gives
Omarchy 4.0.3's default menu; `action-accepted-menu-action.json` is one row run
through it with a fake session.

Script `# omarchy:*` annotations provide help and bounded argument validation.
They do not add rows or create a second command catalog.

## Invoking a row (PERF-5)

`catalog_revision` on an invocation is a **hint, not a gate**. It says which
catalog the client was looking at; it does not have to be the one the host is
looking at now, and a mismatch is not a refusal. The host keeps a catalog on
the shelf (the maintenance tick rebuilds it every two seconds) and answers in
this order:

1. The shelf has the id and the revision matches: run it. Nothing is re-read.
   This is the ordinary tap and it costs no subprocess.
2. The revision moved, or the shelf does not have the id: re-read **that row's
   own sources** - the menu source stamps, the row's `when`/`checked`, and the
   provider listing it came out of - and resolve the id again in the result.
   `CatalogRuntime.invalidate_row` is that narrowing; an id the snapshot does
   not have at all falls back to the whole table.
3. The id is still there and still means what it meant: run it. The receipt
   carries `catalog_revision`, the revision it was actually resolved against,
   so the client adopts it rather than staying one behind.
4. The id is gone and the revision had moved: `stale_catalog_revision` (409).
   An id that is simply not in the current catalog, with a revision that
   matches it, is `route_unavailable` (404) - the client is asking for
   something that was never there.
5. The id is there but has been redefined under the client - the source action,
   the pinned source fields, or the declared surface changed - `stale_target`
   (409). Nothing is dispatched.

Before PERF-5 every invocation re-read the whole table first (all provider
listings, every shell condition) and refused any revision mismatch with
`stale_catalog_revision`. That read is what made a tap cost 175 ms of
subprocess on a healthy host and the better part of two seconds on a loaded
one, and it is why the same tap was sometimes instant and sometimes not. What
it protected - never running a row the host has removed or redefined - is kept
by the re-resolution above plus the adapters' own identity checks at the moment
of execution: the apps provider re-reads the desktop entry it is about to
launch, and the keybinding provider re-reads the records and looks the binding
up by a reference that is a digest of the whole record. Those refusals arrive
as a failed receipt (`code`) rather than an HTTP error, because by then the
request id has already been accepted and a retry must not run twice.

The complete rendered snapshot `contracts/fixtures/catalog.json` and
`route-descriptors.json` are safe for web/iOS/plugin mocks. They contain no
credentials or live host state. The demo has no condition engine, so its
unknown conditions remain unavailable.

Validation from this checkout:

```sh
PYTHONPATH=src .venv/bin/python -m unittest tests.test_catalog_routes tests.test_contracts
.venv/bin/python scripts/verify_contracts.py
```

The verifier walks every JSON fixture, checks that all four route surfaces are
represented, resolves schema references locally and rejects fixture drift
against the current serializers. `--write` regenerates the examples from fixed
synthetic inputs and a fixed clock. See `contracts/README.md` for the
resource/envelope distinction.

Bar layout is a flat semantic projection of `shell.json`; workspace and focus
state are their own top-level state fields. Workspace rows reference
`omodachi.workspace.select.<n>` and `omodachi.workspace.move.<n>` from the third
JSONC source through `select_entry_id` and `move_entry_id`. The catalog source
owns the aliases used by search. Clients render these references and do not
synthesize a private action registry.
