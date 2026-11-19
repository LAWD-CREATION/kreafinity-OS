"""Tool: list installed applications."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from kreafinity.i18n import get_language
from kreafinity.system import desktop
from kreafinity.tools.base import RiskLevel, Tool, ToolContext, ToolResult
from kreafinity.tools.registry import register_tool


@register_tool
class ListApplications(Tool):
    name = "list_applications"
    description = (
        "List the applications installed on the computer, optionally filtered by a search "
        "text (name, description or keyword, e.g. 'navigateur', 'photo', 'firefox'). "
        "Use the returned id with open_application."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Text to search for. Default: all."},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 300,
                "description": "Maximum number of results. Default: 50.",
            },
        },
        "additionalProperties": False,
    }
    risk = RiskLevel.SAFE

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        apps = await asyncio.to_thread(desktop.list_applications, get_language())
        query = arguments.get("query", "").strip()
        if query:
            apps = [app for app in apps if app.matches(query)]
        limit = arguments.get("limit", 50)
        return ToolResult.json(
            {
                "total": len(apps),
                "truncated": len(apps) > limit,
                "applications": [
                    {"id": app.id, "name": app.name, "description": app.comment}
                    for app in apps[:limit]
                ],
            }
        )
