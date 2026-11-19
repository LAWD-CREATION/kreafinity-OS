"""Tool: move files and folders to the trash (never deletes)."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any, ClassVar

from kreafinity.i18n import t
from kreafinity.system.trash import TrashError, move_to_trash
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

# Folder sizes shown for approval are computed up to this many entries.
MAX_ENTRIES_FOR_SIZE = 20_000


@register_tool
class MoveToTrash(Tool):
    name = "move_to_trash"
    description = (
        "Move files or folders to the trash. This is the only way to delete: items can be "
        "restored from the trash by the user. Give the exact paths (find them first with "
        "list_directory or search_files)."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 100,
                "description": "Files or folders to move to the trash.",
            }
        },
        "required": ["paths"],
        "additionalProperties": False,
    }
    risk = RiskLevel.DANGER

    def _plan(self, arguments: dict[str, Any], context: ToolContext) -> list[Path]:
        paths: list[Path] = []
        for raw in arguments["paths"]:
            path = context.resolve_entry(raw)
            if not os.path.lexists(path):
                raise ToolError(f"Not found: {path}")
            if context.is_protected(path):
                raise ToolError(f"{path} is a protected folder and cannot be trashed")
            paths.append(path)
        # Drop duplicates and items inside a folder that is trashed anyway.
        selected: list[Path] = []
        for path in sorted(set(paths)):
            if not any(path.is_relative_to(parent) for parent in selected):
                selected.append(path)
        return selected

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        paths = self._plan(arguments, context)
        details = await asyncio.to_thread(lambda: tuple(_describe(p) for p in paths))
        return ActionPreview(
            summary=t("tool.move_to_trash.summary", count=len(paths)), details=details
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        paths = self._plan(arguments, context)
        trashed: list[str] = []
        failed: list[dict[str, str]] = []
        for path in paths:
            try:
                await asyncio.to_thread(move_to_trash, path)
            except TrashError as exc:
                failed.append({"path": str(path), "error": str(exc)})
            else:
                trashed.append(str(path))
        return ToolResult.json({"trashed": trashed, "failed": failed}, is_error=not trashed)


def _describe(path: Path) -> str:
    if path.is_symlink():
        return f"{path} ({t('tool.size.symlink')})"
    if not path.is_dir():
        return f"{path} ({human_size(path.lstat().st_size)})"
    total, count = 0, 0
    for current, _dirs, files in os.walk(path):
        for name in files:
            count += 1
            if count > MAX_ENTRIES_FOR_SIZE:
                return f"{path} ({t('tool.size.folder_large', count=MAX_ENTRIES_FOR_SIZE)})"
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(current, name)).st_size
    return f"{path} ({t('tool.size.folder', size=human_size(total), count=count)})"
