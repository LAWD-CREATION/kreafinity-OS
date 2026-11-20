"""Tool: search the past conversations (full text, accents ignored)."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from kreafinity.agent.history import ConversationStore
from kreafinity.tools.base import RiskLevel, Tool, ToolContext, ToolResult
from kreafinity.tools.registry import register_tool


@register_tool
class SearchConversations(Tool):
    name = "search_conversations"
    description = (
        "Search your past conversations with the user, to recall what was said or done "
        "before (« comme la dernière fois », a file mentioned last week…). Returns excerpts "
        "with their date and conversation title."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 200},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    risk = RiskLevel.SAFE  # reads the user's own history, kept on this computer

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        found = await asyncio.to_thread(
            ConversationStore().search, arguments["query"], limit=arguments.get("limit", 8)
        )
        return ToolResult.json(
            {
                "results": [
                    {
                        "conversation": result.title or result.conversation_id,
                        "date": result.date[:10],
                        "who": "user" if result.role == "user" else "assistant",
                        "excerpt": result.excerpt,
                    }
                    for result in found
                ]
            }
        )
