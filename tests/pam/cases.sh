#!/bin/bash
# The four AUTH-1 Docker cases, plus the two things that must be true around
# them: the password never stops working, and a refusal is fast.
#
# Every case prints what it asserted and why it passed. The discriminator is
# always the same: a `sudo` run with EMPTY STDIN and a recognisable prompt
# string. If the prompt string shows up in stderr, PAM fell through to the
# password, which is the correct behaviour for every case but the first.
set -uo pipefail

OWNER=omodachi
HOME_DIR=/home/$OWNER
# AUTH-2 moved the socket. Nothing below passes --socket to omodachid: the
# daemon's own default is what the PAM config, the systemd unit and the polkit
# drop-in all have to agree with, so the harness makes it prove itself.
# /run/omodachi/<uid> is the installed-host path (see tests/pam/sandbox for why
# it is not $XDG_RUNTIME_DIR); the runtime directory is the fallback.
SHARED_DIR=/run/omodachi/1000
SOCKET=$SHARED_DIR/omodachid.sock
FALLBACK_SOCKET=/run/user/1000/omodachi/omodachid.sock
LEGACY_SOCKET=$HOME_DIR/.cache/omodachi/omodachid.sock
DROPIN=/etc/systemd/system/polkit-agent-helper@.service.d/60-omodachi.conf
TMPFILES=/etc/tmpfiles.d/omodachi.conf
PROMPT='OMODACHI-PASSWORD-PROMPT:'
FAILURES=0
DAEMON_PID=
DEVICE_PID=

say() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILURES=$((FAILURES+1)); }
check() { if [ "$1" = "$2" ]; then pass "$3 ($1)"; else fail "$3: expected [$2] got [$1]"; fi; }

as_owner() { runuser -u "$OWNER" -- bash -lc "$1"; }

# One sudo attempt with no password available. Prints "rc=<n> secs=<n>" and
# writes the combined output to $2.
attempt() {
  local label="$1" out="$2" start end rc
  start=$(date +%s%N)
  runuser -u "$OWNER" -- bash -lc "sudo -k; sudo -S -p '$PROMPT' /usr/bin/true" \
      </dev/null >"$out" 2>&1
  rc=$?
  end=$(date +%s%N)
  printf '  %s: rc=%d elapsed=%d.%03ds\n' "$label" "$rc" "$(( (end-start)/1000000000 ))" "$(( ((end-start)/1000000)%1000 ))"
  sed 's/^/    | /' "$out"
  return $rc
}

prompted() { grep -q "$PROMPT" "$1"; }

# The failed attempts above are real failures as far as `pam_faillock` is
# concerned (deny=10 in Arch's system-auth), and this script makes a lot of
# them. Resetting before each "the password still works" check keeps the
# harness from proving faillock rather than proving AUTH-1. Note that a
# successful device approval never reaches faillock at all: our `sufficient`
# line sits above the whole `system-auth` include.
password_works() {
  faillock --user "$OWNER" --reset >/dev/null 2>&1
  runuser -u "$OWNER" -- bash -lc "sudo -k; echo hunter2 | sudo -S -p '' /usr/bin/true" >"$1" 2>&1
}

trace_tail() {
  [ -f /tmp/pam-helper.log ] && tail -n "${1:-4}" /tmp/pam-helper.log | sed 's/^/    ~ /'
}

start_daemon() {
  runuser -u "$OWNER" -- bash -lc \
    "omodachid --secret-file $HOME_DIR/.config/omodachi/device.secret \
       --listen 127.0.0.1 --port 8099 --allow-loopback-http --demo --discovery off" \
    >/tmp/daemon.log 2>&1 &
  DAEMON_PID=$!
  for _ in $(seq 1 60); do
    [ -S "$SOCKET" ] && grep -q '"ready": *true' /tmp/daemon.log && return 0
    sleep 0.5
  done
  echo "daemon did not start"; cat /tmp/daemon.log; return 1
}

stop_daemon() { [ -n "$DAEMON_PID" ] && kill "$DAEMON_PID" 2>/dev/null; wait "$DAEMON_PID" 2>/dev/null; DAEMON_PID=; }

start_device() {
  local behaviour="$1" id="$2" enabled="${3:-true}"
  rm -f /tmp/device-ready
  runuser -u "$OWNER" -- bash -lc \
    "python /opt/harness/fake_device.py --behaviour $behaviour --device-id $id \
       --device-enabled $enabled \
       --name 'Docker iPad' --base http://127.0.0.1:8099" \
    >/tmp/device-$id.log 2>&1 &
  DEVICE_PID=$!
  for _ in $(seq 1 80); do
    grep -q '"step": "subscribed"' /tmp/device-$id.log && return 0
    sleep 0.5
  done
  echo "device did not subscribe"; cat /tmp/device-$id.log; return 1
}

