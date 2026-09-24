"""Daemon and local helper commands. All path flags are local admin inputs."""
from __future__ import annotations

import argparse
import asyncio
import importlib
from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import subprocess

from .auth import DEFAULT_GRACE_SECONDS, DEFAULT_TTL_SECONDS, DeviceAuthenticator
from .hub import Hub
from .ipc import JsonLineClient, JsonLineServer
from .remote import SUNSHINE_PAIRING_SOCKET
from .runtime_paths import client_socket_path, default_socket_path, legacy_socket_path
from .protocol import PANEL_VIEWS

DEFAULT_QUALITY = {"max_pixels": 4000000, "fps": 60, "bitrate_kbps": 20000}
DEFAULT_DECODER = {"max_width": 4096, "max_height": 4096, "max_pixels": 16777216,
                   "max_fps": 60, "max_bitrate_kbps": 40000, "codecs": ["h264"]}


def bar_occlusion(value: str) -> dict:
    """REMOTE-SAFE-1: `TOP,BOTTOM,LEFT,RIGHT` in the device's points."""
    try:
        numbers = [float(part) for part in value.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("expected TOP,BOTTOM,LEFT,RIGHT") from None
    if len(numbers) != 4:
        raise argparse.ArgumentTypeError("expected TOP,BOTTOM,LEFT,RIGHT")
    return dict(zip(("top", "bottom", "left", "right"), numbers))


def viewport(value: str) -> dict:
    try:
        width, height = (int(part) for part in value.lower().split("x", 1))
    except ValueError:
        raise argparse.ArgumentTypeError("viewport must be WIDTHxHEIGHT") from None
    if not 64 <= width <= 16384 or not 64 <= height <= 16384:
        raise argparse.ArgumentTypeError("viewport is out of range")
    return {"width": width, "height": height}


def daemon_main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="omodachid", description="Omodachi device hub")
    # AUTH-2: `/run/omodachi/<uid>` when the host's root step made it, the
    # runtime directory otherwise - never the home cache. polkit's agent helper
    # runs under `ProtectHome=yes`, which blanks `/home`, `/root` and
    # `/run/user`, so a socket in any of those is one it cannot reach.
    parser.add_argument("--socket", default=default_socket_path())
    parser.add_argument("--compatibility-symlink", action=argparse.BooleanOptionalAction, default=True,
                        help="keep ~/.cache/omodachi/omodachid.sock pointing at the socket "
                             "while the daemon runs, for anything still holding the old path")
    parser.add_argument("--secret-file", default=os.path.expanduser("~/.config/omodachi/device.secret"))
    admin = parser.add_mutually_exclusive_group()
    admin.add_argument("--issue-token", metavar="DEVICE_ID")
    admin.add_argument("--revoke-device", metavar="DEVICE_ID")
    parser.add_argument("--demo", action="store_true", help="synthetic local fixture adapters; no host changes")
    parser.add_argument("--default-menu", type=Path)
    parser.add_argument("--user-menu", type=Path)
    parser.add_argument("--omodachi-menu", type=Path)
    parser.add_argument("--shell-config", type=Path)
    parser.add_argument("--listen", metavar="IP", help="enable HTTPS/WSS, e.g. 127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--tls-cert")
    parser.add_argument("--tls-key")
    parser.add_argument("--allow-loopback-http", action="store_true")
    parser.add_argument("--discovery", choices=("auto", "off"), default="auto",
                        help="advertise _omodachi._tcp when actually listening on a LAN address")
    parser.add_argument("--discovery-name", help="public LAN discovery instance name; requires --discovery-server and --discovery-address")
    parser.add_argument("--discovery-server", help="public .local hostname for LAN discovery")
    parser.add_argument("--discovery-address", action="append", default=[], help="explicit LAN address to advertise; repeatable")
    parser.add_argument("--remote-ttl", type=float, default=30.0,
                        help="default heartbeat budget for a Remote session, in seconds")
    # CORE-2 §1. Test switches, not preferences: they exist so the whole
    # credential lifecycle (renew, grace, expiry) can be walked in minutes on a
    # throwaway daemon. The installed unit never passes them.
    parser.add_argument("--credential-ttl", type=int, default=DEFAULT_TTL_SECONDS, metavar="SECONDS",
                        help="testing only: device credential lifetime (default: 30 days)")
    parser.add_argument("--credential-grace", type=int, default=DEFAULT_GRACE_SECONDS, metavar="SECONDS",
                        help="testing only: how long a renewed credential keeps working (default: 24 h)")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535 or not 5 <= args.remote_ttl <= 3600:
        parser.error("invalid port or Remote TTL")
    if not 60 <= args.credential_ttl <= 366 * 86400 or not 0 <= args.credential_grace <= 7 * 86400:
        parser.error("invalid credential TTL or grace")
    authority = DeviceAuthenticator.from_file(args.secret_file, ttl_seconds=args.credential_ttl,
                                              grace_seconds=args.credential_grace)
    if args.issue_token:
        print(authority.issue(args.issue_token).token)
        return 0
    if args.revoke_device:
        try:
            result = asyncio.run(JsonLineClient(args.socket, timeout=12).request("local.devices.revoke", device_id=args.revoke_device))
        except (OSError, asyncio.TimeoutError):
            result = {"ok": False, "error": "daemon_unavailable"}
        print(json.dumps(result["result"] if result.get("ok") else result))
        return 0 if result.get("ok") else 1

    async def run():
        from .bootstrap import create_service
        hub = Hub(authenticator=authority)
        remote_manager, remote_manager_factory = None, None
        if not args.demo:
            from .remote import build_manager

            def remote_manager_factory():
                manager = build_manager()
                manager.default_ttl = args.remote_ttl
                return manager

            try:
                remote_manager = remote_manager_factory()
            except Exception as error:
                # No graphical session yet: the daemon still serves Panel, agent
                # and Herdr, and reports Remote as unavailable. INSTALL-1 §1.3:
                # it is the same factory that gets tried again later, so this
                # is now a first attempt rather than the only one. On a fresh
                # install the daemon is started by the installer, before the
                # user has ever logged into Hyprland, so this branch is the
                # ordinary path and not the exception.
                print(json.dumps({"remote": "unavailable", "retrying": True,
                                  "reason": getattr(error, "code", type(error).__name__)}),
                      flush=True)
        from .media_pairing import MediaPairingBridge, MediaPairingStore, SunshinePairingIPC
        runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        media_bridge = MediaPairingBridge(
            SunshinePairingIPC(runtime_dir / SUNSHINE_PAIRING_SOCKET),
            MediaPairingStore(Path(args.secret_file).parent / "media-pairing/state.json"))
        from .preferences import HostPreferencesStore
        preferences_store = HostPreferencesStore(Path(args.secret_file).parent / "preferences/state.json")
        from .audio_uplink import PulseVirtualInputFactory
        audio_factory = None if args.demo else PulseVirtualInputFactory()
        from .host_identity import HostIdentity, host_addresses
        identity = HostIdentity.load(Path(args.secret_file).parent,
                                     certificate=args.tls_cert, port=args.port)
        discovery_config, discovery_reason = None, "off"
        explicit = any((args.discovery_name, args.discovery_server, args.discovery_address))
        if args.discovery != "off" and (explicit or args.listen):
            if not args.listen: raise ValueError("discovery_requires_https_listener")
            from .lan_discovery import DiscoveryConfig, DiscoveryError
            if explicit and not (args.discovery_name and args.discovery_server and args.discovery_address):
                raise ValueError("discovery requires --discovery-name, --discovery-server and --discovery-address")
            # Advertising the host's own name and addresses is the ordinary
            # case; the explicit flags stay as an operator override. Whether the
            # record is actually published is still decided by the real bound
            # sockets, so a loopback listener never advertises.
            addresses = (tuple(args.discovery_address) if explicit
                         else tuple(value for value in host_addresses() if ":" not in value))
            try:
                if not addresses: raise DiscoveryError("discovery_no_lan_address")
                discovery_config = DiscoveryConfig(
                    args.discovery_name or identity.host_name,
                    args.discovery_server or identity.host_name + ".local.",
                    addresses, host_id=identity.host_id, host_name=identity.host_name,
                    fingerprint=identity.fingerprint())
                discovery_reason = "configured" if explicit else "auto"
            except DiscoveryError as error:
                if explicit: raise ValueError(error.code) from None
                # An unadvertisable name or address never stops the API; the
                # reason travels in the ready frame, which stays one line.
                discovery_reason = error.code
        service = create_service(hub, demo=args.demo, media_pairing=media_bridge, preferences_store=preferences_store, audio_session_factory=audio_factory, default_menu=args.default_menu,
                                 user_menu=args.user_menu, omodachi_menu=args.omodachi_menu, shell_config=args.shell_config,
                                 remote_manager=remote_manager,
                                 remote_manager_factory=remote_manager_factory)
        from .pairing import PairingStore
        service.pairing = PairingStore(authority, Path(args.secret_file).parent / "pairing.json")
        from .herdr_bridge import HerdrSessionChoices
        service.herdr_choices = HerdrSessionChoices(Path(args.secret_file).parent / "herdr-sessions.json")
        # AUTH-1. The broker holds no secret of its own: enrolled public keys
        # on disk, pending approvals in memory only. A daemon restart forgets
        # every pending approval, which is the correct answer - the PAM prompt
        # that was waiting has already fallen back to the password.
        from .biometric import ApprovalBroker, BiometricKeyStore
        service.biometric = ApprovalBroker(
            hub, BiometricKeyStore(Path(args.secret_file).parent / "biometric-keys.json"),
            preferences=preferences_store, host_identity=identity)
        service.install_host_identity(identity)
        server = JsonLineServer(hub, args.socket, local_handler=service.dispatch_local_async,
                                compatibility_link=(legacy_socket_path()
                                                    if args.compatibility_symlink else None))
        network = None
        maintenance = None
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        try:
            await server.start()
            if args.listen:
                from .network import NetworkServer
                network = NetworkServer(service, host=args.listen, port=args.port, certificate=args.tls_cert,
                                        private_key=args.tls_key, allow_loopback_http=args.allow_loopback_http,
                                        discovery_config=discovery_config)
                await network.start()
                identity.port = network.bound_port
            from .resources import ResourceMonitor
            hub.resources = ResourceMonitor(hub)
            async def maintain():
                ticks = 0
                while True:
                    await asyncio.sleep(0.25)
                    hub.tick()
                    ticks += 1
                    # PERF-4 §0. Four numbers, once a minute, in the journal.
                    # The host was at 12 GB before anybody knew there was
                    # anything to look at.
                    if ticks % 240 == 0:
                        reading = hub.resources.snapshot()
                        print(json.dumps({"log": hub.resources.line(reading),
                                          "level": "warning" if hub.resources.warnings(reading) else "info"}),
                              flush=True)
                    if ticks % 4 == 0:
                        # PERF-4 §0: one compositor probe a second, not two.
                        # Each is three `hyprctl -j` calls and it is not what
                        # makes a tap feel immediate - the client draws the
                        # square it tapped and the host reads the workspace
                        # back inside `select_existing` either way.
                        service.schedule_workspace_refresh()
                    if ticks % 2 == 0:
                        service.schedule_media_maintenance()
                    if ticks % 8 == 0:
                        # PERF-4. Every `when`/`checked` in the Omarchy menu is
                        # a shell command and this tick used to run the cold
                        # ones on the event loop, which is the thread that also
                        # answers every HTTP request: a `GET /v1/capabilities`
                        # that reads one dict measured 2-5 s on the live host.
                        # The readings are taken on a worker; the refresh below
                        # then finds them cached and only touches hub state.
                        await asyncio.to_thread(service.warm_readings)
                        service.refresh_sources()
                        # Herdr publishes events only on the connection that
                        # subscribed, and 0.8.2's replay behaviour on subscribe
                        # is unverified, so the owned session is projected from
                        # its snapshot on the two-second cadence SPEC-F1 sets.
                        service.schedule_herdr_layout_refresh()
                    if ticks % 20 == 0:
                        service.schedule_agent_refresh()
                    if ticks % 60 == 0 and service.remote.manager is None:
                        # INSTALL-1 §1.3. The daemon starts before the
                        # graphical session does, so the first attempt to build
                        # the Remote manager fails on every fresh install. This
                        # is what makes the panel stop saying "no Remote" by
                        # itself once the desktop is up, instead of waiting for
                        # something to ask for capabilities. It runs only while
                        # there is no manager: the moment one is built this
                        # call publishes the new capabilities and the condition
                        # is false for ever after.
                        await service.remote.refresh_capabilities()
            if not args.demo:
                service.schedule_agent_refresh()
                # The hooks report later changes; the daemon reads the host
                # theme and fonts once at startup so the first client to ask
                # gets an answer that is already current.
                await asyncio.to_thread(service.notify_theme_changed)
                await asyncio.to_thread(service.notify_fonts_changed)
            maintenance = asyncio.create_task(maintain())
            startup_token = os.environ.get("OMODACHI_SIMULATOR_STARTUP_TOKEN")
            ready = {"ready": True, "pid": os.getpid(), "socket": args.socket, "host": args.listen,
                     "port": network.bound_port if network else None, "demo": args.demo,
                     "instance_id": hub.instance_id, "host_id": identity.host_id,
                     "discovery": discovery_reason,
                     "tls_fingerprint_sha256": identity.fingerprint(),
                     "contract_revision": "omodachi.v1"}
            if startup_token:
                ready["startup_token"] = startup_token
            print(json.dumps(ready), flush=True)
            await stop.wait()
        finally:
            if maintenance:
                maintenance.cancel()
                with suppress(asyncio.CancelledError):
                    await maintenance
            try:
                if network:
                    await network.close()
            finally:
                try:
                    await server.close()
                finally:
                    await service.close_media()
    try:
        asyncio.run(run())
    except (OSError, ValueError, ImportError, AttributeError) as exc:
        parser.exit(1, f"omodachid: {exc}\n")
    return 0


