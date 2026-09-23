#!/usr/bin/env python3
"""Put one line in `/etc/pam.d/<service>`, and be able to take it out again.

The whole contract of this module is reversibility. Before a service file is
touched, its exact bytes are copied to `/etc/omodachi/pam-backup/<service>.orig`
(or, for a service that had no `/etc` file at all, a `.absent` marker is written
instead), and `remove()` puts the file back byte for byte or deletes it. The
unit tests assert the byte equality, and so does the Docker harness.

`system-auth` is never touched. Only the per-service files are, and only by
inserting a marker comment and one rule above the first `auth` rule:

    # omodachi-auth (AUTH-1) ...
    auth      sufficient   pam_exec.so quiet stdout /usr/local/bin/omodachi-pam --timeout 45

The marker lives on its own line and never at the end of the rule. Linux-PAM
only treats `#` as a comment at the *start* of a line: a trailing `# marker`
is parsed as two more arguments and handed to the program, which then refuses
every prompt because it does not recognise them. That cost an afternoon in the
Docker harness, and the harness now asserts the rule has no trailing comment.

`sufficient` is the whole mechanism: the helper exiting 0 ends the auth stack
successfully, and the helper exiting non-zero - which is every failure it has -
falls straight through to the rules that were already there. Nothing is removed
from the stack, so the password never stops working.

AUTH-2 adds two files outside `/etc/pam.d`, and they are both about one unit.
`polkit-agent-helper@.service` runs the PAM stack with `ProtectHome=yes` and
`ProtectSystem=strict`; AUTH-1 found the hard way that the helper therefore
could not see the daemon's socket in `/home`, so every polkit prompt fell
through to the password. `ProtectHome=yes` blanks `/run/user` as well, with
systemd's *inaccessible* mount, under which nothing can be mounted - so no
drop-in can reach a socket in either place. The socket has to be somewhere
outside `/home`, `/root` and `/run/user` instead. So:

* `/etc/tmpfiles.d/omodachi.conf` creates `/run/omodachi` (root, 0755) and
  `/run/omodachi/<uid>` (the owner, 0700) at every boot. The daemon binds
  there; a user cannot make that directory itself, which is why this is part
  of the opt-in root step rather than the daemon's own business.
* `/etc/systemd/system/polkit-agent-helper@.service.d/60-omodachi.conf` says
  `ReadWritePaths=` for that one directory. `ProtectSystem=strict` leaves the
  path visible and connectable already, so this is a declaration of the unit's
  dependency rather than the thing that makes it work - the location is. It is
  one line, it is auditable with `systemctl cat`, and it is what keeps this
  working if read-only ever stops being enough.

Both are written only when the configured socket is under `/run/omodachi`,
both are removed by `remove()`, and both ends run `systemctl daemon-reload`.

Everything takes a `root` prefix so the same code runs against a container, a
temporary directory in a unit test, and `/`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

MARKER = "omodachi-auth"
HELPER_PATH = "usr/local/bin/omodachi-pam"
CONFIG_PATH = "etc/omodachi/pam.conf"
BACKUP_DIR = "etc/omodachi/pam-backup"
MANIFEST = "manifest.json"
PAM_DIR = "etc/pam.d"
VENDOR_PAM_DIR = "usr/lib/pam.d"

# The services this is allowed to be pointed at. `sudo` and `polkit-1` are the
# two password prompts a desktop actually produces. The lock screens are listed
# because they are the same kind of prompt, not because they are installed by
# default: approving your own screen unlock from a tablet lying next to the
# laptop is a different trade than approving a sudo, and it is opt-in.
KNOWN_SERVICES = ("sudo", "polkit-1", "hyprlock", "omarchy-lock-password", "su")
DEFAULT_SERVICES = ("sudo", "polkit-1")
DEFAULT_TIMEOUT = 45
_SERVICE = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")

# AUTH-2. The unit whose sandbox hid the socket, and the drop-in that hands one
# directory of it back. The number picks the file's place in the drop-in sort
# order; the name is what makes it findable and removable.
POLKIT_HELPER_UNIT = "polkit-agent-helper@.service"
DROPIN_DIR = "etc/systemd/system/" + POLKIT_HELPER_UNIT + ".d"
DROPIN_NAME = "60-omodachi.conf"
# The service whose prompt this drop-in is for. No polkit in the service list,
# no drop-in and no shared directory: they would be root-owned files granting a
# permission nothing uses.
POLKIT_SERVICE = "polkit-1"
# The tmpfiles fragment that makes the socket directory a user cannot make.
TMPFILES_PATH = "etc/tmpfiles.d/omodachi.conf"
SHARED_RUNTIME_ROOT = "/run/omodachi"


class PamInstallError(RuntimeError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def _validate(services):
    names = tuple(dict.fromkeys(services))
    if not names:
        raise PamInstallError("pam_no_services", "at least one PAM service is required")
    for name in names:
        if not _SERVICE.fullmatch(name) or name not in KNOWN_SERVICES:
            raise PamInstallError("pam_unknown_service", f"refusing unknown PAM service {name!r}")
        if name in {"system-auth", "system-login", "system-local-login", "system-remote-login"}:
            raise PamInstallError("pam_shared_stack", "the shared stacks are never edited")
    return names


def helper_source() -> Path:
    return Path(__file__).resolve().parent / "pam_helper.py"


MARKER_COMMENT = (f"# {MARKER} (AUTH-1): a paired device may answer this prompt. "
                  "Remove with install_host.py --remove-pam.")


def pam_line(timeout=DEFAULT_TIMEOUT, helper="/" + HELPER_PATH) -> str:
    """The rule this installs. Kept in one place so the report can quote it."""
    return f"auth      sufficient   pam_exec.so quiet stdout {helper} --timeout {int(timeout)}"


def _ours(row: str) -> bool:
    """Lines this installer owns: its marker comment and its own rule."""
    return MARKER in row or Path(HELPER_PATH).name in row


class PamInstaller:
    def __init__(self, root="/"):
        self.root = Path(root)

    # -- paths ------------------------------------------------------------
    def path(self, relative) -> Path:
        return self.root / relative

    def service_file(self, service) -> Path:
        return self.path(PAM_DIR) / service

    def vendor_file(self, service) -> Path:
        return self.path(VENDOR_PAM_DIR) / service

    def backup_file(self, service) -> Path:
        return self.path(BACKUP_DIR) / (service + ".orig")

    def absent_marker(self, service) -> Path:
        return self.path(BACKUP_DIR) / (service + ".absent")

    def manifest_file(self) -> Path:
        return self.path(BACKUP_DIR) / MANIFEST

    def dropin_file(self) -> Path:
        return self.path(DROPIN_DIR) / DROPIN_NAME

    def tmpfiles_file(self) -> Path:
        return self.path(TMPFILES_PATH)

    # -- primitives -------------------------------------------------------
    @staticmethod
    def _write(path: Path, data: bytes, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name("." + path.name + ".omodachi-new")
        with open(temporary, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _ensure_backup(self, service) -> str:
        """Record the original once. A second install never overwrites it."""
        target, backup, absent = self.service_file(service), self.backup_file(service), self.absent_marker(service)
        if backup.exists() or absent.exists():
            return "kept"
        backup.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            self._write(backup, target.read_bytes(), 0o600)
            return "saved"
        self._write(absent, b"this service had no /etc/pam.d file before AUTH-1\n", 0o600)
        return "absent"

    def _seed(self, service) -> str:
        """Give a vendor-only service an `/etc` file to edit.

        `polkit-1` on Arch ships as `/usr/lib/pam.d/polkit-1` and has no `/etc`
        copy; PAM reads `/etc/pam.d` first and falls back to `/usr/lib/pam.d`.
        Creating the `/etc` copy from the vendor bytes shadows the vendor file
        with an identical stack, and `remove()` deletes the copy so the vendor
        file is in charge again.
        """
        target = self.service_file(service)
        if target.exists():
            return "present"
        vendor = self.vendor_file(service)
        if not vendor.exists():
            raise PamInstallError("pam_service_missing", f"no PAM file for {service!r}")
        self._write(target, vendor.read_bytes(), 0o644)
        return "seeded-from-vendor"

    @staticmethod
    def _insert(text, line):
        """Put the marker comment and `line` above the first `auth` rule."""
        kept = [row for row in text.splitlines(keepends=True) if not _ours(row)]
        index = next((i for i, row in enumerate(kept) if re.match(r"^-?auth\b", row.strip())), None)
        if index is None:
            index = next((i for i, row in enumerate(kept)
                          if row.strip() and not row.lstrip().startswith("#")), len(kept))
        if kept and not kept[-1].endswith("\n"):
            kept[-1] += "\n"
        kept[index:index] = [MARKER_COMMENT + "\n", line + "\n"]
        return "".join(kept)

    # -- the runtime directory and the polkit drop-in (AUTH-2) ------------
    @staticmethod
    def shared_socket_dir(socket) -> str:
        """`/run/omodachi/<uid>` for a socket that lives in it, else ``""``."""
        directory = os.path.dirname(str(socket))
        parent, _, leaf = directory.rpartition("/")
        return directory if parent == SHARED_RUNTIME_ROOT and leaf else ""

    @staticmethod
    def tmpfiles_body(socket, owner) -> str:
        """The whole file, so the report and the tests can quote one source."""
        directory = PamInstaller.shared_socket_dir(socket)
        return (f"# {MARKER} (AUTH-2). The daemon's local socket has to sit where a systemd\n"
                f"# sandbox can see it: {POLKIT_HELPER_UNIT} runs the PAM stack with\n"
                "# ProtectHome=yes, which blanks /home, /root AND /run/user with an\n"
                "# inaccessible mount - nothing can be mounted back under one, so no drop-in\n"
                "# reaches a socket in any of the three. /run/omodachi is outside all of them.\n"
                "# The parent is root's so no other user can plant a directory here; the\n"
                "# per-uid one is 0700 and the socket inside it is 0600.\n"
                "# Written by install_host.py --pam; removed by --remove-pam.\n"
                f"d {SHARED_RUNTIME_ROOT} 0755 root root -\n"
                f"d {directory} 0700 {owner} {owner} -\n")

    @staticmethod
    def dropin_body(socket) -> str:
        directory = PamInstaller.shared_socket_dir(socket)
        return (f"# {MARKER} (AUTH-2): this unit runs the PAM stack with ProtectHome=yes and\n"
                "# ProtectSystem=strict. What makes the socket reachable at all is that it\n"
                "# is outside every path ProtectHome= blanks, not this line. What this line\n"
                "# does is state the one directory the unit needs, instead of depending on\n"
                "# how strict /run happens to be here - which is not a stable thing to lean\n"
                "# on: ProtectSystem=strict on its own makes /run read-only, and this unit's\n"
                "# ProtectControlGroups= and ProtectKernelTunables= each undo that again\n"
                "# (measured on systemd 257 and 261 alike). A connect() only ever needed the\n"
                "# path visible. One directory, and `systemctl cat` shows who asked for it.\n"
                "# The leading '-' means an absent directory (no daemon) is not a reason for\n"
                "# the helper to fail to start.\n"
                "# Remove with install_host.py --remove-pam.\n"
                "[Service]\n"
                f"ReadWritePaths=-{directory}\n")

    def _run(self, argv, label) -> str:
        """A root-only side effect that is never allowed to be fatal.

        Under `--root` (a test directory, or a container with no service
        manager) there is nothing to run and nothing to report but that.
        """
        if str(self.root) != "/":
            return "skipped_root_prefix"
        if shutil.which(argv[0]) is None:
            return "no_" + argv[0]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            return "failed:" + type(error).__name__
        return label if result.returncode == 0 else "failed:%d" % result.returncode

    def _daemon_reload(self) -> str:
        return self._run(["systemctl", "daemon-reload"], "reloaded")

    def _tmpfiles_create(self) -> str:
        return self._run(["systemd-tmpfiles", "--create", str(self.tmpfiles_file())], "created")

    def install_runtime_dir(self, socket, owner, reason=None) -> dict:
        """Write the tmpfiles fragment and make the directory now."""
        directory = self.shared_socket_dir(socket)
        if reason or not directory:
            # An install that does not need this file must not leave an earlier
            # one behind: a reinstall pointed somewhere else is exactly how a
            # root-owned grant outlives the thing that asked for it.
            return {"written": False, "reason": reason or "socket_not_in_shared_runtime_root",
                    "path": str(self.tmpfiles_file()), "socket_dir": os.path.dirname(str(socket)),
                    **self.remove_runtime_dir()}
        body = self.tmpfiles_body(socket, owner).encode()
        target = self.tmpfiles_file()
        changed = not target.exists() or target.read_bytes() != body
        if changed:
            self._write(target, body, 0o644)
        return {"written": True, "changed": changed, "path": str(target),
                "directory": directory, "tmpfiles": self._tmpfiles_create()}

    def install_dropin(self, socket, reason=None) -> dict:
        """Write the drop-in, or say exactly why there is none."""
        directory = self.shared_socket_dir(socket)
        if reason or not directory:
            # A socket in the home or in /run/user cannot be reached from inside
            # ProtectHome=yes at all, whatever this file said. AUTH-1's state.
            # An earlier drop-in goes with it, for the same reason as above.
            return {"written": False, "reason": reason or "socket_not_in_shared_runtime_root",
                    "path": str(self.dropin_file()), "socket_dir": os.path.dirname(str(socket)),
                    **self.remove_dropin()}
        body = self.dropin_body(socket).encode()
        target = self.dropin_file()
        changed = not target.exists() or target.read_bytes() != body
        if changed:
            self._write(target, body, 0o644)
        return {"written": True, "changed": changed, "path": str(target),
                "socket_dir": directory, "unit": POLKIT_HELPER_UNIT,
                "daemon_reload": self._daemon_reload() if changed else "unchanged"}

    def remove_dropin(self) -> dict:
        target = self.dropin_file()
        present = target.exists()
        if present:
            target.unlink()
        directory = self.path(DROPIN_DIR)
        pruned = False
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
            pruned = True
        return {"removed": present, "path": str(target), "directory_pruned": pruned,
                "daemon_reload": self._daemon_reload() if present else "unchanged"}

    def remove_runtime_dir(self) -> dict:
        """Take the fragment away. The directory itself goes at the next boot.

        Removing `/run/omodachi/<uid>` here would pull the socket out from
        under a daemon that is running on it, which is a worse outcome than a
        directory that outlives its fragment until the machine restarts.
        """
        target = self.tmpfiles_file()
        present = target.exists()
        if present:
            target.unlink()
        return {"removed": present, "path": str(target)}

    # -- operations -------------------------------------------------------
    def install(self, *, owner, socket, services=DEFAULT_SERVICES, timeout=DEFAULT_TIMEOUT,
                debug=None) -> dict:
        names = _validate(services)
        if not owner or "/" in owner or len(owner) > 64:
            raise PamInstallError("pam_owner_invalid")
        if not str(socket).startswith("/"):
            raise PamInstallError("pam_socket_invalid")
        timeout = int(timeout)
        if not 5 <= timeout <= 120:
            raise PamInstallError("pam_timeout_invalid")

        helper = self.path(HELPER_PATH)
        body = helper_source().read_bytes()
        self._write(helper, body, 0o755)

        config = ("# Written by omodachi install_host.py --pam. Root-owned on purpose:\n"
                  "# the PAM path must not have a user-writable component in it.\n"
                  f"owner={owner}\n"
                  f"socket={socket}\n"
                  f"services={','.join(names)}\n"
                  f"timeout={timeout}\n"
                  + (f"debug={debug}\n" if debug else ""))
        self._write(self.path(CONFIG_PATH), config.encode(), 0o644)

        line = pam_line(timeout)
        installed = []
        for service in names:
            # Order matters: record what was there *before* a vendor-only
            # service is given an /etc copy, or the copy becomes the "original"
            # and `remove()` would restore our own shadow file forever.
            backup = self._ensure_backup(service)
            seeded = self._seed(service)
            target = self.service_file(service)
            before = target.read_text(encoding="utf-8", errors="surrogateescape")
            after = self._insert(before, line)
            changed = after != before
            if changed:
                self._write(target, after.encode("utf-8", errors="surrogateescape"), 0o644)
            installed.append({"service": service, "file": str(target), "backup_state": backup,
                              "seed": seeded, "changed": changed})

        # AUTH-2. Only for polkit, and only for a socket in /run/omodachi;
        # every other shape gets the reason instead of a root-owned file.
        skipped = None if POLKIT_SERVICE in names else "polkit_not_configured"
        runtime = self.install_runtime_dir(socket, owner, skipped)
        dropin = self.install_dropin(socket, skipped)

        manifest = {"version": 2, "installed_at": int(time.time()), "owner": owner,
                    "socket": str(socket), "timeout": timeout, "services": list(names),
                    "helper": str(helper), "helper_sha256": hashlib.sha256(body).hexdigest(),
                    "line": line, "dropin": dropin["path"] if dropin.get("written") else None,
                    "tmpfiles": runtime["path"] if runtime.get("written") else None}
        self._write(self.manifest_file(), (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(), 0o600)
        return {"installed": True, "services": installed, "line": line, "manifest": manifest,
                "config": str(self.path(CONFIG_PATH)), "helper": str(helper),
                "backup_dir": str(self.path(BACKUP_DIR)), "dropin": dropin,
                "runtime_dir": runtime}

    def remove(self, *, keep_backups=False) -> dict:
        """Put every touched file back exactly as it was."""
        manifest = {}
        try:
            manifest = json.loads(self.manifest_file().read_text())
        except (OSError, ValueError):
            manifest = {}
        services = manifest.get("services") or []
        if not services:
            # No manifest (or a broken one) is not a reason to leave a line in
            # a PAM file: fall back to every service we could have touched.
            def touched(name):
                if self.backup_file(name).exists() or self.absent_marker(name).exists():
                    return True
                try:
                    return any(_ours(row) for row in
                               self.service_file(name).read_text(errors="surrogateescape").splitlines())
                except OSError:
                    return False
            services = [name for name in KNOWN_SERVICES if touched(name)]
        restored = []
        for service in services:
            target, backup, absent = self.service_file(service), self.backup_file(service), self.absent_marker(service)
            if backup.exists():
                self._write(target, backup.read_bytes(), 0o644)
                how = "restored"
            elif absent.exists():
                if target.exists():
                    target.unlink()
                how = "removed"
            elif target.exists():
                # No record of the original. Strip our own line and nothing else.
                text = target.read_text(errors="surrogateescape")
                stripped = "".join(row for row in text.splitlines(keepends=True) if not _ours(row))
                if stripped != text:
                    self._write(target, stripped.encode("utf-8", errors="surrogateescape"), 0o644)
                how = "stripped"
            else:
                how = "absent"
            if not keep_backups:
                for path in (backup, absent):
                    if path.exists():
                        path.unlink()
            restored.append({"service": service, "file": str(target), "how": how})
        dropin = self.remove_dropin()
        runtime = self.remove_runtime_dir()
        for relative in (HELPER_PATH, CONFIG_PATH):
            path = self.path(relative)
            if path.exists():
                path.unlink()
        if not keep_backups and self.manifest_file().exists():
            self.manifest_file().unlink()
        directory = self.path(BACKUP_DIR)
        if not keep_backups and directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
        parent = self.path("etc/omodachi")
        if not keep_backups and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
        return {"removed": True, "services": restored, "dropin": dropin, "runtime_dir": runtime}

    def status(self) -> dict:
        try:
            manifest = json.loads(self.manifest_file().read_text())
        except (OSError, ValueError):
            manifest = None
        rows = []
        for service in (manifest or {}).get("services") or KNOWN_SERVICES:
            target = self.service_file(service)
            text = target.read_text(errors="surrogateescape") if target.exists() else ""
            rows.append({"service": service, "file": str(target), "present": target.exists(),
                         "line_installed": any(_ours(row) for row in text.splitlines()),
                         "backup": self.backup_file(service).exists() or self.absent_marker(service).exists()})
        helper = self.path(HELPER_PATH)
        dropin = self.dropin_file()
        return {"installed": bool(manifest) and any(row["line_installed"] for row in rows),
                "helper": str(helper), "helper_present": helper.exists(),
                "config": str(self.path(CONFIG_PATH)), "config_present": self.path(CONFIG_PATH).exists(),
                "dropin": str(dropin), "dropin_present": dropin.exists(),
                "tmpfiles": str(self.tmpfiles_file()), "tmpfiles_present": self.tmpfiles_file().exists(),
                "manifest": manifest, "services": rows}

    def verify_restored(self) -> dict:
        """Byte-compare every kept backup against the live file. For the report."""
        rows = []
        for backup in sorted(self.path(BACKUP_DIR).glob("*.orig")):
            service = backup.name[: -len(".orig")]
            target = self.service_file(service)
            rows.append({"service": service, "identical": target.exists()
                         and target.read_bytes() == backup.read_bytes()})
        for marker in sorted(self.path(BACKUP_DIR).glob("*.absent")):
            service = marker.name[: -len(".absent")]
            rows.append({"service": service, "identical": not self.service_file(service).exists()})
        return {"checked": len(rows), "all_identical": all(row["identical"] for row in rows), "services": rows}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="omodachi-pam-install", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("install", "remove", "status", "verify-restored"))
    parser.add_argument("--root", default="/", help="prefix every path (containers and tests)")
    parser.add_argument("--owner", help="the Unix user whose omodachid answers, e.g. alex")
    parser.add_argument("--socket", help="that user's omodachid socket path")
    parser.add_argument("--services", default=",".join(DEFAULT_SERVICES))
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--keep-backups", action="store_true")
    parser.add_argument("--debug-log", help="write one line per helper run here (diagnosis only)")
    args = parser.parse_args(argv)

    installer = PamInstaller(args.root)
    if args.root == "/" and os.geteuid() != 0 and args.action in {"install", "remove"}:
        print(json.dumps({"ok": False, "error": "pam_root_required"}))
        return 1
    try:
        if args.action == "install":
            if not args.owner or not args.socket:
                parser.error("install needs --owner and --socket")
            result = installer.install(owner=args.owner, socket=args.socket,
                                       services=[name for name in args.services.split(",") if name],
                                       timeout=args.timeout, debug=args.debug_log)
        elif args.action == "remove":
            result = installer.remove(keep_backups=args.keep_backups)
        elif args.action == "verify-restored":
            result = installer.verify_restored()
        else:
            result = installer.status()
    except (PamInstallError, OSError) as error:
        print(json.dumps({"ok": False, "error": getattr(error, "code", "pam_install_failed"),
                          "message": str(error)}))
        return 1
    print(json.dumps({"ok": True, "result": result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
