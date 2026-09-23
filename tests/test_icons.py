"""ICON-1. The host's icon theme, the XDG lookup, and `GET /v1/icons/{name}`.

Every tree these tests build mirrors a shape read off the real Omarchy host:
`Yaru-blue` inheriting `Yaru, Humanity, hicolor`, a `256x256/apps` directory
declared `Type=Scalable MinSize=64`, an icon that exists only in a theme nobody
selected, and a user `hicolor` with no `index.theme` of its own.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

import aiohttp
from omodachi_core.bootstrap import create_service
from omodachi_core.catalog import compile_catalog
from omodachi_core.hub import Hub
from omodachi_core.icons import HostIcons, IconsUnavailable, classify_icon
from omodachi_core.network import NetworkServer
from omodachi_core.service import CoreService, ServiceError

PNG = bytes.fromhex("89504e470d0a1a0a")
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"></svg>'


def write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


class IconKindTests(unittest.TestCase):
    """`icon_kind` names what the host actually put in `icon`."""

    def test_the_four_shapes_a_real_host_publishes(self):
        # Every value here was read off `omodachi-host catalog` on omarchy.
        self.assertEqual(classify_icon(""), "none")
        self.assertEqual(classify_icon("\U000f003b"), "glyph")      # Apps, a Nerd code point
        self.assertEqual(classify_icon("\U000f0249"), "glyph")   # Install, plane-15
        self.assertEqual(classify_icon("\u2713"), "glyph")       # style.font's tick
        self.assertEqual(classify_icon("\U0001f7e2"), "glyph")   # update.channel's emoji
        self.assertEqual(classify_icon("org.gnome.Nautilus"), "xdg")
        self.assertEqual(classify_icon("google-chrome"), "xdg")
        self.assertEqual(classify_icon("/opt/vendor/share/logo.png"), "path")

    def test_a_one_letter_name_is_a_name_and_not_a_code_point(self):
        """`apps.X` publishes `"x"`; drawing that as a glyph is the bug UX-1 saw."""
        self.assertEqual(classify_icon("x"), "xdg")
        self.assertEqual(classify_icon("7"), "xdg")

    def test_a_row_tagged_for_omarchys_private_face_is_a_glyph(self):
        self.assertEqual(classify_icon("\ue800", "omarchy"), "glyph")

    def test_every_catalog_row_carries_a_kind_and_an_untouched_icon(self):
        catalog = compile_catalog([
            {"id": "root", "label": "Go"},
            {"id": "apps", "label": "Apps", "icon": "\U000f003b"},
            {"id": "apps.org.gnome.Nautilus", "parent": "apps", "kind": "app",
             "appId": "org.gnome.Nautilus", "label": "Files", "icon": "org.gnome.Nautilus"},
            {"id": "omodachi", "label": "Omodachi", "icon": ""},
        ])
        rows = {row["id"]: row for row in catalog.as_dict()["entries"]}
        self.assertEqual(rows["apps"]["icon"], "\U000f003b")
        self.assertEqual(rows["apps"]["icon_kind"], "glyph")
        self.assertEqual(rows["apps.org.gnome.Nautilus"]["icon"], "org.gnome.Nautilus")
        self.assertEqual(rows["apps.org.gnome.Nautilus"]["icon_kind"], "xdg")
        self.assertEqual(rows["omodachi"]["icon_kind"], "none")


class IconThemeTests(unittest.TestCase):
    """Which theme, read the way Omarchy sets it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def icons(self, *, answer="'Yaru-blue'\n", environ=None):
        def runner(argv, **kwargs):
            if argv[0].endswith("gsettings"):
                return answer
            raise IconsUnavailable("icon_theme_unavailable")
        return HostIcons(runner=runner, home=self.home,
                         environ=environ if environ is not None else {"HOME": str(self.home)})

    def test_gsettings_answers_and_its_quotes_are_not_part_of_the_name(self):
        self.assertEqual(self.icons().theme_source(),
                         {"theme": "Yaru-blue", "source": "gsettings"})

    def test_a_gtk_settings_ini_answers_when_gsettings_does_not(self):
        settings = self.home / ".config/gtk-3.0/settings.ini"
        settings.parent.mkdir(parents=True)
        settings.write_text("[Settings]\ngtk-icon-theme-name=Papirus\n")
        source = self.icons(answer="").theme_source()
        self.assertEqual(source, {"theme": "Papirus", "source": "gtk-3.0/settings.ini"})

    def test_a_machine_that_says_nothing_falls_to_hicolor(self):
        self.assertEqual(self.icons(answer="").theme_source()["source"], "default")
        self.assertEqual(self.icons(answer="").theme_name(), "hicolor")

    def test_a_forged_theme_name_is_refused_rather_than_walked(self):
        self.assertEqual(self.icons(answer="'../../etc'\n").theme_name(), "hicolor")

    def test_base_dirs_are_the_xdg_ones_in_the_xdg_order(self):
        icons = self.icons(environ={"HOME": str(self.home), "XDG_DATA_DIRS": "/usr/local/share:/usr/share"})
        self.assertEqual([str(path) for path in icons.base_dirs()],
                         [str(self.home / ".local/share/icons"), str(self.home / ".icons"),
                          "/usr/local/share/icons", "/usr/share/icons"])


