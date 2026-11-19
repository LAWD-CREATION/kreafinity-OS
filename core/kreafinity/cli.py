"""Command-line interface for Kreafinity OS."""

from __future__ import annotations

import asyncio
import unicodedata
from dataclasses import asdict
from typing import Annotated

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
    ProviderFallback,
    ToolCallFinished,
    ToolCallStarted,
)
from kreafinity.config import (
    ConfigError,
    Settings,
    default_config_path,
    load_settings,
    save_setting,
)
from kreafinity.i18n import set_language, t
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
from kreafinity.tools import RiskLevel, ToolContext, ToolRegistry, printable

app = typer.Typer(
    name="kreafinity",
    help="Kreafinity OS — l'assistant système piloté par IA.",
    no_args_is_help=True,
)
provider_app = typer.Typer(help="Choisir et gérer les fournisseurs d'IA.", no_args_is_help=True)
app.add_typer(provider_app, name="provider")
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
# Until keys are stored in the system keyring (CLAUDE.md §3.6).
API_KEY_VARIABLES = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}


@app.callback()
def main() -> None:
    """Kreafinity OS — l'assistant système piloté par IA."""


@app.command()
def version() -> None:
    """Affiche la version de Kreafinity OS."""
    console.print(f"Kreafinity OS [bold]{__version__}[/bold]")


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Votre demande, entre guillemets.")],
    provider: ProviderOption = None,
) -> None:
    """Pose une question à l'assistant et affiche sa réponse."""
    agent = _build_agent(_load_settings(), provider)

    async def run() -> bool:
        try:
            return await _run_turn(agent, Conversation(), question)
        finally:
            await _close_providers(agent)

    if not asyncio.run(run()):
        raise typer.Exit(1)


@app.command()
def chat(provider: ProviderOption = None) -> None:
    """Ouvre une conversation interactive avec l'assistant."""
    settings = _load_settings()
    agent = _build_agent(settings, provider)
    console.print(t("chat.welcome", provider=_describe(agent.provider, agent.model)))
    console.print(Text(t("chat.help"), style="dim"))

    async def run() -> None:
        conversation = Conversation()
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
                    _switch_provider(agent, settings, argument.strip(), replaced)
                elif command.lower() in HELP_COMMANDS:
                    console.print(Text(t("chat.help"), style="dim"))
                elif text:
                    console.print()
                    await _run_turn(agent, conversation, text)
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
    for column in ("provider.column.id", "provider.column.name", "provider.column.where", ""):
        table.add_column(t(column) if column else "")
    for name in available_providers():
        provider_class = get_provider_class(name)
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
            Text(", ".join(marks), style="bold green"),
        )
    console.print(table)


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
        return create_provider(name, **settings.provider_options(name)), model
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
    return Agent(
        provider,
        ToolRegistry.load(settings.tools),
        ToolContext.create(allowed_paths=settings.allowed_paths),
        model=model,
        language=settings.language,
        max_iterations=settings.max_iterations,
        action_log=ActionLog(),
        fallback=fallback,
    )


def _switch_provider(
    agent: Agent, settings: Settings, spec: str, replaced: list[LLMProvider]
) -> None:
    """Handle ``/fournisseur [spec]`` in a chat: show or change the provider."""
    if not spec:
        console.print(t("provider.current", provider=_describe(agent.provider, agent.model)))
        return
    try:
        provider, model = _make_provider(settings, spec)
    except _UserError as exc:
        console.print(Text(str(exc), style="red"))
        return
    replaced.append(agent.provider)
    agent.use_provider(provider, model)
    console.print(Text(t("provider.switched", provider=_describe(provider, model)), style="green"))


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
        if provider.name in API_KEY_VARIABLES:
            lines.append(t("hint.api_key", variable=API_KEY_VARIABLES[provider.name]))
    else:
        lines = [t("error.provider", provider=provider.display_name, detail=exc)]
        if provider.name == "ollama" and "not found" in str(exc):
            lines.append(t("hint.ollama_model", model=model))
    err_console.print(Text("\n".join(lines), style="red"))


if __name__ == "__main__":
    app()
