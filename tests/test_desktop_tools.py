import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import psutil
import pytest

from kreafinity.system import desktop
from kreafinity.system.desktop import DesktopApp, LaunchError
from kreafinity.tools import RiskLevel, ToolContext, ToolError, ToolRegistry


def tool(name: str) -> Any:
    found = ToolRegistry.load([name]).get(name)
    assert found is not None
    return found


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record programs launched instead of starting them."""
    calls: list[list[str]] = []
    monkeypatch.setattr(desktop, "launch", lambda argv, **_: calls.append(list(argv)))
    monkeypatch.setattr(desktop, "find_program", lambda *names: f"/usr/bin/{names[0]}")
    return calls


@pytest.fixture
def applications(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake installed applications: user dir overrides system dir."""
    system = tmp_path / "system-share"
    monkeypatch.setenv("XDG_DATA_DIRS", str(system))
    monkeypatch.setattr(desktop, "EXTRA_DATA_DIRS", ())
    entries = {
        system / "applications" / "firefox.desktop": (
            "[Desktop Entry]\nType=Application\nName=Firefox\nName[fr]=Navigateur Firefox\n"
            "Comment=Browse the Web\nComment[fr]=Naviguer sur le Web\n"
            "Keywords=web;internet;\nExec=firefox %u\n"
        ),
        system / "applications" / "org.gnome.Nautilus.desktop": (
            "[Desktop Entry]\nType=Application\nName=Files\nName[fr_FR]=Fichiers\nExec=nautilus\n"
        ),
        system / "applications" / "hidden.desktop": (
            "[Desktop Entry]\nType=Application\nName=Hidden\nNoDisplay=true\n"
        ),
        system / "applications" / "kde" / "kate.desktop": (
            "[Desktop Entry]\nType=Application\nName=Kate\n"
        ),
        system / "applications" / "broken.desktop": "not an ini file",
        system / "applications" / "link.desktop": "[Desktop Entry]\nType=Link\nName=Site\n",
        # Masks the system Kate: the user hid it.
        tmp_path / "xdg-data" / "applications" / "kde-kate.desktop": (
            "[Desktop Entry]\nType=Application\nName=Kate\nHidden=true\n"
        ),
    }
    for path, text in entries.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return system


def test_risk_levels() -> None:
    assert tool("list_applications").risk is RiskLevel.SAFE
    for name in ("open_application", "close_application", "open_file", "open_url"):
        assert tool(name).risk is RiskLevel.CONFIRM


# --- Installed applications ------------------------------------------------------


def test_list_applications_localized(applications: Path) -> None:
    apps = desktop.list_applications("fr")
    assert [(a.id, a.name) for a in apps] == [
        ("org.gnome.Nautilus", "Fichiers"),
        ("firefox", "Navigateur Firefox"),
    ]
    assert apps[1].comment == "Naviguer sur le Web"
    english = desktop.list_applications("en")
    assert {a.name for a in english} == {"Files", "Firefox"}


def test_find_application(applications: Path) -> None:
    assert desktop.find_application("firefox.desktop") is not None
    assert desktop.find_application("FICHIERS") is not None
    assert desktop.find_application("kate") is None
    assert desktop.find_application("Hidden") is None


async def test_list_applications_tool(applications: Path, context: ToolContext) -> None:
    result = json.loads((await tool("list_applications").run({"query": "web"}, context)).content)
    assert result["applications"] == [
        {"id": "firefox", "name": "Navigateur Firefox", "description": "Naviguer sur le Web"}
    ]


async def test_open_application(
    applications: Path, context: ToolContext, launched: list[list[str]]
) -> None:
    opener = tool("open_application")
    preview = await opener.preview({"application": "firefox"}, context)
    assert preview.summary == "Ouvrir l'application « Navigateur Firefox »"
    await opener.run({"application": "firefox"}, context)
    assert launched == [
        ["/usr/bin/gio", "launch", str(applications / "applications" / "firefox.desktop")]
    ]
    with pytest.raises(ToolError, match="list_applications"):
        await opener.preview({"application": "photoshop"}, context)


