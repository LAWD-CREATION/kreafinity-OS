"""Helpers shared by the filesystem tools (private module, not a tool)."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from typing import Any, Literal

SortKey = Literal["name", "size", "modified"]


def entry_info(path: str, *, name: str | None = None) -> dict[str, Any] | None:
    """Describe a filesystem entry without following symlinks.

    Returns ``None`` if the entry vanished or cannot be read.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        kind = "symlink"
    elif stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
    else:
        kind = "other"
    entry: dict[str, Any] = {"name": name or os.path.basename(path), "type": kind}
    if kind == "file":
        entry["size_bytes"] = info.st_size
        entry["size"] = human_size(info.st_size)
    entry["modified"] = (
        datetime.fromtimestamp(info.st_mtime, tz=UTC).astimezone().isoformat(timespec="seconds")
    )
    entry["_mtime"] = info.st_mtime
    return entry


def sort_entries(entries: list[dict[str, Any]], key: SortKey) -> None:
    """Sort in place: by name (A→Z), size (largest first) or modified (newest first)."""
    if key == "size":
        entries.sort(key=lambda e: e.get("size_bytes", -1), reverse=True)
    elif key == "modified":
        entries.sort(key=lambda e: e["_mtime"], reverse=True)
    else:
        entries.sort(key=lambda e: (e["type"] != "directory", e["name"].casefold()))


def strip_private(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove the internal fields (prefixed with ``_``) before output."""
    return [{k: v for k, v in e.items() if not k.startswith("_")} for e in entries]


def human_size(size: float) -> str:
    """Format a size in bytes with binary units (``1.5 MiB``)."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")
