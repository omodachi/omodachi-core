# Pairing and the pinned host certificate

Contract revision: `omodachi.v1`.

Omodachi's trust model is the SSH one. The host owns a long-lived self-signed
certificate. Pairing hands the companion that certificate's fingerprint at the
same moment it hands over a device credential, and the companion pins it. There
is no CA, no installed configuration profile and no certificate the user has to
approve in Settings.

One approval covers three things: **screen, terminal and agent**. The same local
Approve issues the device credential, grants the Sunshine streaming certificate
and writes the companion's SSH public key to `authorized_keys`. There is no
second approval anywhere, no public key to copy by hand and no SSH host
fingerprint to compare.

And it is the *only* thing the user does. By default pairing needs no invitation
code: the companion asks, the computer approves once, and that is the handshake.
The security boundary has always been that local approval — a same-UID decision
the network cannot make — so the invitation was never what kept a stranger out.
It survives as `pairing_mode = invite`, an administrator's lock, described below.

## The host certificate

`install_host.py` generates `~/.config/omodachi/tls/server.pem` and
`server.key` once, `0600`, if they do not already exist:

- self-signed, ECDSA P-256, valid ten years;
- `CN=<hostname>`;
- SAN covering `<hostname>`, `<hostname>.local` and every non-loopback address
  the host holds at install time, so reaching the host by address, by mDNS name
  or over Tailscale all present a matching certificate.

An existing pair is never replaced by an install. The daemon serves it with
`--tls-cert`/`--tls-key`; the unit listens on `0.0.0.0:8099`.

`~/.config/omodachi/host-id` holds a random 128-bit installation identity,
created on first start. It is independent of the key, so rotating the
certificate does not change which host a companion thinks it paired with.

### Rotation

```sh
omodachi-host tls show      # the current fingerprint and advertised addresses
omodachi-host tls rotate    # new certificate, restart omodachid, print the new fingerprint
```

Rotation is deliberate and manual; v1 has no automatic rotation. Every paired
client pinned the old fingerprint, so after a rotation each one hard-fails to
connect until the user explicitly trusts the new certificate in the app.

## The anchor: `GET /health`

`/health` needs no credential and carries no device data:

```json
{
  "contract_revision": "omodachi.v1",
  "service": "omodachid",
  "sunshine_required": false,
  "host_id": "…32 hex…",
  "tls_fingerprint_sha256": "…64 hex…",
  "pairing": {"mode": "open"}
}
```

This is an anchor, not authorization. Anyone on the LAN can read it, and a
malicious peer can serve a document that looks exactly like it. Reading the
fingerprint here establishes nothing; only a successful claim does.

`pairing.mode` is one word, and it is the only thing about pairing this document
says: `open` means a request needs no invitation, `invite` means this host has
been locked and will refuse one without. A companion uses it to label its host
list — "unpaired" or "needs an invitation" — before anybody taps anything.

Both identity fields are `null` on a daemon started without an installed host
identity (the loopback development path); `pairing.mode` always answers.

## The handshake

Three steps. Nothing is copied by hand:

1. The companion taps the host in its list and posts `POST /v1/pairing/requests`
   with its `device_id`, `device_name` and optionally its `ssh_public_key` — one
   line of OpenSSH, validated on arrival, refused with `invalid_request` when
   `sshd` could not parse it. No `invitation` field. This is the only
   unauthenticated write on the API, and the request becomes `pending` with a
   300 s TTL counted from here.
2. The user approves on the computer: the plugin's Devices page shows the card
   (device name, source address, the first twelve characters of the SSH
   fingerprint and what the one approval grants) and a desktop notification
   carries the same thing; `omodachi-host pair pending` and
   `omodachi-host pair approve <request_id>` are the same decision from a
   terminal. **Approval is a local, same-UID decision. The network can never
   make it**, which is why step 1 does not need a secret to be safe.
   The approval is where the other two grants land: it calls `media-pairing
   grant-remote`, and when the request carried a key it calls the same code
   path as `omodachi-host ssh authorize --device <device_id>`, writing exactly
   one line marked `# omodachi:<device>`. Neither failure hides: each is
   reported in the approve result and recorded on the request, so the claim
   reports what actually happened.

   **Streaming is not a flag.** Since PAIR-3 one approval means screen,
   terminal and agent, and `remote` defaults to true — `--remote` is still
   accepted and does nothing. `--no-remote` is the one way to approve the
   companion credential alone. Making it opt-in is what produced a device with
   `grants: {companion: true, media: false, ssh: true}`: paired in the panel,
   unpaired for Remote, with nothing anywhere saying which. A grant that cannot
   be made never fails the approval; the result carries `remote.media_authorized:
   false` with a `remote.reason`, and `grants.media` stays false.

