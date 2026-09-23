"""Unpublished Desktop profile planning over adapter-proven capabilities only.

No compositor, encoder, network or media side effect lives here. A real adapter
must supply finite supported output/stream combinations after its own probes.
UIKit points, decoded pixels, output pixels and desktop logical units are
separate types. iOS nativeScale is deliberately not a planning input.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re
from typing import Any


class DesktopProfileError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def number(value: Any, minimum: float, maximum: float) -> float:
    if type(value) not in {int, float} or not math.isfinite(value) or not minimum <= value <= maximum:
        raise DesktopProfileError("invalid_geometry")
    return float(value)


def integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise DesktopProfileError("invalid_geometry")
    return value


def exact_fields(data: dict, fields: set[str]) -> None:
    if not isinstance(data, dict) or set(data) != fields:
        raise DesktopProfileError("invalid_geometry")


@dataclass(frozen=True)
class PointSize:
    width: float
    height: float

    def __post_init__(self):
        number(self.width, 1, 16384); number(self.height, 1, 16384)

    @classmethod
    def from_dict(cls, data):
        exact_fields(data, {"width", "height"})
        return cls(**data)

    @property
    def aspect(self): return self.width / self.height

    def to_dict(self): return asdict(self)


@dataclass(frozen=True)
class PixelSize:
    width: int
    height: int

    def __post_init__(self):
        integer(self.width, 2, 16384); integer(self.height, 2, 16384)

    @classmethod
    def from_dict(cls, data):
        exact_fields(data, {"width", "height"})
        return cls(**data)

    @property
    def aspect(self): return self.width / self.height

    @property
    def area(self): return self.width * self.height

    def to_dict(self): return asdict(self)


@dataclass(frozen=True)
class PointRect:
    x: float
    y: float
    width: float
    height: float

    def __post_init__(self):
        number(self.x, 0, 16384); number(self.y, 0, 16384)
        number(self.width, 1, 16384); number(self.height, 1, 16384)

    @classmethod
    def from_dict(cls, data):
        exact_fields(data, {"x", "y", "width", "height"})
        return cls(**data)

    def to_dict(self): return asdict(self)


ORIENTATIONS = {"portrait", "portrait_upside_down", "landscape_left", "landscape_right"}


def orientation_class(orientation: str) -> str:
    if not isinstance(orientation, str) or orientation not in ORIENTATIONS:
        raise DesktopProfileError("invalid_orientation")
    return "portrait" if orientation.startswith("portrait") else "landscape"


def aspect_error(a: float, b: float) -> float:
    return abs(a / b - 1.0)


# STREAM-1. The codecs a stream may carry, best first. HEVC is chosen whenever
# both ends can do it: at the same bitrate it is visibly sharper than H.264 on
# text and fine desktop detail, or the same picture for 30-40 % fewer bits.
# H.264 stays the baseline every client and every managed encoder must have,
# so the fallback is always there and never a refusal.
STREAM_CODECS = ("hevc", "h264")


def negotiate_codec(decoder_codecs, host_codecs) -> str:
    """The best codec both the client's decoder and the host's encoder name.

    The host side is what the encoder is *actually* serving right now (the
    managed fork's own answer), not a configured wish. Anything that is not a
    known codec is ignored rather than trusted; nothing in common but H.264 -
    or nothing said at all - is H.264.
    """
    decoder = set(decoder_codecs or ())
    host = set(host_codecs or ())
    for codec in STREAM_CODECS:
        if codec in decoder and codec in host:
            return codec
    return "h264"


@dataclass(frozen=True)
class OutputChoice:
    """One proven adaptive headless output; physical transform is not inferred."""
    output_id: str
    mode_pixels: PixelSize
    scale: float
    stream_sizes: tuple[PixelSize, ...]
    refresh_hz: int = 60
    transform: int = 0
    strategy: str = "headless"

    def __post_init__(self):
        if not isinstance(self.output_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", self.output_id):
            raise DesktopProfileError("invalid_output_id")
        number(self.scale, .25, 8)
        integer(self.refresh_hz, 1, 240)
        if self.strategy != "headless" or type(self.transform) is not int or self.transform != 0:
            raise DesktopProfileError("output_strategy_unverified")
        if (not isinstance(self.mode_pixels, PixelSize) or not isinstance(self.stream_sizes, tuple)
                or not self.stream_sizes or len(self.stream_sizes) > 128):
            raise DesktopProfileError("profile_unsupported")
        if any(not isinstance(size, PixelSize) or aspect_error(size.aspect, self.mode_pixels.aspect) > .01 for size in self.stream_sizes):
            raise DesktopProfileError("stream_output_aspect_mismatch")
        PointSize(self.mode_pixels.width / self.scale, self.mode_pixels.height / self.scale)

    @property
    def logical_size(self):
        return PointSize(self.mode_pixels.width / self.scale, self.mode_pixels.height / self.scale)


@dataclass(frozen=True)
class DecodeLimits:
    max_width: int
    max_height: int
    max_pixels: int
    max_fps: int
    max_bitrate_kbps: int
    codecs: tuple[str, ...] = ("h264",)

    def __post_init__(self):
        integer(self.max_width, 2, 16384); integer(self.max_height, 2, 16384)
        integer(self.max_pixels, 4, 16384 * 16384)
        integer(self.max_fps, 1, 240); integer(self.max_bitrate_kbps, 64, 250000)
        if (not isinstance(self.codecs, tuple) or not self.codecs or len(self.codecs) > 3
                or any(not isinstance(codec, str) or codec not in {"h264", "hevc", "av1"} for codec in self.codecs)):
            raise DesktopProfileError("invalid_decoder_limits")

    @classmethod
    def from_dict(cls, data):
        exact_fields(data, {"max_width", "max_height", "max_pixels", "max_fps", "max_bitrate_kbps", "codecs"})
        if not isinstance(data["codecs"], list): raise DesktopProfileError("invalid_decoder_limits")
        return cls(**{**data, "codecs": tuple(data["codecs"])})

    def to_dict(self):
        return {**asdict(self), "codecs": list(self.codecs)}


@dataclass(frozen=True)
class EncoderLimits:
    """Host adapter's explicit H.264 SDR constraints, not an inferred GPU claim.

    Alignment is in encoded pixels, independently for width and height. The
    selected baseline requires even dimensions; adapters may impose stricter
    power-of-two alignment. Unsupported dimensions are rejected, never rounded.
    """
    max_width: int
    max_height: int
    max_pixels: int
    max_fps: int
    max_bitrate_kbps: int
    width_alignment: int
    height_alignment: int
    codecs: tuple[str, ...] = ("h264",)

    def __post_init__(self):
        DecodeLimits(self.max_width, self.max_height, self.max_pixels, self.max_fps,
                     self.max_bitrate_kbps, self.codecs)
        for value in (self.width_alignment, self.height_alignment):
            if type(value) is not int or value not in {2, 4, 8, 16, 32, 64, 128, 256}:
                raise DesktopProfileError("invalid_encoder_alignment")

    def supports(self, pixels: PixelSize) -> bool:
        return (pixels.width <= self.max_width and pixels.height <= self.max_height
                and pixels.area <= self.max_pixels
                and pixels.width % self.width_alignment == 0
                and pixels.height % self.height_alignment == 0)


@dataclass(frozen=True)
class QualityBudget:
    max_pixels: int
    fps: int
    bitrate_kbps: int

    def __post_init__(self):
        integer(self.max_pixels, 4, 16384 * 16384)
        integer(self.fps, 1, 240); integer(self.bitrate_kbps, 64, 250000)

    @classmethod
    def from_dict(cls, data):
        exact_fields(data, {"max_pixels", "fps", "bitrate_kbps"})
        return cls(**data)

    def to_dict(self): return asdict(self)


@dataclass(frozen=True)
class ProfileRequest:
    viewport_points: PointSize
    orientation: str
    logical_long_edge: float
    quality: QualityBudget
    decoder: DecodeLimits

    def __post_init__(self):
        if (not isinstance(self.viewport_points, PointSize) or not isinstance(self.quality, QualityBudget)
                or not isinstance(self.decoder, DecodeLimits)):
            raise DesktopProfileError("invalid_profile_request")
        category = orientation_class(self.orientation)
        number(self.logical_long_edge, 320, 4096)
        if (category == "portrait" and self.viewport_points.width > self.viewport_points.height
                or category == "landscape" and self.viewport_points.height > self.viewport_points.width):
            raise DesktopProfileError("unstable_orientation_bounds")

    @classmethod
    def from_dict(cls, data):
        exact_fields(data, {"viewport_points", "orientation", "logical_long_edge", "quality", "decoder"})
        return cls(PointSize.from_dict(data["viewport_points"]), data["orientation"], data["logical_long_edge"],
                   QualityBudget.from_dict(data["quality"]), DecodeLimits.from_dict(data["decoder"]))

    def to_dict(self):
        return {"viewport_points": self.viewport_points.to_dict(), "orientation": self.orientation,
                "logical_long_edge": self.logical_long_edge, "quality": self.quality.to_dict(),
                "decoder": self.decoder.to_dict()}


@dataclass(frozen=True)
class DesktopProfile:
    output_id: str
    output_mode_pixels: PixelSize
    output_scale: float
    logical_size: PointSize
    stream_pixels: PixelSize
    fps: int
    bitrate_kbps: int
    codec: str = "h264"
    dynamic_range: str = "sdr"
    transform: int = 0
    strategy: str = "headless"

    def __post_init__(self):
        if (not isinstance(self.output_mode_pixels, PixelSize) or not isinstance(self.stream_pixels, PixelSize)
                or not isinstance(self.logical_size, PointSize)):
            raise DesktopProfileError("invalid_geometry")
        if self.stream_pixels.width % 2 or self.stream_pixels.height % 2:
            raise DesktopProfileError("stream_alignment_unsupported")
        OutputChoice(self.output_id, self.output_mode_pixels, self.output_scale, (self.stream_pixels,), self.fps,
                     self.transform, self.strategy)
        QualityBudget(self.stream_pixels.area, self.fps, self.bitrate_kbps)
        if self.codec not in STREAM_CODECS or self.dynamic_range != "sdr":
            raise DesktopProfileError("profile_unsupported")
        expected = PointSize(self.output_mode_pixels.width / self.output_scale, self.output_mode_pixels.height / self.output_scale)
        if not math.isclose(expected.width, self.logical_size.width, rel_tol=1e-9) or not math.isclose(expected.height, self.logical_size.height, rel_tol=1e-9):
            raise DesktopProfileError("logical_size_mismatch")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        exact_fields(data, set(cls.__dataclass_fields__))
        return cls(**{**data, "output_mode_pixels": PixelSize.from_dict(data["output_mode_pixels"]),
                      "logical_size": PointSize.from_dict(data["logical_size"]),
                      "stream_pixels": PixelSize.from_dict(data["stream_pixels"])})

    @property
    def profile_id(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]


class DesktopProfilePlanner:
    def __init__(self, choices: tuple[OutputChoice, ...], *, encoder: EncoderLimits):
        if (not isinstance(choices, tuple) or not choices or len(choices) > 256
                or any(not isinstance(choice, OutputChoice) for choice in choices)):
            raise DesktopProfileError("profile_unsupported")
        if not isinstance(encoder, EncoderLimits):
            raise DesktopProfileError("encoder_capabilities_required")
        self.choices, self.encoder = choices, encoder

    def plan(self, request: ProfileRequest, *, codec: str = "h264") -> DesktopProfile:
        if not isinstance(request, ProfileRequest):
            raise DesktopProfileError("invalid_profile_request")
        if "h264" not in request.decoder.codecs or "h264" not in self.encoder.codecs:
            raise DesktopProfileError("profile_unsupported")
        # The codec is negotiated by the caller against the live encoder; the
        # planner only refuses one the client never said it could decode.
        if codec not in STREAM_CODECS or codec not in request.decoder.codecs:
            raise DesktopProfileError("profile_unsupported")
        matching = [choice for choice in self.choices
                    if aspect_error(choice.mode_pixels.aspect, request.viewport_points.aspect) <= .01]
        if not matching:
            raise DesktopProfileError("viewport_aspect_unsupported")
        # Density is explicit, not an invitation to select the nearest desktop
        # size. Preserve the requested logical long edge or reject the request.
        density = [choice for choice in matching if math.isclose(
            max(choice.logical_size.width, choice.logical_size.height), request.logical_long_edge,
            rel_tol=1e-9, abs_tol=1e-6)]
        if not density:
            raise DesktopProfileError("density_profile_unsupported")
        # Resolve short-edge rounding independently of quality. This prevents a
        # lower stream budget from switching to a different desktop layout.
        anchor = min(density, key=lambda choice: (
            aspect_error(choice.mode_pixels.aspect, request.viewport_points.aspect),
            choice.logical_size.width, choice.logical_size.height))
        same_density = [choice for choice in density if
                        math.isclose(choice.logical_size.width, anchor.logical_size.width, rel_tol=1e-9, abs_tol=1e-6)
                        and math.isclose(choice.logical_size.height, anchor.logical_size.height, rel_tol=1e-9, abs_tol=1e-6)]
        budget = min(request.quality.max_pixels, request.decoder.max_pixels, self.encoder.max_pixels)
        feasible = []
        for choice in same_density:
            for stream in choice.stream_sizes:
                if (stream.area <= budget and stream.width <= request.decoder.max_width
                        and stream.height <= request.decoder.max_height and self.encoder.supports(stream)
                        and aspect_error(stream.aspect, request.viewport_points.aspect) <= .01):
                    feasible.append((choice, stream))
        if not feasible:
            raise DesktopProfileError("quality_profile_unsupported")
        # All candidates preserve the same logical geometry. Quality may pick
        # another supported output/scale pair, rather than falsely rejecting a
        # feasible pair because a different output happened to be first.
        choice, stream = min(feasible, key=lambda pair: (
            -pair[1].area, aspect_error(pair[1].aspect, request.viewport_points.aspect),
            pair[0].mode_pixels.area, pair[0].output_id, pair[0].scale,
            -pair[0].refresh_hz, pair[1].width, pair[1].height))
        return DesktopProfile(choice.output_id, choice.mode_pixels, choice.scale, choice.logical_size, stream,
                              min(choice.refresh_hz, self.encoder.max_fps, request.decoder.max_fps, request.quality.fps),
                              min(self.encoder.max_bitrate_kbps, request.decoder.max_bitrate_kbps, request.quality.bitrate_kbps),
                              codec)



class ViewportProfilePlanner:
    """Generate candidates from Remote geometry, then use the existing planner.

    No device/aspect preset list, compositor call, or media readiness claim.
    Output rendering density is independent of stream quality so reducing an
    encoder budget does not resize the host desktop or change logical density.
    """
    def __init__(self, output_id: str, *, encoder: EncoderLimits, render_density: float = 2.0):
        if not isinstance(output_id,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}",output_id):
            raise DesktopProfileError("invalid_output_id")
        if not isinstance(encoder,EncoderLimits):raise DesktopProfileError("encoder_capabilities_required")
        number(render_density,.25,8)
        self.output_id,self.encoder,self.render_density=output_id,encoder,render_density

    def _aligned(self, aspect, *, max_width, max_height, max_pixels, mode=False, additional_aspect=None):
        width_step,height_step=self.encoder.width_alignment,self.encoder.height_alignment
        best=None;best_key=None
        # EncoderLimits bounds widths <=16384, so at most8192 iterations. Both
        # neighboring aligned heights are considered; no quadratic pixel scan.
        for width in range(width_step,int(max_width)+1,width_step):
            ideal=width/aspect
            lower=int(math.floor(ideal/height_step))*height_step
            for height in {lower,lower+height_step}:
                if height<height_step or height>max_height or width*height>max_pixels:continue
                ratio=width/height
                if aspect_error(ratio,aspect)>.01:continue
                if additional_aspect is not None and aspect_error(ratio,additional_aspect)>.01:continue
                key=(-max(width,height),aspect_error(ratio,aspect),-width*height) if mode else (-width*height,aspect_error(ratio,aspect))
                if best_key is None or key<best_key:
                    best=PixelSize(width,height);best_key=key
        if best is None:raise DesktopProfileError("quality_profile_unsupported" if not mode else "viewport_geometry_unsupported")
        return best

    def _output_mode(self, request):
        aspect=request.viewport_points.aspect
        edge=request.logical_long_edge*self.render_density
        wanted_width=edge if aspect>=1 else edge*aspect
        wanted_height=edge/aspect if aspect>=1 else edge
        # Quantization admits one alignment unit at the short edge. Requested
        # aspect remains within the same strict existing planner tolerance.
        width_cap=min(self.encoder.max_width,math.ceil(wanted_width/self.encoder.width_alignment)*self.encoder.width_alignment)
        height_cap=min(self.encoder.max_height,math.ceil(wanted_height/self.encoder.height_alignment)*self.encoder.height_alignment)
        mode=self._aligned(aspect,max_width=width_cap,max_height=height_cap,max_pixels=self.encoder.max_pixels,mode=True)
        scale=max(mode.width,mode.height)/request.logical_long_edge
        if not .25<=scale<=8:raise DesktopProfileError("density_profile_unsupported")
        return mode,scale

    def plan(self, request: ProfileRequest, *, codec: str = "h264") -> DesktopProfile:
        if not isinstance(request,ProfileRequest):raise DesktopProfileError("invalid_profile_request")
        if "h264" not in request.decoder.codecs or "h264" not in self.encoder.codecs:
            raise DesktopProfileError("profile_unsupported")
        mode,scale=self._output_mode(request)
        stream=self._aligned(mode.aspect,max_width=min(mode.width,self.encoder.max_width,request.decoder.max_width),
            max_height=min(mode.height,self.encoder.max_height,request.decoder.max_height),
            max_pixels=min(request.quality.max_pixels,request.decoder.max_pixels,self.encoder.max_pixels),
            additional_aspect=request.viewport_points.aspect)
        choice=OutputChoice(self.output_id,mode,scale,(stream,),self.encoder.max_fps)
        # Retain the existing canonical selection and validation semantics.
        return DesktopProfilePlanner((choice,),encoder=self.encoder).plan(request,codec=codec)

    def authorizes(self, profile):
        """Technical guard for server-planned and owned-journal restore profiles.

        Request/lease authority stays in the existing coordinator. This predicate
        cannot authorize another output or exceed installed technical limits.
        """
        return (isinstance(profile,DesktopProfile) and profile.output_id==self.output_id
            and self.encoder.supports(profile.output_mode_pixels) and self.encoder.supports(profile.stream_pixels)
            and profile.stream_pixels.width<=profile.output_mode_pixels.width
            and profile.stream_pixels.height<=profile.output_mode_pixels.height
            and 320<=max(profile.logical_size.width,profile.logical_size.height)<=4096
            and profile.fps<=self.encoder.max_fps and profile.bitrate_kbps<=self.encoder.max_bitrate_kbps)


def validate_presented_geometry(viewport: PointSize, video_rect: PointRect, stream: PixelSize) -> None:
    if not isinstance(viewport, PointSize) or not isinstance(video_rect, PointRect) or not isinstance(stream, PixelSize):
        raise DesktopProfileError("invalid_geometry")
    if video_rect.x + video_rect.width > viewport.width + .5 or video_rect.y + video_rect.height > viewport.height + .5:
        raise DesktopProfileError("video_rect_outside_viewport")
    if aspect_error(video_rect.width / video_rect.height, stream.aspect) > .01:
        raise DesktopProfileError("video_stretched")
    # The decoded geometry must fill the aspect-fitted rectangle, not a tiny
    # correctly proportioned region. A half-point tolerance admits rounding.
    scale = min(viewport.width / stream.width, viewport.height / stream.height)
    width, height = stream.width * scale, stream.height * scale
    if (abs(video_rect.width - width) > max(.5, width * .01)
            or abs(video_rect.height - height) > max(.5, height * .01)
            or abs(video_rect.x - (viewport.width - width) / 2) > .5
            or abs(video_rect.y - (viewport.height - height) / 2) > .5):
        raise DesktopProfileError("video_rect_not_aspect_fit")
