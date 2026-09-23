"""Planner-only checks using declared synthetic host/decoder capabilities.

Nothing in this module opens a display, starts media, or proves an encoder on
an actual host supports the dimensions used by these test adapters.
"""
from __future__ import annotations

from dataclasses import replace
import itertools
import math
import unittest

from omodachi_core.remote.profile import (
    DecodeLimits, DesktopProfile, DesktopProfileError, DesktopProfilePlanner,
    EncoderLimits, OutputChoice, PixelSize, PointRect, PointSize, ProfileRequest,
    QualityBudget, ViewportProfilePlanner, aspect_error, negotiate_codec, validate_presented_geometry,
)


def encoder(**overrides):
    values = dict(max_width=4096, max_height=4096, max_pixels=16777216,
                  max_fps=60, max_bitrate_kbps=50000, width_alignment=2, height_alignment=2)
    return EncoderLimits(**(values | overrides))


def request(*, portrait=False, logical=1280, budget=4000000, **overrides):
    values = dict(viewport_points=PointSize(450, 800) if portrait else PointSize(800, 450),
                  orientation="portrait" if portrait else "landscape_left", logical_long_edge=logical,
                  quality=QualityBudget(budget, 60, 40000),
                  decoder=DecodeLimits(4096, 4096, 16777216, 60, 40000))
    return ProfileRequest(**(values | overrides))


def output(*, portrait=False, name="owned-test-output"):
    return OutputChoice(name, PixelSize(1440, 2560) if portrait else PixelSize(2560, 1440), 2,
                        (PixelSize(1440, 2560), PixelSize(720, 1280), PixelSize(360, 640)) if portrait
                        else (PixelSize(2560, 1440), PixelSize(1280, 720), PixelSize(640, 360)))


