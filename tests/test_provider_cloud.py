"""Ollama Cloud and OpenRouter, with their HTTP traffic simulated (no network)."""

import json
from typing import Any

import httpx
import httpx2
import openai
import pytest

from kreafinity.config import resolve_provider_options
from kreafinity.providers import (
    Message,
    ProviderAuthError,
    ProviderError,
    ResponseDone,
    TextDelta,
    ToolSpec,
    create_provider,
    get_provider_class,
)
from kreafinity.providers.ollama_cloud import OllamaCloudProvider
from kreafinity.providers.openrouter import OpenRouterProvider

LIST_DIR = ToolSpec(
    name="list_directory",
    description="Liste le contenu d'un dossier.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
)


@pytest.fixture(autouse=True)
def no_keys_in_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("OLLAMA_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(variable, raising=False)


async def collect(provider: Any, *args: Any, **kwargs: Any) -> list[Any]:
    return [event async for event in provider.chat(*args, **kwargs)]


def test_registered() -> None:
    for name, variable in (
        ("ollama-cloud", "OLLAMA_API_KEY"),
        ("openrouter", "OPENROUTER_API_KEY"),
    ):
        provider_class = get_provider_class(name)
        assert provider_class.requires_api_key and provider_class.api_key_env_var == variable
        assert not provider_class.is_local  # no use as an offline fallback


# --- Ollama Cloud ----------------------------------------------------------------------


def ollama_cloud(handler: Any, **options: Any) -> OllamaCloudProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaCloudProvider(client=client, **options)


async def test_ollama_cloud_chat_sends_the_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        lines = [
            {"model": "gpt-oss:120b", "message": {"role": "assistant", "content": "Bonjour"}},
            {
                "model": "gpt-oss:120b",
                "message": {"content": ""},
                "done": True,
                "done_reason": "stop",
            },
        ]
        return httpx.Response(200, content="".join(json.dumps(x) + "\n" for x in lines).encode())

    events = await collect(
        ollama_cloud(handler, api_key="ok-key"), [Message.user("Salut")], [LIST_DIR]
    )

    assert str(seen[0].url) == "https://ollama.com/api/chat"
    assert seen[0].headers["authorization"] == "Bearer ok-key"
    assert json.loads(seen[0].content)["model"] == "gpt-oss:120b"
    assert events[0] == TextDelta("Bonjour")
    assert isinstance(events[-1], ResponseDone) and events[-1].model == "gpt-oss:120b"


async def test_ollama_cloud_reads_its_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_API_KEY", "env-key")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"models": [{"name": "gpt-oss:120b"}, {"name": "qwen3-coder:480b"}]}
        )

    models = await ollama_cloud(handler).list_models()
    assert [m.id for m in models] == ["gpt-oss:120b", "qwen3-coder:480b"]
    assert {m.provider for m in models} == {"ollama-cloud"}
    assert str(seen[0].url) == "https://ollama.com/api/tags"
    assert seen[0].headers["authorization"] == "Bearer env-key"


async def test_ollama_cloud_without_key_sends_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request without a key")

    provider = ollama_cloud(handler)
    with pytest.raises(ProviderAuthError, match="No API key for Ollama Cloud"):
        await collect(provider, [Message.user("Salut")])
    with pytest.raises(ProviderAuthError):
        await provider.list_models()


@pytest.mark.parametrize(("status", "error"), [(401, ProviderAuthError), (403, ProviderAuthError)])
async def test_ollama_cloud_rejected_key(status: int, error: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "unauthorized"})

    with pytest.raises(error, match="Ollama Cloud returned HTTP"):
        await collect(ollama_cloud(handler, api_key="bad"), [Message.user("Salut")])


async def test_ollama_server_error_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "overloaded"})

    with pytest.raises(ProviderError, match="overloaded") as info:
        await collect(ollama_cloud(handler, api_key="ok"), [Message.user("Salut")])
    assert info.value.retryable and not isinstance(info.value, ProviderAuthError)


# --- OpenRouter ----------------------------------------------------------------------------


class FakeOpenRouter:
    def __init__(self, *responses: httpx2.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def provider(self, **options: Any) -> OpenRouterProvider:
        http = openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self))
        return OpenRouterProvider(http_client=http, **options)


