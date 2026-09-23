# WayVNC remote backend

WayVNC is the low-overhead alternative to the managed Sunshine fork. It reuses
the same owned headless output and the same session lifecycle; only the
transport and the frame source differ. Implementation: `remote/vnc.py`
(`ManagedWayVNC`) and `remote/backends.py` (`VncBackend`).

## Choosing it

`POST /v1/remote/sessions` takes an explicit `backend` of `sunshine` or `vnc`,
and `POST /v1/remote/sessions/{id}/backend` changes it mid-session without
destroying the output or the applications on it: the old backend is stopped and
released, the same profile is re-applied and the new backend is prepared. A
failure to start the new backend is explicit — there is no automatic fallback.
An explicit choice is remembered in `~/.config/omodachi/remote-backend.json`.

`GET /v1/remote/capabilities` reports `vnc.available` from the fixed WayVNC
0.10.1 binary check, plus `transport: "wss"` and `audio: false`; there
is no RFB audio. `scripts/install_wayvnc.py` installs the dependency through the
distribution package manager only when `--install` is given and the exact
repository version is available; it never starts capture. `--check` is read-only.

## Connection

```json
{"backend":"vnc","transport":"wss","path":"/v1/remote/sessions/rs_…/vnc",
 "output_id":"OMODACHI-…",
 "initial_framebuffer_pixels":{"width":1280,"height":894},
 "framebuffer_pixels":{"width":2560,"height":1788}}
```

VNC geometry is the full output mode, so for this backend the planned profile's
`stream_pixels` is set to `output_mode_pixels`: WayVNC serves the framebuffer
itself rather than an encoded stream sized to a quality budget. A quality change
therefore moves only the fps and bitrate caps.

The client's `decoder` limits describe its H.264 decoder and do not bound this
framebuffer, because nothing on this leg decodes H.264.

### WayVNC has two framebuffer sizes, and both are on the document

WayVNC 0.10.1 shows one client **two** framebuffer sizes. Measured against the
real host on a 2560×1764 scale-2 owned output, with a client that advertises
`NewFBSize`:

```
ServerInit       : 1280x882            <- the compositor's logical size
update 0: 1 rect : 1280x882   raw
update 1: 2 rects: 2560x1764  NewFBSize <- corrected to the buffer pixels
                   2560x1764  raw
update 2: 1 rect : 2560x1764  raw
```

So the logical size is an opening state WayVNC abandons one update in, and the
steady state is the output's buffer pixels. Only the **first** client sees it:
every later client is served the settled size from its own ServerInit.

`VncBackend.prepare` therefore **settles WayVNC itself** (`ManagedWayVNC.settle`)
before the session is handed a bridge: one throwaway RFB connection on the owned
loopback port asks for a full update, absorbs the correction and goes away, so
the session's real client meets one size and no mid-stream resize. It is best
effort and bounded; a prime that could not finish is not a failed session.

This matters because it is not hypothetical. The iOS client's vendored
LibVNCClient 0.9.15 refuses the real host's correction outright —
`Rect too large: 2560x1764 at (0, 0)` — and the session then survives only
because REMOTE-4 re-dials it, which costs about half a second and a reconnect
nobody asked for. Measured on the real host, REMOTE-6.

`initial_framebuffer_pixels` is what ServerInit will announce: the settled size
when the prime worked, the compositor's logical size when it did not, so a
client always knows whether a correction is coming.

SPEC-E3 answered this by planning the vnc backend at a render density of 1, so
that the two sizes were the same number and there was nothing to follow. That
worked and it cost the picture half its pixels: the same iPad got a 2560×1920
scale-2 desktop over Sunshine and a 1280×960 scale-1 one over VNC, which on a
retina screen is visibly soft. REMOTE-6 took that back. **Both backends are now
planned at the host's `render_density`**, the flip is followed on the client,
and the two sizes are named here so no client has to discover it the hard way.

The client still handles the correction when it meets one: the Omodachi view
follows the new size, keeps the old picture on screen and holds input between
the resize and the first complete frame at the new size, because a pointer
mapped in that window would land at half the intended place. Belt and braces —
the prime is what makes it not happen.

### What a host that wants the old behaviour does

`render_density` in `~/.config/omodachi/desktop-runtime.json` is the one knob,
and it applies to both backends. Setting it to `1.0` restores exactly the
SPEC-E3 shape — `output_mode_pixels == logical_size == framebuffer_pixels`, a
quarter of the bytes on the wire, and no resize at all — for Sunshine as well.

WayVNC runs with `-R`, `-o <owned output>`, a private control socket and a
pre-bound `127.0.0.1` file descriptor. Nothing is exposed on the LAN and no
extra user account is created. Per-instance stderr goes to a bounded owned
diagnostic file, never into a response.

## The WSS bridge

`GET /v1/remote/sessions/{id}/vnc` upgrades to a WebSocket on the connection the
client already has: the same TLS certificate it pinned at pairing, the same
`Authorization: Bearer` credential, the same port 8099. There is no second
identity and no SSH involved — the Remote media path does not depend on the
client holding a shell account on the host.

Core answers the upgrade only for the device that owns a `ready` session whose
backend is `vnc`; a foreign device gets `403 permission_denied`, an unknown
session `404 session_not_found`, a Sunshine session `409
vnc_bridge_unavailable`. It then connects `127.0.0.1:<owned WayVNC port>` and
copies bytes: **one WebSocket BINARY message is a run of TCP bytes**, with no
framing, length prefix, base64 or envelope in either direction. TEXT frames are
not a control channel and close the bridge. Either end closing closes the other;
a revoked credential or a released session ends it within a second. At most one
bridge exists per session, because a second RFB client would fight the first for
the same framebuffer.

Readiness comes from WayVNC 0.10.1's `output-list`, whose `response.data` is an
array of `{name, width, height, captured, power}`. Those width and height are
the compositor's **logical** size, not RFB framebuffer pixels; the two
coordinate spaces are compared only against the logical size, never against the
mode pixels.

## Cleanup

Stopping ends the owned process and then completes the same identity-checked
control-socket path: a terminated instance can leave its socket inode behind, so
the socket is unlinked only after confirming that nothing is listening on that
exact private inode. Restart recovery can stop a matching owned instance through
its own control socket. Panel, Agent, Herdr and SSH are untouched.

WayVNC still needs a running Hyprland session with at least one enabled physical
output to fall back to; operation with zero outputs is not supported, and no
alternate compositor is launched.
