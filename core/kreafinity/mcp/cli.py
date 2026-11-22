"""« kreafinity mcp » : add, list and remove MCP servers (extensions)."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from kreafinity.config import ConfigError, SecretStoreError, load_settings, save_settings
from kreafinity.i18n import t
from kreafinity.mcp import config as mcp_config
from kreafinity.mcp.manager import McpManager

app = typer.Typer(
    help="Extensions MCP : des outils en plus pour l'assistant.", no_args_is_help=True
)
console = Console()
err_console = Console(stderr=True)


def _servers() -> dict[str, dict[str, object]]:
    try:
        return {name: dict(entry) for name, entry in load_settings().mcp_servers.items()}
    except ConfigError as exc:
        err_console.print(Text(str(exc), style="red"))
        raise typer.Exit(2) from exc


def _save(servers: dict[str, dict[str, object]]) -> None:
    try:
        save_settings({"mcp_servers": servers})
    except ConfigError as exc:
        err_console.print(Text(str(exc), style="red"))
        raise typer.Exit(2) from exc


@app.command("list")
def list_servers() -> None:
    """Liste les extensions, leur état et leurs outils (en les démarrant)."""
    servers = _servers()
    if not servers:
        console.print(t("mcp.none"))
        return

    async def check() -> list[dict[str, object]]:
        manager = McpManager()
        manager.configure(servers)
        await manager.wait_ready(30)
        status = manager.status()
        await manager.aclose()
        return status

    table = Table(show_header=True, header_style="bold")
    for column in ("mcp.col.name", "mcp.col.status", "mcp.col.target", "mcp.col.tools"):
        table.add_column(t(column))
    for server in asyncio.run(check()):
        tools = server["tools"]
        assert isinstance(tools, list)
        names = ", ".join(f"{tool['name']}{' ✓' if tool['auto_approve'] else ''}" for tool in tools)
        status = t(f"mcp.status.{server['status']}")
        if server["error"]:
            status += f" — {server['error']}"
        table.add_row(str(server["name"]), status, str(server["target"]), names or "—")
    console.print(table)
    console.print(Text(t("mcp.legend"), style="dim"))


@app.command(
    "add",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def add_server(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Nom court de l'extension, par ex. « heure ».")],
    url: Annotated[str, typer.Option("--url", help="Adresse d'un serveur MCP (HTTP).")] = "",
    token: Annotated[
        bool, typer.Option("--token", help="Le serveur demande un jeton (gardé dans le trousseau).")
    ] = False,
    env: Annotated[
        list[str] | None,
        typer.Option("--env", "-e", help="Variable d'environnement NOM=valeur (pas de secret)."),
    ] = None,
    secret: Annotated[
        list[str] | None,
        typer.Option(
            "--secret", "-s", help="Variable secrète, demandée puis gardée dans le trousseau."
        ),
    ] = None,
) -> None:
    """Ajoute une extension : « kreafinity mcp add heure -- uvx mcp-server-time »."""
    command = list(context.args)
    entry: dict[str, object] = {}
    if command:
        entry["command"], entry["args"] = command[0], command[1:]
    if url:
        entry["url"] = url
    if token:
        entry["token"] = True
    variables = {}
    for item in env or []:
        key, sep, value = item.partition("=")
        if not sep:
            err_console.print(Text(t("mcp.bad_env", item=item), style="red"))
            raise typer.Exit(2)
        variables[key] = value
    if variables:
        entry["env"] = variables
    if secret:
        entry["secret_env"] = list(secret)
    servers = _servers()
    if name in servers:
        err_console.print(Text(t("mcp.exists", name=name), style="red"))
        raise typer.Exit(2)
    try:
        server = mcp_config.parse_server(name, entry)
    except mcp_config.McpConfigError as exc:
        err_console.print(Text(str(exc), style="red"))
        raise typer.Exit(2) from exc
    for variable in [*server.secret_env, *(["token"] if server.token else [])]:
        value = typer.prompt(t("mcp.secret_prompt", name=variable), hide_input=True)
        try:
            mcp_config.set_secret(name, variable, value)
        except (ValueError, SecretStoreError) as exc:
            err_console.print(Text(str(exc), style="red"))
            raise typer.Exit(1) from exc
    servers[name] = mcp_config.to_mapping(server)
    _save(servers)
    console.print(Text(t("mcp.added", name=name), style="green"))


@app.command("remove")
def remove_server(name: Annotated[str, typer.Argument(help="Nom de l'extension.")]) -> None:
    """Retire une extension (et ses secrets du trousseau)."""
    servers = _servers()
    entry = servers.pop(name, None)
    if entry is None:
        err_console.print(Text(t("mcp.unknown", name=name), style="red"))
        raise typer.Exit(1)
    secrets = entry.get("secret_env") or []
    assert isinstance(secrets, list)
    for variable in [*secrets, "token"]:
        mcp_config.delete_secret(name, str(variable))
    _save(servers)
    console.print(t("mcp.removed", name=name))


@app.command("secret")
def set_secret(
    name: Annotated[str, typer.Argument(help="Nom de l'extension.")],
    variable: Annotated[str, typer.Argument(help="Variable secrète (ou « token »).")],
) -> None:
    """Enregistre (ou remplace) un secret d'une extension dans le trousseau."""
    entry = _servers().get(name)
    if entry is None:
        err_console.print(Text(t("mcp.unknown", name=name), style="red"))
        raise typer.Exit(1)
    expected = list(entry.get("secret_env") or []) + (["token"] if entry.get("token") else [])  # type: ignore[call-overload]
    if variable not in expected:
        err_console.print(Text(t("mcp.not_secret", name=name, variable=variable), style="red"))
        raise typer.Exit(2)
    value = typer.prompt(t("mcp.secret_prompt", name=variable), hide_input=True)
    try:
        mcp_config.set_secret(name, variable, value)
    except (ValueError, SecretStoreError) as exc:
        err_console.print(Text(str(exc), style="red"))
        raise typer.Exit(1) from exc
    console.print(Text(t("mcp.secret_saved"), style="green"))


@app.command("approve")
def approve(
    name: Annotated[str, typer.Argument(help="Nom de l'extension.")],
    tool: Annotated[
        str, typer.Argument(help="Outil de l'extension (voir « kreafinity mcp list »).")
    ],
    off: Annotated[
        bool, typer.Option("--off", "--retirer", help="Redemander la validation.")
    ] = False,
) -> None:
    """Laisse un outil d'extension s'exécuter sans validation (ou --off pour l'annuler)."""
    servers = _servers()
    entry = servers.get(name)
    if entry is None:
        err_console.print(Text(t("mcp.unknown", name=name), style="red"))
        raise typer.Exit(1)
    approved = [str(n) for n in entry.get("auto_approve") or []]  # type: ignore[attr-defined]
    approved = [n for n in approved if n != tool] if off else sorted({*approved, tool})
    entry["auto_approve"] = approved
    if not approved:
        entry.pop("auto_approve")
    _save(servers)
    console.print(t("mcp.approve_off" if off else "mcp.approve_on", tool=tool, name=name))