class IconLookupTests(unittest.TestCase):
    """The XDG lookup itself, on a tree shaped like the host's."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.share = self.root / "usr/share/icons"
        self.pixmaps = self.root / "usr/share/pixmaps"
        self.pixmaps.mkdir(parents=True)
        # Yaru-blue, exactly as /usr/share/icons/Yaru-blue/index.theme declares
        # the two directories these tests care about.
        write(self.share / "Yaru-blue/index.theme", b"""[Icon Theme]
Name=Yaru-blue
Inherits=Yaru,Humanity,hicolor
Directories=48x48/apps,256x256/apps,16x16/actions

[48x48/apps]
Context=Applications
Size=48
Type=Fixed

[256x256/apps]
Context=Applications
Size=256
MinSize=64
MaxSize=256
Type=Scalable

[16x16/actions]
Context=Actions
Size=16
Type=Fixed
""")
        write(self.share / "Yaru/index.theme", b"""[Icon Theme]
Name=Yaru
Inherits=Humanity,hicolor
Directories=48x48/apps,48x48/devices

[48x48/apps]
Context=Applications
Size=48
Type=Fixed

[48x48/devices]
Context=Devices
Size=48
Type=Fixed
""")
        write(self.share / "hicolor/index.theme", b"""[Icon Theme]
Name=hicolor
Directories=scalable/apps

[scalable/apps]
Context=Applications
Size=48
MinSize=8
MaxSize=512
Type=Scalable
""")

    def icons(self):
        return HostIcons(runner=lambda argv, **kwargs: "'Yaru-blue'\n", home=self.home,
                         pixmaps=self.pixmaps, rsvg_convert=None,
                         environ={"HOME": str(self.home),
                                  "XDG_DATA_DIRS": str(self.root / "usr/share")})

    def themed(self, name, size=48):
        """The themed chain on its own — what `lookup()` now *falls back* to.

        Every tree in this class files its icons under `*/apps/*` or
        `*/devices/*`, which is precisely what the host's flat index scans, so
        `lookup()` answers from the index and never reaches the size ranking
        these tests are about. That is the whole point of UX-2 §5, and it is
        also why the ranking has to be exercised here rather than through
        `lookup()`: on a real host almost nothing gets this far.
        """
        icons = self.icons()
        for theme_name in icons.theme_chain():
            found = icons._in_theme(theme_name, name, size, 1)
            if found is not None:
                return {"theme": theme_name, "path": found[0], "nominal": found[1]}
        return None

    def test_the_chain_is_the_theme_then_what_it_inherits_then_hicolor(self):
        self.assertEqual(self.icons().theme_chain(), ["Yaru-blue", "Yaru", "Humanity", "hicolor"])

    def test_a_theme_that_inherits_in_a_circle_still_terminates(self):
        write(self.share / "Loop/index.theme",
              b"[Icon Theme]\nInherits=Loop\nDirectories=48x48/apps\n\n"
              b"[48x48/apps]\nContext=Applications\nSize=48\nType=Fixed\n")
        icons = HostIcons(runner=lambda argv, **kwargs: "'Loop'\n", home=self.home,
                          pixmaps=self.pixmaps, rsvg_convert=None,
                          environ={"HOME": str(self.home), "XDG_DATA_DIRS": str(self.root / "usr/share")})
        self.assertEqual(icons.theme_chain(), ["Loop", "hicolor"])

    def test_a_scalable_directory_that_covers_the_size_wins_the_selected_theme(self):
        """The host's real answer for `org.gnome.Nautilus` at 64: the 256 file.

        `256x256/apps` is `Type=Scalable MinSize=64`, so it *matches* 64 and
        directory order decides — which is the XDG rule, and what GTK does.
        """
        write(self.share / "Yaru-blue/48x48/apps/org.gnome.Nautilus.png", PNG)
        write(self.share / "Yaru-blue/256x256/apps/org.gnome.Nautilus.png", PNG + b"big")
        found = self.themed("org.gnome.Nautilus", size=64)
        self.assertEqual(found["theme"], "Yaru-blue")
        self.assertTrue(str(found["path"]).endswith("Yaru-blue/256x256/apps/org.gnome.Nautilus.png"))

    def test_the_cheapest_sufficient_representation_wins_among_matches(self):
        """A 256 PNG and a 96 PNG both match 72; the 96 is the one that travels."""
        write(self.share / "Yaru-blue/index.theme", b"""[Icon Theme]
Inherits=hicolor
Directories=256x256/apps,96x96/apps,32x32/apps

[256x256/apps]
Context=Applications
Size=256
MinSize=64
MaxSize=256
Type=Scalable

[96x96/apps]
Context=Applications
Size=96
MinSize=64
MaxSize=96
Type=Scalable