class DesktopProfilePlannerTests(unittest.TestCase):
    def test_portrait_and_landscape_use_distinct_modes_with_transform_zero(self):
        planner = DesktopProfilePlanner((output(), output(portrait=True)), encoder=encoder())
        landscape = planner.plan(request())
        portrait = planner.plan(request(portrait=True))
        self.assertEqual(landscape.output_mode_pixels, PixelSize(2560, 1440))
        self.assertEqual(portrait.output_mode_pixels, PixelSize(1440, 2560))
        self.assertEqual(landscape.logical_size, PointSize(1280, 720))
        self.assertEqual(portrait.logical_size, PointSize(720, 1280))
        self.assertEqual((landscape.transform, portrait.transform), (0, 0))
        for profile, viewport in [(landscape, PointSize(800,450)), (portrait,PointSize(450,800))]:
            self.assertLessEqual(aspect_error(profile.stream_pixels.aspect,viewport.aspect), .01)
            self.assertLessEqual(aspect_error(profile.output_mode_pixels.aspect,viewport.aspect), .01)

    def test_reproduced_feasible_output_is_not_hidden_by_first_infeasible_output(self):
        a = OutputChoice("a", PixelSize(1920,1080), 1, (PixelSize(1920,1080),))
        b = OutputChoice("b", PixelSize(3840,2160), 2, (PixelSize(960,540),))
        wanted = request(logical=1920, budget=1000000, viewport_points=PointSize(320,180))
        results = [DesktopProfilePlanner(choices,encoder=encoder()).plan(wanted) for choices in [(a,b),(b,a)]]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0].output_id, "b")
        self.assertEqual(results[0].stream_pixels, PixelSize(960,540))
        self.assertEqual(results[0].logical_size, PointSize(1920,1080))

    def test_lower_quality_can_change_output_scale_pair_but_preserves_logical_geometry(self):
        a = OutputChoice("a",PixelSize(1920,1080),1,(PixelSize(1920,1080),))
        b = OutputChoice("b",PixelSize(3840,2160),2,(PixelSize(960,540),))
        planner = DesktopProfilePlanner((a,b),encoder=encoder())
        high = planner.plan(request(logical=1920))
        low = planner.plan(request(logical=1920,budget=1000000))
        self.assertEqual(high.output_id,"a")
        self.assertEqual(low.output_id,"b")
        self.assertNotEqual(high.output_scale,low.output_scale)
        self.assertEqual(high.logical_size,low.logical_size)

    def test_stream_budget_changes_quality_without_changing_output_density(self):
        planner = DesktopProfilePlanner((output(),),encoder=encoder())
        high, low = planner.plan(request()), planner.plan(request(budget=1000000))
        self.assertEqual(high.stream_pixels,PixelSize(2560,1440))
        self.assertEqual(low.stream_pixels,PixelSize(1280,720))
        self.assertEqual(high.output_mode_pixels,low.output_mode_pixels)
        self.assertEqual(high.output_scale,low.output_scale)
        self.assertEqual(high.logical_size,low.logical_size)

    def test_exact_requested_density_is_not_silently_replaced_with_nearest(self):
        planner = DesktopProfilePlanner((output(),),encoder=encoder())
        with self.assertRaisesRegex(DesktopProfileError,"density_profile_unsupported"):
            planner.plan(request(logical=1920))

    def test_budget_does_not_fall_back_to_a_different_density(self):
        exact = OutputChoice("exact",PixelSize(2560,1440),2,(PixelSize(2560,1440),))
        cheap = OutputChoice("cheap",PixelSize(1280,720),2,(PixelSize(640,360),))
        planner=DesktopProfilePlanner((exact,cheap),encoder=encoder())
        with self.assertRaisesRegex(DesktopProfileError,"quality_profile_unsupported"):
            planner.plan(request(budget=1000000))

    def test_odd_h264_stream_is_rejected_without_rounding(self):
        odd=OutputChoice("odd",PixelSize(1920,1080),1,(PixelSize(1281,721),))
        with self.assertRaisesRegex(DesktopProfileError,"quality_profile_unsupported"):
            DesktopProfilePlanner((odd,),encoder=encoder()).plan(request(logical=1920))
        safe=OutputChoice("mixed",PixelSize(1920,1080),1,(PixelSize(1281,721),PixelSize(1280,720)))
        profile=DesktopProfilePlanner((safe,),encoder=encoder()).plan(request(logical=1920))
        self.assertEqual(profile.stream_pixels,PixelSize(1280,720))
        with self.assertRaisesRegex(DesktopProfileError,"stream_alignment_unsupported"):
            replace(profile,stream_pixels=PixelSize(1281,721))

    def test_adapter_specific_alignment_filters_each_dimension(self):
        choices=(OutputChoice("aligned",PixelSize(1920,1080),1,
                              (PixelSize(1920,1080),PixelSize(1280,720),PixelSize(960,540))),)
        profile=DesktopProfilePlanner(choices,encoder=encoder(width_alignment=16,height_alignment=16)).plan(request(logical=1920))
        self.assertEqual(profile.stream_pixels,PixelSize(1280,720))
        # 1920 meets width alignment, but 1080 fails height alignment.
        self.assertEqual(profile.stream_pixels.width % 16,0)
        self.assertEqual(profile.stream_pixels.height % 16,0)

    def test_encoder_and_decoder_limits_are_both_applied(self):
        limited=encoder(max_width=1280,max_height=720,max_pixels=1000000,max_fps=30,max_bitrate_kbps=20000)
        wanted=request(decoder=DecodeLimits(1920,1080,1500000,48,25000))
        profile=DesktopProfilePlanner((output(),),encoder=limited).plan(wanted)
        self.assertEqual(profile.stream_pixels,PixelSize(1280,720))
        self.assertEqual(profile.fps,30)
        self.assertEqual(profile.bitrate_kbps,20000)
        self.assertEqual(profile.output_mode_pixels,PixelSize(2560,1440))
        # Decoder dimensions describe coded video, not the compositor mode.
        wanted=replace(wanted,decoder=DecodeLimits(700,400,250000,24,12000))
        profile=DesktopProfilePlanner((output(),),encoder=limited).plan(wanted)
        self.assertEqual(profile.stream_pixels,PixelSize(640,360))
        self.assertEqual((profile.fps,profile.bitrate_kbps),(24,12000))

    def test_output_refresh_and_requested_rates_are_not_exceeded(self):
        wanted=request(quality=QualityBudget(4000000,25,7000))
        profile=DesktopProfilePlanner((replace(output(),refresh_hz=20),),encoder=encoder()).plan(wanted)
        self.assertEqual((profile.fps,profile.bitrate_kbps),(20,7000))

    def test_budget_exact_boundary_is_inclusive_and_one_pixel_below_falls_back(self):
        planner=DesktopProfilePlanner((output(),),encoder=encoder())
        self.assertEqual(planner.plan(request(budget=1280*720)).stream_pixels,PixelSize(1280,720))
        self.assertEqual(planner.plan(request(budget=1280*720-1)).stream_pixels,PixelSize(640,360))
        with self.assertRaisesRegex(DesktopProfileError,"quality_profile_unsupported"):
            planner.plan(request(budget=640*360-1))

    def test_no_viewport_match_never_returns_fixed_mirror(self):
        planner=DesktopProfilePlanner((output(),),encoder=encoder())
        with self.assertRaisesRegex(DesktopProfileError,"viewport_aspect_unsupported"):
            planner.plan(request(viewport_points=PointSize(800,600)))

    def test_aspect_tolerance_accepts_alignment_rounding_but_not_over_one_percent(self):
        planner=DesktopProfilePlanner((output(),),encoder=encoder())
        self.assertEqual(planner.plan(request(viewport_points=PointSize(800,452))).strategy,"headless")
        with self.assertRaisesRegex(DesktopProfileError,"viewport_aspect_unsupported"):
            planner.plan(request(viewport_points=PointSize(800,460)))

    def test_half_turn_does_not_change_profile_identity(self):
        planner=DesktopProfilePlanner((output(),output(portrait=True)),encoder=encoder())
        landscape=planner.plan(request())
        opposite=planner.plan(request(orientation="landscape_right"))
        portrait=planner.plan(request(portrait=True))
        upside=planner.plan(request(portrait=True,orientation="portrait_upside_down"))
        self.assertEqual(landscape.profile_id,opposite.profile_id)
        self.assertEqual(portrait.profile_id,upside.profile_id)

    def test_selection_is_deterministic_under_all_capability_permutations(self):
        choices=(output(name="b"),output(name="a"),replace(output(name="c"),refresh_hz=30))
        profiles=[DesktopProfilePlanner(items,encoder=encoder()).plan(request()) for items in itertools.permutations(choices)]
        self.assertTrue(all(profile==profiles[0] for profile in profiles))

    def test_profile_and_request_round_trip_preserves_separate_geometry(self):
        wanted=request(budget=1000000)
        self.assertEqual(ProfileRequest.from_dict(wanted.to_dict()),wanted)
        profile=DesktopProfilePlanner((output(),),encoder=encoder()).plan(wanted)
        self.assertEqual(DesktopProfile.from_dict(profile.to_dict()),profile)
        self.assertNotIn("video_rect_points",profile.to_dict())
        self.assertNotIn("viewport_points",profile.to_dict())
        self.assertNotEqual(profile.stream_pixels,profile.output_mode_pixels)
        with self.assertRaisesRegex(DesktopProfileError,"logical_size_mismatch"):
            replace(profile,logical_size=PointSize(1920,1080))

    def test_native_scale_and_client_output_paths_are_not_request_fields(self):
        for extra in [{"nativeScale":3},{"output_id":"client-chosen"},{"scale":2},{"command":"hyprctl"}]:
            with self.subTest(extra=extra),self.assertRaises(DesktopProfileError):
                ProfileRequest.from_dict(request().to_dict() | extra)

    def test_h264_requires_both_host_and_client_support(self):
        with self.assertRaisesRegex(DesktopProfileError,"profile_unsupported"):
            DesktopProfilePlanner((output(),),encoder=encoder(codecs=("hevc",))).plan(request())
        with self.assertRaisesRegex(DesktopProfileError,"profile_unsupported"):
            DesktopProfilePlanner((output(),),encoder=encoder()).plan(request(decoder=DecodeLimits(4096,4096,4000000,60,40000,("av1",))))

    def test_invalid_numerics_types_bounds_and_mutable_capabilities_fail_closed(self):
        constructors=[
            lambda:PointSize(True,600),lambda:PointSize(math.nan,600),lambda:PointSize(math.inf,600),
            lambda:PointSize(0,600),lambda:PointSize(16385,600),lambda:PixelSize(1280.0,720),
            lambda:PixelSize(1,720),lambda:QualityBudget(True,60,1000),lambda:QualityBudget(1000,0,1000),
            lambda:QualityBudget(1000,60,250001),lambda:DecodeLimits(200,200,40000,60,1000,(["h264"],)),
            lambda:DecodeLimits(200,200,40000,60,1000,["h264"]),lambda:request(orientation=[]),
            lambda:request(orientation="face_up"),lambda:request(orientation="portrait"),lambda:request(logical=319),
            lambda:request(viewport_points={"width":800,"height":450}),lambda:encoder(width_alignment=1),
            lambda:encoder(height_alignment=3),lambda:encoder(width_alignment=True),lambda:encoder(width_alignment=512),
            lambda:OutputChoice("x",PixelSize(1280,720),0,(PixelSize(1280,720),)),
            lambda:OutputChoice("x",PixelSize(1280,720),1,[PixelSize(1280,720)]),
            lambda:replace(output(),strategy="physical_transform"),lambda:replace(output(),transform=1),
            lambda:DesktopProfilePlanner((),encoder=encoder()),lambda:DesktopProfilePlanner((None,),encoder=encoder()),
            lambda:DesktopProfilePlanner((output(),),encoder=None),
        ]
        for index,constructor in enumerate(constructors):
            with self.subTest(index=index),self.assertRaises(DesktopProfileError): constructor()


