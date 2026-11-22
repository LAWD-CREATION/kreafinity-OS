"""Location of the interfaces' files: web interface, desktop bubble, Plasma add-ons."""

from __future__ import annotations

import contextlib
from importlib import resources
from pathlib import Path


def _find(packaged: str, checkout: str, marker: str) -> Path | None:
    """A folder packaged in the wheel (kreafinity/<packaged>) or in a checkout (ui/<checkout>)."""
    candidates = []
    with contextlib.suppress(Exception):
        candidates.append(Path(str(resources.files("kreafinity") / packaged)))
    candidates.append(Path(__file__).resolve().parents[3] / "ui" / checkout)
    for candidate in candidates:
        if (candidate / marker).is_file():
            return candidate
    return None


def web_root() -> Path | None:
    """Folder of the web interface: packaged in the wheel, or ``ui/web`` in a checkout."""
    return _find("web", "web", "index.html")


def bubble_script() -> Path | None:
    """The desktop bubble program (``ui/desktop/kreafinity_bubble.py``)."""
    folder = _find("desktop", "desktop", "kreafinity_bubble.py")
    return folder / "kreafinity_bubble.py" if folder else None


def pilot_banner_script() -> Path | None:
    """The pilot mode banner (``ui/desktop/kreafinity_pilot_banner.py``)."""
    folder = _find("desktop", "desktop", "kreafinity_pilot_banner.py")
    return folder / "kreafinity_pilot_banner.py" if folder else None


def plasma_assets() -> Path | None:
    """Plasma add-ons: search plugin, taskbar widget, theme (``ui/plasma`` in a checkout)."""
    return _find("plasma_assets", "plasma", "krunner/kreafinity.desktop")
