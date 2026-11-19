"""Configuration (``~/.config/kreafinity/config.yaml``) and API keys (system keyring)."""

from kreafinity.config.secrets import (
    SecretStoreError,
    api_key_source,
    delete_api_key,
    get_api_key,
    keyring_available,
    resolve_provider_options,
    set_api_key,
)
from kreafinity.config.settings import (
    ConfigError,
    Settings,
    default_config_path,
    load_settings,
    save_setting,
    save_settings,
)

__all__ = [
    "ConfigError",
    "SecretStoreError",
    "Settings",
    "api_key_source",
    "default_config_path",
    "delete_api_key",
    "get_api_key",
    "keyring_available",
    "load_settings",
    "resolve_provider_options",
    "save_setting",
    "save_settings",
    "set_api_key",
]
