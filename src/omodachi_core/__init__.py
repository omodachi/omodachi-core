"""Omodachi core: device-level hub primitives independent of Sunshine."""
from .hub import Hub
from .auth import DeviceAuthenticator
from .ipc import JsonLineServer, JsonLineClient
from .remote import RemoteError, RemoteManager, RemoteSession

__all__ = ["Hub", "DeviceAuthenticator", "JsonLineServer", "JsonLineClient",
           "RemoteError", "RemoteManager", "RemoteSession"]

from .catalog import Catalog, CatalogEntry, CatalogError, JsoncError, compile_catalog, compile_catalog_from_jsonc, loads_jsonc, load_jsonc, parse_script_annotations, validate_action_params
from .catalog_runtime import CatalogRuntime
from .routes import RouteDescriptor, RoutePolicy, describe_route, resolve_route_id, route_entry, unwrap_terminal_action

__all__ += ["Catalog", "CatalogEntry", "CatalogError", "JsoncError", "CatalogRuntime", "compile_catalog", "compile_catalog_from_jsonc", "loads_jsonc", "load_jsonc", "parse_script_annotations", "validate_action_params", "RouteDescriptor", "RoutePolicy", "describe_route", "resolve_route_id", "route_entry", "unwrap_terminal_action"]

from .agent import AgentState, AgentStatus, AgentTarget, DefaultAgentCapabilities, HerdrStatusSnapshot, ProbeStatus, ReadOnlyAgentProbe, ReadOnlyProbeCommands, ProbeOutput, build_default_agent_capabilities
from .protocol import CONTRACT_REVISION
__all__ += ["AgentState", "AgentStatus", "AgentTarget", "DefaultAgentCapabilities", "HerdrStatusSnapshot", "ProbeStatus", "ReadOnlyAgentProbe", "ReadOnlyProbeCommands", "ProbeOutput", "build_default_agent_capabilities", "CONTRACT_REVISION"]
from .catalog_runtime import CatalogRuntime
from .routes import unwrap_terminal_action
__all__ += ["CatalogRuntime", "unwrap_terminal_action"]
