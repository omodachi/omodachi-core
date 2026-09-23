from __future__ import annotations

"""Safe route descriptors for catalog actions.

A descriptor describes where an action lands; it is not an execution API. The
host must still resolve the catalog id and run a separately approved adapter.
"""

from dataclasses import dataclass
import shlex
from typing import Any, Callable, Mapping


TERMINAL_LAUNCHERS = {
    "omarchy-launch-floating-terminal-with-presentation",
    "omarchy-launch-or-focus-tui",
    "omarchy-launch-terminal",
}


@dataclass(frozen=True)
class RouteDescriptor:
    route: str  # host | terminal | desktop | native
    supported: bool
    argv: tuple[str, ...] = ()
    native_view: str | None = None
    reason: str | None = None
    entry_id: str | None = None
    command: str | None = None
    #: MENU-4 / Study 04 A-68. The row changes the machine in a way a stray tap
    #: must not: power and session, erasing, updating, security and boot. The
    #: client asks for a second tap before it sends the invocation.
    confirm: bool = False

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"route": self.route, "supported": self.supported}
        if self.argv:
            data["argv"] = list(self.argv)
        if self.native_view:
            data["native_view"] = self.native_view
        if self.reason:
            data["reason"] = self.reason
        if self.entry_id:
            data["entry_id"] = self.entry_id
        if self.command is not None:
            data["command"] = self.command
        if self.confirm:
            data["confirm"] = True
        return data


def _split_command(command: str) -> tuple[str, ...] | None:
    try:
        return tuple(shlex.split(command, posix=True))
    except ValueError:
        return None


def describe_route(entry_id: str, action: Any = None, *, surface: str | None = None) -> RouteDescriptor:
    """Compile an action into a host/terminal/desktop/native descriptor.

    Unknown shell expressions stay host-routed but unsupported for mobile
    invocation; no whitespace splitting or arbitrary URL/path execution occurs.
    """
    if surface and not isinstance(surface, str):
        return RouteDescriptor("host", False, entry_id=entry_id, reason="surface must be a string")
    if surface:
        if surface == "desktop":
            # Built-in surface is recognized, but an integration still decides
            # whether a desktop adapter is available.
            return RouteDescriptor("desktop", entry_id in {"omodachi.desktop"}, entry_id=entry_id,
                                   reason=None if entry_id == "omodachi.desktop" else "desktop surface is not registered")
        if surface == "terminal":
            return RouteDescriptor("terminal", entry_id in {"omodachi.agent", "omodachi.herdr"}, entry_id=entry_id,
                                   reason=None if entry_id in {"omodachi.agent", "omodachi.herdr"} else "terminal surface is not registered")
        if surface == "host":
            return RouteDescriptor("host", False, entry_id=entry_id, reason="host adapter is not registered")
        if surface.startswith("native:"):
            view = surface.split(":", 1)[1]
            return RouteDescriptor("native", False, native_view=view or None, entry_id=entry_id,
                                   reason="native view requires a registered adapter" if view else "native view is empty")
        return RouteDescriptor("host", False, entry_id=entry_id, reason=f"unknown surface: {surface}")

    if entry_id in {"omodachi.desktop", "omodachi.agent", "omodachi.herdr"}:
        return RouteDescriptor("desktop" if entry_id == "omodachi.desktop" else "terminal", True, entry_id=entry_id)
    if not isinstance(action, str) or not action.strip():
        return RouteDescriptor("host", False, entry_id=entry_id, reason="action has no executable command")
    command = action.strip()
    # Shell operators indicate a compound expression. Even when the first
    # token is a known launcher, do not turn the remainder into mobile argv.
    # A reviewed host adapter may handle it separately.
    if any(op in command for op in (";", "&&", "||", "|", "`", "$(", "\n")):
        return RouteDescriptor("host", False, entry_id=entry_id, command=command,
                               reason="complex shell action requires a reviewed host adapter")
    argv = _split_command(command)
    if argv and argv[0] in TERMINAL_LAUNCHERS:
        # Keep command argv as an audit hint. The terminal adapter decides how
        # to remove the host window wrapper; this descriptor never executes it.
        return RouteDescriptor("terminal", False, argv=argv, entry_id=entry_id, command=command,
                               reason="terminal command requires a registered adapter")
    if argv and argv[0] == "herdr":
        return RouteDescriptor("terminal", False, argv=argv, entry_id=entry_id, command=command,
                               reason="Herdr target requires an explicit registered adapter")
    # Explicit host UI launchers are retained as host actions until a native
    # mapping is registered. Complex shell chains are never made mobile-safe.
    if argv and argv[0] in {"omarchy-shell", "omarchy-menu-select", "omarchy-launch-config-editor"}:
        return RouteDescriptor("host", False, argv=argv, entry_id=entry_id, command=command,
                               reason="host UI command requires a registered adapter")
    simple = bool(argv and all(not any(ch in part for ch in ";&|$`\n") for part in argv))
    return RouteDescriptor("host", False, argv=argv if simple else (), entry_id=entry_id, command=command,
                           reason="host command requires a registered adapter" if simple else "complex shell action requires a reviewed host adapter")


