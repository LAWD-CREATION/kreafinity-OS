"""Tool: move or rename a file or folder."""

from __future__ import annotations

import errno
import os
import shutil
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
class MoveFile(Tool):
    name = "move_file"
    description = (
        "Move or rename a file or folder. If the destination is an existing folder, the "
        "item is moved into it. Never overwrites an existing file."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "File or folder to move."},
            "destination": {
                "type": "string",
                "description": "New path, or existing folder to move the item into.",
            },
        },
        "required": ["source", "destination"],
        "additionalProperties": False,
    }
    risk = RiskLevel.CONFIRM

    def _plan(self, arguments: dict[str, Any], context: ToolContext) -> tuple[Path, Path]:
        source = context.resolve_entry(arguments["source"])
        if not os.path.lexists(source):
            raise ToolError(f"Not found: {source}")
        if context.is_protected(source):
            raise ToolError(f"{source} is a protected folder and cannot be moved")

        destination = context.resolve_entry(arguments["destination"])
        if destination.is_dir():  # existing folder: move inside it
            destination = context.resolve(arguments["destination"]) / source.name
            context.resolve_entry(str(destination))
        if destination == source:
            raise ToolError("Source and destination are the same")
        if os.path.lexists(destination):
            raise ToolError(f"Already exists: {destination} (nothing is overwritten)")
        if not destination.parent.is_dir():
            raise ToolError(f"Folder not found: {destination.parent}")
        if source.is_dir() and not source.is_symlink() and destination.is_relative_to(source):
            raise ToolError("A folder cannot be moved into itself")
        return source, destination

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        source, destination = self._plan(arguments, context)
        key = (
            "tool.move_file.rename"
            if source.parent == destination.parent
            else "tool.move_file.move"
        )
        return ActionPreview(summary=t(key), details=(f"{source} → {destination}",))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        source, destination = self._plan(arguments, context)
        try:
            os.rename(source, destination)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise ToolError(f"Cannot move {source}: {exc.strerror}") from exc
            try:  # another disk: copy then remove the original
                shutil.move(source, destination)
            except OSError as move_exc:
                raise ToolError(f"Cannot move {source}: {move_exc}") from move_exc
        return ToolResult.json({"moved": str(source), "to": str(destination)})