stop_device() { [ -n "$DEVICE_PID" ] && kill "$DEVICE_PID" 2>/dev/null; wait "$DEVICE_PID" 2>/dev/null; DEVICE_PID=; }

# ---------------------------------------------------------------------------
say "0 · the ground truth before anything is installed"
cp /etc/pam.d/sudo /tmp/sudo.before
sha_before_sudo=$(sha256sum /etc/pam.d/sudo | cut -d' ' -f1)
note "/etc/pam.d/sudo sha256 = $sha_before_sudo"
cat /etc/pam.d/sudo | sed 's/^/    | /'
note "/etc/pam.d/polkit-1 exists: $([ -f /etc/pam.d/polkit-1 ] && echo yes || echo 'no (vendor file at /usr/lib/pam.d/polkit-1)')"
if attempt "sudo with no password" /tmp/case0.log; then
  fail "sudo succeeded with no password before install"
else
  prompted /tmp/case0.log && pass "unmodified sudo asks for the password" || fail "no prompt seen"
fi
if password_works /tmp/case0b.log
then pass "the password itself works"; else fail "the password did not work before install"; sed 's/^/    | /' /tmp/case0b.log; fi

# ---------------------------------------------------------------------------
say "1 · install the PAM entry"
python -m omodachi_core.pam_install install --owner "$OWNER" --socket "$SOCKET" \
    --services sudo --timeout 10 --debug-log /tmp/pam-helper.log
echo
note "/etc/pam.d/sudo after:"
sed 's/^/    | /' /etc/pam.d/sudo
grep -q 'omodachi-pam --timeout 10$' /etc/pam.d/sudo \
  && pass "the rule ends at its own arguments (PAM has no trailing comments)" \
  || fail "the rule has trailing text PAM would hand to the helper as arguments"
note "/etc/omodachi/pam.conf:"
sed 's/^/    | /' /etc/omodachi/pam.conf
ls -l /usr/local/bin/omodachi-pam /etc/omodachi/pam.conf | sed 's/^/    | /'
grep -q "^socket=$SOCKET$" /etc/omodachi/pam.conf \
  && pass "the root-owned config names the shared runtime socket" \
  || fail "the config does not name $SOCKET"
# AUTH-2: both root-owned extras are for polkit's sandbox. This install is
# sudo only, so neither of them may appear.
[ -e "$DROPIN" ] \
  && fail "a polkit drop-in was written for a service list without polkit-1" \
  || pass "no polkit in the service list, no drop-in"
[ -e "$TMPFILES" ] \
  && fail "a tmpfiles fragment was written for a service list without polkit-1" \
  || pass "and no tmpfiles fragment either"

say "1b · the password still works with the entry installed and no daemon"
if password_works /tmp/case1b.log
then pass "the password still works"; else fail "the password stopped working"; sed 's/^/    | /' /tmp/case1b.log; fi

# ---------------------------------------------------------------------------
say "CASE 3 (first, because it needs no daemon) · daemon absent -> straight to the password"
attempt "sudo with no daemon running" /tmp/case3.log
rc=$?
check "$rc" "1" "sudo failed rather than succeeding"
prompted /tmp/case3.log && pass "fell through to the password prompt" || fail "no password prompt"
note "what the helper traced (this is also the record of what pam_exec hands it):"
trace_tail 3
# PAM_RUSER is load-bearing: it is the only thing that says the person driving
# a `sudo` is the owner whose iPad may answer, rather than some other account.
grep -q "ruser='$OWNER'" /tmp/pam-helper.log \
  && pass "pam_exec hands the helper PAM_RUSER=$OWNER" \
  || fail "PAM_RUSER is not the invoking user; the owner check needs another source"
grep -q "No such file or directory" /tmp/pam-helper.log \
  && pass "the refusal was 'there is no daemon', decided locally and instantly" \
  || fail "the refusal was not the missing socket"

# ---------------------------------------------------------------------------
say "daemon up"
start_daemon || exit 1
grep -o '"ready": true.*' /tmp/daemon.log | head -1 | sed 's/^/    | /'

