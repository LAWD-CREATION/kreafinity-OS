"""Tests of the OpenAI and Mistral adapters against their real SDKs, mocked at the HTTP level."""

import json
from collections.abc import Callable
from typing import Any

import httpx2
import openai
import pytest
from mistralai.client import Mistral

from kreafinity.providers import (
    LLMProvider,
    Message,
    ProviderAuthError,
    ProviderError,
    ProviderUnavailableError,
    ResponseDone,
    TextDelta,
    ToolCall,
    ToolSpec,
)
from kreafinity.providers.mistral import MistralProvider, mistral_tool_call_id
from kreafinity.providers.openai import OpenAIProvider

LIST_DIR = ToolSpec(
    name="list_directory",
    description="Liste le contenu d'un dossier.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
)

Handler = Callable[[httpx2.Request], httpx2.Response]


class FakeAPI:
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


def openai_provider(api: FakeAPI, **options: Any) -> OpenAIProvider:
    client = openai.AsyncOpenAI(
        api_key="test-key",
        base_url="https://api.openai.test/v1",
        max_retries=0,
        http_client=openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(api)),
    )
    return OpenAIProvider(client=client, **options)


def mistral_provider(api: FakeAPI, **options: Any) -> MistralProvider:
    client = Mistral(
        api_key="test-key",
        server_url="https://api.mistral.test",
        async_client=httpx2.AsyncClient(transport=httpx2.MockTransport(api)),
    )
    return MistralProvider(client=client, **options)


def sse(*chunks: dict[str, Any]) -> httpx2.Response:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=body.encode()
    )


def chunk(
    model: str,
    delta: dict[str, Any] | None = None,
    finish: str | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "chunk-1",
        "object": "chat.completion.chunk",
        "created": 1_700_000_000,
        "model": model,
        "choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage:
        data["usage"] = usage
    return data


async def collect(provider: LLMProvider, *args: Any, **kwargs: Any) -> list[Any]:
    return [event async for event in provider.chat(*args, **kwargs)]


HISTORY_FROM_CLAUDE = [
    Message.system("Tu es Kreafinity."),
    Message.user("Que contient /tmp ?"),
    Message.assistant(
        "Je regarde.", [ToolCall(id="toolu_01ABC", name="list_directory", arguments={"path": "/"})]
    ),
    Message.tool_result("toolu_01ABC", "list_directory", "denied", is_error=True),
    Message.user("Et ~ ?"),
]


# --- OpenAI -----------------------------------------------------------------------


async def test_openai_streams_text() -> None:
    model = "gpt-5-mini"
    api = FakeAPI(
        sse(
            chunk(model, {"role": "assistant", "content": "Bon"}),
            chunk(model, {"content": "jour"}),
            chunk(model, {}, finish="stop"),
            chunk(model, usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}),
        )
    )
    events = await collect(openai_provider(api), [Message.system("S"), Message.user("Salut")])

    assert events[:2] == [TextDelta("Bon"), TextDelta("jour")]
    done = events[-1]
    assert isinstance(done, ResponseDone)
    assert done.message == Message.assistant("Bonjour")
    assert done.stop_reason == "end_turn"
    assert (done.usage.input_tokens, done.usage.output_tokens) == (12, 3)
    body = api.body()
    assert api.requests[0].url.path == "/v1/chat/completions"
    assert body["model"] == model
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"] == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "Salut"},
    ]
    assert "tools" not in body and "max_completion_tokens" not in body


async def test_openai_streamed_tool_calls_and_history() -> None:
    model = "gpt-5-mini"
    api = FakeAPI(
        sse(
            chunk(
                model,
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_A",
                            "type": "function",
                            "function": {"name": "list_directory", "arguments": ""},
                        }
                    ]
                },
            ),
            chunk(model, {"tool_calls": [{"index": 0, "function": {"arguments": '{"path"'}}]}),
            chunk(model, {"tool_calls": [{"index": 0, "function": {"arguments": ': "~"}'}}]}),
            chunk(
                model,
                {
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "call_B",
                            "type": "function",
                            "function": {"name": "system_info", "arguments": "{}"},
                        }
                    ]
                },
            ),
            chunk(model, {}, finish="tool_calls"),
        )
    )
    events = await collect(openai_provider(api, max_tokens=500), HISTORY_FROM_CLAUDE, [LIST_DIR])

    done = events[-1]
    assert done.stop_reason == "tool_use"
    assert done.message.tool_calls == (
        ToolCall(id="call_A", name="list_directory", arguments={"path": "~"}),
        ToolCall(id="call_B", name="system_info", arguments={}),
    )
    body = api.body()
    assert body["max_completion_tokens"] == 500
    assert body["tools"][0] == {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "Liste le contenu d'un dossier.",
            "parameters": LIST_DIR.parameters,
        },
    }
    assert body["messages"][2:4] == [
        {
            "role": "assistant",
            "content": "Je regarde.",
            "tool_calls": [
                {
                    "id": "toolu_01ABC",
                    "type": "function",
                    "function": {"name": "list_directory", "arguments": '{"path": "/"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_01ABC", "content": "Error: denied"},
    ]


async def test_openai_invalid_tool_arguments_are_kept_for_validation() -> None:
    model = "gpt-5-mini"
    api = FakeAPI(
        sse(
            chunk(
                model,
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_A",
                            "type": "function",
                            "function": {"name": "list_directory", "arguments": '{"path": '},
                        }
                    ]
                },
            ),
            chunk(model, {}, finish="tool_calls"),
        )
    )
    done = (await collect(openai_provider(api), [Message.user("x")], [LIST_DIR]))[-1]
    assert done.message.tool_calls[0].arguments == {"__invalid_json__": '{"path":'}


@pytest.mark.parametrize(
    ("finish", "expected"), [("length", "max_tokens"), ("content_filter", "refusal")]
)
async def test_openai_stop_reasons(finish: str, expected: str) -> None:
    api = FakeAPI(sse(chunk("m", {"content": "…"}, finish=finish)))
    done = (await collect(openai_provider(api), [Message.user("x")]))[-1]
    assert done.stop_reason == expected


@pytest.mark.parametrize(
    ("status", "error", "retryable"),
    [(401, ProviderAuthError, False), (429, ProviderError, True), (500, ProviderError, True)],
)
async def test_openai_errors(status: int, error: type[ProviderError], retryable: bool) -> None:
    api = FakeAPI(httpx2.Response(status, json={"error": {"message": "nope", "type": "x"}}))
    with pytest.raises(error) as info:
        await collect(openai_provider(api), [Message.user("x")])
    assert info.value.retryable is retryable


async def test_openai_offline() -> None:
    def offline(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("Network is unreachable", request=request)

    with pytest.raises(ProviderUnavailableError):
        await collect(openai_provider(FakeAPI(offline)), [Message.user("x")])


async def test_openai_list_models() -> None:
    api = FakeAPI(
        httpx2.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": i, "object": "model", "created": 1, "owned_by": "openai"}
                    for i in ("gpt-5-mini", "text-embedding-3-small", "o3", "dall-e-3")
                ],
            },
        ),
        httpx2.Response(200, json={"object": "list", "data": []}),
    )
    models = await openai_provider(api).list_models()
    assert [m.id for m in models] == ["gpt-5-mini", "o3"]


