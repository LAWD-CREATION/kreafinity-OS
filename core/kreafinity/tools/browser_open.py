"""Tool: open a page in the assistant's own browser window."""

from __future__ import annotations

from typing import Any, ClassVar

from kreafinity.browser import get_session
from kreafinity.i18n import t
from kreafinity.tools._browser import browser_errors, web_url
from kreafinity.tools.base import ActionPreview, RiskLevel, Tool, ToolContext, ToolResult
from kreafinity.tools.registry import register_tool


@register_tool
class BrowserOpen(Tool):
    name = "browser_open"
    description = (
        "Open a web page in the assistant's own browser window, to look at it with the user, "
        "guide them step by step (browser_point) or do something for them (browser_act). It is "
        "a separate window: none of the user's saved passwords, history or open sessions. Then "
        "read the page with browser_read. For simply showing a page to the user, open_url "
        "opens it in their usual browser."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Full address, e.g. https://www.service-public.fr",
            }
        },
        "required": ["url"],
        "additionalProperties": False,
    }
    risk = RiskLevel.CONFIRM

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        return ActionPreview(
            summary=t("tool.browser_open.summary"),
            details=(web_url(arguments.get("url")), t("tool.browser.privacy")),
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        url = web_url(arguments.get("url"))
        with browser_errors():
            page = await get_session().open(url)
        return ToolResult.json({"opened": page["url"], "title": page["title"]})
