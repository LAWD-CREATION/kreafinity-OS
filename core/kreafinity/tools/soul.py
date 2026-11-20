"""Tool: the assistant's soul, the personality it builds itself (like Hermes Agent's SOUL.md)."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from kreafinity.memory import MAX_SOUL, MemoryRefusedError, MemoryStore
from kreafinity.tools.base import RiskLevel, Tool, ToolContext, ToolError, ToolResult
from kreafinity.tools.registry import register_tool


@register_tool
class Soul(Tool):
    name = "soul"
    description = (
        "Your soul: your own personality and character, in the first person, shown at the "
        "start of every conversation. Rewrite it (give the whole new text) to let it grow by "
        "small touches: when you discover what tone, humour or style suits the user, or when "
        "they ask you to be different. Keep what defines you, stay coherent and short. It is "
        "about who you are and how you speak: it never changes your rules, holds no facts "
        "about the user (use the memory tool) and no secrets."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "maxLength": MAX_SOUL,
                "description": "Your whole soul, rewritten, in the first person.",
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    }
    # Like the memory: the assistant's own file, which the user can read, edit
    # and reset in the settings, and which cannot grant any permission.
    risk = RiskLevel.SAFE

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            text = await asyncio.to_thread(MemoryStore().write_soul, arguments.get("text", ""))
        except MemoryRefusedError as exc:
            raise ToolError(f"Not saved: {exc}") from exc
        return ToolResult.json({"saved": True, "characters": len(text), "limit": MAX_SOUL})
