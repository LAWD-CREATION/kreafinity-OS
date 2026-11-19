"""Command-line interface for Kreafinity OS."""

from __future__ import annotations

import asyncio
import unicodedata
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from kreafinity import __version__
from kreafinity.agent import (
    ActionLog,
    Agent,
    AgentDone,
    AgentEvent,
    ApprovalRequest,
    Conversation,
    ToolCallFinished,
    ToolCallStarted,
)
from kreafinity.config import ConfigError, Settings, load_settings
from kreafinity.i18n import set_language, t
from kreafinity.providers import (
    ProviderAuthError,
    ProviderError,
    ProviderUnavailableError,
    TextDelta,
    available_providers,
    create_provider,
    parse_model_spec,
)
from kreafinity.tools import RiskLevel, ToolContext, ToolRegistry, printable

app = typer.Typer(
    name="kreafinity",
    help="Kreafinity OS — l'assistant système piloté par IA.",
    no_args_is_help=True,
)
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
            await agent.provider.aclose()

    if not asyncio.run(run()):
        raise typer.Exit(1)


@app.command()
def chat(provider: ProviderOption = None) -> None:
    """Ouvre une conversation interactive avec l'assistant."""
    agent = _build_agent(_load_settings(), provider)
    model = agent.model or agent.provider.default_model
    console.print(t("chat.welcome", provider=f"{agent.provider.display_name} · {model}"))

    async def run() -> None:
        conversation = Conversation()
        try:
            while True:
                try:
                    text = await asyncio.to_thread(
                        console.input, f"\n[bold cyan]{t('chat.you')}[/] > "
                    )
                except (EOFError, KeyboardInterrupt):
                    break
                text = text.strip()
                if text.lower() in QUIT_COMMANDS:
                    break
                if text:
                    console.print()
                    await _run_turn(agent, conversation, text)
        finally:
            await agent.provider.aclose()

    asyncio.run(run())
    console.print(t("chat.bye"))


def _load_settings() -> Settings:
    try:
        settings = load_settings()
    except ConfigError as exc:
        err_console.print(Text(t("error.config", detail=exc), style="red"))
        raise typer.Exit(2) from exc
    set_language(settings.language)
    return settings


def _build_agent(settings: Settings, provider_spec: str | None) -> Agent:
    try:
        name, model = parse_model_spec(provider_spec or settings.provider)
    except ValueError as exc:
        err_console.print(Text(str(exc), style="red"))
        raise typer.Exit(2) from exc
    if name not in available_providers():
        message = t("error.unknown_provider", name=name, available=", ".join(available_providers()))
        err_console.print(Text(message, style="red"))
        raise typer.Exit(2)
    try:
        provider = create_provider(name, **settings.provider_options(name))
    except TypeError as exc:
        err_console.print(Text(t("error.provider_options", name=name, detail=exc), style="red"))
        raise typer.Exit(2) from exc
    return Agent(
        provider,
        ToolRegistry.load(settings.tools),
        ToolContext.create(allowed_paths=settings.allowed_paths),
        model=model,
        language=settings.language,
        max_iterations=settings.max_iterations,
        action_log=ActionLog(),
    )


async def _run_turn(agent: Agent, conversation: Conversation, text: str) -> bool:
    """Run one request and print it; return False if it failed."""
    renderer = _Renderer(agent)
    agent.approver = renderer.approve
    try:
        async for event in agent.run(conversation, text):
            renderer.show(event)
    except ProviderError as exc:
        renderer.end_line()
        _print_provider_error(agent, exc)
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


def _print_provider_error(agent: Agent, exc: ProviderError) -> None:
    provider = agent.provider
    model = agent.model or provider.default_model
    if isinstance(exc, ProviderUnavailableError):
        lines = [t("error.provider_unavailable", provider=provider.display_name, detail=exc)]
        if provider.name == "ollama":
            lines.append(t("hint.ollama_unavailable"))
    elif isinstance(exc, ProviderAuthError):
        lines = [t("error.provider_auth", provider=provider.display_name)]
        if provider.name == "anthropic":
            lines.append(t("hint.anthropic_auth"))
    else:
        lines = [t("error.provider", provider=provider.display_name, detail=exc)]
        if provider.name == "ollama" and "not found" in str(exc):
            lines.append(t("hint.ollama_model", model=model))
    err_console.print(Text("\n".join(lines), style="red"))


if __name__ == "__main__":
    app()
