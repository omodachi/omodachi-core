#!/usr/bin/env python3
"""A stand-in iPad: pair, enrol a P-256 key, hold the event stream, sign.

This is the only thing in the harness that acts like a companion, and it acts
like one for real: it pairs over the HTTP API, enrols a public key with a
proof-of-possession signature, keeps a live `/v1/events` WebSocket open (which
is what makes it *eligible* to be asked), and answers an approval by signing
the exact bytes `biometric.approval_message` defines.

Its private key is a plain integer in this process. That is the one thing it
cannot imitate about an iPad - there is no Secure Enclave in a container - and
it is also the one thing the host never sees either way, which is the point:
the host verifies a signature and learns nothing about where it came from.

`--behaviour` is what each Docker case turns:

  approve  sign every approval
  decline  answer `decline`, so PAM gets its refusal immediately
  ignore   receive the approval and do nothing, so the host times out
  replay   sign, submit, then submit the identical body a second time, which
           is the nonce-reuse case: the host must refuse the repeat
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import ssl
import sys

import aiohttp

sys.path.insert(0, os.environ.get("OMODACHI_SRC", "/opt/omodachi/src"))
from omodachi_core.biometric import _G, _N, _add, _multiply, approval_message, enrollment_message


def sign(private, message: bytes) -> bytes:
    """ECDSA P-256 / SHA-256, DER encoded. The device half of the contract."""
    digest = int.from_bytes(hashlib.sha256(message).digest(), "big")
    while True:
        k = secrets.randbelow(_N - 1) + 1
        point = _multiply(k, _G)
        r = point[0] % _N
        if r == 0:
            continue
        s = pow(k, _N - 2, _N) * (digest + r * private) % _N
        if s == 0:
            continue
        return _der(r, s)


def _der(r, s) -> bytes:
    def integer(value):
        raw = value.to_bytes((value.bit_length() + 8) // 8 or 1, "big")
        return bytes([0x02, len(raw)]) + raw
    body = integer(r) + integer(s)
    return bytes([0x30, len(body)]) + body


def public_key(private) -> bytes:
    point = _multiply(private, _G)
    return b"\x04" + point[0].to_bytes(32, "big") + point[1].to_bytes(32, "big")


class FakeDevice:
    def __init__(self, base, device_id, name, behaviour, log, device_enabled=True,
                 insecure=False):
        self.base, self.device_id, self.name = base.rstrip("/"), device_id, name
        # AUTH-2: the same stand-in, pointed at a real host over its self-signed
        # HTTPS. A companion pins the host's fingerprint at pairing; this tool
        # is a harness and skips that, which is why it takes saying so.
        self.insecure = insecure
        self.behaviour, self.log = behaviour, log
        # Leo's rule: both ends opt in. This is the device's end, and it is
        # what the App's "approve host prompts with Face ID" switch reports.
        self.device_enabled = device_enabled
        self.private = secrets.randbelow(_N - 1) + 1
        self.token = None
        self.host_id = None
        self.approvals = 0

    def say(self, **entry):
        print(json.dumps({"device": self.device_id, **entry}), flush=True)

    async def pair(self, session, request_id_path="/v1/pairing/requests"):
        async with session.post(self.base + request_id_path,
                                json={"device_id": self.device_id, "device_name": self.name}) as response:
            body = await response.json()
        request_id, secret = body["request_id"], body["request_secret"]
        self.say(step="pairing_requested", request_id=request_id)
        # The local approval is the harness's job (it runs `omodachi-host pair
        # approve` as the owner), exactly as a person would on the host.
        process = await asyncio.create_subprocess_exec(
            "omodachi-host", "pair", "approve", request_id, "--no-remote",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await process.communicate()
        self.say(step="pair_approved", rc=process.returncode, out=out.decode()[:400])
        async with session.post(f"{self.base}/v1/pairing/requests/{request_id}/claim",
                                json={"request_secret": secret}) as response:
            claim = await response.json()
        self.token = claim["credential"]
        self.host_id = claim.get("host_id")
        self.say(step="claimed", host_id=self.host_id, grants=claim.get("grants"))

    @property
    def headers(self):
        return {"Authorization": "Bearer " + self.token}

    async def enrol(self, session):
        async with session.get(self.base + "/v1/auth/keys", headers=self.headers) as response:
            info = await response.json()
        challenge, host_id = info["challenge"], info["host_id"]
        encoded = base64.b64encode(public_key(self.private)).decode()
        message = enrollment_message(host_id=host_id, device_id=self.device_id,
                                     challenge=challenge, public_key_b64=encoded)
        payload = {"public_key": encoded, "label": self.name, "challenge": challenge,
                   "secure_enclave": False, "enabled": self.device_enabled,
                   "signature": base64.b64encode(sign(self.private, message)).decode()}
        async with session.post(self.base + "/v1/auth/keys", headers=self.headers,
                                json=payload) as response:
            result = await response.json()
            self.say(step="enrolled", status=response.status, result=result)
            if response.status != 200:
                raise SystemExit(2)
        self.host_id = host_id

    async def answer(self, session, event):
        payload = event["payload"]
        self.approvals += 1
        if self.behaviour == "ignore":
            self.say(step="approval_ignored", approval_id=payload["approval_id"])
            return
        url = f"{self.base}/v1/auth/approvals/{payload['approval_id']}"
        if self.behaviour == "decline":
            body = {"decision": "decline"}
        else:
            message = approval_message(host_id=payload["host_id"], approval_id=payload["approval_id"],
                                       nonce=payload["nonce"], service=payload["service"],
                                       user=payload["user"], device_id=self.device_id)
            body = {"decision": "approve",
                    "signature": base64.b64encode(sign(self.private, message)).decode()}
        async with session.post(url, headers=self.headers, json=body) as response:
            self.say(step="approval_answered", approval_id=payload["approval_id"],
                     behaviour=self.behaviour, status=response.status, body=await response.json())
        if self.behaviour == "replay":
            # The identical bytes, a second time. One nonce, one use.
            async with session.post(url, headers=self.headers, json=body) as response:
                self.say(step="approval_replayed", approval_id=payload["approval_id"],
                         status=response.status, body=await response.json())

    async def run(self, ready_path=None):
        connector = None
        if self.insecure:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            connector = aiohttp.TCPConnector(ssl=context)
        async with aiohttp.ClientSession(connector=connector) as session:
            await self.pair(session)
            await self.enrol(session)
            url = self.base.replace("http://", "ws://").replace("https://", "wss://") + "/v1/events"
            async with session.ws_connect(url, headers=self.headers, heartbeat=20) as socket:
                self.say(step="subscribed")
                if ready_path:
                    with open(ready_path, "w") as stream:
                        stream.write("ready\n")
                async for message in socket:
                    if message.type is not aiohttp.WSMsgType.TEXT:
                        continue
                    frame = json.loads(message.data)
                    event = frame.get("event")
                    if not event:
                        continue
                    if event["type"] == "auth.approval.requested":
                        self.say(step="approval_received", payload=event["payload"])
                        await self.answer(session, event)
                    elif event["type"] == "auth.approval.resolved":
                        self.say(step="approval_resolved", payload=event["payload"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8099")
    parser.add_argument("--device-id", default="fake-ipad")
    parser.add_argument("--name", default="Docker iPad")
    parser.add_argument("--behaviour", choices=("approve", "decline", "ignore", "replay"), default="approve")
    parser.add_argument("--device-enabled", choices=("true", "false"), default="true",
                        help="the App-side switch this device reports to the host")
    parser.add_argument("--insecure", action="store_true",
                        help="accept the host's self-signed certificate (a real host, not the container)")
    parser.add_argument("--ready-file")
    parser.add_argument("--log")
    args = parser.parse_args()
    device = FakeDevice(args.base, args.device_id, args.name, args.behaviour, args.log,
                        device_enabled=args.device_enabled == "true", insecure=args.insecure)
    try:
        asyncio.run(device.run(args.ready_file))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
