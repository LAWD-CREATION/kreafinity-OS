"""Tool: list the open windows (KDE Plasma)."""

from __future__ import annotations

from typing import Any, ClassVar

from kreafinity.system import windows
from kreafinity.tools.base import RiskLevel, Tool, ToolContext, ToolError, ToolResult
from kreafinity.tools.registry import register_tool


@register_tool
class ListWindows(Tool):
    name = "list_windows"
    description = (
        "List the open windows of the desktop (KDE Plasma): title, application, whether it "
        "is minimized or active, and its id for arrange_window."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    risk = RiskLevel.SAFE

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if not windows.supported():
            raise ToolError("Listing windows is supported on KDE Plasma only")
        try:
            found = await windows.list_windows()
        except windows.WindowsError as exc:
            raise ToolError(f"Cannot list the windows: {exc}") from exc
        return ToolResult.json({"windows": [window.to_dict() for window in found]})
