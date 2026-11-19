from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fakes import ScriptedProvider, call, reply
from kreafinity import __version__, cli
from kreafinity.config import default_config_path
from kreafinity.i18n import set_language
from kreafinity.providers import ProviderAuthError, ProviderError, ProviderUnavailableError

runner = CliRunner()


@pytest.fixture(autouse=True)
def reset_language() -> Iterator[None]:
    yield
    set_language("fr")


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch, home: Path) -> list[Any]:
    """Make the CLI use a scripted provider; returns [created provider, options]."""
    monkeypatch.setenv("HOME", str(home))
    created: list[Any] = []

    def install(provider: ScriptedProvider, *, name: str = "ollama") -> None:
        provider.name = name

        def create(provider_name: str, **options: Any) -> ScriptedProvider:
            created.append((provider_name, options))
            return provider

        monkeypatch.setattr(cli, "create_provider", create)

    created.append(install)
    return created


def test_version_command_prints_version() -> None:
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert "Kreafinity OS" in result.output
    assert __version__ in result.output


def test_no_args_shows_help() -> None:
    result = runner.invoke(cli.app, [])
    assert "ask" in result.output and "chat" in result.output


def test_ask_streams_answer_and_tool_calls(fake_provider: list[Any]) -> None:
    provider = ScriptedProvider(
        reply("", [call("list_directory", path="~/Téléchargements")]),
        reply("Il y a 4 éléments."),
    )
    fake_provider[0](provider)

    result = runner.invoke(cli.app, ["ask", "Que contient Téléchargements ?"])

    assert result.exit_code == 0, result.output
    assert "list_directory(path='~/Téléchargements')" in result.output
    assert "Il y a 4 éléments." in result.output
    assert provider.closed
    assert fake_provider[1] == ("ollama", {})
    assert provider.requests[0][2] == "llama3.1"  # model from the default spec


def test_ask_with_provider_option_and_config(fake_provider: list[Any]) -> None:
    path = default_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("providers:\n  anthropic:\n    max_tokens: 1000\n", encoding="utf-8")
    provider = ScriptedProvider(reply("Bonjour"))
    fake_provider[0](provider, name="anthropic")

    result = runner.invoke(cli.app, ["ask", "-p", "anthropic/claude-opus-5-5", "Salut"])

    assert result.exit_code == 0, result.output
    assert fake_provider[1] == ("anthropic", {"max_tokens": 1000})
    assert provider.requests[0][2] == "claude-opus-5-5"


@pytest.mark.parametrize(
    ("error", "provider_name", "expected"),
    [
        (ProviderUnavailableError("refused", provider="ollama"), "ollama", "ollama serve"),
        (ProviderError("model 'x' not found", provider="ollama"), "ollama", "ollama pull"),
        (ProviderAuthError("401", provider="anthropic"), "anthropic", "ANTHROPIC_API_KEY"),
    ],
)
def test_ask_provider_errors(
    fake_provider: list[Any], error: ProviderError, provider_name: str, expected: str
) -> None:
    fake_provider[0](ScriptedProvider(error), name=provider_name)
    result = runner.invoke(cli.app, ["ask", "-p", provider_name, "Salut"])
    assert result.exit_code == 1
    assert expected in result.output


def test_ask_reports_iteration_limit(fake_provider: list[Any]) -> None:
    path = default_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("max_iterations: 2\n", encoding="utf-8")
    fake_provider[0](ScriptedProvider(reply("", [call("system_info")])))
    result = runner.invoke(cli.app, ["ask", "Boucle"])
    assert result.exit_code == 1
    assert "limite de 2 étapes" in result.output


def test_unknown_provider() -> None:
    result = runner.invoke(cli.app, ["ask", "-p", "skynet/t800", "Salut"])
    assert result.exit_code == 2
    assert "skynet" in result.output and "ollama" in result.output


def test_invalid_config() -> None:
    path = default_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("max_iterations: zero\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["ask", "Salut"])
    assert result.exit_code == 2
    assert "Configuration invalide" in result.output


def test_chat_session(fake_provider: list[Any]) -> None:
    provider = ScriptedProvider(reply("Bonjour David."), reply("Au revoir."))
    fake_provider[0](provider)

    result = runner.invoke(cli.app, ["chat"], input="Bonjour\n\nÇa va ?\n/quitter\n")

    assert result.exit_code == 0, result.output
    assert "Bonjour David." in result.output and "Au revoir." in result.output
    assert "À bientôt !" in result.output
    # The second request carries the whole conversation.
    assert [m.role for m in provider.requests[1][0]] == ["system", "user", "assistant", "user"]
    assert provider.closed


def test_chat_in_english(fake_provider: list[Any]) -> None:
    path = default_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("language: en\n", encoding="utf-8")
    fake_provider[0](ScriptedProvider(reply("Hi")))
    result = runner.invoke(cli.app, ["chat"], input="")
    assert "Type /quit to exit." in result.output
    assert "Goodbye!" in result.output