# ---------------------------------------------------------------------------
say "AUTH-2 · the socket is in /run/omodachi, and the old path still answers"
note "the daemon was given no --socket at all; this is its own default:"
grep -o '"socket": "[^"]*"' /tmp/daemon.log | head -1 | sed 's/^/    | /'
grep -q "\"socket\": \"$SOCKET\"" /tmp/daemon.log \
  && pass "the default socket is $SOCKET" \
  || fail "the daemon did not bind the runtime socket"
ls -ld "$SHARED_DIR" "$SOCKET" "$LEGACY_SOCKET" | sed 's/^/    | /'
check "$(stat -c '%a %U' "$SHARED_DIR")" "700 $OWNER" "the socket directory is private to the owner"
check "$(stat -c '%a %U' "$SOCKET")" "600 $OWNER" "the socket is private to the owner"
check "$(readlink "$LEGACY_SOCKET")" "$SOCKET" "the pre-AUTH-2 path is a symlink to it"
# The whole reason the symlink exists: something still holding the old path.
as_owner "omodachi-host --socket $LEGACY_SOCKET health" >/tmp/legacy-health.log 2>&1
grep -q '"ok": true' /tmp/legacy-health.log \
  && pass "a client using the old path reaches the same daemon" \
  || { fail "the old path did not answer"; sed 's/^/    | /' /tmp/legacy-health.log; }
# And the reason it is not left behind: a dangling symlink looks like a daemon.

say "BOTH SWITCHES · host off + device on -> the password prompt"
note "the host preference is off by default; nothing the device does can change that"
as_owner "omodachi-host preferences get" | sed 's/^/    | /'
start_device approve ipad-hostoff true || exit 1
attempt "sudo with biometric_auth off and a willing device connected" /tmp/case-off.log
prompted /tmp/case-off.log && pass "an unconfigured host is an unchanged host" || fail "no password prompt"
grep -q '"reason":"disabled"' /tmp/daemon.log \
  && pass "the host switch refused before anything was published" || fail "no 'disabled' refusal journaled"
if grep -q 'approval_received' /tmp/device-ipad-hostoff.log
then fail "the device was asked while the host switch was off"
else pass "the device was never asked"; fi
stop_device

say "turn the preference on"
revision=$(as_owner "omodachi-host preferences get" | python -c 'import json,sys;print(json.load(sys.stdin)["result"]["revision"])')
as_owner "omodachi-host preferences set --revision $revision --biometric-auth true" | sed 's/^/    | /'

say "no enrolled, connected device -> still the password"
attempt "sudo with nobody to ask" /tmp/case-nodevice.log
prompted /tmp/case-nodevice.log && pass "an approval nobody can answer is never raised" || fail "no password prompt"
as_owner "omodachi-host auth status" | sed 's/^/    | /'

# ---------------------------------------------------------------------------
say "BOTH SWITCHES · host on + device off -> the password prompt"
start_device approve ipad-deviceoff false || exit 1
as_owner "omodachi-host auth status" | python -c 'import json,sys;print(json.dumps(json.load(sys.stdin)["result"]["keys"],indent=2))' | sed 's/^/    | /'
attempt "sudo with the device switch off" /tmp/case-deviceoff.log
prompted /tmp/case-deviceoff.log && pass "a registered key with the switch off is not an audience" || fail "no password prompt"
grep -q '"reason":"no_connected_device"' /tmp/daemon.log \
  && pass "the host found nobody to ask" || fail "no 'no_connected_device' refusal journaled"
if grep -q 'approval_received' /tmp/device-ipad-deviceoff.log
then fail "the device was asked with its own switch off"
else pass "the device was never asked"; fi
stop_device

# ---------------------------------------------------------------------------
say "CASE 1 · both switches on, the device approves -> sudo with no password"
start_device approve ipad-approve || exit 1
attempt "sudo, device approves" /tmp/case1.log
rc=$?
check "$rc" "0" "sudo succeeded"
if prompted /tmp/case1.log; then fail "a password prompt was shown anyway"; else pass "no password was asked for"; fi
note "what the helper traced:"; trace_tail 4
note "the whole device log:"; sed 's/^/    | /' /tmp/device-ipad-approve.log
grep -q 'Approved on Docker iPad' /tmp/case1.log \
  && pass "PAM showed which device approved" || fail "the approval line was not shown"
note "what the device saw:"
grep -E 'approval_received|approval_answered' /tmp/device-ipad-approve.log | sed 's/^/    | /'
note "what the journal (daemon stdout) says:"
grep -E '"omodachi": *"auth"' /tmp/daemon.log | sed 's/^/    | /'
stop_device

