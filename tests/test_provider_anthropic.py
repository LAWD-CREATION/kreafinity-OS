"""Tests of the Anthropic adapter against the real SDK, with a mocked HTTP layer."""

import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest

from kreafinity.providers import (
    Message,
    ProviderAuthError,
    ProviderError,
    ProviderUnavailableError,
    ResponseDone,
    TextDelta,
    ToolCall,
    ToolSpec,
)
from kreafinity.providers.anthropic import FALLBACK_BETA, AnthropicProvider

Handler = Callable[[httpx2.Request], httpx2.Response]

LIST_DIR = ToolSpec(
    name="list_directory",
    description="Liste le contenu d'un dossier.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
)


class FakeAPI:
    """Records requests and answers with canned responses."""

    def __init__(self, *responses: httpx2.Response | Handler) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        return response(request) if callable(response) else response

    def body(self, index: int = -1) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.requests[index].content)
        return result

    def provider(self, **options: Any) -> AnthropicProvider:
        client = anthropic.AsyncAnthropic(
            api_key="test-key",
            base_url="https://api.anthropic.test",
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self)),
        )
        return AnthropicProvider(client=client, **options)


def sse_response(
    blocks: list[dict[str, Any]],
    *,
    stop_reason: str = "end_turn",
    model: str = "claude-opus-5-5",
) -> httpx2.Response:
    """Build a streamed Messages API response from complete content blocks.

    Text and thinking are sent as deltas; tool inputs as JSON fragments.
    """
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 42, "output_tokens": 1},
            },
        }
    ]
    for index, block in enumerate(blocks):
        start = dict(block)
        deltas: list[dict[str, Any]] = []
        if block["type"] == "text":
            start["text"] = ""
            deltas.append({"type": "text_delta", "text": block["text"]})
        elif block["type"] == "thinking":
            start.update(thinking="", signature="")
            deltas.append({"type": "thinking_delta", "thinking": block["thinking"]})
            deltas.append({"type": "signature_delta", "signature": block["signature"]})
        elif block["type"] == "tool_use":
            start["input"] = {}
            deltas.append({"type": "input_json_delta", "partial_json": json.dumps(block["input"])})
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        events.extend(
            {"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas
        )
        events.append({"type": "content_block_stop", "index": index})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 17},
        }
    )
    events.append({"type": "message_stop"})
    payload = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=payload.encode()
    )


async def collect(provider: AnthropicProvider, *args: Any, **kwargs: Any) -> list[Any]:
    return [event async for event in provider.chat(*args, **kwargs)]


async def test_streams_text_with_system_prompt_and_fallback() -> None:
    api = FakeAPI(sse_response([{"type": "text", "text": "Bonjour !"}]))
    events = await collect(
        api.provider(), [Message.system("Tu es Kreafinity."), Message.user("Salut")]
    )

    assert events[0] == TextDelta("Bonjour !")
    done = events[-1]
    assert isinstance(done, ResponseDone)
    assert done.message.content == "Bonjour !"
    assert done.message.tool_calls == ()
    assert done.stop_reason == "end_turn"
    assert done.model == "claude-opus-5-5"
    assert (done.usage.input_tokens, done.usage.output_tokens) == (42, 17)

    request = api.requests[0]
    assert request.url.path == "/v1/messages"
    assert FALLBACK_BETA in request.headers["anthropic-beta"]
    body = api.body()
    assert body["model"] == "claude-opus-5-5"
    assert body["stream"] is True
    assert body["system"] == "Tu es Kreafinity."
    assert body["fallbacks"] == "default"
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Salut"}]}]
    assert "tools" not in body


async def test_no_fallback_for_models_without_it() -> None:
    api = FakeAPI(sse_response([{"type": "text", "text": "ok"}], model="claude-haiku-4-5"))
    await collect(api.provider(), [Message.user("Salut")], model="claude-haiku-4-5")
    assert "fallbacks" not in api.body()
    assert "anthropic-beta" not in api.requests[0].headers


async def test_tool_use_round_trip_replays_thinking_unchanged() -> None:
    thinking = {"type": "thinking", "thinking": "", "signature": "sig-abc"}
    tool_use = {
        "type": "tool_use",
        "id": "toolu_01",
        "name": "list_directory",
        "input": {"path": "~/Téléchargements"},
    }
    api = FakeAPI(
        sse_response(
            [thinking, {"type": "text", "text": "Je regarde."}, tool_use], stop_reason="tool_use"
        ),
        sse_response([{"type": "text", "text": "Voici le contenu."}]),
    )
    provider = api.provider()
    history = [Message.user("Que contient Téléchargements ?")]

    done = (await collect(provider, history, [LIST_DIR]))[-1]
    assert done.stop_reason == "tool_use"
    assert done.message.tool_calls == (
        ToolCall(id="toolu_01", name="list_directory", arguments={"path": "~/Téléchargements"}),
    )
    assert api.body(0)["tools"] == [
        {
            "name": "list_directory",
            "description": "Liste le contenu d'un dossier.",
            "input_schema": LIST_DIR.parameters,
        }
    ]

    history += [
        done.message,
        Message.tool_result("toolu_01", "list_directory", "facture.pdf"),
    ]
    await collect(provider, history, [LIST_DIR])

    messages = api.body(1)["messages"]
    assert messages[1] == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig-abc"},
            {"type": "text", "text": "Je regarde."},
            tool_use,
        ],
    }
    assert messages[2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_01", "content": "facture.pdf"}],
    }


