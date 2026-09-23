"""Adapter boundary between the existing HTTPS listener and LAN discovery.

This module intentionally does not alter NetworkServer, authentication, pairing,
or listener startup. The owner calls start_after_listener() immediately after a
secure non-loopback listener succeeds and stop_before_listener() before close.
IPC-only/loopback/HTTP listeners remain suppressed.
"""
from __future__ import annotations
import asyncio
import ipaddress
from typing import Any

from .lan_discovery import DiscoveryError, DiscoveryConfig, ListenerDiscovery, ReadyListener

class NetworkDiscoveryBinding:
    def __init__(self, network_server, discovery: ListenerDiscovery):
        if network_server is None or not isinstance(discovery,ListenerDiscovery):
            raise DiscoveryError('discovery_binding_invalid')
        self.network_server,self.discovery=network_server,discovery
        self.started=False
        self.last_state=None

    @staticmethod
    def _site_server(network_server):
        site=getattr(network_server,'site',None)
        server=getattr(site,'_server',None)
        if server is None or not callable(getattr(server,'is_serving',None)):
            raise DiscoveryError('discovery_listener_not_ready')
        return server

    @staticmethod
    def _non_loopback(server):
        try:
            return any(not ipaddress.ip_address(sock.getsockname()[0]).is_loopback
                       for sock in server.sockets)
        except (OSError,TypeError,ValueError,AttributeError):
            raise DiscoveryError('discovery_listener_not_ready') from None

    async def start_after_listener(self):
        if self.started:
            return self.last_state
        secure=bool(getattr(self.network_server,'certificate',None)
                    and getattr(self.network_server,'private_key',None))
        server=self._site_server(self.network_server)
        if not secure or not self._non_loopback(server):
            # No publisher construction or socket side effect for IPC/HTTP/
            # loopback development listeners.
            self.last_state=await self.discovery.listener_ready(
                ReadyListener.from_server(server,secure=secure))
            self.started=True
            return self.last_state
        self.last_state=await self.discovery.listener_ready(ReadyListener.from_server(server,secure=True))
        self.started=True
        return self.last_state

    async def stop_before_listener(self):
        if not self.started:
            return self.discovery.snapshot()
        self.last_state=await self.discovery.listener_stopping()
        self.started=False
        return self.last_state

    async def close(self):
        result=await self.discovery.close()
        self.started=False;self.last_state=result
        return result


def bind_network_discovery(network_server, config: DiscoveryConfig, *, publisher_factory=None):
    kwargs={} if publisher_factory is None else {'publisher_factory':publisher_factory}
    return NetworkDiscoveryBinding(network_server,ListenerDiscovery(config,**kwargs))
