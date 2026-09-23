"""HTTPS/WSS adapter. TLS is mandatory except explicit loopback development."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict
import ipaddress
import json
from pathlib import Path
import re
import ssl
import uuid
from aiohttp import web, WSMsgType

from .protocol import CONTRACT_REVISION
from .clipboard import ClipboardError, LIMIT as CLIPBOARD_LIMIT
from .service import CoreService, ServiceError, fields
from .audio_uplink import AudioUplinkError, AuthorizedAudioUplink, FORMAT
from .voice import VoiceError
from .notifications import NotificationsUnavailable
from .lan_discovery import DiscoveryConfig, ListenerDiscovery
from .network_discovery import NetworkDiscoveryBinding
from .remote.errors import RemoteError

_RequestKey = getattr(web, "RequestKey", web.AppKey)
DEVICE = _RequestKey("device", str)
TOKEN = _RequestKey("token", str)


def credential_refused(error=None, message="invalid or revoked device credential"):
    """CORE-2 §1: every 401 for a refused credential says which refusal it is.

    `error` is whatever the check raised: `auth.CredentialError` carries the
    reason; a bare `ValueError` (the token authenticated as some *other*
    device) is `unknown_credential`.
    """
    refusal = ServiceError("permission_denied", message, 401)
    refusal.reason = getattr(error, "reason", None) or "unknown_credential"
    return refusal


def renewal_document(authority, renewed):
    """`POST /v1/pairing/renew`'s body (`pairing-renew.schema.json`)."""
    return {"device_id": renewed.device_id, "credential": renewed.token,
            "issued_at": renewed.issued_at, "credential_expires_at": renewed.expires_at,
            "renewable_at": renewed.expires_at - authority.renew_window_seconds,
            # The credential that was traded in works at most until here.
            "previous_credential_expires_at": renewed.issued_at + authority.grace_seconds}


def error_response(code, message, status=400, detail=None, reason=None):
    """`detail` is the bounded, code-specific context remote-api.md promises.

    `409 remote_session_exists` has always carried the existing `session_id` in
    `RemoteError.detail`; until now the boundary dropped it on the floor, so the
    documented field simply did not exist on the wire.
    """
    error = {"code": code, "message": message}
    if isinstance(reason, str) and reason:
        error["reason"] = reason
    if isinstance(detail, dict) and detail:
        bounded = {key: value for key, value in detail.items()
                   if isinstance(key, str) and type(value) in (str, int, bool)}
        if bounded:
            error["detail"] = dict(list(bounded.items())[:8])
    return web.json_response({"contract_revision": CONTRACT_REVISION, "error": error}, status=status)


