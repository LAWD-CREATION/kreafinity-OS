"""Test doubles shared by the test modules."""

from collections.abc import AsyncIterator, Sequence
from typing import Any

from kreafinity.providers import (
    LLMProvider,
    Message,
    ModelInfo,
    ProviderError,
    ResponseDone,
    StopReason,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
)


def reply(
    text: str = "",
    calls: Sequence[ToolCall] = (),
    *,
    stop: StopReason | None = None,
    model: str = "fake-model",
) -> list[StreamEvent]:
    """Events of one scripted LLM response."""
    stop_reason: StopReason = stop or ("tool_use" if calls else "end_turn")
    events: list[StreamEvent] = [TextDelta(text)] if text else []
    events.append(
        ResponseDone(message=Message.assistant(text, calls), stop_reason=stop_reason, model=model)
    )
    return events


def call(name: str, call_id: str = "call_1", **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


class ScriptedProvider(LLMProvider):
    """Provider replaying scripted responses and recording what it receives.

    Each item of `responses` is the event list of one `chat` call, or an
    exception to raise. When the script runs out, the last response repeats.
    """

    name = "scripted"
    display_name = "Scripted"

    def __init__(self, *responses: list[StreamEvent] | Exception) -> None:
        super().__init__(default_model="fake-model")
        self.responses = list(responses)
        self.requests: list[tuple[list[Message], list[ToolSpec], str | None]] = []
        self.closed = False

    async def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        *,
        model: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.requests.append((list(messages), list(tools), model))
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        for event in response:
            yield event

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id="fake-model", provider=self.name)]

    async def aclose(self) -> None:
        self.closed = True


__all__ = ["ProviderError", "ScriptedProvider", "call", "reply"]