[32x32/apps]
Context=Applications
Size=32
MinSize=8
MaxSize=256
Type=Scalable
""")
        for directory in ("256x256", "96x96", "32x32"):
            write(self.share / f"Yaru-blue/{directory}/apps/big.png", PNG + directory.encode())
        found = self.themed("big", size=72)
        self.assertTrue(str(found["path"]).endswith("96x96/apps/big.png"), found["path"])

    def test_a_vector_beats_every_bitmap_that_also_matches(self):
        write(self.share / "Yaru-blue/256x256/apps/both.png", PNG)
        write(self.share / "Yaru-blue/256x256/apps/both.svg", SVG)
        self.assertTrue(str(self.themed("both", size=72)["path"]).endswith(".svg"))

    def test_a_vector_beats_a_bitmap_that_is_exactly_the_size_asked_for(self):
        """The tie `_rank` used to lose: both scored `(0, 0)` and `.png` came first."""
        write(self.share / "Yaru-blue/48x48/apps/exact.png", PNG)
        write(self.share / "Yaru-blue/48x48/apps/exact.svg", SVG)
        self.assertTrue(str(self.themed("exact", size=48)["path"]).endswith(".svg"))

    def test_a_fixed_directory_is_taken_when_it_is_the_exact_size(self):
        write(self.share / "Yaru-blue/48x48/apps/foo.png", PNG)
        write(self.share / "Yaru-blue/256x256/apps/foo.png", PNG + b"big")
        found = self.themed("foo", size=48)
        self.assertTrue(str(found["path"]).endswith("48x48/apps/foo.png"))

    def test_the_selected_theme_beats_the_one_it_inherits(self):
        write(self.share / "Yaru-blue/48x48/apps/bar.png", PNG)
        write(self.share / "Yaru/48x48/apps/bar.png", PNG + b"yaru")
        self.assertEqual(self.themed("bar", size=48)["theme"], "Yaru-blue")

    def test_an_inherited_theme_answers_what_the_selected_one_has_not(self):
        write(self.share / "Yaru/48x48/apps/libreoffice-calc.png", PNG)
        self.assertEqual(self.themed("libreoffice-calc", size=48)["theme"], "Yaru")

    def test_a_devices_icon_is_reachable_because_desktop_files_use_them(self):
        """`apps.system-config-printer` publishes `printer`, a Devices icon."""
        write(self.share / "Yaru/48x48/devices/printer.png", PNG)
        self.assertEqual(self.themed("printer", size=48)["theme"], "Yaru")
        self.assertEqual(self.icons().lookup("printer", size=48)["source"], "index")

    def test_an_actions_icon_never_wins_an_application_name(self):
        """`AppLibrary.qml:63-65`'s own warning, as a rule rather than a comment."""
        write(self.share / "Yaru-blue/16x16/actions/zoom.png", PNG)
        with self.assertRaises(IconsUnavailable):
            self.icons().lookup("zoom", size=16)

    def test_a_user_theme_directory_without_an_index_is_still_part_of_the_theme(self):
        """`~/.local/share/icons/hicolor` has no `index.theme` on the host."""
        write(self.home / ".local/share/icons/hicolor/scalable/apps/com.omodachi.host.svg", SVG)
        found = self.themed("com.omodachi.host", size=64)
        self.assertEqual(found["theme"], "hicolor")
        self.assertTrue(str(found["path"]).startswith(str(self.home)))
        # And the index reaches the same user directory, which is the branch
        # that actually answers on the host.
        self.assertTrue(str(self.icons().lookup("com.omodachi.host", size=64)["path"])
                        .startswith(str(self.home)))

    def test_an_icon_only_a_theme_nobody_selected_has_is_still_answered(self):
        """`audio-input-microphone` lives in HighContrast on the host, not Yaru."""
        write(self.share / "HighContrast/scalable/devices/audio-input-microphone.svg", SVG)
        found = self.icons().lookup("audio-input-microphone", size=64)
        self.assertEqual(found["source"], "index")
        self.assertIsNone(found["theme"])

    def test_pixmaps_are_part_of_the_hosts_own_index(self):
        """`Alacritty` is `/usr/share/pixmaps/Alacritty.svg`, and the menu finds it.

        `iconIndexScanCommand()` ends each extension pass with `find
        /usr/share/pixmaps -maxdepth 1`, so a pixmap is an index hit and not a
        separate last resort.
        """
        write(self.pixmaps / "Alacritty.svg", SVG)
        found = self.icons().lookup("Alacritty", size=64)
        self.assertEqual(found["source"], "index")
        self.assertEqual(found["path"], self.pixmaps / "Alacritty.svg")

    def test_an_xpm_pixmap_is_the_one_thing_left_after_the_index_and_the_theme(self):
        """The host's scan only ever asks for `svg` and `png`; `.xpm` is ours."""
        write(self.pixmaps / "legacy.xpm", b"/* XPM */")
        self.assertEqual(self.icons().lookup("legacy", size=64)["source"], "pixmaps")

    def test_a_name_that_is_nowhere_is_a_miss_and_not_a_guess(self):
        with self.assertRaises(IconsUnavailable):
            self.icons().lookup("no-such-application", size=64)

    def test_a_name_that_is_not_a_name_never_reaches_the_file_system(self):
        for value in ("../../etc/shadow", "a/b", "", "\u0000", "." * 2):
            with self.subTest(value=value), self.assertRaises(IconsUnavailable):
                self.icons().lookup(value, size=64)

    def test_an_absolute_icon_path_is_served_only_from_a_data_root(self):
        inside = write(self.root / "usr/share/pixmaps/vendor.png", PNG)
        outside = write(self.root / "secrets/vendor.png", PNG)
        self.assertEqual(self.icons().lookup(str(inside))["source"], "path")
        with self.assertRaises(IconsUnavailable):
            self.icons().lookup(str(outside))

    def test_an_absolute_path_that_is_not_an_image_is_refused(self):
        secret = write(self.root / "usr/share/pixmaps/key.pem", b"-----BEGIN PRIVATE KEY-----")
        with self.assertRaises(IconsUnavailable):
            self.icons().lookup(str(secret))

    def test_a_symlink_out_of_a_data_root_is_refused_after_it_is_resolved(self):
        target = write(self.root / "secrets/real.png", PNG)
        link = self.root / "usr/share/pixmaps/escape.png"
        link.symlink_to(target)
        with self.assertRaises(IconsUnavailable):
            self.icons().lookup(str(link))