def test_openai_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ProviderAuthError):
        _ = OpenAIProvider().client


# --- Mistral --------------------------------------------------------------------------


async def test_mistral_streams_text_and_tool_calls() -> None:
    model = "mistral-large-latest"
    api = FakeAPI(
        sse(
            chunk(model, {"role": "assistant", "content": "Je regarde."}),
            chunk(
                model,
                {
                    "tool_calls": [
                        {
                            "id": "abcDEF123",
                            "function": {"name": "list_directory", "arguments": '{"path": "~"}'},
                            "index": 0,
                        }
                    ]
                },
                finish="tool_calls",
                usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
            ),
        )
    )
    events = await collect(mistral_provider(api), HISTORY_FROM_CLAUDE, [LIST_DIR])

    assert events[0] == TextDelta("Je regarde.")
    done = events[-1]
    assert done.stop_reason == "tool_use"
    assert done.message.tool_calls == (
        ToolCall(id="abcDEF123", name="list_directory", arguments={"path": "~"}),
    )
    assert (done.usage.input_tokens, done.usage.output_tokens) == (20, 8)

    body = api.body()
    assert api.requests[0].url.path == "/v1/chat/completions"
    assert body["model"] == model and body["stream"] is True
    mapped = mistral_tool_call_id("toolu_01ABC")
    assert len(mapped) == 9 and mapped.isalnum()
    assert body["messages"][2]["tool_calls"][0]["id"] == mapped
    assert body["messages"][3] == {
        "role": "tool",
        "tool_call_id": mapped,
        "name": "list_directory",
        "content": "Error: denied",
    }


def test_mistral_tool_call_ids() -> None:
    assert mistral_tool_call_id("abcDEF123") == "abcDEF123"
    assert mistral_tool_call_id("call_123") == mistral_tool_call_id("call_123")
    assert mistral_tool_call_id("call_123") != mistral_tool_call_id("call_124")


async def test_mistral_errors() -> None:
    api = FakeAPI(httpx2.Response(401, json={"message": "Unauthorized"}))
    with pytest.raises(ProviderAuthError):
        await collect(mistral_provider(api), [Message.user("x")])
    api = FakeAPI(httpx2.Response(429, json={"message": "Rate limit"}))
    with pytest.raises(ProviderError) as info:
        await collect(mistral_provider(api), [Message.user("x")])
    assert info.value.retryable


async def test_mistral_offline() -> None:
    def offline(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("Network is unreachable", request=request)

    provider = mistral_provider(FakeAPI(offline, offline))
    with pytest.raises(ProviderUnavailableError):
        await collect(provider, [Message.user("x")])
    with pytest.raises(ProviderUnavailableError):
        await provider.list_models()


async def test_mistral_list_models() -> None:
    def card(model_id: str, chat: bool) -> dict[str, Any]:
        return {
            "id": model_id,
            "object": "model",
            "type": "base",
            "name": model_id.title(),
            "capabilities": {"completion_chat": chat, "function_calling": chat},
        }

    api = FakeAPI(
        httpx2.Response(
            200,
            json={
                "object": "list",
                "data": [card("mistral-large-latest", True), card("mistral-embed", False)],
            },
        )
    )
    models = await mistral_provider(api).list_models()
    assert [(m.id, m.display_name) for m in models] == [
        ("mistral-large-latest", "Mistral-Large-Latest")
    ]


async def test_mistral_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    with pytest.raises(ProviderAuthError, match="MISTRAL_API_KEY"):
        await collect(MistralProvider(), [Message.user("x")])
