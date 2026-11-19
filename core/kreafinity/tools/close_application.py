"""Tool: close a running application (politely, never forced)."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import psutil

from kreafinity.i18n import t
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
class CloseApplication(Tool):
    name = "close_application"
    description = (
        "Ask a running application of the user to quit, by its program name (e.g. 'firefox', "
        "'gedit', 'vlc'). The application may ask to save open documents first. It is never "
        "killed by force."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"name": {"type": "string", "minLength": 1, "description": "Program name."}},
        "required": ["name"],
        "additionalProperties": False,
    }
    risk = RiskLevel.CONFIRM

    async def _find(self, arguments: dict[str, Any]) -> list[psutil.Process]:
        processes = await asyncio.to_thread(desktop.find_processes, arguments["name"])
        if not processes:
            raise ToolError(f"No running program named {arguments['name']!r}")
        return processes

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        processes = await self._find(arguments)
        details = []
        for process in processes:
            try:
                command = " ".join(process.cmdline()) or process.name()
            except psutil.Error:
                command = "?"
            details.append(f"PID {process.pid} — {command[:120]}")
        return ActionPreview(
            summary=t(
                "tool.close_application.summary", name=arguments["name"], count=len(processes)
            ),
            details=tuple(details),
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        processes = await self._find(arguments)
        try:
            gone, alive = await asyncio.to_thread(desktop.terminate, processes)
        except desktop.LaunchError as exc:
            raise ToolError(str(exc)) from exc
        result: dict[str, Any] = {
            "closed_pids": [p.pid for p in gone],
            "still_running_pids": [p.pid for p in alive],
        }
        if alive:
            result["note"] = "Some processes are still running; they may be waiting for the user."
        return ToolResult.json(result)
