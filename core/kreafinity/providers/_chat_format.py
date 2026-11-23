"""Conversion helpers for "chat completions" style APIs (OpenAI, Mistral…).

Private module: not a provider, ignored by the provider discovery.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from kreafinity.providers.base import (
    IMAGES_INTRO,
    Message,
    StopReason,
    ToolCall,
    ToolSpec,
    new_tool_call_id,
)

ERROR_PREFIX = "Error: "

FINISH_REASONS: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "model_length": "max_tokens",
    "content_filter": "refusal",
}


def to_chat_messages(
    messages: Sequence[Message],
    *,
    map_id: Callable[[str], str] = lambda tool_call_id: tool_call_id,
    tool_name_field: bool = False,
) -> list[dict[str, Any]]:
    """Convert neutral messages to chat-completions messages.

    Args:
        messages: The conversation.
        map_id: Rewrites tool call ids (for APIs with a strict id format).
        tool_name_field: Add the tool name to tool result messages.
    """
    result: list[dict[str, Any]] = []
    # Tool results are text only: their images go in a user message right after
    # the results of the same turn (a message between them would be refused).
    pending: list[Message] = []

    def flush_images() -> None:
        if pending:
            result.append(images_message(pending))
            pending.clear()

    for message in messages:
        if message.role != "tool":
            flush_images()
        if message.role == "tool":
            entry: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": map_id(message.tool_call_id or ""),
                "content": (ERROR_PREFIX if message.is_error else "") + message.content,
            }
            if tool_name_field and message.name:
                entry["name"] = message.name
            result.append(entry)
            if message.images:
                pending.append(message)
        elif message.role == "assistant":
            if not message.content and not message.tool_calls:
                continue
            entry = {"role": "assistant", "content": message.content or None}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": map_id(call.id),
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in message.tool_calls
                ]
            result.append(entry)
        elif message.images:  # files the user attached
            parts: list[dict[str, Any]] = [{"type": "text", "text": message.content}]
            parts += [
                {"type": "image_url", "image_url": {"url": f"data:{i.media_type};base64,{i.data}"}}
                for i in message.images
            ]
            result.append({"role": message.role, "content": parts})
        else:
            result.append({"role": message.role, "content": message.content})
    flush_images()
    return result


def images_message(tool_messages: Sequence[Message]) -> dict[str, Any]:
    """A user message holding the images of these tool results (data URLs)."""
    names = ", ".join(dict.fromkeys(m.name or "tool" for m in tool_messages))
    parts: list[dict[str, Any]] = [{"type": "text", "text": IMAGES_INTRO.format(names=names)}]
    for message in tool_messages:
        for image in message.images:
            url = f"data:{image.media_type};base64,{image.data}"
            parts.append({"type": "image_url", "image_url": {"url": url}})
    return {"role": "user", "content": parts}


def to_chat_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """Convert tool specs to chat-completions function tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]


@dataclass
class _PartialCall:
    id: str | None = None
    name: str = ""
    arguments: list[str] = field(default_factory=list)
    parsed: dict[str, Any] | None = None


class ToolCallAccumulator:
    """Rebuilds tool calls streamed as fragments, keyed by their index."""

    def __init__(self) -> None:
        self._calls: dict[int, _PartialCall] = {}

    def add(
        self,
        index: int | None,
        *,
        call_id: str | None = None,
        name: str | None = None,
        arguments: str | dict[str, Any] | None = None,
    ) -> None:
        """Record one streamed fragment of a tool call."""
        key = index if index is not None else len(self._calls)
        call = self._calls.setdefault(key, _PartialCall())
        if call_id:
            call.id = call_id
        if name:
            call.name += name
        if isinstance(arguments, dict):
            call.parsed = arguments
        elif arguments:
            call.arguments.append(arguments)

    def calls(self) -> list[ToolCall]:
        """The complete tool calls, in order."""
        result = []
        for key in sorted(self._calls):
            call = self._calls[key]
            arguments = call.parsed if call.parsed is not None else parse_arguments(call.arguments)
            result.append(
                ToolCall(id=call.id or new_tool_call_id(), name=call.name, arguments=arguments)
            )
        return result


def parse_arguments(fragments: Sequence[str]) -> dict[str, Any]:
    """Parse JSON tool arguments; invalid JSON is kept so validation reports it."""
    raw = "".join(fragments).strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"__invalid_json__": raw[:500]}
    return value if isinstance(value, dict) else {"__invalid_json__": raw[:500]}


def stop_reason(finish_reason: str | None, tool_calls: Sequence[ToolCall]) -> StopReason:
    """Map a chat-completions ``finish_reason`` to the neutral stop reason."""
    if finish_reason is None:
        return "tool_use" if tool_calls else "end_turn"
    return FINISH_REASONS.get(finish_reason, "other")
