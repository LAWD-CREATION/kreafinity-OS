"""Tool: open a file or folder with the default application."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

from kreafinity.i18n import t
from kreafinity.system import desktop
from kreafinity.tools.base import (
    ActionPreview,
    RiskLevel,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
)
from kreafinity.tools.registry import register_tool


@register_tool
class OpenFile(Tool):
    name = "open_file"
    description = (
        "Open a file with its default application (document viewer, image viewer, media "
        "player…), or a folder in the file manager."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File or folder to open."}},
        "required": ["path"],
        "additionalProperties": False,
    }
    risk = RiskLevel.CONFIRM

    def _target(self, arguments: dict[str, Any], context: ToolContext) -> Path:
        path = context.resolve(arguments["path"])
        if not path.exists():
            raise ToolError(f"Not found: {path}")
        return path

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        path = self._target(arguments, context)
        key = "tool.open_file.folder" if path.is_dir() else "tool.open_file.summary"
        return ActionPreview(summary=t(key), details=(str(path),))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = self._target(arguments, context)
        try:
            await asyncio.to_thread(desktop.open_with_default_app, str(path))
        except desktop.LaunchError as exc:
            raise ToolError(f"Cannot open {path}: {exc}") from exc
        return ToolResult.json({"opened": str(path)})