class HostIndexParityTests(unittest.TestCase):
    """UX-2 §5. The menu's picture and core's picture are the same file.

    `AppLibrary.iconSource()` asks `root.iconIndex` — a flat, unthemed,
    size-blind index of every `*/apps/*` and `*/devices/*` file plus
    `/usr/share/pixmaps` — *before* it asks Qt's themed lookup. Core used to
    ask the theme first, and on the real host that disagreed with the menu on
    13 of the 47 applications it shows. Every tree below is one of those
    disagreements, shrunk.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.share = self.root / "usr/share/icons"
        self.pixmaps = self.root / "usr/share/pixmaps"
        self.pixmaps.mkdir(parents=True)
        write(self.share / "Yaru-blue/index.theme", b"""[Icon Theme]
Name=Yaru-blue
Inherits=Yaru,hicolor
Directories=256x256/apps,256x256/devices,256x256/mimetypes

[256x256/apps]
Context=Applications
Size=256
MinSize=64
MaxSize=256
Type=Scalable

[256x256/devices]
Context=Devices
Size=256
MinSize=64
MaxSize=256
Type=Scalable

[256x256/mimetypes]
Context=MimeTypes
Size=256
MinSize=64
MaxSize=256
Type=Scalable
""")
        write(self.share / "hicolor/index.theme", b"""[Icon Theme]
Name=hicolor
Directories=scalable/apps,64x64/apps,128x128/apps

[scalable/apps]
Context=Applications
Size=48
MinSize=8
MaxSize=512
Type=Scalable

[64x64/apps]
Context=Applications
Size=64
Type=Fixed

