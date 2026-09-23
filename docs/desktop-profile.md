# Desktop profile planning

`remote/profile.py` is a pure library: it imports no host CLI, network, media,
display or persistence adapter and has no side effects. It turns a client's
viewport, orientation, desired desktop density and quality budget into a single
`DesktopProfile` that `remote/session.py` then applies to the owned output and
reads back.

## API to consume

The host adapter constructs a finite tuple of `OutputChoice` records, each containing a validated output ID, `mode_pixels`, `scale`, exact adapter-proven `stream_sizes`, output `refresh_hz`, `strategy="headless"` and `transform=0`. Merely constructing these records does not discover or prove support. Actual output creation/readback/capture remains the future adapter's responsibility. A physical-transform choice is rejected; only the owned headless strategy is modelled.

`DesktopProfilePlanner(choices, encoder=EncoderLimits(...)).plan(ProfileRequest(...))` returns a `DesktopProfile` or raises `DesktopProfileError` with a bounded code. Encoder limits are mandatory, not defaults inferred from a GPU, operating system, decoder or viewport:

| Host encoder field | Meaning |
| --- | --- |
| `max_width`, `max_height` | Maximum encoded pixel dimensions, independently bounded |
| `max_pixels` | Maximum encoded pixel count |
| `max_fps`, `max_bitrate_kbps` | Verified encoder rate limits |
| `width_alignment`, `height_alignment` | Explicit dimension multiples from the adapter; each is a power of two from 2 through 256 |
| `codecs` | Explicit host support; the planner selects H.264 SDR only |

The H.264 baseline requires even encoded dimensions. A stricter adapter alignment is honored independently on both axes. Dimensions are filtered, never silently rounded or padded. The constraint refers to encoded stream dimensions, not arbitrary output-pixel alignment assumptions. A final `DesktopProfile` also rejects odd stream dimensions to prevent bypassing the baseline invariant through decoding or direct construction.

`ProfileRequest.from_dict` accepts exactly these fields:

| Request field | Meaning |
| --- | --- |
| `viewport_points: {width,height}` | Stable UIKit available bounds in points; no nativeScale multiplication |
| `orientation` | `portrait`, `portrait_upside_down`, `landscape_left` or `landscape_right`; must agree with bounds |
| `logical_long_edge` | Explicit desired desktop logical long edge, 320–4096; distinct from quality |
| `quality: {max_pixels,fps,bitrate_kbps}` | User's requested encoded-video ceiling |
| `decoder: {max_width,max_height,max_pixels,max_fps,max_bitrate_kbps,codecs}` | Client-declared, independently checked decoder limits |

The input does not accept arbitrary output IDs, compositor paths, command strings, scale, or iOS `nativeScale`. Orientation stabilization/debounce, direction locking, keyboard/panel exclusion and session validation are outside the planner; the caller must pass a stable request. Opposite orientations in the same category yield the same profile and therefore do not themselves require a host rebuild.

## Selection and density rules

1. Filter host-supported output modes to viewport aspect error at most 1%. If none match, return `viewport_aspect_unsupported`; never fall back to fixed mirror or stretch.
2. Require the requested logical long edge to match an output's `mode_pixels / scale` within numerical tolerance (`1e-9` relative, `1e-6` absolute). Do not choose the nearest desktop density. If absent, return `density_profile_unsupported`.
3. Determine the best-aspect logical geometry without reference to quality. Preserve both logical width and height when considering lower quality. This prevents a budget decrease from reflowing the desktop even when two output candidates have slightly different aspect rounding.
4. Evaluate every output/scale pair with that same logical geometry. Filter all stream options by host encoder limits, explicit alignment, client decoder limits, requested quality budget and viewport aspect error.
5. Select the highest feasible pixel count, with deterministic aspect/output tie breakers. FPS is capped by output refresh, host encoder, decoder and user request; bitrate is capped by host, decoder and user request. With no feasible stream at that density, return `quality_profile_unsupported`.

This fixes the concrete regression where output A was selected before stream-budget filtering: A=`1920×1080 / scale 1`, stream=`1920×1080`; B=`3840×2160 / scale 2`, stream=`960×540`. With viewport `320×180`, logical long edge `1920`, and one-million-pixel budget, both output orderings now choose B and retain logical `1920×1080`. Higher quality may choose A; lower quality changes output/scale together without changing desktop logical geometry.

The second reported regression, stream `1281×721`, now fails the explicit H.264 alignment check. If an aligned `1280×720` choice is also advertised, it is selected; otherwise the planner rejects the request. The tests separately verify a host that requires 16×16 encoded alignment.

## Geometry returned and observed

The profile returns `output_id`, `output_mode_pixels`, `output_scale`, `logical_size`, `stream_pixels`, `fps`, `bitrate_kbps`, `codec="h264"`, `dynamic_range="sdr"`, `transform=0`, and `strategy="headless"`. These are a **planned profile**, not actual compositor or decoded-frame readback. A deterministic `profile_id` property can identify equal local plans.

The five geometry concepts remain separate:

- `viewport_points`: request-side stable UI bounds.
- `video_rect_points`: actual presentation rectangle; never inferred as an already observed result by the planner.
- `stream_pixels`: planned encoded pixels in a profile; actual decoded pixels are the client's own business.
- `output_mode_pixels`: selected compositor mode pixels, which may exceed encoded pixels if that exact pairing was proved by the adapter.
- `output_scale` and `logical_size`: desktop density and layout, with `logical_size = output_mode_pixels / output_scale`.

`validate_presented_geometry(viewport, video_rect, decoded_stream)` checks separately supplied presentation geometry: rectangle within viewport, no stretching beyond 1%, centered aspect fit, and bounded rounding tolerance. It rejects a tiny same-aspect image or a shifted/stretched rectangle. It does not prove that a frame was presented; `POST /v1/remote/sessions/{id}/presented` records its result as telemetry and blocks nothing. It does not inject touch input.

## Validation

```sh
PYTHONPATH=src .venv/bin/python -m unittest tests.test_remote_profile
```

The dedicated tests cover portrait and landscape modes, opposite-orientation
identity, adapter alignment on both axes, exact pixel-budget boundaries,
separate encoder and decoder limits, FPS and bitrate caps, density invariance
across quality changes, explicit unavailable-profile rejection, deterministic
capability ordering, strict request fields, bool/NaN/infinity/out-of-range
rejection, round trips and presented-rectangle validation.