def create_app(service: CoreService, *, auth_check_interval=1.0) -> web.Application:
    @web.middleware
    async def boundary(request, handler):
        try:
            pairing_path = request.method == "POST" and (request.path == "/v1/pairing/requests" or
                bool(re.fullmatch(r"/v1/pairing/requests/pair_[0-9a-f]{32}/claim", request.path)))
            if request.path not in {"/", "/health"} and not pairing_path:
                authorization = request.headers.get("Authorization", "")
                if not authorization.startswith("Bearer ") or len(authorization) > 4096:
                    raise ServiceError("pairing_required", status=401)
                token = authorization[7:]
                try:
                    request[DEVICE] = service.hub.authenticate(token)
                except ValueError as error:
                    raise credential_refused(error)
                request[TOKEN] = token
            response = await handler(request)
            response.headers["Cache-Control"] = "no-store"
            return response
        except (ServiceError, AudioUplinkError, VoiceError, NotificationsUnavailable, RemoteError,
                ClipboardError) as exc:
            return error_response(exc.code, str(exc), exc.status, getattr(exc, "detail", None),
                                  getattr(exc, "reason", None))
        except PermissionError:
            return error_response("permission_denied", "not authorized", 403)
        except (ValueError, TypeError, KeyError) as exc:
            code = "stale_plan" if "stale_plan" in str(exc) else "invalid_request"
            if str(exc) in {"stale_catalog_revision", "menu_action_changed", "route_unavailable", "invalid_route_parameters"}:
                code = str(exc)
            return error_response(code, code, 409 if code.startswith("stale") else 400)
        except web.HTTPException as exc:
            return error_response("request_rejected", exc.reason, exc.status)

    app = web.Application(middlewares=[boundary], client_max_size=65536)

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    async def body(request):
        value = {}
        if request.can_read_body:
            try:
                value = await asyncio.wait_for(request.json(loads=lambda text: json.loads(text, object_pairs_hook=unique_object, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))), 5.0)
            except asyncio.TimeoutError:
                raise ServiceError("request_timeout", "request body timed out", 408)
        if not isinstance(value, dict):
            raise ServiceError("invalid_request")
        # Body reads yield to the event loop. A credential may have expired or
        # been revoked after middleware ran; recheck at the mutation boundary.
        try:
            if service.hub.authenticate(request[TOKEN]) != request[DEVICE]:
                raise ValueError
        except ValueError as error:
            raise credential_refused(error, "credential revoked or expired") from None
        return value

    async def pairing_request(request):
        try:
            payload = await asyncio.wait_for(request.json(), 5.0)
        except asyncio.TimeoutError:
            raise ServiceError("request_timeout", status=408)
        if "id" in request.match_info:
            # The claim response carries the certificate fingerprint and the
            # addresses this host answers on; the client pins them right here.
            result = service.pairing_claim(request.match_info["id"], payload)
        else:
            # PAIR-2: an invitation-less request is bounded per source, so the
            # peer address the socket already knows travels with it. It is
            # shown to whoever approves; it authorizes nothing.
            result = service.pairing_request(payload, remote_addr=request.remote)
        return web.json_response(result)

    async def pairing_credential(request):
        """CORE-2 §1: the caller's own credential - when it ends, when it can be renewed.

        Side-effect free apart from the one thing `verify` always does: learn
        the `iat` of a credential issued before the registry kept it.
        """
        if request.query or request.can_read_body:
            raise ServiceError("invalid_request")
        from .auth import CredentialError
        try:
            info = service.hub.auth.credential_info(request[TOKEN])
        except CredentialError as error:
            raise credential_refused(error) from None
        if info["device_id"] != request[DEVICE]:
            raise credential_refused()
        return web.json_response(service.resource(info))

    async def pairing_renew(request):
        """CORE-2 §1: trade a credential in its last week for a new one.

        The old one keeps working for the grace period, so a reply lost on the
        way back costs nothing: the device still holds a working credential and
        asks again. Revoked, purged and expired credentials are 401s with their
        reason - renewal is never a way back in.
        """
        if request.query:
            raise ServiceError("invalid_request")
        fields(await body(request))
        from .auth import CredentialError, CredentialRenewalRefused
        try:
            renewed = service.hub.auth.renew(request[TOKEN])
        except CredentialError as error:
            raise credential_refused(error) from None
        except CredentialRenewalRefused as refusal:
            detail = {key: value for key, value in (("expires_at", refusal.expires_at),
                                                    ("renewable_at", refusal.renewable_at))
                      if value is not None}
            raise ServiceError(refusal.code, refusal.code, 409, **detail) from None
        if renewed.device_id != request[DEVICE]:
            raise credential_refused()
        print(json.dumps({"omodachi": "pairing", "event": "credential_renewed",
                          "device_id": renewed.device_id, "issued_at": renewed.issued_at,
                          "expires_at": renewed.expires_at}), flush=True)
        return web.json_response(service.resource(renewal_document(service.hub.auth, renewed)))

    async def root(request):
        # The companion API and the public Web draft are separate surfaces.
        # Keep this route unauthenticated so opening the API origin in a browser
        # explains the boundary instead of looking like a failed pairing flow.
        return web.json_response({
            "service": "omodachid",
            "contract_revision": CONTRACT_REVISION,
            "message": "This is the Omodachi host API, not the Web UI.",
            "health": "/health",
            "authenticated_api": "/v1/*",
            "web_ui": "run omodachi-web with npm run dev",
        })

    async def health(request):
        return web.json_response(service.resource(service.hub.dispatch("health", {})))

    async def read(request):
        name = request.match_info["name"]
        values = {"state": lambda: service.state(request[DEVICE]),
                  "capabilities": service.hub.capabilities_snapshot,
                  "herdr": service.hub.herdr_snapshot,
                  "catalog": lambda: service.search_catalog(request.query.get("q", ""))}
        if set(request.query) - ({"q"} if name == "catalog" else set()):
            raise ServiceError("invalid_request")
        if name not in values:
            raise ServiceError("route_unavailable", status=404)
        return web.json_response(service.resource(values[name]()))

    async def action(request):
        payload = await body(request)
        if "entry_id" in payload:
            raise ServiceError("invalid_request")
        result = service.dispatch("actions.invoke", {**payload, "entry_id": request.match_info["id"]}, request[DEVICE])
        return web.json_response(result)

    async def agent_task(request):
        payload = await body(request)
        manager = getattr(service, "agent_manager", None)
        if manager is None: raise ServiceError("agent_management_unavailable", status=503)
        device, token = request[DEVICE], request[TOKEN]
        def authorize():
            try:
                if service.hub.authenticate(token) != device: raise ValueError()
            except ValueError as error:
                raise credential_refused(error) from None
        try:
            result = await asyncio.to_thread(manager.submit, payload, device, authorize)
        finally:
            await service.refresh_agent_now()
        service.hub.publish("agent.task_result", result, device_id=device)
        return web.json_response(service.resource(result))

    async def agent_ensure(request):
        payload = await body(request)
        fields(payload,(),('surface',))
        if payload.get('surface') not in (None,'chat'):raise ServiceError('invalid_request')
        manager = getattr(service, "agent_manager", None)
        if manager is None: raise ServiceError("agent_management_unavailable", status=503)
        device, token = request[DEVICE], request[TOKEN]
        def authorize():
            try:
                if service.hub.authenticate(token) != device: raise ValueError()
            except ValueError as error:
                raise credential_refused(error) from None
        try:
            if payload.get('surface')=='chat':
                chat=getattr(service,'agent_chat',None)
                if chat is None:raise ServiceError('agent_management_unavailable',status=503)
                result=await chat.ensure(authorize)
            else:result = await asyncio.to_thread(manager.ensure, authorize)
        finally:
            await service.refresh_agent_now()
        return web.json_response(service.resource(result))

    def chat_authority(request):
        device,token=request[DEVICE],request[TOKEN]
        def authorize():
            try:
                if service.hub.authenticate(token)!=device:raise ValueError()
            except ValueError as error:raise credential_refused(error) from None
        return authorize

    def chat_service():
        chat=getattr(service,'agent_chat',None)
        if chat is None:raise ServiceError('agent_management_unavailable',status=503)
        return chat

    async def chat_commands(request):
        chat_authority(request)()
        return web.json_response(service.resource(chat_service().commands.listing()))

    async def chat_command_execute(request):
        result=await chat_service().commands.execute(request.match_info['command'],await body(request),chat_authority(request))
        return web.json_response(service.resource(result))

    async def chat_recover_empty(request):
        return web.json_response(service.resource(await chat_service().recover_empty(await body(request),chat_authority(request))))

    async def chat_handoff_prepare(request):
        fields(await body(request))
        return web.json_response(service.resource(await chat_service().handoff.prepare(chat_authority(request))))

    async def chat_handoff_confirm(request):
        return web.json_response(service.resource(await chat_service().handoff.confirm(await body(request),chat_authority(request))))

    async def chat_snapshot(request):
        return web.json_response(service.resource(await chat_service().snapshot(chat_authority(request))))

    async def chat_send(request):
        return web.json_response(service.resource(await chat_service().send(await body(request),chat_authority(request))))

    async def chat_interrupt(request):
        return web.json_response(service.resource(await chat_service().cancel(await body(request),chat_authority(request))))

    async def chat_steer(request):
        return web.json_response(service.resource(await chat_service().steer(await body(request),chat_authority(request))))

    async def chat_approvals(request):
        if request.query or request.can_read_body:raise ServiceError('invalid_request')
        return web.json_response(service.resource(await chat_service().approvals(chat_authority(request))))

    async def chat_approval_resolve(request):
        result=await chat_service().approve(request.match_info['request_id'],await body(request),chat_authority(request))
        return web.json_response(service.resource(result))

    async def chat_models(request):
        if request.query or request.can_read_body:raise ServiceError('invalid_request')
        return web.json_response(service.resource(await chat_service().models(chat_authority(request))))

    async def chat_usage(request):
        if request.query or request.can_read_body:raise ServiceError('invalid_request')
        return web.json_response(service.resource(await chat_service().usage(chat_authority(request))))

    async def chat_events(request):
        """A read-only event stream that still has to read.

        aiohttp answers a client PING inside `receive()`, so a stream that only
        ever writes never pongs and a conforming client drops the connection on
        its own keepalive timeout - which is exactly the long-lived agent chat
        this surface exists for. The reader below both keeps the heartbeat
        honest and refuses anything a client tries to send.
        """
        chat=chat_service();authorize=chat_authority(request)
        await chat.require(authorize)
        queue=asyncio.Queue(maxsize=256);chat.subscribers.add(queue)
        ws=web.WebSocketResponse(heartbeat=20,max_msg_size=65536)
        async def pump(snapshot):
            await ws.send_json({'type':'snapshot','snapshot':snapshot})
            last=snapshot['sequence']
            while not ws.closed:
                authorize()
                try:event=await asyncio.wait_for(queue.get(),1)
                except asyncio.TimeoutError:continue
                if event.get('type')=='resync_required':
                    await ws.send_json(event);return
                if event['sequence']<=last:continue
                await ws.send_json(event);last=event['sequence']
                if event['event']['type']=='connectionLost':return
        async def reader():
            async for message in ws:
                if message.type in (WSMsgType.TEXT,WSMsgType.BINARY):
                    await ws.close(code=1008,message=b'read-only event stream');return
        try:
            snapshot=await chat.snapshot(authorize)
            await ws.prepare(request);sockets.add(ws)
            tasks=[asyncio.create_task(pump(snapshot)),asyncio.create_task(reader())]
            try:
                finished,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                for task in finished:
                    if not task.cancelled() and task.exception() is not None:raise task.exception()
            finally:
                for task in tasks:task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
            if not ws.closed:await ws.close()
        except (ServiceError,ConnectionError):
            if ws.prepared:await ws.close(code=1008)
            else:raise
        finally:chat.subscribers.discard(queue);sockets.discard(ws)
        return ws

    async def media_pairing(request):
        if request.query:
            raise ServiceError("invalid_request")
        operation = "discover" if request.path == "/v1/media/pairing/discover" else None
        if "attempt_id" in request.match_info:
            if request.method == "DELETE":
                fields(await body(request))
            elif request.can_read_body:
                raise ServiceError("invalid_request")
            operation = "status" if request.method == "GET" else "cancel"
            payload = {"attempt_id": request.match_info["attempt_id"]}
        else:
            payload = await body(request)
            operation = operation or "submit"
        device, token = request[DEVICE], request[TOKEN]
        def authorize():
            return service.hub.authenticate(token) == device
        return web.json_response(await service.dispatch_media_async(operation, payload, device, authorize=authorize))

    async def audio_socket(request):
        if request.query or request.can_read_body:
            raise ServiceError("invalid_request")
        device, token, lease_id = request[DEVICE], request[TOKEN], request.match_info["id"]
        audio = service.remote.audio
        def authorize():
            try:
                if service.hub.authenticate(token) != device: raise ValueError()
            except ValueError as error:
                raise credential_refused(error) from None
        audio._lease(device, lease_id, authorize)
        if not await audio.refresh_availability():
            raise ServiceError("audio_input_transport_unavailable", status=503)
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=4096, compress=False)
        await ws.prepare(request)
        audio_sockets.add(ws)
        channel_id = "audio-" + uuid.uuid4().hex
        generation = 0
        sequence = 0
        begun = False
        close_reason = "disconnected"
        begin_deadline = asyncio.get_running_loop().time() + 10
        async def send(value):
            await asyncio.wait_for(ws.send_json(value), 2)
        async def reject(reason):
            await send({"type": "rejected", "generation": generation, "sequence": sequence, "reason": reason})
        try:
            while not ws.closed:
                # Check expiry/authority while idle, not only when PCM arrives.
                audio._lease(device, lease_id, authorize)
                if begun:
                    current = audio.active
                    if current is None or current.uplink.channel_id != channel_id or current.ending:
                        raise AudioUplinkError("audio_input_not_active")
                elif asyncio.get_running_loop().time() >= begin_deadline:
                    raise AudioUplinkError("audio_input_begin_timeout", 408)
                try:
                    message = await ws.receive(timeout=.25)
                except asyncio.TimeoutError:
                    continue
                if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    break
                if message.type == WSMsgType.BINARY:
                    sequence += 1  # Wire receive ordinal, never capture sequence.
                    if not begun: raise AudioUplinkError("audio_input_begin_required", 400)
                    if len(message.data) != FORMAT["frame_bytes"]:
                        raise AudioUplinkError("audio_input_frame_invalid", 400)
                    result = await audio.accept(device, lease_id, generation, message.data, authorize, channel_id=channel_id)
                    if result["accepted"]:
                        await send({"type": "accepted", "generation": generation, "sequence": sequence})
                    else:
                        await reject("audio_input_backpressure")
                    continue
                if message.type != WSMsgType.TEXT:
                    raise AudioUplinkError("audio_input_message_invalid", 400)
                if len(message.data.encode()) > 1024:
                    raise AudioUplinkError("audio_input_message_invalid", 400)
                try:
                    value = json.loads(message.data, object_pairs_hook=unique_object,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                except (ValueError, TypeError):
                    raise AudioUplinkError("audio_input_message_invalid", 400) from None
                if not begun:
                    fields(value, ("generation", "format", "rate", "channels", "frame_samples"))
                    requested = value["generation"]
                    if type(requested) is not int or not 0 < requested < 2**53:
                        raise AudioUplinkError("audio_input_generation_invalid", 400)
                    generation = requested
                    for key in ("format", "rate", "channels", "frame_samples"):
                        if type(value[key]) is not type(FORMAT[key]) or value[key] != FORMAT[key]:
                            raise AudioUplinkError("audio_input_format_unsupported", 400)
                    uplink = AuthorizedAudioUplink(channel_id, generation)
                    audio.attach(device, lease_id, uplink, authorize)
                    await audio.begin(device, lease_id, generation, authorize, channel_id=channel_id)
                    begun = True
                    await send({"type": "begun", "generation": generation, **FORMAT})
                else:
                    fields(value, ("type", "generation"), ("reason",))
                    if value["type"] != "end" or type(value["generation"]) is not int or value["generation"] != generation:
                        raise AudioUplinkError("audio_input_generation_mismatch", 409)
                    reason = value.get("reason", "session_end")
                    if not isinstance(reason, str) or reason not in {"user_disabled", "route_changed", "session_end", "disconnected"}:
                        raise AudioUplinkError("audio_input_message_invalid", 400)
                    await audio.end(device, lease_id, generation, reason, authorize, channel_id=channel_id)
                    close_reason = reason
                    await send({"type": "ended", "generation": generation})
                    await ws.close(code=1000)
                    break
        except (AudioUplinkError, ServiceError) as error:
            close_reason = error.code
            if not ws.closed:
                with suppress(ConnectionError, asyncio.TimeoutError):
                    await reject(error.code)
                    await ws.close(code=1008)
        except (ConnectionError, asyncio.TimeoutError):
            close_reason = "disconnected"
        finally:
            # Own-handle cleanup after disconnect is valid even when the lease
            # expired. It cannot close a replacement socket's newer generation.
            try:
                await audio.close_channel(channel_id, close_reason)
            except AudioUplinkError:
                pass  # Manager retains owned cleanup state for its watchdog.
            audio_sockets.discard(ws)
            await ws.close()
        return ws

    async def vnc_socket(request):
        """WayVNC over the one TLS connection this device already pinned.

        The frames are RFB bytes and nothing else: a WebSocket BINARY message is
        a run of TCP bytes, with no framing, length prefix or envelope added in
        either direction. Authorization is the ordinary Bearer middleware plus
        the session owner check; the loopback port stays inside this process.
        """
        if request.query or request.can_read_body:
            raise ServiceError("invalid_request")
        device, token, session_id = request[DEVICE], request[TOKEN], request.match_info["id"]
        remote = service.remote
        def authorize():
            try:
                if service.hub.authenticate(token) != device: raise ValueError()
            except ValueError as error:
                raise credential_refused(error) from None
        port = remote.vnc_endpoint(device, session_id)
        channel = "vnc-" + uuid.uuid4().hex
        remote.claim_vnc_bridge(session_id, channel)  # 409 before the upgrade
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), 5)
        except (OSError, asyncio.TimeoutError):
            remote.release_vnc_bridge(session_id, channel)
            raise ServiceError("vnc_bridge_unavailable", "the VNC backend is not accepting connections", 503)
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1048576, compress=False)
        try:
            await ws.prepare(request)
        except ConnectionError:
            writer.close()
            remote.release_vnc_bridge(session_id, channel)
            raise
        vnc_sockets.add(ws)

        async def to_client():
            while True:
                data = await reader.read(65536)
                if not data:
                    return "backend_closed"
                await ws.send_bytes(data)

        async def to_host():
            async for message in ws:
                if message.type == WSMsgType.BINARY:
                    writer.write(message.data)
                    await writer.drain()
                elif message.type == WSMsgType.TEXT:
                    # RFB is a byte stream; a text frame is a client bug, never
                    # a control channel this bridge is willing to interpret.
                    return "text_frame_rejected"
                else:
                    return "client_closed"
            return "client_closed"

        async def guard():
            # A revoked credential, a released session or an owner change ends
            # the bridge without waiting for either side to notice.
            while True:
                await asyncio.sleep(1.0)
                authorize()
                remote.vnc_endpoint(device, session_id)

        tasks = [asyncio.create_task(job()) for job in (to_client, to_host, guard)]
        reason = "disconnected"
        try:
            finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in finished:
                if task.cancelled():
                    continue
                failure = task.exception()
                if failure is None:
                    reason = task.result() or reason
                elif isinstance(failure, (ServiceError, RemoteError)):
                    reason = failure.code
                elif isinstance(failure, (ConnectionError, OSError)):
                    reason = "backend_closed"
                else:
                    raise failure
                break
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Either end closing closes the other, always.
            writer.close()
            with suppress(ConnectionError, OSError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 2)
            vnc_sockets.discard(ws)
            remote.release_vnc_bridge(session_id, channel)
            if not ws.closed:
                await ws.close(code=1000 if reason in {"backend_closed", "client_closed"} else 1008,
                               message=reason.encode()[:120])
        return ws

    def _no_query(request):
        if request.query or request.can_read_body:
            raise ServiceError("invalid_request")

    async def _file_response(request, path, sha256, content_type, size):
        """One pinned representation per byte string: the ETag is its sha256.

        A client that already has the wallpaper or the font asks once with
        `If-None-Match` and gets 304, which is the whole point of publishing
        the digest in the JSON surface first. The body is sent from memory
        rather than by `FileResponse`, whose own mtime-derived validator would
        replace the digest the JSON surface promised.
        """
        etag = '"' + sha256 + '"'
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if etag in {value.strip() for value in (request.headers.get("If-None-Match") or "").split(",")}:
            return web.Response(status=304, headers=headers)
        if size > 33_554_432:
            raise ServiceError("asset_too_large", status=503)
        body = await asyncio.to_thread(Path(path).read_bytes)
        return web.Response(body=body, content_type=content_type, headers=headers)

    async def theme(request):
        _no_query(request)
        return web.json_response(await asyncio.to_thread(service.theme_snapshot))

    async def theme_background(request):
        _no_query(request)
        path, meta = await asyncio.to_thread(service.theme_background)
        return await _file_response(request, path, meta["sha256"], meta["content_type"], meta["bytes"])

    async def fonts(request):
        _no_query(request)
        return web.json_response(await asyncio.to_thread(service.fonts_snapshot))

    async def font_file(request):
        _no_query(request)
        row = await asyncio.to_thread(service.font_file, request.match_info["id"])
        return await _file_response(request, Path(row["path"]), row["sha256"], row["content_type"], row["bytes"])

    async def icon_file(request):
        """`GET /v1/icons/{name}?size=64` — the picture behind an XDG icon name.

        The only query this route takes is the size, and the ETag is the
        source file's sha256 (plus the raster size when this host turned an
        SVG into a PNG), so a client that already holds the bytes gets a 304
        exactly as it does for a font or the wallpaper. The `Cache-Control`
        set here is the same one the fonts and the wallpaper set, and the
        daemon's own response header replaces it with `no-store`: what makes
        the second request cheap is the validator, not an HTTP cache.
        """
        if request.can_read_body or set(request.query) - {"size"}:
            raise ServiceError("invalid_request")
        row = await asyncio.to_thread(service.icon_file, request.match_info["name"],
                                      request.query.get("size"))
        etag = '"' + row["sha256"] + '"'
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if etag in {value.strip() for value in (request.headers.get("If-None-Match") or "").split(",")}:
            return web.Response(status=304, headers=headers)
        return web.Response(body=row["bytes"], content_type=row["content_type"], headers=headers)

    async def herdr_layout(request):
        _no_query(request)
        return web.json_response(await asyncio.to_thread(service.herdr_layout, request[DEVICE]))

    async def herdr_sessions(request):
        """Every Herdr session on the host, and which one this device is on."""
        _no_query(request)
        return web.json_response(await asyncio.to_thread(service.herdr_sessions, request[DEVICE]))

    async def herdr_select_session(request):
        await body(request)
        result = await asyncio.to_thread(service.herdr_select_session, request[DEVICE],
                                         request.match_info["name"])
        return web.json_response(result)

    async def herdr_pane_action(request):
        payload = await body(request)
        result = await asyncio.to_thread(service.herdr_pane_action, request.match_info["pane"],
                                         request.match_info["action"], payload, request[DEVICE])
        return web.json_response(result)

    async def herdr_workspace_select(request):
        await body(request)
        result = await asyncio.to_thread(service.herdr_workspace_select, request.match_info["id"],
                                         request[DEVICE])
        return web.json_response(result)

    async def herdr_tabs(request):
        if request.method == "DELETE":
            if request.query or request.can_read_body:
                raise ServiceError("invalid_request")
            result = await asyncio.to_thread(service.herdr_tab_close, request.match_info["id"],
                                             request.match_info["tab"], request[DEVICE])
        else:
            payload = await body(request)
            fields(payload, (), ("label", "focus"))
            result = await asyncio.to_thread(service.herdr_tab_create, request.match_info["id"], payload,
                                             request[DEVICE])
        return web.json_response(result)

    async def herdr_stream(request):
        """`terminal session observe|control` on the WSS the device already has.

        Every NDJSON line Herdr writes becomes one WebSocket TEXT message,
        byte for byte: `terminal.frame`, `terminal.closed` and anything a later
        Herdr adds travel unchanged, because this bridge is a pipe, not a
        protocol. In the other direction only `control` listens, and only to
        Herdr's own four stdin commands.

        `observe` reads no stdin at all on 0.8.2, so a client resize there is
        served by restarting the stream at the new size; `control` takes
        `terminal.resize` in band and the same process keeps running.
        """
        mode = request.match_info["mode"]
        pane = request.match_info["pane"]
        device, token = request[DEVICE], request[TOKEN]
        if getattr(service, "herdr_bridge", None) is None:
            raise ServiceError("herdr_unavailable", status=503)
        # The stream belongs to the session this device selected, so the argv,
        # the one-controller claim and the socket are all that session's.
        bridge = service._herdr(device)
        allowed = {"cols", "rows"} | ({"takeover"} if mode == "control" else set())
        if set(request.query) - allowed or request.can_read_body:
            raise ServiceError("invalid_request")
        try:
            cols = int(request.query.get("cols", 80))
            rows = int(request.query.get("rows", 24))
        except ValueError:
            raise ServiceError("invalid_geometry") from None
        # `takeover=1` is what SPEC-G1 documents; `true` was the earlier
        # spelling and both mean the same single flag.
        takeover = request.query.get("takeover") in {"1", "true"}
        from .herdr_bridge import HerdrUnavailable, control_command
        def authorize():
            try:
                if service.hub.authenticate(token) != device: raise ValueError()
            except ValueError as error:
                raise credential_refused(error) from None
        try:
            argv = bridge.stream_argv(pane, mode, cols, rows, takeover=takeover)
        except HerdrUnavailable as error:
            raise service._herdr_error(error) from None
        channel = "herdr-" + uuid.uuid4().hex
        if mode == "control":
            try:
                bridge.claim_control(pane, channel)  # 409 before the upgrade
            except HerdrUnavailable as error:
                raise service._herdr_error(error) from None
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=262144, compress=False)
        try:
            await ws.prepare(request)
        except ConnectionError:
            if mode == "control":
                bridge.release_control(pane, channel)
            raise
        herdr_sockets.add(ws)
        state = {"argv": argv, "process": None, "restart": False}

        async def spawn():
            return await asyncio.create_subprocess_exec(
                *state["argv"], stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=1048576)

        async def stop():
            process = state["process"]
            if process is None or process.returncode is not None:
                return
            if process.stdin is not None and not process.stdin.is_closing():
                with suppress(ConnectionError, OSError):
                    process.stdin.close()
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), 2)

        async def to_client():
            while True:
                process = state["process"]
                try:
                    line = await process.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    return "frame_too_large"
                if not line:
                    if state["restart"]:
                        state["restart"] = False
                        await stop()
                        state["process"] = await spawn()
                        continue
                    return "stream_closed"
                await ws.send_str(line.decode("utf-8", "replace").rstrip("\n"))

        async def to_host():
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    return "binary_frame_rejected"
                try:
                    value = json.loads(message.data)
                except ValueError:
                    return "invalid_control_command"
                if mode == "observe":
                    # Read-only stream: the one thing a viewer may ask for is a
                    # different size, and Herdr answers that only on restart.
                    if not isinstance(value, dict) or value.get("type") != "resize":
                        return "invalid_control_command"
                    try:
                        new_argv = bridge.stream_argv(pane, mode, value.get("cols"), value.get("rows"))
                    except HerdrUnavailable:
                        return "invalid_geometry"
                    state["argv"], state["restart"] = new_argv, True
                    await stop()
                    continue
                try:
                    command = control_command(value)
                except HerdrUnavailable:
                    return "invalid_control_command"
                process = state["process"]
                if process.stdin is None or process.stdin.is_closing():
                    return "stream_closed"
                process.stdin.write((json.dumps(command, allow_nan=False) + "\n").encode())
                await process.stdin.drain()
            return "client_closed"

        async def guard():
            while True:
                await asyncio.sleep(1.0)
                authorize()

        reason = "disconnected"
        tasks: list[asyncio.Task] = []
        try:
            state["process"] = await spawn()
            tasks = [asyncio.create_task(job()) for job in (to_client, to_host, guard)]
            finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in finished:
                if task.cancelled():
                    continue
                failure = task.exception()
                if failure is None:
                    reason = task.result() or reason
                elif isinstance(failure, ServiceError):
                    reason = failure.code
                elif isinstance(failure, (ConnectionError, OSError)):
                    reason = "stream_closed"
                else:
                    raise failure
                break
        except OSError:
            reason = "herdr_unavailable"
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await stop()
            herdr_sockets.discard(ws)
            if mode == "control":
                bridge.release_control(pane, channel)
            if not ws.closed:
                await ws.close(code=1000 if reason in {"stream_closed", "client_closed"} else 1008,
                               message=reason.encode()[:120])
        return ws

    def voice_service():
        voice = getattr(service, "voice", None)
        if voice is None:
            raise ServiceError("voice_unavailable", status=503)
        return voice

    async def voice_capabilities(request):
        if request.query or request.can_read_body:
            raise ServiceError("invalid_request")
        return web.json_response(service.resource(await voice_service().capabilities(request[DEVICE])))

    async def voice_dictation(request):
        voice = voice_service()
        payload = await body(request)
        fields(payload, (), ("target",))
        if request.match_info["operation"] == "start":
            result = await voice.dictation_start(request[DEVICE], payload)
        else:
            result = await voice.dictation_stop(request[DEVICE], payload)
        return web.json_response(service.resource(result))

    async def voice_uplink(request):
        """The microphone on its own socket, with no Remote session involved.

        Same PCM contract as the Remote uplink because it is the same virtual
        microphone; what differs is that nothing here leases a screen. With
        `?levels=1` the socket also carries the waveform rows read from
        Voxtype's own fan-out socket.
        """
        if set(request.query) - {"levels"} or request.can_read_body:
            raise ServiceError("invalid_request")
        voice = voice_service()
        device, token = request[DEVICE], request[TOKEN]
        def authorize():
            try:
                if service.hub.authenticate(token) != device: raise ValueError()
            except ValueError as error:
                raise credential_refused(error) from None
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=4096, compress=False)
        await ws.prepare(request)
        audio_sockets.add(ws)
        channel_id = "voice-" + uuid.uuid4().hex
        generation, sequence, begun = 0, 0, False
        close_reason = "disconnected"
        levels_task = None
        begin_deadline = asyncio.get_running_loop().time() + 10
        async def send(value):
            await asyncio.wait_for(ws.send_json(value), 2)
        async def pump_levels():
            # A blocking reader on its own thread; one queue hop per row.
            queue = asyncio.Queue(maxsize=8)
            loop = asyncio.get_running_loop()
            def reader():
                try:
                    for row in voice.level_frames():
                        loop.call_soon_threadsafe(lambda value=row: queue.full() or queue.put_nowait(value))
                except VoiceError:
                    pass
            worker = asyncio.create_task(asyncio.to_thread(reader))
            try:
                while True:
                    await send({"type": "voice.level", **await queue.get()})
            finally:
                worker.cancel()
        try:
            if request.query.get("levels") == "1":
                levels_task = asyncio.create_task(pump_levels())
            while not ws.closed:
                authorize()
                if not begun and asyncio.get_running_loop().time() >= begin_deadline:
                    raise AudioUplinkError("audio_input_begin_timeout", 408)
                try:
                    message = await ws.receive(timeout=.25)
                except asyncio.TimeoutError:
                    continue
                if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    break
                if message.type == WSMsgType.BINARY:
                    sequence += 1
                    if not begun: raise AudioUplinkError("audio_input_begin_required", 400)
                    result = voice.accept(device, channel_id, generation, message.data)
                    await send({"type": "accepted" if result["accepted"] else "rejected",
                                "generation": generation, "sequence": sequence,
                                **({} if result["accepted"] else {"reason": "audio_input_backpressure"})})
                    continue
                if message.type != WSMsgType.TEXT or len(message.data.encode()) > 1024:
                    raise AudioUplinkError("audio_input_message_invalid", 400)
                try:
                    value = json.loads(message.data, object_pairs_hook=unique_object,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                except (ValueError, TypeError):
                    raise AudioUplinkError("audio_input_message_invalid", 400) from None
                if not begun:
                    fields(value, ("generation", "format", "rate", "channels", "frame_samples"))
                    for key in ("format", "rate", "channels", "frame_samples"):
                        if type(value[key]) is not type(FORMAT[key]) or value[key] != FORMAT[key]:
                            raise AudioUplinkError("audio_input_format_unsupported", 400)
                    generation = value["generation"]
                    started = await voice.begin(device, channel_id, generation)
                    begun = True
                    await send({"type": "begun", **started})
                else:
                    fields(value, ("type", "generation"), ("reason",))
                    if value["type"] != "end" or value["generation"] != generation:
                        raise AudioUplinkError("audio_input_generation_mismatch", 409)
                    reason = value.get("reason", "session_end")
                    if reason not in {"user_disabled", "route_changed", "session_end", "disconnected"}:
                        raise AudioUplinkError("audio_input_message_invalid", 400)
                    await voice.end(channel_id, reason)
                    close_reason = reason
                    await send({"type": "ended", "generation": generation})
                    await ws.close(code=1000)
                    break
        except (AudioUplinkError, VoiceError, ServiceError) as error:
            close_reason = error.code
            if not ws.closed:
                with suppress(ConnectionError, asyncio.TimeoutError):
                    await send({"type": "rejected", "generation": generation,
                                "sequence": sequence, "reason": error.code})
                    await ws.close(code=1008)
        except (ConnectionError, asyncio.TimeoutError):
            close_reason = "disconnected"
        finally:
            if levels_task is not None:
                levels_task.cancel()
                with suppress(asyncio.CancelledError):
                    await levels_task
            with suppress(AudioUplinkError):
                await voice.end(channel_id, close_reason)
            audio_sockets.discard(ws)
            await ws.close()
        return ws

    def notification_service():
        notifications = getattr(service, "notifications", None)
        if notifications is None:
            raise ServiceError("notifications_unavailable", status=503)
        return notifications

    async def notifications_read(request):
        if set(request.query) - {"since", "limit"} or request.can_read_body:
            raise ServiceError("invalid_request")
        return web.json_response(service.resource(await notification_service().history(
            since=request.query.get("since"), limit=request.query.get("limit"))))

    async def notification_action(request):
        notifications = notification_service()
        fields(await body(request))
        result = await notifications.act(request.match_info["id"], request.match_info["action"])
        return web.json_response(service.resource(result))

    async def notifications_dnd(request):
        notifications = notification_service()
        if request.method == "GET":
            if request.query or request.can_read_body:
                raise ServiceError("invalid_request")
            return web.json_response(service.resource(await notifications.dnd()))
        payload = await body(request)
        fields(payload, (), ("enabled",))
        return web.json_response(service.resource(await notifications.set_dnd(payload.get("enabled"))))

    async def audio_capabilities(request):
        if request.query or request.can_read_body:
            raise ServiceError("invalid_request")
        result = await service.remote.audio_capabilities(request[DEVICE])
        return web.json_response(service.resource(result))

    def broker():
        value = getattr(service, "biometric", None)
        if value is None:
            raise ServiceError("biometric_unavailable", status=503)
        return value

    async def auth_keys(request):
        """The device's own approval key: read the challenge, enrol, revoke.

        AUTH-1 §2 allows enrolment either in the pairing claim or later over an
        authenticated channel; this is the later one, so a device that paired
        before this existed can enrol without pairing again. The proof of
        possession is a signature over a challenge minted here, which is what
        stops a device from registering a public key it does not hold.
        """
        keys = broker()
        device = request[DEVICE]
        if request.method == "GET":
            if set(request.query):
                raise ServiceError("invalid_request")
            enrolled = keys.keys.list()
            return web.json_response(service.resource({
                "enabled": keys.enabled(), "host_id": keys.host_id, "host_name": keys.host_name,
                "device_id": device, "challenge": keys.challenge(device),
                "enrolled": any(row["device_id"] == device for row in enrolled),
                "keys": enrolled}))
        if request.method == "DELETE":
            return web.json_response(service.resource(keys.revoke(device)))
        return web.json_response(service.resource(keys.enroll(device, await body(request))))

    async def ssh_key(request):
        """UX-4 §2. `PUT /v1/ssh/key` - this device states the key it offers.

        The device is the one on the credential, never a field in the body: a
        credential can replace its own `authorized_keys` line and nothing else.
        """
        if request.method == "GET":
            if set(request.query):
                raise ServiceError("invalid_request")
            return web.json_response(service.ssh_key_state(request[DEVICE]))
        return web.json_response(service.ssh_key(request[DEVICE], await body(request)))

    async def auth_key_switch(request):
        """The device's own half of the two switches, without re-enrolling.

        Turning the App's toggle off unregisters the key outright; this exists
        for the states in between - a device that registered while its own
        switch was off, and a device turning itself back on.
        """
        return web.json_response(service.resource(
            broker().set_enabled(request[DEVICE], await body(request))))

    async def auth_approvals(request):
        keys = broker()
        device = request[DEVICE]
        if request.method == "GET":
            if set(request.query):
                raise ServiceError("invalid_request")
            return web.json_response(service.resource(keys.pending_for(device)))
        return web.json_response(service.resource(
            keys.resolve(request.match_info["id"], device, await body(request))))

    async def preferences(request):
        if request.query or request.can_read_body:
            raise ServiceError("invalid_request")
        result = await service._media_worker(service.preferences_snapshot)
        try:
            if service.hub.authenticate(request[TOKEN]) != request[DEVICE]:
                raise ValueError()
        except ValueError as error:
            raise credential_refused(error) from None
        return web.json_response(service.resource(result))

    async def clipboard(request):
        """CLIP-1. The host's clipboard, as text, both ways.

        It is deliberately not a JSON resource. What travels is the clipboard
        itself, so the body is the text and nothing else: no envelope to parse
        before the paste, and nothing tempting a client to keep the answer in
        a structure it logs. The write answers with a count, never with the
        text it was just given.
        """
        if request.query:
            raise ServiceError("invalid_request")
        if request.method == "GET":
            if request.can_read_body:
                raise ServiceError("invalid_request")
            text = await service._media_worker(service.clipboard_read)
            return web.Response(text=text, content_type="text/plain", charset="utf-8")
        kind = (request.headers.get("Content-Type") or "text/plain").split(";")[0].strip().lower()
        if kind != "text/plain":
            raise ClipboardError("clipboard_not_text", 415)
        length = request.content_length
        if length is not None and length > CLIPBOARD_LIMIT:
            raise ClipboardError("clipboard_too_large", 413)
        try:
            raw = await asyncio.wait_for(request.content.read(CLIPBOARD_LIMIT + 1), 5.0)
        except asyncio.TimeoutError:
            raise ServiceError("request_timeout", "request body timed out", 408) from None
        if len(raw) > CLIPBOARD_LIMIT:
            raise ClipboardError("clipboard_too_large", 413)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ClipboardError("clipboard_not_text", 415) from None
        # The same recheck `body()` does: a credential revoked between the
        # middleware and here must not get to write the host's clipboard.
        try:
            if service.hub.authenticate(request[TOKEN]) != request[DEVICE]:
                raise ValueError
        except ValueError as error:
            raise credential_refused(error, "credential revoked or expired") from None
        result = await service._media_worker(service.clipboard_write, text)
        return web.json_response(service.resource(result))

    async def remote_capabilities(request):
        if request.query or request.can_read_body:
            raise ServiceError("invalid_request")
        return web.json_response(await service.dispatch_remote_async("remote.capabilities", {}, request[DEVICE]))

    async def shortcuts(request):
        return web.json_response(service.shortcuts_snapshot())

    async def control_wake(request):
        result=await service.wake_control(request[DEVICE],await body(request))
        return web.json_response(result)

    async def workspace_select_relative(request):
        """`e+1` / `e-1`: the host names the neighbour, not the client."""
        payload=await body(request);fields(payload)
        result=service.dispatch("workspace.select",{"relative":request.match_info["step"]},request[DEVICE])
        return web.json_response(result)

    async def workspace_select(request):
        payload=await body(request);fields(payload)
        result=service.dispatch("workspace.select",{"workspace_id":int(request.match_info["id"])},request[DEVICE])
        return web.json_response(result)

    async def remote_session(request):
        """Create, read, change or release the one Remote session."""
        operation = request.match_info.get("operation")
        method = request.method
        payload = {} if method in {"GET", "DELETE"} else await body(request)
        if "session_id" in payload:
            raise ServiceError("invalid_request")
        if method == "POST" and operation is None and "id" not in request.match_info:
            op = "remote.start"
        elif method == "GET":
            op = "remote.get"
        elif method == "DELETE":
            op = "remote.stop"
        else:
            op = "remote." + operation
        if "id" in request.match_info:
            payload["session_id"] = request.match_info["id"]
        return web.json_response(await service.dispatch_remote_async(op, payload, request[DEVICE]),
                                 status=201 if op == "remote.start" else 200)

    sockets: set[web.WebSocketResponse] = set()
    audio_sockets: set[web.WebSocketResponse] = set()
    vnc_sockets: set[web.WebSocketResponse] = set()
    herdr_sockets: set[web.WebSocketResponse] = set()

    async def events(request):
        if set(request.query) - {"since", "instance_id"}:
            raise ServiceError("invalid_request", "event query may contain only since and instance_id")
        try:
            since = int(request.query.get("since", "0"))
            if since < 0:
                raise ValueError
        except ValueError:
            raise ServiceError("invalid_request", "invalid event cursor")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1024, compress=False)
        await ws.prepare(request)
        sockets.add(ws)
        state = service.state(request[DEVICE])
        same_instance = request.query.get("instance_id") == service.instance_id
        if not same_instance or "since" not in request.query:
            since = state["event_cursor"]
            await ws.send_json({"type": "snapshot", "state": state, "instance_id": service.instance_id, "cursor": since})
        else:
            await ws.send_json({"type": "ready", "instance_id": service.instance_id, "cursor": since})

        async def pump():
            after_cursor = since
            async for event in service.hub.subscribe(since=since, device_id=request[DEVICE], token=request[TOKEN]):
                await ws.send_json({"event": asdict(event), "instance_id": service.instance_id,
                                    "after_cursor": after_cursor})
                after_cursor = event.seq

        async def check_auth():
            while not ws.closed:
                await asyncio.sleep(auth_check_interval)
                try:
                    service.hub.authenticate(request[TOKEN])
                except ValueError as error:
                    # CORE-2: the close frame names the refusal the same way a
                    # 401 body does, after the words older clients match on.
                    reason = getattr(error, "reason", None) or "unknown_credential"
                    await ws.close(code=1008, message=f"credential revoked or expired: {reason}".encode())
                    return

        async def receive():
            async for message in ws:
                # This is a server event stream, never a proxy for client code.
                if message.type in (WSMsgType.TEXT, WSMsgType.BINARY):
                    await ws.close(code=1008, message=b"read-only event stream")
                    return

        tasks = [asyncio.create_task(task()) for task in (pump, check_auth, receive)]
        try:
            finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if tasks[0] in finished and not tasks[0].cancelled():
                failure = tasks[0].exception()
                if isinstance(failure, ValueError):
                    reason = getattr(failure, "reason", None) or "unknown_credential"
                    await ws.close(code=1008, message=f"credential revoked or expired: {reason}".encode())
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            sockets.discard(ws)
            await ws.close()
        return ws

    async def ticker(app):
        async def tick():
            while True:
                await asyncio.sleep(0.25)
                service.hub.tick()
        task = asyncio.create_task(tick())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        for socket in tuple(sockets):
            await socket.close(code=1001, message=b"daemon shutting down")

    async def shutdown(app):
        # CLIP-1: the clipboard watcher is a child process, not a task, so a
        # clean shutdown has to end it here. A kill still leaves it to the
        # kernel (`setpriv --pdeathsig`), which is the case the host walk found.
        clipboard = getattr(service, "clipboard", None)
        if clipboard is not None:
            await asyncio.to_thread(clipboard.stop)
        await asyncio.gather(*(socket.close(code=1001, message=b"daemon shutting down")
                              for socket in tuple(sockets | audio_sockets | vnc_sockets | herdr_sockets)))

    app.on_shutdown.append(shutdown)
    app.cleanup_ctx.append(ticker)
    async def remote_lifecycle(app):
        await service.remote.attach_transport()
        yield
        await service.remote.detach_transport()
    app.cleanup_ctx.append(remote_lifecycle)
    async def voice_lifecycle(app):
        mirror = getattr(service, "notification_mirror", None)
        if mirror is not None:
            await asyncio.to_thread(mirror.start)
        yield
        if mirror is not None:
            await asyncio.to_thread(mirror.stop)
        voice = getattr(service, "voice", None)
        if voice is not None:
            # A dictation session must not outlive the daemon with Voxtype
            # still pointed at a source that is about to disappear.
            with suppress(Exception):
                await voice.close()
    app.cleanup_ctx.append(voice_lifecycle)
    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    app.router.add_get("/v1/events", events)
    app.router.add_post("/v1/pairing/requests", pairing_request)
    app.router.add_post("/v1/pairing/requests/{id}/claim", pairing_request)
    app.router.add_get("/v1/pairing/credential", pairing_credential, allow_head=False)
    app.router.add_post("/v1/pairing/renew", pairing_renew)
    app.router.add_post("/v1/actions/{id}:invoke", action)
    app.router.add_post("/v1/control/wake", control_wake)
    app.router.add_get("/v1/shortcuts", shortcuts)
    app.router.add_post("/v1/workspaces/{id:-?[0-9]+}/select", workspace_select)
    app.router.add_post("/v1/workspaces/relative/{step:e[+-]1}/select", workspace_select_relative)
    app.router.add_post("/v1/agent/tasks", agent_task)
    app.router.add_post("/v1/agent/default:ensure", agent_ensure)
    app.router.add_get("/v1/agent/default/chat/commands",chat_commands)
    app.router.add_post("/v1/agent/default/chat/commands/{command}:execute",chat_command_execute)
    app.router.add_post("/v1/agent/default/chat/recover-empty",chat_recover_empty)
    app.router.add_post("/v1/agent/default/chat/handoff:prepare",chat_handoff_prepare)
    app.router.add_post("/v1/agent/default/chat/handoff:confirm",chat_handoff_confirm)
    app.router.add_get("/v1/agent/default/chat",chat_snapshot)
    app.router.add_get("/v1/agent/default/chat/events",chat_events)
    app.router.add_post("/v1/agent/default/chat/messages",chat_send)
    app.router.add_post("/v1/agent/default/chat/interrupt",chat_interrupt)
    app.router.add_post("/v1/agent/default/chat/steer",chat_steer)
    app.router.add_get("/v1/agent/default/chat/approvals",chat_approvals,allow_head=False)
    app.router.add_post("/v1/agent/default/chat/approvals/{request_id}",chat_approval_resolve)
    app.router.add_get("/v1/agent/default/chat/usage",chat_usage,allow_head=False)
    app.router.add_get("/v1/agent/default/models",chat_models,allow_head=False)
    app.router.add_post("/v1/media/pairing/discover", media_pairing)
    app.router.add_post("/v1/media/pairing/requests", media_pairing)
    app.router.add_get("/v1/media/pairing/requests/{attempt_id}", media_pairing, allow_head=False)
    app.router.add_delete("/v1/media/pairing/requests/{attempt_id}", media_pairing)
    app.router.add_get("/v1/audio/input", audio_capabilities, allow_head=False)
    app.router.add_get("/v1/voice/capabilities", voice_capabilities, allow_head=False)
    app.router.add_get("/v1/voice/uplink", voice_uplink, allow_head=False)
    app.router.add_post("/v1/voice/dictation:{operation:start|stop}", voice_dictation)
    app.router.add_get("/v1/notifications", notifications_read, allow_head=False)
    app.router.add_get("/v1/notifications/dnd", notifications_dnd, allow_head=False)
    app.router.add_post("/v1/notifications/dnd", notifications_dnd)
    app.router.add_post(r"/v1/notifications/{id:[0-9]{1,16}-[0-9]{1,12}}:{action:invoke|dismiss}", notification_action)
    app.router.add_get("/v1/remote/sessions/{id}/audio", audio_socket, allow_head=False)
    app.router.add_get("/v1/remote/sessions/{id}/vnc", vnc_socket, allow_head=False)
    app.router.add_get("/v1/herdr/layout", herdr_layout, allow_head=False)
    app.router.add_get("/v1/herdr/sessions", herdr_sessions, allow_head=False)
    app.router.add_post("/v1/herdr/sessions/{name:[A-Za-z0-9][A-Za-z0-9_.-]{0,63}}/select", herdr_select_session)
    app.router.add_get("/v1/herdr/panes/{pane}/{mode:observe|control}", herdr_stream, allow_head=False)
    app.router.add_post("/v1/herdr/panes/{pane}/{action:split|zoom|focus|close}", herdr_pane_action)
    app.router.add_post("/v1/herdr/workspaces/{id}/select", herdr_workspace_select)
    app.router.add_post("/v1/herdr/workspaces/{id}/tabs", herdr_tabs)
    app.router.add_delete("/v1/herdr/workspaces/{id}/tabs/{tab}", herdr_tabs)
    app.router.add_get("/v1/theme", theme, allow_head=False)
    app.router.add_get("/v1/theme/background", theme_background, allow_head=False)
    app.router.add_get("/v1/fonts", fonts, allow_head=False)
    app.router.add_get("/v1/fonts/{id}", font_file, allow_head=False)
    app.router.add_get(r"/v1/icons/{name:[^/]{1,1024}}", icon_file, allow_head=False)
    app.router.add_get("/v1/auth/keys", auth_keys, allow_head=False)
    app.router.add_post("/v1/auth/keys", auth_keys)
    app.router.add_delete("/v1/auth/keys", auth_keys)
    app.router.add_post("/v1/auth/keys/enabled", auth_key_switch)
    app.router.add_get("/v1/ssh/key", ssh_key, allow_head=False)
    app.router.add_put("/v1/ssh/key", ssh_key)
    app.router.add_get("/v1/auth/approvals", auth_approvals, allow_head=False)
    app.router.add_post("/v1/auth/approvals/{id:appr_[0-9a-f]{32}}", auth_approvals)
    app.router.add_get("/v1/preferences", preferences, allow_head=False)
    app.router.add_get("/v1/clipboard", clipboard, allow_head=False)
    app.router.add_put("/v1/clipboard", clipboard)
    app.router.add_get("/v1/remote/capabilities", remote_capabilities, allow_head=False)
    app.router.add_post("/v1/remote/sessions", remote_session)
    app.router.add_get("/v1/remote/sessions/{id}", remote_session, allow_head=False)
    app.router.add_delete("/v1/remote/sessions/{id}", remote_session)
    app.router.add_post("/v1/remote/sessions/{id}/{operation:resize|backend|heartbeat|presented}", remote_session)
    app.router.add_get("/v1/{name}", read)
    return app