[128x128/apps]
Context=Applications
Size=128
Type=Fixed
""")

    def icons(self):
        return HostIcons(runner=lambda argv, **kwargs: "'Yaru-blue'\n", home=self.home,
                         pixmaps=self.pixmaps, rsvg_convert=None,
                         environ={"HOME": str(self.home),
                                  "XDG_DATA_DIRS": str(self.root / "usr/share")})

    def test_the_flat_index_answers_before_the_selected_theme_does(self):
        """`org.gnome.Nautilus`: the grey GNOME folder, not the blue Yaru one.

        Both files exist on the host. The menu draws
        `hicolor/scalable/apps/org.gnome.Nautilus.svg` because the index is
        size-blind and puts every SVG before every PNG; core used to draw
        `Yaru-blue/256x256/apps/org.gnome.Nautilus.png` because the selected
        theme came first.
        """
        write(self.share / "Yaru-blue/256x256/apps/org.gnome.Nautilus.png", PNG + b"yaru")
        write(self.share / "hicolor/scalable/apps/org.gnome.Nautilus.svg", SVG)
        found = self.icons().lookup("org.gnome.Nautilus", size=64)
        self.assertEqual(found["source"], "index")
        self.assertEqual(found["path"], self.share / "hicolor/scalable/apps/org.gnome.Nautilus.svg")

    def test_the_index_is_size_blind_where_the_themed_lookup_was_not(self):
        """`chromium`: `hicolor/128x128`, because that is the line `find` printed first."""
        write(self.share / "hicolor/64x64/apps/chromium.png", PNG + b"64")
        write(self.share / "hicolor/128x128/apps/chromium.png", PNG + b"128")
        icons = self.icons()
        order = [entry.name for entry in os.scandir(self.share / "hicolor")]
        first = next(name for name in order if name in ("64x64", "128x128"))
        found = icons.lookup("chromium", size=64)
        self.assertEqual(found["source"], "index")
        self.assertEqual(found["path"], self.share / "hicolor" / first / "apps/chromium.png")

    def test_every_svg_anywhere_comes_before_every_png_anywhere(self):
        """`kdenlive`, `mpv`: an SVG in one theme beats a PNG in the selected one.

        The host's scan runs two whole passes, `svg` then `png`, over every
        base directory — so the preference crosses themes, which is exactly
        what a per-theme lookup can never reproduce.
        """
        write(self.share / "Yaru-blue/256x256/apps/kdenlive.png", PNG + b"yaru")
        write(self.share / "hicolor/scalable/apps/kdenlive.svg", SVG)
        found = self.icons().lookup("kdenlive", size=64)
        self.assertEqual(found["path"], self.share / "hicolor/scalable/apps/kdenlive.svg")

    def test_the_first_line_find_prints_wins_and_that_is_readdir_order(self):
        """Not a tie-break we chose: the host has none either, so neither do we."""
        for theme in ("Alpha", "Beta"):
            write(self.share / f"{theme}/48x48/apps/twice.png", PNG + theme.encode())
        order = [entry.name for entry in os.scandir(self.share)]
        first = next(name for name in order if name in ("Alpha", "Beta"))
        found = self.icons().lookup("twice", size=48)
        self.assertEqual(found["path"], self.share / first / "48x48/apps/twice.png")

    def test_the_walk_descends_the_moment_it_reaches_a_directory(self):
        """`find`'s order, which `os.walk` does not have.

        `find` tests a subdirectory and recurses into it before it looks at
        the next sibling; `os.walk` drains a directory first and descends
        after. With a first-wins index those two pick different files, so the
        walk is `find`'s and not `os.walk`'s.
        """
        from omodachi_core.icons import _find
        apps = self.share / "hicolor/48x48/apps"
        write(apps / "nested/dup.png", PNG)
        write(apps / "sibling.png", PNG)
        order = list(_find(str(self.share / "hicolor/48x48")))
        self.assertEqual(order.index(str(apps / "nested/dup.png")),
                         order.index(str(apps / "nested")) + 1)

    def test_the_index_only_looks_where_the_host_looks(self):
        """`*/apps/*` and `*/devices/*`, and nothing else — `zoom` stays an app."""
        write(self.share / "Yaru-blue/256x256/actions/zoom.png", PNG)
        with self.assertRaises(IconsUnavailable):
            self.icons().lookup("zoom", size=64)
        write(self.share / "Yaru-blue/256x256/devices/printer.svg", SVG)
        self.assertEqual(self.icons().lookup("printer", size=64)["source"], "index")

    def test_the_generic_icon_the_host_falls_back_to_is_one_core_can_answer(self):
        """`application-x-executable` is a `MimeTypes` icon, and Qt does not filter.

        Core filtered it out of every theme it parsed, so the one name its own
        404 tells the client about was the one name it could not serve.
        """
        from omodachi_core.icons import GENERIC_ICON
        write(self.share / f"Yaru-blue/256x256/mimetypes/{GENERIC_ICON}.png", PNG + b"generic")
        found = self.icons().lookup(GENERIC_ICON, size=64)
        self.assertEqual(found["source"], "theme")
        self.assertEqual(found["path"], self.share / f"Yaru-blue/256x256/mimetypes/{GENERIC_ICON}.png")

    def test_an_absolute_icon_path_still_skips_the_index_entirely(self):
        """`iconSource()` branch 3, and it comes before the index there too."""
        write(self.share / "hicolor/scalable/apps/vendor.svg", SVG + b"themed")
        exact = write(self.pixmaps / "vendor.png", PNG + b"exact")
        found = self.icons().lookup(str(exact))
        self.assertEqual(found["source"], "path")
        self.assertEqual(found["path"], exact.resolve())

    def test_a_file_url_names_the_same_file_the_menu_would_have_drawn(self):
        """`iconSource()` branch 2, percent-decoded the way `Util.fileUrl()` encodes."""
        target = write(self.pixmaps / "a name+sign.png", PNG)
        from urllib.parse import quote
        url = "file://" + quote(str(target))
        self.assertEqual(self.icons().lookup(url)["path"], target.resolve())

    def test_an_image_provider_url_is_a_miss_because_it_is_not_a_file(self):
        """`image://` lives inside the shell process; nothing can send its bytes."""
        with self.assertRaises(IconsUnavailable):
            self.icons().lookup("image://icon/whatever")

    def test_a_file_url_outside_a_data_root_is_refused_like_any_other_path(self):
        outside = write(self.root / "secrets/leak.png", PNG)
        with self.assertRaises(IconsUnavailable):
            self.icons().lookup("file://" + str(outside))

    def test_pixmaps_come_after_the_base_directories_inside_each_pass(self):
        """`find /usr/share/pixmaps` is the *last* line of each extension pass."""
        write(self.share / "hicolor/scalable/apps/twin.svg", SVG + b"theme")
        write(self.pixmaps / "twin.svg", SVG + b"pixmap")
        self.assertEqual(self.icons().lookup("twin", size=64)["path"],
                         self.share / "hicolor/scalable/apps/twin.svg")

    def test_a_pixmap_svg_still_beats_a_themed_png(self):
        """Because the whole `svg` pass, pixmaps included, runs before any `png`."""
        write(self.share / "hicolor/128x128/apps/mixed.png", PNG)
        write(self.pixmaps / "mixed.svg", SVG)
        self.assertEqual(self.icons().lookup("mixed", size=64)["path"], self.pixmaps / "mixed.svg")

    def test_the_index_is_reused_until_a_base_directory_moves(self):
        write(self.share / "hicolor/scalable/apps/first.svg", SVG)
        icons = self.icons()
        self.assertEqual(icons.lookup("first", size=64)["source"], "index")
        write(self.share / "NewTheme/scalable/apps/second.svg", SVG)
        # `self.share` is a base directory, and a new theme changes its mtime,
        # which is the cheap signal that retires the cache.
        self.assertEqual(icons.lookup("second", size=64)["source"], "index")

    def test_the_index_walks_where_the_host_walks_and_in_its_order(self):
        """`~/.icons` first, `$XDG_DATA_HOME` never — `iconIndexScanCommand()`'s own list."""
        icons = HostIcons(runner=lambda argv, **kwargs: "", home=self.home,
                          pixmaps=self.pixmaps, rsvg_convert=None,
                          environ={"HOME": str(self.home),
                                   "XDG_DATA_HOME": str(self.root / "elsewhere"),
                                   "XDG_DATA_DIRS": "/usr/local/share:" + str(self.root / "usr/share")})
        self.assertEqual([str(path) for path in icons.index_base_dirs()],
                         [str(self.home / ".icons"), str(self.home / ".local/share/icons"),
                          "/usr/local/share/icons", str(self.root / "usr/share/icons")])
        # `base_dirs()` is the XDG order and honours `$XDG_DATA_HOME`; the two
        # lists differ on purpose, because the host's two lookups differ.
        self.assertEqual(str(icons.base_dirs()[0]), str(self.root / "elsewhere/icons"))


