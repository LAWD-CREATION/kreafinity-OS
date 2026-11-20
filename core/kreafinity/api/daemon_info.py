"""Where the running daemon listens (a small file written by the daemon)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kreafinity.paths import data_dir

#: The daemon only ever listens on the loopback interface.
HOST = "127.0.0.1"


def daemon_info_path() -> Path:
    """File telling clients where the running daemon listens."""
    return data_dir() / "daemon.json"


def read_daemon_info() -> dict[str, Any] | None:
    """Where the running daemon listens (``url``, ``port``, ``pid``), if it runs."""
    try:
        info = json.loads(daemon_info_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None
