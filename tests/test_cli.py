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
from kreafinity.providers import (
    ProviderAuthError,
    ProviderError,
    ProviderUnavailableError,
    get_provider_class,
)

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
        real = get_provider_class(name)  # same API key needs as the real provider
        provider.requires_api_key = real.requires_api_key  # type: ignore[misc]
        provider.api_key_env_var = real.api_key_env_var  # type: ignore[misc]

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


# --- Providers: list, use, models, switching and fallback -----------------------------


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch, home: Path) -> dict[str, ScriptedProvider]:
    """Scripted providers by name, created by the CLI instead of the real ones."""
    monkeypatch.setenv("HOME", str(home))
    by_name: dict[str, ScriptedProvider] = {}

    def create(name: str, **options: Any) -> ScriptedProvider:
        provider = by_name[name]
        provider.name = name
        provider.display_name = name.title()
        return provider

    monkeypatch.setattr(cli, "create_provider", create)
    return by_name


def test_provider_list() -> None:
    path = default_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("provider: mistral/mistral-small-latest\nfallback: ollama\n")
    result = runner.invoke(cli.app, ["provider", "list"], env={"COLUMNS": "150"})
    assert result.exit_code == 0, result.output
    for name in ("anthropic", "mistral", "ollama", "openai"):
        assert name in result.output
    assert "par défaut (mistral-small-latest)" in result.output
    assert "secours hors connexion" in result.output
    assert "sur cet ordinateur" in result.output


def test_provider_use_saves_default(providers: dict[str, ScriptedProvider]) -> None:
    providers["mistral"] = ScriptedProvider(reply("ok"))
    result = runner.invoke(cli.app, ["provider", "use", "mistral/mistral-small-latest"])
    assert result.exit_code == 0, result.output
    assert "Mistral · mistral-small-latest" in result.output
    assert "provider: mistral/mistral-small-latest" in default_config_path().read_text()

    result = runner.invoke(cli.app, ["provider", "use", "skynet"])
    assert result.exit_code == 2
    assert "provider: mistral/mistral-small-latest" in default_config_path().read_text()


def test_provider_models(providers: dict[str, ScriptedProvider]) -> None:
    providers["ollama"] = ScriptedProvider(reply("ok"))
    result = runner.invoke(cli.app, ["provider", "models"])
    assert result.exit_code == 0, result.output
    assert "ollama/fake-model" in result.output


def test_provider_models_offline(providers: dict[str, ScriptedProvider]) -> None:
    class Offline(ScriptedProvider):
        async def list_models(self) -> Any:
            raise ProviderUnavailableError("refused", provider="ollama")

    providers["ollama"] = Offline(reply("ok"))
    result = runner.invoke(cli.app, ["provider", "models", "ollama"])
    assert result.exit_code == 1
    assert "ollama serve" in result.output


def test_chat_switches_provider_and_keeps_conversation(
    providers: dict[str, ScriptedProvider],
) -> None:
    providers["ollama"] = ScriptedProvider(reply("Bonjour depuis Ollama."))
    providers["anthropic"] = ScriptedProvider(reply("Bonjour depuis Claude.", model="claude"))

    result = runner.invoke(
        cli.app,
        ["chat"],
        input="Salut\n/fournisseur\n/fournisseur anthropic/claude-opus-5-5\nTu es qui ?\n"
        "/fournisseur skynet\n/quitter\n",
    )

    assert result.exit_code == 0, result.output
    assert "Fournisseur actuel : Ollama · llama3.1" in result.output
    assert "Vous parlez maintenant à Anthropic · claude-opus-5-5" in result.output
    assert "Fournisseur inconnu" in result.output
    messages, _, model = providers["anthropic"].requests[0]
    assert model == "claude-opus-5-5"
    assert [m.content for m in messages[1:]] == [
        "Salut",
        "Bonjour depuis Ollama.",
        "Tu es qui ?",
    ]
    assert providers["ollama"].closed and providers["anthropic"].closed


def test_ask_falls_back_to_local_model(providers: dict[str, ScriptedProvider]) -> None:
    path = default_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("provider: anthropic\nfallback: ollama/llama3.1\n")
    providers["anthropic"] = ScriptedProvider(
        ProviderUnavailableError("no network", provider="anthropic")
    )
    providers["ollama"] = ScriptedProvider(reply("Réponse hors connexion."))

    result = runner.invoke(cli.app, ["ask", "Salut"])

    assert result.exit_code == 0, result.output
    assert "Anthropic est injoignable : je continue avec Ollama." in result.output
    assert "Réponse hors connexion." in result.output


