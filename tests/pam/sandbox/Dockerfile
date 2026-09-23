# The AUTH-2 sandbox harness: real systemd, the real sandboxing properties of
# `polkit-agent-helper@.service`, the real PAM helper, a real omodachid and the
# same fake iPad the AUTH-1 harness uses.
#
# The question this image exists to answer is narrow and was not answerable in
# the AUTH-1 harness at all, because that container has no service manager:
# **can a process started under those unit properties reach the daemon's socket,
# and does a `ReadWritePaths=` drop-in change the answer?**
#
# It runs systemd as PID 1, which needs `--privileged` (run.sh passes it). The
# base is Debian rather than Arch because this image has to boot systemd and
# `archlinux:latest` is amd64 only - emulating a systemd boot on an Apple
# Silicon Mac is minutes of qemu per run. The property semantics under test
# (`ProtectHome=`, `ProtectSystem=`, `ReadWritePaths=`) are systemd's, not the
# distribution's, and the unit file is copied in verbatim from the host, so the
# only thing Debian contributes is the version of systemd. It prints it.
FROM debian:trixie

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus python3 python3-venv python3-pip \
      polkitd pkexec procps iproute2 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

RUN useradd -m -u 1000 -s /bin/bash omodachi

COPY pyproject.toml /opt/omodachi/pyproject.toml
COPY src /opt/omodachi/src
RUN pip install --no-cache-dir -e /opt/omodachi

# The unit as it ships on the host this feature is for: Arch's polkit 127-3,
# byte for byte out of /usr/lib/systemd/system. It is installed over whatever
# this distribution ships so the properties under test are the host's.
COPY tests/pam/sandbox/polkit-agent-helper@.service /usr/lib/systemd/system/polkit-agent-helper@.service
COPY tests/pam/fake_device.py /opt/harness/fake_device.py
COPY tests/pam/sandbox/cases.sh /opt/harness/sandbox-cases.sh
RUN chmod +x /opt/harness/sandbox-cases.sh

STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
