# Device hub and transport

Contract revision: `omodachi.v1`.

The daemon creates a `CoreService`, reads the three menu sources, updates the
read-only default-agent/Herdr state, and shares operations between a real Unix
JSONL listener and an optional HTTPS/WSS listener. `--demo` selects synthetic
adapters and is reported in both readiness output and capabilities. Live mode
reads local Omarchy menu files and the fixed read-only Herdr CLI; it does not
start Herdr or change Omarchy configuration. If the default menu is missing,
only bundled Omodachi entries remain and host graphical state is unavailable.

### Where the socket is

`/run/omodachi/<uid>/omodachid.sock` on an installed host, and
`$XDG_RUNTIME_DIR/omodachi/omodachid.sock` everywhere else. In both cases a
`0700` directory holding a `0600` socket. Nothing passes `--socket` — the unit
does not, the installer does not — because the daemon, the client and the
root-owned `/etc/omodachi/pam.conf` all have to name the same path, and the way
to keep three copies of a value in agreement is not to have three copies.

It used to be `~/.cache/omodachi/omodachid.sock`. AUTH-2 moved it because
`polkit-agent-helper@.service` ships with `ProtectHome=yes`, and AUTH-1's PAM
helper — started by that unit — found no socket, so every polkit prompt fell
back to the password. The surprise is that `ProtectHome=yes` blanks
**`/run/user` as well as `/home` and `/root`**, using systemd's *inaccessible*
mount, under which nothing can be mounted at all: `ReadWritePaths=`,
`BindPaths=` and `BindReadOnlyPaths=` into `/run/user/<uid>/…` are silently
dropped, and `ProtectHome=tmpfs` does not change it. There is no drop-in that
makes a socket in the home or in the runtime directory reachable.
`/run/omodachi` is outside all three, and `ProtectSystem=strict` leaves it
visible and connectable. The evidence is in `tests/pam/sandbox/`.

`/run/omodachi` is root-owned and `0755`, so the user cannot create their own
directory in it; `install_host.py --pam` writes `/etc/tmpfiles.d/omodachi.conf`
to make `/run/omodachi` and `/run/omodachi/<uid>` at every boot, and
`--remove-pam` takes the fragment away again. Without that opt-in root step the
daemon uses `$XDG_RUNTIME_DIR/omodachi/`, which needs nobody's permission and
is exactly as private — it is simply not somewhere a sandboxed helper can be
sent.

While the daemon runs it also keeps `~/.cache/omodachi/omodachid.sock` as a
symlink to the real socket, for one release cycle, so anything still holding
the old path keeps working. The link exists exactly as long as the daemon does.
Nothing that is not ours is replaced: a regular file, a live socket, or another
user's anything at that path means no link is made and the daemon starts
anyway. `omodachid --no-compatibility-symlink` turns it off.

`omodachi-host` tries the three paths newest-first and takes the one that is
actually there, so a client from after the move still finds a daemon from
before it.

Unix IPC is a user-owned socket. Unknown peer UID or another UID is denied;
regular files and live sockets are never removed; the daemon reclaims only its
own socket inode when a connection probe proves nothing is listening on it, so
a `kill -9` cannot lock the daemon out of its own path — and with it out of the
Remote journal recovery that runs at startup.
Each server removes only the socket inode it created. Service shutdown closes
active writers, cancels subscriptions, then waits with a deadline. Both Unix
and network event connections recheck credentials while idle and before events.

The secret and credential-hash registry use private files, process locking,
atomic replacement, and fsync. A running daemon reloads the registry on every
credential check, so a separate local CLI can issue or revoke credentials.
There is no unauthenticated network pairing endpoint.

The hub itself knows nothing about Remote beyond one projection. `RemoteService`
owns the session, serializes every compositor and backend call into a worker
job, expires a session whose heartbeats stopped, and publishes
`remote.session.changed` plus the `remote` and `remote_bar` state. A cancelled
HTTP response never releases the job lock while a host mutation is still
running. `capabilities.desktop` means a Remote backend is available, not that a
frame was ever captured.

Event cursors are the pair `(instance_id, seq)`. Subscription registers before
replay and replays all retained visible events, including more than 100. New
events during replay queue until caught up. Missing history, queue overflow,
future cursors, or a previous process identity require resync. WSS sends a full
snapshot on initial connection or when the process identity differs. A client
must install the snapshot and cursor together before applying later events.

`catalog.changed` is shaped as:

```json
{
  "seq": 7,
  "event_id": "evt_00000007",
  "type": "catalog.changed",
  "device_id": null,
  "payload": {
    "revision": 7,
    "catalog": {
      "contract_revision": "omodachi.v1",
      "revision": "opaque-catalog-hash",
      "source_revision": "opaque-source-hash",
      "entries": []
    }
  },
  "ts": 1789516800.0
}
```

The outer `payload.revision` is an integer state revision. The nested
`payload.catalog.revision` is an opaque string catalog revision. Canonical
fixtures are generated from `CoreService → Hub`, rather than hand-written.

`route.supported` means a registered descriptor is recognized. `route.ready`
additionally checks the current source, executor or relevant capability. A
terminal descriptor does not assert that the client's SSH authentication or
PTY connection has succeeded. Generic terminal preparation does not depend on
Herdr; Agent and Herdr targets have their own readiness. The panoramic Herdr
route requires a server-registered explicit session target. No focused-pane or
focused-session fallback is used.

Supported HTTPS routes are listed in `docs/local-integration.md`. Local IPC
queries return `{ok:true,result:...}`, errors return `{ok:false,error,message}`,
and subscriptions return `{event,instance_id}` after a subscription ack. HTTP
uses raw resource objects and `{contract_revision,error:{code,message}}` errors.
These envelopes intentionally differ and have separate schemas.