def sse(*chunks: dict[str, Any]) -> httpx2.Response:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=body.encode()
    )


def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "gen-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "anthropic/claude-opus-5.5",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


async def test_openrouter_chat() -> None:
    api = FakeOpenRouter(
        sse(
            chunk({"role": "assistant", "content": "Je regarde."}),
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "list_directory", "arguments": '{"path": "~"}'},
                        }
                    ]
                },
                finish="tool_calls",
            ),
        )
    )
    events = await collect(
        api.provider(api_key="or-key"), [Message.user("Mon dossier ?")], [LIST_DIR]
    )

    request = api.requests[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer or-key"
    assert request.headers["x-title"] == "Kreafinity"
    assert request.headers["http-referer"] == "https://kreafinity.com"
    body = json.loads(request.content)
    assert body["model"] == "anthropic/claude-opus-5.5"
    assert body["tools"][0]["function"]["name"] == "list_directory"
    done = events[-1]
    assert isinstance(done, ResponseDone) and done.stop_reason == "tool_use"
    assert done.message.tool_calls[0].arguments == {"path": "~"}


async def test_openrouter_max_tokens_parameter() -> None:
    api = FakeOpenRouter(sse(chunk({"content": "ok"}, finish="stop")))
    await collect(
        api.provider(api_key="k", max_tokens=500), [Message.user("x")], model="openai/gpt-5-mini"
    )
    body = json.loads(api.requests[0].content)
    assert body["max_tokens"] == 500 and "max_completion_tokens" not in body
    assert body["model"] == "openai/gpt-5-mini"


async def test_openrouter_never_uses_the_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
    api = FakeOpenRouter()
    with pytest.raises(ProviderAuthError, match="No API key for OpenRouter"):
        await collect(api.provider(), [Message.user("Salut")])
    assert api.requests == []

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-env")
    api = FakeOpenRouter(sse(chunk({"content": "ok"}, finish="stop")))
    await collect(api.provider(), [Message.user("Salut")])
    assert api.requests[0].headers["authorization"] == "Bearer or-env"


async def test_openrouter_lists_models_able_to_use_tools() -> None:
    api = FakeOpenRouter(
        httpx2.Response(
            200,
            json={
                "data": [
                    {
                        "id": "openai/gpt-5-mini",
                        "name": "OpenAI: GPT-5 Mini",
                        "supported_parameters": ["tools", "max_tokens"],
                    },
                    {
                        "id": "some/text-only",
                        "name": "Text only",
                        "supported_parameters": ["max_tokens"],
                    },
                    {
                        "id": "anthropic/claude-opus-5.5",
                        "name": "Anthropic: Claude Opus 5.5",
                        "supported_parameters": ["tools"],
                    },
                ]
            },
        )
    )
    models = await api.provider(api_key="k").list_models()
    assert [(m.id, m.display_name) for m in models] == [
        ("anthropic/claude-opus-5.5", "Anthropic: Claude Opus 5.5"),
        ("openai/gpt-5-mini", "OpenAI: GPT-5 Mini"),
    ]
    assert api.requests[0].url.params["supported_parameters"] == "tools"


@pytest.mark.parametrize(
    ("status", "error", "message"),
    [
        (401, ProviderAuthError, "OpenRouter rejected the credentials"),
        (402, ProviderError, "OpenRouter API error"),  # no credits left
    ],
)
async def test_openrouter_errors(status: int, error: type[Exception], message: str) -> None:
    api = FakeOpenRouter(
        httpx2.Response(status, json={"error": {"code": status, "message": "nope"}})
    )
    with pytest.raises(error, match=message):
        await collect(api.provider(api_key="k"), [Message.user("Salut")])


def test_keys_come_from_the_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = {"openrouter": "or-from-keyring", "ollama-cloud": "oc-from-keyring"}
    monkeypatch.setattr("kreafinity.config.secrets.get_api_key", lambda name: keys.get(name))
    for name in keys:
        options = resolve_provider_options(name, {})
        assert options == {"api_key": keys[name]}
        assert create_provider(name, **options)._api_key == keys[name]  # type: ignore[attr-defined]
