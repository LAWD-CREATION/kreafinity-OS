"""Tool: mouse and keyboard steps in pilot mode."""

from __future__ import annotations

from typing import Any, ClassVar

from kreafinity import pilot
from kreafinity.pilot.backends import InputError
from kreafinity.pilot.keys import ComboError, parse_combo
from kreafinity.providers.base import Image
from kreafinity.tools.base import RiskLevel, Tool, ToolContext, ToolError, ToolResult
from kreafinity.tools.registry import register_tool

MAX_STEPS = 10
POINTED = ("click", "move", "drag", "scroll")


@register_tool
class PilotAct(Tool):
    name = "pilot_act"
    description = (
        "In pilot mode (pilot_start first): do mouse and keyboard steps on the user's screen, in "
        "order. Positions are pixels of the latest screenshot you received. click (button "
        "left/right/middle, double), move, drag (to to_x, to_y), scroll (direction, amount), "
        "type (text, into the focused field), key (a combination like ctrl+s, alt+f4, enter, "
        "tab; Escape is the user's), wait (seconds). Returns a new screenshot: look at it "
        "before the next steps. Refused in terminals (use run_command) and on Kreafinity's "
        "windows. Never type passwords or bank card numbers: ask the user to."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_STEPS,
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["click", "move", "drag", "scroll", "type", "key", "wait"],
                        },
                        "x": {"type": "number", "minimum": 0},
                        "y": {"type": "number", "minimum": 0},
                        "to_x": {"type": "number", "minimum": 0},
                        "to_y": {"type": "number", "minimum": 0},
                        "button": {"type": "string", "enum": ["left", "right", "middle"]},
                        "double": {"type": "boolean"},
                        "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                        "amount": {"type": "integer", "minimum": 1, "maximum": 15},
                        "text": {"type": "string", "minLength": 1, "maxLength": 1000},
                        "keys": {"type": "string", "minLength": 1, "maxLength": 40},
                        "seconds": {"type": "number", "minimum": 0, "maximum": 5},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["steps"],
        "additionalProperties": False,
    }
    # Each step changes the screen; the user approved them all by starting
    # pilot mode (banner on screen, Escape or a mouse move to stop).
    risk = RiskLevel.CONFIRM

    def pre_approved(self, arguments: dict[str, Any]) -> bool:
        return pilot.get_session().active

    @staticmethod
    def _check(steps: list[dict[str, Any]]) -> None:
        for number, step in enumerate(steps, 1):
            action = step["action"]
            needed = {
                "click": ("x", "y"),
                "move": ("x", "y"),
                "drag": ("x", "y", "to_x", "to_y"),
                "scroll": ("x", "y"),
                "type": ("text",),
                "key": ("keys",),
            }.get(action, ())
            missing = [name for name in needed if name not in step]
            if missing:
                raise ToolError(f"Step {number} ({action}) needs: {', '.join(missing)}")
            if action == "key":
                try:
                    parse_combo(str(step["keys"]))
                except ComboError as exc:
                    raise ToolError(f"Step {number}: {exc}") from exc

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        # Only reached when pilot mode is off: tell the model, ask nothing.
        raise ToolError("Pilot mode is not running: start it with pilot_start")

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        steps = arguments["steps"]
        self._check(steps)
        try:
            done, shot = await pilot.get_session().act(steps)
        except pilot.PilotStopped as exc:
            raise ToolError(
                f"Pilot mode stopped: {exc}. Do not try again unless the user asks."
            ) from exc
        except (pilot.PilotError, InputError) as exc:
            raise ToolError(str(exc)) from exc
        return ToolResult.json(
            {
                "done": done,
                "screenshot": "attached as an image (positions now refer to it)",
                "image_size": f"{shot.width}x{shot.height}",
            },
            images=[Image(media_type=shot.media_type, data=shot.data)],
        )

    def iteration_limit(self) -> int | None:
        return pilot.ITERATIONS if pilot.get_session().active else None

    async def end_request(self) -> None:
        await pilot.end_request()
