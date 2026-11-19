"""Tool: open a web address in the default browser."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar
from urllib.parse import urlsplit

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
class OpenUrl(Tool):
    name = "open_url"
    description = "Open a web page (http or https address) in the user's default browser."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full address, e.g. https://kreafinity.com"}
        },
        "required": ["url"],
        "additionalProperties": False,
    }
    risk = RiskLevel.CONFIRM

    @staticmethod
    def _url(arguments: dict[str, Any]) -> str:
        url: str = arguments["url"].strip()
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            raise ToolError("Only web addresses starting with http:// or https:// can be opened")
        if any(char.isspace() or not char.isprintable() for char in url):
            raise ToolError("The address contains invalid characters")
        return url

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        return ActionPreview(summary=t("tool.open_url.summary"), details=(self._url(arguments),))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        url = self._url(arguments)
        try:
            await asyncio.to_thread(desktop.open_with_default_app, url)
        except desktop.LaunchError as exc:
            raise ToolError(f"Cannot open {url}: {exc}") from exc
        return ToolResult.json({"opened": url})