# ---------------------------------------------------------------------------
say "CASE 2a · the device declines -> back to the password"
start_device decline ipad-decline || exit 1
attempt "sudo, device declines" /tmp/case2a.log
rc=$?
check "$rc" "1" "sudo failed"
prompted /tmp/case2a.log && pass "fell through to the password prompt" || fail "no password prompt"
grep -E '"step": "approval_answered"' /tmp/device-ipad-decline.log | sed 's/^/    | /'
stop_device

say "CASE 2b · the device never answers -> the 10 s timeout, then the password"
start_device ignore ipad-ignore || exit 1
attempt "sudo, device ignores" /tmp/case2b.log
rc=$?
check "$rc" "1" "sudo failed"
prompted /tmp/case2b.log && pass "fell through to the password prompt" || fail "no password prompt"
grep -E '"outcome": *"timeout"' /tmp/daemon.log | sed 's/^/    | /'
stop_device

say "the password still works while the whole thing is live"
if password_works /tmp/case2c.log
then pass "the password still works"; else fail "the password stopped working"; sed 's/^/    | /' /tmp/case2c.log; fi

say "a replayed signature cannot approve a second prompt (one nonce, one use)"
start_device replay ipad-replay || exit 1
attempt "sudo, device signs then replays the identical body" /tmp/case-replay.log
rc=$?
check "$rc" "0" "the first submission approved the prompt"
sleep 1
grep -E '"step": "approval_(answered|replayed)"' /tmp/device-ipad-replay.log | sed 's/^/    | /'
if grep -q '"step": "approval_replayed", "status": 404' /tmp/device-ipad-replay.log \
   || python - <<'PY'
import json, sys
rows = [json.loads(l) for l in open('/tmp/device-ipad-replay.log') if l.startswith('{')]
replays = [r for r in rows if r.get('step') == 'approval_replayed']
sys.exit(0 if replays and all(r['status'] in (404, 409) for r in replays) else 1)
PY
then pass "the second, identical submission was refused"; else fail "a replay was accepted"; fi
stop_device

stop_daemon

say "AUTH-2 · the compatibility symlink lives exactly as long as the daemon"
if [ -e "$LEGACY_SOCKET" ] || [ -L "$LEGACY_SOCKET" ]; then
  fail "the old path survived the daemon; a dangling symlink looks like a daemon"
  ls -l "$LEGACY_SOCKET" | sed 's/^/    | /'
else
  pass "the old path is gone now that there is nothing behind it"
fi
[ -e "$SOCKET" ] && fail "the real socket survived the daemon" || pass "and so is the socket itself"

