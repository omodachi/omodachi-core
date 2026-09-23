# Voice: the uplink, Voxtype, and the transcript

Omarchy already has a voice stack. Voxtype is a first-class citizen there: the
first-run hook offers to install it, `omarchy-voxtype-install` pulls in `wtype`
and the `voxtype.service` user unit, and `SUPER+CTRL+X` / `F9` are bound to
`voxtype record toggle|start|stop`. Omodachi does not add a second speech
engine. It makes the phone's microphone reachable from the host's own one.

## Why it is shaped this way

Voxtype captures in-process with `cpal` from one named PulseAudio/PipeWire
source, and it has no interface for being handed audio or handed text. There is
exactly one `UnixListener` in the daemon and it is a write-only level fan-out.
So the only honest integration is:

1. core publishes a virtual source the phone writes PCM into, and
2. Voxtype is pointed at that source while the user is dictating.

Everything else here follows from that, including the parts that look
conservative.

## The virtual microphone

`module-null-sink` plus `module-remap-source`, owned by core:

| Node | Name |
| --- | --- |
| sink | `omodachi_mic_sink` |
| monitor | `omodachi_mic_sink.monitor` |
| source | **`omodachi_mic`** |

The source name is fixed because Voxtype finds its device by name from
`config.toml` — a per-session name could not be configured. A fixed name means a
leaked module from a crashed daemon would block the next session forever, so
`begin()` reclaims one, and only one: a module whose type *and full argument
list* are byte-for-byte the ones core creates. Nothing else is ever unloaded.

Because the name is a single host resource, the Remote session uplink
(`/v1/remote/sessions/{id}/audio`) and the standalone voice uplink share a
`MicrophoneArbiter`. Whoever asks second gets `audio_input_busy`.

## `GET /v1/voice/uplink`

The same PCM contract as the Remote uplink — 48 kHz s16le mono, 960-sample
(1920-byte) frames, at most three queued — on a socket that needs **no Remote
session**. Scenario 2 is "I am away from the machine and talking to the agent";
there is no stream in that picture.

```
client → {"generation": 1, "format": "s16le", "rate": 48000, "channels": 1, "frame_samples": 960}
server → {"type": "begun", "generation": 1, "source_name": "omodachi_mic", …}
client → <1920 bytes>                server → {"type": "accepted", "generation": 1, "sequence": 1}
client → {"type": "end", "generation": 1}    server → {"type": "ended", "generation": 1}
```

Generations only move forward per device, a binary frame before `begun` is
refused, and closing the socket cleans the modules up whatever the reason.

With `?levels=1` the same socket also carries the waveform:

```json
{"type": "voice.level", "seq": 1204, "peak": -14.3, "rms": 0.21, "vad": true}
```

Those come from `$XDG_RUNTIME_DIR/voxtype/audio.sock`, Voxtype's own 16-byte
`AudioFrame {seq, min, max, peak_dbfs}` fan-out at 100 Hz, throttled to 20 Hz.
Only the newest frame in a batch is sent; the rest is backlog. Restarting
Voxtype recreates that socket, so the reader reconnects rather than ending the
stream — the same thing `voxtype-audio-bridge --reconnect-secs` does.

## `POST /v1/voice/dictation:start` and `:stop`

Start requires three things, in this order, and changes nothing until all three
hold: the **`voice_uplink` preference is on**, Voxtype is installed with
`record stop --wait`, and an uplink is live. Pointing Voxtype at a source that
does not exist would only restart it into a broken capture.

### Which route, and why there are two

Voxtype picks its capture device through `cpal`. On a PipeWire machine cpal's
ALSA backend advertises exactly two devices — `default` and `pipewire` — and no
PulseAudio source names at all, which `voxtype info devices` reports verbatim:

```
Audio input devices
  default (default)
  pipewire
```

On such a host `[audio] device = "omodachi_mic"` does not select our source, it
*breaks capture*: `Failed to start audio: Audio device not found: 'omodachi_mic'`.
So `voxtype info devices` decides the route, and the answer is in
`capabilities.dictation.route`:

**`config`** — the source is selectable, so this is the documented path:

1. read `~/.config/voxtype/config.toml`, save the bytes to
   `config.toml.omodachi-dictation-bak` (0600), and replace the quoted value on
   the one `device = ` line inside `[audio]` with `omodachi_mic`. The file is
   not parsed as TOML, not reformatted and not rewritten: one line's value
   changes and every other byte is identical. A file with no `[audio] device`
   line gets `voxtype_config_unsupported` rather than a line invented for it.
2. `systemctl --user restart voxtype.service`, once.

**`default_source`** — it is not, so **Voxtype's config is never opened**.
Instead the PipeWire/Pulse default source is pointed at `omodachi_mic` before
the recording opens its stream (`pactl set-default-source`), the previous
default is remembered, and stopping puts it back. Voxtype's own capture stream
is also moved onto the source (`pactl move-source-output`) as a second chance
for a stream that opened early; failing to find it is not an error. No restart
of Voxtype happens on this route at all.

Both routes then run

```
voxtype record start --file <transcript> --no-osd --no-auto-submit --no-smart-auto-submit
```

`--no-osd` exists for exactly this: an external tool drawing its own dictation UI.

Stop runs `voxtype record stop --wait --json --wait-file <transcript>` (exit 0
transcribed, 3 nothing, 4 timed out), **undoes whichever change it made** — the
saved config bytes, or the previous default source — deletes the transcript, and
answers:

```json
{"text": "make the bar taller", "chars": 19, "status": "ok",
 "target": "client", "delivered_to_host": null}
```

The same text is published as the device event `voice.transcript {text, chars,
status}`. A failed start restores the config before it raises, and a daemon
shutdown restores it too — the backup file is the receipt, and it is only
removed once the original bytes are confirmed back in place.

The `default_source` route does change one global setting for the length of the
recording. That is a real trade-off and it is why it is the fallback rather than
the default: it is bounded by the dictation, it is restored on every exit path
including a failed start and a daemon shutdown, and the alternative on this
class of host is no voice input at all.

`target` decides where the words go:

| `target` | Effect |
| --- | --- |
| `client` (default) | text comes back on the wire only |
| `host` | typed into the focused host window; the response carries no text |
| `both` | both |

Host delivery is `wtype -- <text>`, falling back to `wl-copy` plus a synthetic
paste. The chord depends on the focused window: `SHIFT+Insert` when Hyprland's
`activewindow` carries the `terminal` tag, `CTRL+V` otherwise — which is what
Omarchy's own universal-paste helper does, and what makes CJK IME input survive.

## `GET /v1/voice/capabilities`

One document covering all three: whether the uplink is available, whether
dictation is (installed, `--wait`-capable, switched on), whether levels are, and
the raw Voxtype view (`installed`, `service_active`, `state`, current `device`).
When Voxtype is missing the answer carries `install_command:
["omarchy-voxtype-install"]` and `reason: "voxtype_not_installed"`, so the phone
can offer the official installer instead of a dead button.

Contracts: `contracts/voice-capabilities.schema.json`,
`contracts/voice-dictation.schema.json`, and the `voice.transcript` branch of
`contracts/events.schema.json`.

## What this never does

It does not write anything into `~/.config/voxtype` except that one value, it
does not change Voxtype's engine, model, hotkey or output mode, and it does not
keep a copy of the audio. The transcript file lives in
`~/.cache/omodachi/voice/` for the length of one dictation and is deleted with
its `.done` marker when the session ends.
