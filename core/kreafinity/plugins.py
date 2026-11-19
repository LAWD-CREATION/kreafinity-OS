"""Discovery of built-in modules and third-party plugins.

Used by the provider and tool registries: every module of a package is
imported (each one registers what it defines), then the objects published
under an entry-point group by other installed packages are loaded.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Collection
from importlib.metadata import entry_points

logger = logging.getLogger(__name__)


def import_package_modules(package_name: str, *, skip: Collection[str] = ()) -> None:
    """Import every module of `package_name`, except `skip` and private ones.

    A module whose optional dependency is missing is skipped with a warning.
    """
    package = importlib.import_module(package_name)
    for module in pkgutil.iter_modules(package.__path__):
        if module.name in skip or module.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{package_name}.{module.name}")
        except ImportError as exc:
            logger.warning("Module %s.%s unavailable: %s", package_name, module.name, exc)


def load_entry_points(group: str) -> list[tuple[str, object]]:
    """Load the objects published under the entry-point `group`.

    Plugins that fail to load are skipped with a warning.
    """
    loaded: list[tuple[str, object]] = []
    for entry_point in entry_points(group=group):
        try:
            loaded.append((entry_point.name, entry_point.load()))
        except Exception as exc:
            logger.warning("Cannot load plugin %r from %r: %s", entry_point.name, group, exc)
    return loaded