async def remote_request(args, token):
    """One live session per host, so the CLI can resolve its ID itself."""
    client = JsonLineClient(args.socket, token, timeout=90)
    operation = args.remote_operation
    if operation == "status":
        return await client.request("remote.status")
    if operation == "recover":
        if getattr(args, "orphans", False):
            return await client.request("remote.recover", orphans=True)
        return await client.request("remote.recover")
    session_id = getattr(args, "session_id", None)
    if operation != "start" and not session_id:
        status = await client.request("remote.status")
        if not status.get("ok"):
            return status
        session = (status["result"] or {}).get("session")
        if session is None:
            return {"ok": False, "error": "session_not_found", "message": "no live Remote session"}
        session_id = session["id"]
    if operation == "stop":
        return await client.request("remote.stop", session_id=session_id)
    view = args.viewport
    payload = {"quality": DEFAULT_QUALITY, "decoder": DEFAULT_DECODER}
    if view is not None:
        payload["viewport_points"] = view
        payload["orientation"] = args.orientation or ("landscape_left" if view["width"] >= view["height"] else "portrait")
    elif args.orientation:
        payload["orientation"] = args.orientation
    payload["logical_long_edge"] = args.logical_long_edge or 1280.0
    if getattr(args, "quality_preset", None):
        payload["quality_preset"] = args.quality_preset
    if getattr(args, "bar_occlusion", None) is not None:
        payload["bar_occlusion_points"] = args.bar_occlusion
    if getattr(args, "fps", None) or getattr(args, "bitrate_kbps", None):
        payload["quality"] = {**DEFAULT_QUALITY, **({"fps": args.fps} if args.fps else {}),
                              **({"bitrate_kbps": args.bitrate_kbps} if args.bitrate_kbps else {})}
    if operation == "start":
        payload.update(mode=args.mode, backend=args.backend, placement=args.placement, ttl_seconds=args.ttl)
        return await client.request("remote.start", **payload)
    status = await client.request("remote.status")
    if not status.get("ok"):
        return status
    session = (status["result"] or {}).get("session") or {}
    return await client.request("remote.resize", session_id=session_id,
                                expected_revision=session.get("revision"), **payload)


