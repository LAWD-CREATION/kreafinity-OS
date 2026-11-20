"""Command-line interface for Kreafinity OS."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
import webbrowser
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from kreafinity import __version__
from kreafinity.agent import (
    ActionLog,
    Agent,
    AgentDone,
    AgentEvent,
    ApprovalRequest,
    Conversation,
    ConversationInfo,
    ConversationNotFoundError,
    ConversationStore,
    ProviderFallback,
    ToolCallFinished,
    ToolCallStarted,
    complete_unanswered_calls,
)
from kreafinity.config import (
    ConfigError,
    SecretStoreError,
    Settings,
    api_key_source,
    default_config_path,
    delete_api_key,
    keyring_available,
    load_settings,
    resolve_provider_options,
    save_setting,
    set_api_key,
)
from kreafinity.i18n import set_language, t
from kreafinity.paths import config_dir, data_dir
from kreafinity.providers import (
    LLMProvider,
    ProviderAuthError,
    ProviderError,
    ProviderUnavailableError,
    TextDelta,
    available_providers,
    create_provider,
    get_provider_class,
    parse_model_spec,
)
from kreafinity.system import integration
from kreafinity.tools import RiskLevel, ToolContext, ToolRegistry, printable

app = typer.Typer(
    name="kreafinity",
    help="Kreafinity OS — l'assistant système piloté par IA.",
    no_args_is_help=True,
)
provider_app = typer.Typer(help="Choisir et gérer les fournisseurs d'IA.", no_args_is_help=True)
app.add_typer(provider_app, name="provider")
desktop_app = typer.Typer(
    help="Intégrer Kreafinity au bureau KDE Plasma : recherche, barre des tâches, thème.",
    no_args_is_help=True,
)
app.add_typer(desktop_app, name="desktop")
app.add_typer(desktop_app, name="bureau", hidden=True)
console = Console()
err_console = Console(stderr=True)

ProviderOption = Annotated[
    str | None,
    typer.Option(
        "--provider",
        "-p",
        help="Fournisseur et modèle à utiliser, par ex. « ollama/llama3.1 » ou « anthropic ».",
    ),
]
QUIT_COMMANDS = frozenset({"/quitter", "/quit", "/exit", "/q"})
PROVIDER_COMMANDS = frozenset({"/fournisseur", "/provider", "/ia"})
HELP_COMMANDS = frozenset({"/aide", "/help", "/?"})


@app.callback()
def main() -> None:
    """Kreafinity OS — l'assistant système piloté par IA."""


@app.command()
def version() -> None:
    """Affiche la version de Kreafinity OS."""
    from kreafinity.update import installation

    record = installation()
    detail = ""
    if record is not None and record.commit:
        detail = f" ({record.commit[:7]}, {record.ref})"
    console.print(f"Kreafinity OS [bold]{__version__}[/bold]{detail}")


