import json
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


# --- Approval of confirm/danger actions ----------------------------------------------


@pytest.mark.parametrize(
    ("answer", "created"), [("o", True), ("oui", True), ("n", False), ("", False)]
)
def test_confirm_action_needs_yes(
    fake_provider: list[Any], home: Path, answer: str, created: bool
) -> None:
    provider = ScriptedProvider(
        reply("", [call("create_folder", path="~/Projets")]), reply("Terminé.")
    )
    fake_provider[0](provider)

    result = runner.invoke(cli.app, ["ask", "Crée un dossier Projets"], input=f"{answer}\n")

    assert result.exit_code == 0, result.output
    assert "Validation requise" in result.output
    assert "Créer un dossier" in result.output
    assert (home / "Projets").is_dir() is created
    assert ("action refusée" in result.output) is not created


@pytest.mark.parametrize(("answer", "trashed"), [("oui", True), ("o", False), ("OUI ", True)])
def test_danger_action_needs_explicit_word(
    fake_provider: list[Any], home: Path, answer: str, trashed: bool
) -> None:
    provider = ScriptedProvider(
        reply("", [call("move_to_trash", paths=["~/Documents/Facture-2026.pdf"])]),
        reply("Fait."),
    )
    fake_provider[0](provider)

    result = runner.invoke(cli.app, ["ask", "Supprime la facture"], input=f"{answer}\n")

    assert "Action sensible" in result.output
    assert "Facture-2026.pdf" in result.output
    assert (home / "Documents" / "Facture-2026.pdf").exists() is not trashed


def test_approval_without_input_is_denied(fake_provider: list[Any], home: Path) -> None:
    provider = ScriptedProvider(
        reply("", [call("move_to_trash", paths=["~/Documents"])]), reply("Ok.")
    )
    fake_provider[0](provider)
    result = runner.invoke(cli.app, ["ask", "Supprime Documents"], input="")
    assert (home / "Documents").is_dir()
    assert "action refusée" in result.output


def test_model_output_control_characters_are_stripped(fake_provider: list[Any]) -> None:
    fake_provider[0](ScriptedProvider(reply("Salut\x1b[2J\x07 !\n")))
    result = runner.invoke(cli.app, ["ask", "Salut"])
    assert "\x1b" not in result.output and "\x07" not in result.output
    assert "Salut[2J !" in result.output  # inert text once the escape character is gone


def test_end_of_step_1_scenario(fake_provider: list[Any], home: Path) -> None:
    """« Montre-moi les 5 plus gros fichiers de mon dossier Téléchargements et mets-les à
    la corbeille » : the AI lists them, asks for approval, then trashes them."""
    downloads = home / "Téléchargements"
    for index, size in enumerate([9000, 8000, 7000, 6000, 1]):
        (downloads / f"fichier{index}.bin").write_bytes(b"x" * size)
    largest = ["fichier0.bin", "fichier1.bin", "fichier2.bin", "fichier3.bin", "big.iso"]

    provider = ScriptedProvider(
        reply(
            "Je cherche les plus gros fichiers.",
            [
                call(
                    "search_files",
                    pattern="*",
                    path="~/Téléchargements",
                    sort_by="size",
                    limit=5,
                )
            ],
        ),
        reply(
            "Voici les 5 plus gros fichiers. Je les mets à la corbeille.",
            [call("move_to_trash", "call_2", paths=[f"~/Téléchargements/{n}" for n in largest])],
        ),
        reply("C'est fait : les 5 fichiers sont dans la corbeille."),
    )
    fake_provider[0](provider)

    result = runner.invoke(
        cli.app,
        [
            "ask",
            "Montre-moi les 5 plus gros fichiers de mon dossier Téléchargements et "
            "mets-les à la corbeille",
        ],
        input="oui\n",
    )

    assert result.exit_code == 0, result.output
    # 1. The search result given to the model lists the 5 largest files.
    search_result = provider.requests[1][0][-1]
    names = [r["name"] for r in json.loads(search_result.content)["results"]]
    assert names == largest
    # 2. The user saw the exact list before approving.
    assert "Mettre 5 éléments à la corbeille" in result.output
    for name in largest:
        assert name in result.output
    # 3. The files are in the trash, restorable; the others are untouched.
    trash = home.parent.parent / "xdg-data" / "Trash"
    for name in largest:
        assert not (downloads / name).exists()
        assert (trash / "files" / name).exists()
        assert (trash / "info" / f"{name}.trashinfo").exists()
    assert (downloads / "fichier4.bin").exists()
    assert "C'est fait" in result.output
