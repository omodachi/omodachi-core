#!/bin/bash
# AUTH-2 · can the PAM helper reach the socket from inside polkit's sandbox?
#
# AUTH-1 shipped a working PAM factor that polkit could never use: the unit
# `polkit-agent-helper@.service` carries `ProtectHome=yes`, so the helper it
# starts saw an empty `/home` where the daemon's socket was. This script
# answers, with the real service manager, the real unit properties (copied byte
# for byte off the host) and the real helper:
#
#   1. what does `ProtectHome=yes` actually blank - and can a drop-in put any
#      of it back?
#   2. where can the socket live so that a helper under those properties can
#      reach it?
#   3. from there, does an approval actually come through - exit 0, no
#      password, and the daemon's journal saying which device said yes?
#
# Everything is run through `systemd-run` carrying the unit's own properties,
# so the property list printed in section 0 is the property list under test.
set -uo pipefail

OWNER=omodachi
HOME_DIR=/home/$OWNER
SHARED_DIR=/run/omodachi/1000
SOCKET=$SHARED_DIR/omodachid.sock
RUNTIME_DIR=/run/user/1000
RUNTIME_SOCKET=$RUNTIME_DIR/omodachi/omodachid.sock
LEGACY_SOCKET=$HOME_DIR/.cache/omodachi/omodachid.sock
UNIT=/usr/lib/systemd/system/polkit-agent-helper@.service
DROPIN_DIR=/etc/systemd/system/polkit-agent-helper@.service.d
DROPIN=$DROPIN_DIR/60-omodachi.conf
TMPFILES=/etc/tmpfiles.d/omodachi.conf
HELPER=/usr/local/bin/omodachi-pam
# `bash -lc` re-reads /etc/profile, which rewrites PATH on this distribution,
# so everything the owner runs is named by its full path.
VENV=/opt/venv/bin
FAILURES=0
DAEMON_PID=
DEVICE_PID=

say() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILURES=$((FAILURES+1)); }
check() { if [ "$1" = "$2" ]; then pass "$3 ($1)"; else fail "$3: expected [$2] got [$1]"; fi; }
as_owner() { runuser -u "$OWNER" -- bash -lc "$VENV/$1"; }

# The sandboxing half of the real unit: every [Service] directive except the
# handful that describe *what* it runs and how it is wired to its socket.
# Those are the parts systemd-run supplies itself; everything that confines
# the process is kept exactly as the host ships it.
sandbox_properties() {
  local in_service=0 key line
  while IFS= read -r line; do
    case "$line" in
      "[Service]") in_service=1; continue ;;
      "["*"]") in_service=0; continue ;;
    esac
    [ "$in_service" = 1 ] || continue
    [ -n "$line" ] || continue
    case "$line" in \#*) continue ;; esac
    key=${line%%=*}
    case "$key" in
      ExecStart|Type|SuccessExitStatus|StandardInput|StandardOutput|StandardError) continue ;;
    esac
    printf '%s\n' "$line"
  done < "$UNIT"
}

mapfile -t SANDBOX_LINES < <(sandbox_properties)
SANDBOX_ARGS=()
for property in "${SANDBOX_LINES[@]}"; do SANDBOX_ARGS+=(-p "$property"); done

# One run of something under those properties, plus whatever extra properties
# the case is testing (everything before a literal `--`).
in_sandbox() {
  local extra=()
  while [ "${1:-}" != "--" ]; do extra+=(-p "$1"); shift; done
  shift
  systemd-run --quiet --wait --pipe --collect --unit="omodachi-probe-$RANDOM" \
    "${SANDBOX_ARGS[@]}" "${extra[@]}" \
    --setenv=PAM_TYPE=auth --setenv=PAM_SERVICE=polkit-1 \
    --setenv=PAM_USER=$OWNER --setenv=PAM_RUSER=${RUSER:-$OWNER} --setenv=PAM_TTY=/dev/pts/0 \
    "$@"
}

# Is $VISIBLE_PATH a socket, as seen from inside the sandbox?
visible() { in_sandbox "$@" -- /bin/sh -c "[ -S $VISIBLE_PATH ] && echo yes || echo no" 2>/dev/null; }

