"""Tool: read a text file."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

from kreafinity.tools.base import RiskLevel, Tool, ToolContext, ToolError, ToolResult
from kreafinity.tools.registry import register_tool

MAX_BYTES = 100_000


@register_tool
class ReadFile(Tool):
    name = "read_file"
    description = (
        "Read a text file of the user (notes, configuration, code, CSV…). Only the first "
        f"{MAX_BYTES // 1000} kB are returned. Binary files (images, PDF, archives) cannot be read."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File to read."}},
        "required": ["path"],
        "additionalProperties": False,
    }
    risk = RiskLevel.SAFE

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = context.resolve(arguments["path"])
        return await asyncio.to_thread(self._read, path)

    @staticmethod
    def _read(path: Path) -> ToolResult:
        if not path.exists():
            raise ToolError(f"File not found: {path}")
        if not path.is_file():
            raise ToolError(f"Not a file: {path}")
        try:
            with path.open("rb") as handle:
                data = handle.read(MAX_BYTES + 1)
            size = path.stat().st_size
        except PermissionError as exc:
            raise ToolError(f"Permission denied: {path}") from exc
        if b"\x00" in data[:8192]:
            raise ToolError(f"{path} is a binary file, not a text file")
        return ToolResult.json(
            {
                "path": str(path),
                "size_bytes": size,
                "truncated": len(data) > MAX_BYTES,
                "content": data[:MAX_BYTES].decode("utf-8", errors="replace"),
            }
        )
