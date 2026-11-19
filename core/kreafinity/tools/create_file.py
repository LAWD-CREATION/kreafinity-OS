"""Tool: create a text file."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar

from kreafinity.i18n import t
from kreafinity.tools._fs import human_size
from kreafinity.tools.base import (
    ActionPreview,
    RiskLevel,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
)
from kreafinity.tools.registry import register_tool

PREVIEW_LINES = 5


@register_tool
class CreateFile(Tool):
    name = "create_file"
    description = (
        "Create a new text file with the given content. Never overwrites: fails if the file "
        "already exists. The parent folder must exist (use create_folder first if needed)."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File to create."},
            "content": {
                "type": "string",
                "maxLength": 1_000_000,
                "description": "Text to write (UTF-8).",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }
    risk = RiskLevel.CONFIRM

    def _target(self, arguments: dict[str, Any], context: ToolContext) -> Path:
        path = context.resolve_entry(arguments["path"])
        if os.path.lexists(path):
            raise ToolError(f"Already exists: {path}")
        if not path.parent.is_dir():
            raise ToolError(f"Folder not found: {path.parent}")
        return path

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        path = self._target(arguments, context)
        content: str = arguments["content"]
        lines = content.splitlines()
        excerpt = [f"│ {line[:100]}" for line in lines[:PREVIEW_LINES]]
        if len(lines) > PREVIEW_LINES:
            excerpt.append("│ …")
        return ActionPreview(
            summary=t("tool.create_file.summary", size=human_size(len(content.encode()))),
            details=(str(path), *excerpt),
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = self._target(arguments, context)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except OSError as exc:
            raise ToolError(f"Cannot create {path}: {exc.strerror}") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(arguments["content"])
        return ToolResult.json({"created": str(path)})
