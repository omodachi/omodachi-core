# AUTH-2 · the sandbox harness

Real systemd, the real sandboxing properties of `polkit-agent-helper@.service`
(copied byte for byte off omarchy — `sha256 e950c316…`, Arch polkit 127-3), the
real PAM helper, a real `omodachid`, and the same fake iPad the AUTH-1 harness
next door uses.

```sh
tests/pam/sandbox/run.sh            # build, boot systemd, run every case
tests/pam/sandbox/run.sh --shell    # leave the container up and shell into it
```

Exit status is the number of failed assertions.

## What it is for

AUTH-1 shipped a PAM factor that worked for `su` and could never work for
polkit, because that unit runs the stack with `ProtectHome=yes` and the daemon's
socket was in `/home`. The AUTH-1 harness beside this one cannot answer the
follow-up question, because it has no service manager at all: **what do those
unit properties do to the helper, and where does the socket have to live?**

The answer it produced, and which decided AUTH-2's design:

| socket at | seen from inside the unit's sandbox | helper |
| --- | --- | --- |
| `~/.cache/omodachi/omodachid.sock` | gone (`ProtectHome=yes`) | refuses |
| `$XDG_RUNTIME_DIR/omodachi/omodachid.sock` | **also gone** — `ProtectHome=yes` blanks `/run/user` too | refuses |
| `/run/omodachi/<uid>/omodachid.sock` | there, read-only | **approves** |

And, in the same run, that no drop-in rescues the middle row:
`ReadWritePaths=-/run/user/<uid>/omodachi`, `ReadWritePaths=/run/user/`,
`BindPaths=`, `BindReadOnlyPaths=` and `ProtectHome=tmpfs` all leave it gone.
systemd mounts those three paths *inaccessible* and drops every mount it was
asked to make underneath one. That is why the socket moved out of the runtime
directory rather than the drop-in being made wider.

It also shows the reverse: from `/run/omodachi/<uid>` the helper approves *with
or without* the `ReadWritePaths=` drop-in, because connecting to a unix socket
only needs the path to be visible. The drop-in is therefore a declaration of the
unit's dependency — visible in `systemctl cat` — and not the mechanism. The
location is the mechanism, and the harness says so out loud.

How strict `/run` even is inside that sandbox turns out not to be a stable thing
to lean on, which is the other reason the drop-in stays. `ProtectSystem=strict`
on its own makes `/run` read-only — but this unit also sets
`ProtectControlGroups=` and `ProtectKernelTunables=`, and each of those undoes
that remount again, so under the unit's real property set `/run` is *writable*
to the helper before any drop-in exists. Measured the same way on systemd 257
here and 261 on omarchy, and bisected property by property on the host. The
harness prints the row it measures rather than asserting a value.

## How it runs the unit's properties

`cases.sh` parses `[Service]` out of the unit file and passes every directive to
`systemd-run -p`, dropping only the ones that describe *what* is run and how it
is wired to its socket (`ExecStart`, `Type`, `SuccessExitStatus`, `Standard*`).
Section 0 prints the list it ended up with, so the properties under test are on
screen rather than implied. `SuccessExitStatus=2` is not carried, so a helper
exit code means what it means everywhere else.

## Why Debian and not Arch

This image boots systemd as PID 1. `archlinux:latest` is amd64 only, so on an
Apple Silicon Mac that is a qemu-emulated systemd boot per run. The properties
under test are systemd's, not the distribution's, and the unit file is the
host's own bytes, so the only thing Debian contributes is the systemd version —
which section 0 prints (257 here; omarchy runs 261). The host acceptance in
`docs/specs/AUTH-2-report.md` is the same experiment on the real 261 and the
real polkit.

The PAM entry here is `polkit-1` only: Debian's `polkitd` ships
`/usr/lib/pam.d/polkit-1`, the same vendor-file-with-no-`/etc`-copy shape Arch
has, and `sudo` is not installed because `sudo` is the AUTH-1 harness's job.

## It needs `--privileged`, and that has a side effect

systemd as PID 1 needs `--privileged` and the host cgroup tree. The AUTH-1
harness deliberately has neither and keeps testing PAM and `sudo` with no extra
capabilities at all.

**On this Mac, running this container removes Docker's `qemu-x86_64` binfmt
handler**, so the next `tests/pam/run.sh` fails to build with
`exec /bin/sh: exec format error`. Reproduced twice. Put it back with:

```sh
docker run --rm --privileged tonistiigi/binfmt --install amd64
```

Running the AUTH-1 harness first, or reinstalling the handler between the two,
avoids the surprise.
