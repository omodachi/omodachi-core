# Existing-lease microphone WebSocket transport

This transport reuses the existing Bearer middleware and Desktop control lease.
It adds no identity, certificate, pairing, device permission or Settings protocol.
A fixed existing-display lease is sufficient: no adaptive coordinator/session
needs to exist. The native client asks for OS microphone permission only after
an explicit user action, connects, sends begin, and waits for `begun` before
starting AVAudioEngine capture.

## Supported, available and active are different

Authenticated `GET /v1/audio/input` returns the existing `contract_revision` and:

```json
{
  "supported": true,
  "available": true,
  "active": false,
  "transport": "websocket",
  "endpoint": "/v1/remote/sessions/{session_id}/audio",
  "format": "s16le",
  "rate": 48000,
  "channels": 1,
  "frame_samples": 960,
  "frame_bytes": 1920,
  "max_queued_frames": 3,
  "reason": "ready"
}
```

The example shows an installed and reachable backend. The normal daemon installs
`PulseVirtualInputFactory`; demo mode installs no real audio backend. Its probe
checks Linux, `pactl`/`pacat`, an existing same-UID user Pulse socket, and a bounded
server-info call directed at that existing socket. Info output is discarded:
no physical source/default-route information is retained. Probe and construction
do not load audio modules or launch playback. Probes run off the event loop and
are briefly cached. Missing dependencies/server yield `supported:false`,
`available:false`, with `audio_backend_not_installed` or `audio_backend_unavailable`.
Availability no longer waits for a native connection to exist.

`active` is separate and scoped to the authenticated device. It becomes true
only after that device explicitly began a live channel. Incomplete owned cleanup
sets `available:false,active:false,reason:audio_input_cleanup_pending`; owned
cleanup state remains retained for retry.

## Exact wire contract

Connect to `WSS /v1/remote/sessions/{session_id}/audio` with the existing Authorization
header. Missing credentials, another lease owner, an expired lease or unavailable
backend is rejected before upgrade. Opening an idle socket creates no sink.
Query/body fields are not accepted on upgrade. A begin must arrive within ten
seconds. Idle sessions recheck credential/lease validity every 250 milliseconds.

The first TEXT frame contains exactly:

```json
{"generation":1,"format":"s16le","rate":48000,"channels":1,"frame_samples":960}
```

Only this explicit begin creates the session-owned virtual microphone. Generation
is a positive integer below 2^53, increasing on each microphone enable, route
reset or reconnect within the same lease. It is a microphone channel generation,
not the adaptive media connection generation. The server creates its own channel
handle and binds it to owner, lease ID, lease epoch and microphone generation.
A stale generation cannot attach, and concurrent channels cannot take over an
active channel.

After backend creation and a second lease/credential check, the server replies:

```json
{"type":"begun","generation":1,"format":"s16le","rate":48000,"channels":1,"frame_samples":960,"frame_bytes":1920,"max_queued_frames":3}
```

Each subsequent BINARY frame is exactly 1,920 bytes: 960 signed little-endian
16-bit mono samples at 48 kHz (20 ms). Binary messages are not JSON/base64.
Every binary message admitted by the WebSocket parser receives one small ack:

```json
{"type":"accepted","generation":1,"sequence":1}
```

When the bounded backend queue is full:

```json
{"type":"rejected","generation":1,"sequence":2,"reason":"audio_input_backpressure"}
```

That backpressure rejection is recoverable and the channel stays open. It does
not imply microphone permission failed. Accepted means queued for the owned
backend, not heard/recorded by a host application. The backend queue contains at
most three frames and enqueue does not wait for playback. No PCM is persisted
or included in responses/logs.

`sequence` is strictly the channel-local **wire receive ordinal**, starting at
one and advancing for rejected binary messages too. It is not a capture sequence:
client frames dropped before WebSocket.send do not consume it. Native keeps a
FIFO mapping for at most three sent/unacked frames; both accepted and rejected
acks consume one ordinal. An ack timeout or mismatched ordinal closes the channel
instead of guessing which captured frame was acknowledged.

TEXT end:

```json
{"type":"end","generation":1,"reason":"user_disabled"}
```

Optional reason is `user_disabled`, `route_changed`, `session_end` or
`disconnected`; omission means `session_end`. After confirmed cleanup, reply:

```json
{"type":"ended","generation":1}
```

The server then closes normally. Wrong generation, owner, frame size, malformed
JSON, duplicate/unknown fields, wrong format or binary-before-begin are rejected
with a stable reason and policy close. A 1,921-byte frame receives rejected ack;
messages exceeding the bounded 4 KiB WebSocket parser limit close with 1009
before application acknowledgement. Native must treat that close as terminal.

The earlier unshipped JSON/base64 audio begin/frame/end routes are superseded by
this single WebSocket ingress; they are not installed.

## Cleanup and concurrency

`AudioUplinkManager` owns a separate serialized audio lifecycle lock, so desktop
release does not reacquire its own display lock while ending audio. It also owns
audio for fixed-display leases without creating placeholder display resources.
End/detach check owner and generation before touching the active reference.
Server cleanup after disconnect uses the exact server-held channel handle;
an old socket cannot close a replacement channel's newer generation.

End, route change, WebSocket disconnect, lease expiry/release, credential loss,
recovery and last-transport shutdown stop microphone input and clean up only
owned modules. Closing a separate IPC transport does not end a still-live network
channel. Failed cleanup retains its backend IDs/session, closes input, refuses
new input and retries via the ordinary lifecycle watchdog. It is not reported as
successful closure.

The backend module owner implements nonce-scoped names and owned-ID readback.
No physical source, default sink, default source, loopback, host output or DPMS
is changed by this transport.

## Validation

`tests/test_audio_websocket.py` runs real temporary aiohttp HTTP/WebSocket and
Unix transports with an injected fixed identity and bounded synthetic backends.
It covers fixed-display lease operation, capability and idle no-sink behavior,
wire format and ordinals, local drops followed by an accepted ack, bounds,
foreign and stale requests, idle expiry, release without lock reentry, shared
transport lifetime, cleanup retry and schema validation. A real
`VirtualMicrophoneSession` with synthetic `pactl` metadata writes PCM frames
through an anonymous pipe and the test verifies owned reverse-order unload.

Machine-readable wire definitions are in `contracts/audio-input.schema.json`.