class CodecNegotiationTests(unittest.TestCase):
    """STREAM-1: HEVC when both ends can, H.264 whenever either cannot."""

    def test_hevc_when_the_decoder_and_the_encoder_both_do_it(self):
        self.assertEqual(negotiate_codec(("h264", "hevc"), ("h264", "hevc")), "hevc")

    def test_h264_when_either_side_does_not_say_hevc(self):
        self.assertEqual(negotiate_codec(("h264",), ("h264", "hevc")), "h264")
        self.assertEqual(negotiate_codec(("h264", "hevc"), ("h264",)), "h264")
        self.assertEqual(negotiate_codec(("h264", "hevc"), ()), "h264")
        self.assertEqual(negotiate_codec(None, None), "h264")

    def test_a_codec_nobody_ships_a_profile_for_is_never_chosen(self):
        # AV1 is a known name, but the Sunshine host has no AV1 encoder and no
        # profile here may carry it; it must fall through, not be picked.
        self.assertEqual(negotiate_codec(("h264", "av1"), ("h264", "av1")), "h264")

    def test_the_planner_carries_the_negotiated_codec_into_the_profile(self):
        hevc = request(decoder=DecodeLimits(4096, 4096, 16777216, 60, 40000, ("h264", "hevc")))
        planner = ViewportProfilePlanner("OMODACHI-0123456789abcdef", encoder=encoder())
        self.assertEqual(planner.plan(hevc, codec="hevc").codec, "hevc")
        self.assertEqual(planner.plan(hevc).codec, "h264")
        # Same geometry and the same rates either way: the codec is the only change.
        self.assertEqual(replace(planner.plan(hevc, codec="hevc"), codec="h264"), planner.plan(hevc))

    def test_the_planner_refuses_a_codec_the_client_never_said_it_decodes(self):
        planner = ViewportProfilePlanner("OMODACHI-0123456789abcdef", encoder=encoder())
        with self.assertRaises(DesktopProfileError) as caught:
            planner.plan(request(), codec="hevc")
        self.assertEqual(caught.exception.code, "profile_unsupported")

    def test_a_profile_may_be_hevc_but_not_av1(self):
        planned = ViewportProfilePlanner("OMODACHI-0123456789abcdef", encoder=encoder()).plan(request())
        self.assertEqual(DesktopProfile.from_dict({**planned.to_dict(), "codec": "hevc"}).codec, "hevc")
        with self.assertRaises(DesktopProfileError):
            DesktopProfile.from_dict({**planned.to_dict(), "codec": "av1"})