### Repairing a half-paired device

`omodachi-host devices list` carries `media_authorized` and the
`source_request_id` the grant can be replayed from, so "companion yes,
streaming no" is visible on the computer rather than only on the iPad:

```sh
omodachi-host media-pairing grant-remote <device_id> <source_request_id>
```

The approval this names is kept for as long as the device is: a **claimed**
request is no longer pruned at the 300 s TTL (only unclaimed ones are), because
it is the record of what the device was granted, not a queue entry. A revoke
drops it along with the credential, the certificate and the `authorized_keys`
line.
3. The companion posts `POST /v1/pairing/requests/{request_id}/claim` with its
   `request_secret` and receives the device credential. It is now in the panel.

### What bounds step 1

An invitation-less request is the one thing an unauthenticated peer can create,
so the store bounds it and reports the source:

- one pending request per `device_id` — the same device asking again **replaces**
  its own pending request, and the old `request_secret` stops working. Tapping
  retry never queues a second row for somebody to wade through;
- at most two pending requests per source address (`429
  pairing_source_capacity`);
- the store-wide maximum of 64 pending requests, as before;
- expired requests are dropped whenever the store is read or written;
- `remote_addr` is recorded and shown in `pair pending` and on the plugin's card.
  It is context for the person deciding, never authorization.

None of these can approve anything. The only thing they bound is how much a
stranger on the LAN can put in front of the person at the keyboard.

### Locked mode: `pairing_mode = invite`

```sh
omodachi-host preferences get                                  # read `revision`
omodachi-host preferences set --revision <n> --pairing-mode invite
```

In `invite` mode a request without an `invitation` is refused with
`403 pairing_invitation_required`, and the handshake regains its old first step:

```sh
omodachi-host pair begin      # a single-use invitation, 300 s TTL
```

The companion then posts that invitation with its request. Invitations work in
both modes — they are still single-use, still 300 s, still consumed by the
request that redeems them — so a host can be locked without anything else
changing. The app shows its invitation field only after a host has answered
`pairing_invitation_required`; on the default path no user ever sees one.

## What the claim adds

The claim response carries the host identity the client pins:

```json
{
  "contract_revision": "omodachi.v1",
  "request_id": "pair_…",
  "device_id": "ipad",
  "device_name": "Leo's iPad",
  "status": "claimed",
  "expires_at": 1789430700,
  "credential": "…",
  "issued_at": 1789430400,
  "credential_expires_at": 1792022400,
  "ssh_public_key": "ssh-ed25519 AAAA…",
  "ssh_fingerprint": "SHA256:…",
  "grants": {"companion": true, "media": true, "ssh": true},
  "remote_addr": "192.168.1.23",
  "ssh": {"user": "alex", "host": "192.168.1.10", "port": 22},
  "host_id": "…32 hex…",
  "host_name": "omarchy",
  "tls_fingerprint_sha256": "…64 hex…",
  "endpoints": [{"host": "192.168.1.10", "port": 8099}]
}
```

`grants` is what the one approval actually landed, not what it promised:
`companion` is this credential, `media` the Sunshine streaming certificate,
`ssh` the `authorized_keys` line. A client shows the terminal only when
`grants.ssh` is true, and `ssh` is where that terminal lives — no user ever
types a host, a port or a username for it. `ssh_public_key` echoes the key the
request carried so a client can tell which of its keys this host now trusts.

`endpoints` is every non-loopback address the daemon currently answers on with
its bound port; a client stores them as reconnection candidates alongside
whatever `_omodachi._tcp` discovery later reports.

A claim against a request that is approved but not yet decided returns the same
shape without the credential fields, so a client polling a claim already knows
which host it is talking to.

## The key a device offers can change without re-pairing (UX-4)

`PUT /v1/ssh/key` · authenticated · body `{"public_key": "ssh-ed25519 AAAA… "}`

The key in the pairing request is the key at the moment of pairing, and a
companion can outlive it. On iOS the Keychain survives the app being deleted
and the app container does not, so a reinstalled companion comes back holding
its credential — authenticated, trusted, already paired — and a *different*
SSH private key, whose public half this host has never seen. Every connection
then fails `publickey`, and the one repair the client could offer, pairing
again, is the one thing an already-paired device does not think to do.

