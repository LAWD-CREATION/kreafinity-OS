"""Tool: click, type and scroll in the assistant's browser, with the user's approval."""

from __future__ import annotations

from typing import Any, ClassVar

from kreafinity.browser import KEYS, get_session
from kreafinity.i18n import t
from kreafinity.tools._browser import browser_errors, known_element, site, target
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

MAX_STEPS = 10
MAX_TEXT = 500
DIRECTIONS = ("down", "up", "top", "bottom")


@register_tool
class BrowserAct(Tool):
    name = "browser_act"
    description = (
        "Do steps in the assistant's browser for the user, in order: click an element, type "
        "text in a field (submit=true presses Enter after), select an option of a list, press "
        "a key, scroll, go back. The user approves all the steps at once, so group the steps of "
        "one form. Element numbers come from the latest browser_read: read the page again "
        "after it changes. Never type passwords, codes or bank card numbers: show the field "
        "with browser_point and let the user type them. Ask before buying, paying, sending a "
        "message or deleting anything online."
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
                            "enum": ["click", "type", "select", "press", "scroll", "back"],
                        },
                        "ref": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Element number (click, type, select).",
                        },
                        "text": {
                            "type": "string",
                            "maxLength": MAX_TEXT,
                            "description": "Text to type (type); replaces what the field holds.",
                        },
                        "submit": {"type": "boolean", "description": "Press Enter after typing."},
                        "option": {"type": "string", "description": "Option to choose (select)."},
                        "key": {"type": "string", "enum": list(KEYS), "description": "(press)"},
                        "direction": {"type": "string", "enum": list(DIRECTIONS)},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["steps"],
        "additionalProperties": False,
    }
    risk = RiskLevel.CONFIRM

    @staticmethod
    def _describe(step: dict[str, Any]) -> str:
        """One step for the approval card (refuses impossible steps before asking)."""
        action = step["action"]
        if action in ("click", "type", "select"):
            if "ref" not in step:
                raise ToolError(f"The {action} step needs the element number (ref)")
            element = known_element(step["ref"])
            name = target(element)
            if action == "click":
                return t("tool.browser_act.click", target=name)
            if action == "type":
                if "text" not in step:
                    raise ToolError("The type step needs the text to type")
                if element.get("secret"):
                    raise ToolError(
                        "This field is for a password or bank card: never type it. Show it "
                        "with browser_point and let the user type it."
                    )
                text = printable(str(step["text"]))
                key = (
                    "tool.browser_act.type_submit"
                    if step.get("submit")
                    else "tool.browser_act.type"
                )
                return t(key, text=text[:120] + ("…" if len(text) > 120 else ""), target=name)
            if not str(step.get("option", "")).strip():
                raise ToolError("The select step needs the option to choose")
            return t(
                "tool.browser_act.select", option=printable(str(step["option"]))[:80], target=name
            )
        if action == "press":
            if step.get("key") not in KEYS:
                raise ToolError("The press step needs a key: " + ", ".join(KEYS))
            return t("tool.browser_act.press", name=t(f"tool.browser.key.{step['key']}"))
        if action == "scroll":
            return t(f"tool.browser_act.scroll.{step.get('direction', 'down')}")
        return t("tool.browser_act.back")

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        steps = [self._describe(step) for step in arguments["steps"]]
        place = site(get_session().url) if get_session().url else ""
        return ActionPreview(
            summary=t("tool.browser_act.summary", site=place), details=tuple(steps)
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        steps = arguments["steps"]
        for step in steps:
            self._describe(step)
        session = get_session()
        with browser_errors():
            done = await session.act(steps)
            page = await session.where()
        return ToolResult.json(
            {
                "done": done,
                "url": page["url"],
                "title": page["title"],
                "next": "Read the page again (browser_read) to see the result.",
            }
        )