class DesktopPresentedGeometryTests(unittest.TestCase):
    def test_full_frame_and_centered_letterbox_are_valid(self):
        validate_presented_geometry(PointSize(800,450),PointRect(0,0,800,450),PixelSize(1280,720))
        validate_presented_geometry(PointSize(800,600),PointRect(0,75,800,450),PixelSize(1280,720))
        validate_presented_geometry(PointSize(600,800),PointRect(75,0,450,800),PixelSize(720,1280))

    def test_rect_outside_viewport_is_rejected(self):
        with self.assertRaisesRegex(DesktopProfileError,"video_rect_outside_viewport"):
            validate_presented_geometry(PointSize(800,450),PointRect(5,0,800,450),PixelSize(1280,720))

    def test_stretching_small_correct_aspect_and_offcenter_rect_are_rejected(self):
        for rect,code in [(PointRect(0,0,800,600),"video_stretched"),
                          (PointRect(0,0,80,45),"video_rect_not_aspect_fit"),
                          (PointRect(0,70,800,450),"video_rect_not_aspect_fit")]:
            with self.subTest(rect=rect),self.assertRaisesRegex(DesktopProfileError,code):
                validate_presented_geometry(PointSize(800,600),rect,PixelSize(1280,720))

    def test_geometry_type_errors_and_negative_rect_are_rejected(self):
        with self.assertRaises(DesktopProfileError): PointRect(-1,0,800,450)
        with self.assertRaises(DesktopProfileError):
            validate_presented_geometry({},PointRect(0,0,800,450),PixelSize(1280,720))
