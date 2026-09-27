#!/usr/bin/env python3
"""The root-owned store of device keys the PAM helper will accept (RELEASE-9 B1).

Before RELEASE-9 the helper that `pam_exec` runs as root trusted whatever the
owner's daemon said, and the daemon checked signatures against a key file in
the owner's home. Anything running as the owner could therefore answer a sudo
prompt. The keys now live here, where only root can write them:

    /etc/omodachi/pam/keys.json      root 0600  the public keys the helper verifies with
    /etc/omodachi/pam/enrolled.json  root 0644  device id, label, key fingerprint - no key -
                                                so the daemon and the panel can say which
                                                devices are enrolled without reading the store

Nothing but this program writes them, and it only runs as root: from
`install_host.py --pam` (which enrols the keys the owner's devices have
registered, in the same sudo step) and `install_host.py --pam-enroll` /
`--pam-unenroll`, each of which asks for the owner's password in the terminal
they are run from. So adding a device that can answer a password prompt costs
the password once, and a process that only has the owner's uid cannot do it.

What is enrolled is shown before it is written (label, device id, the key's
sha256 fingerprint), because the key comes from the owner's own files and the
person typing the password is the check that it is the key they meant.

Like pam_install.py this imports nothing outside the standard library, so root
can run it as a plain file under `/usr/bin/python3 -I`.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import unicodedata

STORE_DIR = "etc/omodachi/pam"
KEYS_NAME = "keys.json"
INDEX_NAME = "enrolled.json"
CONFIG_PATH = "etc/omodachi/pam.conf"
MAX_KEYS = 32
_DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HOST_ID = re.compile(r"[0-9a-f]{32}\Z")
_OWNER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\$?\Z")
_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B


class EnrollError(RuntimeError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def _point(raw: bytes) -> bytes:
    """The 65-byte X9.63 point, from that or its SPKI wrapping, checked on the curve."""
    if len(raw) == 91 and raw.startswith(_SPKI_PREFIX):
        raw = raw[len(_SPKI_PREFIX):]
    if len(raw) != 65 or raw[0] != 0x04:
        raise EnrollError("pam_key_invalid", "not an uncompressed P-256 public key")
    x, y = int.from_bytes(raw[1:33], "big"), int.from_bytes(raw[33:], "big")
    if not (0 <= x < _P and 0 <= y < _P) or (y * y - (x * x * x - 3 * x + _B)) % _P or (x, y) == (0, 0):
        raise EnrollError("pam_key_invalid", "public key is not on the curve")
    return raw


def fingerprint(raw: bytes) -> str:
    """Same string as pam_helper.key_fingerprint: `sha256:` + hex of the point."""
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def clean_label(value) -> str:
    """A label that is safe to print into a terminal and into the PAM prompt."""
    if not isinstance(value, str):
        return "device"
    kept = "".join(" " if c.isspace() else c for c in value
                   if c.isspace() or unicodedata.category(c) not in {"Cc", "Cf", "Cs", "Co", "Cn"})
    kept = " ".join(kept.split())[:64]
    return kept or "device"


def parse_request(data) -> tuple[str, str, list[dict]]:
    """`(owner, host_id, rows)` from what the user-side installer hands root.

    Everything is re-validated here: the input comes from the owner's files,
    which is exactly the thing this store exists not to trust blindly.
    """
    if not isinstance(data, dict):
        raise EnrollError("pam_enroll_invalid")
    owner, host_id, keys = data.get("owner"), data.get("host_id"), data.get("keys")
    if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
        raise EnrollError("pam_enroll_invalid", "owner")
    if not isinstance(host_id, str) or not _HOST_ID.fullmatch(host_id):
        raise EnrollError("pam_enroll_invalid", "host_id")
    if not isinstance(keys, list) or len(keys) > MAX_KEYS:
        raise EnrollError("pam_enroll_invalid", "keys")
    rows, seen = [], set()
    for row in keys:
        if not isinstance(row, dict):
            raise EnrollError("pam_enroll_invalid", "key row")
        device_id = row.get("device_id")
        if not isinstance(device_id, str) or not _DEVICE_ID.fullmatch(device_id) or device_id in seen:
            raise EnrollError("pam_enroll_invalid", "device_id")
        seen.add(device_id)
        try:
            raw = _point(base64.b64decode(row.get("public_key") or "", validate=True))
        except (binascii.Error, ValueError, TypeError):
            raise EnrollError("pam_key_invalid") from None
        rows.append({"device_id": device_id, "public_key": base64.b64encode(raw).decode("ascii"),
                     "label": clean_label(row.get("label")),
                     "secure_enclave": row.get("secure_enclave") is True,
                     "fingerprint": fingerprint(raw)})
    return owner, host_id, rows


def collect_request(config_dir, owner, devices=None) -> dict:
    """The user side: what the owner's devices have registered, for root to check.

    Reads `host-id` and `biometric-keys.json` from the owner's config directory
    (the daemon writes both). Only keys whose device has its own switch on are
    offered, and `devices` narrows it further. Nothing here is trusted by root:
    `PamKeyStore.enroll` validates it all again, and the person typing the
    password is shown every row first.
    """
    config_dir = Path(config_dir)
    try:
        host_id = (config_dir / "host-id").read_text(encoding="utf-8").strip()
    except OSError:
        raise EnrollError("pam_host_id_missing", "the daemon has not written host-id yet") from None
    try:
        data = json.loads((config_dir / "biometric-keys.json").read_text(encoding="utf-8"))
        registered = data["keys"]
        if not isinstance(registered, dict):
            raise ValueError
    except FileNotFoundError:
        registered = {}
    except (OSError, ValueError, KeyError, TypeError):
        raise EnrollError("pam_user_keys_invalid", str(config_dir / "biometric-keys.json")) from None
    keys = []
    for device_id, row in sorted(registered.items()):
        if not isinstance(row, dict) or row.get("enabled") is not True:
            continue
        if devices and device_id not in devices:
            continue
        keys.append({"device_id": device_id, "public_key": row.get("public_key"),
                     "label": row.get("label"), "secure_enclave": row.get("secure_enclave") is True})
    return {"owner": owner, "host_id": host_id, "keys": keys}


def describe(request) -> list[str]:
    """One line per key, for the terminal the password is about to be typed in."""
    _owner, _host_id, rows = parse_request(request)
    return [f"  {row['label']}  (device {row['device_id']}, key {row['fingerprint'][:23]}…, "
            f"{'Secure Enclave' if row['secure_enclave'] else 'keychain - no Secure Enclave'})"
            for row in rows]


class PamKeyStore:
    def __init__(self, root="/"):
        self.root = Path(root)

    @property
    def directory(self) -> Path:
        return self.root / STORE_DIR

    @property
    def keys_file(self) -> Path:
        return self.directory / KEYS_NAME

    @property
    def index_file(self) -> Path:
        return self.directory / INDEX_NAME

    @staticmethod
    def _write(path: Path, data: bytes, mode: int) -> None:
        temporary = path.with_name("." + path.name + ".omodachi-new")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), mode)
        try:
            os.fchmod(fd, mode)
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)

    def configured_owner(self) -> str | None:
        try:
            text = (self.root / CONFIG_PATH).read_text(encoding="utf-8")
        except OSError:
            return None
        for line in text.splitlines():
            key, _, value = line.strip().partition("=")
            if key.strip() == "owner":
                return value.strip() or None
        return None

    def load(self) -> dict:
        try:
            data = json.loads(self.keys_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise EnrollError("pam_store_invalid", str(self.keys_file)) from None
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("keys"), dict):
            raise EnrollError("pam_store_invalid", str(self.keys_file))
        return data

    def _save(self, owner: str, host_id: str, keys: dict) -> None:
        self.directory.mkdir(mode=0o755, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o755)
        document = {"version": 1, "owner": owner, "host_id": host_id, "keys": keys}
        self._write(self.keys_file, (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(), 0o600)
        index = {"version": 1, "owner": owner, "host_id": host_id,
                 "devices": {device_id: {"fingerprint": row["fingerprint"], "label": row["label"],
                                         "enrolled_at": row["enrolled_at"]}
                             for device_id, row in keys.items()}}
        self._write(self.index_file, (json.dumps(index, indent=2, sort_keys=True) + "\n").encode(), 0o644)

    def enroll(self, request, *, invoker=None) -> dict:
        owner, host_id, rows = parse_request(request)
        configured = self.configured_owner()
        if configured is None:
            raise EnrollError("pam_not_installed", "run install_host.py --pam first")
        if configured != owner:
            raise EnrollError("pam_owner_mismatch", "the PAM entry is configured for %r" % configured)
        if invoker is not None and invoker != owner:
            # sudo says who asked. Only the owner enrols keys for the owner's prompts.
            raise EnrollError("pam_owner_mismatch", "enrolment was asked for by %r" % invoker)
        current = self.load()
        keys = dict(current.get("keys") or {}) if current.get("owner") == owner and current.get("host_id") == host_id else {}
        now = int(time.time())
        for row in rows:
            keys[row["device_id"]] = {"public_key": row["public_key"], "label": row["label"],
                                      "secure_enclave": row["secure_enclave"],
                                      "fingerprint": row["fingerprint"], "enrolled_at": now}
        if len(keys) > MAX_KEYS:
            raise EnrollError("pam_store_full")
        self._save(owner, host_id, keys)
        return {"enrolled": [{"device_id": row["device_id"], "label": row["label"],
                              "fingerprint": row["fingerprint"]} for row in rows],
                "devices": sorted(keys), "store": str(self.keys_file)}

    def unenroll(self, device_ids=None) -> dict:
        current = self.load()
        if not current:
            return {"removed": [], "devices": []}
        keys = dict(current["keys"])
        removed = sorted(keys) if device_ids is None else [d for d in device_ids if d in keys]
        for device_id in removed:
            keys.pop(device_id, None)
        if keys:
            self._save(current["owner"], current["host_id"], keys)
        else:
            self.remove()
        return {"removed": removed, "devices": sorted(keys)}

    def listing(self) -> dict:
        current = self.load()
        return {"owner": current.get("owner"), "host_id": current.get("host_id"),
                "devices": [{"device_id": device_id, "label": row.get("label"),
                             "fingerprint": row.get("fingerprint"), "enrolled_at": row.get("enrolled_at")}
                            for device_id, row in sorted((current.get("keys") or {}).items())]}

    def remove(self) -> dict:
        """Delete the two files this program writes, and the directory if that empties it."""
        removed = []
        for path in (self.keys_file, self.index_file):
            if path.is_file() and not path.is_symlink():
                path.unlink()
                removed.append(str(path))
        for path in self.directory.glob(".*.omodachi-new") if self.directory.is_dir() else ():
            path.unlink()
        pruned = False
        if self.directory.is_dir() and not any(self.directory.iterdir()):
            self.directory.rmdir()
            pruned = True
        return {"removed": removed, "directory_pruned": pruned}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="omodachi-pam-enroll", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("enroll", "unenroll", "keys"))
    parser.add_argument("--root", default="/")
    parser.add_argument("--keys-json", help="enroll: {owner, host_id, keys: [...]}")
    parser.add_argument("--device", action="append", help="unenroll: this device (repeatable)")
    parser.add_argument("--all", action="store_true", help="unenroll: every device")
    args = parser.parse_args(argv)
    store = PamKeyStore(args.root)
    if args.root == "/" and os.geteuid() != 0 and args.action in {"enroll", "unenroll"}:
        print(json.dumps({"ok": False, "error": "pam_root_required"}))
        return 1
    try:
        if args.action == "enroll":
            try:
                request = json.loads(args.keys_json or "")
            except ValueError:
                raise EnrollError("pam_enroll_invalid") from None
            result = store.enroll(request, invoker=os.environ.get("SUDO_USER") if args.root == "/" else None)
        elif args.action == "unenroll":
            if not args.all and not args.device:
                parser.error("unenroll needs --device or --all")
            result = store.unenroll(None if args.all else args.device)
        else:
            result = store.listing()
    except (EnrollError, OSError) as error:
        print(json.dumps({"ok": False, "error": getattr(error, "code", "pam_enroll_failed"),
                          "message": str(error)}))
        return 1
    print(json.dumps({"ok": True, "result": result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
