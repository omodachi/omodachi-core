"""Optional public LAN discovery, separate from authorization and TLS trust.

No network side effects at import/construction. The listener owner supplies
actual bound sockets AFTER successful HTTPS startup, and closes this lifecycle
BEFORE listener shutdown. No service/CLI hook is installed by this module.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
import inspect
import ipaddress
import re
import socket
from typing import Protocol

from .protocol import CONTRACT_REVISION

SERVICE_TYPE = '_omodachi._tcp.local.'
ZEROCONF_REQUIREMENT = 'zeroconf==0.151.3'

class DiscoveryError(ValueError):
    def __init__(self, code):self.code=code;super().__init__(code)


def _address(value):
    try:
        if not isinstance(value,str) or '%' in value:raise ValueError()
        address=ipaddress.ip_address(value)
    except ValueError:raise DiscoveryError('discovery_address_invalid') from None
    if address.is_unspecified or address.is_multicast or address.is_loopback or address.is_reserved:
        raise DiscoveryError('discovery_address_not_lan_unicast')
    if address.version==6 and address.is_link_local:
        # Scope/interface-index aware IPv6 discovery is not silently guessed.
        raise DiscoveryError('discovery_ipv6_scope_required')
    return address

@dataclass(frozen=True)
class DiscoveryConfig:
    display_name: str
    server_name: str
    interface_addresses: tuple[str,...]
    enabled: bool = True
    # Identity a browsing client shows and matches; `fingerprint` is the full
    # certificate fingerprint, of which only a 16-hex prefix is advertised.
    host_id: str|None = None
    host_name: str|None = None
    fingerprint: str|None = None
    def __post_init__(self):
        if type(self.enabled) is not bool:raise DiscoveryError('discovery_config_invalid')
        if self.host_id is not None and not re.fullmatch(r'[0-9a-f]{32}',self.host_id):
            raise DiscoveryError('discovery_host_id_invalid')
        if self.fingerprint is not None and not re.fullmatch(r'[0-9a-f]{64}',self.fingerprint):
            raise DiscoveryError('discovery_fingerprint_invalid')
        if self.host_name is not None and (not isinstance(self.host_name,str)
                or not 1<=len(self.host_name.encode('utf-8'))<=63
                or any(ord(c)<32 or ord(c)==127 for c in self.host_name)):
            raise DiscoveryError('discovery_host_name_invalid')
        if (not isinstance(self.display_name,str) or not 1<=len(self.display_name.encode('utf-8'))<=63
                or self.display_name.strip()!=self.display_name
                or any(ord(c)<32 or ord(c)==127 or c in '.\\' for c in self.display_name)):
            raise DiscoveryError('discovery_name_invalid')
        if (not isinstance(self.server_name,str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.local\.',self.server_name)):
            raise DiscoveryError('discovery_server_name_invalid')
        if (not isinstance(self.interface_addresses,tuple) or not 1<=len(self.interface_addresses)<=16
                or any(not isinstance(item,str) for item in self.interface_addresses)
                or len(set(self.interface_addresses))!=len(self.interface_addresses)):
            raise DiscoveryError('discovery_interfaces_invalid')
        for address in self.interface_addresses:_address(address)

@dataclass(frozen=True)
class ReadyListener:
    """Transport observation, not a certificate/pairing or media assertion."""
    secure: bool
    endpoints: tuple[tuple[str,int],...]
    def __post_init__(self):
        if (type(self.secure) is not bool or not isinstance(self.endpoints,tuple) or not 1<=len(self.endpoints)<=16):
            raise DiscoveryError('discovery_listener_not_ready')
        for row in self.endpoints:
            if (not isinstance(row,tuple) or len(row)!=2 or not isinstance(row[0],str)
                    or type(row[1]) is not int or not 1<=row[1]<=65535):
                raise DiscoveryError('discovery_listener_invalid')
            try:ipaddress.ip_address(row[0])
            except ValueError:raise DiscoveryError('discovery_listener_invalid') from None
        if len({port for _,port in self.endpoints})!=1:raise DiscoveryError('discovery_listener_not_ready')
    @classmethod
    def from_server(cls,server,*,secure):
        if type(secure) is not bool:raise DiscoveryError('discovery_listener_invalid')
        endpoints=[]
        try:
            if server.is_serving() is not True:raise ValueError()
            for sock in server.sockets:
                if sock.getsockopt(socket.SOL_SOCKET,socket.SO_TYPE)!=socket.SOCK_STREAM:raise ValueError()
                value=sock.getsockname()
                address=str(ipaddress.ip_address(value[0]))
                port=value[1]
                if type(port) is not int or not 1<=port<=65535:raise ValueError()
                endpoints.append((address,port))
        except (OSError,TypeError,ValueError,IndexError,AttributeError):
            raise DiscoveryError('discovery_listener_not_ready') from None
        if not endpoints or len(endpoints)>16 or len({port for _,port in endpoints})!=1:
            raise DiscoveryError('discovery_listener_not_ready')
        return cls(secure,tuple(endpoints))

@dataclass(frozen=True)
class PublicServiceDescriptor:
    name: str
    server: str
    port: int
    addresses: tuple[str,...]
    host_id: str|None = None
    host_name: str|None = None
    fingerprint_prefix: str|None = None
    def to_dict(self):
        # Deliberate closed projection: no arbitrary metadata, auth material,
        # certificate identifiers, device registry or media readiness is copied.
        return {'service_type':SERVICE_TYPE,'name':self.name,'server':self.server,
            'port':self.port,'addresses':list(self.addresses),'scheme':'https','path':'/',
            'contract_revision':CONTRACT_REVISION,'authorization_required':True,
            'trust_state':'unverified','pairing_state':'unknown',
            'host_id':self.host_id,'host_name':self.host_name,
            'fingerprint_prefix':self.fingerprint_prefix,
            'origin_hint':f'https://{self.server.rstrip(".")}:{self.port}'}
    def txt(self):
        # A browsing client needs enough to name the host and tell two hosts
        # apart in a list. `fp` is a 16-hex prefix: it is a label, never the
        # pinned value. The full fingerprint comes from /health, and only a
        # completed pairing claim turns it into trust.
        value={b'txtvers':b'1',b'v':CONTRACT_REVISION.encode('ascii'),b'scheme':b'https',
               b'path':b'/',b'auth':b'required',b'port':str(self.port).encode('ascii')}
        if self.host_id is not None:value[b'host_id']=self.host_id.encode('ascii')
        if self.host_name is not None:value[b'host_name']=self.host_name.encode('utf-8')
        if self.fingerprint_prefix is not None:value[b'fp']=self.fingerprint_prefix.encode('ascii')
        return value


def descriptor_for(config,listener):
    if not isinstance(config,DiscoveryConfig) or not isinstance(listener,ReadyListener):
        raise DiscoveryError('discovery_listener_invalid')
    if not config.enabled:raise DiscoveryError('discovery_disabled')
    if listener.secure is not True:raise DiscoveryError('discovery_https_required')
    if not listener.endpoints:raise DiscoveryError('discovery_listener_not_ready')
    ports={port for _,port in listener.endpoints}
    if len(ports)!=1 or any(type(port) is not int or not 1<=port<=65535 for port in ports):
        raise DiscoveryError('discovery_listener_not_ready')
    matched=[]
    try:bound=[ipaddress.ip_address(address) for address,_ in listener.endpoints]
    except ValueError:raise DiscoveryError('discovery_listener_invalid') from None
    for configured in config.interface_addresses:
        address=_address(configured)
        if any(address==endpoint or endpoint.is_unspecified and endpoint.version==address.version for endpoint in bound):
            matched.append(str(address))
    if not matched:raise DiscoveryError('discovery_no_lan_listener')
    return PublicServiceDescriptor(config.display_name,config.server_name,next(iter(ports)),tuple(matched),
        host_id=config.host_id,host_name=config.host_name,
        fingerprint_prefix=config.fingerprint[:16] if config.fingerprint else None)

class Publisher(Protocol):
    async def register(self,descriptor:PublicServiceDescriptor):...
    async def unregister(self):...
    async def close(self):...

class ZeroconfPublisher:
    """Optional python-zeroconf adapter; creates sockets only on register()."""
    def __init__(self):self._zc=None;self._info=None
    @staticmethod
    def _load():
        try:
            from zeroconf import ServiceInfo
            from zeroconf.asyncio import AsyncZeroconf
        except ImportError:raise DiscoveryError('discovery_dependency_unavailable') from None
        return ServiceInfo,AsyncZeroconf
    @staticmethod
    async def _completed(operation):
        value=await operation
        # The pinned asyncio registration API returns a second awaitable for
        # the actual announcements/goodbyes. Await both, not only scheduling.
        if inspect.isawaitable(value):await value
    async def register(self,descriptor):
        ServiceInfo,AsyncZeroconf=self._load()
        self._zc=AsyncZeroconf(interfaces=list(descriptor.addresses))
        self._info=ServiceInfo(SERVICE_TYPE,descriptor.name+'.'+SERVICE_TYPE,
            addresses=[ipaddress.ip_address(address).packed for address in descriptor.addresses],
            port=descriptor.port,properties=descriptor.txt(),server=descriptor.server)
        # No implicit name rename: the configured name either registers or a
        # conflict is reported. Discovery never edits listener settings/trust.
        await self._completed(self._zc.async_register_service(self._info,allow_name_change=False))
    async def unregister(self):
        if self._zc is not None and self._info is not None:
            await self._completed(self._zc.async_unregister_service(self._info))
            self._info=None
    async def close(self):
        if self._zc is not None:
            await self._zc.async_close()
            self._zc=None;self._info=None

class ListenerDiscovery:
    """Serialized announce/withdraw after listener readiness; failures stay visible."""
    def __init__(self,config,*,publisher_factory=ZeroconfPublisher,operation_timeout=5.0):
        if not isinstance(config,DiscoveryConfig):raise DiscoveryError('discovery_config_invalid')
        if type(operation_timeout) not in {int,float} or not 0<operation_timeout<=10:
            raise DiscoveryError('discovery_timeout_invalid')
        self.config,self.factory=config,publisher_factory
        self.operation_timeout=operation_timeout
        self._publisher=None;self._descriptor=None;self._lock=asyncio.Lock()
        self._status='idle';self._reason='listener_not_ready';self._closed=False
    def snapshot(self):
        return {'status':self._status,'reason':self._reason,'advertised':self._status=='advertised',
            'discovery_is_authorization':False,
            'service':self._descriptor.to_dict() if self._descriptor is not None else None}
    async def _withdraw(self):
        if self._publisher is None:return self._descriptor is None
        publisher=self._publisher;unregistered=False;closed=False
        try:await asyncio.wait_for(publisher.unregister(),self.operation_timeout);unregistered=True
        except Exception:pass
        try:await asyncio.wait_for(publisher.close(),self.operation_timeout);closed=True
        except Exception:pass
        if closed:self._publisher=None
        if unregistered:
            self._descriptor=None
            return closed
        # Closing sockets without a confirmed goodbye is not proof that remote
        # caches removed the record. Keep this failure explicit.
        return False
    async def listener_ready(self,listener):
        async with self._lock:
            if self._closed:raise DiscoveryError('discovery_lifecycle_closed')
            try:descriptor=descriptor_for(self.config,listener)
            except DiscoveryError as error:
                clean=await self._withdraw()
                self._status='suppressed' if clean else 'withdrawal_unconfirmed'
                self._reason=error.code if clean else 'discovery_withdrawal_unconfirmed'
                return self.snapshot()
            if self._descriptor==descriptor and self._status=='advertised':return self.snapshot()
            if not await self._withdraw():
                self._status='withdrawal_unconfirmed';self._reason='discovery_withdrawal_unconfirmed'
                return self.snapshot()
            try:publisher=self.factory()
            except Exception:
                self._status='unavailable';self._reason='discovery_publisher_unavailable'
                return self.snapshot()
            self._publisher=publisher;self._descriptor=descriptor
            try:await asyncio.wait_for(publisher.register(descriptor),self.operation_timeout)
            except asyncio.CancelledError:
                clean=await self._withdraw()
                self._status='stopped' if clean else 'withdrawal_unconfirmed'
                self._reason='registration_cancelled' if clean else 'discovery_withdrawal_unconfirmed'
                raise
            except Exception as error:
                reason=(error.code if isinstance(error,DiscoveryError) else 'discovery_registration_timeout'
                        if isinstance(error,asyncio.TimeoutError) else 'discovery_registration_failed')
                if not isinstance(reason,str) or not re.fullmatch(r'discovery_[a-z0-9_]{1,85}',reason):
                    reason='discovery_registration_failed'
                clean=await self._withdraw()
                self._status='unavailable' if clean else 'withdrawal_unconfirmed'
                self._reason=reason if clean else 'discovery_withdrawal_unconfirmed'
                return self.snapshot()
            self._status='advertised';self._reason='ready'
            return self.snapshot()
    async def listener_stopping(self):
        async with self._lock:
            clean=await self._withdraw()
            self._status='stopped' if clean else 'withdrawal_unconfirmed'
            self._reason='listener_stopped' if clean else 'discovery_withdrawal_unconfirmed'
            return self.snapshot()
    async def close(self):
        async with self._lock:
            self._closed=True
            clean=await self._withdraw()
            self._status='stopped' if clean else 'withdrawal_unconfirmed'
            self._reason='listener_stopped' if clean else 'discovery_withdrawal_unconfirmed'
            return self.snapshot()