@app.command("memory")
def memory_command(
    forget: Annotated[
        bool,
        typer.Option("--forget", "--oublier", help="Tout effacer (notes, savoir-faire, âme)."),
    ] = False,
    soul_reset: Annotated[
        bool,
        typer.Option(
            "--reset-soul", "--nouvelle-ame", help="Rendre à Kreafinity sa personnalité d'origine."
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Ne pas demander de confirmation.")
    ] = False,
) -> None:
    """Montre ce dont Kreafinity se souvient de vous et son âme (sa personnalité), ou l'efface."""
    from kreafinity.memory import MemoryStore

    settings = _load_settings()
    store = MemoryStore()
    if soul_reset:
        store.reset_soul()
        console.print(Text(t("memory.cli.soul_reset"), style="green"))
        return
    if forget:
        if not yes and not typer.confirm(t("memory.cli.forget_confirm"), default=False):
            raise typer.Exit(1)
        store.forget_all()
        console.print(Text(t("memory.cli.forgotten"), style="green"))
        return
    title = "memory.cli.soul" if store.has_own_soul() else "memory.cli.soul_seed"
    console.print(Text(t(title), style="bold"))
    for line in store.soul(settings.language).splitlines():
        console.print(Text(f"  {line}"))
    if store.is_empty():
        console.print(t("memory.cli.empty"))
        return
    for section in ("user", "notes"):
        entries = store.entries(section)
        if entries:
            console.print(Text(t(f"memory.cli.{section}"), style="bold"))
            for entry in entries:
                console.print(Text(f"  • {entry}"))
    skills = store.skills()
    if skills:
        console.print(Text(t("memory.cli.skills"), style="bold"))
        for skill in skills:
            console.print(Text(f"  • {skill.title} — {skill.description}"))
    console.print(Text(t("memory.cli.where", path=store.folder), style="dim"))


@app.command("update")
def update_command(
    check_only: Annotated[
        bool, typer.Option("--check", help="Seulement dire si une mise à jour existe.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Ne pas demander de confirmation.")
    ] = False,
) -> None:
    """Met à jour Kreafinity depuis GitHub (la page web propose aussi un bouton)."""
    from kreafinity import update as updates

    _load_settings()
    record = updates.installation()
    if record is None:
        err_console.print(Text(t("update.cli.dev"), style="yellow"))
        raise typer.Exit(1)
    try:
        result = asyncio.run(updates.check(record))
    except updates.UpdateError as exc:
        err_console.print(Text(t("update.cli.check_failed", detail=exc), style="red"))
        raise typer.Exit(1) from exc
    result.notified = updates.load_check().notified
    updates.save_check(result)
    if not result.available or result.latest is None:
        console.print(Text(t("update.cli.up_to_date"), style="green"))
        return
    console.print(Text(t("update.cli.available", count=len(result.changes)), style="bold"))
    for change in result.changes:
        console.print(Text(f"  • {change.title}"))
    if check_only:
        console.print(Text(t("update.cli.hint"), style="dim"))
        return
    if not yes and not typer.confirm(t("update.cli.confirm"), default=True):
        raise typer.Exit(1)
    try:
        updates.apply(result.latest, log=lambda line: console.print(Text(line, style="dim")))
    except updates.UpdateError as exc:
        err_console.print(Text(t("update.cli.failed", detail=exc), style="red"))
        raise typer.Exit(1) from exc
    console.print(Text(t("update.cli.done"), style="green"))


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Votre demande, entre guillemets.")],
    provider: ProviderOption = None,
) -> None:
    """Pose une question à l'assistant et affiche sa réponse."""
    agent = _build_agent(_load_settings(), provider)
    history = _History(_open_store(), Conversation(), stored=False, provider=provider)

    async def run() -> bool:
        try:
            return await _run_turn(agent, history.conversation, question)
        finally:
            history.save()
            await _close_providers(agent)

    if not asyncio.run(run()):
        raise typer.Exit(1)


@app.command()
def chat(
    provider: ProviderOption = None,
    resume: Annotated[
        str | None,
        typer.Option(
            "--reprendre",
            "--resume",
            "-r",
            help="Reprendre une conversation de l'historique (identifiant ou son début).",
        ),
    ] = None,
) -> None:
    """Ouvre une conversation interactive avec l'assistant."""
    settings = _load_settings()
    store = _open_store()
    conversation, stored, spec = Conversation(), False, provider
    if resume:
        info = _find_conversation(store, resume)
        assert store is not None  # _find_conversation exits otherwise
        loaded = store.messages(info.id)
        messages = complete_unanswered_calls(loaded)
        if messages != loaded:
            store.replace_messages(info.id, messages)
        conversation, stored, spec = (
            Conversation(id=info.id, messages=messages),
            True,
            (provider or info.provider),
        )
    agent = _build_agent(settings, spec)
    history = _History(store, conversation, stored=stored, provider=spec)
    console.print(t("chat.welcome", provider=_describe(agent.provider, agent.model)))
    if resume:
        console.print(
            Text(
                t("chat.resumed", title=info.title or "…", count=len(conversation.messages)),
                style="green",
            )
        )
    console.print(Text(t("chat.help"), style="dim"))

    async def run() -> None:
        replaced: list[LLMProvider] = []
        try:
            while True:
                try:
                    text = await asyncio.to_thread(
                        console.input, f"\n[bold cyan]{t('chat.you')}[/] > "
                    )
                except (EOFError, KeyboardInterrupt):
                    break
                text = text.strip()
                command, _, argument = text.partition(" ")
                if command.lower() in QUIT_COMMANDS:
                    break
                if command.lower() in PROVIDER_COMMANDS:
                    if _switch_provider(agent, settings, argument.strip(), replaced):
                        history.set_provider(argument.strip())
                elif command.lower() in HELP_COMMANDS:
                    console.print(Text(t("chat.help"), style="dim"))
                elif text:
                    console.print()
                    try:
                        await _run_turn(agent, conversation, text)
                    finally:
                        history.save()
        finally:
            await _close_providers(agent, *replaced)

    asyncio.run(run())
    console.print(t("chat.bye"))