say "AUTH-2 · with no shared directory the daemon falls back to the runtime one"
mv "$SHARED_DIR" "$SHARED_DIR.parked"
start_daemon >/dev/null 2>&1 || true
for _ in $(seq 1 40); do grep -q '"ready": *true' /tmp/daemon.log && break; sleep 0.5; done
grep -o '"socket": "[^"]*"' /tmp/daemon.log | head -1 | sed 's/^/    | /'
check "$(grep -o '"socket": "[^"]*"' /tmp/daemon.log | head -1)" "\"socket\": \"$FALLBACK_SOCKET\"" \
      "a host that never ran the root step still gets a daemon"
stop_daemon
mv "$SHARED_DIR.parked" "$SHARED_DIR"

# ---------------------------------------------------------------------------
say "CASE 4 · --remove restores every file byte for byte"
sha_installed=$(sha256sum /etc/pam.d/sudo | cut -d' ' -f1)
note "installed sha256 = $sha_installed"
python -m omodachi_core.pam_install remove
sha_after=$(sha256sum /etc/pam.d/sudo | cut -d' ' -f1)
note "restored  sha256 = $sha_after"
check "$sha_after" "$sha_before_sudo" "/etc/pam.d/sudo is byte-identical to the original"
if cmp -s /tmp/sudo.before /etc/pam.d/sudo; then pass "cmp agrees"; else fail "cmp disagrees"; diff /tmp/sudo.before /etc/pam.d/sudo; fi
[ -e /usr/local/bin/omodachi-pam ] && fail "the helper is still installed" || pass "the helper is gone"
[ -e /etc/omodachi/pam.conf ] && fail "the config is still there" || pass "the config is gone"
[ -e /etc/omodachi ] && fail "/etc/omodachi is still there" || pass "/etc/omodachi is gone"
note "/etc/pam.d/sudo now:"
sed 's/^/    | /' /etc/pam.d/sudo

say "and sudo still behaves exactly as it did at the start"
attempt "sudo with no password, after removal" /tmp/case4.log
prompted /tmp/case4.log && pass "asks for the password" || fail "no prompt"
if password_works /tmp/case4b.log
then pass "the password works"; else fail "the password does not work"; sed 's/^/    | /' /tmp/case4b.log; fi

# ---------------------------------------------------------------------------
say "CASE 5 · polkit-1 has no /etc file; installing one and removing it again"
python -m omodachi_core.pam_install install --owner "$OWNER" --socket "$SOCKET" \
    --services sudo,polkit-1 --timeout 10 >/dev/null
say "AUTH-2 · and polkit-1 brings the tmpfiles fragment and the drop-in with it"
if [ -f "$TMPFILES" ]; then
  pass "the tmpfiles fragment is at $TMPFILES"
  sed 's/^/    | /' "$TMPFILES"
  check "$(grep -c '^d ' "$TMPFILES")" "2" "it declares the root parent and the private child"
  grep -q "^d /run/omodachi 0755 root root -$" "$TMPFILES" \
    && pass "the parent is root's, so no other user can plant a directory in it" \
    || fail "the parent line is not the one expected"
  grep -q "^d $SHARED_DIR 0700 $OWNER $OWNER -$" "$TMPFILES" \
    && pass "and the per-uid one is 0700 and the owner's" \
    || fail "the per-uid line is not the one expected"
else
  fail "no tmpfiles fragment was written for polkit-1"
fi
if [ -f "$DROPIN" ]; then
  pass "the drop-in is at $DROPIN"
  sed 's/^/    | /' "$DROPIN"
  check "$(grep -c '^ReadWritePaths=' "$DROPIN")" "1" "it carries exactly one ReadWritePaths"
  check "$(grep '^ReadWritePaths=' "$DROPIN")" "ReadWritePaths=-$SHARED_DIR" \
        "and it is the socket directory, nothing wider"
  grep -q '^\[Service\]$' "$DROPIN" && pass "it is a [Service] drop-in" || fail "no [Service] section"
else
  fail "no drop-in was written for polkit-1"
fi
# Two shapes the sandbox could never reach, whatever a drop-in said: the home
# (AUTH-1's) and the runtime directory (the one that looks like it should work).
for unreachable in "$LEGACY_SOCKET" "$FALLBACK_SOCKET"; do
  python -m omodachi_core.pam_install install --owner "$OWNER" --socket "$unreachable" \
      --services sudo,polkit-1 --timeout 10 \
    | python -c 'import json,sys; r=json.load(sys.stdin)["result"]; print("    |", r["manifest"]["socket"], "->", r["dropin"]["reason"], "/", r["runtime_dir"]["reason"])'
  [ -e "$DROPIN" ] && fail "a drop-in was written for $unreachable" \
                   || pass "no drop-in for a socket the sandbox cannot see"
done
python -m omodachi_core.pam_install install --owner "$OWNER" --socket "$SOCKET" \
    --services sudo,polkit-1 --timeout 10 >/dev/null
note "/etc/pam.d/polkit-1 (created, shadowing the vendor file):"
sed 's/^/    | /' /etc/pam.d/polkit-1
diff <(sed -e '/omodachi-auth/d' -e '/omodachi-pam/d' /etc/pam.d/polkit-1) /usr/lib/pam.d/polkit-1 >/dev/null \
  && pass "it is the vendor stack plus exactly our comment and our rule" \
  || { fail "it is not the vendor stack plus our two lines"
       diff <(sed -e '/omodachi-auth/d' -e '/omodachi-pam/d' /etc/pam.d/polkit-1) /usr/lib/pam.d/polkit-1 | sed 's/^/    | /'; }
python -m omodachi_core.pam_install remove >/dev/null
[ -e /etc/pam.d/polkit-1 ] && fail "the shadow file survived removal" || pass "the shadow file is gone and the vendor file is in charge again"
[ -e "$DROPIN" ] && fail "the drop-in survived removal" || pass "the drop-in is gone"
[ -d "$(dirname "$DROPIN")" ] && fail "the drop-in directory survived removal" || pass "and so is the directory it made"
[ -e "$TMPFILES" ] && fail "the tmpfiles fragment survived removal" || pass "and the tmpfiles fragment too"
check "$(sha256sum /etc/pam.d/sudo | cut -d' ' -f1)" "$sha_before_sudo" "/etc/pam.d/sudo restored again"

# ---------------------------------------------------------------------------
say "RESULT"
if [ "$FAILURES" -eq 0 ]; then printf '\033[32mall assertions passed\033[0m\n'; else printf '\033[31m%d assertion(s) failed\033[0m\n' "$FAILURES"; fi
exit "$FAILURES"
