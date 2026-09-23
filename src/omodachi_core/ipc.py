"""Bounded JSONL IPC with same-UID peer authentication and explicit subscriptions."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict
import inspect
import json
import os
import socket
import stat
import struct
from typing import Any

from .auth import CredentialError
from .hub import Hub
from .runtime_paths import SOCKET_DIR_MODE, SOCKET_MODE

# Local operations a *root* peer may also perform. There is exactly one, and it
# exists because PAM runs `pam_exec` as root: `sudo` is setuid, so the helper
# asking "may this prompt be satisfied" cannot be the daemon's own uid.
#
# This is not a privilege grant. Root can already do anything on this machine,
# including reading this socket whatever its mode; what the allowance does is
# let the daemon answer instead of hanging up. The operation itself is
# read-only with respect to the daemon: it mints an approval request, publishes
# it to devices, and returns a boolean. Every other `local.*` operation - the
# ones that revoke devices, approve pairings, or change preferences - stays
# strictly same-uid, so root gains nothing over this socket that it lacked.
ROOT_LOCAL_OPS = frozenset({"local.auth.approve"})
# An approval is a human being picking up a tablet. The default 11 s belongs to
# operations that talk to the local desktop.
LOCAL_TIMEOUTS = {"local.auth.approve": 150.0}
DEFAULT_LOCAL_TIMEOUT = 11.0


class JsonLineServer:
    def __init__(self, hub: Hub, path: str, *, require_same_uid: bool = True,
                 max_frame_bytes: int = 65536, request_timeout: float = 5.0,
                 write_timeout: float = 5.0, local_handler=None,
                 compatibility_link: str | None = None):
        if type(max_frame_bytes) is not int or max_frame_bytes < 256:
            raise ValueError("frame limit must be at least 256 bytes")
        if request_timeout <= 0 or write_timeout <= 0:
            raise ValueError("IPC timeouts must be positive")
        self.hub = hub
        self.local_handler = local_handler
        self.path = str(path)
        # AUTH-2: the pre-move path, kept pointing at the real one for a
        # release cycle. None means "do not maintain one".
        self.compatibility_link = None if compatibility_link is None else str(compatibility_link)
        self._linked = False
        self.require_same_uid = require_same_uid
        self.max_frame_bytes = max_frame_bytes
        self.request_timeout = request_timeout
        self.write_timeout = write_timeout
        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._connections: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._remote_attached = False

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("IPC server is already running")
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, mode=SOCKET_DIR_MODE, exist_ok=True)
        # `makedirs` applies the umask, and an existing directory keeps whatever
        # mode it had. The mode is load-bearing now that this lives in the
        # runtime directory: it is the only thing between the socket and every
        # other process that can read `/run/omodachi` or `/run/user/<uid>`.
        with suppress(OSError):
            if os.stat(directory).st_uid == os.getuid():
                os.chmod(directory, SOCKET_DIR_MODE)
        self._reclaim_dead_socket()
        # Binding our own socket avoids asyncio's removal of an existing socket
        # path: a live listener is never taken over.
        raw_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            raw_socket.bind(self.path)
            info = os.lstat(self.path)
            self._socket_identity = (info.st_dev, info.st_ino)
            os.chmod(self.path, SOCKET_MODE)
            self._link_compatibility_path()
            raw_socket.setblocking(False)
            self._server = await asyncio.start_unix_server(
                self._handle, sock=raw_socket, limit=self.max_frame_bytes,
            )
            remote=getattr(self.hub,"remote",None)
            if remote is not None:
                await remote.attach_transport()
                self._remote_attached = True
        except BaseException:
            raw_socket.close()
            self._unlink_compatibility_path()
            self._remove_owned_socket()
            raise

    @staticmethod
    def _nobody_listening(path: str) -> bool:
        """True only for an explicit ECONNREFUSED: a live listener always wins."""
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(1.0)
            probe.connect(path)
            return False
        except ConnectionRefusedError:
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def _link_compatibility_path(self) -> None:
        """Point the pre-AUTH-2 path at the socket, for one release cycle.

        The link exists exactly as long as the daemon does, which is the only
        honest lifetime for it: a symlink left behind after a shutdown is a
        path that looks like a daemon and is not one.

        Nothing that is not ours is ever replaced. A symlink is ours to
        rewrite; a dead socket of our own uid is the inode a pre-AUTH-2 daemon
        left behind and is reclaimed the same way `start()` reclaims its own;
        anything else - a regular file, a live socket, another user's
        anything - is left alone and the link is simply not made.
        """
        link = self.compatibility_link
        if not link or os.path.abspath(link) == os.path.abspath(self.path):
            return
        try:
            os.makedirs(os.path.dirname(link) or ".", mode=0o700, exist_ok=True)
            try:
                info = os.lstat(link)
            except FileNotFoundError:
                info = None
            if info is not None:
                replaceable = stat.S_ISLNK(info.st_mode) or (
                    stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()
                    and self._nobody_listening(link))
                if not replaceable:
                    return
            temporary = link + ".omodachi-new"
            with suppress(FileNotFoundError):
                os.unlink(temporary)
            os.symlink(self.path, temporary)
            os.replace(temporary, link)
            self._linked = True
        except OSError:
            # A compatibility path that cannot be written is not a reason for
            # the daemon not to start. The real socket is already bound.
            self._linked = False

    def _unlink_compatibility_path(self) -> None:
        if not self._linked or not self.compatibility_link:
            return
        self._linked = False
        with suppress(OSError):
            info = os.lstat(self.compatibility_link)
            if stat.S_ISLNK(info.st_mode) and os.readlink(self.compatibility_link) == self.path:
                os.unlink(self.compatibility_link)

    def _reclaim_dead_socket(self) -> None:
        """Remove our own socket inode when nothing is listening on it.

        `kill -9` leaves the inode behind, and bind() then fails with EADDRINUSE
        for every restart — which would also block the Remote journal recovery
        that runs at startup. A live listener still wins: the probe connects
        first and only an explicit ECONNREFUSED from the same inode, owned by
        this user, permits the unlink. Regular files are never touched.
        """
        try:
            before = os.lstat(self.path)
        except (FileNotFoundError, NotADirectoryError):
            return
        if not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.getuid():
            return
        if not self._nobody_listening(self.path):
            return  # somebody is listening; bind() must fail
        after = os.lstat(self.path)
        if (stat.S_ISSOCK(after.st_mode) and after.st_uid == os.getuid()
                and (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)):
            os.unlink(self.path)

    def _remove_owned_socket(self) -> None:
        if self._socket_identity is None:
            return
        try:
            info = os.lstat(self.path)
            if (stat.S_ISSOCK(info.st_mode)
                    and (info.st_dev, info.st_ino) == self._socket_identity):
                os.unlink(self.path)
        except FileNotFoundError:
            pass
        finally:
            self._socket_identity = None

    async def close(self) -> None:
        # Python 3.13+ wait_closed also waits for active connections. Close and
        # cancel those before waiting for listener shutdown.
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for writer in tuple(self._writers):
            writer.close()
        tasks = [task for task in self._connections if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        try:
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), self.write_timeout)
            if server is not None:
                await asyncio.wait_for(server.wait_closed(), self.write_timeout)
        finally:
            self._unlink_compatibility_path()
            self._remove_owned_socket()
            desktop=getattr(self.hub,"_desktop_control",None)
            if desktop is not None and self._desktop_attached:
                self._remote_attached = False
                await desktop.detach_transport()

    def _peer_uid(self, writer: asyncio.StreamWriter) -> int | None:
        peer = writer.get_extra_info("socket")
        if peer is None:
            return None
        try:
            if hasattr(peer, "getpeereid"):
                return peer.getpeereid()[0]
            if hasattr(socket, "SO_PEERCRED"):
                raw = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                return struct.unpack("3i", raw)[1]
            if hasattr(socket, "LOCAL_PEERCRED"):
                # Darwin's xucred begins with version:uint32, uid:uint32.
                raw = peer.getsockopt(0, socket.LOCAL_PEERCRED, 256)
                if len(raw) >= 8:
                    version, uid = struct.unpack_from("=II", raw)
                    if version == 0:
                        return uid
        except (OSError, ValueError, struct.error):
            return None
        return None

    async def _send(self, writer: asyncio.StreamWriter, data: dict[str, Any]) -> None:
        writer.write((json.dumps(data, separators=(",", ":"), allow_nan=False) + "\n").encode())
        await asyncio.wait_for(writer.drain(), self.write_timeout)

    async def _stream(self, request, device_id, reader, writer, *, local=False) -> None:
        since = request.get("since", 0)
        if type(since) is not int or since < 0:
            raise ValueError("event cursor must be a nonnegative integer")
        options = {"local": True} if local else {}
        subscription = self.hub.subscribe(since=since, device_id=device_id, token=request["token"],
                                          instance_id=request.get("instance_id"), **options)
        disconnected = asyncio.create_task(reader.read(1))
        next_event = None
        after_cursor = since
        try:
            await self._send(writer, {"ok": True, "result": {"subscribed": True, "instance_id": self.hub.instance_id,
                                                                  "cursor": self.hub.event_cursor}})
            while True:
                next_event = asyncio.create_task(anext(subscription))
                finished, _ = await asyncio.wait({disconnected, next_event}, return_when=asyncio.FIRST_COMPLETED)
                if disconnected in finished:
                    # JSONL subscription accepts no additional client frames.
                    return
                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    return
                await self._send(writer, {"event": asdict(event), "instance_id": self.hub.instance_id,
                                          "after_cursor": after_cursor})
                after_cursor = event.seq
        finally:
            for task in (disconnected, next_event):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (disconnected, next_event) if task is not None),
                                 return_exceptions=True)
            await subscription.aclose()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._connections.add(task)
        self._writers.add(writer)
        try:
            peer_uid = self._peer_uid(writer)
            if self.require_same_uid:
                # Root is let as far as the first frame and no further: the
                # operation it names is checked against ROOT_LOCAL_OPS below,
                # and anything else is refused exactly as a stranger would be.
                # PAM runs `pam_exec` as root because `sudo` is setuid, so
                # hanging up before reading the frame is how AUTH-1's helper
                # ends up with a broken pipe and the user ends up at the
                # password prompt for no reason.
                if peer_uid is None or peer_uid not in (os.getuid(), 0):
                    raise PermissionError("IPC peer credentials could not be verified")
            try:
                frame = await asyncio.wait_for(reader.readuntil(b"\n"), self.request_timeout)
            except asyncio.IncompleteReadError as exc:
                if not exc.partial:
                    return
                raise ValueError("incomplete IPC frame") from exc
            except asyncio.LimitOverrunError as exc:
                raise ValueError("IPC frame exceeds limit") from exc
            if len(frame) > self.max_frame_bytes:
                raise ValueError("IPC frame exceeds limit")
            request = json.loads(frame)
            if not isinstance(request, dict) or not isinstance(request.get("op"), str):
                raise ValueError("IPC request requires an operation object")
            device_id = None
            if (self.require_same_uid and peer_uid != os.getuid()
                    and request["op"] not in ROOT_LOCAL_OPS):
                raise PermissionError("IPC peer credentials could not be verified")
            if request["op"].startswith("local."):
                # Local authority comes exclusively from the connected Unix
                # peer, even on a test server with ordinary UID checks disabled.
                operation = request["op"]
                def permitted(uid):
                    return uid is not None and (uid == os.getuid()
                                                or (uid == 0 and operation in ROOT_LOCAL_OPS))
                uid = self._peer_uid(writer)
                if not permitted(uid) or self.local_handler is None:
                    raise PermissionError("permission_denied")
                if "token" in request:
                    raise PermissionError("local_token_not_accepted")
                def local_authorize():
                    return permitted(self._peer_uid(writer)) and not writer.is_closing()
                result = self.local_handler(operation,
                    {k: v for k, v in request.items() if k != "op"}, local_authorize=local_authorize)
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(
                        result, LOCAL_TIMEOUTS.get(operation, DEFAULT_LOCAL_TIMEOUT))
                await self._send(writer, {"ok": True, "result": result})
                return
            # CORE-2: a peer this socket has proven to be the same user is
            # "local" for the credential check - the one place the panel's own
            # credential does not age out (DeviceAuthenticator.verify).
            local = self.require_same_uid and peer_uid == os.getuid()
            if request["op"] != "health":
                device_id = (self.hub.authenticate(request.get("token"), local=True) if local
                             else self.hub.authenticate(request.get("token")))
            if request["op"] == "events.subscribe":
                await self._stream(request, device_id, reader, writer, local=local)
            else:
                result = self.dispatch(request, device_id)
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, self.request_timeout)
                await self._send(writer, {"ok": True, "result": result})
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception as exc:
            try:
                message = "IPC request timed out" if isinstance(exc, asyncio.TimeoutError) else str(exc)
                # A refused credential keeps the IPC name it always had
                # (`ipc-envelope.schema.json` fixes the frame); the reason is
                # an HTTPS/WSS addition (CORE-2).
                name = "ValueError" if isinstance(exc, CredentialError) else type(exc).__name__
                await self._send(writer, {"ok": False, "error": getattr(exc, "code", name), "message": message})
            except (ConnectionError, OSError, asyncio.TimeoutError):
                pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), self.write_timeout)
            except (ConnectionError, OSError, asyncio.TimeoutError):
                pass
            self._connections.discard(task)
            self._writers.discard(writer)

    def dispatch(self, request: dict[str, Any], device_id=None):
        return self.hub.dispatch(request["op"],
                                 {key: value for key, value in request.items() if key not in {"op", "token"}},
                                 device_id)


class JsonLineClient:
    def __init__(self, path, token=None, *, timeout=5.0, max_response_bytes=4 * 1024 * 1024):
        self.path, self.token = path, token
        self.timeout, self.max_response_bytes = timeout, max_response_bytes

    async def request(self, op, **params):
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self.path, limit=self.max_response_bytes), self.timeout,
        )
        try:
            request = {"op": op, **params}
            if self.token:
                request["token"] = self.token
            writer.write((json.dumps(request, allow_nan=False) + "\n").encode())
            await asyncio.wait_for(writer.drain(), self.timeout)
            return json.loads(await asyncio.wait_for(reader.readline(), self.timeout))
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), self.timeout)
            except (ConnectionError, asyncio.TimeoutError):
                pass
