from datetime import date
from pathlib import Path

import pytest

from kreafinity.agent import build_system_prompt
from kreafinity.config import ConfigError, Settings, default_config_path, load_settings
from kreafinity.i18n import get_language, set_language, t
from kreafinity.paths import config_dir, data_dir, user_dirs


def write_config(text: str) -> Path:
    path = default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_file() -> None:
    assert load_settings() == Settings()
    assert Settings().provider == "ollama/llama3.1"
    assert Settings().max_iterations == 10


def test_empty_file() -> None:
    write_config("")
    assert load_settings() == Settings()


def test_full_file() -> None:
    write_config(
        """
provider: anthropic/claude-opus-5-5
language: en
max_iterations: 5
allowed_paths: ["~", "/media/usb"]
tools: [system_info]
providers:
  ollama:
    base_url: http://192.168.1.10:11434
future_option: true
"""
    )
    settings = load_settings()
    assert settings.provider == "anthropic/claude-opus-5-5"
    assert settings.language == "en"
    assert settings.max_iterations == 5
    assert settings.allowed_paths == ["~", "/media/usb"]
    assert settings.tools == ["system_info"]
    assert settings.provider_options("ollama") == {"base_url": "http://192.168.1.10:11434"}
    assert settings.provider_options("anthropic") == {}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("max_iterations: beaucoup", "max_iterations"),
        ("max_iterations: 0", "at least 1"),
        ("max_iterations: true", "max_iterations"),
        ("allowed_paths: ~", "allowed_paths"),
        ("providers: [ollama]", "providers"),
        ("- a\n- b", "mapping"),
        ("provider: [", "while parsing"),
    ],
)
def test_invalid_files(text: str, message: str) -> None:
    write_config(text)
    with pytest.raises(ConfigError, match=message):
        load_settings()


def test_api_keys_are_refused_in_config() -> None:
    write_config("providers:\n  anthropic:\n    api_key: sk-not-a-real-key\n")
    with pytest.raises(ConfigError, match="API keys must not be stored"):
        load_settings()


def test_xdg_directories(tmp_path: Path) -> None:
    assert config_dir() == tmp_path / "xdg-config" / "kreafinity"
    assert data_dir() == tmp_path / "xdg-data" / "kreafinity"


def test_user_dirs(home: Path, tmp_path: Path) -> None:
    config = tmp_path / "xdg-config"
    config.mkdir(exist_ok=True)
    (config / "user-dirs.dirs").write_text(
        "# written by xdg-user-dirs-update\n"
        'XDG_DOWNLOAD_DIR="$HOME/Téléchargements"\n'
        'XDG_DOCUMENTS_DIR="$HOME/Documents"\n'
        'XDG_MUSIC_DIR="$HOME/Musique"\n'  # does not exist
        'XDG_DESKTOP_DIR="$HOME/"\n',  # the home itself
        encoding="utf-8",
    )
    assert user_dirs(home) == {
        "DOWNLOAD": home / "Téléchargements",
        "DOCUMENTS": home / "Documents",
    }


def test_system_prompt_lists_user_folders(home: Path) -> None:
    prompt = build_system_prompt(
        language="fr",
        home=home,
        allowed_roots=[home],
        user_dirs={"DOWNLOAD": home / "Téléchargements"},
        today=date(2026, 11, 19),
    )
    assert f"Téléchargements : {home / 'Téléchargements'}" in prompt
    assert "2026-11-19" in prompt


def test_translations() -> None:
    previous = get_language()
    try:
        set_language("fr")
        assert t("chat.bye") == "À bientôt !"
        assert "ollama" in t("hint.ollama_model", model="ollama")
        set_language("en")
        assert t("chat.bye") == "Goodbye!"
        set_language("xx")  # unknown language falls back to French
        assert t("chat.bye") == "À bientôt !"
        assert t("no.such.key") == "no.such.key"
    finally:
        set_language(previous)
