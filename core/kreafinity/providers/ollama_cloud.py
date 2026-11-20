"""Ollama Cloud adapter: large models run by ollama.com, with an API key.

Same API as a local Ollama server, on https://ollama.com, with the key in an
``Authorization: Bearer`` header. Docs: https://docs.ollama.com/cloud
"""

from __future__ import annotations

import os

import httpx

from kreafinity.providers.base import ProviderAuthError
from kreafinity.providers.ollama import OllamaProvider
from kreafinity.providers.registry import register_provider

CLOUD_URL = "https://ollama.com"
DEFAULT_MODEL = "gpt-oss:120b"


@register_provider
class OllamaCloudProvider(OllamaProvider):
    """Models of Ollama's cloud service (``gpt-oss:120b``…), billed by Ollama."""

    name = "ollama-cloud"
    display_name = "Ollama Cloud"
    is_local = False
    requires_api_key = True
    api_key_env_var = "OLLAMA_API_KEY"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = CLOUD_URL,
        default_model: str = DEFAULT_MODEL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create the adapter.

        Args:
            api_key: API key from ollama.com; when omitted, ``OLLAMA_API_KEY`` is read.
            base_url: URL of the service.
            default_model: Model used when `chat` is not given one.
            client: HTTP client to use instead of creating one (for tests).
        """
        self._api_key = api_key or os.environ.get(self.api_key_env_var or "") or None
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        super().__init__(
            base_url=base_url, default_model=default_model, client=client, headers=headers
        )

    def _request_headers(self) -> dict[str, str]:
        if not self._api_key:
            raise ProviderAuthError(
                "No API key for Ollama Cloud (create one on ollama.com, then "
                "« kreafinity provider key ollama-cloud »)",
                provider=self.name,
            )
        return super()._request_headers()