@provider_app.command("list")
def provider_list() -> None:
    """Liste les fournisseurs d'IA disponibles."""
    settings = _load_settings()
    try:
        default_name, default_model = _parse_spec(settings.provider)
        fallback_name = _parse_spec(settings.fallback)[0] if settings.fallback else None
    except _UserError as exc:
        err_console.print(Text(str(exc), style="red"))
        raise typer.Exit(2) from exc
    table = Table(title=t("provider.list.title"), title_justify="left")
    for column in (
        "provider.column.id",
        "provider.column.name",
        "provider.column.where",
        "provider.column.key",
        "",
    ):
        table.add_column(t(column) if column else "")
    for name in available_providers():
        provider_class = get_provider_class(name)
        source = api_key_source(name)
        marks = []
        if name == default_name:
            marks.append(
                t("provider.mark.default", model=default_model or t("provider.default_model"))
            )
        if name == fallback_name:
            marks.append(t("provider.mark.fallback"))
        table.add_row(
            name,
            provider_class.display_name,
            t("provider.local" if provider_class.is_local else "provider.online"),
            Text(
                t(f"provider.key.{source}") if source else "—",
                style="yellow" if source == "missing" else "",
            ),
            Text(", ".join(marks), style="bold green"),
        )
    console.print(table)


@provider_app.command("key")
def provider_key(
    name: Annotated[str, typer.Argument(help="Fournisseur : anthropic, openai, mistral…")],
    delete: Annotated[
        bool, typer.Option("--supprimer", "--delete", help="Retirer la clé du trousseau.")
    ] = False,
) -> None:
    """Enregistre la clé API d'un fournisseur dans le trousseau système."""
    _load_settings()
    if name not in available_providers():
        message = t("error.unknown_provider", name=name, available=", ".join(available_providers()))
        err_console.print(Text(message, style="red"))
        raise typer.Exit(2)
    provider_class = get_provider_class(name)
    if not provider_class.requires_api_key:
        console.print(t("provider.key.not_needed", provider=provider_class.display_name))
        return
    if not keyring_available():
        err_console.print(
            Text(
                t("provider.key.no_keyring", variable=provider_class.api_key_env_var or ""),
                style="red",
            )
        )
        raise typer.Exit(1)
    try:
        if delete:
            removed = delete_api_key(name)
            console.print(t("provider.key.deleted" if removed else "provider.key.none_stored"))
            return
        key = typer.prompt(
            t("provider.key.prompt", provider=provider_class.display_name), hide_input=True
        )
        set_api_key(name, key)
    except ValueError as exc:
        err_console.print(Text(t("provider.key.invalid"), style="red"))
        raise typer.Exit(2) from exc
    except SecretStoreError as exc:
        err_console.print(Text(t("provider.key.store_failed", detail=exc), style="red"))
        raise typer.Exit(1) from exc
    console.print(
        Text(t("provider.key.saved", provider=provider_class.display_name), style="green")
    )


@provider_app.command("models")
def provider_models(
    name: Annotated[
        str | None, typer.Argument(help="Fournisseur (par défaut : celui configuré).")
    ] = None,
) -> None:
    """Liste les modèles proposés par un fournisseur."""
    settings = _load_settings()
    provider, _ = _make_provider_or_exit(settings, name or settings.provider)

    async def run() -> list[str]:
        try:
            return [m.id for m in await provider.list_models()]
        finally:
            await provider.aclose()

    try:
        model_ids = asyncio.run(run())
    except ProviderError as exc:
        _print_provider_error(provider, None, exc)
        raise typer.Exit(1) from exc
    if not model_ids:
        console.print(t("provider.models.none"))
    for model_id in model_ids:
        console.print(Text(f"{provider.name}/{model_id}"))


@provider_app.command("use")
def provider_use(
    spec: Annotated[str, typer.Argument(help="Par ex. « ollama/llama3.1 » ou « mistral ».")],
) -> None:
    """Choisit le fournisseur et le modèle utilisés par défaut."""
    settings = _load_settings()
    provider, model = _make_provider_or_exit(settings, spec)
    try:
        save_setting("provider", spec.strip())
    except ConfigError as exc:
        err_console.print(Text(t("error.config", detail=exc), style="red"))
        raise typer.Exit(2) from exc
    console.print(t("provider.default_set", provider=_describe(provider, model)))