class IconBytesTests(unittest.TestCase):
    """What actually travels, and the validator it travels with."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.share = self.root / "usr/share/icons"
        write(self.share / "hicolor/index.theme",
              b"[Icon Theme]\nDirectories=scalable/apps,48x48/apps\n\n"
              b"[scalable/apps]\nContext=Applications\nSize=48\nMinSize=8\nMaxSize=512\nType=Scalable\n\n"
              b"[48x48/apps]\nContext=Applications\nSize=48\nType=Fixed\n")

    def icons(self, rsvg=None):
        return HostIcons(runner=lambda argv, **kwargs: "", home=self.root / "home",
                         pixmaps=self.root / "usr/share/pixmaps", rsvg_convert=rsvg,
                         environ={"HOME": str(self.root / "home"),
                                  "XDG_DATA_DIRS": str(self.root / "usr/share")})

    def test_a_png_travels_as_itself_with_its_sha256_as_the_etag(self):
        path = write(self.share / "hicolor/48x48/apps/kitty.png", PNG + b"kitty")
        row = self.icons().render("kitty", size=48)
        self.assertEqual(row["bytes"], path.read_bytes())
        self.assertEqual(row["content_type"], "image/png")
        self.assertEqual(row["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_an_svg_travels_as_an_svg_when_the_host_cannot_rasterize(self):
        write(self.share / "hicolor/scalable/apps/foot.svg", SVG)
        row = self.icons().render("foot", size=64)
        self.assertEqual(row["content_type"], "image/svg+xml")
        self.assertEqual(row["bytes"], SVG)

    def test_a_rasterized_svg_does_not_share_a_validator_across_sizes(self):
        """@2x asks for 72 and @3x for 108; one ETag for both would be a lie."""
        write(self.share / "hicolor/scalable/apps/foot.svg", SVG)
        fake = self.root / "bin/rsvg-convert"
        fake.parent.mkdir(parents=True)
        fake.write_text("#!/bin/sh\nwhile [ $# -gt 0 ]; do case $1 in --output) out=$2;; --width) w=$2;; esac; shift; done\n"
                        "printf 'PNG%s' \"$w\" > \"$out\"\n")
        fake.chmod(0o755)
        icons = self.icons(rsvg=str(fake))
        small, large = icons.render("foot", size=72), icons.render("foot", size=108)
        self.assertEqual(small["content_type"], "image/png")
        self.assertEqual(small["bytes"], b"PNG72")
        self.assertEqual(large["bytes"], b"PNG108")
        self.assertNotEqual(small["sha256"], large["sha256"])
        self.assertTrue(small["sha256"].endswith("-png72"))

    def test_a_size_outside_the_bounds_is_refused_before_any_lookup(self):
        for size in (0, 7, 513, "64", 64.0):
            with self.subTest(size=size), self.assertRaises(IconsUnavailable):
                self.icons().render("kitty", size=size)


class IconServiceTests(unittest.TestCase):
    """The boundary: a miss is a 404 that tells the client what to draw."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write(self.root / "usr/share/icons/hicolor/index.theme",
              b"[Icon Theme]\nDirectories=48x48/apps\n\n"
              b"[48x48/apps]\nContext=Applications\nSize=48\nType=Fixed\n")
        write(self.root / "usr/share/icons/hicolor/48x48/apps/kitty.png", PNG + b"kitty")
        self.service = create_service(Hub(), demo=True)
        self.service.icons = HostIcons(
            runner=lambda argv, **kwargs: "", home=self.root / "home",
            pixmaps=self.root / "usr/share/pixmaps", rsvg_convert=None,
            environ={"HOME": str(self.root / "home"), "XDG_DATA_DIRS": str(self.root / "usr/share")})

    def test_a_known_name_answers_bytes_and_a_type(self):
        row = self.service.icon_file("kitty", "48")
        self.assertEqual(row["content_type"], "image/png")

    def test_a_miss_is_a_404_that_names_the_fallback(self):
        with self.assertRaises(ServiceError) as raised:
            self.service.icon_file("nothing-here", "48")
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(raised.exception.code, "icon_not_found")
        self.assertEqual(raised.exception.detail, {"fallback": "application"})

    def test_a_size_that_is_not_a_bounded_integer_is_a_400(self):
        for size in ("abc", "0", "4096", "-8", "6 4"):
            with self.subTest(size=size), self.assertRaises(ServiceError) as raised:
                self.service.icon_file("kitty", size)
            self.assertEqual(raised.exception.status, 400)

    def test_a_host_without_an_icon_theme_reader_answers_503(self):
        self.service.icons = None
        with self.assertRaises(ServiceError) as raised:
            self.service.icon_file("kitty", None)
        self.assertEqual(raised.exception.status, 503)


