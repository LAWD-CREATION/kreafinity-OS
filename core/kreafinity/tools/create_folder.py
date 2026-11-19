"""Tool: create a folder."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar

from kreafinity.i18n import t
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
class CreateFolder(Tool):
    name = "create_folder"
    description = "Create a new folder (missing parent folders are created too)."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Folder to create."}},
        "required": ["path"],
        "additionalProperties": False,
    }
    risk = RiskLevel.CONFIRM

    def _target(self, arguments: dict[str, Any], context: ToolContext) -> Path:
        path = context.resolve_entry(arguments["path"])
        if os.path.lexists(path):
            raise ToolError(f"Already exists: {path}")
        return path

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        path = self._target(arguments, context)
        return ActionPreview(summary=t("tool.create_folder.summary"), details=(str(path),))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = self._target(arguments, context)
        try:
            path.mkdir(parents=True)
        except OSError as exc:
            raise ToolError(f"Cannot create {path}: {exc.strerror}") from exc
        return ToolResult.json({"created": str(path)})