def host_main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="omodachi-host")
    # The newest of the three paths that is actually there, so a client built
    # after the move still finds a daemon from before it. Resolved after
    # parsing rather than as the argparse default, so `plugin-watch` can tell
    # "the user named a socket" from "find it" and keep finding it (RELEASE-3b).
    parser.add_argument("--socket", default=None)
    parser.add_argument("--token", help="prefer OMODACHI_TOKEN to keep credentials out of process arguments")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("health", "state", "capabilities", "catalog", "plugin-watch",
                 "theme-changed", "font-changed"):
        sub.add_parser(name)
    # `omodachi-host herdr` still prints the agent/Herdr snapshot; HERDR-2's two
    # operations hang off it rather than becoming top-level commands.
    herdr = sub.add_parser("herdr", help="the Herdr snapshot, and the host's sessions")
    herdr_sub = herdr.add_subparsers(dest="herdr_operation")
    herdr_sessions = herdr_sub.add_parser("sessions", help="every Herdr session on this host")
    herdr_sessions.add_argument("--device", help="report the session this paired device is on")
    herdr_select = herdr_sub.add_parser("select", help="which session the panel shows")
    herdr_select.add_argument("name")
    herdr_select.add_argument("--device", help="remember the choice for one paired device; "
                                               "without it the host-wide default moves")
    # SUPER+K should land on the iPad's keybindings overlay rather than its root
    # panel, so the view the host asked for travels with the recall.
    sub.add_parser("panel-summon").add_argument("--view", choices=PANEL_VIEWS, default="overview")
    plugin_action = sub.add_parser("plugin-action")
    plugin_action.add_argument("entry_id")
    plugin_action.add_argument("--catalog-revision", required=True)
    plugin_action.add_argument("--request-id", required=True)
    plugin_action.add_argument("--params-json", default="{}")
    plugin_action.add_argument("--state-revision", type=int)
    plugin_action.add_argument("--target-token")
    plugin_action.add_argument("--workspace-revision", type=int)
    plugin_action.add_argument("--workspace-instance")
    desktop_entry = sub.add_parser("desktop-entry", help="install or inspect the user-level launcher")
    desktop_entry_sub = desktop_entry.add_subparsers(dest="desktop_entry_operation", required=True)
    for operation in ("install", "status"):
        command = desktop_entry_sub.add_parser(operation)
        command.add_argument("--home", type=Path, default=Path.home())
    tls = sub.add_parser("tls", help="the self-signed certificate companions pin")
    tls_sub = tls.add_subparsers(dest="tls_operation", required=True)
    for operation in ("show", "rotate"):
        command = tls_sub.add_parser(operation)
        command.add_argument("--tls-dir", type=Path,
                             default=Path(os.path.expanduser("~/.config/omodachi/tls")))
        if operation == "rotate":
            command.add_argument("--no-restart", action="store_true",
                                 help="write the new certificate without restarting omodachid")
    ssh = sub.add_parser("ssh", help="the lines Omodachi owns in ~/.ssh/authorized_keys")
    ssh_sub = ssh.add_subparsers(dest="ssh_operation", required=True)
    for operation in ("authorize", "revoke", "list"):
        command = ssh_sub.add_parser(operation)
        command.add_argument("--home", type=Path, default=Path.home())
        if operation == "authorize":
            command.add_argument("pubkey", help="one OpenSSH public key line, quoted")
        if operation == "list":
            # UX-4 §3: one device with several owned lines is a key drift left
            # behind. Pruning keeps the newest and only ever touches a device
            # that has more than one, so a host whose devices hold one key each
            # is a no-op.
            command.add_argument("--prune", action="store_true",
                                 help="delete the older lines of any device that has more "
                                      "than one, keeping the newest")
            command.add_argument("--device",
                                 help="limit --prune to one device")
        if operation in {"authorize", "revoke"}:
            command.add_argument("--device", required=True,
                                 help="the device the line belongs to; it becomes the "
                                      "'# omodachi:<device>' marker a revoke matches on")
    devices = sub.add_parser("devices")
    devices_sub = devices.add_subparsers(dest="device_operation", required=True)
    devices_list = devices_sub.add_parser("list")
    # PLUG-4 §2.2: the default list is what a user can act on. Everything that
    # was ever revoked is still there, one flag away.
    devices_list.add_argument("--all", action="store_true",
                              help="also list devices whose access is already revoked")
    # UX-4 §3: the rows carry `ssh_keys` now, so the page that shows a device
    # holding two of them is also the page that can put it right.
    devices_list.add_argument("--prune-ssh-keys", dest="prune_ssh_keys", action="store_true",
                              help="before listing, delete the older authorized_keys lines of any "
                                   "device that has more than one, keeping the newest")
    devices_purge = devices_sub.add_parser("purge")
    devices_purge.add_argument("--older-than", type=float, metavar="DAYS", dest="older_than_days",
                               help="only purge devices whose media permission last changed more "
                                    "than DAYS ago; a device with no timestamp is never purged by age")
    devices_revoke = devices_sub.add_parser("revoke")
    devices_revoke.add_argument("device_id")
    # The plugin's own credential draws the page a revoke would be undone from,
    # so taking it back is deliberate rather than one row among the others.
    devices_revoke.add_argument("--force", action="store_true",
                                help="also allow revoking this host's own plugin credential")
    pair = sub.add_parser("pair")
    pair_sub = pair.add_subparsers(dest="pair_operation", required=True)
    for operation in ("begin", "pending", "approve", "reject"):
        command = pair_sub.add_parser(operation)
        if operation in {"approve", "reject"}: command.add_argument("request_id")
        if operation == "approve":
            # PAIR-3: Remote is what one approval means, so it is the default.
            # `--remote` stays as an accepted no-op - the old instruction to
            # "always pass --remote" is in reports and in muscle memory, and it
            # must keep working rather than become an error.
            command.add_argument("--remote", action="store_true",
                                 help="accepted and ignored; Remote is granted by default")
            command.add_argument("--no-remote", dest="no_remote", action="store_true",
                                 help="approve the companion credential only, without Remote streaming")
    media = sub.add_parser("media-pairing")
    media_sub = media.add_subparsers(dest="media_operation", required=True)
    media_sub.add_parser("pending")
    for operation in ("approve", "cancel"):
        command = media_sub.add_parser(operation)
        for field in ("attempt_id", "request_id", "client_cert_sha256"):
            command.add_argument(field)
    media_sub.add_parser("revoke").add_argument("device_id")
    certificates = media_sub.add_parser("certificates")
    certificates.add_argument("--purge-unknown", action="store_true", dest="purge_unknown",
                              help="revoke the certificates this host has no binding for; "
                                   "a certificate it does know is never touched here")
    grant = media_sub.add_parser("grant-remote")
    grant.add_argument("device_id")
    grant.add_argument("source_request_id")
    auth = sub.add_parser("auth", help="AUTH-1: the keys a paired device approves host prompts with")
    auth_sub = auth.add_subparsers(dest="auth_operation", required=True)
    auth_sub.add_parser("status", help="the preference, the enrolled keys, and who could answer now")
    auth_sub.add_parser("revoke").add_argument("device_id")
    auth_test = auth_sub.add_parser("test", help="raise one approval exactly as PAM would, and print the answer")
    auth_test.add_argument("--service", default="sudo")
    auth_test.add_argument("--timeout", type=float, default=45.0)
    preferences = sub.add_parser("preferences")
    preferences_sub = preferences.add_subparsers(dest="preferences_operation", required=True)
    preferences_sub.add_parser("get")
    prefs_set = preferences_sub.add_parser("set")
    prefs_set.add_argument("--revision", type=int, required=True)
    prefs_set.add_argument("--allow-dynamic-resolution", choices=("true", "false"))
    prefs_set.add_argument("--quality", choices=("balanced", "quality", "performance"))
    prefs_set.add_argument("--host-audio-playback", choices=("true", "false"))
    prefs_set.add_argument("--pairing-mode", choices=("open", "invite"),
                           help="open: a request needs no invitation; invite: it does")
    prefs_set.add_argument("--biometric-auth", choices=("true", "false"),
                           help="AUTH-1: may a paired device satisfy a host password prompt")
    prefs_set.add_argument("--clipboard-sync", choices=("off", "host_to_device", "both"),
                           help="CLIP-1: off; host_to_device lets a paired device read this "
                                "clipboard; both also lets it write this clipboard")
    remote = sub.add_parser("remote", help="the iPad desktop session on this host")
    remote_sub = remote.add_subparsers(dest="remote_operation", required=True)
    remote_sub.add_parser("status")
    remote_sub.add_parser("recover").add_argument(
        "--orphans", action="store_true",
        help="also remove OMODACHI-* outputs no journal of this daemon accounts for; "
             "without it they are reported and left alone, because another daemon "
             "on this compositor may own them (CORE-2)")
    remote_sub.add_parser("stop").add_argument("--session-id")
    for operation in ("start", "resize"):
        command = remote_sub.add_parser(operation)
        command.add_argument("--viewport", type=viewport, required=operation == "start")
        command.add_argument("--orientation", choices=("portrait", "portrait_upside_down", "landscape_left", "landscape_right"))
        command.add_argument("--logical-long-edge", type=float)
        command.add_argument("--session-id")
        # STREAM-1. The device's own point on the host's quality table, or
        # `custom` with --fps / --bitrate-kbps. Absent = the host's preference.
        command.add_argument("--quality-preset", choices=("host", "performance", "balanced", "quality", "custom"))
        command.add_argument("--fps", type=int)
        command.add_argument("--bitrate-kbps", type=int)
        # REMOTE-SAFE-1: what the App sends as bar_occlusion_points, for an
        # operator reproducing a device's corners without the device.
        command.add_argument("--bar-occlusion", type=bar_occlusion, metavar="TOP,BOTTOM,LEFT,RIGHT")
        if operation == "start":
            command.add_argument("--mode", choices=("extend", "takeover"), default="extend")
            command.add_argument("--backend", choices=("sunshine", "vnc"), default="sunshine")
            command.add_argument("--placement", choices=("right", "left", "above", "below"), default="right")
            command.add_argument("--ttl", type=float, default=60.0)
    workspace = sub.add_parser("workspace")
    workspace.add_argument("operation", choices=("select", "move-focused"))
    workspace.add_argument("number", type=int)
    ev = sub.add_parser("events")
    ev.add_argument("--since", type=int, default=0)
    args = parser.parse_args(argv)
    explicit_socket = args.socket is not None
    if not explicit_socket:
        args.socket = client_socket_path()
    if args.command == "plugin-action":
        from .plugin_actions import invoke_plugin_action, parameters, failure
        if args.token is not None:
            parser.error("plugin-action uses the existing internal plugin credential helper")
        try:
            params = parameters(args.params_json)
            result = asyncio.run(invoke_plugin_action(args.socket, entry_id=args.entry_id,
                catalog_revision=args.catalog_revision, request_id=args.request_id, params=params,
                state_revision=args.state_revision, target_token=args.target_token,
                workspace_revision=args.workspace_revision, workspace_instance=args.workspace_instance))
        except (ValueError, TypeError):
            result = failure("invalid_request")
        print(json.dumps(result))
        return 0 if result.get("ok") and result["result"]["status"] != "failed" else 1
    if args.command == "tls":
        # Local administrator operation: it never reads a device credential and
        # never touches the credential registry or pairing state.
        from .host_identity import (HostIdentityError, certificate_fingerprint,
                                    generate_certificate, host_addresses, host_name,
                                    CERTIFICATE_NAME, PRIVATE_KEY_NAME)
        directory = args.tls_dir.expanduser()
        if args.tls_operation == "show":
            fingerprint = certificate_fingerprint(directory / CERTIFICATE_NAME)
            result = {"certificate": str(directory / CERTIFICATE_NAME),
                      "tls_fingerprint_sha256": fingerprint,
                      "addresses": host_addresses(), "host_name": host_name()}
            print(json.dumps({"ok": fingerprint is not None, "result": result}, indent=2))
            return 0 if fingerprint else 1
        try:
            result = generate_certificate(directory)
        except HostIdentityError as error:
            print(json.dumps({"ok": False, "error": error.code}))
            return 1
        result["private_key"] = str(directory / PRIVATE_KEY_NAME)
        result["restarted"] = False
        if not args.no_restart:
            # Every paired client pinned the old fingerprint; after this it must
            # be told to trust the new one explicitly.
            restart = subprocess.run(["systemctl", "--user", "restart", "omodachid.service"],
                                     capture_output=True, text=True)
            result["restarted"] = restart.returncode == 0
            if restart.returncode != 0:
                result["restart_error"] = restart.stderr.strip()[:200]
        print(json.dumps({"ok": True, "result": result}, indent=2))
        return 0
    if args.command == "ssh":
        # Local administrator operation, like `tls`: the Unix owner of the file
        # is the only authority involved. No device credential, no daemon.
        from .ssh_keys import AuthorizedKeys, SshKeyError
        keys = AuthorizedKeys(args.home.expanduser())
        try:
            if args.ssh_operation == "authorize":
                result = keys.authorize(args.pubkey, args.device)
            elif args.ssh_operation == "revoke":
                result = keys.revoke(args.device)
            else:
                from .ssh_keys import duplicate_devices
                pruned = keys.prune(args.device) if args.prune else None
                rows = keys.listing()
                result = {"keys": rows, "path": str(keys.path),
                          "duplicates": duplicate_devices(rows)}
                if pruned is not None:
                    result["pruned"] = pruned
        except SshKeyError as error:
            print(json.dumps({"ok": False, "error": error.code}))
            return 1
        print(json.dumps({"ok": True, "result": result}, indent=2))
        return 0
    if args.command == "desktop-entry":
        from . import desktop_launcher
        try:
            result = getattr(desktop_launcher, args.desktop_entry_operation)(args.home.expanduser().absolute())
        except (OSError, ValueError):
            result = {"installed": False, "scope": "user_desktop_entry", "error": "desktop_entry_unavailable"}
        ok = args.desktop_entry_operation == "status" or result.get("installed") is True
        print(json.dumps({"ok": ok, "result": result}))
        return 0 if ok else 1
    token = args.token or os.environ.get("OMODACHI_TOKEN")
    if args.command == "remote":
        if not token:
            parser.error("OMODACHI_TOKEN or --token required")
        result = asyncio.run(remote_request(args, token))
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    if args.command == "preferences":
        params = {}
        if args.preferences_operation == "set":
            changes = {key: getattr(args, key) for key in
                       ("allow_dynamic_resolution", "quality", "host_audio_playback", "pairing_mode",
                        "biometric_auth", "clipboard_sync")
                       if getattr(args, key) is not None}
            for key in ("allow_dynamic_resolution", "host_audio_playback", "biometric_auth"):
                if key in changes: changes[key] = changes[key] == "true"
            if not changes: parser.error("preferences set requires at least one preference")
            params = {"expected_revision": args.revision, "changes": changes}
        try:
            result = asyncio.run(JsonLineClient(args.socket, timeout=12).request(
                "local.preferences." + args.preferences_operation, **params))
        except (OSError, asyncio.TimeoutError):
            result = {"ok": False, "error": "setup_required", "message": "daemon_unavailable"}
        print(json.dumps(result))
        return 0 if result.get("ok") else 1
    if args.command == "auth":
        # Local administrator operations over the same verified Unix peer as
        # `pair` and `devices`. `test` deliberately speaks the exact operation
        # the root PAM helper speaks, so what it prints is what PAM would see.
        if args.auth_operation == "test":
            params = {"service": args.service, "user": os.environ.get("USER") or "",
                      "requester": os.environ.get("USER") or "", "tty": "omodachi-host auth test",
                      "timeout": args.timeout}
            operation = "local.auth.approve"
        else:
            params = {"device_id": args.device_id} if args.auth_operation == "revoke" else {}
            operation = "local.auth." + args.auth_operation
        try:
            result = asyncio.run(JsonLineClient(args.socket, timeout=args.timeout + 20
                                                if args.auth_operation == "test" else 12).request(operation, **params))
        except (OSError, asyncio.TimeoutError):
            result = {"ok": False, "error": "setup_required", "message": "daemon_unavailable"}
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    if args.command in {"theme-changed", "font-changed"}:
        # The Omarchy hook scripts run this. Local authority is the Unix peer
        # UID, exactly like pairing: no device credential exists at hook time.
        operation = "local.theme.changed" if args.command == "theme-changed" else "local.fonts.changed"
        try:
            result = asyncio.run(JsonLineClient(args.socket, timeout=12).request(operation))
        except (OSError, asyncio.TimeoutError):
            result = {"ok": False, "error": "setup_required", "message": "daemon_unavailable"}
        print(json.dumps(result))
        return 0 if result.get("ok") else 1
    if args.command in {"devices", "pair", "media-pairing"}:
        operation = getattr(args, {"devices": "device_operation", "pair": "pair_operation", "media-pairing": "media_operation"}[args.command])
        params = {field: getattr(args, field) for field in
                  ("device_id", "request_id", "attempt_id", "client_cert_sha256", "source_request_id")
                  if hasattr(args, field)}
        if args.command == "devices" and operation == "revoke":
            params["force"] = args.force
        if args.command == "devices" and operation == "list":
            params["all"] = args.all
            params["prune_ssh_keys"] = args.prune_ssh_keys
        if args.command == "devices" and operation == "purge" and args.older_than_days is not None:
            params["older_than_days"] = args.older_than_days
        if args.command == "media-pairing" and operation == "certificates":
            params["purge_unknown"] = args.purge_unknown
        if args.command == "pair" and operation == "approve":
            params["remote"] = not args.no_remote
        try:
            # Intentionally no Companion/plugin token: the daemon verifies the
            # Unix peer and keeps the sole bridge's transient PIN ownership.
            result = asyncio.run(JsonLineClient(args.socket, timeout=12).request(
                "local." + args.command + "." + operation, **params))
        except (OSError, asyncio.TimeoutError):
            result = {"ok": False, "error": "setup_required", "message": "daemon_unavailable"}
        print(json.dumps(result))
        return 0 if result.get("ok") else 1
    if args.command == "plugin-watch":
        from .plugin_bridge import watch
        # RELEASE-3b. The helper lives as long as the panel does, and `--pam`
        # moves the daemon's socket under it. Unless the user named a socket,
        # look again before every reconnect instead of trusting the path found
        # at startup.
        socket = args.socket if explicit_socket else (lambda: client_socket_path())
        async def run_bridge():
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with suppress(NotImplementedError):
                    loop.add_signal_handler(sig, stop.set)
            if token:
                await watch(socket, credential_loader=lambda: token, stop=stop)
            else:
                await watch(socket, stop=stop)
        try:
            asyncio.run(run_bridge())
        except BrokenPipeError:
            return 0
        return 0
    if args.command == "panel-summon" and not token:
        from .plugin_bridge import plugin_credential, BridgeError
        try:
            token = plugin_credential()
        except BridgeError as exc:
            print(json.dumps({"ok": False, "error": exc.code, "message": exc.code}))
            return 1
    if args.command != "health" and not token:
        parser.error("OMODACHI_TOKEN or --token required")
    if args.command == "workspace":
        import uuid
        async def workspace_request():
            client = JsonLineClient(args.socket, token)
            if args.operation == "select":
                return await client.request("workspace.select",workspace_id=args.number)
            response = await client.request("state")
            if not response.get("ok"):
                return response
            state = response["result"]
            item = next((row for row in state.get("workspace", {}).get("items", []) if row["id"] == args.number), None)
            key = "select_entry_id" if args.operation == "select" else "move_entry_id"
            if not item or not item.get(key):
                return {"ok": False, "error": "route_unavailable", "message": "workspace has no catalog action"}
            payload = {"entry_id": item[key], "request_id": uuid.uuid4().hex,
                       "catalog_revision": state["catalog"]["revision"], "params": {}}
            if args.operation == "move-focused":
                payload.update(state_revision=state["revision"], target_token=state["focus"].get("target_token"))
            return await client.request("actions.invoke", **payload)
        result = asyncio.run(workspace_request())
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    if args.command == "herdr" and getattr(args, "herdr_operation", None):
        params = ({"device": args.device} if args.herdr_operation == "sessions"
                  else {"name": args.name, "device": args.device})
        try:
            result = asyncio.run(JsonLineClient(args.socket, token).request(
                "herdr." + args.herdr_operation, **params))
        except (OSError, asyncio.TimeoutError):
            result = {"ok": False, "error": "setup_required", "message": "daemon_unavailable"}
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    op = {"panel-summon": "panel.summon"}.get(args.command, args.command)
    params = {"since": args.since} if args.command == "events" else {}
    if args.command == "panel-summon":
        params = {"view": args.view}
    try:
        result = asyncio.run(JsonLineClient(args.socket, token).request(op, **params))
    except (OSError, asyncio.TimeoutError):
        result = {"ok": False, "error": "setup_required", "message": "daemon_unavailable"}
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(daemon_main())
