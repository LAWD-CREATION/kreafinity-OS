"""Tool: bring a window forward, minimize, maximize or snap it (KDE Plasma)."""

from __future__ import annotations

from typing import Any, ClassVar

from kreafinity.i18n import t
from kreafinity.system import windows
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
class ArrangeWindow(Tool):
    name = "arrange_window"
    description = (
        "Arrange an open window (KDE Plasma): bring it to the front ('focus'), minimize it, "
        "show it again ('restore'), maximize it, or snap it to the left or right half of the "
        "screen (two calls put two windows side by side). Give the window's id from "
        "list_windows, or part of its title or application name."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "window": {
                "type": "string",
                "description": "Window id (list_windows), or part of its title or application.",
            },
            "action": {"type": "string", "enum": list(windows.ACTIONS)},
        },
        "required": ["window", "action"],
        "additionalProperties": False,
    }
    risk = RiskLevel.CONFIRM

    async def _target(self, arguments: dict[str, Any]) -> windows.Window:
        if not windows.supported():
            raise ToolError("Arranging windows is supported on KDE Plasma only")
        if arguments.get("action") not in windows.ACTIONS:
            raise ToolError(f"Unknown action: {', '.join(windows.ACTIONS)}")
        wanted = str(arguments.get("window", "")).strip()
        if not wanted:
            raise ToolError("Say which window (id, title or application)")
        try:
            found = await windows.list_windows()
        except windows.WindowsError as exc:
            raise ToolError(f"Cannot list the windows: {exc}") from exc
        exact = [window for window in found if window.id == wanted]
        if exact:
            return exact[0]
        needle = wanted.casefold()
        matches = [
            window
            for window in found
            if needle in window.title.casefold() or needle in window.application.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        titles = "; ".join(f"{window.title} ({window.id})" for window in (matches or found)[:10])
        if not matches:
            raise ToolError(f"No open window matches {wanted!r}. Open windows: {titles or 'none'}")
        raise ToolError(f"{len(matches)} windows match {wanted!r}: {titles}. Give the id.")

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        window = await self._target(arguments)
        title = printable(window.title)
        return ActionPreview(
            summary=t(f"tool.arrange_window.{arguments['action']}", title=title),
            details=(f"{title} — {printable(window.application)}",),
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        window = await self._target(arguments)
        try:
            after = await windows.act(window.id, arguments["action"])
        except windows.WindowsError as exc:
            raise ToolError(f"Cannot arrange the window: {exc}") from exc
        # Not "active": KWin gives the focus a moment later.
        return ToolResult.json(
            {
                "done": arguments["action"],
                "window": {"id": after.id, "title": after.title, "minimized": after.minimized},
            }
        )
