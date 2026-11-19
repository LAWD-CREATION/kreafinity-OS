"""Registry of provider adapters.

Built-in adapters live one per module in this package and register
themselves with `register_provider` when imported. Third-party packages can
add providers without touching this code by declaring an entry point in the
``kreafinity.providers`` group that points to an `LLMProvider` subclass.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from importlib.metadata import entry_points
from typing import Any, TypeVar

from kreafinity.providers.base import LLMProvider

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "kreafinity.providers"
_INTERNAL_MODULES = frozenset({"base", "registry"})

_registry: dict[str, type[LLMProvider]] = {}
_discovered = False

P = TypeVar("P", bound=type[LLMProvider])


def register_provider(cls: P) -> P:
    """Class decorator adding a provider adapter to the registry."""
    existing = _registry.get(cls.name)
    if existing is not None and existing is not cls:
        raise ValueError(f"Provider name {cls.name!r} is already registered by {existing!r}")
    _registry[cls.name] = cls
    return cls


def _discover() -> None:
    """Import built-in provider modules and load entry-point plugins, once."""
    global _discovered
    if _discovered:
        return
    _discovered = True

    package = importlib.import_module("kreafinity.providers")
    for module in pkgutil.iter_modules(package.__path__):
        if module.name in _INTERNAL_MODULES or module.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{package.__name__}.{module.name}")
        except ImportError as exc:  # optional SDK not installed
            logger.warning("Provider module %r unavailable: %s", module.name, exc)

    for entry_point in entry_points(group=ENTRY_POINT_GROUP):
        try:
            cls = entry_point.load()
        except Exception as exc:
            logger.warning("Cannot load provider plugin %r: %s", entry_point.name, exc)
            continue
        if not (isinstance(cls, type) and issubclass(cls, LLMProvider)):
            logger.warning("Provider plugin %r is not an LLMProvider subclass", entry_point.name)
            continue
        if cls.name not in _registry:
            register_provider(cls)


def available_providers() -> list[str]:
    """Return the names of all registered providers, sorted."""
    _discover()
    return sorted(_registry)


def get_provider_class(name: str) -> type[LLMProvider]:
    """Return the adapter class registered under `name`.

    Raises:
        KeyError: No provider has this name.
    """
    _discover()
    try:
        return _registry[name]
    except KeyError:
        known = ", ".join(sorted(_registry)) or "none"
        raise KeyError(f"Unknown provider {name!r} (available: {known})") from None


def create_provider(name: str, **options: Any) -> LLMProvider:
    """Instantiate the provider registered under `name` with `options`."""
    return get_provider_class(name)(**options)


def parse_model_spec(spec: str) -> tuple[str, str | None]:
    """Split ``"provider/model"`` into its parts.

    ``"ollama/llama3.1"`` gives ``("ollama", "llama3.1")`` and ``"anthropic"``
    gives ``("anthropic", None)``. Only the first ``/`` separates, so model
    names may contain slashes themselves.
    """
    provider, _, model = spec.strip().partition("/")
    if not provider:
        raise ValueError(f"Invalid provider specification: {spec!r}")
    return provider, model or None
