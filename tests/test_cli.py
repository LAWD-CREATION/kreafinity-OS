from typer.testing import CliRunner

from kreafinity import __version__
from kreafinity.cli import app

runner = CliRunner()


def test_version_command_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "Kreafinity OS" in result.output
    assert __version__ in result.output


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "version" in result.output
