"""Location of the web interface's files."""

from __future__ import annotations

import contextlib
from importlib import resources
from pathlib import Path


def web_root() -> Path | None:
    """Folder of the web interface: packaged in the wheel, or ``ui/web`` in a checkout."""
    candidates = []
    with contextlib.suppress(Exception):
        candidates.append(Path(str(resources.files("kreafinity") / "web")))
    candidates.append(Path(__file__).resolve().parents[3] / "ui" / "web")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None
