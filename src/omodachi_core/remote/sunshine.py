"""Private bounded JSONL client for the managed Sunshine Desktop protocol.

No HTTP password, pairing secret or user configuration is read here. Endpoint
and peer identities are checked before sending any lease-scoped command.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import stat
import struct
import time

from .errors import RemoteError

PROTOCOL = "omodachi.sunshine.desktop.v1"
OPERATIONS = frozenset({"desktop.status", "desktop.claim", "desktop.prepare", "desktop.session", "desktop.stop", "desktop.release", "media.list", "media.get"})


def read_private_json(path: Path, limit=65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RemoteError("desktop_config_unsafe")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise RemoteError("desktop_config_too_large")
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value: raise ValueError()
            value[key] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict): raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise RemoteError("desktop_config_invalid") from None


class SunshineDesktopIPC:
    def __init__(self, path: Path, *, timeout=2.0):
        self.path = Path(path)
        if not self.path.is_absolute() or not 0 < timeout <= 5:
            raise RemoteError("sunshine_ipc_config_invalid")
        self.timeout = timeout

    def request(self, op, **fields):
        if op not in OPERATIONS:
            raise RemoteError("sunshine_ipc_operation_invalid")
        message = {"op":op,**fields}
        if op.startswith('desktop.'):message['protocol']=PROTOCOL
        elif (op=='media.list' and fields) or (op=='media.get' and (set(fields)!={'session_id'} or not isinstance(fields['session_id'],str))):
            raise RemoteError('sunshine_ipc_request_invalid')
        raw = (json.dumps(message, allow_nan=False, separators=(',', ':')) + '\n').encode()
        if len(raw) > 4096: raise RemoteError("sunshine_ipc_request_limit")
        try:
            # The socket and its immediate private same-UID parent must be
            # real entries. Peer UID/process is checked after connecting.
            parent = self.path.parent.lstat()
            endpoint = self.path.lstat()
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077
                    or not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != os.getuid() or endpoint.st_mode & 0o077):
                raise RemoteError("sunshine_ipc_unsafe")
            deadline = time.monotonic() + self.timeout
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(str(self.path))
                self._verify_peer(client)
                client.sendall(raw)
                response = bytearray()
                while b'\n' not in response:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0: raise TimeoutError()
                    client.settimeout(remaining)
                    chunk = client.recv(min(4096, 32769-len(response)))
                    if not chunk: raise RemoteError("sunshine_ipc_incomplete")
                    response.extend(chunk)
                    if len(response) > 32768: raise RemoteError("sunshine_ipc_response_limit")
                if response.count(b'\n') != 1 or not response.endswith(b'\n'):
                    raise RemoteError("sunshine_ipc_invalid")
                value = json.loads(response)
            if not isinstance(value, dict) or (op.startswith('desktop.') and value.get('protocol') != PROTOCOL):
                raise RemoteError("sunshine_ipc_version_mismatch")
            if value.get('ok') is not True:
                # Remote errors and logs are untrusted; never echo arbitrary
                # messages, request bodies, certificate data or URLs.
                code = value.get('error', {}).get('code') if isinstance(value.get('error'), dict) else None
                allowed = {'desktop_busy', 'stale_generation', 'capture_unavailable', 'session_not_stopped', 'binding_mismatch'}
                raise RemoteError(code if code in allowed else 'sunshine_ipc_rejected')
            if op=='media.list':
                rows=value.get('sessions')
                if not isinstance(rows,list) or len(rows)>16:raise RemoteError('sunshine_ipc_invalid')
                return {'sessions':rows}
            result = value.get('session') if op=='media.get' else value.get('result')
            if not isinstance(result, dict): raise RemoteError("sunshine_ipc_invalid")
            return result
        except RemoteError:
            raise
        except (OSError, ValueError, UnicodeError):
            raise RemoteError("sunshine_ipc_unavailable") from None

    @staticmethod
    def _verify_peer(client):
        if hasattr(socket, 'SO_PEERCRED'):
            pid,uid,_=struct.unpack('3i',client.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if uid!=os.getuid():raise RemoteError('sunshine_ipc_peer_mismatch')
            process=Path('/proc')/str(pid)
            if (process/'comm').read_text().strip()!='sunshine':raise RemoteError('sunshine_ipc_peer_mismatch')
        elif hasattr(client,'getpeereid'):
            if client.getpeereid()[0]!=os.getuid():raise RemoteError('sunshine_ipc_peer_mismatch')
        else:
            # macOS supports getpeereid in libc even on Python builds without
            # the socket convenience method. Production host uses SO_PEERCRED.
            import sys
            if sys.platform!='darwin':raise RemoteError('sunshine_ipc_peer_unavailable')
            import ctypes
            libc=ctypes.CDLL(None,use_errno=True)
            uid,gid=ctypes.c_uint(),ctypes.c_uint()
            if libc.getpeereid(client.fileno(),ctypes.byref(uid),ctypes.byref(gid)) or uid.value!=os.getuid():
                raise RemoteError('sunshine_ipc_peer_mismatch')