def route_entry(entry: Mapping[str, Any]) -> RouteDescriptor:
    return describe_route(str(entry.get("id", "")), entry.get("action"), surface=entry.get("surface"))


def unwrap_terminal_action(action: str) -> tuple[str, ...]:
    """Decode only a reviewed launcher form, never shell substitutions/chains.

    Both ``launcher command arg`` and ``launcher 'command arg'`` occur in
    Omarchy. The returned argv removes the local host window wrapper.
    Registration is still mandatory before this argv can be returned to a client.
    """
    if not isinstance(action, str) or any(op in action for op in (";", "&", "|", "`", "$", "\n", "\r", "<", ">")):
        raise ValueError("terminal action requires a reviewed simple command")
    outer = _split_command(action)
    if not outer or outer[0] not in TERMINAL_LAUNCHERS:
        raise ValueError("unrecognized terminal wrapper")
    args = outer[1:]
    if len(args) == 1:
        args = _split_command(args[0]) or ()
    if not args or any("\x00" in arg for arg in args):
        raise ValueError("terminal command is empty or invalid")
    return tuple(args)


class RoutePolicy:
    """Closed registry: menu hints never grant execution authority.

    Each host-owned registration pins a source action and a fixed argv. Client
    parameters are accepted only from explicit finite enums; they cannot choose
    an executable, a URL, a path, a shell body, or a Herdr pane implicitly.
    """
    def __init__(self) -> None:
        self._adapters: dict[str, RouteDescriptor] = {}
        self._source_actions: dict[str, str | None] = {}
        self._parameter_enums: dict[str, dict[str, tuple[Any, ...]]] = {}
        self._reviewed_sources: dict[str, dict[str, str]] = {}
        self._availability: dict[str, Callable[[Mapping[str, Any]], str | None]] = {}
        #: MENU-4. Rows an adapter looked at and will not run, each with the
        #: source action it looked at and the reason. A row that is not here and
        #: has no adapter is one nothing has looked at.
        self._declined: dict[str, tuple[str, str]] = {}
        for entry_id, route, argv in (
            ("omodachi.desktop", "desktop", ()),
            ("omodachi.agent", "terminal", ("herdr", "agent", "attach", "default")),
            ("omodachi.herdr", "terminal", ()),
        ):
            self._adapters[entry_id] = RouteDescriptor(route, True, argv=argv, entry_id=entry_id)
            self._source_actions[entry_id] = ""
            self._parameter_enums[entry_id] = {}

    def register(self, entry_id: str, descriptor: RouteDescriptor, *,
                 source_action: str | None = None,
                 parameter_enums: Mapping[str, Any] | None = None,
                 reviewed_source: Mapping[str, str] | None = None,
                 availability: Callable[[Mapping[str, Any]], str | None] | None = None) -> None:
        if descriptor.entry_id and descriptor.entry_id != entry_id:
            raise ValueError("descriptor entry_id does not match registration")
        if descriptor.route not in {"host", "terminal", "desktop", "native"}:
            raise ValueError("invalid route")
        if descriptor.route == "native" and not descriptor.native_view:
            raise ValueError("native adapter requires native_view")
        if descriptor.route in {"terminal", "host"} and not descriptor.argv:
            raise ValueError("command adapter requires fixed argv")
        if any(not isinstance(arg, str) or "\x00" in arg for arg in descriptor.argv):
            raise ValueError("argv must contain strings without null bytes")
        if reviewed_source is not None and (set(reviewed_source) != {"action", "when", "checked", "target", "provider", "surface"}
                or any(not isinstance(value, str) for value in reviewed_source.values())):
            raise ValueError("reviewed source requires all routing/condition fields")
        if availability is not None and not callable(availability):
            raise ValueError("availability gate must be callable")
        enums = {key: tuple(values) for key, values in (parameter_enums or {}).items()}
        placeholders = {part[1:-1] for part in descriptor.argv if part.startswith("{") and part.endswith("}")}
        if placeholders != set(enums) or any(not values or len(values) > 128 for values in enums.values()):
            raise ValueError("every argv placeholder requires a finite parameter enum")
        self._adapters[entry_id] = RouteDescriptor(descriptor.route, True, argv=descriptor.argv,
            native_view=descriptor.native_view, entry_id=entry_id, command=descriptor.command,
            confirm=descriptor.confirm)
        self._declined.pop(entry_id, None)
        # For simple host commands default to a canonical fixed-argv source
        # match. Wrapped terminal/native mappings should pass their source
        # action explicitly when registering against a menu row.
        self._source_actions[entry_id] = source_action if source_action is not None else (
            descriptor.command if descriptor.command is not None else (
                shlex.join(descriptor.argv) if descriptor.route in {"host", "terminal"} else None))
        self._parameter_enums[entry_id] = enums
        if reviewed_source is None: self._reviewed_sources.pop(entry_id, None)
        else: self._reviewed_sources[entry_id] = dict(reviewed_source)
        if availability is None: self._availability.pop(entry_id, None)
        else: self._availability[entry_id] = availability

    def unregister(self, entry_id: str, *, expected: RouteDescriptor) -> bool:
        """Retire only the exact registration held by its owner."""
        if self._adapters.get(entry_id) is not expected:
            return False
        self._adapters.pop(entry_id, None)
        self._source_actions.pop(entry_id, None)
        self._parameter_enums.pop(entry_id, None)
        self._reviewed_sources.pop(entry_id, None)
        self._availability.pop(entry_id, None)
        return True

    def decline(self, entry_id: str, reason: str, *, source_action: str) -> None:
        """Say why a row has no adapter, for as long as its action is this one.

        MENU-4 §3: a row the menu-action adapter will not run (an empty action,
        a command that needs a terminal it is not given) stays grey with a
        reason a client can put into words, instead of the generic
        `route adapter is not registered`. A registration replaces it.
        """
        if not isinstance(reason, str) or not reason or not isinstance(source_action, str):
            raise ValueError("a declined row needs a reason and the action it was declined for")
        if entry_id not in self._adapters:
            self._declined[entry_id] = (source_action, reason)

    def undecline(self, entry_id: str) -> None:
        self._declined.pop(entry_id, None)

    def register_native(self, entry_id: str, view: str, *, source_action: str | None = None) -> None:
        self.register(entry_id, RouteDescriptor("native", True, native_view=view, entry_id=entry_id), source_action=source_action)

    def register_terminal(self, entry_id: str, source_action: str) -> None:
        self.register(entry_id, RouteDescriptor("terminal", True, argv=unwrap_terminal_action(source_action), entry_id=entry_id),
                      source_action=source_action)

    def resolve(self, entry: Mapping[str, Any]) -> RouteDescriptor:
        entry_id = str(entry.get("id", ""))
        discovered = route_entry(entry)
        adapter = self._adapters.get(entry_id)
        if adapter is None:
            declined = self._declined.get(entry_id)
            reason = "route adapter is not registered"
            if entry.get("kind") in {"menu", "link"} and not entry.get("surface") and not entry.get("action"):
                # MENU-4. A submenu opens on the device; there is nothing on
                # the host to route it to, and nothing is missing.
                reason = "menu_row_not_invocable"
            elif declined is not None and str(entry.get("action") or "") == declined[0]:
                reason = declined[1]
            else:
                # A keybinding row the host publishes with no dispatcher says
                # so in its own record (SHORTCUT-1); repeat that, not "no adapter".
                shortcut = entry.get("shortcut")
                if isinstance(shortcut, Mapping) and isinstance(shortcut.get("disabled_reason"), str) \
                        and shortcut["disabled_reason"]:
                    reason = shortcut["disabled_reason"]
            return RouteDescriptor(discovered.route, False, entry_id=entry_id,
                                   native_view=discovered.native_view, reason=reason)
        expected = self._source_actions.get(entry_id)
        if expected is not None and str(entry.get("action") or "") != expected:
            # Match simple argv quoting equivalently, but never execute the
            # source string. Unknown override syntax cannot change this adapter.
            actual = _split_command(str(entry.get("action") or ""))
            if not expected or actual != _split_command(expected):
                return RouteDescriptor(adapter.route, False, entry_id=entry_id, reason="menu_action_changed")
        surface = entry.get("surface")
        if surface and surface != adapter.route and surface != "native:" + (adapter.native_view or ""):
            return RouteDescriptor(adapter.route, False, entry_id=entry_id, reason="menu_surface_changed")
        reviewed = self._reviewed_sources.get(entry_id)
        if reviewed is not None and any((entry.get(key) or "") != value for key, value in reviewed.items()):
            return RouteDescriptor(adapter.route, False, entry_id=entry_id, reason="menu_source_changed")
        gate = self._availability.get(entry_id)
        if gate is not None:
            try: reason = gate(entry)
            except Exception: reason = "route_state_unavailable"
            if reason:
                return RouteDescriptor(adapter.route, False, entry_id=entry_id, reason="route_state_unavailable")
        return adapter

    def validate_invocation(self, entry: Mapping[str, Any], *, params: Mapping[str, Any] | None = None,
                            catalog_revision: str | None = None, expected_revision: str | None = None) -> tuple[bool, str | None]:
        if expected_revision is not None and catalog_revision != expected_revision:
            return False, "stale_catalog_revision"
        descriptor = self.resolve(entry)
        if not descriptor.supported:
            return False, descriptor.reason or "route_unavailable"
        values = dict(params or {})
        enums = self._parameter_enums.get(str(entry.get("id", "")), {})
        if set(values) != set(enums):
            return False, "invalid_route_parameters"
        for name, value in values.items():
            if type(value) not in {str, int, bool} or not any(type(value) is type(option) and value == option for option in enums[name]):
                return False, "invalid_route_parameters"
        return True, None

    def prepare_invocation(self, entry: Mapping[str, Any], *, params: Mapping[str, Any] | None = None,
                           catalog_revision: str | None = None, expected_revision: str | None = None) -> RouteDescriptor:
        valid, reason = self.validate_invocation(entry, params=params, catalog_revision=catalog_revision, expected_revision=expected_revision)
        if not valid:
            raise ValueError(reason)
        adapter = self.resolve(entry)
        values = dict(params or {})
        argv = tuple(str(values[part[1:-1]]) if part.startswith("{") and part.endswith("}") else part for part in adapter.argv)
        return RouteDescriptor(adapter.route, True, argv=argv, native_view=adapter.native_view, entry_id=adapter.entry_id,
                               confirm=adapter.confirm)


def resolve_route_id(entries: Any, value: str | None) -> str:
    """Resolve a menu id or alias using MenuModel's exact-id-first rules."""
    raw = str(value or "").lower().replace("_", "-")
    if not raw or raw in {"go", "menu"}:
        return "root"
    if hasattr(entries, "entries"):
        rows = entries.entries
    elif isinstance(entries, Mapping):
        rows = entries.get("entries", ())
    else:
        rows = entries or ()
    by_id = {str(row.id if hasattr(row, "id") else row.get("id")): row for row in rows}
    if raw in by_id:
        return raw
    for entry_id, row in by_id.items():
        kind = row.kind if hasattr(row, "kind") else row.get("kind")
        if kind == "app":
            continue
        aliases = row.aliases if hasattr(row, "aliases") else row.get("aliases", ())
        for alias in aliases or ():
            if str(alias).lower().replace("_", "-") == raw:
                return entry_id
    return raw