def test_config_command() -> None:
    result = runner.invoke(cli.app, ["config"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "config.yaml" in result.output
    assert "provider: ollama/llama3.1" in result.output


# --- History shared with the daemon -----------------------------------------------------


def test_ask_is_saved_and_listed(fake_provider: list[Any]) -> None:
    from kreafinity.agent import ConversationStore

    fake_provider[0](ScriptedProvider(reply("Il fait beau.")))
    assert (
        runner.invoke(cli.app, ["history"]).output.strip() == "Aucune conversation pour l'instant."
    )

    result = runner.invoke(cli.app, ["ask", "Quel temps fait-il ?"])
    assert result.exit_code == 0, result.output

    [info] = ConversationStore().list_conversations()
    assert info.title == "Quel temps fait-il ?"
    assert [m.content for m in ConversationStore().messages(info.id)] == [
        "Quel temps fait-il ?",
        "Il fait beau.",
    ]
    listed = runner.invoke(cli.app, ["history"], env={"COLUMNS": "200"})
    assert listed.exit_code == 0
    assert info.id[:8] in listed.output and "Quel temps fait-il ?" in listed.output
    assert "--reprendre" in listed.output


def test_chat_resumes_a_conversation(providers: dict[str, ScriptedProvider]) -> None:
    from kreafinity.agent import ConversationStore
    from kreafinity.providers import Message, ToolCall

    store = ConversationStore()
    info = store.create(provider="anthropic/claude-opus-5-5")
    store.append(
        info.id,
        [
            Message.user("Liste Documents"),
            # interrupted before the tool result was saved
            Message.assistant("", [ToolCall(id="t1", name="list_directory", arguments={})]),
        ],
    )
    providers["anthropic"] = ScriptedProvider(reply("Je reprends."))
    providers["mistral"] = ScriptedProvider(reply("Bonjour de Mistral."))

    result = runner.invoke(
        cli.app,
        ["chat", "--reprendre", info.id[:6]],
        input="On continue ?\n/fournisseur mistral\nEt toi ?\n/quitter\n",
    )

    assert result.exit_code == 0, result.output
    assert "Reprise de « Liste Documents » (3 messages)." in result.output
    messages, _, model = providers["anthropic"].requests[0]
    assert model == "claude-opus-5-5"  # the provider remembered for this conversation
    assert [m.role for m in messages[1:]] == ["user", "assistant", "tool", "user"]
    saved = store.messages(info.id)
    assert [m.content for m in saved[-4:]] == [
        "On continue ?",
        "Je reprends.",
        "Et toi ?",
        "Bonjour de Mistral.",
    ]
    assert store.get(info.id).provider == "mistral"
    assert len(store.list_conversations()) == 1


def test_resume_unknown_conversation() -> None:
    result = runner.invoke(cli.app, ["chat", "--reprendre", "abcdef"])
    assert result.exit_code == 2
    assert "Aucune conversation ne commence par « abcdef »." in result.output


# --- kreafinity ui ---------------------------------------------------------------------------


def test_ui_opens_the_running_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(cli, "_running_daemon_url", lambda settings: "http://127.0.0.1:27494")
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url) or True)

    result = runner.invoke(cli.app, ["ui"])

    assert result.exit_code == 0, result.output
    assert opened == ["http://127.0.0.1:27494"]  # no secret in the address
    assert "Interface ouverte" in result.output

    printed = runner.invoke(cli.app, ["ui", "--sans-navigateur"])
    assert "Ouvrez cette adresse" in printed.output and len(opened) == 1


def test_ui_reports_a_daemon_that_does_not_start(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[list[str]] = []
    monkeypatch.setattr(cli, "_running_daemon_url", lambda settings: None)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda args, **kwargs: started.append(args))
    monkeypatch.setattr(cli.time, "monotonic", iter([0.0, 0.0, 100.0]).__next__)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)

    result = runner.invoke(cli.app, ["ui", "--sans-navigateur"])

    assert result.exit_code == 1
    assert started and started[0][1:] == ["-m", "kreafinity.api.server"]
    assert "daemon.log" in result.output
