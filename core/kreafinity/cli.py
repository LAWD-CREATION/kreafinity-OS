"""Command-line interface for Kreafinity OS.

Only the skeleton exists for now; `ask`, `chat`, `provider` and `config`
commands are added in later steps (see CLAUDE.md §7).
"""

import typer
from rich.console import Console

from kreafinity import __version__

app = typer.Typer(
    name="kreafinity",
    help="Kreafinity OS — l'assistant système piloté par IA.",
    no_args_is_help=True,
)
console = Console()


@app.callback()
def main() -> None:
    """Kreafinity OS — l'assistant système piloté par IA."""


@app.command()
def version() -> None:
    """Affiche la version de Kreafinity OS."""
    console.print(f"Kreafinity OS [bold]{__version__}[/bold]")


if __name__ == "__main__":
    app()