@app.command("history")
def history_command(
    limit: Annotated[
        int, typer.Option("--limit", "-n", min=1, max=500, help="Nombre de conversations.")
    ] = 20,
) -> None:
    """Affiche les dernières conversations (CLI et interface web)."""
    _load_settings()
    store = _open_store()
    if store is None:
        raise typer.Exit(1)
    conversations = store.list_conversations(limit=limit)
    if not conversations:
        console.print(t("history.empty"))
        return
    table = Table(title=t("history.title"), title_justify="left")
    for key in ("id", "title", "provider", "updated", "messages"):
        table.add_column(t(f"history.column.{key}"), overflow="fold")
    for info in conversations:
        updated = datetime.fromisoformat(info.updated_at).astimezone()
        table.add_row(
            info.id[:8],
            Text(printable(info.title or "…")),
            info.provider or "",
            updated.strftime(t("history.date_format")),
            str(info.message_count),
        )
    console.print(table)
    console.print(Text(t("history.hint"), style="dim"))


@app.command()
def ui(
    browser: Annotated[
        bool,
        typer.Option(
            "--navigateur/--sans-navigateur",
            "--browser/--no-browser",
            help="Ouvrir le navigateur (sinon, afficher seulement l'adresse).",
        ),
    ] = True,
) -> None:
    """Ouvre l'interface web de Kreafinity (en lançant le démon si besoin)."""
    settings = _load_settings()
    url = _running_daemon_url(settings) or _start_daemon(settings)
    # No secret in the address: the daemon recognizes the user's own browser.
    if browser and webbrowser.open(url):
        console.print(t("ui.opened", url=url))
    else:
        console.print(t("ui.address", url=url))


def _running_daemon_url(settings: Settings) -> str | None:
    """Address of the running daemon, if it answers."""
    from kreafinity.api.daemon_info import HOST, read_daemon_info

    info = read_daemon_info()
    url = info["url"] if info and isinstance(info.get("url"), str) else None
    url = url or f"http://{HOST}:{settings.port}"
    try:
        health = httpx.get(f"{url}/api/health", timeout=2).json()
    except (httpx.HTTPError, ValueError):
        return None
    return url if isinstance(health, dict) and health.get("name") == "kreafinity" else None


def _start_daemon(settings: Settings) -> str:
    """Start the daemon in the background and wait until it answers.

    Through its systemd user service when it is installed, so that it stays
    managed by systemd; otherwise as a detached process.
    """
    from kreafinity.api.client import launch_daemon

    log_path = data_dir() / "daemon.log"
    console.print(Text(t("ui.starting"), style="dim"))
    launch_daemon()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        url = _running_daemon_url(settings)
        if url:
            return url
        time.sleep(0.3)
    err_console.print(Text(t("ui.start_failed", log=log_path), style="red"))
    raise typer.Exit(1)


#: Checks that a Python interpreter has GTK 3 and WebKitGTK (needed by the bubble).
GUI_CHECK = """
import gi
gi.require_version("Gtk", "3.0")
try:
    gi.require_version("WebKit2", "4.1")
except ValueError:
    gi.require_version("WebKit2", "4.0")
from gi.repository import WebKit2
"""