class FocusIconTests(unittest.TestCase):
    """ICON-1 §1: the bar's focus item needs a picture, not an `app_id`."""

    def service(self):
        from omodachi_core.catalog_runtime import CatalogRuntime
        catalog = compile_catalog([
            {"id": "root", "label": "Go"},
            {"id": "apps", "label": "Apps"},
            {"id": "apps.org.gnome.Nautilus", "parent": "apps", "kind": "app",
             "appId": "org.gnome.Nautilus", "label": "Files", "icon": "org.gnome.Nautilus"},
        ])
        hub = Hub()
        service = CoreService(hub, runtime=CatalogRuntime(catalog))
        service.runtime.refresh()
        return service

    def test_the_focused_window_carries_the_icon_its_desktop_entry_declares(self):
        service = self.service()
        service.set_workspace_snapshot(
            active=1, window_counts={1: 1},
            focused_window={"id": "0x1", "app_id": "org.gnome.Nautilus", "app_name": "Files"})
        focus = service.hub.state_view("focus")["focus"]
        self.assertEqual(focus["icon"], "org.gnome.Nautilus")
        self.assertEqual(focus["icon_kind"], "xdg")

    def test_the_match_ignores_the_case_the_compositor_reports(self):
        service = self.service()
        service.set_workspace_snapshot(
            active=1, window_counts={1: 1},
            focused_window={"id": "0x3", "app_id": "ORG.GNOME.NAUTILUS", "app_name": "Files"})
        self.assertEqual(service.hub.state_view("focus")["focus"]["icon"], "org.gnome.Nautilus")

    def test_an_unknown_focused_app_id_claims_no_icon(self):
        service = self.service()
        service.set_workspace_snapshot(
            active=1, window_counts={1: 1},
            focused_window={"id": "0x2", "app_id": "not.an.installed.app", "app_name": "Ghost"})
        focus = service.hub.state_view("focus")["focus"]
        self.assertEqual((focus["icon"], focus["icon_kind"]), ("", "none"))

    def test_nothing_focused_publishes_no_icon_either(self):
        service = self.service()
        service.set_workspace_snapshot(active=1, window_counts={1: 0})
        focus = service.hub.state_view("focus")["focus"]
        self.assertEqual((focus["icon"], focus["icon_kind"]), ("", "none"))


