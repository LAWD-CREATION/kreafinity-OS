"""Tool: change the sound volume, or mute and unmute."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, ClassVar

from kreafinity.i18n import t
from kreafinity.system import audio
from kreafinity.tools.base import (
    ActionPreview,
    RiskLevel,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
)
from kreafinity.tools.registry import register_tool


@dataclass(frozen=True, slots=True)
class _Change:
    current: audio.VolumeState
    percent: int | None
    muted: bool | None


@register_tool
class SetVolume(Tool):
    name = "set_volume"
    description = (
        "Change the volume of the computer's sound output: set a level, raise or lower it, "
        "mute or unmute. The current volume is given by system_info."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "level": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "New volume, in percent.",
            },
            "change": {
                "type": "integer",
                "minimum": -100,
                "maximum": 100,
                "description": "Relative change, in percentage points (e.g. 10 or -10).",
            },
            "mute": {
                "type": "boolean",
                "description": "true to cut the sound, false to bring it back.",
            },
        },
        "minProperties": 1,
        "additionalProperties": False,
    }
    risk = RiskLevel.CONFIRM

    async def _plan(self, arguments: dict[str, Any]) -> _Change:
        level, change, mute = arguments.get("level"), arguments.get("change"), arguments.get("mute")
        if level is not None and change is not None:
            raise ToolError("Give either level or change, not both")
        if mute is True and (level is not None or change is not None):
            raise ToolError("To cut the sound, give only mute=true")
        try:
            current = await asyncio.to_thread(audio.get_volume)
        except audio.AudioError as exc:
            raise ToolError(f"Cannot read the volume: {exc}") from exc
        percent = level
        if change is not None:
            percent = min(100, max(0, current.percent + change))
        if mute is False and not current.muted and percent is not None:
            mute = None  # the sound is already on: only the level changes
        if percent is not None and percent == current.percent and mute is None:
            raise ToolError(f"The volume is already at {percent}%")
        if mute is not None and mute == current.muted and percent is None:
            raise ToolError("The sound is already " + ("muted" if mute else "on"))
        return _Change(current=current, percent=percent, muted=mute)

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        plan = await self._plan(arguments)
        if plan.muted is True:
            summary = t("tool.set_volume.mute")
        elif plan.muted is False and plan.percent is None:
            summary = t("tool.set_volume.unmute", current=plan.current.percent)
        elif plan.muted is False:
            summary = t("tool.set_volume.unmute_level", level=plan.percent)
        else:
            summary = t("tool.set_volume.level", level=plan.percent, current=plan.current.percent)
        return ActionPreview(summary=summary)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        plan = await self._plan(arguments)
        try:
            state = await asyncio.to_thread(
                audio.set_volume, percent=plan.percent, muted=plan.muted
            )
        except audio.AudioError as exc:
            raise ToolError(f"Cannot change the volume: {exc}") from exc
        return ToolResult.json(
            {"volume_percent": state.percent, "muted": state.muted, "before": plan.current.percent}
        )
