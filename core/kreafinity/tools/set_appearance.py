"""Tool: change the look of the desktop (light or dark theme, wallpaper)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from kreafinity.i18n import t
from kreafinity.system import appearance
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

KREAFINITY_WALLPAPER = "kreafinity"


@dataclass(frozen=True, slots=True)
class _Plan:
    theme: str | None
    wallpaper: Path | None


@register_tool
class SetAppearance(Tool):
    name = "set_appearance"
    description = (
        "Change the look of the desktop (KDE Plasma or GNOME): a dark, light or Kreafinity "
        "(dark and violet) theme, and/or the wallpaper (an image file of the user, or "
        "'kreafinity' for the Kreafinity wallpaper). The layout of the desktop is not changed."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "theme": {"type": "string", "enum": list(appearance.THEMES)},
            "wallpaper": {
                "type": "string",
                "description": "Image file to use as wallpaper, or 'kreafinity'.",
            },
        },
        "minProperties": 1,
        "additionalProperties": False,
    }
    risk = RiskLevel.CONFIRM

    def _plan(self, arguments: dict[str, Any], context: ToolContext) -> _Plan:
        theme, raw = arguments.get("theme"), arguments.get("wallpaper")
        if theme is None and raw is None:
            raise ToolError("Give a theme, a wallpaper, or both")
        if theme is not None and theme not in appearance.THEMES:
            raise ToolError(f"Unknown theme {theme!r}: {', '.join(appearance.THEMES)}")
        wallpaper: Path | None = None
        if raw is not None and raw.strip().lower() == KREAFINITY_WALLPAPER:
            wallpaper = appearance.kreafinity_wallpaper()
            if wallpaper is None:
                raise ToolError("The Kreafinity wallpaper is not installed")
        elif raw is not None:
            wallpaper = context.resolve(raw)  # only the folders the user allowed
            if not wallpaper.is_file():
                raise ToolError(f"Not found: {wallpaper}")
            if wallpaper.suffix.lower() not in appearance.IMAGE_SUFFIXES:
                raise ToolError(f"Not an image: {wallpaper.name}")
        if appearance.desktop() is None:
            raise ToolError("Changing the look is supported on KDE Plasma and GNOME only")
        return _Plan(theme=theme, wallpaper=wallpaper)

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        plan = self._plan(arguments, context)
        parts = []
        if plan.theme is not None:
            parts.append(t(f"tool.set_appearance.theme.{plan.theme}"))
        if plan.wallpaper is not None:
            parts.append(t("tool.set_appearance.wallpaper"))
        details = (printable(str(plan.wallpaper)),) if plan.wallpaper is not None else ()
        return ActionPreview(summary=t("tool.set_appearance.and").join(parts), details=details)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        plan = self._plan(arguments, context)
        result: dict[str, Any] = {}
        try:
            if plan.theme is not None:
                result["desktop"] = await asyncio.to_thread(appearance.set_theme, plan.theme)
                result["theme"] = plan.theme
            if plan.wallpaper is not None:
                result["desktop"] = await asyncio.to_thread(
                    appearance.set_wallpaper, plan.wallpaper
                )
                result["wallpaper"] = str(plan.wallpaper)
        except appearance.AppearanceError as exc:
            done = f" (done: theme {result['theme']})" if "theme" in result else ""
            raise ToolError(f"Cannot change the look: {exc}{done}") from exc
        return ToolResult.json(result)
