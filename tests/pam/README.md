# AUTH-1 · the PAM harness

> AUTH-2 moved the daemon's socket to `/run/omodachi/<uid>/omodachid.sock`, and
> every case below now runs against it: nothing here passes `--socket` any more,
> so the daemon's own choice is what PAM is tested against. The sibling harness
> in `sandbox/` is the one that answers *why* it moved — it boots real systemd
> and puts the helper under `polkit-agent-helper@.service`'s own properties.

A real Arch container, a real PAM stack, a real `sudo`, a real `omodachid`, and
a fake iPad. Nothing is mocked except the tablet — and the tablet is only fake
in that its private key is an integer in a Python process instead of a Secure
Enclave, which is the one difference the host cannot see anyway: the host
verifies a P-256 signature and learns nothing about where it came from.

```sh
tests/pam/run.sh            # build the image and run every case
tests/pam/run.sh --shell    # a shell in the same container, to poke at it
```

On Apple Silicon this is `--platform linux/amd64` (there is no arm64
`archlinux` tag) and therefore emulated and slow; the first build is minutes,
a run is about two. `OMODACHI_PAM_PLATFORM` overrides the platform on a Linux
machine. Exit status is the number of failed assertions.

## Why a container and not a unit test

`tests/test_pam_install.py` covers the file surgery and every way the helper
refuses, against a `--root` sandbox and a stub socket. What it cannot cover is
the only question that matters: **does `sudo` actually let you in, and does the
password still work when it does not.** That needs `pam_exec.so` loading our
program out of a real `/etc/pam.d/sudo`, with a real setuid `sudo` around it.

It has already earned its keep twice:

* The rule was first written as `... /usr/local/bin/omodachi-pam --timeout 45
  # omodachi-auth`. Linux-PAM only treats `#` as a comment at the *start* of a
  line, so PAM handed the helper two extra arguments, the helper refused every
  prompt, and nothing in the unit tests could see it. The marker now lives on
  its own comment line, and both the harness and a unit test assert that the
  rule has no trailing text.
* The daemon's IPC gate hung up on any peer that was not its own uid, before
  reading the frame. PAM runs `pam_exec` as root, because `sudo` is setuid — so
  the helper got a broken pipe and the user got the password prompt for no
  reason. Root is now admitted exactly as far as naming `local.auth.approve`.

## The discriminator

Every case runs one `sudo` with **empty stdin** and a recognisable prompt
string:

```sh
sudo -k; sudo -S -p 'OMODACHI-PASSWORD-PROMPT:' /usr/bin/true </dev/null
```

If that string appears in the output, PAM fell through to the password, which
is the *correct* result for every case but the first. If it does not appear and
the exit status is 0, a device answered and no password was involved.

## What it asserts

| | |
|---|---|
| ground truth | unmodified `sudo` prompts; the password works |
| install | the rule lands above the first `auth`, with no trailing comment; the password still works with the entry installed and no daemon |
| **case 3** | no daemon → straight to the password, and the helper's own trace says why (it also proves `pam_exec` hands us `PAM_RUSER`) |
| both switches | host off + a willing device connected → password, nothing published, the device is never asked |
| both switches | host on + device switch off → password, the device is never asked |
| no audience | host on, key enrolled, nobody connected → password |
| **case 1** | both switches on, the device signs → `sudo` succeeds, no prompt, and PAM prints which device approved |
| **case 2a** | the device declines → password |
| **case 2b** | the device ignores it → the timeout elapses, then password |
| replay | the identical signed body submitted twice → the second is `404` |
| liveness | the password still works while all of this is live |
| **case 4** | `remove` restores `/etc/pam.d/sudo` byte for byte (sha256 + `cmp`), deletes the helper, the config and `/etc/omodachi` |
| case 5 | `polkit-1` has no `/etc` file on Arch: one is created from the vendor bytes, is the vendor stack plus exactly our two lines, and is deleted again on removal |
| **AUTH-2** | the daemon's own default is `/run/omodachi/<uid>/omodachid.sock`, `0700` directory and `0600` socket, and the root-owned `pam.conf` names that same path |
| AUTH-2 | `~/.cache/omodachi/omodachid.sock` is a symlink to it that answers, and that is gone once the daemon is |
| AUTH-2 | with no `/run/omodachi/<uid>` the daemon falls back to `$XDG_RUNTIME_DIR/omodachi/` and still starts |
| AUTH-2 | `polkit-1` in the service list brings the `tmpfiles.d` fragment and the `ReadWritePaths=` drop-in; `sudo` alone brings neither; a socket in the home or in `/run/user` brings neither, and takes an earlier one away |

## The pieces

* `Dockerfile` — Arch + `pam sudo polkit python`, an unprivileged `omodachi`
  user with a password, the checkout installed into a virtualenv, and the two
  directories the daemon chooses between (`/run/omodachi/1000`, made by
  `tmpfiles.d` on a real host, and `/run/user/1000`, made by logind).
* `cases.sh` — the cases above. It is the container's entrypoint.
* `fake_device.py` — pairs over the HTTP API, enrols a P-256 key with a
  proof-of-possession signature, holds a `/v1/events` WebSocket open, and
  answers approvals. `--behaviour approve|decline|ignore|replay` and
  `--device-enabled true|false` are what the cases turn.
* `run.sh` — build and run, from the checkout.
