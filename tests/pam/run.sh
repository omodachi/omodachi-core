#!/bin/bash
# Build and run the AUTH-1 PAM harness. Run it from the omodachi-core checkout:
#
#     tests/pam/run.sh                 # build and run every case
#     tests/pam/run.sh --shell         # drop into the container instead
#
# On Apple Silicon this forces linux/amd64, because `archlinux:latest` has no
# arm64 tag. It is emulated and therefore slow (the first build is minutes);
# `--platform` is the only concession to the Mac - everything inside the
# container is the same Arch, PAM and sudo the host runs.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE=omodachi-auth1-pam
PLATFORM="${OMODACHI_PAM_PLATFORM:-linux/amd64}"

echo "building $IMAGE for $PLATFORM from $ROOT"
docker build --platform "$PLATFORM" -f "$ROOT/tests/pam/Dockerfile" -t "$IMAGE" "$ROOT"

if [ "${1:-}" = "--shell" ]; then
  exec docker run --rm -it --platform "$PLATFORM" --entrypoint /bin/bash "$IMAGE"
fi

# No extra capabilities, no privileged mode, no host mounts: PAM, sudo and the
# daemon all run inside the container as they would on a machine.
exec docker run --rm --platform "$PLATFORM" "$IMAGE" "$@"
