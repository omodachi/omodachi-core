"""Host identity and the self-signed certificate a companion pins on pairing.

The trust model is the SSH one: the host owns a long-lived self-signed
certificate, `GET /health` and the pairing claim publish its SHA-256
fingerprint, and the client pins that fingerprint the moment a claim succeeds.
There is no CA, no installed profile and no automatic rotation; a deliberate
`omodachi-host tls rotate` is the only thing that changes the fingerprint.

The fingerprint published here is an anchor, not an authorization: reading it
proves nothing until the local approval step of pairing has happened.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import tempfile

CERTIFICATE_DAYS = 3650
CERTIFICATE_NAME = "server.pem"
PRIVATE_KEY_NAME = "server.key"
_HOST_ID = re.compile(r"[0-9a-f]{32}\Z")
_PEM = re.compile(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----\n?", re.S)


class HostIdentityError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def certificate_fingerprint(path) -> str | None:
    """SHA-256 over the certificate DER, lowercase hex; None when unreadable."""
    try:
        raw = Path(path).read_text()
    except (OSError, UnicodeError, TypeError):
        return None
    match = _PEM.search(raw)
    if match is None:
        return None
    try:
        der = ssl.PEM_cert_to_DER_cert(match.group(0))
    except (ValueError, TypeError):
        return None
    return hashlib.sha256(der).hexdigest()


def host_addresses() -> list[str]:
    """Every non-loopback unicast address this host currently holds."""
    values: list[str] = []
    try:
        raw = subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True,
                             timeout=5, check=True).stdout
        for link in json.loads(raw):
            for entry in link.get("addr_info", []):
                values.append(entry.get("local"))
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        try:
            values = [row[4][0] for row in socket.getaddrinfo(socket.gethostname(), None)]
        except (OSError, IndexError):
            values = []
    result: list[str] = []
    for value in values:
        try:
            address = ipaddress.ip_address(value)
        except (TypeError, ValueError):
            continue
        if (address.is_loopback or address.is_link_local or address.is_multicast
                or address.is_unspecified or address.is_reserved):
            continue
        if str(address) not in result:
            result.append(str(address))
    return result


def host_name() -> str:
    name = socket.gethostname().split(".")[0].lower()
    return name if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", name) else "omodachi"


def load_host_id(config_dir) -> str:
    """A stable random identity for this installation, independent of the key."""
    path = Path(config_dir) / "host-id"
    try:
        value = path.read_text().strip()
    except (OSError, UnicodeError):
        value = ""
    if _HOST_ID.fullmatch(value):
        return value
    value = secrets.token_hex(16)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(value + "\n")
    return value


def generate_certificate(directory, *, name=None, addresses=None) -> dict:
    """Write a fresh self-signed ECDSA P-256 certificate and key, 0600.

    The SAN carries the hostname, `<hostname>.local` and every current
    non-loopback address, so a client that reached the host by address, by
    mDNS name or through Tailscale sees a matching certificate.
    """
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = name or host_name()
    addresses = list(host_addresses() if addresses is None else addresses)
    subject_alt = [f"DNS:{name}", f"DNS:{name}.local"]
    for value in addresses:
        try:
            subject_alt.append("IP:" + str(ipaddress.ip_address(value)))
        except ValueError:
            raise HostIdentityError("tls_address_invalid") from None
    certificate, private_key = directory / CERTIFICATE_NAME, directory / PRIVATE_KEY_NAME
    handle, temporary_certificate = tempfile.mkstemp(prefix=".server-", suffix=".pem", dir=directory)
    os.close(handle)
    handle, temporary_key = tempfile.mkstemp(prefix=".server-", suffix=".key", dir=directory)
    os.close(handle)
    try:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec",
                        "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                        "-days", str(CERTIFICATE_DAYS), "-subj", f"/CN={name}",
                        "-addext", "subjectAltName=" + ",".join(subject_alt),
                        "-addext", "basicConstraints=critical,CA:FALSE",
                        "-keyout", temporary_key, "-out", temporary_certificate],
                       capture_output=True, check=True, timeout=60)
        os.chmod(temporary_certificate, 0o600)
        os.chmod(temporary_key, 0o600)
        os.replace(temporary_certificate, certificate)
        os.replace(temporary_key, private_key)
    except (OSError, subprocess.SubprocessError) as error:
        raise HostIdentityError("tls_generation_failed") from error
    finally:
        for leftover in (temporary_certificate, temporary_key):
            if os.path.exists(leftover):
                os.unlink(leftover)
    return {"certificate": str(certificate), "private_key": str(private_key),
            "name": name, "addresses": addresses,
            "tls_fingerprint_sha256": certificate_fingerprint(certificate)}


def ensure_certificate(directory, *, name=None, addresses=None) -> dict:
    """Generate the certificate once; an existing pair is never replaced."""
    directory = Path(directory)
    certificate, private_key = directory / CERTIFICATE_NAME, directory / PRIVATE_KEY_NAME
    if certificate.is_file() and private_key.is_file():
        return {"certificate": str(certificate), "private_key": str(private_key),
                "name": name or host_name(), "addresses": list(addresses or []),
                "created": False, "tls_fingerprint_sha256": certificate_fingerprint(certificate)}
    return {**generate_certificate(directory, name=name, addresses=addresses), "created": True}


ENDPOINT_LIMIT = 16


def typed_endpoints(addresses, name, port, tailnet_reading=None) -> list[dict]:
    """TAIL-2 P0-2: every road to this host, each saying which road it is.

    `lan` addresses first (IPv4 before IPv6, as before, so a client from
    before TAIL-2 that dials the first row keeps dialing the LAN), then the
    mDNS name, then the MagicDNS name - the only tailnet road an iOS app can
    dial (ATS) - then the tailnet addresses, which a client keeps only to
    recognise a typed 100.x as this host.
    """
    from .tailnet import is_tailnet_address
    lan, tail = [], []
    for value in addresses or []:
        (tail if is_tailnet_address(value) else lan).append(str(value))
    lan.sort(key=lambda value: ":" in value)
    rows = [{"host": value, "port": port, "kind": "lan"} for value in lan]
    if name:
        rows.append({"host": f"{name}.local", "port": port, "kind": "mdns"})
    reading = tailnet_reading if isinstance(tailnet_reading, dict) and tailnet_reading.get("running") else None
    if reading and reading.get("dns_name"):
        rows.append({"host": reading["dns_name"], "port": port, "kind": "tailnet"})
    for value in list((reading or {}).get("ips") or []) + tail:
        if not any(row["host"] == value for row in rows):
            rows.append({"host": value, "port": port, "kind": "tailnet"})
    return rows[:ENDPOINT_LIMIT]


@dataclass
class HostIdentity:
    """What a companion needs to reach this host again and recognise it."""
    host_id: str
    host_name: str
    certificate: str | None = None
    port: int = 8099
    addresses: list[str] = field(default_factory=list)
    # TAIL-2: a callable answering the cached `tailnet.reading()`, or None.
    tailnet: object = None
    # LITE-1b C7: a callable answering Odomo's https URL from the
    # installer's `tailscale serve` record, or None. Authenticated documents
    # only (roads, the claim) - never `/health`.
    odomo: object = None
    # LITE-1d: a callable answering how many of this host's web devices hold a
    # live push subscription (an int), or None when this host cannot say.
    odomo_push: object = None
    _seen: list[str] | None = field(default=None, repr=False)

    @classmethod
    def load(cls, config_dir, *, certificate=None, port=8099):
        return cls(load_host_id(config_dir), host_name(), certificate, port)

    def fingerprint(self) -> str | None:
        return certificate_fingerprint(self.certificate) if self.certificate else None

    def refresh_addresses(self) -> list[str]:
        """Re-read the host's addresses; called off the event loop."""
        self._seen = host_addresses()
        return self._seen

    def tailnet_reading(self):
        try:
            return self.tailnet() if callable(self.tailnet) else None
        except Exception:
            return None

    def endpoints(self) -> list[dict]:
        values = self.addresses or (self._seen if self._seen is not None else host_addresses())
        return typed_endpoints(values, self.host_name, self.port, self.tailnet_reading())

    def health(self) -> dict:
        # Unauthenticated: an anchor (who, which certificate), never where
        # this host can be reached. host_id + fingerprint is all a paired app
        # needs to recognise the same host on another address (TAIL-2).
        return {"host_id": self.host_id, "tls_fingerprint_sha256": self.fingerprint()}

    def roads(self) -> dict:
        """`GET /v1/host/roads` (authenticated): every road and the tailnet name.

        TAIL-2 follow-up: the typed endpoints and the MagicDNS name used to be
        on `/health`, which told anyone who could reach port 8099 this host's
        LAN addresses and its tailnet's name. They are for a paired device.
        """
        from .tailnet import public
        value = {"host_id": self.host_id, "tls_fingerprint_sha256": self.fingerprint(),
                 "endpoints": self.endpoints()}
        tailnet = public(self.tailnet_reading())
        if tailnet is not None:
            value["tailnet"] = tailnet
        self._add_odomo(value)
        return value

    def _add_odomo(self, value: dict) -> None:
        """`odomo_url` (LITE-1b) and, LITE-1d, `odomo: {url, push_devices}`.

        `odomo` is there whenever this host knows either half: the url (null
        without a front door) and how many web devices it can push to. A
        native app reads it to stop offering Odomo once a phone has it.
        """
        odomo = self.odomo_url()
        if odomo is not None:
            value["odomo_url"] = odomo
        devices = self.odomo_push_devices()
        if odomo is not None or devices is not None:
            value["odomo"] = {"url": odomo}
            if devices is not None:
                value["odomo"]["push_devices"] = devices

    def odomo_push_devices(self):
        try:
            value = self.odomo_push() if callable(self.odomo_push) else None
        except Exception:
            return None
        return value if type(value) is int and value >= 0 else None

    def odomo_url(self):
        try:
            value = self.odomo() if callable(self.odomo) else None
        except Exception:
            return None
        return value if isinstance(value, str) and value.startswith("https://") else None

    def descriptor(self) -> dict:
        value = {"host_id": self.host_id, "host_name": self.host_name,
                 "tls_fingerprint_sha256": self.fingerprint(), "endpoints": self.endpoints()}
        self._add_odomo(value)
        return value
