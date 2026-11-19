"""Tool: start an installed application."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from kreafinity.i18n import get_language, t
from kreafinity.system import desktop
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
class OpenApplication(Tool):
    name = "open_application"
    description = (
        "Start an installed application, given its id or exact name as returned by "
        "list_applications."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "application": {
                "type": "string",
                "description": "Application id (e.g. 'org.gnome.Nautilus') or exact name.",
            }
        },
        "required": ["application"],
        "additionalProperties": False,
    }
    risk = RiskLevel.CONFIRM

    async def _find(self, arguments: dict[str, Any]) -> desktop.DesktopApp:
        query = arguments["application"]
        app = await asyncio.to_thread(desktop.find_application, query, get_language())
        if app is None:
            raise ToolError(
                f"No installed application matches {query!r}; use list_applications to find it"
            )
        return app

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        app = await self._find(arguments)
        return ActionPreview(
            summary=t("tool.open_application.summary", name=app.name), details=(app.id,)
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        app = await self._find(arguments)
        try:
            await asyncio.to_thread(desktop.launch_application, app)
        except desktop.LaunchError as exc:
            raise ToolError(f"Cannot start {app.name}: {exc}") from exc
        return ToolResult.json({"opened": app.name, "id": app.id})
