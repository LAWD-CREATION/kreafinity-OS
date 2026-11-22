"""MCP servers in the configuration file (``mcp_servers``), and their secrets.

Example::

    mcp_servers:
      heure:
        command: uvx
        args: [mcp-server-time, --local-timezone=Indian/Reunion]
        auto_approve: [get_current_time]      # tools run without asking
      github:
        command: npx
        args: [-y, "@modelcontextprotocol/server-github"]
        secret_env: [GITHUB_PERSONAL_ACCESS_TOKEN]   # value in the keyring
      maison:
        url: http://homeassistant.local:8123/mcp_server/sse
        token: true                            # bearer token in the keyring

Secrets (tokens, keys) never go in the file: environment variables listed in
``secret_env`` and the HTTP ``token`` are read from the system keyring.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from kreafinity.config.secrets import KEYRING_SERVICE, SecretStoreError

SERVER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
#: Words that make an environment variable a secret (refused in the file).
SECRET_WORDS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH")


class McpConfigError(ValueError):
    """An invalid ``mcp_servers`` entry."""


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """One MCP server: a program to start (stdio) or an address (HTTP)."""

    name: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    secret_env: tuple[str, ...] = ()
    url: str = ""
    token: bool = False
    enabled: bool = True
    auto_approve: tuple[str, ...] = ()

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"

    def describe(self) -> str:
        """The command line or the address, for the user."""
        return self.url or " ".join([self.command, *self.args])


def looks_secret(name: str) -> bool:
    upper = name.upper()
    return any(word in upper for word in SECRET_WORDS)


def parse_servers(raw: Any) -> dict[str, ServerConfig]:
    """Validate the ``mcp_servers`` mapping.

    Raises:
        McpConfigError: With a message naming the bad entry.
    """
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise McpConfigError("'mcp_servers' must map server names to their settings")
    servers = {}
    for name, entry in raw.items():
        servers[str(name)] = parse_server(str(name), entry)
    return servers


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise McpConfigError(f"{where} must be a list of strings")
    return tuple(value)


def parse_server(name: str, entry: Any) -> ServerConfig:
    where = f"mcp_servers.{name}"
    if not SERVER_NAME.match(name):
        raise McpConfigError(
            f"{where}: the name must be lower case letters, digits, - or _ (24 at most)"
        )
    if not isinstance(entry, dict):
        raise McpConfigError(f"{where} must be a mapping")
    known = {"command", "args", "env", "secret_env", "url", "token", "enabled", "auto_approve"}
    unknown = sorted(set(entry) - known)
    if unknown:
        raise McpConfigError(f"{where}: unknown setting {unknown[0]!r}")
    command, url = entry.get("command") or "", entry.get("url") or ""
    if not isinstance(command, str) or not isinstance(url, str):
        raise McpConfigError(f"{where}: 'command' and 'url' must be strings")
    if bool(command) == bool(url):
        raise McpConfigError(f"{where}: give either 'command' (a program) or 'url' (an address)")
    if url:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise McpConfigError(f"{where}.url must start with http:// or https://")
        if parts.password or parts.username:
            raise McpConfigError(f"{where}.url must not contain credentials: use 'token: true'")
    env = entry.get("env") or {}
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise McpConfigError(f"{where}.env must map variable names to text values")
    for variable in env:
        if not ENV_NAME.match(variable):
            raise McpConfigError(f"{where}.env: invalid variable name {variable!r}")
        if looks_secret(variable):
            raise McpConfigError(
                f"{where}.env.{variable}: secrets must not be stored in the configuration "
                "file; list it in 'secret_env' and store it in the keyring"
            )
    secret_env = _strings(entry.get("secret_env"), f"{where}.secret_env")
    for variable in secret_env:
        if not ENV_NAME.match(variable):
            raise McpConfigError(f"{where}.secret_env: invalid variable name {variable!r}")
    for key in ("token", "enabled"):
        if key in entry and not isinstance(entry[key], bool):
            raise McpConfigError(f"{where}.{key} must be true or false")
    return ServerConfig(
        name=name,
        command=command,
        args=_strings(entry.get("args"), f"{where}.args"),
        env=dict(env),
        secret_env=secret_env,
        url=url,
        token=bool(entry.get("token", False)),
        enabled=bool(entry.get("enabled", True)),
        auto_approve=_strings(entry.get("auto_approve"), f"{where}.auto_approve"),
    )


def to_mapping(server: ServerConfig) -> dict[str, Any]:
    """The entry as written in the configuration file (defaults left out)."""
    data: dict[str, Any] = {}
    if server.command:
        data["command"] = server.command
        if server.args:
            data["args"] = list(server.args)
    else:
        data["url"] = server.url
        if server.token:
            data["token"] = True
    if server.env:
        data["env"] = dict(server.env)
    if server.secret_env:
        data["secret_env"] = list(server.secret_env)
    if not server.enabled:
        data["enabled"] = False
    if server.auto_approve:
        data["auto_approve"] = list(server.auto_approve)
    return data


# --- Secrets in the keyring -----------------------------------------------------------------


def _account(server: str, name: str) -> str:
    return f"mcp:{server}:{name}"


def get_secret(server: str, name: str) -> str | None:
    """A server's secret (an environment variable, or ``token``), if stored."""
    import keyring
    from keyring.errors import KeyringError

    try:
        return keyring.get_password(KEYRING_SERVICE, _account(server, name)) or None
    except (KeyringError, RuntimeError):
        return None


def set_secret(server: str, name: str, value: str) -> None:
    """Store a server's secret in the keyring.

    Raises:
        ValueError: Empty or too long.
        SecretStoreError: No keyring, or it refused.
    """
    import keyring
    from keyring.errors import KeyringError

    from kreafinity.config.secrets import keyring_available

    value = value.strip()
    if not value or len(value) > 4000 or "\n" in value:
        raise ValueError("invalid secret")
    if not keyring_available():
        raise SecretStoreError("no system keyring is available to store the secret")
    try:
        keyring.set_password(KEYRING_SERVICE, _account(server, name), value)
    except (KeyringError, RuntimeError) as exc:
        raise SecretStoreError(f"the keyring refused the secret: {exc}") from exc


def delete_secret(server: str, name: str) -> None:
    import keyring
    from keyring.errors import KeyringError, PasswordDeleteError

    with contextlib.suppress(PasswordDeleteError, KeyringError, RuntimeError):
        keyring.delete_password(KEYRING_SERVICE, _account(server, name))