So an authenticated device may state the key it is holding now:

```json
{"contract_revision": "omodachi.v1", "device_id": "ios-…", "authorized": true,
 "changed": true, "reason": "replaced", "fingerprint": "SHA256:…",
 "removed": 1, "replaced": ["SHA256:…"]}
```

Three things this is not:

- It is not a second way to authorize a device. The authority is the credential
  on the request; `device_id` is never read from the body, so a credential can
  only ever rewrite **its own** `# omodachi:<device>` line.
- It is not an append. Every line marked for that device goes and one takes
  their place, so a device has exactly one key and a revoke takes back exactly
  what is open. `replaced` names the fingerprints that stopped working.
- It is not a takeover. A key body already sitting on a line this host does not
  own, or on another device's line, is refused (`409 public_key_not_owned` /
  `409 public_key_owned_by_other_device`) rather than adopted.

`GET /v1/ssh/key` answers the same device with an inventory and no key:

```json
{"contract_revision": "omodachi.v1", "device_id": "ios-…", "authorized": true,
 "fingerprint": "SHA256:…", "fingerprints": ["SHA256:…"]}
```

That is how a client finds out its private half and the host's line have come
apart *before* the terminal fails: `omodachi-host ssh list` is a local
administrator command and the claim only echoes the key the request carried, so
until now a companion had no way to see it. `fingerprints` is oldest first and
more than one entry is a drift; `fingerprint` is the newest of them.

The replacement is recorded on the daemon's stdout, which systemd captures:
`{"omodachi":"ssh","event":"ssh.key.replaced","device_id":"…","fingerprint":"SHA256:…","reason":"replaced","removed":1,"replaced":["SHA256:…"]}`.

`omodachi-host devices list` carries `ssh_keys` and `ssh_key_duplicates` per
row, and `omodachi-host ssh list --prune` (optionally `--device <id>`) deletes
the older lines of any device holding more than one, keeping the newest — which
for the lines this module owns is the last one in the file, because both
`authorize` and the replacement above append. A device with one key is never
touched.

## Client rules

- Pin `tls_fingerprint_sha256` at the instant the claim returns a credential,
  together with `host_id`. Store `ssh` as the terminal's target in the same
  step: the claim is the only place it is published.
- On every later connection, compare the presented certificate's DER SHA-256 to
  the pinned value. A mismatch is a hard failure: do not connect, do not fall
  back, do not offer "continue anyway". Present it as "this host's certificate
  changed" and require the user to trust the new one explicitly — which is what
  `omodachi-host tls rotate` produces.
- Revoking is one action too. `omodachi-host devices revoke <device_id>` takes
  back the credential, the Sunshine certificate and the `authorized_keys` line
  in the same call; a client's "forget this host" only clears its own keychain.
  `omodachi-host devices list` marks each row `role`: `companion` for a paired
  device, `plugin` for this host's own panel credential (`com.omodachi.host`).
  Revoking the plugin's own row would blank the page a user would undo it from,
  so it answers `409 plugin_credential` unless `--force` is given, and the
  Devices page draws that row without a Remove button.

## What a revoke actually takes back (PLUG-4)

The managed fork can revoke a certificate since PLUG-4 (`pairing.revoke`,
`omodachi-sunshine/docs/IPC-v3.md`). Until then it could not, and that was both
clutter and a hole: every revoke left the record `revocation_pending` **and**
left the certificate authorized in Sunshine, so a revoked device could still
pair straight to the GameStream port, bypassing core.

- `devices revoke` now asks the fork to drop the certificate. When it does, the
  binding and the attempt are removed - no `revoked` tombstone - and the device
  leaves the credential registry entirely, as if purged. When the fork cannot
  (an older build), nothing changes: the record stays `revocation_pending`, the
  reply carries `certificate_revocation_supported: false`, and the row survives.
- The records left behind by every revoke made before this are caught up, not
  re-decided: `media-pairing pending` and the daemon's own maintenance tick
  retry them until the fork confirms. `pairing.revoke` answering `not_found` is
  a completed revocation - the fork does not authorize that certificate either
  way.
- `devices list` answers what a user can act on: authorized devices, this
  host's own panel credential, and anything with a request still waiting.
  `revoked_hidden` counts what it left out. `devices list --all` is the whole
  registry.
