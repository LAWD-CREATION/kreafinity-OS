"""Tool: start pilot mode (the assistant drives the mouse and keyboard)."""

from __future__ import annotations

from typing import Any, ClassVar

from kreafinity import pilot
from kreafinity.i18n import get_language, t
from kreafinity.pilot.backends import InputError
from kreafinity.pilot.desktop import is_kde
from kreafinity.providers.base import Image
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
class PilotStart(Tool):
    name = "pilot_start"
    description = (
        "Start pilot mode: you move the mouse and use the keyboard on the user's screen, while "
        "they watch, to do something no other tool can do (a setting only reachable through "
        "an application's windows, guiding them by example…). Prefer the dedicated tools "
        "(files, settings, applications, browser_*) when one fits. The user approves; a "
        "banner stays on screen and they take back control at any time (Escape, moving the "
        "mouse). You get a screenshot; then use pilot_act, and pilot_stop when done. Pilot "
        "mode ends with the request."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "What you will do, shown to the user (in their language).",
            }
        },
        "required": ["reason"],
        "additionalProperties": False,
    }
    # Handing over the mouse and keyboard: explicit approval, shown clearly.
    risk = RiskLevel.DANGER

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        reason = printable(str(arguments.get("reason", "")).strip())[:200]
        take_back = (
            "tool.pilot_start.take_back_escape" if is_kde() else "tool.pilot_start.take_back"
        )
        return ActionPreview(
            summary=t("tool.pilot_start.summary"),
            details=tuple(
                line
                for line in (
                    reason,
                    t("tool.pilot_start.banner"),
                    t(take_back),
                    t("tool.pilot_start.limits"),
                    t("tool.pilot_start.privacy"),
                )
                if line
            ),
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        session = pilot.get_session()
        try:
            shot = await session.start(str(arguments.get("reason", "")), get_language())
        except (pilot.PilotError, InputError) as exc:
            raise ToolError(f"Cannot start pilot mode: {exc}") from exc
        return ToolResult.json(
            {
                "pilot": "on",
                "screenshot": "attached as an image",
                "image_size": f"{shot.width}x{shot.height}",
                "how": "Give positions in pixels of the latest screenshot (this one, then the "
                "one returned by each pilot_act). Never type passwords or bank card numbers: "
                "ask the user. Stop with pilot_stop when done.",
                "take_back": "Escape, moving the mouse or the banner's Stop button"
                if session.escape
                else "moving the mouse or the banner's Stop button",
            },
            images=[Image(media_type=shot.media_type, data=shot.data)],
        )

    def iteration_limit(self) -> int | None:
        return pilot.ITERATIONS if pilot.get_session().active else None

    async def end_request(self) -> None:
        await pilot.end_request()
