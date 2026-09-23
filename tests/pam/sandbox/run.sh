#!/bin/bash
# Build and run the AUTH-2 sandbox harness. From the omodachi-core checkout:
#
#     tests/pam/sandbox/run.sh           # build, boot systemd, run every case
#     tests/pam/sandbox/run.sh --shell   # leave the container up and shell in
#
# This one boots systemd as PID 1, which is the whole point: the question is
# what a *unit's* sandboxing properties do to the PAM helper, and only a
# service manager can answer it. That needs --privileged and the host's cgroup
# tree. The AUTH-1 harness beside it deliberately has neither, and keeps
# testing PAM and sudo with no extra capabilities at all.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
IMAGE=omodachi-auth2-sandbox
NAME=omodachi-auth2-sandbox-run

echo "building $IMAGE from $ROOT"
docker build -f "$ROOT/tests/pam/sandbox/Dockerfile" -t "$IMAGE" "$ROOT"

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock --tmpfs /tmp \
  "$IMAGE" >/dev/null

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
if [ "${1:-}" != "--shell" ]; then trap cleanup EXIT; fi

for _ in $(seq 1 60); do
  if docker exec "$NAME" systemctl is-system-running --wait >/dev/null 2>&1; then break; fi
  state=$(docker exec "$NAME" systemctl is-system-running 2>/dev/null || true)
  case "$state" in running|degraded) break ;; esac
  sleep 1
done
echo "systemd is $(docker exec "$NAME" systemctl is-system-running 2>/dev/null || echo unknown)"

if [ "${1:-}" = "--shell" ]; then
  echo "container $NAME is up; 'docker rm -f $NAME' when you are done"
  exec docker exec -it "$NAME" /bin/bash
fi

docker exec "$NAME" /opt/harness/sandbox-cases.sh
