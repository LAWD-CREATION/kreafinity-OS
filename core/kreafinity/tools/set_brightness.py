"""Tool: change the screen brightness (laptop backlight)."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from kreafinity.i18n import t
from kreafinity.system import brightness
from kreafinity.tools.base import (
    ActionPreview,
    RiskLevel,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
)
from kreafinity.tools.registry import register_tool

#: Never darker than this: a black screen would look broken.
MINIMUM_PERCENT = 5


@register_tool
class SetBrightness(Tool):
    name = "set_brightness"
    description = (
        "Change the brightness of the computer's built-in screen (laptops): set a level, "
        f"or raise or lower it. Never below {MINIMUM_PERCENT}%. External monitors are not "
        "supported. The current brightness is given by system_info."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "level": {
                "type": "integer",
                "minimum": MINIMUM_PERCENT,
                "maximum": 100,
                "description": "New brightness, in percent.",
            },
            "change": {
                "type": "integer",
                "minimum": -100,
                "maximum": 100,
                "description": "Relative change, in percentage points (e.g. 20 or -20).",
            },
        },
        "minProperties": 1,
        "maxProperties": 1,
        "additionalProperties": False,
    }
    risk = RiskLevel.CONFIRM

    async def _plan(self, arguments: dict[str, Any]) -> tuple[brightness.Backlight, int]:
        try:
            backlight = await asyncio.to_thread(brightness.find_backlight)
        except brightness.BrightnessError as exc:
            raise ToolError(f"Cannot change the brightness: {exc}") from exc
        percent = arguments.get("level")
        if percent is None:
            percent = backlight.percent + arguments["change"]
        percent = min(100, max(MINIMUM_PERCENT, percent))
        if percent == backlight.percent:
            raise ToolError(f"The brightness is already at {percent}%")
        return backlight, percent

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        backlight, percent = await self._plan(arguments)
        return ActionPreview(
            summary=t("tool.set_brightness.summary", level=percent, current=backlight.percent)
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        backlight, percent = await self._plan(arguments)
        try:
            await brightness.set_brightness(backlight, backlight.raw_level(percent))
        except brightness.BrightnessError as exc:
            raise ToolError(f"Cannot change the brightness: {exc}") from exc
        return ToolResult.json({"brightness_percent": percent, "before": backlight.percent})
