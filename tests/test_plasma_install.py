"""Installing the KDE Plasma integration (files, D-Bus activation, theme, removal)."""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from dbus_fast import Message
from dbus_fast.aio import MessageBus
from typer.testing import CliRunner

from kreafinity import cli
from kreafinity.api.web import plasma_assets
from kreafinity.plasma import install as plasma
from kreafinity.plasma import krunner

runner = CliRunner()


@pytest.fixture
def tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake Plasma command line tools recording their arguments; no plasmashell."""
    folder = tmp_path / "plasma-bin"
    folder.mkdir()
    log = tmp_path / "plasma-tools.log"
    for name in (
        "plasma-apply-lookandfeel",
        "plasma-apply-colorscheme",
        "plasma-apply-wallpaperimage",
        "kquitapp5",
        "kquitapp6",
    ):
        script = folder / name
        script.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n')
        script.chmod(0o755)
    systemctl = folder / "systemctl"  # never the real user session
    systemctl.write_text("#!/bin/sh\nexit 1\n")
    systemctl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setattr(plasma, "evaluate_script", lambda script: None)
    return log


def calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


# --- The shipped files agree with the code ------------------------------------------------------


def test_assets_are_consistent() -> None:
    assets = plasma_assets()
    assert assets is not None
    runner_file = (assets / "krunner" / "kreafinity.desktop").read_text()
    assert f"X-Plasma-DBusRunner-Service={krunner.BUS_NAME}\n" in runner_file
    assert f"X-Plasma-DBusRunner-Path={krunner.OBJECT_PATH}\n" in runner_file
    assert f"X-Plasma-Runner-Min-Letter-Count={krunner.MIN_LETTERS}\n" in runner_file
    for version in plasma.SUPPORTED_VERSIONS:
        widget = assets / f"plasmoid-{version}" / plasma.WIDGET_ID
        assert (
            json.loads((widget / "metadata.json").read_text())["KPlugin"]["Id"] == plasma.WIDGET_ID
        )
        assert "/api/ui/requests" in (widget / "contents" / "ui" / "main.qml").read_text()
    theme = assets / "look-and-feel" / plasma.THEME_ID
    assert json.loads((theme / "metadata.json").read_text())["KPlugin"]["Id"] == plasma.THEME_ID
    defaults = (theme / "contents" / "defaults").read_text()
    assert f"ColorScheme={plasma.COLOR_SCHEME}\n" in defaults
    assert f"Image={plasma.WALLPAPER}\n" in defaults
    layout = (theme / "contents" / "layouts" / "org.kde.plasma.desktop-layout.js").read_text()
    assert f'panel.addWidget("{plasma.WIDGET_ID}")' in layout
    assert (theme / "contents" / "previews" / "preview.png").is_file()
    wallpaper = assets / "wallpapers" / plasma.WALLPAPER
    assert (
        json.loads((wallpaper / "metadata.json").read_text())["KPlugin"]["Id"] == plasma.WALLPAPER
    )
    assert (wallpaper / "contents" / "images" / "1920x1080.jpg").is_file()
    colors = (assets / "color-schemes" / f"{plasma.COLOR_SCHEME}.colors").read_text()
    assert f"ColorScheme={plasma.COLOR_SCHEME}\n" in colors


# --- Install / apply / remove ----------------------------------------------------------------


@pytest.mark.parametrize("version", [5, 6])
def test_install(tools: Path, tmp_path: Path, version: int) -> None:
    report = plasma.install(version, port=27600)
    data = tmp_path / "xdg-data"

    service = (data / "dbus-1" / "services" / "org.kreafinity.KRunner.service").read_text()
    assert service == (
        "[D-BUS Service]\nName=org.kreafinity.KRunner\n"
        f"Exec={sys.executable} -m kreafinity.plasma.krunner\n"
    )
    assert (data / "krunner" / "dbusplugins" / "kreafinity.desktop").is_file()
    widget = data / "plasma" / "plasmoids" / plasma.WIDGET_ID
    metadata = json.loads((widget / "metadata.json").read_text())
    assert ("KPackageStructure" in metadata) == (version == 6)  # the right Plasma API
    config = (widget / "contents" / "config" / "main.xml").read_text()
    assert "<default>http://127.0.0.1:27600</default>" in config
    assert (data / "plasma" / "look-and-feel" / plasma.THEME_ID / "metadata.json").is_file()
    assert (data / "color-schemes" / "Kreafinity.colors").is_file()
    assert (data / "wallpapers" / "Kreafinity" / "contents" / "images" / "1920x1080.jpg").is_file()
    assert (data / "icons" / "hicolor" / "scalable" / "apps" / "kreafinity.svg").is_file()

    assert f"kquitapp{version} krunner" in calls(tools)  # KRunner reloads its plugins
    assert [key for key, _ in report.done] == [
        "plasma.search",
        "plasma.widget",
        "plasma.files_menu",
        "plasma.theme_files",
        "plasma.widget_manual",  # plasmashell not running here
    ]
    # File manager menu: executable (KDE ignores the others), running this Python.
    for menu in plasma.service_menu_paths():
        assert menu.stat().st_mode & 0o111
        text = menu.read_text()
        assert "@COMMAND@" not in text
        assert f"{sys.executable} -m kreafinity.plasma.files_menu" in text
        assert "X-KDE-Submenu=Kreafinity" in text
    plasma.install(version, port=27600)  # again: replaces in place


def test_install_rejects_unknown_versions(tools: Path) -> None:
    with pytest.raises(plasma.PlasmaError, match="Plasma 4"):
        plasma.install(4, port=27494)


@pytest.mark.parametrize("layout", [False, True])
def test_apply_theme(tools: Path, layout: bool) -> None:
    plasma.install(6, port=27494)
    report = plasma.apply_theme(6, layout=layout)
    wallpaper = plasma.wallpaper_path() / "contents" / "images" / "1920x1080.jpg"
    expected = "plasma-apply-lookandfeel --apply org.kreafinity.desktop"
    assert (expected + " --resetLayout" if layout else expected) in calls(tools)
    assert "plasma-apply-colorscheme Kreafinity" in calls(tools)
    assert f"plasma-apply-wallpaperimage {wallpaper}" in calls(tools)
    assert [key for key, _ in report.done] == [
        "plasma.layout_applied" if layout else "plasma.theme_applied",
        "plasma.colors_applied",
        "plasma.wallpaper_applied",
    ]


def test_remove(tools: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plasma.install(6, port=27494)
    kreadconfig = tmp_path / "plasma-bin" / "kreadconfig6"
    kreadconfig.write_text("#!/bin/sh\necho Kreafinity\n")  # our colors are in use
    kreadconfig.chmod(0o755)
    scripts: list[str] = []
    monkeypatch.setattr(plasma, "evaluate_script", lambda script: scripts.append(script) or "")

    report = plasma.remove(6)

    assert not any(path.exists() for path in plasma.installed_paths())
    assert len(report.done) == len(plasma.installed_paths())
    assert "widget.remove()" in scripts[0]  # taken off the taskbar
    assert "plasma-apply-colorscheme BreezeDark" in calls(tools)
    assert not plasma.is_installed()


def test_plasma_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDE_SESSION_VERSION", "6")
    assert plasma.plasma_version() == 6
    monkeypatch.delenv("KDE_SESSION_VERSION")
    monkeypatch.setenv("PATH", str(tmp_path))  # no plasmashell
    assert plasma.plasma_version() is None
    fake = tmp_path / "plasmashell"
    fake.write_text("#!/bin/sh\necho 'plasmashell 5.27.12'\n")
    fake.chmod(0o755)
    assert plasma.plasma_version() == 5


# --- Adding the widget to the running taskbar (plasmashell over D-Bus) -------------------------


@pytest.fixture
def session_bus(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    if shutil.which("dbus-daemon") is None:
        pytest.skip("dbus-daemon is not installed")
    process = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert process.stdout is not None
    address = process.stdout.readline().strip()
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)
    yield address
    process.terminate()
    process.wait()
    process.stdout.close()


async def test_widget_added_through_plasmashell(session_bus: str) -> None:
    scripts: list[str] = []

    def plasmashell(message: Message) -> Message | None:
        if message.member != "evaluateScript":
            return None
        scripts.append(message.body[0])
        return Message.new_method_return(message, "s", ["added\n"])

    bus = await MessageBus(bus_address=session_bus).connect()
    await bus.request_name("org.kde.plasmashell")
    bus.add_message_handler(plasmashell)
    try:
        answer = await plasma._evaluate_script(
            plasma.ADD_WIDGET_SCRIPT
            % {"id": plasma.WIDGET_ID, "shortcut": plasma.SHORTCUT, "add": "true"}
        )
    finally:
        bus.disconnect()
        await bus.wait_for_disconnect()  # type: ignore[no-untyped-call]
    assert answer == "added\n"
    assert 'panel.addWidget("org.kreafinity.assistant")' in scripts[0]
    assert 'found[0].globalShortcut = "Meta+C"' in scripts[0]


@pytest.mark.parametrize(
    ("answer", "keys"),
    [
        ("added shortcut\n", ["plasma.widget_added", "plasma.shortcut"]),
        ("present shortcut", ["plasma.widget_added", "plasma.shortcut"]),
        ("present", ["plasma.widget_added"]),  # the user's own shortcut is kept
        ("no-panel", ["plasma.widget_manual"]),
        (None, ["plasma.widget_manual"]),
    ],
)
def test_widget_and_shortcut_report(
    tools: Path, monkeypatch: pytest.MonkeyPatch, answer: str | None, keys: list[str]
) -> None:
    monkeypatch.setattr(plasma, "evaluate_script", lambda script: answer)
    report = plasma.install(6, port=27494)
    assert [key for key, _ in report.done][4:] == keys


async def test_no_plasmashell(session_bus: str) -> None:
    assert await plasma._evaluate_script("print('x')") is None


def test_script_error_is_none(session_bus: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent/bus")
    assert plasma.evaluate_script("print('x')") is None


# --- Command line -------------------------------------------------------------------------------


def test_desktop_install_command(tools: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plasma, "plasma_version", lambda: None)  # not a Plasma desktop
    result = runner.invoke(cli.app, ["desktop", "install"])
    assert result.exit_code == 1 and "KDE Plasma n'est pas détecté" in result.output

    result = runner.invoke(
        cli.app, ["desktop", "install", "--plasma", "6", "--layout"], input="n\n"
    )
    assert result.exit_code == 1  # the layout replaces panels: asked first
    assert not plasma.is_installed()

    result = runner.invoke(cli.app, ["bureau", "install", "--plasma", "6", "--theme"])
    assert result.exit_code == 0, result.output
    assert "Demander à Kreafinity" in result.output
    assert "Couleurs Kreafinity appliquées" in result.output
    assert "--resetLayout" not in "\n".join(calls(tools))

    result = runner.invoke(cli.app, ["desktop", "remove"])
    assert result.exit_code == 0 and "Intégration à KDE Plasma retirée" in result.output
    assert not plasma.is_installed()


def test_uninstall_removes_the_plasma_integration(
    tools: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    plasma.install(6, port=27494)
    result = runner.invoke(cli.app, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    assert not plasma.is_installed()


@pytest.mark.parametrize(
    ("answer", "keys"),
    [("present shortcut", ["plasma.shortcut"]), ("present", []), ("absent", []), (None, [])],
)
def test_update_gives_the_shortcut_without_adding_the_widget(
    tools: Path, monkeypatch: pytest.MonkeyPatch, answer: str | None, keys: list[str]
) -> None:
    scripts: list[str] = []
    monkeypatch.setattr(plasma, "evaluate_script", lambda script: scripts.append(script) or answer)
    report = plasma.install(6, port=27494, add_widget=False)  # what an update runs
    assert "var mayAdd = false;" in scripts[0]  # a widget the user removed stays removed
    assert [key for key, _ in report.done][4:] == keys
