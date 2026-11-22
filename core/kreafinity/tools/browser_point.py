"""Tool: show the user an element of the page, to guide them."""

from __future__ import annotations

from typing import Any, ClassVar

from kreafinity.browser import get_session
from kreafinity.tools._browser import browser_errors, known_element
from kreafinity.tools.base import RiskLevel, Tool, ToolContext, ToolError, ToolResult, printable
from kreafinity.tools.registry import register_tool


@register_tool
class BrowserPoint(Tool):
    name = "browser_point"
    description = (
        "Show the user where to click or what to fill in, in the assistant's browser: a "
        "glowing frame around the element and a bubble with your short message (« Cliquez "
        "ici pour… »). Use it to guide and teach step by step when the user does it "
        "themselves; then read the page again to see what they did."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "ref": {
                "type": "integer",
                "minimum": 1,
                "description": "Element number (browser_read).",
            },
            "message": {
                "type": "string",
                "minLength": 1,
                "maxLength": 160,
                "description": "Short instruction shown next to it, in the user's language.",
            },
        },
        "required": ["ref", "message"],
        "additionalProperties": False,
    }
    # Only draws on the assistant's own window; nothing is clicked or sent.
    risk = RiskLevel.SAFE

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        element = known_element(arguments.get("ref"))
        message = printable(str(arguments.get("message", "")).strip())[:160]
        if not message:
            raise ToolError("The message is empty")
        with browser_errors():
            await get_session().point(int(element["ref"]), message)
        return ToolResult.json({"shown": element["ref"], "label": element.get("label", "")})
