#!/usr/bin/env python3
"""Prove the committed Odomo build is what `odomo/` builds to.

LITE-1a C8. Core serves `src/omodachi_core/data/odomo/` (built, committed: the
host never runs a JavaScript toolchain). That only means something if anyone
can check those bytes came from the source next to them, so this script:

1. copies `odomo/` (without `node_modules`) into a scratch tree shaped like the
   repository,
2. installs the hash-locked toolchain there with `npm ci --ignore-scripts`
   (`package-lock.json` pins every package with its integrity hash; no
   dependency runs anything at install time),
3. builds it with that toolchain,
4. compares the result with the committed `data/odomo/` file by file, byte
   by byte - the same paths, the same sha256 - and fails on any difference.

    .venv/bin/python scripts/verify_odomo.py            # verify (exit 1 on drift)
    .venv/bin/python scripts/verify_odomo.py --write    # rebuild data/odomo in place

The pinned Node is `odomo/.node-version` (major must match; `engines` in
package.json says the same). Exit 2 means the toolchain is not available here,
which `tests/test_verify_odomo.py` turns into a skip with that message.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ODOMO = ROOT / "odomo"
BUILT = ROOT / "src" / "omodachi_core" / "data" / "odomo"
SKIP = 2


class Unavailable(RuntimeError):
    """No Node/npm of the pinned major here."""


def digest_tree(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def toolchain() -> dict[str, str]:
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        raise Unavailable("node/npm not found on PATH")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, check=True).stdout.strip()
    pinned = (ODOMO / ".node-version").read_text().strip()
    if version.lstrip("v").split(".")[0] != pinned.split(".")[0]:
        raise Unavailable(f"node {version} is not the pinned major of {pinned} (odomo/.node-version)")
    return {"node": node, "npm": npm, "version": version}


def check_lock():
    """No surprise in the lock: every package pinned with an integrity hash."""
    lock = json.loads((ODOMO / "package-lock.json").read_text())
    if lock.get("lockfileVersion") != 3:
        raise ValueError("odomo/package-lock.json must be lockfileVersion 3")
    for name, row in lock["packages"].items():
        if not name:
            continue
        if not row.get("integrity", "").startswith("sha512-"):
            raise ValueError(f"{name} has no sha512 integrity in the lock")
        if not row.get("resolved", "").startswith("https://registry.npmjs.org/"):
            raise ValueError(f"{name} resolves outside registry.npmjs.org")
    npmrc = (ODOMO / ".npmrc").read_text().split()
    if "ignore-scripts=true" not in npmrc:
        raise ValueError("odomo/.npmrc must say ignore-scripts=true")
    return len(lock["packages"]) - 1


def build(tree: Path, tools) -> None:
    env = {**os.environ, "npm_config_ignore_scripts": "true",
           "npm_config_audit": "false", "npm_config_fund": "false", "npm_config_update_notifier": "false"}
    env.pop("NODE_ENV", None)  # `npm ci` would leave out the devDependencies, which are all of them
    subprocess.run([tools["npm"], "ci", "--include=dev", "--ignore-scripts", "--no-audit", "--no-fund", "--prefer-offline"],
                   cwd=tree, env=env, check=True, stdout=subprocess.DEVNULL)
    subprocess.run([tools["node"], "node_modules/vite/bin/vite.js", "build", "--logLevel", "warn"],
                   cwd=tree, env=env, check=True)


def verify(*, write=False, keep=False) -> dict:
    tools = toolchain()
    packages = check_lock()
    if write:
        build(ODOMO, tools)
        return {"written": len(digest_tree(BUILT)), "packages": packages, "node": tools["version"]}
    scratch = Path(tempfile.mkdtemp(prefix="verify-odomo-"))
    try:
        shutil.copytree(ODOMO, scratch / "odomo", ignore=shutil.ignore_patterns("node_modules", ".DS_Store"))
        (scratch / "src" / "omodachi_core" / "data").mkdir(parents=True)
        build(scratch / "odomo", tools)
        fresh = digest_tree(scratch / "src" / "omodachi_core" / "data" / "odomo")
        committed = digest_tree(BUILT)
        if not fresh:
            raise ValueError("the build produced nothing")
        missing = sorted(set(fresh) - set(committed))
        extra = sorted(set(committed) - set(fresh))
        changed = sorted(path for path in set(fresh) & set(committed) if fresh[path] != committed[path])
        if missing or extra or changed:
            raise ValueError("data/odomo is not what odomo/ builds to: "
                             + json.dumps({"missing": missing, "extra": extra, "changed": changed})
                             + " - run scripts/verify_odomo.py --write and commit the result")
        return {"files": len(fresh), "packages": packages, "node": tools["version"],
                "digest": hashlib.sha256(json.dumps(fresh, sort_keys=True).encode()).hexdigest()[:16]}
    finally:
        if keep:
            print(f"kept {scratch}", file=sys.stderr)
        else:
            shutil.rmtree(scratch, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--write", action="store_true", help="rebuild src/omodachi_core/data/odomo in place")
    parser.add_argument("--keep", action="store_true", help="keep the scratch tree for inspection")
    args = parser.parse_args(argv)
    try:
        result = verify(write=args.write, keep=args.keep)
    except Unavailable as error:
        print(f"verify_odomo: SKIP - {error}", file=sys.stderr)
        return SKIP
    if args.write:
        print(f"Odomo: rebuilt {result['written']} files with node {result['node']} ({result['packages']} locked packages).")
    else:
        print(f"Odomo: {result['files']} committed files match a clean build byte for byte "
              f"(node {result['node']}, {result['packages']} locked packages, tree {result['digest']}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
