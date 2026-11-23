"""MCP tools as assistant tools.

Every tool of a connected server becomes a Kreafinity tool named
``mcp_<server>_<tool>``. It asks the user's approval before each call
(``confirm``), unless the user listed it in the server's ``auto_approve``; a
tool the server marks as destructive needs the explicit ``danger`` approval.
What a server says about its tools (descriptions, hints) is untrusted: it
never makes a tool run without approval by itself.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any, ClassVar

import jsonschema

from kreafinity.i18n import t
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

if TYPE_CHECKING:
    from kreafinity.mcp.manager import McpServer

MAX_DESCRIPTION = 1000
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_IMAGE = 8 * 1024 * 1024  # base64 characters
EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


class McpTool(Tool):
    """A tool of an MCP server (subclassed once per tool by `make_tool`)."""

    server: ClassVar[McpServer]
    remote_name: ClassVar[str]
    title: ClassVar[str]
    description = ""
    parameters: ClassVar[dict[str, Any]] = EMPTY_SCHEMA
    risk = RiskLevel.CONFIRM

    def describe(self, arguments: dict[str, Any]) -> str:
        return f"{self.server.name}.{self.remote_name}({_short(arguments, 120)})"

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        details = [
            f"{printable(str(key))} : {printable(_short(value, 200))}"
            for key, value in list(arguments.items())[:12]
        ]
        details.append(
            t("tool.mcp.origin", server=self.server.name, command=self.server.config.describe())
        )
        return ActionPreview(
            summary=t("tool.mcp.summary", tool=printable(self.title), server=self.server.name),
            details=tuple(details),
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        from kreafinity.mcp.client import McpError

        try:
            result = await self.server.call(self.remote_name, arguments)
        except McpError as exc:
            raise ToolError(f"MCP server {self.server.name}: {exc}") from exc
        return convert(result)


def _short(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tool_name(server: str, remote: str, taken: set[str]) -> str:
    """A valid, unique tool name for every provider (64 characters at most)."""
    name = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp_{server}_{remote}")
    if len(name) > 64 or name in taken:
        digest = hashlib.sha256(remote.encode()).hexdigest()[:8]
        name = f"{name[:55]}_{digest}"
    return name


def _schema(raw: Any) -> dict[str, Any]:
    """The tool's input schema, if usable (else: any object)."""
    if not isinstance(raw, dict) or raw.get("type", "object") != "object":
        return dict(EMPTY_SCHEMA)
    try:
        jsonschema.Draft202012Validator.check_schema(raw)
    except jsonschema.SchemaError:
        return dict(EMPTY_SCHEMA)
    schema = dict(raw)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def risk_of(remote: dict[str, Any], auto_approve: tuple[str, ...]) -> RiskLevel:
    if remote.get("name") in auto_approve:
        return RiskLevel.SAFE
    hints = remote.get("annotations") or {}
    if (
        isinstance(hints, dict)
        and hints.get("destructiveHint") is True
        and not hints.get("readOnlyHint")
    ):
        return RiskLevel.DANGER
    return RiskLevel.CONFIRM


def make_tool(server: McpServer, remote: dict[str, Any], taken: set[str]) -> McpTool:
    """An assistant tool calling `remote` (an entry of ``tools/list``) on `server`."""
    remote_name = str(remote["name"])
    name = tool_name(server.name, remote_name, taken)
    taken.add(name)
    title = str(
        remote.get("title") or (remote.get("annotations") or {}).get("title") or remote_name
    )
    text = str(remote.get("description") or title).strip()[:MAX_DESCRIPTION]
    attributes = {
        "name": name,
        "server": server,
        "remote_name": remote_name,
        "title": title[:80],
        "description": f"[{server.name}, MCP extension] {text}",
        "parameters": _schema(remote.get("inputSchema")),
        "risk": risk_of(remote, server.config.auto_approve),
    }
    tool_class = type(f"McpTool_{name}", (McpTool,), attributes)
    return tool_class()  # type: ignore[no-any-return]


def convert(result: Any) -> ToolResult:
    """A ``tools/call`` result -> the assistant's tool result (text and images)."""
    if not isinstance(result, dict):
        return ToolResult(_short(result, 20_000))
    parts: list[str] = []
    images: list[Image] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "image":
            mime, data = str(item.get("mimeType", "")), str(item.get("data", ""))
            if mime in IMAGE_TYPES and 0 < len(data) <= MAX_IMAGE:
                images.append(Image(media_type=mime, data=data))
            else:
                parts.append(f"[image not shown: {mime or 'unknown type'}]")
        elif kind == "resource":
            resource = item.get("resource") or {}
            uri = resource.get("uri", "")
            if "text" in resource:
                parts.append(f"[{uri}]\n{resource['text']}")
            else:
                parts.append(f"[{uri}: {resource.get('mimeType', 'binary')} content not shown]")
        elif kind == "resource_link":
            parts.append(f"[link: {item.get('uri', '')}] {item.get('name', '')}".strip())
        else:
            parts.append(f"[{kind} content not shown]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return ToolResult(
        "\n".join(parts) or "(no content)",
        is_error=bool(result.get("isError")),
        images=tuple(images),
    )
