"""The one-command installer (packaging/install.sh), run for real in a fresh home.

It builds a virtual environment and downloads the dependencies from PyPI:
skipped when PyPI cannot be reached. Uses a fake `systemctl` reporting
"no user manager", so the real session is never touched.
"""

import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

import pytest

from kreafinity import cli

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "packaging" / "install.sh"
#: Reuse the downloads of previous runs (the fresh home has an empty cache).
PIP_CACHE = os.environ.get("PIP_CACHE_DIR") or str(Path.home() / ".cache" / "pip")

pytestmark = pytest.mark.timeout(900)


def pypi_reachable() -> bool:
    try:
        with urllib.request.urlopen("https://pypi.org/simple/pip/", timeout=10):
            return True
    except OSError:
        return False


@pytest.fixture
def fresh_home(tmp_path: Path) -> Path:
    if shutil.which("bash") is None:
        pytest.skip("bash is not available")
    if not pypi_reachable():
        pytest.skip("PyPI is not reachable")
    home = tmp_path / "fresh-home"
    home.mkdir()
    return home


def environment(home: Path, tmp_path: Path) -> dict[str, str]:
    """A login-like environment: new home, no display, nothing from the test run."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    systemctl = fake_bin / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 1\n")  # no systemd user manager
    systemctl.chmod(0o755)
    removed = ("XDG_", "KREAFINITY_", "DISPLAY", "WAYLAND_DISPLAY", "VIRTUAL_ENV", "PYTHON")
    env = {k: v for k, v in os.environ.items() if not k.startswith(removed)}
    env.update(
        HOME=str(home),
        PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        PIP_CACHE_DIR=PIP_CACHE,
        KREAFINITY_SOURCE=str(REPO),
        LANG="C.UTF-8",
        COLUMNS="200",
    )
    return env


def run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # no terminal: no question can block the test
    )


def install(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    options = ["--yes", "--provider=later", "--no-system-packages"]
    if os.geteuid() == 0:
        options.append("--allow-root")  # CI containers
    result = run(["bash", str(INSTALLER), *options], env)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_install_then_uninstall(fresh_home: Path, tmp_path: Path) -> None:
    env = environment(fresh_home, tmp_path)
    result = install(env)
    assert "Kreafinity est installé" in result.stdout

    venv = fresh_home / ".local" / "share" / "kreafinity" / "venv"
    command = fresh_home / ".local" / "bin" / "kreafinity"
    assert command.resolve() == (venv / "bin" / "kreafinity").resolve()
    assert (venv.parent.stat().st_mode & 0o777) == 0o700
    version = run([str(command), "version"], env)
    assert version.returncode == 0 and "Kreafinity OS" in version.stdout

    unit = fresh_home / ".config" / "systemd" / "user" / "kreafinity.service"
    assert f"ExecStart={venv}/bin/python -m kreafinity.api.server" in unit.read_text()
    menu = fresh_home / ".local" / "share" / "applications" / "kreafinity.desktop"
    assert f"Exec={venv}/bin/python -m kreafinity.cli ui" in menu.read_text()
    autostart = fresh_home / ".config" / "autostart" / "kreafinity-bubble.desktop"
    assert autostart.exists() == (cli._gui_python() is not None)

    # Running it again updates in place.
    assert "Kreafinity est installé" in install(env).stdout
    (fresh_home / ".local" / "share" / "kreafinity" / "history.db").write_text("")

    removed = run([str(command), "uninstall", "--yes"], env)
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert not venv.exists()
    assert not command.is_symlink()
    assert not (fresh_home / ".local" / "bin" / "kreafinity-core").is_symlink()
    assert not unit.exists() and not menu.exists() and not autostart.exists()
    assert (fresh_home / ".local" / "share" / "kreafinity" / "history.db").exists()
