"""API keys, kept in the system keyring (Secret Service: GNOME Keyring, KWallet…).

Keys never go in the configuration file. For each provider, the key used is,
in order: the provider's environment variable if it is set (explicit
override, e.g. ``ANTHROPIC_API_KEY``), then the key stored in the keyring.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

import keyring
from keyring.backends import fail, null
from keyring.errors import KeyringError, PasswordDeleteError

from kreafinity.providers.registry import get_provider_class

logger = logging.getLogger(__name__)

#: Name under which the keys are filed in the keyring.
KEYRING_SERVICE = "kreafinity"
MAX_KEY_LENGTH = 1000

KeySource = Literal["environment", "keyring", "missing"]


class SecretStoreError(Exception):
    """The system keyring is unavailable or refused the operation."""


def keyring_available() -> bool:
    """Whether a real system keyring can be used on this machine."""
    try:
        backend = keyring.get_keyring()
    except Exception:  # broken keyring configuration
        return False
    return not isinstance(backend, fail.Keyring | null.Keyring)


def get_api_key(provider: str) -> str | None:
    """The key stored in the keyring for `provider`, if any."""
    try:
        return keyring.get_password(KEYRING_SERVICE, provider) or None
    except (KeyringError, RuntimeError) as exc:
        logger.debug("Keyring unavailable: %s", exc)
        return None


def set_api_key(provider: str, key: str) -> None:
    """Store the key of `provider` in the keyring.

    Raises:
        ValueError: The key is empty, too long or contains spaces.
        SecretStoreError: No keyring, or it refused.
    """
    key = key.strip()
    if not key or len(key) > MAX_KEY_LENGTH or any(char.isspace() for char in key):
        raise ValueError("Invalid API key")
    if not keyring_available():
        raise SecretStoreError("No system keyring is available")
    try:
        keyring.set_password(KEYRING_SERVICE, provider, key)
    except (KeyringError, RuntimeError) as exc:
        raise SecretStoreError(str(exc)) from exc


def delete_api_key(provider: str) -> bool:
    """Remove the key of `provider` from the keyring; False if there was none.

    Raises:
        SecretStoreError: No keyring, or it refused.
    """
    if not keyring_available():
        raise SecretStoreError("No system keyring is available")
    try:
        keyring.delete_password(KEYRING_SERVICE, provider)
    except PasswordDeleteError:
        return False
    except (KeyringError, RuntimeError) as exc:
        raise SecretStoreError(str(exc)) from exc
    return True


def api_key_source(provider: str) -> KeySource | None:
    """Where the key of `provider` comes from, or None if it needs no key."""
    provider_class = get_provider_class(provider)
    if not provider_class.requires_api_key:
        return None
    variable = provider_class.api_key_env_var
    if variable and os.environ.get(variable):
        return "environment"
    if get_api_key(provider):
        return "keyring"
    return "missing"


def resolve_provider_options(provider: str, options: dict[str, Any]) -> dict[str, Any]:
    """Add the keyring's API key to a provider's options when it needs one.

    The environment variable, when set, is left to the provider itself.
    """
    provider_class = get_provider_class(provider)
    if not provider_class.requires_api_key or options.get("api_key"):
        return options
    variable = provider_class.api_key_env_var
    if variable and os.environ.get(variable):
        return options
    key = get_api_key(provider)
    return {**options, "api_key": key} if key else options