- `omodachi-host media-pairing certificates [--purge-unknown]` lists what the
  fork actually authorizes beside what this host knows: each row carries the
  certificate's fingerprint, the fork's display name, and the `device_id` of
  the binding that claims it, or `known: false` when nothing here does.
  `--purge-unknown` revokes only the unclaimed ones. A certificate core knows
  is never touched by it - taking that one back is `devices revoke`, which also
  takes back the credential behind it. This exists because three certificates
  older than managed pairing sat in the fork's store with no core record at
  all: no `devices revoke` could name them, and each was still good for a
  direct stream. `unreadable` counts records whose stored certificate does not
  parse, which therefore have no fingerprint to revoke by.
- `devices purge [--older-than DAYS]` removes revoked devices: credential
  hashes, display name, media permission row, bindings whose certificate the
  fork has really taken back, and terminal attempts. Anything unresolved keeps
  its device, and the reply says which and why. `--older-than` measures the
  media permission row's timestamp, the only per-device time the host keeps; a
  device with no such row is never purged by age. An authorized device and the
  plugin's own credential are never purged.
- A host reachable at a new address but presenting the pinned fingerprint and
  `host_id` is the same host; trust it and update the stored endpoint.
- Discovery (`docs/lan-discovery.md`) never establishes this trust. Its `fp`
  TXT record is a 16-hex prefix used only to tell two hosts apart in a list.

The declared shapes are `contracts/health.schema.json` and
`contracts/pairing-claim.schema.json`, with generated examples in
`contracts/fixtures/health.json` and `contracts/fixtures/pairing-claim.json`.

## A credential's life: expiry, renewal and why a 401 happened (CORE-2)

A device credential lives **30 days** from `issued_at` (`credential_expires_at`
in the claim). Nothing about it is silent any more:

- **Every refused credential says why.** A 401 `permission_denied` for a bearer
  token carries `error.reason` (`http-error.schema.json`):

  | `reason` | What happened | What the app does |
  | --- | --- | --- |
  | `credential_expired` | past its 30 days, or past the grace period after it was renewed | one ordinary pairing request, one Approve on the computer |
  | `credential_revoked` | `devices revoke` on the computer, and the record is still there | says so and goes back to the host list |
  | `device_purged` | revoked and then removed from the registry (a normal revoke on a host whose fork can take the certificate back) | the same as revoked |
  | `unknown_credential` | not a token this host signed: another installation's, or a damaged one | the same cleanup PAIR-5 always did |

  The event stream closes with 1008 and the message
  `credential revoked or expired: <reason>`, the old words first.
  A missing `Authorization` header is still `pairing_required` with no reason.

- **`GET /v1/pairing/credential`** (`pairing-credential.schema.json`) tells the
  caller about its own credential: `issued_at`, `expires_at`, `renewable_at`,
  `renewable`, and `superseded` once it has been traded in.

- **`POST /v1/pairing/renew`** with body `{}` (`pairing-renew.schema.json`)
  trades a credential in its **last 7 days** for a new 30-day one. The one that
  was presented keeps working for **24 h** (`previous_credential_expires_at`),
  so a reply lost on the way back costs nothing: the device still holds a
  working credential and asks again; every other live credential of the device
  gets the same 24 h, so a device ends up holding exactly one. Refusals:
  401 with `reason` for revoked / purged / expired / unknown (renewal is never a
  way back in), `409 credential_renewal_not_due` with `detail.expires_at` and
  `detail.renewable_at` before the window, `409 plugin_credential` for the
  panel's own credential.

- **`devices list`** rows carry `expires_at` and `expired_credentials`. An
  expired credential is no longer counted in `active_credentials`, a device
  whose only credentials have run out is `status: "expired"` (still listed, it
  is something to act on), and such a device can pair again - before CORE-2 the
  expired hash still counted as active and the request was refused with
  `409 pairing_device_exists`.

- A registry written before CORE-2 does not know when its credentials were
  issued. The `iat` is inside every token, so the host records it the first
  time each token comes back; until then `expires_at` is `null`. Nothing about
  the credential itself changes.

- **The panel's own credential** (`com.omodachi.host`, `~/.config/omodachi/plugin.token`)
  has no TTL expiry on the same-UID Unix socket, where the peer's uid is the real
  authority and nothing could renew it. Presented over HTTPS it ages like any other.

- `omodachid --credential-ttl SECONDS --credential-grace SECONDS` exist for
  walking the whole lifecycle in minutes on a throwaway daemon. They are test
  switches, not preferences; the installed unit never passes them.
