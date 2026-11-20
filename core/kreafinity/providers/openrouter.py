"""OpenRouter adapter: hundreds of models (Claude, GPT, Gemini, Llama…) with one key.

OpenRouter speaks the OpenAI Chat Completions API, so this reuses the OpenAI
adapter with its own address and key. Docs: https://openrouter.ai/docs
"""

from __future__ import annotations

import os
from typing import Any

import openai

from kreafinity.providers.base import ModelInfo, ProviderAuthError
from kreafinity.providers.openai import OpenAIProvider, _map_error
from kreafinity.providers.registry import register_provider

OPENROUTER_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-opus-5.5"
#: Optional headers naming the application in OpenRouter's statistics.
ATTRIBUTION_HEADERS = {"HTTP-Referer": "https://kreafinity.com", "X-Title": "Kreafinity"}


@register_provider
class OpenRouterProvider(OpenAIProvider):
    """Models from many vendors through OpenRouter (``anthropic/claude-opus-5.5``…)."""

    name = "openrouter"
    display_name = "OpenRouter"
    requires_api_key = True
    api_key_env_var = "OPENROUTER_API_KEY"
    max_tokens_parameter = "max_tokens"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = OPENROUTER_URL,
        default_model: str = DEFAULT_MODEL,
        max_tokens: int | None = None,
        client: openai.AsyncOpenAI | None = None,
        http_client: Any = None,
    ) -> None:
        """Create the adapter.

        Args:
            api_key: OpenRouter key; when omitted, ``OPENROUTER_API_KEY`` is read
                (never ``OPENAI_API_KEY``: an OpenAI key must not go to OpenRouter).
            base_url: URL of the service.
            default_model: Model used when `chat` is not given one.
            max_tokens: Maximum length of a response (no limit by default).
            client: SDK client to use instead of creating one (for tests).
            http_client: HTTP client given to the SDK.
        """
        super().__init__(
            api_key=api_key or os.environ.get(self.api_key_env_var or "") or None,
            base_url=base_url,
            default_model=default_model,
            max_tokens=max_tokens,
            client=client,
            http_client=http_client,
        )

    @property
    def client(self) -> openai.AsyncOpenAI:
        if self._client is None:
            if not self._api_key:  # the SDK would fall back to OPENAI_API_KEY
                raise ProviderAuthError(
                    "No API key for OpenRouter (create one on openrouter.ai, then "
                    "« kreafinity provider key openrouter »)",
                    provider=self.name,
                )
            self._client = openai.AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                default_headers=ATTRIBUTION_HEADERS,
                http_client=self._http_client,
            )
        return self._client

    async def list_models(self) -> list[ModelInfo]:
        """Models able to call tools (the others cannot act on the computer)."""
        models = []
        try:
            async for model in self.client.models.list(
                extra_query={"supported_parameters": "tools"}
            ):
                extra = model.model_extra or {}
                if "tools" not in extra.get("supported_parameters", ["tools"]):
                    continue
                name = extra.get("name")
                models.append(
                    ModelInfo(
                        id=model.id,
                        provider=self.name,
                        display_name=name if isinstance(name, str) else model.id,
                    )
                )
        except openai.APIError as exc:
            raise _map_error(exc, self) from exc
        return sorted(models, key=lambda info: info.id)