class NetworkServer:
    def __init__(self, service: CoreService, *, host="127.0.0.1", port=0,
                 certificate: str | None = None, private_key: str | None = None,
                 allow_loopback_http=False, discovery=None, discovery_config=None, discovery_publisher_factory=None):
        self.service, self.host, self.port = service, host, port
        if discovery is not None and discovery_config is not None:
            raise ValueError("discovery and discovery_config are mutually exclusive")
        if discovery_config is not None:
            if not isinstance(discovery_config, DiscoveryConfig): raise ValueError("discovery_config invalid")
            kwargs={} if discovery_publisher_factory is None else {"publisher_factory":discovery_publisher_factory}
            discovery=NetworkDiscoveryBinding(self, ListenerDiscovery(discovery_config, **kwargs))
        self.discovery = discovery
        self.certificate, self.private_key = certificate, private_key
        self.allow_loopback_http = allow_loopback_http
        self.runner = self.site = None
        self.bound_port = None

    async def start(self):
        context = None
        if self.certificate and self.private_key:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(self.certificate, self.private_key)
        elif not (self.allow_loopback_http and ipaddress.ip_address(self.host).is_loopback):
            raise ValueError("HTTPS requires --tls-cert and --tls-key; HTTP is restricted to explicit loopback development")
        self.runner = web.AppRunner(create_app(self.service), access_log=None, shutdown_timeout=2.0)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port, ssl_context=context)
        try:
            await self.site.start()
        except Exception:
            await self.runner.cleanup()
            raise
        self.bound_port = self.site._server.sockets[0].getsockname()[1]
        if self.discovery is not None:
            try:
                await self.discovery.start_after_listener()
            except Exception:
                # Discovery is an optional public hint; an unavailable optional
                # publisher cannot turn a healthy HTTPS listener into failure.
                self.discovery.last_state = {"status":"unavailable","reason":"discovery_integration_failed","advertised":False}
        return self.bound_port

    async def close(self):
        if self.discovery is not None and self.runner is not None:
            try: await self.discovery.stop_before_listener()
            except Exception: pass
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        if self.discovery is not None:
            try: await self.discovery.close()
            except Exception: pass
