"""API keys in the system keyring: library, CLI and API."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import keyring
import pytest
from fastapi.testclient import TestClient
from keyring.backends import fail
from typer.testing import CliRunner

from kreafinity import cli
from kreafinity.agent import ConversationStore
from kreafinity.api import create_app
from kreafinity.config import (
    SecretStoreError,
    api_key_source,
    delete_api_key,
    get_api_key,
    keyring_available,
    resolve_provider_options,
    set_api_key,
)
from kreafinity.service import AssistantService
from kreafinity.tools import ToolContext
from memory_keyring import MemoryKeyring

runner = CliRunner()
TOKEN = "k" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryKeyring]:
    for variable in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MISTRAL_API_KEY"):
        monkeypatch.delenv(variable, raising=False)
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def no_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for variable in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MISTRAL_API_KEY"):
        monkeypatch.delenv(variable, raising=False)
    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    yield
    keyring.set_keyring(previous)


def test_store_read_delete(memory_keyring: MemoryKeyring) -> None:
    assert keyring_available()
    assert api_key_source("anthropic") == "missing"
    set_api_key("anthropic", "  sk-ant-test-123  ")
    assert memory_keyring.passwords == {("kreafinity", "anthropic"): "sk-ant-test-123"}
    assert get_api_key("anthropic") == "sk-ant-test-123"
    assert api_key_source("anthropic") == "keyring"
    assert delete_api_key("anthropic") is True
    assert delete_api_key("anthropic") is False
    assert get_api_key("anthropic") is None


@pytest.mark.parametrize("key", ["", "   ", "sk ant", "x" * 1001])
def test_invalid_keys(memory_keyring: MemoryKeyring, key: str) -> None:
    with pytest.raises(ValueError):
        set_api_key("anthropic", key)


def test_environment_variable_wins(
    memory_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_api_key("openai", "sk-from-keyring")
    assert resolve_provider_options("openai", {"base_url": "x"}) == {
        "base_url": "x",
        "api_key": "sk-from-keyring",
    }
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert api_key_source("openai") == "environment"
    assert resolve_provider_options("openai", {}) == {}  # the SDK reads the variable
    assert api_key_source("ollama") is None
    assert resolve_provider_options("ollama", {"base_url": "x"}) == {"base_url": "x"}


def test_without_system_keyring(no_keyring: None) -> None:
    assert not keyring_available()
    assert get_api_key("anthropic") is None
    assert api_key_source("anthropic") == "missing"
    with pytest.raises(SecretStoreError):
        set_api_key("anthropic", "sk-x")
    with pytest.raises(SecretStoreError):
        delete_api_key("anthropic")


def test_key_reaches_the_provider(memory_keyring: MemoryKeyring) -> None:
    from kreafinity.providers import create_provider

    set_api_key("anthropic", "sk-ant-secret")
    provider = create_provider("anthropic", **resolve_provider_options("anthropic", {}))
    assert provider._api_key == "sk-ant-secret"  # type: ignore[attr-defined]


# --- CLI ------------------------------------------------------------------------------


def test_cli_provider_key(memory_keyring: MemoryKeyring) -> None:
    result = runner.invoke(cli.app, ["provider", "key", "mistral"], input="mk-secret-1\n")
    assert result.exit_code == 0, result.output
    assert "mk-secret-1" not in result.output  # the key is never echoed
    assert get_api_key("mistral") == "mk-secret-1"

    listed = runner.invoke(cli.app, ["provider", "list"], env={"COLUMNS": "160"})
    assert "dans le trousseau" in listed.output and "absente" in listed.output

    deleted = runner.invoke(cli.app, ["provider", "key", "mistral", "--supprimer"])
    assert deleted.exit_code == 0 and get_api_key("mistral") is None
    assert "n'utilise pas de clé" in runner.invoke(cli.app, ["provider", "key", "ollama"]).output
    assert runner.invoke(cli.app, ["provider", "key", "skynet"]).exit_code == 2


def test_cli_without_keyring(no_keyring: None) -> None:
    result = runner.invoke(cli.app, ["provider", "key", "anthropic"], input="sk\n")
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output


# --- API -------------------------------------------------------------------------------


@pytest.fixture
def client(home: Path) -> Iterator[TestClient]:
    created: list[Any] = []

    def factory() -> AssistantService:
        service = AssistantService(ConversationStore(), context=ToolContext.create(home=home))
        created.append(service)
        return service

    app = create_app(factory, TOKEN, port=27494, allowed_hosts=("testserver",))
    with TestClient(app) as test_client:
        yield test_client


def test_api_keys(memory_keyring: MemoryKeyring, client: TestClient) -> None:
    providers = client.get("/api/providers", headers=AUTH).json()
    assert providers["keyring_available"] is True
    anthropic = next(p for p in providers["providers"] if p["name"] == "anthropic")
    assert anthropic == {
        **anthropic,
        "requires_api_key": True,
        "api_key": "missing",
        "api_key_env_var": "ANTHROPIC_API_KEY",
    }
    ollama = next(p for p in providers["providers"] if p["name"] == "ollama")
    assert ollama["requires_api_key"] is False and ollama["api_key"] is None

    saved = client.put("/api/providers/anthropic/key", headers=AUTH, json={"api_key": "sk-1"})
    assert saved.json() == {"provider": "anthropic", "api_key": "keyring"}
    assert get_api_key("anthropic") == "sk-1"
    listed = client.get("/api/providers", headers=AUTH).json()
    assert "sk-1" not in str(listed)  # never sent back
    removed = client.delete("/api/providers/anthropic/key", headers=AUTH)
    assert removed.json() == {"provider": "anthropic", "api_key": "missing"}

    assert (
        client.put("/api/providers/ollama/key", headers=AUTH, json={"api_key": "x"}).status_code
        == 400
    )
    assert (
        client.put("/api/providers/skynet/key", headers=AUTH, json={"api_key": "x"}).status_code
        == 404
    )
    assert (
        client.put("/api/providers/openai/key", headers=AUTH, json={"api_key": "a b"}).status_code
        == 400
    )
    assert client.put("/api/providers/openai/key", json={"api_key": "sk"}).status_code == 401


def test_api_keys_without_keyring(no_keyring: None, client: TestClient) -> None:
    assert client.get("/api/providers", headers=AUTH).json()["keyring_available"] is False
    response = client.put("/api/providers/anthropic/key", headers=AUTH, json={"api_key": "sk"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "keyring_unavailable"


def test_api_info(client: TestClient, home: Path) -> None:
    info = client.get("/api/info", headers=AUTH).json()
    assert info["home"] == str(home)
    assert info["user_name"]
    assert set(info) == {"version", "home", "user_name", "keyring_available"}
