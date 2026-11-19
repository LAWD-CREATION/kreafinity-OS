"""Registry of tools.

Built-in tools live one per module in this package and register themselves
with `register_tool`. Third-party packages can add tools through an entry
point in the ``kreafinity.tools`` group pointing to a `Tool` subclass.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from typing import TypeVar

from kreafinity.plugins import import_package_modules, load_entry_points
from kreafinity.providers.base import ToolSpec
from kreafinity.tools.base import Tool

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "kreafinity.tools"

_classes: dict[str, type[Tool]] = {}
_discovered = False

T = TypeVar("T", bound=type[Tool])


def register_tool(cls: T) -> T:
    """Class decorator adding a tool to the registry."""
    existing = _classes.get(cls.name)
    if existing is not None and existing is not cls:
        raise ValueError(f"Tool name {cls.name!r} is already registered by {existing!r}")
    _classes[cls.name] = cls
    return cls


def _discover() -> None:
    global _discovered
    if _discovered:
        return
    _discovered = True
    import_package_modules("kreafinity.tools", skip={"base", "registry"})
    for name, obj in load_entry_points(ENTRY_POINT_GROUP):
        if not (isinstance(obj, type) and issubclass(obj, Tool)):
            logger.warning("Tool plugin %r is not a Tool subclass", name)
        elif obj.name not in _classes:
            register_tool(obj)


def available_tools() -> list[str]:
    """Names of all registered tools, sorted."""
    _discover()
    return sorted(_classes)


class ToolRegistry:
    """The set of tools enabled for an agent."""

    def __init__(self, tools: Iterable[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    @classmethod
    def load(cls, enabled: Iterable[str] | None = None) -> ToolRegistry:
        """Instantiate the registered tools, or only those named in `enabled`.

        Unknown names are ignored with a warning (e.g. a plugin uninstalled).
        """
        _discover()
        names = sorted(_classes) if enabled is None else list(enabled)
        tools: list[Tool] = []
        for name in names:
            tool_class = _classes.get(name)
            if tool_class is None:
                logger.warning("Unknown tool %r ignored", name)
            else:
                tools.append(tool_class())
        return cls(tools)

    def get(self, name: str) -> Tool | None:
        """The tool called `name`, if enabled."""
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        """Descriptions of the enabled tools, for the LLM provider."""
        return [tool.spec() for tool in self._tools.values()]

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)
