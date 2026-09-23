# LAN discovery

Omodachi advertises the host over DNS-SD so a companion device can find it on
the same LAN without typing an address. Discovery is deliberately thin: it
publishes public network metadata only, and it never establishes authorization
or TLS trust.

## Public descriptor

DNS-SD type is `_omodachi._tcp.local.`. The configured human-readable instance
name is a bounded DNS label; the configured target is a `.local.` hostname.
Port is taken from the actual ready listener's bound sockets, not a requested
port0 or a hard-coded default. Explicit configured interface addresses are
filtered to the socket's actual bound addresses/families. A wildcard listener
requires explicit actual LAN interface addresses from installation/configuration;
this module does not guess an address from hostname resolution or advertise every
interface automatically. Invalid, unspecified, multicast, loopback and reserved
advertisement addresses are rejected. Scoped IPv6 link-local support is not
implemented; it is explicitly rejected rather than dropping the scope.

A public descriptor contains only:

```json
{
  "service_type":"_omodachi._tcp.local.",
  "name":"omarchy",
  "server":"omarchy.local.",
  "port":8099,
  "addresses":["192.168.1.10"],
  "scheme":"https",
  "path":"/",
  "contract_revision":"omodachi.v1",
  "authorization_required":true,
  "trust_state":"unverified",
  "pairing_state":"unknown",
  "host_id":"…32 hex…",
  "host_name":"omarchy",
  "fingerprint_prefix":"…16 hex…",
  "origin_hint":"https://omarchy.local:8099"
}
```

The TXT allowlist is `txtvers=1`, `v=<contract revision>`, `scheme=https`,
`path=/`, `auth=required`, `port=<bound port>`, plus `host_id`, `host_name` and
`fp` when the host identity is installed. It contains no token, invitation, PIN,
device registry, SSH detail or media readiness.

`fp` is the first 16 hex of the certificate fingerprint: a label for telling two
hosts apart in a browse list, never the value a client pins. The full
fingerprint comes from `GET /health`, and only a completed pairing claim turns
it into trust (see [pairing.md](pairing.md)). The name/addresses/port are
ordinary public network metadata. A malicious LAN peer can spoof all of it;
this descriptor never establishes authorization or TLS trust and does not
trigger pairing.

## Listener-owner lifecycle contract

The future ordinary network owner will construct one lifecycle for its configured
name/hostname/LAN addresses. Construction/import does not open multicast sockets.
After its existing HTTPS setup and listener start succeed, it supplies actual
bound sockets:

```python
from omodachi_core.lan_discovery import DiscoveryConfig, ListenerDiscovery, ReadyListener

announcement = ListenerDiscovery(DiscoveryConfig(
    display_name=configured_public_name,
    server_name=configured_local_hostname,
    interface_addresses=tuple(configured_actual_lan_addresses),
))

# Existing listener startup remains owned by NetworkServer. Only after success:
facts = ReadyListener.from_server(listener_server, secure=actual_tls_enabled)
state = await announcement.listener_ready(facts)

# On shutdown/rebind, withdraw before the existing listener stops:
await announcement.listener_stopping()
# ... stop listener ...
await announcement.close()
```

`listener_server` must be the already-started asyncio server; its is_serving()
state and actual TCP sockets are checked. The current NetworkServer exposes it
through TCPSite after start and also exposes bound_port; a later small owner hook can pass these facts. TLS truth is from the
owner's successful SSL context/listener setup, not an mDNS client field. Unready,
plain HTTP, loopback-only, mismatched address-family or disabled discovery never
registers. Loopback development servers remain non-advertised.

Repeated identical readiness is idempotent. Rebind/port changes first withdraw
and close the old publisher, then register the new descriptor. Stop always
attempts a goodbye and closes the publisher. A failed withdrawal is reported as
`withdrawal_unconfirmed`, including repeated close, rather than claiming remote
caches were cleared. Registration failures are safe availability codes and do not
change the HTTP listener, credentials or pairing state. Cancellation also attempts
owned withdrawal. Register/unregister/close each have an explicit bounded timeout
(default5seconds per operation); a timed-out registration cannot stay ready. TTL/cache removal after a failed goodbye is not proven.

## Optional dependency and installation

The concrete adapter uses `python-zeroconf`, pinned for this source interface as
`zeroconf==0.151.3` (Python>=3.10; core already requires>=3.11). Its implementation
was checked against the primary project source at tag0.151.3. The async register
and unregister calls return a second awaitable for announcements/goodbyes; this
adapter waits for both completion stages. Name conflicts are reported; it does
not silently rename the configured host.

`zeroconf` is now an ordinary package dependency, so `install_host.py` puts it
in the host virtualenv along with aiohttp. A missing dependency still produces
`discovery_dependency_unavailable` rather than failing the listener. The adapter opens sockets only when register
is called, and `NetworkServer` creates no publisher unless a caller injects a
`DiscoveryConfig` and binding. Nothing advertises before a non-loopback HTTPS
listener exists.

## Client expectations

`omodachid` derives the advertisement from the host itself: with `--listen` on
a non-loopback address it advertises the hostname, `<hostname>.local.` and the
host's current non-loopback IPv4 addresses, carrying `host_id`, `host_name` and
`fp`. `--discovery off` disables it; the explicit `--discovery-name`,
`--discovery-server` and `--discovery-address` flags remain as an operator
override. Whether a record is actually published is still decided by the real
bound sockets, so a loopback development listener never advertises.

Verify from the host with `avahi-browse -rt _omodachi._tcp`, or from a Mac with
`dns-sd -B _omodachi._tcp` and `dns-sd -L <name> _omodachi._tcp`.

A companion browses `_omodachi._tcp`, resolves the service target and port and
shows the public name plus the unverified origin hint. Resolve and withdraw
updates should update the list, and manual HTTPS-address entry must stay
available. Discovery never saves a credential, suppresses a certificate error,
pairs automatically or establishes SSH or media trust. Local-network permission
denial and recovery belong to the client.
