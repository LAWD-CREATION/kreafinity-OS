"""Registry of provider adapters.

Built-in adapters live one per module in this package and register
themselves with `register_provider` when imported. Third-party packages can
add providers without touching this code by declaring an entry point in the
``kreafinity.providers`` group that points to an `LLMProvider` subclass.
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar

from kreafinity.plugins import import_package_modules, load_entry_points
from kreafinity.providers.base import LLMProvider

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "kreafinity.providers"

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
    import_package_modules("kreafinity.providers", skip={"base", "registry"})
    for name, obj in load_entry_points(ENTRY_POINT_GROUP):
        if not (isinstance(obj, type) and issubclass(obj, LLMProvider)):
            logger.warning("Provider plugin %r is not an LLMProvider subclass", name)
        elif obj.name not in _registry:
            register_provider(obj)


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
