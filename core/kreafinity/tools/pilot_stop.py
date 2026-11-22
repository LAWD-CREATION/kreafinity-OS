"""Tool: end pilot mode (give the mouse and keyboard back to the user)."""

from __future__ import annotations

from typing import Any, ClassVar

from kreafinity import pilot
from kreafinity.tools.base import RiskLevel, Tool, ToolContext, ToolResult
from kreafinity.tools.registry import register_tool


@register_tool
class PilotStop(Tool):
    name = "pilot_stop"
    description = (
        "End pilot mode as soon as the task is done (or if you are stuck): the banner "
        "disappears and the user has the mouse and keyboard back."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    risk = RiskLevel.SAFE  # only gives control back

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        session = pilot.get_session()
        was_active = session.active
        await session.stop("assistant")
        return ToolResult.json({"pilot": "off", "was_on": was_active})