start_daemon() {
  # logind would have made this; there is none here, and `--tmpfs /run` means
  # the image's copy is gone. The fallback probes in section 2 need it to be a
  # real directory rather than a missing one.
  install -d -m 0700 -o "$OWNER" -g "$OWNER" "$RUNTIME_DIR"
  install -d -m 0700 -o "$OWNER" -g "$OWNER" "$RUNTIME_DIR/omodachi"
  runuser -u "$OWNER" -- bash -lc \
    "XDG_RUNTIME_DIR=$RUNTIME_DIR $VENV/omodachid --secret-file $HOME_DIR/.config/omodachi/device.secret \
       --listen 127.0.0.1 --port 8099 --allow-loopback-http --demo --discovery off" \
    >/tmp/daemon.log 2>&1 &
  DAEMON_PID=$!
  for _ in $(seq 1 60); do
    [ -S "$SOCKET" ] && grep -q '"ready": *true' /tmp/daemon.log && return 0
    sleep 0.5
  done
  echo "daemon did not start"; cat /tmp/daemon.log; return 1
}

start_device() {
  runuser -u "$OWNER" -- bash -lc \
    "PATH=$VENV:\$PATH XDG_RUNTIME_DIR=$RUNTIME_DIR $VENV/python3 /opt/harness/fake_device.py \
       --behaviour approve --device-id ipad-sandbox --device-enabled true \
       --name 'Sandbox iPad' --base http://127.0.0.1:8099" \
    >/tmp/device.log 2>&1 &
  DEVICE_PID=$!
  for _ in $(seq 1 80); do
    grep -q '"step": "subscribed"' /tmp/device.log && return 0
    sleep 0.5
  done
  echo "device did not subscribe"; cat /tmp/device.log; return 1
}

# ---------------------------------------------------------------------------
say "0 · what is running this"
systemctl --version | head -1 | sed 's/^/    | /'
note "polkit: $(dpkg-query -W -f='${Version}' polkitd 2>/dev/null || echo 'not packaged here')"
note "the unit under test, byte for byte off the host (Arch polkit 127-3):"
sha256sum "$UNIT" | sed 's/^/    | /'
note "the sandboxing properties every probe below carries:"
printf '    | %s\n' "${SANDBOX_LINES[@]}"

# ---------------------------------------------------------------------------
say "1 · the root step: the PAM entry, the tmpfiles fragment and the drop-in"
python3 -m omodachi_core.pam_install install --owner "$OWNER" --socket "$SOCKET" \
    --services polkit-1 --timeout 20 >/tmp/install.json
python3 -c 'import json;r=json.load(open("/tmp/install.json"))["result"]
print("    | tmpfiles:", json.dumps(r["runtime_dir"],sort_keys=True))
print("    | dropin:  ", json.dumps(r["dropin"],sort_keys=True))'
note "$TMPFILES, and the directory systemd-tmpfiles made from it:"
sed 's/^/    | /' "$TMPFILES"
ls -ld /run/omodachi "$SHARED_DIR" | sed 's/^/    | /'
check "$(stat -c '%a %U' /run/omodachi)" "755 root" "the parent belongs to root"
check "$(stat -c '%a %U' "$SHARED_DIR")" "700 $OWNER" "the per-uid directory belongs to the owner"
note "$DROPIN:"
sed 's/^/    | /' "$DROPIN"
systemctl daemon-reload
note "systemd-analyze verify (only the Documentation= warning is expected):"
systemd-analyze verify "polkit-agent-helper@7.service" 2>&1 | sed 's/^/    | /'
note "what systemd now says about the real unit:"
systemctl show "polkit-agent-helper@7.service" -p ProtectHome -p ProtectSystem -p ReadWritePaths \
  | sed 's/^/    | /'
check "$(systemctl show 'polkit-agent-helper@7.service' -p ProtectHome --value)" "yes" \
      "the vendor unit still protects the home"
# systemd keeps the '-' prefix in the property value; it means "ignore if absent".
check "$(systemctl show 'polkit-agent-helper@7.service' -p ReadWritePaths --value)" "-$SHARED_DIR" \
      "and the drop-in added exactly the socket directory"

