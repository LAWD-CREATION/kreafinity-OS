"""Tool: list the contents of a folder."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, ClassVar

from kreafinity.tools._fs import SortKey, entry_info, sort_entries, strip_private
from kreafinity.tools.base import RiskLevel, Tool, ToolContext, ToolError, ToolResult
from kreafinity.tools.registry import register_tool


@register_tool
class ListDirectory(Tool):
    name = "list_directory"
    description = (
        "List the entries of a folder on the user's computer, with their type, size and "
        "modification date. Use sort_by='size' with a small limit to find the largest files "
        "of a folder. Does not recurse into subfolders: use search_files for that."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Folder to list. '~' is the home folder (default).",
            },
            "sort_by": {
                "type": "string",
                "enum": ["name", "size", "modified"],
                "description": "name (A to Z, folders first), size (largest first) or "
                "modified (newest first). Default: name.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "Maximum number of entries returned. Default: 100.",
            },
            "include_hidden": {
                "type": "boolean",
                "description": "Include hidden entries (name starting with '.'). Default: false.",
            },
        },
        "additionalProperties": False,
    }
    risk = RiskLevel.SAFE

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = context.resolve(arguments.get("path"))
        return await asyncio.to_thread(
            self._list,
            path,
            context,
            sort_by=arguments.get("sort_by", "name"),
            limit=arguments.get("limit", 100),
            include_hidden=arguments.get("include_hidden", False),
        )

    @staticmethod
    def _list(
        path: Path, context: ToolContext, *, sort_by: SortKey, limit: int, include_hidden: bool
    ) -> ToolResult:
        if not path.exists():
            raise ToolError(f"Folder not found: {path}")
        if not path.is_dir():
            raise ToolError(f"Not a folder: {path}")
        try:
            names = os.listdir(path)
        except PermissionError as exc:
            raise ToolError(f"Permission denied: {path}") from exc

        entries = []
        for name in names:
            if name.startswith(".") and not include_hidden:
                continue
            full = path / name  # symlinks are listed, never followed
            if not context.is_allowed(full):
                continue
            entry = entry_info(str(full), name=name)
            if entry is not None:
                entries.append(entry)
        sort_entries(entries, sort_by)
        return ToolResult.json(
            {
                "path": str(path),
                "total_entries": len(entries),
                "truncated": len(entries) > limit,
                "entries": strip_private(entries[:limit]),
            }
        )
