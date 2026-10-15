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
from .auth import CredentialStoreUnavailable
from .audio_uplink import AudioUplinkError, AuthorizedAudioUplink, FORMAT
from .voice import VoiceError
from .notifications import NotificationsUnavailable
from .lan_discovery import DiscoveryConfig, ListenerDiscovery
from .network_discovery import NetworkDiscoveryBinding
from .remote.errors import RemoteError
from . import tailnet as tailnet_module
from . import browser_gate
from .odomo import OdomoBundle, IMMUTABLE, REVALIDATE, security_headers
from .push import PushError
from .uploads import REFUSALS, journal as journal_upload

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


# HERDR-11 §4. A JSON read that compresses to a fraction of itself (the state
# with its catalog was ~590 KB per event) and answers `304` to a client that
# already holds exactly these bytes. Only a body this large is compressed: a
# small reply costs more to deflate than it saves.
COMPRESS_FROM = 1024
CATALOG_HEADER = "X-Omodachi-Catalog"


def matches_etag(request, etag: str) -> bool:
    sent = request.headers.get("If-None-Match") or ""
    return etag in {value.strip() for value in sent.split(",")} or sent.strip() == "*"


def conditional_json(request, value, *, etag: str | None = None):
    """`value` as JSON with an `ETag` (a hash of the body unless one is given),
    `304` when the client's `If-None-Match` names it, and gzip when the client
    takes it and the body is worth it."""
    import hashlib
    body = json.dumps(value).encode()
    tag = etag or '"' + hashlib.sha256(body).hexdigest()[:24] + '"'
    if matches_etag(request, tag):
        return web.Response(status=304, headers={"ETag": tag})
    response = web.Response(body=body, content_type="application/json", headers={"ETag": tag})
    if len(body) >= COMPRESS_FROM and "gzip" in (request.headers.get("Accept-Encoding") or "").lower():
        response.enable_compression(web.ContentCoding.gzip)
    return response


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


def tailnet_tools(service):
    """TAIL-2: the `tailnet` readings, replaceable by a test on the service."""
    return getattr(service, "tailnet_tools", None) or tailnet_module


def local_address(request):
    """The host address this request arrived on - the road the client is using."""
    try:
        value = request.transport.get_extra_info("sockname") if request.transport else None
        return str(ipaddress.ip_address(value[0].split("%", 1)[0])) if value else None
    except (TypeError, ValueError, IndexError, AttributeError):
        return None


def on_this_road(document, request, tailnet=None):
    """TAIL-2 P2-2: a Sunshine connection names the road this request used.

    The backend answers with the address the host would use to reach its LAN,
    which is unreachable for a client on the tailnet. Moonlight dials exactly
    what it is given, so the connection document says how the client is
    already reaching this host: its LAN address at home, and on the tailnet
    the host's MagicDNS name. A loopback request (tests, the simulator
    backend) keeps the backend's own answer.

    REMOTE-FIX-2: on the tailnet it is the name, never the 100.x / fd7a:
    literal. TAIL-3b's rule is that a Tailscale host *is* its MagicDNS name,
    and the iOS app's Moonlight requests are NSURLSession loads under App
    Transport Security, which treats a tailnet literal as neither local
    networking nor the `*.ts.net` exception: it demands system trust for the
    fork's self-signed certificate and fails every request with -1200
    (-9802), whatever the app's pinning answers. Leo's iPad hit exactly that
    on 2026-10-04. `tailnet` is this node's cached Tailscale reading
    (`tailnet.reading`); only when it names no MagicDNS name does the literal
    go out, and the app says in its diagnostics why that one cannot work.
    """
    session = document.get("session") if isinstance(document, dict) else None
    connection = session.get("connection") if isinstance(session, dict) else None
    if not isinstance(connection, dict) or connection.get("backend") != "sunshine":
        return document
    address = local_address(request)
    if address is None or ipaddress.ip_address(address).is_loopback or ipaddress.ip_address(address).is_unspecified:
        return document
    host = address
    if tailnet_module.is_tailnet_address(address):
        host = tailnet_name(tailnet) or address
    return {**document, "session": {**session, "connection": {**connection, "host": host}}}


def tailnet_name(reading):
    """This node's MagicDNS name from a Tailscale reading, while it is up."""
    if not isinstance(reading, dict) or reading.get("running") is not True:
        return None
    return tailnet_module._dns_name(reading.get("dns_name"))


def apply_road(capabilities, kind, relayed):
    """`remote/capabilities` for one requesting peer (TAIL-2 P2-2)."""
    road = {"kind": kind, "relayed": relayed, "reason": None}
    value = {**capabilities, "road": road}
    vnc = (capabilities.get("backends") or {}).get("vnc") or {}
    if relayed is True and vnc.get("available") is True:
        value["default_backend"] = "vnc"
        road["reason"] = "tailnet_relayed"
    return value


def web_journal(entry):
    """ODOMO-2: one JSON line on the daemon's stdout (the journal), tagged
    `"omodachi":"web"`, the shape `service._ssh_journal` uses."""
    try:
        print(json.dumps({"omodachi": "web", **entry}, separators=(",", ":"), default=str), flush=True)
    except (OSError, ValueError):
        pass


# ATTACH-1: how often the uploads directory is swept (and once at start).
SWEEP_INTERVAL = 3600.0

# LITE-1a L4: seconds between `{"type": "heartbeat"}` on an opted-in event stream.
HEARTBEAT = 15.0


