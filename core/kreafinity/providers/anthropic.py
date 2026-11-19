"""Anthropic adapter: Claude models through the official `anthropic` SDK."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import anthropic

from kreafinity.providers.base import (
    LLMProvider,
    Message,
    ModelInfo,
    ProviderAuthError,
    ProviderError,
    ProviderUnavailableError,
    ResponseDone,
    StopReason,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
    Usage,
)
from kreafinity.providers.registry import register_provider

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_MAX_TOKENS = 64_000

# Server-side refusal fallback: if a safety classifier declines a request, the
# API re-runs it on a suitable model within the same call instead of stopping.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = frozenset(
    {
        "claude-fable-5-2",
        "claude-fable-5-1",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-sonnet-5-5",
    }
)

_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
    "refusal": "refusal",
}


@register_provider
class AnthropicProvider(LLMProvider):
    """Claude models from Anthropic."""

    name = "anthropic"
    display_name = "Claude (Anthropic)"
    requires_api_key = True
    api_key_env_var = "ANTHROPIC_API_KEY"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        """Create the adapter.

        Args:
            api_key: API key; when omitted the SDK resolves credentials itself
                (``ANTHROPIC_API_KEY``, ``ant auth login`` profile…).
            default_model: Model used when `chat` is not given one.
            max_tokens: Maximum length of a response, in tokens.
            client: SDK client to use instead of creating one (for tests).
        """
        super().__init__(default_model=default_model)
        self.max_tokens = max_tokens
        self._api_key = api_key
        self._client = client

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        """The SDK client, created on first use."""
        if self._client is None:
            try:
                self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
            except anthropic.AnthropicError as exc:
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
        system, api_messages = _to_anthropic_messages(messages)
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": api_messages,
        }
        if system:
            params["system"] = system
        if tools:
            # `eager_input_streaming` is deliberately left off: tool inputs are
            # not displayed while they stream, and server-side validation of
            # the arguments matters for tools that act on the user's machine.
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        if model in FALLBACK_MODELS:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"

        try:
            async with self.client.beta.messages.stream(**params) as stream:
                async for event in stream:
                    if event.type == "text" and event.text:
                        yield TextDelta(event.text)
                final = await stream.get_final_message()
        except anthropic.APIError as exc:
            raise _map_error(exc) from exc

        raw_blocks = [
            block.model_dump(mode="json", by_alias=True, exclude_none=True)
            for block in final.content
        ]
        replay_blocks, live_blocks = _split_at_fallback(raw_blocks)
        text = "".join(b["text"] for b in raw_blocks if b["type"] == "text")
        tool_calls = [
            ToolCall(id=b["id"], name=b["name"], arguments=dict(b.get("input") or {}))
            for b in live_blocks
            if b["type"] == "tool_use"
        ]
        message = Message(
            role="assistant",
            content=text,
            tool_calls=tuple(tool_calls),
            provider_data={self.name: {"model": final.model, "content": replay_blocks}},
        )
        yield ResponseDone(
            message=message,
            stop_reason=_STOP_REASONS.get(final.stop_reason or "", "other"),
            model=final.model,
            usage=Usage(
                input_tokens=final.usage.input_tokens,
                output_tokens=final.usage.output_tokens,
            ),
        )

    async def list_models(self) -> list[ModelInfo]:
        try:
            return [
                ModelInfo(id=m.id, provider=self.name, display_name=m.display_name)
                async for m in self.client.models.list()
            ]
        except anthropic.APIError as exc:
            raise _map_error(exc) from exc

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()


def _to_anthropic_messages(
    messages: Sequence[Message],
) -> tuple[str, list[dict[str, Any]]]:
    """Convert neutral messages to the Messages API ``system`` and ``messages``."""
    system_parts: list[str] = []
    result: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            system_parts.append(message.content)
        elif message.role == "tool":
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.content,
            }
            if message.is_error:
                block["is_error"] = True
            # All results of one assistant turn go back in a single user message.
            if result and result[-1].get("_tool_results"):
                result[-1]["content"].append(block)
            else:
                result.append({"role": "user", "content": [block], "_tool_results": True})
        elif message.role == "assistant":
            content = _assistant_content(message)
            if content:
                result.append({"role": "assistant", "content": content})
        else:
            result.append({"role": "user", "content": [{"type": "text", "text": message.content}]})
    for entry in result:
        entry.pop("_tool_results", None)
    return "\n\n".join(p for p in system_parts if p), result


def _assistant_content(message: Message) -> list[dict[str, Any]]:
    # Replay our own blocks unchanged (thinking blocks must not be altered).
    data = message.provider_data.get(AnthropicProvider.name)
    if isinstance(data, dict) and data.get("content"):
        return list(data["content"])
    content: list[dict[str, Any]] = []
    if message.content:
        content.append({"type": "text", "text": message.content})
    content.extend(
        {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
        for call in message.tool_calls
    )
    return content


def _split_at_fallback(
    blocks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Handle a mid-response refusal fallback.

    Returns the blocks to replay in later requests and the blocks produced
    by the model that finally served the response. Before the last
    ``fallback`` block, only text and the fallback markers themselves may be
    echoed back (the declined model's thinking and tool calls are dropped,
    and those tool calls must not run).
    """
    boundary = max((i for i, b in enumerate(blocks) if b["type"] == "fallback"), default=-1)
    if boundary < 0:
        return blocks, blocks
    before = [b for b in blocks[:boundary] if b["type"] in ("text", "fallback")]
    after = blocks[boundary + 1 :]
    return [*before, blocks[boundary], *after], after


def _map_error(exc: anthropic.APIError) -> ProviderError:
    provider = AnthropicProvider.name
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderUnavailableError(f"Cannot reach the Anthropic API: {exc}", provider=provider)
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return ProviderAuthError(f"Anthropic rejected the credentials: {exc}", provider=provider)
    if isinstance(exc, anthropic.APIStatusError):
        retryable = exc.status_code == 429 or exc.status_code >= 500
        return ProviderError(f"Anthropic API error: {exc}", provider=provider, retryable=retryable)
    return ProviderError(f"Anthropic API error: {exc}", provider=provider)