say "1b · the daemon and the device"
start_daemon || exit 1
grep -o '"socket": "[^"]*"' /tmp/daemon.log | head -1 | sed 's/^/    | /'
check "$(grep -o '"socket": "[^"]*"' /tmp/daemon.log | head -1)" "\"socket\": \"$SOCKET\"" \
      "the daemon found the directory the root step made and bound there"
check "$(grep '^socket=' /etc/omodachi/pam.conf)" "socket=$SOCKET" \
      "and the root-owned PAM config names the same path"
check "$(stat -c '%a' "$SOCKET")" "600" "the socket is private"
start_device || exit 1
revision=$(as_owner "omodachi-host preferences get" | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"]["revision"])')
as_owner "omodachi-host preferences set --revision $revision --biometric-auth true" >/dev/null
as_owner "omodachi-host auth status" \
  | python3 -c 'import json,sys;r=json.load(sys.stdin)["result"];print("    | enabled:",r["enabled"],"eligible:",r["eligible_devices"])'

# ---------------------------------------------------------------------------
say "2 · what ProtectHome=yes actually blanks"
note "the same three directories, outside the sandbox:"
printf '    | %s\n' "$(ls -d $HOME_DIR/.cache/omodachi $RUNTIME_DIR/omodachi $SHARED_DIR 2>&1 | tr '\n' ' ')"
note "and inside it:"
in_sandbox -- /bin/sh -c "
  printf 'home          : '; ls -a $HOME_DIR 2>&1 | tr '\n' ' '; echo
  printf '/run/user     : '; ls -a $RUNTIME_DIR 2>&1 | tr '\n' ' '; echo
  printf '/run/omodachi : '; ls -a $SHARED_DIR 2>&1 | tr '\n' ' '; echo" 2>&1 | sed 's/^/    | /'
note "ProtectHome=yes blanks /home, /root AND /run/user. That second half is"
note "the whole reason the socket is not in \$XDG_RUNTIME_DIR."

say "2b · and nothing a drop-in can say puts /run/user back"
for property in "ReadWritePaths=-$RUNTIME_DIR/omodachi" "ReadWritePaths=/run/user/" \
                "BindPaths=$RUNTIME_DIR/omodachi" "BindReadOnlyPaths=$RUNTIME_DIR/omodachi"; do
  seen=$(in_sandbox "$property" -- /bin/sh -c "ls -a $RUNTIME_DIR/omodachi >/dev/null 2>&1 && echo yes || echo no" 2>/dev/null)
  check "$seen" "no" "with $property the runtime directory is still gone"
done
seen=$(in_sandbox "ProtectHome=tmpfs" "ReadWritePaths=-$RUNTIME_DIR/omodachi" -- \
        /bin/sh -c "ls -a $RUNTIME_DIR/omodachi >/dev/null 2>&1 && echo yes || echo no" 2>/dev/null)
check "$seen" "no" "and ProtectHome=tmpfs does not change it either"
note "systemd mounts those three paths *inaccessible* and drops every mount it"
note "was asked to make underneath one. ProtectHome=read-only would show them -"
note "by showing the whole of /home as well, which is not a trade this feature"
note "is worth."

say "2c · /run/omodachi is outside all three"
VISIBLE_PATH=$SOCKET
check "$(visible "ReadWritePaths=-$SHARED_DIR")" "yes" "with the drop-in's property the socket is there"
check "$(visible)" "yes" "and without it too: a connect() only needs the path visible"
note "how strict /run actually is inside this sandbox, with and without the"
note "drop-in's property - it is not the stable thing the design leans on:"
for property in "" "ReadWritePaths=-$SHARED_DIR"; do
  if [ -z "$property" ]; then
    printf '    | no ReadWritePaths : %s\n' \
      "$(in_sandbox -- /bin/sh -c "touch $SHARED_DIR/probe && rm -f $SHARED_DIR/probe && echo writable || echo read-only" 2>&1 | tail -1)"
  else
    printf '    | %-18s: %s\n' "with it" \
      "$(in_sandbox "$property" -- /bin/sh -c "touch $SHARED_DIR/probe && rm -f $SHARED_DIR/probe && echo writable || echo read-only" 2>&1 | tail -1)"
  fi
done
note "(ProtectSystem=strict alone makes /run read-only; this unit's own"
note "ProtectControlGroups= and ProtectKernelTunables= each undo that again, on"
note "systemd 257 here and on 261 on omarchy. Which is exactly why the drop-in"
note "states what it needs rather than inferring it from the rest of the unit.)"
note "The location is the mechanism; the drop-in is the declaration."

# ---------------------------------------------------------------------------
say "3 · the helper itself, under those properties"

HELPER_OUTPUT=
run_helper() {
  local label="$1" expected="$2"; shift 2
  local out rc start end
  start=$(date +%s%N)
  out=$(in_sandbox "$@" 2>&1); rc=$?
  end=$(date +%s%N)
  printf '  %s: rc=%d elapsed=%d.%03ds\n' "$label" "$rc" \
      "$(( (end-start)/1000000000 ))" "$(( ((end-start)/1000000)%1000 ))"
  [ -n "$out" ] && printf '%s\n' "$out" | sed 's/^/    | /'
  check "$rc" "$expected" "$label"
  HELPER_OUTPUT=$out
}

point_config_at() {
  python3 -m omodachi_core.pam_install install --owner "$OWNER" --socket "$1" \
      --services polkit-1 --timeout 20 >/dev/null
}

say "3a · AUTH-1's shape: the socket in the home -> refusal"
point_config_at "$LEGACY_SOCKET"
run_helper "helper with socket=\$HOME/.cache/..." 1 -- "$HELPER" --timeout 20
note "this is the omarchy journal line AUTH-1 ended on, reproduced:"
note "  pam_exec(polkit-1:auth): /usr/local/bin/omodachi-pam failed: exit code 1"

say "3b · the runtime directory: the one that looks like it should work -> refusal"
point_config_at "$RUNTIME_SOCKET"
run_helper "helper with socket=\$XDG_RUNTIME_DIR/omodachi/..." 1 -- "$HELPER" --timeout 20

say "3c · /run/omodachi/<uid>, with the drop-in -> the device approves"
point_config_at "$SOCKET"
run_helper "helper with socket=/run/omodachi/1000/..." 0 "ReadWritePaths=-$SHARED_DIR" -- \
  "$HELPER" --timeout 20
case "$HELPER_OUTPUT" in
  *"Approved on Sandbox iPad"*) pass "PAM was told which device approved" ;;
  *) fail "the approval line was not printed" ;;
