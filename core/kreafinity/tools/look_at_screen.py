"""Tool: look at the screen, to help the user with what they see (with their consent)."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from kreafinity.i18n import t
from kreafinity.providers.base import Image
from kreafinity.system import screen
from kreafinity.tools.base import (
    ActionPreview,
    RiskLevel,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
    printable,
)
from kreafinity.tools.registry import register_tool


@register_tool
class LookAtScreen(Tool):
    name = "look_at_screen"
    description = (
        "Take a screenshot of the user's screen and look at it, to understand what they see "
        "(an error message, a window, a setting) and guide them step by step. The user "
        "approves every screenshot. Say why you want to look. Describe what you see before "
        "advising; never read passwords or private messages aloud."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "maxLength": 200,
                "description": "Why you want to look, shown to the user (in their language).",
            },
        },
        "required": ["reason"],
        "additionalProperties": False,
    }
    # The screen can show anything (messages, documents…), and the screenshot
    # goes to the AI the user chose: always asked first.
    risk = RiskLevel.CONFIRM

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        reason = printable(str(arguments.get("reason", "")).strip())[:200]
        return ActionPreview(
            summary=t("tool.look_at_screen.summary"),
            details=tuple(line for line in (reason, t("tool.look_at_screen.privacy")) if line),
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            shot = await asyncio.to_thread(screen.capture)
        except screen.ScreenError as exc:
            raise ToolError(f"Cannot see the screen: {exc}") from exc
        return ToolResult.json(
            {
                "screenshot": "attached as an image",
                "size": f"{shot.width}x{shot.height}",
                "screen": f"{shot.original[0]}x{shot.original[1]}",
            },
            images=[Image(media_type=shot.media_type, data=shot.data)],
        )
