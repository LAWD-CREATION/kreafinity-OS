"""Mistral adapter: Mistral AI models through the official `mistralai` SDK."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx2
from mistralai.client import Mistral, errors

from kreafinity.providers._chat_format import (
    ToolCallAccumulator,
    stop_reason,
    to_chat_messages,
    to_chat_tools,
)
from kreafinity.providers.base import (
    LLMProvider,
    Message,
    ModelInfo,
    ProviderAuthError,
    ProviderError,
    ProviderUnavailableError,
    ResponseDone,
    StreamEvent,
    TextDelta,
    ToolSpec,
    Usage,
)
from kreafinity.providers.registry import register_provider

DEFAULT_MODEL = "mistral-large-latest"
API_KEY_VARIABLE = "MISTRAL_API_KEY"
# Mistral only accepts tool call ids made of exactly 9 letters or digits.
_MISTRAL_ID = re.compile(r"[A-Za-z0-9]{9}")


def mistral_tool_call_id(tool_call_id: str) -> str:
    """Map any tool call id (e.g. from another provider) to the Mistral format, stably."""
    if _MISTRAL_ID.fullmatch(tool_call_id):
        return tool_call_id
    return hashlib.sha256(tool_call_id.encode()).hexdigest()[:9]


@register_provider
class MistralProvider(LLMProvider):
    """Models from Mistral AI (Paris)."""

    name = "mistral"
    display_name = "Mistral AI"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        server_url: str | None = None,
        default_model: str = DEFAULT_MODEL,
        max_tokens: int | None = None,
        client: Mistral | None = None,
    ) -> None:
        """Create the adapter.

        Args:
            api_key: API key; when omitted, read from ``MISTRAL_API_KEY``.
            server_url: API URL, to use another region or a gateway.
            default_model: Model used when `chat` is not given one.
            max_tokens: Maximum length of a response (no limit by default).
            client: SDK client to use instead of creating one (for tests).
        """
        super().__init__(default_model=default_model)
        self.max_tokens = max_tokens
        self._api_key = api_key
        self._server_url = server_url
        self._client = client

    @property
    def client(self) -> Mistral:
        """The SDK client, created on first use."""
        if self._client is None:
            api_key = self._api_key or os.environ.get(API_KEY_VARIABLE)
            if not api_key:
                raise ProviderAuthError(f"No API key ({API_KEY_VARIABLE})", provider=self.name)
            self._client = Mistral(api_key=api_key, server_url=self._server_url)
        return self._client

    async def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        *,
        model: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        model = model or self.default_model
        params: dict[str, Any] = {
            "model": model,
            "messages": to_chat_messages(
                messages, map_id=mistral_tool_call_id, tool_name_field=True
            ),
        }
        if tools:
            params["tools"] = to_chat_tools(tools)
        if self.max_tokens:
            params["max_tokens"] = self.max_tokens

        text: list[str] = []
        calls = ToolCallAccumulator()
        finish_reason: str | None = None
        served_model = model
        usage = Usage()
        try:
            stream = await self.client.chat.stream_async(**params)
            async with stream as events:
                async for event in events:
                    chunk = event.data
                    served_model = chunk.model or served_model
                    if chunk.usage is not None:
                        usage = Usage(
                            input_tokens=chunk.usage.prompt_tokens,
                            output_tokens=chunk.usage.completion_tokens,
                        )
                    for choice in chunk.choices[:1]:
                        piece = _text_of(choice.delta.content)
                        if piece:
                            text.append(piece)
                            yield TextDelta(piece)
                        for call in choice.delta.tool_calls or []:
                            calls.add(
                                call.index,
                                call_id=call.id if call.id and call.id != "null" else None,
                                name=call.function.name,
                                arguments=call.function.arguments,
                            )
                        finish_reason = choice.finish_reason or finish_reason
        except (httpx2.TransportError, errors.NoResponseError) as exc:
            raise ProviderUnavailableError(
                f"Cannot reach the Mistral API: {exc}", provider=self.name
            ) from exc
        except errors.MistralError as exc:
            raise _map_status_error(exc) from exc

        tool_calls = calls.calls()
        yield ResponseDone(
            message=Message.assistant("".join(text), tool_calls),
            stop_reason=stop_reason(finish_reason, tool_calls),
            model=served_model,
            usage=usage,
        )

    async def list_models(self) -> list[ModelInfo]:
        try:
            result = await self.client.models.list_async()
        except (httpx2.TransportError, errors.NoResponseError) as exc:
            raise ProviderUnavailableError(
                f"Cannot reach the Mistral API: {exc}", provider=self.name
            ) from exc
        except errors.MistralError as exc:
            raise _map_status_error(exc) from exc
        models = []
        for card in result.data or []:
            capabilities = getattr(card, "capabilities", None)
            if capabilities is not None and not getattr(capabilities, "completion_chat", True):
                continue
            card_id = getattr(card, "id", None)
            if card_id:
                name = getattr(card, "name", None) or card_id
                models.append(ModelInfo(id=card_id, provider=self.name, display_name=name))
        return sorted(models, key=lambda m: m.id)


def _text_of(content: Any) -> str:
    """Text of a streamed delta: a string, or a list of chunks (text, thinking…)."""
    if not content:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for chunk in content:
        if getattr(chunk, "type", None) == "text":
            parts.append(getattr(chunk, "text", ""))
    return "".join(parts)


def _map_status_error(exc: errors.MistralError) -> ProviderError:
    provider = MistralProvider.name
    status = exc.status_code
    if status in (401, 403):
        return ProviderAuthError(f"Mistral rejected the credentials: {exc}", provider=provider)
    retryable = status == 429 or status >= 500
    return ProviderError(
        f"Mistral API error ({status}): {exc}", provider=provider, retryable=retryable
    )