class IconRouteTests(unittest.IsolatedAsyncioTestCase):
    """`GET /v1/icons/{name}` over a real loopback server."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        write(self.root / "usr/share/icons/hicolor/index.theme",
              b"[Icon Theme]\nDirectories=48x48/apps\n\n"
              b"[48x48/apps]\nContext=Applications\nSize=48\nType=Fixed\n")
        self.file = write(self.root / "usr/share/icons/hicolor/48x48/apps/kitty.png", PNG + b"kitty")
        self.hub = Hub(auth_check_interval=0.05)
        self.token = self.hub.register_device("phone-a")
        self.service = create_service(self.hub, demo=True)
        self.service.icons = HostIcons(
            runner=lambda argv, **kwargs: "", home=self.root / "home",
            pixmaps=self.root / "usr/share/pixmaps", rsvg_convert=None,
            environ={"HOME": str(self.root / "home"), "XDG_DATA_DIRS": str(self.root / "usr/share")})
        self.server = NetworkServer(self.service, allow_loopback_http=True)
        await self.server.start()
        self.url = f"http://127.0.0.1:{self.server.bound_port}"
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))

    async def asyncTearDown(self):
        await self.client.close()
        await asyncio.wait_for(self.server.close(), 3)
        self.temp.cleanup()

    def headers(self, extra=None):
        return {"Authorization": "Bearer " + self.token} | (extra or {})

    async def test_an_icon_is_bytes_with_the_files_digest_as_its_etag(self):
        async with self.client.get(self.url + "/v1/icons/kitty?size=48",
                                   headers=self.headers()) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "image/png")
            body = await response.read()
            etag = response.headers["ETag"]
        self.assertEqual(body, self.file.read_bytes())
        self.assertEqual(etag, '"' + hashlib.sha256(self.file.read_bytes()).hexdigest() + '"')

    async def test_a_client_that_already_holds_the_bytes_is_answered_304(self):
        digest = hashlib.sha256(self.file.read_bytes()).hexdigest()
        async with self.client.get(self.url + "/v1/icons/kitty?size=48",
                                   headers=self.headers({"If-None-Match": '"' + digest + '"'})) as response:
            self.assertEqual(response.status, 304)
            self.assertEqual(await response.read(), b"")

    async def test_an_unknown_name_is_a_404_carrying_the_fallback(self):
        async with self.client.get(self.url + "/v1/icons/nothing-here",
                                   headers=self.headers()) as response:
            self.assertEqual(response.status, 404)
            body = await response.json()
        self.assertEqual(body["error"]["code"], "icon_not_found")
        self.assertEqual(body["error"]["detail"], {"fallback": "application"})

    async def test_the_route_is_behind_the_same_credential_as_every_other(self):
        async with self.client.get(self.url + "/v1/icons/kitty") as response:
            self.assertEqual(response.status, 401)

    async def test_the_only_query_this_route_takes_is_the_size(self):
        async with self.client.get(self.url + "/v1/icons/kitty?path=/etc/shadow",
                                   headers=self.headers()) as response:
            self.assertEqual(response.status, 400)

    async def test_a_traversal_in_the_name_is_a_miss_not_a_file(self):
        for name in ("..%2F..%2Fetc%2Fshadow", "%2Fetc%2Fshadow"):
            async with self.client.get(self.url + "/v1/icons/" + name,
                                       headers=self.headers()) as response:
                self.assertEqual(response.status, 404, name)
                self.assertEqual((await response.json())["error"]["code"], "icon_not_found")


if __name__ == "__main__":
    unittest.main()


class ForeignPreferenceTests(unittest.TestCase):
    """ICON-1's incident, not its feature: a store written by a newer build.

    On `omarchy` at 22:27 a build that knows a `biometric_auth` control had
    written the key, and a build that does not could no longer read the store:
    `validate_changes(..., complete=True)` refused it, `_read` raised
    `preferences_store_invalid`, and `omodachid` crash-restarted for ever (the
    `[Errno 9] Bad file descriptor` in the journal is the IPC server being
    closed on the way out, not the cause). A preference this build has never
    heard of is somebody else's control, not a corrupt store.
    """

    #: A control this build genuinely does not have. `biometric_auth` was the
    #: real one on the night, and it is a known control here now that AUTH-1
    #: has landed — which is the point: the next one will be a different name.
    FOREIGN = "holographic_projection"

    def store(self, values):
        from omodachi_core.preferences import DEFAULTS, HostPreferencesStore
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "state.json"
        path.write_text(json.dumps({"version": 2, "revision": 6, "values": values}))
        path.chmod(0o600)
        return HostPreferencesStore(path), DEFAULTS

    def test_a_preference_this_build_does_not_know_does_not_stop_it(self):
        from omodachi_core.preferences import DEFAULTS
        store, _ = self.store({**DEFAULTS, self.FOREIGN: False})
        view = store.get()
        self.assertEqual(view["revision"], 6)
        self.assertEqual(view["values"]["quality"], DEFAULTS["quality"])

    def test_the_foreign_preference_is_kept_exactly_as_it_was_written(self):
        from omodachi_core.preferences import DEFAULTS
        store, _ = self.store({**DEFAULTS, self.FOREIGN: True})
        store.set(expected_revision=6, changes={"quality": "quality"})
        self.assertEqual(json.loads(store.path.read_text())["values"][self.FOREIGN], True)

    def test_a_client_still_cannot_set_a_control_this_build_has_no_idea_about(self):
        from omodachi_core.preferences import DEFAULTS, PreferencesError
        store, _ = self.store(dict(DEFAULTS))
        with self.assertRaises(PreferencesError):
            store.set(expected_revision=6, changes={self.FOREIGN: True})

    def test_a_genuinely_broken_value_is_still_refused(self):
        from omodachi_core.preferences import DEFAULTS, PreferencesError
        store, _ = self.store({**DEFAULTS, "quality": "ludicrous"})
        with self.assertRaises(PreferencesError):
            store.get()

    def test_a_missing_control_is_still_backfilled_and_written_through(self):
        from omodachi_core.preferences import DEFAULTS
        partial = {key: value for key, value in DEFAULTS.items() if key != "voice_uplink"}
        store, _ = self.store({**partial, self.FOREIGN: False})
        self.assertEqual(store.get()["values"]["voice_uplink"], DEFAULTS["voice_uplink"])
        written = json.loads(store.path.read_text())["values"]
        self.assertIn("voice_uplink", written)
        self.assertIn(self.FOREIGN, written)
