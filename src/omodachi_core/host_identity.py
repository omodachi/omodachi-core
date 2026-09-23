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


@dataclass
class HostIdentity:
    """What a companion needs to reach this host again and recognise it."""
    host_id: str
    host_name: str
    certificate: str | None = None
    port: int = 8099
    addresses: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, config_dir, *, certificate=None, port=8099):
        return cls(load_host_id(config_dir), host_name(), certificate, port)

    def fingerprint(self) -> str | None:
        return certificate_fingerprint(self.certificate) if self.certificate else None

    def endpoints(self) -> list[dict]:
        values = self.addresses or host_addresses()
        return [{"host": address, "port": self.port} for address in values]

    def health(self) -> dict:
        return {"host_id": self.host_id, "tls_fingerprint_sha256": self.fingerprint()}

    def descriptor(self) -> dict:
        return {"host_id": self.host_id, "host_name": self.host_name,
                "tls_fingerprint_sha256": self.fingerprint(), "endpoints": self.endpoints()}