async def test_history_from_another_provider() -> None:
    api = FakeAPI(sse_response([{"type": "text", "text": "ok"}]))
    calls = [
        ToolCall(id="call_a", name="system_info", arguments={}),
        ToolCall(id="call_b", name="list_directory", arguments={"path": "~"}),
    ]
    history = [
        Message.user("Infos ?"),
        Message.assistant("", calls),  # produced by e.g. Ollama: no provider data
        Message.tool_result("call_a", "system_info", "CPU 4 cœurs"),
        Message.tool_result("call_b", "list_directory", "denied", is_error=True),
        Message.assistant("Voilà."),
        Message.user("Merci"),
    ]
    await collect(api.provider(), history)

    assert api.body()["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Infos ?"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_a", "name": "system_info", "input": {}},
                {
                    "type": "tool_use",
                    "id": "call_b",
                    "name": "list_directory",
                    "input": {"path": "~"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_a", "content": "CPU 4 cœurs"},
                {
                    "type": "tool_result",
                    "tool_use_id": "call_b",
                    "content": "denied",
                    "is_error": True,
                },
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "Voilà."}]},
        {"role": "user", "content": [{"type": "text", "text": "Merci"}]},
    ]


async def test_mid_response_fallback_drops_declined_blocks() -> None:
    declined_tool = {"type": "tool_use", "id": "toolu_old", "name": "list_directory", "input": {}}
    fallback = {
        "type": "fallback",
        "from": {"model": "claude-opus-5-5"},
        "to": {"model": "claude-opus-4-8"},
        "trigger": {"type": "refusal", "category": None},
    }
    live_tool = {
        "type": "tool_use",
        "id": "toolu_new",
        "name": "list_directory",
        "input": {"path": "~"},
    }
    api = FakeAPI(
        sse_response(
            [
                {"type": "thinking", "thinking": "", "signature": "sig-old"},
                {"type": "text", "text": "Je "},
                declined_tool,
                fallback,
                {"type": "text", "text": "regarde."},
                live_tool,
            ],
            stop_reason="tool_use",
            model="claude-opus-4-8",
        )
    )
    done = (await collect(api.provider(), [Message.user("Liste ~")], [LIST_DIR]))[-1]

    assert done.model == "claude-opus-4-8"
    assert done.message.content == "Je regarde."
    assert [c.id for c in done.message.tool_calls] == ["toolu_new"]
    replay = done.message.provider_data["anthropic"]["content"]
    assert [b["type"] for b in replay] == ["text", "fallback", "text", "tool_use"]
    assert replay[1]["from"] == {"model": "claude-opus-5-5"}


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [("max_tokens", "max_tokens"), ("refusal", "refusal"), ("pause_turn", "other")],
)
async def test_stop_reasons(stop_reason: str, expected: str) -> None:
    api = FakeAPI(sse_response([{"type": "text", "text": "…"}], stop_reason=stop_reason))
    done = (await collect(api.provider(), [Message.user("Salut")]))[-1]
    assert done.stop_reason == expected


async def test_invalid_key() -> None:
    api = FakeAPI(
        httpx2.Response(
            401,
            json={
                "type": "error",
                "error": {"type": "authentication_error", "message": "invalid x-api-key"},
            },
        )
    )
    with pytest.raises(ProviderAuthError):
        await collect(api.provider(), [Message.user("Salut")])


@pytest.mark.parametrize(("status", "retryable"), [(529, True), (429, True), (400, False)])
async def test_api_errors(status: int, retryable: bool) -> None:
    api = FakeAPI(
        httpx2.Response(
            status, json={"type": "error", "error": {"type": "api_error", "message": "nope"}}
        )
    )
    with pytest.raises(ProviderError) as info:
        await collect(api.provider(), [Message.user("Salut")])
    assert info.value.retryable is retryable
    assert not isinstance(info.value, ProviderAuthError)


async def test_offline() -> None:
    def offline(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("Network is unreachable", request=request)

    api = FakeAPI(offline, offline)
    provider = api.provider()
    with pytest.raises(ProviderUnavailableError):
        await collect(provider, [Message.user("Salut")])
    with pytest.raises(ProviderUnavailableError):
        await provider.list_models()


async def test_list_models() -> None:
    model = {
        "type": "model",
        "id": "claude-opus-5-5",
        "display_name": "Claude Opus 5.5",
        "created_at": "2026-10-01T00:00:00Z",
        "lifecycle": "active",
    }
    api = FakeAPI(
        httpx2.Response(
            200,
            json={
                "data": [model],
                "has_more": False,
                "first_id": "claude-opus-5-5",
                "last_id": "claude-opus-5-5",
            },
        )
    )
    models = await api.provider().list_models()
    assert [(m.id, m.provider, m.display_name) for m in models] == [
        ("claude-opus-5-5", "anthropic", "Claude Opus 5.5")
    ]
