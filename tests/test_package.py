"""Kreafinity installed from the Debian package: updates come from the system (APT)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kreafinity import cli, update
from kreafinity.voice import manager as voice_manager

ROOT = Path(__file__).resolve().parent.parent
DEBIAN = ROOT / "packaging" / "debian"


@pytest.fixture
def packaged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pretend Kreafinity's Python is the Debian package's (its marker next to it)."""
    monkeypatch.setattr(update.sys, "base_prefix", str(tmp_path))
    marker = tmp_path / update.PACKAGE_MARKER
    marker.write_text(json.dumps({"format": "deb", "version": "0.2.0+git20261121.abc1234"}))
    return marker


def test_not_packaged_by_default() -> None:
    assert update.package_info() is None
    assert (
        update.UpdateManager(publish=lambda event: None, enabled=lambda: True).info()["managed"]
        is None
    )


async def test_updates_come_from_the_system(packaged: Path) -> None:
    manager = update.UpdateManager(publish=lambda event: None, enabled=lambda: True)
    info = manager.info()
    assert info["managed"] == "deb" and info["supported"] is False
    with pytest.raises(update.UpdateError, match="system's package manager") as error:
        await manager.check()
    assert error.value.code == "unsupported"
    with pytest.raises(update.UpdateError, match="system's package manager"):
        manager.start()


def test_commands(packaged: Path) -> None:
    runner = CliRunner()
    assert "paquet 0.2.0+git20261121.abc1234" in runner.invoke(cli.app, ["version"]).output
    result = runner.invoke(cli.app, ["update"])
    assert result.exit_code == 1 and "sudo apt update && sudo apt upgrade" in result.output
    # « uninstall » leaves the program to APT, and --purge still erases the user's data.
    data = Path(cli.data_dir())
    data.mkdir(parents=True, exist_ok=True)
    (data / "history.db").write_text("…")
    result = runner.invoke(cli.app, ["uninstall", "--purge", "--yes"])
    assert result.exit_code == 0, result.output
    assert "sudo apt remove kreafinity" in result.output and not data.exists()


def test_voice_engines_go_to_the_user_folder_when_python_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(voice_manager.subprocess, "run", run)
    monkeypatch.setattr(voice_manager, "environment_writable", lambda: False)
    voice_manager.pip_install(["piper-tts"])
    target = commands[0][commands[0].index("--target") + 1]
    assert target.endswith("kreafinity/voice/site-packages") and commands[0][-1] == "piper-tts"
    monkeypatch.setattr(voice_manager, "environment_writable", lambda: True)
    voice_manager.pip_install(["piper-tts"])
    assert "--target" not in commands[1]  # a script installation: its own environment


# --- The package's files -----------------------------------------------------------------------


def test_package_scripts_are_valid_shell() -> None:
    for name in ("build-deb.sh", "postinst", "prerm", "postrm"):
        script = DEBIAN / name
        assert script.stat().st_mode & 0o111, f"{name} is not executable"
        assert subprocess.run(["sh", "-n", str(script)], check=False).returncode == 0
    if shutil.which("shellcheck"):
        scripts = [str(DEBIAN / name) for name in ("build-deb.sh", "postinst", "prerm", "postrm")]
        assert subprocess.run(["shellcheck", *scripts], check=False).returncode == 0


def test_control_template() -> None:
    control = (DEBIAN / "control.in").read_text()
    for field in ("Package: kreafinity", "Version: @VERSION@", "Architecture: @ARCH@", "Depends:"):
        assert field in control
    # Description: a short line, then indented paragraphs (empty lines as " .").
    lines = control.split("Description:", 1)[1].splitlines()[1:]
    assert lines and all(line.startswith(" ") for line in lines if line)


def test_maintainer_scripts_handle_the_widget_and_the_service() -> None:
    postinst = (DEBIAN / "postinst").read_text()
    assert "systemctl --global enable kreafinity.service" in postinst
    assert "/opt/kreafinity/share/plasmoids/$version" in postinst
    prerm = (DEBIAN / "prerm").read_text()
    assert "systemctl --global disable kreafinity.service" in prerm
    assert "rm -rf /usr/share/plasma/plasmoids/org.kreafinity.assistant" in prerm
    build = (DEBIAN / "build-deb.sh").read_text()
    assert "PYTHONNOUSERSITE=1" in build  # never the build machine's own packages
    assert "python3 -s -m" in build  # never the user's own packages either
