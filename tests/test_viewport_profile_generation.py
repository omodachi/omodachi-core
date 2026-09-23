"""Viewport-derived ordinary geometry, with synthetic owned-output readback.

No credential/certificate fixture, private media IPC, real compositor or host.
"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from omodachi_core.remote.profile import (ViewportProfilePlanner,DesktopProfileError,PointSize,QualityBudget,
    PixelSize,DecodeLimits,aspect_error)
from tests.test_remote_profile import encoder,request

NAME='OMODACHI-0123456789abcdef'

class ViewportProfileGenerationTests(unittest.TestCase):
    def make_request(self,width,height,*,budget=4_000_000,**kwargs):
        return request(portrait=height>width,viewport_points=PointSize(width,height),budget=budget,**kwargs)
    def test_phone_tablet_split_view_and_square_derive_without_aspect_table(self):
        planner=ViewportProfilePlanner(NAME,encoder=encoder())
        for width,height in [(1024,768),(768,1024),(393,852),(852,393),(707,1024),(1001,733),(699.5,987.25),(900,900)]:
            with self.subTest(viewport=(width,height)):
                wanted=self.make_request(width,height);profile=planner.plan(wanted)
                self.assertLessEqual(aspect_error(profile.output_mode_pixels.aspect,width/height),.01)
                self.assertLessEqual(aspect_error(profile.stream_pixels.aspect,width/height),.01)
                self.assertAlmostEqual(max(profile.logical_size.width,profile.logical_size.height),1280)
                self.assertLessEqual(profile.stream_pixels.area,wanted.quality.max_pixels)
                self.assertTrue(planner.authorizes(profile))
                self.assertEqual(profile,planner.plan(wanted))
    def test_quality_only_changes_stream_not_host_mode_or_logical_density(self):
        planner=ViewportProfilePlanner(NAME,encoder=encoder())
        high=planner.plan(self.make_request(1024,768,budget=8_000_000))
        low=planner.plan(self.make_request(1024,768,budget=700_000,quality=QualityBudget(700_000,30,8000)))
        self.assertEqual(high.output_mode_pixels,low.output_mode_pixels)
        self.assertEqual(high.logical_size,low.logical_size);self.assertEqual(high.output_scale,low.output_scale)
        self.assertGreater(high.stream_pixels.area,low.stream_pixels.area)
        self.assertEqual((low.fps,low.bitrate_kbps),(30,8000))
    def test_alignment_and_independent_decoder_caps_are_applied(self):
        limits=encoder(width_alignment=16,height_alignment=8,max_width=3072,max_height=3072,max_pixels=6_000_000)
        planner=ViewportProfilePlanner(NAME,encoder=limits)
        decoder=DecodeLimits(1280,1920,1_500_000,30,15000)
        profile=planner.plan(self.make_request(393,852,decoder=decoder))
        for size in (profile.output_mode_pixels,profile.stream_pixels):
            self.assertEqual(size.width%16,0);self.assertEqual(size.height%8,0)
        self.assertLessEqual(profile.stream_pixels.width,1280);self.assertLessEqual(profile.stream_pixels.height,1920)
        self.assertLessEqual(profile.stream_pixels.area,1_500_000)
        self.assertEqual(profile.fps,30);self.assertEqual(profile.bitrate_kbps,15000)
    def test_impossible_budget_or_alignment_refuses_instead_of_stretching(self):
        planner=ViewportProfilePlanner(NAME,encoder=encoder(width_alignment=256,height_alignment=256))
        with self.assertRaises(DesktopProfileError):planner.plan(self.make_request(393,852,budget=100))
        with self.assertRaises(DesktopProfileError):planner.plan(self.make_request(1,16384))
    def test_other_output_and_excessive_technical_profile_refused(self):
        planner=ViewportProfilePlanner(NAME,encoder=encoder())
        profile=planner.plan(self.make_request(1024,768))
        self.assertFalse(planner.authorizes(replace(profile,output_id='eDP-1')))
        self.assertFalse(planner.authorizes(replace(profile,fps=120)))
        self.assertFalse(planner.authorizes(replace(profile,bitrate_kbps=100000)))

class InstalledManagerGeometryTests(unittest.TestCase):
    def test_build_manager_derives_geometry_from_the_request_without_touching_the_host(self):
        from unittest.mock import patch
        from omodachi_core.remote import build_manager
        from omodachi_core.remote.hyprland import Hyprland
        environment=lambda:{'HYPRLAND_INSTANCE_SIGNATURE':'synthetic-instance'}
        with tempfile.TemporaryDirectory() as home:
            with patch.object(Hyprland,'_local_run',side_effect=AssertionError('unexpected host call')):
                manager=build_manager(home=Path(home),environment=environment)
                self.assertIsNone(manager.session)
                self.assertEqual(sorted(manager.backends),['sunshine','vnc'])
                self.assertEqual(manager.hyprland.instance_id,'synthetic-instance')
                planner=ViewportProfilePlanner(NAME,encoder=manager.encoder,
                                               render_density=manager.render_density)
                profile=planner.plan(request(portrait=True,viewport_points=PointSize(707,1024)))
                self.assertLessEqual(aspect_error(profile.stream_pixels.aspect,707/1024),.01)
                self.assertFalse((Path(home)/'.local').exists())


if __name__=='__main__':
    unittest.main()
