"""The first start: is an AI ready to answer, and helping the user choose one.

The welcome screen of the interface uses this: it opens when no AI can
answer yet (no API key for the chosen provider, Ollama missing or without
the model), lets the user pick a provider, enter a key or download a local
model, and prepare the voice.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import psutil

#: Ollama model names (``qwen2.5:7b``, ``library/llama3.1``…).
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,80}(:[A-Za-z0-9._-]{1,40})?$")
PROGRESS_EVERY = 0.5  # seconds between two progress events


@dataclass(frozen=True, slots=True)
class LocalModel:
    """A model proposed for Ollama, with its download size."""

    name: str
    size_gb: float
    min_ram_gb: int
    vision: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size_gb": self.size_gb,
            "min_ram_gb": self.min_ram_gb,
            "vision": self.vision,
        }


#: Proposed local models, smallest first: good at French and at using tools.
LOCAL_MODELS = (
    LocalModel("qwen2.5:3b", 1.9, 4),
    LocalModel("qwen2.5:7b", 4.7, 8),
    LocalModel("qwen2.5vl:7b", 6.0, 12, vision=True),
)


def memory_gb() -> float:
    return psutil.virtual_memory().total / 1024**3


def recommended(ram_gb: float | None = None) -> LocalModel:
    """The best proposed model this computer can run comfortably (text only)."""
    ram = memory_gb() if ram_gb is None else ram_gb
    fitting = [m for m in LOCAL_MODELS if not m.vision and m.min_ram_gb <= ram + 0.5]
    return fitting[-1] if fitting else LOCAL_MODELS[0]


def has_model(installed: list[str], wanted: str) -> bool:
    """Whether `wanted` (``llama3.1`` means ``llama3.1:latest``) is installed."""
    full = wanted if ":" in wanted else f"{wanted}:latest"
    return any(name in (wanted, full) for name in installed)


def ollama_installed() -> bool:
    return shutil.which("ollama") is not None


def problem(
    *,
    requires_key: bool,
    key_present: bool,
    is_ollama: bool,
    ollama_running: bool,
    model_present: bool,
) -> str | None:
    """Why the chosen AI cannot answer yet, or None if it can."""
    if requires_key:
        return None if key_present else "no_key"
    if is_ollama:
        if not ollama_running:
            return "ollama_stopped" if ollama_installed() else "ollama_missing"
        return None if model_present else "model_missing"
    return None


async def pull(base_url: str, model: str) -> AsyncIterator[dict[str, Any]]:
    """Download a model with Ollama, yielding progress (status, completed, total).

    Progress is throttled; the last item is ``{"done": True}``.

    Raises:
        ValueError: Invalid model name.
        httpx.HTTPError: Ollama cannot be reached.
        RuntimeError: Ollama reported an error (unknown model…).
    """
    if not MODEL_NAME.match(model):
        raise ValueError(f"invalid model name: {model!r}")
    last = 0.0
    status = ""
    timeout = httpx.Timeout(10.0, read=600.0)
    async with (
        httpx.AsyncClient(timeout=timeout) as client,
        client.stream(
            "POST", f"{base_url.rstrip('/')}/api/pull", json={"model": model, "stream": True}
        ) as response,
    ):
        if response.status_code >= 400:
            await response.aread()
            raise RuntimeError(_error(response.text) or f"HTTP {response.status_code}")
        async for line in response.aiter_lines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if "error" in item:
                raise RuntimeError(str(item["error"]))
            now = time.monotonic()
            changed = item.get("status") != status
            status = str(item.get("status", status))
            if changed or now - last >= PROGRESS_EVERY:
                last = now
                yield {
                    "status": status,
                    "completed": int(item.get("completed") or 0),
                    "total": int(item.get("total") or 0),
                }
    yield {"done": True}


def _error(text: str) -> str:
    try:
        return str(json.loads(text).get("error", ""))
    except (ValueError, AttributeError):
        return text.strip()[:200]
