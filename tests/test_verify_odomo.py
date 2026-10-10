"""LITE-1a C8: the committed Odomo build is reproducible from `odomo/`.

Runs `scripts/verify_odomo.py` (a clean `npm ci --ignore-scripts` + build in a
scratch tree, compared byte for byte). Without the pinned Node it is skipped,
and says why.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class VerifyOdomoTests(unittest.TestCase):
    def test_committed_build_matches_a_clean_build(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_odomo.py")],
                                capture_output=True, text=True, timeout=600)
        if result.returncode == 2:
            self.skipTest(result.stderr.strip() or "verify_odomo: toolchain unavailable")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("match a clean build byte for byte", result.stdout)

    def test_lock_and_npmrc_rules(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import verify_odomo
        finally:
            sys.path.pop(0)
        self.assertGreater(verify_odomo.check_lock(), 5)


if __name__ == "__main__":
    unittest.main()
