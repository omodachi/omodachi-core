# Remote to managed Sunshine control contract

Core drives the managed Sunshine fork through its private control IPC. The fork
owns capture, encoding and the GameStream connection; core owns the session, the
owned headless output and the recovery journal.

Transport: a same-UID Unix JSONL endpoint at
`$XDG_RUNTIME_DIR/omodachi-sunshine/pairing.sock`. One request and one response
per connection; at most 4096 request bytes and 32768 response bytes, with a
two-second deadline. No network management password or PIN. The parent
directory is `0700` and the endpoint `0600`; the consumer checks the peer's Unix
UID and, on Linux, its process `comm`. Implementation:
`remote/sunshine.py` (framing) and `remote/backends.py` (`SunshineBackend`).

## Observation operations, consumed as they are

- `{"op":"media.list"}` → `{"ok":true,"sessions":[...]}`
- `{"op":"media.get","session_id":"..."}` → `{"ok":true,"session":{...}}`

Core no longer reads these on the session path: the fork's own `desktop.session`
result is the authority on whether a stream is running, and decoded-frame
correlation was part of the deleted evidence machinery. They remain available
for diagnostics.

## The control protocol

Control requests carry `protocol:"omodachi.sunshine.desktop.v1"`; responses carry
the same version, `ok`, and either `result` or `error.code`. A pairing-only or
observation-only endpoint is not a usable control provider.

Every control operation except `desktop.status` carries this **exact** lease
object — the fork validates the field set, not just the values:

```json
{"lease_id":"rs_<32 hex>","lease_epoch":1,"owner_device_id":"<device>",
 "client_cert_sha256":"<64 lowercase hex>"}
```

`lease_id` is the Remote session ID. `lease_epoch` is always `1`: a session ID is
never reused, so the fork's epoch has nothing left to distinguish. The
fingerprint is the one an actual Moonlight pairing produced for this device
(`media_pairing.paired_certificate`); without it core refuses the session before
it touches the compositor. Claim is a fingerprint reservation, not a media
pairing and not proof of a connection.

`desktop.prepare` carries this **exact** identity object:

```json
{"lease_id":"rs_<32 hex>","lease_epoch":1,"transition_id":"t<32 hex>",
 "geometry_epoch":<revision>,"connection_generation":<revision>}
```

Core has one monotonic session `revision`, and both of the fork's generation
counters are that revision, so the fork's rule — a changed identity must
increase both — is satisfied by the revision increasing. `transition_id` is a
fresh opaque ID per prepare. **These four field names exist in core only here**,
in the two functions that build the fork's wire objects; the session model has
no epochs or generations. The fork's own `generation` and `capture_generation`
are separate counters and are never assigned from this identity.

| Operation | Additional request fields | What core requires of the result |
|---|---|---|
| `desktop.status` | none; no lease | `available`, `backend`, `encoder`. Capability, not frame proof. |
| `desktop.claim` | `output_id` | Reserves the exact paired client and session. Idempotent for the same lease; refuses unrelated active or pending streams. Changes no output and starts no capture. |
| `desktop.prepare` | `identity`, `output_id`, `profile` | `configured_output_id` equal to our output, `session_count:0`, `prepared:true`. An in-memory next-launch target; no user Sunshine configuration is written. |
| `desktop.session` | none | `stopped` and `session_count`, used to confirm a real stop. |
| `desktop.stop` | none | `stopped:true` and `session_count:0`, and the same again from a following `desktop.session`, before core calls the stop complete. Only the reserved session is stopped; there is no global stop. |
| `desktop.release` | none | `released:true`. Requires the session to be stopped. |

## Recovery

Core's journal is the intent record. On release, timeout, daemon restart or
`omodachi-host remote recover`, the backend step reconfirms the **same** lease
with `desktop.claim` before stopping, so a fork restart or a lost response
cannot strand the output. If that claim answers `desktop_busy`, the fork holds
an unrelated owner and therefore holds nothing of ours: core stops nothing and
moves on to restoring the compositor. Core never fabricates a successful lease
or capture, and never stops another client's session.

A stop that reports `session_not_stopped` leaves the session owned; core reports
the step as failed, keeps the journal, and finishes the rest of the restore so
the physical screen is never left dark by a stuck encoder.

The fork's own contract is `omodachi-sunshine/docs/IPC-v3.md`; its validation is
in `src/managed_desktop_control.cpp`.

## Where the managed fork is installed, and why it matters

The fork's asset root is a **build input**, not a runtime path:
`SUNSHINE_ASSETS_DIR` is compiled in at configure time
(`cmake/compile_definitions/common.cmake`), and `graphics.cpp` derives
`SUNSHINE_SHADERS_DIR` from it. Moving an already built binary to another
directory does not move that string, so the expected layout is one per-commit
directory holding both:

```
~/.local/share/omodachi/sunshine/<sha>/
├── sunshine          # configured with -DSUNSHINE_ASSETS_DIR=<this dir>/assets
└── assets/
    ├── shaders/opengl/{ConvertUV,ConvertY,Scene}.{frag,vert}
    └── shaders/vulkan/rgb2yuv.comp
```

and the unit drop-in
`~/.config/systemd/user/app-dev.lizardbyte.app.Sunshine.service.d/60-omodachi.conf`
pointing `ExecStart` at that exact binary. The drop-in is the user's file;
core reads the unit's resolved `ExecStart` and never writes it.

Without the asset tree the fork does not fail: it logs one shader compile error
per file and falls back to software encoding, so the stream works and the
encoder quietly becomes `libx264` (PERF-3). Core therefore probes
`<dirname(ExecStart)>/assets/shaders/opengl` and reports
`reason: "sunshine_assets_missing"` on the `sunshine` backend in
`GET /v1/remote/capabilities` — alongside `available: true`, because a
degraded stream is still a stream. A unit core cannot read leaves the reason
`null`; not being able to look is not the same as it being missing.
`scripts/install_host.py` prints the same finding at install time. Neither
builds the fork; that belongs to the release spec.
