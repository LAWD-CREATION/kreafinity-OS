import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from kreafinity.providers import (
    Message,
    ProviderError,
    ProviderUnavailableError,
    ResponseDone,
    TextDelta,
    ToolCall,
    ToolSpec,
)
from kreafinity.providers.ollama import OllamaProvider

Handler = Callable[[httpx.Request], httpx.Response]

LIST_DIR = ToolSpec(
    name="list_directory",
    description="Liste le contenu d'un dossier.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
)


def ndjson(*chunks: dict[str, Any]) -> bytes:
    return "".join(json.dumps(chunk) + "\n" for chunk in chunks).encode()


def make_provider(handler: Handler) -> OllamaProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaProvider(base_url="http://ollama.test", client=client)


async def collect(provider: OllamaProvider, *args: Any, **kwargs: Any) -> list[Any]:
    return [event async for event in provider.chat(*args, **kwargs)]


async def test_streams_text() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        assert request.url == "http://ollama.test/api/chat"
        return httpx.Response(
            200,
            content=ndjson(
                {"model": "llama3.1", "message": {"role": "assistant", "content": "Bon"}},
                {"model": "llama3.1", "message": {"role": "assistant", "content": "jour"}},
                {
                    "model": "llama3.1",
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 12,
                    "eval_count": 3,
                },
            ),
        )

    events = await collect(
        make_provider(handler), [Message.system("Tu es utile."), Message.user("Salut")]
    )

    assert events[:2] == [TextDelta("Bon"), TextDelta("jour")]
    done = events[-1]
    assert isinstance(done, ResponseDone)
    assert done.message == Message.assistant("Bonjour")
    assert done.stop_reason == "end_turn"
    assert (done.usage.input_tokens, done.usage.output_tokens) == (12, 3)
    assert requests[0] == {
        "model": "llama3.1",
        "stream": True,
        "messages": [
            {"role": "system", "content": "Tu es utile."},
            {"role": "user", "content": "Salut"},
        ],
    }


async def test_tool_calls_and_history_conversion() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=ndjson(
                {
                    "model": "qwen3",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"function": {"name": "list_directory", "arguments": {"path": "~"}}},
                            {"function": {"name": "system_info", "arguments": "{}"}},
                        ],
                    },
                },
                {"model": "qwen3", "message": {"content": ""}, "done": True, "done_reason": "stop"},
            ),
        )

    previous_call = ToolCall(id="toolu_01", name="list_directory", arguments={"path": "/tmp"})
    history = [
        Message.user("Que contient /tmp ?"),
        Message.assistant("", [previous_call]),
        Message.tool_result("toolu_01", "list_directory", "a.txt"),
        Message.user("Et mon dossier perso ?"),
    ]
    events = await collect(make_provider(handler), history, [LIST_DIR], model="qwen3")

    done = events[-1]
    assert isinstance(done, ResponseDone)
    assert done.stop_reason == "tool_use"
    assert done.model == "qwen3"
    calls = done.message.tool_calls
    assert [(c.name, c.arguments) for c in calls] == [
        ("list_directory", {"path": "~"}),
        ("system_info", {}),
    ]
    assert all(c.id.startswith("call_") for c in calls)
    assert len({c.id for c in calls}) == 2

    sent = requests[0]
    assert sent["model"] == "qwen3"
    assert sent["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "list_directory",
                "description": "Liste le contenu d'un dossier.",
                "parameters": LIST_DIR.parameters,
            },
        }
    ]
    assert sent["messages"][1:3] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "list_directory", "arguments": {"path": "/tmp"}}}],
        },
        {"role": "tool", "content": "a.txt", "tool_name": "list_directory"},
    ]


async def test_length_stop_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=ndjson({"message": {"content": "…"}, "done": True, "done_reason": "length"}),
        )

    events = await collect(make_provider(handler), [Message.user("Raconte")])
    assert events[-1].stop_reason == "max_tokens"


async def test_http_error_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model 'llama9' not found"})

    with pytest.raises(ProviderError, match="llama9") as info:
        await collect(make_provider(handler), [Message.user("Salut")], model="llama9")
    assert not info.value.retryable


async def test_error_inside_stream() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ndjson({"error": "out of memory"}))

    with pytest.raises(ProviderError, match="out of memory"):
        await collect(make_provider(handler), [Message.user("Salut")])


async def test_truncated_stream() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ndjson({"message": {"content": "Bon"}}))

    with pytest.raises(ProviderError, match="before finishing"):
        await collect(make_provider(handler), [Message.user("Salut")])


async def test_server_not_running() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    provider = make_provider(handler)
    with pytest.raises(ProviderUnavailableError):
        await collect(provider, [Message.user("Salut")])
    with pytest.raises(ProviderUnavailableError):
        await provider.list_models()


async def test_list_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://ollama.test/api/tags"
        return httpx.Response(
            200, json={"models": [{"name": "llama3.1:latest"}, {"name": "mistral:7b"}]}
        )

    models = await make_provider(handler).list_models()
    assert [(m.id, m.provider) for m in models] == [
        ("llama3.1:latest", "ollama"),
        ("mistral:7b", "ollama"),
    ]
