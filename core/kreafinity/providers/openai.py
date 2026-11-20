"""OpenAI adapter: GPT models through the official `openai` SDK (Chat Completions).

The Chat Completions API is also offered by many compatible servers
(LM Studio, vLLM, OpenRouter…): set ``base_url`` to use one of them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import openai

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

DEFAULT_MODEL = "gpt-5-mini"
# Model families listed by `list_models` on api.openai.com (other ids are
# embeddings, audio, image models…).
CHAT_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-")


@register_provider
class OpenAIProvider(LLMProvider):
    """GPT models from OpenAI, or any server speaking the Chat Completions API."""

    name = "openai"
    display_name = "GPT (OpenAI)"
    requires_api_key = True
    api_key_env_var = "OPENAI_API_KEY"
    #: Name of the response length parameter in this API.
    max_tokens_parameter = "max_completion_tokens"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: str = DEFAULT_MODEL,
        max_tokens: int | None = None,
        client: openai.AsyncOpenAI | None = None,
        http_client: Any = None,
    ) -> None:
        """Create the adapter.

        Args:
            api_key: API key; when omitted the SDK reads ``OPENAI_API_KEY``.
            base_url: URL of a compatible server instead of api.openai.com.
            default_model: Model used when `chat` is not given one.
            max_tokens: Maximum length of a response (no limit by default).
            client: SDK client to use instead of creating one (for tests).
            http_client: HTTP client given to the SDK (``openai.DefaultAsyncHttpxClient``).
        """
        super().__init__(default_model=default_model)
        self.max_tokens = max_tokens
        self._api_key = api_key
        self._base_url = base_url
        self._client = client
        self._http_client = http_client

    @property
    def client(self) -> openai.AsyncOpenAI:
        """The SDK client, created on first use."""
        if self._client is None:
            try:
                self._client = openai.AsyncOpenAI(
                    api_key=self._api_key, base_url=self._base_url, http_client=self._http_client
                )
            except openai.OpenAIError as exc:
                raise ProviderAuthError(str(exc), provider=self.name) from exc
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
            "messages": to_chat_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            params["tools"] = to_chat_tools(tools)
        if self.max_tokens:
            params[self.max_tokens_parameter] = self.max_tokens

        text: list[str] = []
        calls = ToolCallAccumulator()
        finish_reason: str | None = None
        served_model = model
        usage = Usage()
        try:
            stream = await self.client.chat.completions.create(**params)
            async for chunk in stream:
                served_model = chunk.model or served_model
                if chunk.usage is not None:
                    usage = Usage(
                        input_tokens=chunk.usage.prompt_tokens,
                        output_tokens=chunk.usage.completion_tokens,
                    )
                for choice in chunk.choices[:1]:
                    delta = choice.delta
                    piece = (delta.content or "") + (delta.refusal or "")
                    if piece:
                        text.append(piece)
                        yield TextDelta(piece)
                    for call in delta.tool_calls or []:
                        function = call.function
                        calls.add(
                            call.index,
                            call_id=call.id,
                            name=function.name if function else None,
                            arguments=function.arguments if function else None,
                        )
                    finish_reason = choice.finish_reason or finish_reason
        except openai.APIError as exc:
            raise _map_error(exc, self) from exc

        tool_calls = calls.calls()
        yield ResponseDone(
            message=Message.assistant("".join(text), tool_calls),
            stop_reason=stop_reason(finish_reason, tool_calls),
            model=served_model,
            usage=usage,
        )

    async def list_models(self) -> list[ModelInfo]:
        try:
            ids = sorted([m.id async for m in self.client.models.list()])
        except openai.APIError as exc:
            raise _map_error(exc, self) from exc
        if self._base_url is None:
            ids = [i for i in ids if i.startswith(CHAT_MODEL_PREFIXES)]
        return [ModelInfo(id=i, provider=self.name, display_name=i) for i in ids]

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()


def _map_error(exc: openai.APIError, adapter: LLMProvider) -> ProviderError:
    provider, label = adapter.name, adapter.display_name
    if isinstance(exc, openai.APIConnectionError):
        return ProviderUnavailableError(f"Cannot reach {label}: {exc}", provider=provider)
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return ProviderAuthError(f"{label} rejected the credentials: {exc}", provider=provider)
    if isinstance(exc, openai.APIStatusError):
        retryable = exc.status_code == 429 or exc.status_code >= 500
        return ProviderError(f"{label} API error: {exc}", provider=provider, retryable=retryable)
    return ProviderError(f"{label} API error: {exc}", provider=provider)
