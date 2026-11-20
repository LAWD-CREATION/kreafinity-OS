"""User settings, read from ``~/.config/kreafinity/config.yaml``.

Example::

    provider: ollama/llama3.1      # default provider and model
    fallback: ollama/llama3.1      # used when the provider cannot be reached (optional)
    language: fr                   # language of the assistant
    max_iterations: 10             # LLM calls allowed per request
    port: 27494                    # port of the local daemon (127.0.0.1 only)
    allowed_paths: ["~"]           # folders the tools may access
    tools: null                    # enabled tools (null = all)
    check_updates: true            # look for a new version once a day (GitHub)
    memory: true                   # remember things across conversations
    memory_review: true            # after a conversation, learn from it (one more AI call)
    soul_evolution: true           # the assistant grows its own personality (its soul)
    providers:                     # options passed to each adapter
      ollama:
        base_url: http://127.0.0.1:11434

API keys never go in this file: they are kept in the system keyring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from kreafinity.paths import config_dir

logger = logging.getLogger(__name__)

# Option names that would hold a secret; refused in the configuration file.
SECRET_OPTION_NAMES = frozenset({"api_key", "apikey", "token", "secret", "password"})


class ConfigError(Exception):
    """The configuration file is invalid."""


def default_config_path() -> Path:
    """Location of the configuration file."""
    return config_dir() / "config.yaml"


@dataclass
class Settings:
    """Validated user settings, with defaults for everything."""

    provider: str = "ollama/llama3.1"
    fallback: str | None = None
    language: str = "fr"
    max_iterations: int = 10
    port: int = 27494
    allowed_paths: list[str] = field(default_factory=lambda: ["~"])
    tools: list[str] | None = None
    check_updates: bool = True
    memory: bool = True
    memory_review: bool = True
    soul_evolution: bool = True
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)

    def provider_options(self, name: str) -> dict[str, Any]:
        """Constructor options configured for the provider `name`."""
        return dict(self.providers.get(name, {}))


def load_settings(path: Path | None = None) -> Settings:
    """Load the settings file, or return the defaults if it does not exist.

    Raises:
        ConfigError: The file cannot be parsed, has a wrong value type, or
            contains a secret.
    """
    path = path or default_config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Settings()
    except OSError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if data is None:
        return Settings()
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return _from_mapping(data, path)


def _from_mapping(data: dict[str, Any], path: Path) -> Settings:
    known = {f.name for f in fields(Settings)}
    for key in data.keys() - known:
        logger.warning("%s: unknown setting %r ignored", path, key)
    values = {key: value for key, value in data.items() if key in known}

    def check(key: str, expected: type | tuple[type, ...], description: str) -> None:
        value = values.get(key)
        if key in values and (not isinstance(value, expected) or isinstance(value, bool)):
            raise ConfigError(f"{path}: {key!r} must be {description}")

    check("provider", str, "a string such as 'ollama/llama3.1'")
    if values.get("fallback") is not None:
        check("fallback", str, "a string such as 'ollama/llama3.1'")
    check("language", str, "a language code such as 'fr'")
    check("max_iterations", int, "an integer")
    if values.get("max_iterations", 1) < 1:
        raise ConfigError(f"{path}: 'max_iterations' must be at least 1")
    check("port", int, "an integer")
    if not 1024 <= values.get("port", 27494) <= 65535:
        raise ConfigError(f"{path}: 'port' must be between 1024 and 65535")
    for key in ("check_updates", "memory", "memory_review", "soul_evolution"):
        if key in values and not isinstance(values[key], bool):
            raise ConfigError(f"{path}: {key!r} must be true or false")
    for key in ("allowed_paths", "tools"):
        value = values.get(key)
        if value is None and key == "tools":
            continue
        if key in values and not (
            isinstance(value, list) and all(isinstance(v, str) for v in value)
        ):
            raise ConfigError(f"{path}: {key!r} must be a list of strings")

    providers = values.get("providers") or {}
    if not isinstance(providers, dict) or not all(isinstance(v, dict) for v in providers.values()):
        raise ConfigError(f"{path}: 'providers' must map provider names to options")
    for name, options in providers.items():
        secrets = SECRET_OPTION_NAMES & {str(k).lower() for k in options}
        if secrets:
            raise ConfigError(
                f"{path}: providers.{name}.{sorted(secrets)[0]} — API keys must not be "
                "stored in the configuration file"
            )
    values["providers"] = providers
    return Settings(**values)


def save_setting(key: str, value: Any, path: Path | None = None) -> Path:
    """Set one top-level setting in the configuration file, keeping the others.

    See `save_settings`.
    """
    return save_settings({key: value}, path)


def save_settings(changes: dict[str, Any], path: Path | None = None) -> Path:
    """Change top-level settings in the configuration file, keeping the others.

    All changes are validated together before anything is written; the file
    is created if needed and replaced atomically. Comments in the file are
    not preserved. A value of ``None`` for ``fallback`` or ``tools`` restores
    the default.

    Raises:
        ConfigError: The current file or the result is invalid, or a key is unknown.
    """
    known = {f.name for f in fields(Settings)}
    unknown = sorted(changes.keys() - known)
    if unknown:
        raise ConfigError(f"Unknown setting {unknown[0]!r}")
    path = path or default_config_path()
    load_settings(path)  # refuse to rewrite an invalid file
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        data = {}
    data.update(changes)
    _from_mapping(data, path)  # validate the result before writing
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".yaml.tmp")
    temporary.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    temporary.replace(path)
    return path