esac

say "3d · and with the drop-in's property taken away, still an approval"
run_helper "the same, with no ReadWritePaths at all" 0 -- "$HELPER" --timeout 20

say "3e · the control: the same helper outside any sandbox"
env PAM_TYPE=auth PAM_SERVICE=polkit-1 PAM_USER=$OWNER PAM_RUSER=$OWNER \
    "$HELPER" --timeout 20 >/tmp/unsandboxed.log 2>&1
rc=$?
sed 's/^/    | /' /tmp/unsandboxed.log
check "$rc" "0" "unsandboxed, it approves too"

say "3f · a requester who is not the owner never reaches the device, sandbox or not"
RUSER=intruder run_helper "helper with PAM_RUSER=somebody-else" 1 -- "$HELPER" --timeout 20

# ---------------------------------------------------------------------------
say "4 · what the daemon journaled through all of that"
grep -E '"omodachi": *"auth"' /tmp/daemon.log | sed 's/^/    | /'
note "what the device saw:"
grep -E 'approval_received|approval_answered' /tmp/device.log | tail -4 | sed 's/^/    | /'

# ---------------------------------------------------------------------------
say "5 · --remove takes both root-owned files away again"
python3 -m omodachi_core.pam_install remove | python3 -c 'import json,sys;r=json.load(sys.stdin)["result"]
print("    | dropin:  ", json.dumps(r["dropin"],sort_keys=True))
print("    | tmpfiles:", json.dumps(r["runtime_dir"],sort_keys=True))'
[ -e "$DROPIN" ] && fail "the drop-in survived removal" || pass "the drop-in is gone"
[ -d "$DROPIN_DIR" ] && fail "its directory survived removal" || pass "and so is its directory"
[ -e "$TMPFILES" ] && fail "the tmpfiles fragment survived removal" || pass "the tmpfiles fragment is gone"
[ -e "$HELPER" ] && fail "the helper survived removal" || pass "and the helper with it"
note "the directory itself stays until the next boot on purpose - taking it"
note "away now would pull the socket out from under the running daemon:"
ls -ld "$SHARED_DIR" | sed 's/^/    | /'
systemctl daemon-reload
check "$(systemctl show 'polkit-agent-helper@7.service' -p ReadWritePaths --value)" "" \
      "systemd is back to the vendor unit's own property list"
check "$(systemctl show 'polkit-agent-helper@7.service' -p ProtectHome --value)" "yes" \
      "with ProtectHome still on"

[ -n "$DEVICE_PID" ] && kill "$DEVICE_PID" 2>/dev/null
[ -n "$DAEMON_PID" ] && kill "$DAEMON_PID" 2>/dev/null
wait 2>/dev/null

say "RESULT"
if [ "$FAILURES" -eq 0 ]; then printf '\033[32mall assertions passed\033[0m\n'
else printf '\033[31m%d assertion(s) failed\033[0m\n' "$FAILURES"; fi
exit "$FAILURES"
