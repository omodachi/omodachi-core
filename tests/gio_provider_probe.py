#!/usr/bin/env python3
"""Linux/Gio integration probe; isolated temporary XDG files, never real apps.

Run with system Python (the daemon venv intentionally need not import gi).
Temporary .desktop Exec entries all point to /usr/bin/true and are never launched.
"""
from pathlib import Path
import json
import os
import subprocess
import tempfile


def run_probe(module_source: str):
    checks = []
    with tempfile.TemporaryDirectory(prefix="omodachi-gio-provider-") as temporary:
        root = Path(temporary)
        module = root / "catalog_providers.py"
        module.write_text(module_source)
        home, userdata, sysa, sysb = [root / name for name in ("home", "userdata", "sysa", "sysb")]
        for directory in (home, userdata / "applications", sysa / "applications", sysb / "applications", root / "bin"):
            directory.mkdir(parents=True)
        executable = root / "bin" / "fixture-tryexec"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        hides = root / "launcher.hides"
        hides.write_text("FixtureHidden.desktop\n")
        def desktop(base, name, label, extra=""):
            path = base / "applications" / (name + ".desktop")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("[Desktop Entry]\nType=Application\nName=" + label + "\nExec=/usr/bin/true\n" + extra)
            return path
        desktop(sysb, "Priority", "Lower system")
        desktop(sysa, "Priority", "Higher system")
        desktop(userdata, "Priority", "User override")
        desktop(sysa, "HiddenOverride", "System visible")
        desktop(userdata, "HiddenOverride", "User tombstone", "Hidden=true\n")
        desktop(sysa, "NoDisplay", "Do not display", "NoDisplay=true\n")
        desktop(sysa, "OnlyHypr", "Only Hyprland", "OnlyShowIn=Hyprland;\n")
        desktop(sysa, "OnlyOther", "Only other", "OnlyShowIn=Other;\n")
        desktop(sysa, "NotHypr", "Not Hyprland", "NotShowIn=Hyprland;\n")
        desktop(sysa, "MissingTryExec", "Missing executable", "TryExec=fixture-does-not-exist\n")
        desktop(sysa, "PresentTryExec", "Present executable", "TryExec=fixture-tryexec\n")
        desktop(sysa, "Locale", "English fixture", "Name[fr]=Nom français\nName[fr_FR]=Nom régional\n")
        desktop(sysa, "nested/Fixture", "Nested fixture")
        desktop(sysb, "nested/Fixture", "Duplicate lower fixture")
        desktop(sysa, "Fixture App", "Space ID")
        desktop(sysa, "FixtureHidden", "Hidden by Omarchy")
        env = {"HOME": str(home), "XDG_DATA_HOME": str(userdata),
               "XDG_DATA_DIRS": str(sysa) + ":" + str(sysb), "XDG_CONFIG_HOME": str(root / "config"),
               "XDG_CURRENT_DESKTOP": "Hyprland", "LANG": "C.UTF-8", "LANGUAGE": "fr_FR:fr",
               "PATH": str(root / "bin") + ":/usr/bin:/bin"}
        command = '''import importlib.util,sys,json
s=importlib.util.spec_from_file_location("probe_scanner",sys.argv[1]);m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m)
print(json.dumps(m.scan_gio_apps(hides_path=sys.argv[2])))'''
        def scan():
            process = subprocess.run(["/usr/bin/python3", "-c", command, str(module), str(hides)],
                                     env=env, capture_output=True, text=True, timeout=5, check=True)
            data = json.loads(process.stdout)
            return {row["appId"]: row for row in data["apps"]}, data
        rows, data = scan()
        assert rows["Priority"]["label"] == "User override", rows
        checks.append("XDG_DATA_HOME_before_DATA_DIRS_and_first_system_directory")
        assert "HiddenOverride" not in rows and "NoDisplay" not in rows
        checks.append("Hidden_tombstone_overrides_system_and_NoDisplay_filters")
        assert "OnlyHypr" in rows and "OnlyOther" not in rows and "NotHypr" not in rows
        checks.append("OnlyShowIn_and_NotShowIn_use_active_desktop")
        assert "MissingTryExec" not in rows and "PresentTryExec" in rows
        checks.append("TryExec_missing_excluded_and_graphical_PATH_resolved")
        assert rows["Locale"]["label"] == "Nom régional", rows["Locale"]
        checks.append("locale_specific_Name_fr_FR_precedes_language_and_default")
        assert rows["nested-Fixture"]["label"] == "Nested fixture"
        assert len(data["apps"]) == len(rows)
        checks.append("nested_desktop_id_flattening_and_duplicates_stable")
        assert "Fixture App" in rows and "FixtureHidden" not in rows
        checks.append("space_ID_preserved_and_official_launcher_hides_applied")
        before = rows["Priority"]["appRevision"]
        path = userdata / "applications/Priority.desktop"
        path.write_text(path.read_text().replace("Exec=/usr/bin/true", "Exec=/usr/bin/false"))
        changed, _ = scan()
        assert changed["Priority"]["appRevision"] != before
        checks.append("Exec_only_change_changes_opaque_revision_without_exporting_Exec")
        path.unlink()
        changed, _ = scan()
        assert changed["Priority"]["label"] == "Higher system"
        checks.append("fresh_Gio_process_observes_removed_override_and_system_precedence")
        assert all(set(row) == {"appId", "appRevision", "label", "icon"} for row in changed.values())
        checks.append("scanner_schema_excludes_Exec_command_and_source_filename")
        return {"fixture": "temporary XDG tree; no launch, user config, credentials or real application data", "passed": checks}


if __name__ == "__main__":
    print(json.dumps(run_probe((Path(__file__).resolve().parents[1] / "src/omodachi_core/catalog_providers.py").read_text()), indent=2))
