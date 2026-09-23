# The AUTH-1 PAM harness: a real Arch, a real PAM stack, a real sudo, a real
# omodachid, and a fake iPad. Nothing here is mocked except the tablet.
#
# `archlinux:latest` is amd64 only, so on an Apple Silicon Mac this image is
# built and run with `--platform linux/amd64` (run.sh passes it). It is slow
# and it is worth it: the point of this harness is that the PAM stack is the
# same `pam 1.7` / `pam_exec.so` / `sudo` the host runs, not a simulation of it.
FROM archlinux:latest

# pacman 7's alpm sandbox uses seccomp filters the amd64-on-arm64 emulation
# layer rejects ("error restricting syscalls via seccomp: 22"). Turning it off
# affects package installation inside this throwaway image and nothing else.
RUN printf 'DisableSandbox\n' >> /etc/pacman.conf \
 && pacman -Sy --noconfirm --needed python python-pip sudo pam shadow procps-ng diffutils polkit \
 && pacman -Scc --noconfirm

# A virtualenv, because Arch's pip refuses to install into the system tree and
# because the daemon on the real host runs out of one too.
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# The unprivileged owner. The password is the thing this whole feature is
# supposed to make optional, so the harness has to know it to prove that it
# still works.
RUN useradd -m -u 1000 -s /bin/bash omodachi \
 && echo 'omodachi:hunter2' | chpasswd \
 && echo 'omodachi ALL=(ALL) ALL' > /etc/sudoers.d/omodachi \
 && chmod 0440 /etc/sudoers.d/omodachi

# AUTH-2: the two directories the daemon chooses between. /run/omodachi/<uid>
# is what `install_host.py --pam` makes through tmpfiles.d on a real host and
# is what the daemon prefers; /run/user/1000 is what logind would have made,
# and is the fallback. There is neither logind nor systemd in this container,
# so both are made here, exactly as those two things would have made them.
RUN install -d -m 0755 -o root -g root /run/omodachi \
 && install -d -m 0700 -o omodachi -g omodachi /run/omodachi/1000 \
 && install -d -m 0700 -o omodachi -g omodachi /run/user/1000

COPY pyproject.toml /opt/omodachi/pyproject.toml
COPY src /opt/omodachi/src
RUN pip install --no-cache-dir -e /opt/omodachi

COPY tests/pam/fake_device.py /opt/harness/fake_device.py
COPY tests/pam/cases.sh /opt/harness/cases.sh
RUN chmod +x /opt/harness/cases.sh

ENTRYPOINT ["/opt/harness/cases.sh"]
