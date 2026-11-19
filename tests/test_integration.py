"""Integration with the desktop session: systemd service, menu entry, bubble, uninstall."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from kreafinity import cli
from kreafinity.system import integration

runner = CliRunner()
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def systemctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake `systemctl` recording its arguments (exit code from $FAKE_SYSTEMCTL_EXIT)."""
    folder = tmp_path / "fake-bin"
    folder.mkdir()
    log = tmp_path / "systemctl.log"
    script = folder / "systemctl"
    script.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexit "${{FAKE_SYSTEMCTL_EXIT:-0}}"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SYSTEMCTL_EXIT", "0")
    return log


def calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_service_unit() -> None:
    unit = integration.service_unit(["/opt/k f/python", "-m", "kreafinity.api.server"])
    assert 'ExecStart="/opt/k f/python" -m kreafinity.api.server' in unit
    assert "WantedBy=graphical-session.target" in unit
    assert "PartOf=graphical-session.target" in unit
    assert "Restart=on-failure" in unit


def test_reference_unit_is_up_to_date() -> None:
    reference = (REPO / "packaging" / "kreafinity.service").read_text()
    generated = integration.service_unit(
        ["%h/.local/share/kreafinity/venv/bin/python", "-m", "kreafinity.api.server"]
    )
    assert reference.endswith(generated)


def test_install_with_systemd(
    systemctl: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    icon = REPO / "ui" / "web" / "img" / "logo.svg"
    report = integration.install(bubble=True, icon_source=icon, autostart_comment="Bulle")

    unit = tmp_path / "xdg-config" / "systemd" / "user" / "kreafinity.service"
    assert unit.read_text() == integration.service_unit(
        [sys.executable, "-m", "kreafinity.api.server"]
    )
    log = calls(systemctl)
    assert "--user daemon-reload" in log
    assert "--user enable kreafinity.service" in log
    assert "--user import-environment DISPLAY" in log  # display handed to the service
    assert "--user restart kreafinity.service" in log

    menu = (tmp_path / "xdg-data" / "applications" / "kreafinity.desktop").read_text()
    assert f"Exec={sys.executable} -m kreafinity.cli ui" in menu
    assert "Name=Kreafinity" in menu and "Categories=Utility;" in menu
    installed_icon = (
        tmp_path / "xdg-data" / "icons" / "hicolor" / "scalable" / "apps" / "kreafinity.svg"
    )
    assert installed_icon.read_bytes() == icon.read_bytes()
    assert f"Icon={installed_icon}" in menu

    autostart = (tmp_path / "xdg-config" / "autostart" / "kreafinity-bubble.desktop").read_text()
    assert f"Exec={sys.executable} -m kreafinity.cli bubble" in autostart
    keys = [key for key, _ in report.done]
    assert keys == [
        "setup.service_file",
        "setup.service_enabled",
        "setup.service_started",
        "setup.menu_entry",
        "setup.bubble_autostart",
    ]


def test_install_without_systemd(systemctl: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_SYSTEMCTL_EXIT", "1")  # no user manager (container, SSH…)
    report = integration.install(bubble=False, icon_source=None, autostart_comment="")
    keys = [key for key, _ in report.done]
    assert keys == ["setup.service_file", "setup.no_systemd", "setup.menu_entry"]
    assert not integration.autostart_path().exists()
    assert "Icon=kreafinity" in integration.menu_entry_path().read_text()


def test_uninstall_removes_only_what_was_added(systemctl: Path, tmp_path: Path) -> None:
    integration.install(bubble=True, icon_source=None, autostart_comment="")
    keep = tmp_path / "xdg-config" / "kreafinity" / "config.yaml"
    keep.parent.mkdir(parents=True)
    keep.write_text("language: fr\n")

    report = integration.uninstall()

    assert "--user disable --now kreafinity.service" in calls(systemctl)
    for path in (
        integration.service_path(),
        integration.menu_entry_path(),
        integration.autostart_path(),
    ):
        assert not path.exists()
    assert keep.exists()
    assert {key for key, _ in report.done} == {"setup.removed"}


def test_setup_command(systemctl: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_SYSTEMCTL_EXIT", "1")
    monkeypatch.setattr(cli, "_gui_python", lambda: None)
    result = runner.invoke(cli.app, ["setup"])
    assert result.exit_code == 0, result.output
    assert "Bulle non activée" in result.output
    assert "menu des applications" in result.output
    assert "intégré à votre session" in result.output

    removed = runner.invoke(cli.app, ["setup", "--uninstall"])
    assert removed.exit_code == 0 and "Intégration retirée" in removed.output
    assert not integration.service_path().exists()


def test_daemon_started_through_systemd(systemctl: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    integration.install(bubble=False, icon_source=None, autostart_comment="")
    answers = iter([None, "http://127.0.0.1:27494"])  # down, then up once started
    monkeypatch.setattr(cli, "_running_daemon_url", lambda settings: next(answers))
    spawned: list[object] = []
    fake = SimpleNamespace(
        Popen=lambda *args, **kwargs: spawned.append(args), DEVNULL=subprocess.DEVNULL
    )
    monkeypatch.setattr(cli, "subprocess", fake)  # only the CLI's view: systemctl still runs
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)

    assert cli._start_daemon(cli.load_settings()) == "http://127.0.0.1:27494"
    assert "--user start kreafinity.service" in calls(systemctl)
    assert spawned == []  # not started a second time outside systemd


def test_processes_running_from(tmp_path: Path) -> None:
    sleep = [sys.executable, "-c", "import time; time.sleep(60)"]
    root = tmp_path / "xdg-data" / "kreafinity" / "venv"
    ours = subprocess.Popen([*sleep, f"{root}/lib/kreafinity_bubble.py"])
    other = subprocess.Popen([*sleep, f"{root}-dev/lib/kreafinity_bubble.py"])  # another copy
    try:
        found = {process.pid for process in integration.processes_running_from(root)}
        assert ours.pid in found and other.pid not in found
    finally:
        for process in (ours, other):
            process.kill()
            process.wait()


def test_uninstall_command(
    systemctl: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "user-home"
    monkeypatch.setenv("HOME", str(home))
    venv = tmp_path / "xdg-data" / "kreafinity" / "venv"
    (venv / "bin").mkdir(parents=True)
    for name in ("kreafinity", "kreafinity-core"):
        (venv / "bin" / name).write_text("")
    links = home / ".local" / "bin"
    links.mkdir(parents=True)
    (links / "kreafinity").symlink_to(venv / "bin" / "kreafinity")
    (links / "kreafinity-core").symlink_to(venv / "bin" / "kreafinity-core")
    (links / "other-tool").symlink_to("/usr/bin/env")
    history = tmp_path / "xdg-data" / "kreafinity" / "history.db"
    history.write_text("")
    integration.install(bubble=False, icon_source=None, autostart_comment="")

    assert runner.invoke(cli.app, ["uninstall"], input="n\n").exit_code == 1  # asked first
    assert venv.exists()

    result = runner.invoke(cli.app, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    assert not venv.exists()
    assert not (links / "kreafinity").exists() and not (links / "kreafinity-core").is_symlink()
    assert (links / "other-tool").is_symlink()  # not ours: untouched
    assert history.exists()  # conversations kept
    assert not integration.service_path().exists()

    config = tmp_path / "xdg-config" / "kreafinity" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("language: fr\n")
    purged = runner.invoke(cli.app, ["uninstall", "--purge", "--yes"])
    assert purged.exit_code == 0, purged.output
    assert not history.exists() and not config.exists()