@app.command()
def bubble(
    autostart: Annotated[
        bool | None,
        typer.Option(
            "--autostart/--no-autostart",
            help="Afficher (ou non) la bulle automatiquement à l'ouverture de session.",
        ),
    ] = None,
) -> None:
    """Affiche la bulle Kreafinity en bas à droite du bureau."""
    settings = _load_settings()
    if autostart is not None:
        path = integration.autostart_path()
        if autostart:
            path.parent.mkdir(parents=True, exist_ok=True)
            command = integration.python_command("kreafinity.cli", "bubble")
            path.write_text(
                integration.autostart_entry(command, t("bubble.autostart_comment")),
                encoding="utf-8",
            )
            console.print(t("bubble.autostart_on", path=path))
        else:
            path.unlink(missing_ok=True)
            console.print(t("bubble.autostart_off"))
        return

    from kreafinity.api.web import bubble_script

    script = bubble_script()
    python = _gui_python()
    if script is None or python is None:
        err_console.print(Text(t("bubble.no_gui"), style="red"))
        raise typer.Exit(1)
    url = _running_daemon_url(settings) or _start_daemon(settings)
    log_path = data_dir() / "bubble.log"
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as log:
        subprocess.Popen(
            [python, str(script), "--url", url, "--lang", settings.language],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    console.print(t("bubble.started"))


# French alias: « kreafinity bulle »
app.command("bulle", hidden=True)(bubble)


def _gui_python() -> str | None:
    """A Python interpreter with GTK 3 and WebKitGTK (the system one, usually)."""
    system = sorted(
        (
            str(p)
            for p in Path("/usr/bin").glob("python3.*")
            if re.fullmatch(r"python3\.\d+", p.name)
        ),
        key=lambda name: [int(part) for part in name.rsplit("python", 1)[1].split(".")],
        reverse=True,
    )
    for candidate in dict.fromkeys([sys.executable, "/usr/bin/python3", *system]):
        if not os.access(candidate, os.X_OK):
            continue
        try:
            check = subprocess.run(
                [candidate, "-c", GUI_CHECK], capture_output=True, timeout=30, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if check.returncode == 0:
            return candidate
    return None


@app.command()
def setup(
    uninstall: Annotated[
        bool,
        typer.Option("--uninstall", "--desinstaller", help="Retirer l'intégration au bureau."),
    ] = False,
    bubble: Annotated[
        bool,
        typer.Option("--bubble/--no-bubble", help="Afficher la bulle à l'ouverture de session."),
    ] = True,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh", hidden=True, help="Après une mise à jour : réécrire ce qui est installé."
        ),
    ] = False,
) -> None:
    """Intègre Kreafinity à votre session : démarrage automatique, menu, bulle."""
    settings = _load_settings()
    if refresh:
        _refresh_integration(settings.port)
        return
    if uninstall:
        report = integration.uninstall()
        for key, values in report.done:
            console.print(t(key, **values))
        console.print(Text(t("setup.uninstalled"), style="green"))
        return

    from kreafinity.api.web import web_root

    gui = bubble and _gui_python() is not None
    if bubble and not gui:
        err_console.print(Text(t("setup.bubble_unavailable"), style="yellow"))
    root = web_root()
    report = integration.install(
        bubble=gui,
        icon_source=root / "img" / "logo.svg" if root else None,
        autostart_comment=t("bubble.autostart_comment"),
    )
    for key, values in report.done:
        console.print(t(key, **values))
    console.print(Text(t("setup.done"), style="green"))


def _refresh_integration(port: int) -> None:
    """Rewrite the installed session files with this version, keeping the user's choices."""
    from kreafinity.api.web import web_root
    from kreafinity.plasma import install as plasma

    root = web_root()
    if integration.service_installed() or integration.menu_entry_path().exists():
        integration.install(
            bubble=integration.autostart_path().exists(),
            icon_source=root / "img" / "logo.svg" if root else None,
            autostart_comment=t("bubble.autostart_comment"),
            restart=False,  # the updater restarts everything afterwards
        )
    version = plasma.plasma_version()
    if plasma.is_installed() and version in plasma.SUPPORTED_VERSIONS:
        plasma.install(version, port=port, add_widget=False)


