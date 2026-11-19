"""Opening and closing real applications on a virtual X display (Xvfb).

Skipped when Xvfb, xdotool or gio are not installed. On Debian/Ubuntu:
``sudo apt install xvfb xdotool x11-apps x11-utils libglib2.0-bin``.
"""

import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kreafinity.tools import ToolContext, ToolRegistry

REQUIRED = ("Xvfb", "xdotool", "gio", "xclock", "xmessage")
pytestmark = pytest.mark.skipif(
    any(shutil.which(program) is None for program in REQUIRED),
    reason=f"needs {', '.join(REQUIRED)}",
)


def tool(name: str) -> Any:
    found = ToolRegistry.load([name]).get(name)
    assert found is not None
    return found


@pytest.fixture
def display(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Start a virtual X display with a few test applications installed."""
    server = subprocess.Popen(
        ["Xvfb", "-displayfd", "1", "-screen", "0", "800x600x24", "-nolisten", "tcp"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert server.stdout is not None
    display = f":{server.stdout.readline().strip()}"
    monkeypatch.setenv("DISPLAY", display)
    monkeypatch.setenv("XDG_DATA_DIRS", "/usr/share")

    applications = tmp_path / "xdg-data" / "applications"
    applications.mkdir(parents=True)
    (applications / "horloge.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Horloge\nExec=xclock -digital\n"
    )
    (applications / "visionneuse.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Visionneuse\nExec=xmessage -file %f\n"
        "MimeType=text/plain;\n"
    )
    url_log = tmp_path / "opened-urls.txt"
    (applications / "navigateur.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Navigateur de test\n"
        f"Exec=sh -c 'echo \"$1\" >> {url_log}' sh %u\n"
        "MimeType=x-scheme-handler/http;x-scheme-handler/https;\n"
    )
    config = tmp_path / "xdg-config"
    config.mkdir(exist_ok=True)
    (config / "mimeapps.list").write_text(
        "[Default Applications]\ntext/plain=visionneuse.desktop\n"
        "x-scheme-handler/https=navigateur.desktop\nx-scheme-handler/http=navigateur.desktop\n"
    )
    yield display
    server.terminate()  # X clients exit with their display
    server.wait()
    server.stdout.close()


def windows() -> set[str]:
    result = subprocess.run(
        ["xdotool", "search", "--name", ".", "getwindowname", "%@"],
        capture_output=True,
        text=True,
    )
    return set(result.stdout.split())


def wait_for(condition: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


async def test_open_and_close_application(display: str, context: ToolContext) -> None:
    await tool("open_application").run({"application": "Horloge"}, context)
    assert wait_for(lambda: "xclock" in windows()), "the clock window did not appear"

    await tool("close_application").run({"name": "xclock"}, context)
    assert wait_for(lambda: "xclock" not in windows()), "the clock window is still open"


async def test_open_file_with_default_application(
    display: str, context: ToolContext, home: Path
) -> None:
    (home / "Documents" / "note.txt").write_text("Bonjour depuis Kreafinity !")
    await tool("open_file").run({"path": "~/Documents/note.txt"}, context)
    assert wait_for(lambda: "xmessage" in windows()), "the text viewer did not appear"


async def test_open_url_in_default_browser(
    display: str, context: ToolContext, tmp_path: Path
) -> None:
    await tool("open_url").run({"url": "https://kreafinity.com/?q=test"}, context)
    log = tmp_path / "opened-urls.txt"
    assert wait_for(log.exists)
    assert log.read_text().strip() == "https://kreafinity.com/?q=test"