# --- Files and URLs ----------------------------------------------------------------


async def test_open_file(context: ToolContext, home: Path, launched: list[list[str]]) -> None:
    opener = tool("open_file")
    preview = await opener.preview({"path": "~/Documents/Facture-2026.pdf"}, context)
    assert preview.details == (str(home / "Documents" / "Facture-2026.pdf"),)
    await opener.run({"path": "~/Documents/Facture-2026.pdf"}, context)
    assert launched == [["/usr/bin/gio", "open", str(home / "Documents" / "Facture-2026.pdf")]]
    with pytest.raises(ToolError, match="Access denied"):
        await opener.preview({"path": "/etc/shadow"}, context)


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "javascript:alert(1)", "https://", "kreafinity.com", "https://a b.fr"],
)
async def test_open_url_refuses(context: ToolContext, url: str) -> None:
    with pytest.raises(ToolError):
        await tool("open_url").preview({"url": url}, context)


async def test_open_url(context: ToolContext, launched: list[list[str]]) -> None:
    opener = tool("open_url")
    preview = await opener.preview({"url": "https://kreafinity.com/contact"}, context)
    assert preview.details == ("https://kreafinity.com/contact",)
    await opener.run({"url": "https://kreafinity.com/contact"}, context)
    assert launched == [["/usr/bin/gio", "open", "https://kreafinity.com/contact"]]


async def test_open_reports_launch_errors(
    context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(target: str) -> None:
        raise LaunchError("cannot open display")

    monkeypatch.setattr(desktop, "open_with_default_app", fail)
    with pytest.raises(ToolError, match="cannot open display"):
        await tool("open_url").run({"url": "https://kreafinity.com"}, context)


# --- Launching (real processes) -----------------------------------------------------


def test_launch_detects_immediate_failure() -> None:
    with pytest.raises(LaunchError, match="boom"):
        desktop.launch([sys.executable, "-c", "import sys; sys.exit('boom')"])
    with pytest.raises(LaunchError):
        desktop.launch(["/nonexistent/program"])


def test_launch_returns_while_program_runs() -> None:
    desktop.launch([sys.executable, "-c", "import time; time.sleep(5)"], grace_period=0.2)
    desktop.launch([sys.executable, "-c", "pass"], grace_period=2)


def test_no_launcher_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop, "find_program", lambda *names: None)
    with pytest.raises(LaunchError, match="gio"):
        desktop.open_with_default_app("https://kreafinity.com")
    app = DesktopApp(id="x", name="X", comment=None, keywords=(), path=Path("/x.desktop"))
    with pytest.raises(LaunchError):
        desktop.launch_application(app)


# --- Closing applications ------------------------------------------------------------


@pytest.fixture
def sleeper() -> Any:
    """A real process of the current user, named after a copy of the interpreter."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    yield psutil.Process(process.pid)
    process.kill()
    process.wait()


def test_find_processes_never_includes_itself(sleeper: psutil.Process) -> None:
    name = os.path.basename(sys.executable)
    found = {p.pid for p in desktop.find_processes(name)}
    assert sleeper.pid in found
    assert os.getpid() not in found


async def test_close_application(
    context: ToolContext, sleeper: psutil.Process, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desktop, "find_processes", lambda name: [sleeper])
    closer = tool("close_application")
    preview = await closer.preview({"name": "dormeur"}, context)
    assert preview.summary == "Fermer « dormeur » (1 processus)"
    assert preview.details[0].startswith(f"PID {sleeper.pid} — ")

    result = json.loads((await closer.run({"name": "dormeur"}, context)).content)
    assert result["closed_pids"] == [sleeper.pid]
    assert not sleeper.is_running() or sleeper.status() == psutil.STATUS_ZOMBIE


async def test_close_unknown_application(
    context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desktop, "find_processes", lambda name: [])
    with pytest.raises(ToolError, match="No running program"):
        await tool("close_application").preview({"name": "nope"}, context)