@app.command()
def uninstall(
    purge: Annotated[
        bool,
        typer.Option(
            "--purge", help="Effacer aussi réglages, clés API, conversations et journaux."
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Ne pas demander de confirmation.")
    ] = False,
) -> None:
    """Désinstalle Kreafinity (vos données sont gardées, sauf avec --purge)."""
    _load_settings()
    question = "uninstall.confirm_purge" if purge else "uninstall.confirm"
    if not yes and not typer.confirm(t(question), default=False):
        raise typer.Exit(1)

    from kreafinity.plasma import install as plasma

    if plasma.is_installed():
        for key, values in plasma.remove(plasma.plasma_version()).done:
            console.print(t(key, **values))
    for key, values in integration.uninstall().done:
        console.print(t(key, **values))
    venv = data_dir() / "venv"
    for process in integration.processes_running_from(venv):  # bubble, on-demand daemon
        with contextlib.suppress(Exception):
            process.terminate()
            console.print(t("uninstall.stopped", pid=process.pid))
    for name in ("kreafinity", "kreafinity-core"):
        link = Path.home() / ".local" / "bin" / name
        if link.is_symlink() and Path(os.readlink(link)).is_relative_to(venv):
            link.unlink()
            console.print(t("setup.removed", path=link))
    if purge:
        for name in available_providers():
            if get_provider_class(name).requires_api_key:
                with contextlib.suppress(SecretStoreError):
                    delete_api_key(name)
        shutil.rmtree(config_dir(), ignore_errors=True)
        shutil.rmtree(data_dir(), ignore_errors=True)  # includes the environment itself
        console.print(t("uninstall.purged"))
    elif venv.is_dir():
        shutil.rmtree(venv, ignore_errors=True)
        console.print(t("setup.removed", path=venv))
    console.print(Text(t("uninstall.done" if not purge else "uninstall.done_purge"), style="green"))


@desktop_app.command("install")
def desktop_install(
    theme: Annotated[
        bool, typer.Option("--theme", help="Appliquer aussi les couleurs et le fond d'écran.")
    ] = False,
    layout: Annotated[
        bool,
        typer.Option(
            "--layout", help="Réorganiser le bureau à la façon de Windows (remplace vos panneaux)."
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Ne pas demander de confirmation.")
    ] = False,
    plasma_version: Annotated[
        int | None,
        typer.Option("--plasma", help="Version de Plasma (5 ou 6), si elle n'est pas détectée."),
    ] = None,
) -> None:
    """Ajoute Kreafinity à KDE Plasma : recherche du menu démarrer, widget, thème."""
    from kreafinity.plasma import install as plasma

    settings = _load_settings()
    version = plasma_version or plasma.plasma_version()
    if version is None:
        err_console.print(Text(t("desktop.no_plasma"), style="red"))
        raise typer.Exit(1)
    if layout and not yes and not typer.confirm(t("desktop.confirm_layout"), default=False):
        raise typer.Exit(1)
    try:
        report = plasma.install(version, port=settings.port)
    except plasma.PlasmaError as exc:
        err_console.print(Text(t("desktop.error", detail=exc), style="red"))
        raise typer.Exit(1) from exc
    if theme or layout:
        report.done += plasma.apply_theme(version, layout=layout).done
    for key, values in report.done:
        console.print(t(key, **values))
    console.print(Text(t("desktop.done"), style="green"))


@desktop_app.command("remove")
def desktop_remove() -> None:
    """Retire l'intégration à KDE Plasma (recherche, widget, thème)."""
    from kreafinity.plasma import install as plasma

    _load_settings()
    for key, values in plasma.remove(plasma.plasma_version()).done:
        console.print(t(key, **values))
    console.print(Text(t("desktop.removed"), style="green"))


@app.command()
def daemon(
    port: Annotated[
        int | None, typer.Option(help="Port d'écoute (par défaut : réglage « port »).")
    ] = None,
) -> None:
    """Lance le démon Kreafinity (API locale pour l'interface web), sur 127.0.0.1."""
    from kreafinity.api.server import DaemonError, serve  # imported here: slow to load

    _load_settings()
    try:
        serve(port)
    except DaemonError as exc:
        err_console.print(Text(str(exc), style="red"))
        raise typer.Exit(1) from exc


@app.command("config")
def show_config() -> None:
    """Affiche la configuration utilisée."""
    settings = _load_settings()
    path = default_config_path()
    console.print(Text(t("config.path", path=path)))
    if not path.exists():
        console.print(Text(t("config.missing"), style="dim"))
    console.print(
        Text(yaml.safe_dump(asdict(settings), allow_unicode=True, sort_keys=False).rstrip())
    )


class _UserError(Exception):
    """A problem to show to the user, without a traceback."""


def _open_store() -> ConversationStore | None:
    """The shared conversation history, or None (with a warning) if unavailable."""
    try:
        return ConversationStore()
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        err_console.print(Text(t("history.unavailable", detail=exc), style="yellow"))
        return None


def _find_conversation(store: ConversationStore | None, prefix: str) -> ConversationInfo:
    """The stored conversation whose id starts with `prefix` (exits if none or several)."""
    if store is None:
        raise typer.Exit(1)
    prefix = prefix.strip().lower()
    matches = [
        info
        for info in store.list_conversations(limit=100_000)
        if prefix and info.id.startswith(prefix)
    ]
    if len(matches) == 1:
        return matches[0]
    key = "history.ambiguous" if len(matches) > 1 else "history.not_found"
    err_console.print(Text(t(key, id=prefix), style="red"))
    raise typer.Exit(2)


class _History:
    """Saves a CLI conversation in the shared history (also shown in the web interface)."""

    def __init__(
        self,
        store: ConversationStore | None,
        conversation: Conversation,
        *,
        stored: bool,
        provider: str | None,
    ) -> None:
        self.store = store
        self.conversation = conversation
        self.stored = stored
        self.provider = provider
        self.saved = len(conversation.messages)

    def save(self) -> None:
        """Store the messages added since the last call."""
        new = self.conversation.messages[self.saved :]
        if self.store is None or not new:
            return
        try:
            if not self.stored:
                info = self.store.create(provider=self.provider)
                self.conversation.id = info.id
                self.stored = True
            self.store.append(self.conversation.id, new)
            self.saved = len(self.conversation.messages)
        except (sqlite3.Error, OSError, ConversationNotFoundError) as exc:
            err_console.print(Text(t("history.save_failed", detail=exc), style="yellow"))
            self.store = None  # keep chatting without history

    def set_provider(self, spec: str) -> None:
        """Remember the provider chosen for this conversation."""
        self.provider = spec
        if self.store is not None and self.stored:
            with contextlib.suppress(sqlite3.Error, OSError, ConversationNotFoundError):
                self.store.set_provider(self.conversation.id, spec)


def _load_settings() -> Settings:
    try:
        settings = load_settings()
    except ConfigError as exc:
        err_console.print(Text(t("error.config", detail=exc), style="red"))
        raise typer.Exit(2) from exc
    set_language(settings.language)
    return settings


def _parse_spec(spec: str) -> tuple[str, str | None]:
    try:
        return parse_model_spec(spec)
    except ValueError as exc:
        raise _UserError(str(exc)) from exc


def _make_provider(settings: Settings, spec: str) -> tuple[LLMProvider, str | None]:
    """Create the provider named in ``"provider/model"`` with its configured options."""
    name, model = _parse_spec(spec)
    if name not in available_providers():
        raise _UserError(
            t("error.unknown_provider", name=name, available=", ".join(available_providers()))
        )
    try:
        options = resolve_provider_options(name, settings.provider_options(name))
        return create_provider(name, **options), model
    except TypeError as exc:
        raise _UserError(t("error.provider_options", name=name, detail=exc)) from exc


def _make_provider_or_exit(settings: Settings, spec: str) -> tuple[LLMProvider, str | None]:
    try:
        return _make_provider(settings, spec)
    except _UserError as exc:
        err_console.print(Text(str(exc), style="red"))
        raise typer.Exit(2) from exc


def _build_agent(settings: Settings, provider_spec: str | None) -> Agent:
    spec = provider_spec or settings.provider
    provider, model = _make_provider_or_exit(settings, spec)
    fallback = None
    try:
        if settings.fallback and _parse_spec(settings.fallback) != _parse_spec(spec):
            fallback = _make_provider_or_exit(settings, settings.fallback)
    except _UserError as exc:
        err_console.print(Text(str(exc), style="red"))
        raise typer.Exit(2) from exc
    from kreafinity.memory import MEMORY_TOOLS, MemoryStore

    return Agent(
        provider,
        ToolRegistry.load(settings.tools, exclude=() if settings.memory else MEMORY_TOOLS),
        ToolContext.create(allowed_paths=settings.allowed_paths),
        model=model,
        language=settings.language,
        max_iterations=settings.max_iterations,
        action_log=ActionLog(),
        fallback=fallback,
        memory=MemoryStore() if settings.memory else None,
    )


def _switch_provider(
    agent: Agent, settings: Settings, spec: str, replaced: list[LLMProvider]
) -> bool:
    """Handle ``/fournisseur [spec]`` in a chat: show or change the provider.

    Returns whether the provider was changed.
    """
    if not spec:
        console.print(t("provider.current", provider=_describe(agent.provider, agent.model)))
        return False
    try:
        provider, model = _make_provider(settings, spec)
    except _UserError as exc:
        console.print(Text(str(exc), style="red"))
        return False
    replaced.append(agent.provider)
    agent.use_provider(provider, model)
    console.print(Text(t("provider.switched", provider=_describe(provider, model)), style="green"))
    return True


def _describe(provider: LLMProvider, model: str | None) -> str:
    return f"{provider.display_name} · {model or provider.default_model}"


async def _close_providers(agent: Agent, *others: LLMProvider) -> None:
    providers = {id(p): p for p in (agent.provider, *others)}
    if agent.fallback is not None:
        providers[id(agent.fallback[0])] = agent.fallback[0]
    for provider in providers.values():
        await provider.aclose()


async def _run_turn(agent: Agent, conversation: Conversation, text: str) -> bool:
    """Run one request and print it; return False if it failed."""
    renderer = _Renderer(agent)
    agent.approver = renderer.approve
    try:
        async for event in agent.run(conversation, text):
            renderer.show(event)
    except ProviderError as exc:
        renderer.end_line()
        _print_provider_error(agent.provider, agent.model, exc)
        return False
    return renderer.reason == "completed"


class _Renderer:
    """Prints agent events to the terminal."""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self.mid_line = False
        self.reason: str | None = None

    def _short(self, text: str) -> str:
        """Show paths inside the home folder as ``~/…``."""
        home = str(self.agent.context.home)
        return text.replace(home + "/", "~/").replace(f"'{home}'", "'~'")

    def end_line(self) -> None:
        if self.mid_line:
            console.print()
            self.mid_line = False

    async def approve(self, request: ApprovalRequest) -> bool:
        """Show an approval card and read the user's decision."""
        self.end_line()
        danger = request.risk is RiskLevel.DANGER
        body = Text(printable(request.summary), style="bold")
        for line in request.details:
            body.append("\n  • " + self._short(printable(line)), style="default")
        console.print(
            Panel(
                body,
                title=t("approval.title.danger" if danger else "approval.title.confirm"),
                title_align="left",
                border_style="red" if danger else "yellow",
                expand=False,
            )
        )
        prompt = Text(t("approval.prompt.danger" if danger else "approval.prompt.confirm"))
        try:
            answer = await asyncio.to_thread(console.input, prompt)
        except EOFError:
            answer = ""
        answer = answer.strip().casefold()
        if danger:
            return answer == t("approval.danger_word")
        return answer in t("approval.yes_words").split(",")

    def show(self, event: AgentEvent) -> None:
        if isinstance(event, TextDelta):
            text = _strip_control(event.text)
            console.print(text, end="", markup=False, highlight=False, soft_wrap=True)
            self.mid_line = not text.endswith("\n")
        elif isinstance(event, ToolCallStarted):
            self.end_line()
            line = f"  ⚙ {self._short(event.description)}"
            width = max(console.width - 1, 20)
            if len(line) > width:
                line = line[: width - 1] + "…"
            console.print(Text(line, style="dim cyan"), soft_wrap=True)
        elif isinstance(event, ToolCallFinished):
            self._show_tool_outcome(event)
        elif isinstance(event, ProviderFallback):
            self.end_line()
            notice = t("cli.fallback", unavailable=event.unavailable, fallback=event.fallback)
            console.print(Text(notice, style="yellow"))
        elif isinstance(event, AgentDone):
            self.end_line()
            self.reason = event.reason
            if event.reason != "completed":
                notice = t(f"cli.done.{event.reason}", limit=self.agent.max_iterations)
                console.print(Text(notice, style="yellow"))

    @staticmethod
    def _show_tool_outcome(event: ToolCallFinished) -> None:
        if event.status == "ok":
            return
        if event.status == "denied":
            message = t("cli.tool_denied")
        elif event.status == "skipped":
            message = t("cli.tool_skipped")
        else:
            detail = event.result.content.splitlines()[0][:200] if event.result.content else ""
            message = t("cli.tool_error", detail=detail)
        console.print(Text(f"    ✗ {message}", style="yellow"))


def _strip_control(text: str) -> str:
    """Drop terminal control characters from model output (keeps newlines and tabs)."""
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) != "Cc")


def _print_provider_error(provider: LLMProvider, model: str | None, exc: ProviderError) -> None:
    model = model or provider.default_model
    if isinstance(exc, ProviderUnavailableError):
        lines = [t("error.provider_unavailable", provider=provider.display_name, detail=exc)]
        if provider.name == "ollama":
            lines.append(t("hint.ollama_unavailable"))
    elif isinstance(exc, ProviderAuthError):
        lines = [t("error.provider_auth", provider=provider.display_name)]
        if provider.requires_api_key:
            lines.append(
                t("hint.api_key", provider=provider.name, variable=provider.api_key_env_var or "")
            )
    else:
        lines = [t("error.provider", provider=provider.display_name, detail=exc)]
        if provider.name == "ollama" and "not found" in str(exc):
            lines.append(t("hint.ollama_model", model=model))
    err_console.print(Text("\n".join(lines), style="red"))


if __name__ == "__main__":
    app()
