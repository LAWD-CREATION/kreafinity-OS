"""Ollama adapter: local models through the Ollama HTTP API.

API reference: https://github.com/ollama/ollama/blob/main/docs/api.md
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from kreafinity.providers.base import (
    LLMProvider,
    Message,
    ModelInfo,
    ProviderError,
    ProviderUnavailableError,
    ResponseDone,
    StopReason,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
    Usage,
    new_tool_call_id,
)
from kreafinity.providers.registry import register_provider

DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "llama3.1"
# Local models can take a long time to load and to produce each token.
DEFAULT_TIMEOUT = httpx.Timeout(10.0, read=300.0)


@register_provider
class OllamaProvider(LLMProvider):
    """Models served by a local (or LAN) Ollama server."""

    name = "ollama"
    display_name = "Ollama (local)"
    is_local = True

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        default_model: str = DEFAULT_MODEL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create the adapter.

        Args:
            base_url: URL of the Ollama server.
            default_model: Model used when `chat` is not given one.
            client: HTTP client to use instead of creating one (for tests).
        """
        super().__init__(default_model=default_model)
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)

    async def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        *,
        model: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        model = model or self.default_model
        payload: dict[str, Any] = {
            "model": model,
            "messages": [_to_ollama_message(m) for m in messages],
            "stream": True,
        }
        if tools:
            payload["tools"] = [_to_ollama_tool(t) for t in tools]

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        try:
            async with self._client.stream(
                "POST", f"{self.base_url}/api/chat", json=payload
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise ProviderError(
                        f"Ollama returned HTTP {response.status_code}: {_error_detail(response)}",
                        provider=self.name,
                        retryable=response.status_code >= 500,
                    )
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = _parse_chunk(line, provider=self.name)
                    piece = chunk.get("message") or {}
                    text = piece.get("content") or ""
                    if text:
                        text_parts.append(text)
                        yield TextDelta(text)
                    for raw_call in piece.get("tool_calls") or []:
                        tool_calls.append(_parse_tool_call(raw_call))
                    if chunk.get("done"):
                        yield ResponseDone(
                            message=Message.assistant("".join(text_parts), tool_calls),
                            stop_reason=_stop_reason(chunk.get("done_reason"), tool_calls),
                            model=chunk.get("model") or model,
                            usage=Usage(
                                input_tokens=chunk.get("prompt_eval_count"),
                                output_tokens=chunk.get("eval_count"),
                            ),
                        )
                        return
        except httpx.TransportError as exc:
            raise ProviderUnavailableError(
                f"Cannot reach Ollama at {self.base_url}: {exc}", provider=self.name
            ) from exc
        raise ProviderError("Ollama closed the stream before finishing", provider=self.name)

    async def list_models(self) -> list[ModelInfo]:
        try:
            response = await self._client.get(f"{self.base_url}/api/tags")
        except httpx.TransportError as exc:
            raise ProviderUnavailableError(
                f"Cannot reach Ollama at {self.base_url}: {exc}", provider=self.name
            ) from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"Ollama returned HTTP {response.status_code}: {_error_detail(response)}",
                provider=self.name,
            )
        return [
            ModelInfo(id=entry["name"], provider=self.name, display_name=entry["name"])
            for entry in response.json().get("models", [])
        ]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _to_ollama_message(message: Message) -> dict[str, Any]:
    if message.role == "tool":
        result: dict[str, Any] = {"role": "tool", "content": message.content}
        if message.name:
            result["tool_name"] = message.name
        return result
    result = {"role": message.role, "content": message.content}
    if message.tool_calls:
        result["tool_calls"] = [
            {"function": {"name": call.name, "arguments": call.arguments}}
            for call in message.tool_calls
        ]
    return result


def _to_ollama_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _parse_chunk(line: str, *, provider: str) -> dict[str, Any]:
    try:
        chunk = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"Invalid data from Ollama: {line[:200]!r}", provider=provider) from exc
    if not isinstance(chunk, dict):
        raise ProviderError(f"Invalid data from Ollama: {line[:200]!r}", provider=provider)
    if "error" in chunk:
        raise ProviderError(f"Ollama error: {chunk['error']}", provider=provider)
    return chunk


def _parse_tool_call(raw: dict[str, Any]) -> ToolCall:
    function = raw.get("function") or {}
    arguments = function.get("arguments") or {}
    if isinstance(arguments, str):  # some models return a JSON string
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return ToolCall(
        id=raw.get("id") or new_tool_call_id(),
        name=function.get("name", ""),
        arguments=arguments,
    )


def _stop_reason(done_reason: str | None, tool_calls: Sequence[ToolCall]) -> StopReason:
    if tool_calls:
        return "tool_use"
    if done_reason == "length":
        return "max_tokens"
    if done_reason in (None, "stop"):
        return "end_turn"
    return "other"


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict) and "error" in body:
        return str(body["error"])
    return response.text[:200]
