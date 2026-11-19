"""Journal of every tool call (``~/.local/share/kreafinity/actions.log``).

One JSON object per line. Tool outputs are not recorded, only what was
asked, by which model, and how it ended.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from kreafinity.paths import data_dir

ActionStatus = Literal["ok", "error", "denied", "skipped"]


def default_action_log_path() -> Path:
    """Location of the action log."""
    return data_dir() / "actions.log"


class ActionLog:
    """Append-only log of tool calls, readable only by the user."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_action_log_path()

    def record(
        self,
        *,
        tool: str,
        risk: str | None,
        arguments: dict[str, Any],
        status: ActionStatus,
        detail: str | None = None,
        model: str | None = None,
    ) -> None:
        """Append one entry to the log."""
        entry = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "tool": tool,
            "risk": risk,
            "arguments": arguments,
            "status": status,
            "detail": detail,
            "model": model,
        }
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
