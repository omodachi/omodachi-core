"""Authenticated Unix snapshot/event helper for the Omarchy plugin.

QML runs the fixed argv ``omodachi-host plugin-watch``. Local provisioning puts
an already issued device credential in a private file; credentials never enter
stdout, event JSON, QML settings or command arguments. No pairing is invented.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import stat
from typing import Callable, Mapping

from .ipc import JsonLineClient

MAX_FRAME = 4 * 1024 * 1024


class BridgeError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def plugin_credential(environment: Mapping[str, str] | None = None) -> str:
    environment = os.environ if environment is None else environment
    token = environment.get("OMODACHI_TOKEN")
    if not token:
        candidate = Path(environment.get("OMODACHI_TOKEN_FILE") or (Path.home() / ".config/omodachi/plugin.token"))
        try:
            descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise BridgeError("permission_required")
                token = stream.read(4097).decode("ascii").strip()
        except (OSError, UnicodeError):
            raise BridgeError("permission_required") from None
    if not isinstance(token, str) or len(token) > 4096 or token.count(".") != 1 or any(char.isspace() for char in token):
        raise BridgeError("permission_required")
    return token


def check_response(response: dict) -> dict:
    if not isinstance(response, dict):
        raise BridgeError("error")
    if response.get("ok") is not True:
        kind = response.get("error")
        message = response.get("message", "")
        if kind in {"PermissionError", "permission_denied", "pairing_required"} or "credential" in str(message):
            raise BridgeError("permission_required")
        raise BridgeError("error")
    result = response.get("result")
    if not isinstance(result, dict):
        raise BridgeError("error")
    return result


def encode_frame(value: dict) -> str:
    """The frame limit includes the newline and measures actual UTF-8 bytes."""
    try:
        line = json.dumps(value, separators=(",", ":"), allow_nan=False, ensure_ascii=False)
        size = len(line.encode("utf-8")) + 1
    except (ValueError, TypeError, UnicodeError):
        raise BridgeError("error") from None
    if size > MAX_FRAME:
        raise BridgeError("unsupported")
    return line


def emit_bounded(emit, value: dict) -> None:
    encode_frame(value)
    emit(value)


def validate_snapshot(value: dict) -> None:
    if not isinstance(value.get("instance_id"), str) or not value["instance_id"]:
        raise BridgeError("unsupported")
    if type(value.get("revision")) is not int or type(value.get("event_cursor")) is not int:
        raise BridgeError("unsupported")
    if not all(isinstance(value.get(key), dict) for key in ("host", "agent", "herdr", "capabilities", "remote", "catalog")):
        raise BridgeError("unsupported")


async def watch_once(socket_path: str, token: str, emit: Callable[[dict], None], *, timeout=5.0) -> None:
    # Events between this snapshot and subscription are replayed by (instance,
    # cursor). A process restart in between yields resync, never a stale merge.
    response = await JsonLineClient(socket_path, token, timeout=timeout).request("state")
    snapshot = check_response(response)
    validate_snapshot(snapshot)
    emit_bounded(emit, {"ok": True, "result": snapshot})
    cursor = snapshot["event_cursor"]
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(socket_path, limit=MAX_FRAME), timeout)
    try:
        writer.write((json.dumps({"op": "events.subscribe", "token": token, "since": snapshot["event_cursor"],
                                 "instance_id": snapshot["instance_id"]}) + "\n").encode())
        await asyncio.wait_for(writer.drain(), timeout)
        acknowledgment = json.loads(await asyncio.wait_for(reader.readline(), timeout))
        check_response(acknowledgment)
        async for raw in reader:
            if len(raw) > MAX_FRAME:
                raise BridgeError("error")
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise BridgeError("error")
            if "event" not in message:
                check_response(message)
                continue
            event = message["event"]
            if not isinstance(event, dict) or type(event.get("seq")) is not int or not isinstance(event.get("payload"), dict):
                raise BridgeError("error")
            # after_cursor is authenticated transport metadata: it links this
            # visible event to the last visible event, even when other devices'
            # private events occupied intervening global sequence numbers.
            after = message.get("after_cursor")
            if event.get("type") == "resync.required":
                emit_bounded(emit, {"event": event, "instance_id": message.get("instance_id"),
                                    "after_cursor": cursor if after is None else after})
                return
            if message.get("instance_id") != snapshot["instance_id"]:
                raise BridgeError("unavailable")
            if after is None:
                # Legacy peers have no continuity proof for filtered jumps.
                if event["seq"] != cursor + 1:
                    raise BridgeError("unavailable")
                after = cursor
            if type(after) is not int or after != cursor or event["seq"] <= cursor:
                raise BridgeError("unavailable")
            emit_bounded(emit, {"event": event, "instance_id": message["instance_id"], "after_cursor": after})
            cursor = event["seq"]
        raise BridgeError("unavailable")
    finally:
        writer.close()
        with suppress(OSError, ConnectionError, asyncio.TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), timeout)


async def watch(socket_path: str | Callable[[], str], *, credential_loader=plugin_credential, emit=None,
                retry_delay=1.0, max_retry_delay=8.0, stop: asyncio.Event | None = None) -> None:
    """Forward snapshots and events until `stop`, reconnecting on failure.

    `socket_path` is either a fixed path (the user passed `--socket`) or a
    resolver that is called again before every connection attempt. RELEASE-3b:
    `install_host.py --pam` moves the daemon from `/run/user/<uid>/omodachi/`
    to `/run/omodachi/<uid>/` and restarts it while this helper keeps running;
    a path resolved once at startup pointed at the old directory for the rest
    of the helper's life, and the panel said there was no Host until the user
    pressed Reconnect.
    """
    if emit is None:
        emit = lambda value: print(encode_frame(value), flush=True)
    stop = stop or asyncio.Event()
    delay = retry_delay
    last_error = None
    def forward(value):
        nonlocal last_error, delay
        last_error = None
        delay = retry_delay
        emit(value)
    while not stop.is_set():
        connection = None
        cancelled = None
        try:
            token = credential_loader()
            path = socket_path() if callable(socket_path) else socket_path
            connection = asyncio.create_task(watch_once(path, token, forward))
            cancelled = asyncio.create_task(stop.wait())
            finished, _ = await asyncio.wait({connection, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in finished:
                break
            await connection
            continue  # an explicit resync gets a new snapshot immediately
        except BridgeError as exc:
            code = exc.code
        except (FileNotFoundError, ConnectionRefusedError):
            code = "setup_required"
        except (OSError, ConnectionError, asyncio.TimeoutError):
            code = "unavailable"
        except (ValueError, TypeError):
            code = "error"
        finally:
            for task in (connection, cancelled):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (connection, cancelled) if task is not None), return_exceptions=True)
        if code != last_error:
            emit({"ok": False, "error": code, "message": code})
            last_error = code
        try:
            await asyncio.wait_for(stop.wait(), delay)
        except asyncio.TimeoutError:
            pass
        delay = min(max_retry_delay, delay * 2)
