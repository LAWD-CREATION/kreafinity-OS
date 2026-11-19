"""Tool: search files by name, recursively."""

from __future__ import annotations

import asyncio
import fnmatch
import os
from pathlib import Path
from typing import Any, ClassVar

from kreafinity.tools._fs import SortKey, entry_info, sort_entries, strip_private
from kreafinity.tools.base import RiskLevel, Tool, ToolContext, ToolError, ToolResult
from kreafinity.tools.registry import register_tool

# Bounds keeping a search fast enough to answer interactively.
MAX_SCANNED_ENTRIES = 200_000


@register_tool
class SearchFiles(Tool):
    name = "search_files"
    description = (
        "Search the user's files and folders by name, recursively from a folder. "
        "The pattern is case-insensitive and supports shell wildcards ('*.pdf', 'facture*'); "
        "without wildcards it matches names containing the text. Use pattern '*' with "
        "sort_by='size' to find the largest files under a folder."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "minLength": 1,
                "description": "Name pattern, e.g. '*.jpg', 'rapport', 'IMG_2024*'.",
            },
            "path": {
                "type": "string",
                "description": "Folder to search in. '~' is the home folder (default).",
            },
            "kind": {
                "type": "string",
                "enum": ["file", "directory", "any"],
                "description": "Type of entries to return. Default: file.",
            },
            "sort_by": {
                "type": "string",
                "enum": ["name", "size", "modified"],
                "description": "name (A to Z), size (largest first) or modified (newest "
                "first). Default: name.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "Maximum number of results. Default: 50.",
            },
            "include_hidden": {
                "type": "boolean",
                "description": "Also search hidden files and folders. Default: false.",
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }
    risk = RiskLevel.SAFE

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        root = context.resolve(arguments.get("path"))
        return await asyncio.to_thread(
            self._search,
            root,
            context,
            pattern=arguments["pattern"],
            kind=arguments.get("kind", "file"),
            sort_by=arguments.get("sort_by", "name"),
            limit=arguments.get("limit", 50),
            include_hidden=arguments.get("include_hidden", False),
        )

    @staticmethod
    def _search(
        root: Path,
        context: ToolContext,
        *,
        pattern: str,
        kind: str,
        sort_by: SortKey,
        limit: int,
        include_hidden: bool,
    ) -> ToolResult:
        if not root.is_dir():
            raise ToolError(f"Folder not found: {root}")
        pattern = pattern.casefold()
        if not any(char in pattern for char in "*?["):
            pattern = f"*{pattern}*"

        matches: list[dict[str, Any]] = []
        scanned = 0
        complete = True
        # followlinks=False: symlinked folders are not entered, so the search
        # cannot leave the allowed folders.
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            dirnames[:] = [
                d
                for d in dirnames
                if (include_hidden or not d.startswith("."))
                and context.is_allowed(current_path / d)
            ]
            candidates: list[tuple[str, bool]] = [(d, True) for d in dirnames]
            candidates += [(f, False) for f in filenames if include_hidden or not f.startswith(".")]
            for name, is_dir in candidates:
                scanned += 1
                if (kind == "file" and is_dir) or (kind == "directory" and not is_dir):
                    continue
                if not fnmatch.fnmatchcase(name.casefold(), pattern):
                    continue
                entry = entry_info(str(current_path / name), name=name)
                if entry is not None:
                    entry["path"] = str(current_path / name)
                    matches.append(entry)
            if scanned >= MAX_SCANNED_ENTRIES:
                complete = False
                break

        sort_entries(matches, sort_by)
        return ToolResult.json(
            {
                "searched_in": str(root),
                "pattern": pattern,
                "total_matches": len(matches),
                "truncated": len(matches) > limit,
                "search_complete": complete,
                "results": strip_private(matches[:limit]),
            }
        )