def _is_odomo(path):
    return path == "/odomo" or path.startswith("/odomo/")


# LITE-1c: the web app was called Lite and lived at /lite/. A tab still open
# there, or a bookmark, is sent to the same place under /odomo/ (GET and HEAD
# only, the query kept). An installed Home Screen app or a push subscription
# from then belongs to the old scope and has to be added again.
LEGACY_PREFIX = "/lite"


def _is_legacy(path):
    return path == LEGACY_PREFIX or path.startswith(LEGACY_PREFIX + "/")


# LITE-1b (NEXT-1 §6.4): Safari asks the origin's root for these when a page
# is added to the Home Screen, whatever the manifest says. They are Odomo's own
# icons, unauthenticated like /odomo/*, instead of a 401 in the log.
_ROOT_ICON = re.compile(r"/(favicon\.ico|apple-touch-icon(-[0-9]{2,3}x[0-9]{2,3})?(-precomposed)?\.png)\Z")


def _is_root_icon(path):
    return bool(_ROOT_ICON.match(path))


def create_app(service: CoreService, *, auth_check_interval=1.0, odomo_bundle=None) -> web.Application:
    # LITE-1a. The browser gates (`browser_gate`): which names are this host,
    # the single-use WebSocket tickets, and the built Odomo app itself.
    host_names = browser_gate.HostNames(service)
    tickets = browser_gate.WsTickets()
    if odomo_bundle is None:
        odomo_bundle = getattr(service, "odomo_bundle", None)
    odomo = odomo_bundle if odomo_bundle is not None else OdomoBundle()
    # NOTIFY-1: streams that put a pane or the conversation on a screen (`seen.py`).
    seen_watch = getattr(service, "seen_watch", None) or (lambda key, delta: None)

    @web.middleware
    async def boundary(request, handler):
        response = await guarded(request, handler)
        # ODOMO-2: what a web device makes the agent do is on the journal with
        # the device that did it - which route and how it ended, never the
        # words. (Its writes into Herdr panes are in `herdr-writes.jsonl`.)
        device = request.get(DEVICE)
        if (device and request.method == "POST" and request.path.startswith("/v1/agent/")
                and device_role(device) == "web"):
            web_journal({"event": "agent.write", "device": device, "route": request.path[:200],
                         "status": response.status})
        return response

    async def guarded(request, handler):
        try:
            # LITE-1a C2, before anything authenticates: a browser has to name
            # this host, and say it is a page on this host. Native clients send
            # no Origin and are not asked.
            origin = request.headers.get("Origin")
            odomo_path = _is_odomo(request.path) or _is_root_icon(request.path) or _is_legacy(request.path)
            if odomo_path or origin is not None:
                if not await host_names.allows(request.headers.get("Host"), browser_gate.local_port(request)):
                    raise ServiceError("host_not_allowed", "this host does not answer to that name", 403)
                if origin is not None and not browser_gate.origin_matches(request, origin):
                    raise ServiceError("origin_not_allowed", "requests from another origin are refused", 403)
            pairing_path = request.method == "POST" and (request.path == "/v1/pairing/requests" or
                bool(re.fullmatch(r"/v1/pairing/requests/pair_[0-9a-f]{32}/claim", request.path)))
            open_path = request.path in {"/", "/health"} or pairing_path or (
                odomo_path and request.method in {"GET", "HEAD"})
            if not open_path:
                authorization = request.headers.get("Authorization")
                if (authorization is None and request.method == "GET" and "ticket" in request.query
                        and browser_gate.ticket_path(request.path)):
                    # LITE-1a C5: a browser's WebSocket. The ticket is spent
                    # here, whatever happens next, and leaves the query before
                    # the route's own strict query check sees it.
                    try:
                        minted_for, token = tickets.take(request.query.get("ticket"), request.path)
                    except browser_gate.TicketRefused:
                        raise ServiceError("ws_ticket_invalid", "unknown, spent or expired ticket", 401) from None
                    kept = [(key, value) for key, value in request.rel_url.query.items() if key != "ticket"]
                    request = request.clone(rel_url=request.rel_url.with_query(kept))
                else:
                    minted_for = None
                    authorization = authorization or ""
                    if not authorization.startswith("Bearer ") or len(authorization) > 4096:
                        raise ServiceError("pairing_required", status=401)
                    token = authorization[7:]
                try:
                    device = service.hub.authenticate(token)
                except CredentialStoreUnavailable:
                    raise ServiceError("auth_unavailable", "this host could not check credentials just now", 503) from None
                except ValueError as error:
                    raise credential_refused(error)
                if minted_for is not None and device != minted_for:
                    raise credential_refused()
                request[DEVICE] = device
                request[TOKEN] = token
                # LITE-1a C4: a web credential, and anything a browser sends,
                # reaches only the routes `WEB_ALLOWED` lists.
                if (origin is not None or device_role(device) == "web") and \
                        not browser_gate.web_allows(request.method, request.path):
                    raise ServiceError("web_client_forbidden", "not available to the web app", 403)
            response = await handler(request)
            if odomo_path and response.status in (200, 304) and "Cache-Control" in response.headers:
                pass  # the Odomo handler chose immutable or no-cache itself
            else:
                response.headers["Cache-Control"] = "no-store"
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            return response
        except CredentialStoreUnavailable:
            # `credential_info`/`renew` reach the registry again on their own.
            return error_response("auth_unavailable", "this host could not check credentials just now", 503)
        except (ServiceError, AudioUplinkError, VoiceError, NotificationsUnavailable, RemoteError,
                ClipboardError, PushError) as exc:
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

    def device_role(device):
        role = getattr(service.hub.auth, "device_role", None) if hasattr(service.hub, "auth") else None
        try:
            return role(device) if callable(role) else "companion"
        except (ValueError, OSError):
            # An unreadable registry already failed `authenticate`; this is
            # the narrow race after it, and it fails closed.
            return "web"

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

    def still_authorized(request):
        """SEC-RENEW-1: `body()`'s recheck, as a callable a store runs under its own lock.

        A grant that outlives a revoke - an `authorized_keys` line, a push
        subscription - is checked where it is written, inside the lock the
        revoke's cleanup of that store also takes. `body()` alone is one
        thread hop too early.
        """
        device, token = request[DEVICE], request[TOKEN]

        def authorize():
            try:
                if service.hub.authenticate(token) != device:
                    raise ValueError
            except CredentialStoreUnavailable:
                raise  # 503, not 401: the registry, not the credential (REMOTE-PAIR-1)
            except ValueError as error:
                raise credential_refused(error, "credential revoked or expired") from None
        return authorize

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
            # LITE-1a C3: behind `tailscale serve` that address is 127.0.0.1
            # for everyone, so for a loopback peer only it is the forwarded
            # client instead. C4: a request a browser sent is a web client,
            # whatever it says it is.
            remote_addr, served = browser_gate.forwarded_peer(request)
            # TAIL-2 P1-6: over the tailnet, this computer's own tailscaled
            # says who the peer is, so the card never has to show a 100.x.
            # One `via` for both clients (NEXT-1): the road of the real peer -
            # the forwarded one behind serve - and whois on it; serve's
            # `Tailscale-User-Login` only fills a login whois could not give.
            kind = tailnet_module.address_kind(remote_addr)
            via = {"kind": kind, "peer_name": None, "login": None}
            if kind == "tailnet":
                with suppress(Exception):
                    via = await asyncio.wait_for(asyncio.to_thread(
                        tailnet_tools(service).whois, remote_addr), 4.0)
            if served is not None and isinstance(via, dict) and not via.get("login"):
                via = {**via, "login": served.get("login")}
            result = service.pairing_request(payload, remote_addr=remote_addr, via=via,
                                             browser="Origin" in request.headers)
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
        # RELEASE-9 (B2): the terminal line ends with the credential, so it is
        # extended by the same renewal.
        renewed_ssh = getattr(service, "ssh_key_renewed", None)
        if callable(renewed_ssh) and renewed.expires_at is not None:
            renewed_ssh(renewed.device_id, renewed.expires_at)
        return web.json_response(service.resource(renewal_document(service.hub.auth, renewed)))

    async def root(request):
        # LITE-1a: a browser opening the host's address gets Odomo.
        # Everything else still gets the description of the surface, so the
        # API origin explains itself instead of looking like a failed pairing.
        if odomo and "text/html" in (request.headers.get("Accept") or ""):
            return web.Response(status=302, headers={"Location": "/odomo/"})
        return web.json_response({
            "service": "omodachid",
            "contract_revision": CONTRACT_REVISION,
            "message": "This is the Omodachi host API. Odomo is at /odomo/.",
            "health": "/health",
            "authenticated_api": "/v1/*",
            "web_ui": "/odomo/",
        })

    async def odomo_redirect(request):
        return web.Response(status=301, headers={"Location": "/odomo/"})

    async def legacy_redirect(request):
        # The still-encoded path and query, so nothing decoded reaches a header.
        tail = request.rel_url.raw_path[len(LEGACY_PREFIX):].lstrip("/")
        location = "/odomo/" + tail
        if request.rel_url.raw_query_string:
            location += "?" + request.rel_url.raw_query_string
        return web.Response(status=301, headers={"Location": location})

    async def odomo_file(request):
        """LITE-1a C1: one file of the built Odomo app, from memory."""
        if not odomo:
            raise ServiceError("odomo_unavailable", "Odomo is not built into this core", 503)
        asset = odomo.get(request.match_info.get("path", ""))
        if asset is None:
            raise ServiceError("not_found", status=404)
        headers = {**security_headers(odomo.build), "ETag": '"' + asset.sha256 + '"',
                   "Cache-Control": IMMUTABLE if asset.immutable else REVALIDATE}
        if headers["ETag"] in {value.strip() for value in (request.headers.get("If-None-Match") or "").split(",")}:
            return web.Response(status=304, headers=headers)
        response = web.Response(body=asset.body, headers=headers)
        response.headers["Content-Type"] = asset.content_type
        return response

    async def root_icon(request):
        """LITE-1b: `/favicon.ico` and `/apple-touch-icon*.png` are Odomo's icon."""
        if not odomo:
            raise ServiceError("not_found", status=404)
        asset = odomo.get("apple-touch-icon.png" if "apple" in request.path else "icon-192.png")
        if asset is None:
            raise ServiceError("not_found", status=404)
        headers = {**security_headers(odomo.build), "ETag": '"' + asset.sha256 + '"', "Cache-Control": REVALIDATE}
        if headers["ETag"] in {value.strip() for value in (request.headers.get("If-None-Match") or "").split(",")}:
            return web.Response(status=304, headers=headers)
        response = web.Response(body=asset.body, headers=headers)
        response.headers["Content-Type"] = asset.content_type
        return response

    # ── LITE-1b C6: Web Push, for web credentials only ──────────────────
    def push_store(request):
        store = getattr(service, "push_store", None)
        if store is None:
            raise ServiceError("push_unavailable", "push is not available on this host", 503)
        # A subscription belongs to a browser. The App has its own
        # notifications and holds no web credential, so it has nothing here.
        if device_role(request[DEVICE]) != "web":
            raise ServiceError("push_web_only", "push subscriptions belong to the web app", 403)
        return store

    async def push_vapid_key(request):
        if request.query or request.can_read_body:
            raise ServiceError("invalid_request")
        push_store(request)
        vapid = await asyncio.to_thread(service.vapid)
        return web.json_response(service.resource({"vapid_public_key": vapid.public_b64u}))

    async def push_subscriptions(request):
        if request.query:
            raise ServiceError("invalid_request")
        store = push_store(request)
        if request.method == "DELETE":
            if request.can_read_body:
                fields(await body(request))
            removed = await asyncio.to_thread(store.unsubscribe, request[DEVICE])
            return web.json_response(service.resource({"device_id": request[DEVICE], "removed": removed}))
        payload = await body(request)
        record = await asyncio.to_thread(store.subscribe, request[DEVICE], payload,
                                         check=still_authorized(request))
        vapid = await asyncio.to_thread(service.vapid)
        from .push import endpoint_host
        return web.json_response(service.resource({
            "subscription_id": record["subscription_id"], "device_id": request[DEVICE],
            "created_at": record["created_at"], "endpoint_host": endpoint_host(record["endpoint"]),
            "vapid_public_key": vapid.public_b64u}), status=201)

    # ── NOTIFY-2: the App says whether it is in front, and what it alerts ──
    async def presence(request):
        if request.query:
            raise ServiceError("invalid_request")
        payload = await body(request)
        return web.json_response(service.set_presence(request[DEVICE], payload))

    async def notification_prefs(request):
        if request.query:
            raise ServiceError("invalid_request")
        store = push_store(request)
        if request.method == "GET":
            if request.can_read_body:
                raise ServiceError("invalid_request")
            return web.json_response(service.resource(await asyncio.to_thread(store.prefs, request[DEVICE])))
        payload = await body(request)
        fields(payload, ("expected_revision", "changes"))
        return web.json_response(service.resource(await asyncio.to_thread(
            store.set_prefs, request[DEVICE], payload["expected_revision"], payload["changes"])))

    async def ws_ticket(request):
        """LITE-1a C5: one 30-second, single-use ticket for one WebSocket path."""
        if request.query:
            raise ServiceError("invalid_request")
        payload = await body(request)
        fields(payload, ("path",))
        path = payload["path"]
        if not browser_gate.ticket_path(path):
            raise ServiceError("invalid_request", "not a WebSocket path")
        # A ticket can only open what its credential could open itself.
        if (device_role(request[DEVICE]) == "web" or "Origin" in request.headers) and \
                not browser_gate.web_allows("GET", path):
            raise ServiceError("web_client_forbidden", "not available to the web app", 403)
        return web.json_response(service.resource(tickets.mint(request[DEVICE], request[TOKEN], path)))

    async def health(request):
        return web.json_response(service.resource(service.hub.dispatch("health", {})))

    async def read(request):
        name = request.match_info["name"]
        # HERDR-11 §4: the catalog revision this client already holds.
        known = request.headers.get(CATALOG_HEADER)
        known = known if known and len(known) <= 128 else None
        values = {"state": lambda: service.state(request[DEVICE], known_catalog=known),
                  "capabilities": service.hub.capabilities_snapshot,
                  "herdr": service.hub.herdr_snapshot,
                  "catalog": lambda: service.search_catalog(request.query.get("q", ""))}
        if set(request.query) - ({"q"} if name == "catalog" else set()):
            raise ServiceError("invalid_request")
        if name not in values:
            raise ServiceError("route_unavailable", status=404)
        return conditional_json(request, service.resource(values[name]()))

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

    async def chat_new(request):
        """AGENT-NEW-1: a fresh default conversation. One audit line either
        way: the device, its role, the thread it left and the one it got."""
        from .agent_chat_service import journal as journal_agent
        device=request[DEVICE];payload=await body(request)
        previous=payload.get('expected_provider_session_id')
        entry={'event':'agent.conversation.new','device':device,'role':device_role(device),
               'previous_thread_id':previous[:128] if isinstance(previous,str) else None}
        try:result=await chat_service().new_conversation(payload,chat_authority(request))
        except ServiceError as error:
            journal_agent({**entry,'outcome':error.code});raise
        journal_agent({**entry,'outcome':'started','thread_id':result['provider_session_id']})
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

    async def chat_mode(request):
        return web.json_response(service.resource(await chat_service().set_mode(await body(request),chat_authority(request))))

    async def chat_steer(request):
        return web.json_response(service.resource(await chat_service().steer(await body(request),chat_authority(request))))

    async def chat_queue_add(request):
        # AGENT-QUEUE-1: Send while a turn runs - codex's own queue.
        if request.query:raise ServiceError('invalid_request')
        return web.json_response(service.resource(await chat_service().queue_add(await body(request),chat_authority(request))))

    async def chat_queue_item(request):
        # AGENT-QUEUE-1: a waiting message's Send now / Edit / Delete.
        if request.query:raise ServiceError('invalid_request')
        result=await chat_service().queue_item(request.match_info['queue_id'],request.match_info['action'],
                                               await body(request),chat_authority(request))
        return web.json_response(service.resource(result))

    async def chat_history(request):
        # AGENT-QUEUE-1: earlier rows, a page at a time.
        if request.can_read_body or set(request.query)-{'before','limit'}:raise ServiceError('invalid_request')
        result=await chat_service().history(request.query.get('before'),request.query.get('limit'),chat_authority(request))
        return web.json_response(service.resource(result))

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
        queue=asyncio.Queue(maxsize=1024);chat.subscribers.add(queue)
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
        watched=False
        try:
            snapshot=await chat.snapshot(authorize)
            await ws.prepare(request);sockets.add(ws)
            # NOTIFY-1: the conversation on a device's screen is seen (`seen.py`).
            seen_watch('agent:default',+1);watched=True
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
        finally:
            chat.subscribers.discard(queue);sockets.discard(ws)
            if watched:seen_watch('agent:default',-1)
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
        the session owner check; WayVNC's socket path stays inside this process
        (RELEASE-9: a Unix socket in a 0700 directory, not a loopback port).
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
        path = remote.vnc_endpoint(device, session_id)
        channel = "vnc-" + uuid.uuid4().hex
        remote.claim_vnc_bridge(session_id, channel)  # 409 before the upgrade
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path), 5)
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
        layout = await asyncio.to_thread(service.herdr_layout, request[DEVICE])
        # HERDR-11 §4: the session and its revision are the projection's
        # identity; a client holding that revision is answered 304.
        etag = '"%s:%s"' % (layout.get("session"), layout.get("revision"))
        return conditional_json(request, layout, etag=etag)

    async def herdr_pane_reply(request):
        """HERDR-11 §2: the latest reply of the agent in a pane, from its own log."""
        if request.query or request.can_read_body:
            raise ServiceError("invalid_request")
        from .herdr_reply import etag as reply_etag
        reply = await asyncio.to_thread(service.herdr_pane_reply, request.match_info["pane"], request[DEVICE])
        return conditional_json(request, reply, etag=reply_etag(reply))

    async def herdr_folder_favourite(request):
        """HERDR-11 §1: star or un-star a folder on the New workspace card."""
        if request.query:
            raise ServiceError("invalid_request")
        payload = await body(request)
        result = await asyncio.to_thread(service.herdr_folder_favourite, request[DEVICE], payload)
        return web.json_response(result)

    async def herdr_sessions(request):
        """Every Herdr session on the host, and which one this device is on."""
        _no_query(request)
        return web.json_response(await asyncio.to_thread(service.herdr_sessions, request[DEVICE]))

    async def herdr_select_session(request):
        await body(request)
        result = await asyncio.to_thread(service.herdr_select_session, request[DEVICE],
                                         request.match_info["name"])
        return web.json_response(result)

    async def herdr_start_session(request):
        """HERDR-5: bring a stopped session up headless, and land in it."""
        await body(request)
        result = await asyncio.to_thread(service.herdr_start_session, request[DEVICE],
                                         request.match_info["name"])
        return web.json_response(result)

    async def herdr_create_session(request):
        """HERDR-5: a new named session in $HOME, started the same way."""
        if request.query:
            raise ServiceError("invalid_request")
        payload = await body(request)
        result = await asyncio.to_thread(service.herdr_create_session, request[DEVICE], payload)
        return web.json_response(result)

    async def herdr_create_workspace(request):
        """HERDR-6: a new workspace in a running session, in a folder the
        device picked, without moving the computer's screen."""
        if request.query:
            raise ServiceError("invalid_request")
        payload = await body(request)
        result = await asyncio.to_thread(service.herdr_create_workspace, request[DEVICE],
                                         request.match_info["name"], payload)
        return web.json_response(result)

    async def herdr_folders(request):
        """HERDR-6: recent and common folders, or one folder's folders."""
        if request.can_read_body:
            raise ServiceError("invalid_request")
        result = await asyncio.to_thread(service.herdr_folders, request[DEVICE], dict(request.query))
        return web.json_response(result)

    async def herdr_pane_action(request):
        payload = await body(request)
        result = await asyncio.to_thread(service.herdr_pane_action, request.match_info["pane"],
                                         request.match_info["action"], payload, request[DEVICE])
        return web.json_response(result)

    async def herdr_pane_keys(request):
        """HERDR-3: keys into a pane without taking it (`pane.send_keys`)."""
        payload = await body(request)
        result = await asyncio.to_thread(service.herdr_pane_keys, request.match_info["pane"],
                                         payload, request[DEVICE])
        return web.json_response(result)

    async def herdr_pane_text(request):
        """HERDR-3: text into a pane, submitted only with proof on screen."""
        payload = await body(request)
        result = await asyncio.to_thread(service.herdr_pane_text, request.match_info["pane"],
                                         payload, request[DEVICE])
        return web.json_response(result)

    async def herdr_operator_rows(request):
        """HERDR-4: the Quick dock, key presets and harness segment for one
        kind of pane, with the operator's own TOML rows applied."""
        if request.can_read_body:
            raise ServiceError("invalid_request")
        result = await asyncio.to_thread(service.herdr_operator_rows, dict(request.query))
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
        # HERDR-3: `?takeover=1` also takes the pane from another device's
        # stream on this bridge; that stream is told to end with `taken_over`.
        loop = asyncio.get_running_loop()
        evicted = asyncio.Event()
        if mode == "control":
            try:
                bridge.claim_control(pane, channel, takeover=takeover,
                                     evict=lambda: loop.call_soon_threadsafe(evicted.set))  # 409 before the upgrade
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
        # NOTIFY-1: a pane on a device's screen is a pane seen (`seen.py`).
        seen_key = f"herdr:{bridge.session}:{pane}"
        seen_watch(seen_key, +1)
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

        async def taken_over():
            await evicted.wait()
            return "taken_over"

        reason = "disconnected"
        tasks: list[asyncio.Task] = []
        try:
            state["process"] = await spawn()
            tasks = [asyncio.create_task(job()) for job in (to_client, to_host, guard, taken_over)]
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
            seen_watch(seen_key, -1)
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
            enrolled = keys.key_rows()
            return web.json_response(service.resource({
                "enabled": keys.enabled(), "host_id": keys.host_id, "host_name": keys.host_name,
                "device_id": device, "challenge": keys.challenge(device),
                "enrolled": any(row["device_id"] == device for row in enrolled),
                "keys": enrolled}))
        if request.method == "DELETE":
            return web.json_response(service.resource(keys.revoke(device)))
        return web.json_response(service.resource(keys.enroll(device, await body(request),
                                                              check=still_authorized(request))))

    async def ssh_key(request):
        """UX-4 §2. `PUT /v1/ssh/key` - this device states the key it offers.

        The device is the one on the credential, never a field in the body: a
        credential can replace its own `authorized_keys` line and nothing else.
        """
        if request.method == "GET":
            if set(request.query):
                raise ServiceError("invalid_request")
            return web.json_response(service.ssh_key_state(request[DEVICE]))
        payload = await body(request)
        return web.json_response(service.ssh_key(request[DEVICE], payload, authorize=still_authorized(request)))

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

    def upload_store():
        store = getattr(service, "uploads", None)
        if store is None:
            raise ServiceError("upload_unavailable", "uploads are not set up on this host", 503)
        return store

    UPLOAD_QUERY = {"name", "target", "session", "pane"}

    def upload_target(values):
        kind = values.get("target")
        if kind is None:
            return {}
        if kind not in ("agent", "herdr"):
            raise ServiceError("invalid_request", "target is agent or herdr")
        target = {"kind": kind}
        session, pane = values.get("session"), values.get("pane")
        if session is not None:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", session):
                raise ServiceError("invalid_request")
            target["session"] = session
        if pane is not None:
            if not re.fullmatch(r"w[0-9]{1,9}:p[0-9]{1,9}", pane):
                raise ServiceError("invalid_request")
            target["pane"] = pane
        return target

    async def stream_into(receiving, reader):
        """The body, chunk by chunk, into the `.part` file; the cap is checked
        on every chunk, so an oversize upload stops at 25 MB + one chunk."""
        while True:
            try:
                chunk = await asyncio.wait_for(reader(), 30.0)
            except asyncio.TimeoutError:
                receiving.abort()
                raise ServiceError("request_timeout", "upload stalled", 408) from None
            if not chunk:
                return
            await asyncio.to_thread(receiving.write, chunk)

    async def uploads(request):
        """ATTACH-1. `POST /v1/uploads[?name=&target=agent|herdr&session=&pane=]`
        with the file as the raw body, or `multipart/form-data` with one file
        part (and the same fields as parts). Answers `upload.schema.json`."""
        store = upload_store()
        device = request[DEVICE]
        if set(request.query) - UPLOAD_QUERY:
            raise ServiceError("invalid_request")
        values = dict(request.query)
        length = request.content_length
        if length is not None and length > store.max_bytes + 65536:
            journal_upload({"event": "upload.refused", "device": device, "reason": "upload_too_large",
                            "bytes": length})
            raise ServiceError("upload_too_large", REFUSALS["upload_too_large"], 413)
        receiving = await asyncio.to_thread(store.receive)
        name = None
        try:
            kind = (request.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if kind == "multipart/form-data":
                reader = await request.multipart()
                found = False
                while (part := await reader.next()) is not None:
                    if part.filename is not None and part.name in (None, "file"):
                        if found:
                            raise ServiceError("invalid_request", "one file per upload")
                        found = True
                        name = values.get("name") or part.filename
                        await stream_into(receiving, lambda: part.read_chunk(65536))
                    elif part.name in UPLOAD_QUERY - {"name"} or part.name == "name":
                        raw = await part.read(decode=True)
                        if len(raw) > 256:
                            raise ServiceError("invalid_request")
                        values.setdefault(part.name, raw.decode("utf-8", "replace"))
                    else:
                        raise ServiceError("invalid_request", "unexpected multipart field")
                if not found:
                    raise ServiceError("invalid_request", "no file part")
            else:
                name = values.get("name")
                await stream_into(receiving, lambda: request.content.read(65536))
            if not isinstance(name, str) or not name or len(name) > 512:
                raise ServiceError("invalid_request", "the upload needs a file name")
            target = upload_target(values)
            # The recheck `body()` does: a credential revoked while the body
            # streamed in must not get to leave a file behind.
            try:
                if service.hub.authenticate(request[TOKEN]) != device:
                    raise ValueError
            except ValueError as error:
                raise credential_refused(error, "credential revoked or expired") from None
            upload = await asyncio.to_thread(receiving.finish, name, device=device, target=target)
        except ServiceError as error:
            receiving.abort()
            if error.code.startswith("upload_"):
                journal_upload({"event": "upload.refused", "device": device, "reason": error.code,
                                "bytes": receiving.size})
            raise
        except BaseException:
            receiving.abort()
            raise
        journal_upload({"event": "upload.saved", "device": device, "kind": upload.kind,
                        "bytes": upload.bytes, "target": target.get("kind")})
        return web.json_response(service.resource(upload.document(store.ttl)))

    async def upload_delete(request):
        """ATTACH-1: a chip taken off before Send - its file goes now."""
        if request.query or request.can_read_body:
            raise ServiceError("invalid_request")
        result = await asyncio.to_thread(upload_store().delete, request.match_info["id"])
        journal_upload({"event": "upload.removed", "device": request[DEVICE]})
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
        result = await service.dispatch_remote_async("remote.capabilities", {}, request[DEVICE])
        return web.json_response(await road_for(request, result))

    async def road_for(request, capabilities):
        """TAIL-2 P2-2: how this device reaches the host, and what that means.

        TAIL-1 §5.4: over a relayed (DERP) tailnet path VNC moved ~2.8 MiB/s
        with 1-2 s of lag and filled the relay queue. When Tailscale says this
        peer has no direct path, the default backend becomes VNC (when it is
        available) and `road.reason` says why, so the app can say it too.
        """
        if not isinstance(capabilities, dict):
            return capabilities
        kind = tailnet_module.address_kind(request.remote)
        relayed = None
        if kind == "tailnet":
            tools = tailnet_tools(service)
            status = None
            with suppress(Exception):
                status = await asyncio.wait_for(asyncio.to_thread(tools.status_json), 4.0)
            relayed = tools.peer_path(status, request.remote)["relayed"]
        return apply_road(capabilities, kind, relayed)

    async def host_open(request):
        """LINKS-1. `POST /v1/host/open {source, path | url, check?}`: a path or
        an address the agent showed, opened on the computer with its default
        handler (`host_open.py` has every rule). The bytes never leave."""
        from .host_open import parse as parse_open
        device = request[DEVICE]
        open_request = parse_open(await body(request))
        if open_request.source["kind"] == "agent":
            haystack, cwd = service.host_open_agent_source(open_request.source["item"])
        else:
            haystack, cwd = await asyncio.to_thread(service.host_open_herdr_source, open_request.source, device)
        result = await asyncio.to_thread(service.host_open, open_request, device=device,
                                         role=device_role(device), haystack=haystack, cwd=cwd)
        return web.json_response(result)

    async def host_roads(request):
        # TAIL-2 follow-up: authenticated, so `/health` can stay an anchor.
        if request.query or request.can_read_body:
            raise ServiceError("invalid_request")
        return web.json_response(service.host_roads())

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
        result = await service.dispatch_remote_async(op, payload, request[DEVICE])
        reading = service.host_identity.tailnet_reading() if getattr(service, "host_identity", None) is not None else None
        return web.json_response(on_this_road(result, request, reading),
                                 status=201 if op == "remote.start" else 200)

    sockets: set[web.WebSocketResponse] = set()
    audio_sockets: set[web.WebSocketResponse] = set()
    vnc_sockets: set[web.WebSocketResponse] = set()
    herdr_sockets: set[web.WebSocketResponse] = set()

    async def events(request):
        if set(request.query) - {"since", "instance_id", "heartbeat"}:
            raise ServiceError("invalid_request", "event query may contain only since, instance_id and heartbeat")
        # LITE-1a L4. A browser cannot see WebSocket pings, so a path that died
        # quietly looks exactly like a quiet host (TAIL-1 §5.5 found the native
        # app stuck "Connected" that way). `heartbeat=1` asks for a small
        # message every HEARTBEAT seconds; a client that hears nothing for two
        # of them knows the socket is dead. Opt-in, so no existing client sees
        # a message type it does not know.
        if request.query.get("heartbeat", "1") != "1":
            raise ServiceError("invalid_request", "heartbeat may only be 1")
        heartbeat = "heartbeat" in request.query
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

        async def beat():
            while not ws.closed and heartbeat:
                await asyncio.sleep(HEARTBEAT)
                await ws.send_json({"type": "heartbeat", "instance_id": service.instance_id})
            await asyncio.Event().wait()

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

        tasks = [asyncio.create_task(task()) for task in (pump, check_auth, receive, beat)]
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
    async def push_lifecycle(app):
        # LITE-1b C6: the sender lives as long as the HTTPS app does, on its loop.
        sender = getattr(service, "push_sender", None)
        if sender is not None:
            sender.start(asyncio.get_running_loop())
        yield
        if sender is not None:
            await sender.stop()
    app.cleanup_ctx.append(push_lifecycle)
    async def voice_lifecycle(app):
        mirror = getattr(service, "notification_mirror", None)
        if mirror is not None:
            # LITE-1b: the mirror's watch thread publishes on this loop, never
            # into the hub's queues from its own thread.
            loop = asyncio.get_running_loop()
            mirror.dispatch = loop.call_soon_threadsafe
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
    async def uploads_lifecycle(app):
        # ATTACH-1: what was sent more than seven days ago goes, at start and
        # every hour after.
        store = getattr(service, "uploads", None)
        async def sweep():
            while True:
                with suppress(Exception):
                    removed = await asyncio.to_thread(store.sweep)
                    if removed:
                        journal_upload({"event": "upload.swept", "count": len(removed)})
                await asyncio.sleep(SWEEP_INTERVAL)
        task = asyncio.create_task(sweep()) if store is not None else None
        yield
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    app.cleanup_ctx.append(uploads_lifecycle)
    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    app.router.add_get("/odomo", odomo_redirect)
    app.router.add_get("/odomo/", odomo_file)
    app.router.add_get("/odomo/{path:.+}", odomo_file)
    app.router.add_get(LEGACY_PREFIX, legacy_redirect)
    app.router.add_get(LEGACY_PREFIX + "/{path:.*}", legacy_redirect)
    app.router.add_get("/favicon.ico", root_icon)
    app.router.add_get(r"/{name:apple-touch-icon(?:-[0-9]{2,3}x[0-9]{2,3})?(?:-precomposed)?\.png}", root_icon)
    app.router.add_post("/v1/ws-ticket", ws_ticket)
    app.router.add_get("/v1/push/vapid-public-key", push_vapid_key, allow_head=False)
    app.router.add_post("/v1/push/subscriptions", push_subscriptions)
    app.router.add_delete("/v1/push/subscriptions", push_subscriptions)
    app.router.add_get("/v1/notifications/prefs", notification_prefs, allow_head=False)
    app.router.add_put("/v1/notifications/prefs", notification_prefs)
    app.router.add_put("/v1/presence", presence)
    app.router.add_get("/v1/events", events)
    app.router.add_post("/v1/pairing/requests", pairing_request)
    app.router.add_post("/v1/pairing/requests/{id}/claim", pairing_request)
    app.router.add_get("/v1/pairing/credential", pairing_credential, allow_head=False)
    app.router.add_post("/v1/pairing/renew", pairing_renew)
    app.router.add_post("/v1/actions/{id}:invoke", action)
    app.router.add_post("/v1/control/wake", control_wake)
    app.router.add_get("/v1/shortcuts", shortcuts)
    app.router.add_get("/v1/host/roads", host_roads, allow_head=False)
    app.router.add_post("/v1/host/open", host_open)
    app.router.add_post("/v1/workspaces/{id:-?[0-9]+}/select", workspace_select)
    app.router.add_post("/v1/workspaces/relative/{step:e[+-]1}/select", workspace_select_relative)
    app.router.add_post("/v1/agent/tasks", agent_task)
    app.router.add_post("/v1/agent/default:ensure", agent_ensure)
    app.router.add_get("/v1/agent/default/chat/commands",chat_commands)
    app.router.add_post("/v1/agent/default/chat/commands/{command}:execute",chat_command_execute)
    app.router.add_post("/v1/agent/default/chat/recover-empty",chat_recover_empty)
    app.router.add_post("/v1/agent/default/chat:new",chat_new)
    app.router.add_post("/v1/agent/default/chat/handoff:prepare",chat_handoff_prepare)
    app.router.add_post("/v1/agent/default/chat/handoff:confirm",chat_handoff_confirm)
    app.router.add_get("/v1/agent/default/chat",chat_snapshot)
    app.router.add_get("/v1/agent/default/chat/events",chat_events)
    app.router.add_post("/v1/agent/default/chat/messages",chat_send)
    app.router.add_post("/v1/agent/default/chat/interrupt",chat_interrupt)
    app.router.add_post("/v1/agent/default/chat/steer",chat_steer)
    app.router.add_post("/v1/agent/default/chat/mode",chat_mode)
    app.router.add_post("/v1/agent/default/chat/queue",chat_queue_add)
    app.router.add_post(r"/v1/agent/default/chat/queue/{queue_id}:{action:(send|edit|delete)}",chat_queue_item)
    app.router.add_get("/v1/agent/default/chat/history",chat_history,allow_head=False)
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
    app.router.add_get("/v1/herdr/operator-rows", herdr_operator_rows, allow_head=False)
    app.router.add_post("/v1/herdr/sessions/{name:[A-Za-z0-9][A-Za-z0-9_.-]{0,63}}/select", herdr_select_session)
    app.router.add_post("/v1/herdr/sessions/{name:[A-Za-z0-9][A-Za-z0-9_.-]{0,63}}/start", herdr_start_session)
    app.router.add_post("/v1/herdr/sessions", herdr_create_session)
    app.router.add_post("/v1/herdr/sessions/{name:[A-Za-z0-9][A-Za-z0-9_.-]{0,63}}/workspaces", herdr_create_workspace)
    app.router.add_get("/v1/herdr/folders", herdr_folders, allow_head=False)
    app.router.add_post("/v1/herdr/folders/favourites", herdr_folder_favourite)
    app.router.add_get("/v1/herdr/panes/{pane}/reply", herdr_pane_reply, allow_head=False)
    app.router.add_get("/v1/herdr/panes/{pane}/{mode:observe|control}", herdr_stream, allow_head=False)
    app.router.add_post("/v1/herdr/panes/{pane}/{action:split|zoom|focus|close}", herdr_pane_action)
    app.router.add_post("/v1/herdr/panes/{pane}/keys", herdr_pane_keys)
    app.router.add_post("/v1/herdr/panes/{pane}/text", herdr_pane_text)
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
    app.router.add_post("/v1/uploads", uploads)
    app.router.add_delete(r"/v1/uploads/{id:[0-9a-f]{32}}", upload_delete)
    app.router.add_get("/v1/clipboard", clipboard, allow_head=False)
    app.router.add_put("/v1/clipboard", clipboard)
    app.router.add_get("/v1/remote/capabilities", remote_capabilities, allow_head=False)
    app.router.add_post("/v1/remote/sessions", remote_session)
    app.router.add_get("/v1/remote/sessions/{id}", remote_session, allow_head=False)
    app.router.add_delete("/v1/remote/sessions/{id}", remote_session)
    app.router.add_post("/v1/remote/sessions/{id}/{operation:resize|backend|heartbeat|presented|text}", remote_session)
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
