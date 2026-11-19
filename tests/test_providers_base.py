from collections.abc import AsyncIterator, Sequence

import pytest

from kreafinity.providers import (
    LLMProvider,
    Message,
    ModelInfo,
    ProviderError,
    ResponseDone,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
    available_providers,
    create_provider,
    get_provider_class,
    parse_model_spec,
    register_provider,
)
from kreafinity.providers.anthropic import AnthropicProvider
from kreafinity.providers.ollama import OllamaProvider


class ScriptedProvider(LLMProvider):
    """Provider replaying a fixed list of events (not registered)."""

    name = "scripted"
    display_name = "Scripted"

    def __init__(self, events: list[StreamEvent]) -> None:
        super().__init__(default_model="scripted-1")
        self.events = events

    async def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        *,
        model: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        for event in self.events:
            yield event

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id="scripted-1", provider=self.name)]


def test_message_helpers() -> None:
    call = ToolCall(id="call_1", name="system_info", arguments={})
    assert Message.system("s").role == "system"
    assert Message.user("u").content == "u"
    assistant = Message.assistant("", [call])
    assert assistant.tool_calls == (call,)
    result = Message.tool_result("call_1", "system_info", "boom", is_error=True)
    assert (result.role, result.tool_call_id, result.name, result.is_error) == (
        "tool",
        "call_1",
        "system_info",
        True,
    )


def test_builtin_providers_are_discovered() -> None:
    assert {"anthropic", "ollama"} <= set(available_providers())
    assert get_provider_class("ollama") is OllamaProvider
    assert get_provider_class("anthropic") is AnthropicProvider
    assert OllamaProvider.is_local and not AnthropicProvider.is_local


def test_create_provider_passes_options() -> None:
    provider = create_provider("ollama", default_model="mistral")
    assert isinstance(provider, OllamaProvider)
    assert provider.default_model == "mistral"


def test_unknown_provider() -> None:
    with pytest.raises(KeyError, match="nope"):
        get_provider_class("nope")


def test_duplicate_provider_name_is_rejected() -> None:
    class Impostor(ScriptedProvider):
        name = "ollama"

    with pytest.raises(ValueError, match="already registered"):
        register_provider(Impostor)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("ollama/llama3.1", ("ollama", "llama3.1")),
        ("anthropic", ("anthropic", None)),
        ("ollama/hf.co/org/model:Q4", ("ollama", "hf.co/org/model:Q4")),
        (" anthropic/claude-opus-5-5 ", ("anthropic", "claude-opus-5-5")),
    ],
)
def test_parse_model_spec(spec: str, expected: tuple[str, str | None]) -> None:
    assert parse_model_spec(spec) == expected


def test_parse_model_spec_rejects_empty_provider() -> None:
    with pytest.raises(ValueError):
        parse_model_spec("/llama3.1")


async def test_complete_returns_final_event() -> None:
    done = ResponseDone(message=Message.assistant("Salut"), stop_reason="end_turn", model="m")
    provider = ScriptedProvider([TextDelta("Sa"), TextDelta("lut"), done])
    assert await provider.complete([Message.user("Bonjour")]) is done


async def test_complete_without_final_event_fails() -> None:
    provider = ScriptedProvider([TextDelta("Sa")])
    with pytest.raises(ProviderError):
        await provider.complete([Message.user("Bonjour")])
