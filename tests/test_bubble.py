"""The desktop bubble: geometry, the `kreafinity bubble` command, and a real run on Xvfb."""

import importlib.util
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from kreafinity import cli
from kreafinity.api.web import bubble_script

runner = CliRunner()


def load_bubble() -> ModuleType:
    """The bubble program, imported as a module (its GTK imports are lazy)."""
    script = bubble_script()
    assert script is not None
    spec = importlib.util.spec_from_file_location("kreafinity_bubble", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bubble = load_bubble()


# --- Geometry and helpers ------------------------------------------------------------------


def test_round_shape() -> None:
    spans = bubble.circle_spans(60)
    assert len(spans) == 60
    area = sum(width for _, _, width in spans)
    assert abs(area - math.pi * 30**2) < 60  # a disc, to the pixel
    for x, _, width in spans:
        assert x >= 0 and x + width <= 60
        assert x == 60 - (x + width)  # symmetric


def test_bubble_at_the_bottom_right_of_the_work_area() -> None:
    # A 1920x1080 screen with a 40-pixel panel at the bottom.
    assert bubble.bubble_position((0, 0, 1920, 1040)) == (1920 - 60 - 24, 1040 - 60 - 24)
    # Second monitor on the right.
    assert bubble.bubble_position((1920, 0, 1280, 800)) == (1920 + 1280 - 84, 800 - 84)


def test_panel_above_the_bubble_and_on_screen() -> None:
    area = (0, 0, 1280, 800)
    x, y = bubble.panel_position((1196, 716, 60), (400, 620), area)
    assert (x + 400, y + 620) == (1196 + 60, 716 - 12)  # right-aligned, just above
    # Bubble dragged to the top-left: the panel opens below and stays on screen.
    x, y = bubble.panel_position((10, 10, 60), (400, 620), area)
    assert (x, y) == (0, 10 + 60 + 12)
    # Screen smaller than the panel: its top-left corner stays visible.
    assert bubble.panel_position((700, 500, 60), (400, 620), (0, 0, 800, 600)) == (360, 0)


def test_infinity_sign() -> None:
    points = bubble.infinity_points(30, 30, 36)
    assert points[0] == pytest.approx(points[-1])  # closed
    xs = [x for x, _ in points]
    assert min(xs) == pytest.approx(12) and max(xs) == pytest.approx(48)


def test_only_the_local_interface_is_shown() -> None:
    base = "http://127.0.0.1:27494"
    assert bubble.is_internal(f"{base}/?compact=1", base)
    assert bubble.is_internal(base, base)
    assert not bubble.is_internal("http://127.0.0.1:274940/", base)
    assert not bubble.is_internal("https://evil.example/", base)
    assert bubble.is_local_daemon(base) and bubble.is_local_daemon("http://localhost:1234/")
    assert not bubble.is_local_daemon("http://example.com:27494")
    assert not bubble.is_local_daemon("https://127.0.0.1:27494")
    assert not bubble.is_local_daemon(f"{base}/other")


def test_messages_from_the_page() -> None:
    assert bubble.parse_message('{"type": "attention", "count": 2}') == {
        "type": "attention",
        "count": 2,
    }
    assert bubble.parse_message("not json") == {}
    assert bubble.parse_message("[1, 2]") == {}


def test_single_instance(tmp_path: Path) -> None:
    lock = tmp_path / "bubble.lock"
    first = bubble.acquire_single_instance(lock)
    assert first is not None
    second = tmp_path / "second.py"
    second.write_text(
        "import importlib.util, pathlib, sys\n"
        f"spec = importlib.util.spec_from_file_location('b', {str(bubble_script())!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        f"sys.exit(module.acquire_single_instance(pathlib.Path({str(lock)!r})) is None)\n"
    )
    child = subprocess.run([sys.executable, str(second)], check=False)
    assert child.returncode == 1  # another process cannot take the bubble
    first.close()
    third = bubble.acquire_single_instance(lock)  # free again once the first one quits
    assert third is not None
    third.close()


# --- The command -------------------------------------------------------------------------------


def test_bubble_needs_gtk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_gui_python", lambda: None)
    result = runner.invoke(cli.app, ["bubble"])
    assert result.exit_code == 1
    assert "gir1.2-webkit2-4.1" in result.output


def test_bubble_starts_the_program(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[list[str]] = []
    monkeypatch.setattr(cli, "_gui_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(cli, "_running_daemon_url", lambda settings: "http://127.0.0.1:27494")
    monkeypatch.setattr(cli.subprocess, "Popen", lambda args, **kwargs: started.append(args))

    result = runner.invoke(cli.app, ["bulle"])  # French alias

    assert result.exit_code == 0, result.output
    assert "en bas à droite" in result.output
    assert started == [
        [
            "/usr/bin/python3",
            str(bubble_script()),
            "--url",
            "http://127.0.0.1:27494",
            "--lang",
            "fr",
        ]
    ]


def test_bubble_at_login(tmp_path: Path) -> None:
    entry = tmp_path / "xdg-config" / "autostart" / "kreafinity-bubble.desktop"
    result = runner.invoke(cli.app, ["bubble", "--autostart"])
    assert result.exit_code == 0, result.output
    text = entry.read_text()
    assert "[Desktop Entry]" in text and "Type=Application" in text
    assert f"Exec={sys.executable} -m kreafinity.cli bubble" in text
    assert runner.invoke(cli.app, ["bubble", "--no-autostart"]).exit_code == 0
    assert not entry.exists()


def test_desktop_entry_quoting() -> None:
    from kreafinity.system.integration import desktop_quote

    assert desktop_quote("/usr/bin/python3") == "/usr/bin/python3"
    assert desktop_quote("/opt/my apps/python") == '"/opt/my apps/python"'
    assert desktop_quote('a"b$c') == '"a\\"b\\$c"'


# --- A real bubble on a virtual screen ----------------------------------------------------------

GUI = cli._gui_python() if shutil.which("Xvfb") and shutil.which("xdotool") else None


@pytest.fixture
def screen() -> Iterator[dict[str, str]]:
    server = subprocess.Popen(
        ["Xvfb", "-displayfd", "1", "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert server.stdout is not None
    env = {**os.environ, "DISPLAY": f":{server.stdout.readline().strip()}"}
    # Test only: WebKit's own sandbox needs user namespaces, often unavailable in CI.
    env["WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS"] = "1"
    yield env
    server.terminate()
    server.wait()
    server.stdout.close()


def windows(env: dict[str, str], name: str) -> list[str]:
    found = subprocess.run(
        ["xdotool", "search", "--onlyvisible", "--name", name],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return found.stdout.split()


def wait_until(condition: Any, timeout: float = 20) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


@pytest.mark.skipif(GUI is None, reason="needs Xvfb, xdotool and GTK 3 + WebKitGTK for Python")
def test_real_bubble_on_screen(screen: dict[str, str], tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        **screen,
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
    }
    daemon = subprocess.Popen(
        [sys.executable, "-m", "kreafinity.api.server", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    assert wait_until(lambda: _healthy(url))
    assert GUI is not None
    program = subprocess.Popen(
        [GUI, str(bubble_script()), "--url", url],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert wait_until(lambda: windows(env, "Kreafinity — bulle")), "no bubble on screen"
        [window] = windows(env, "Kreafinity — bulle")
        geometry = subprocess.run(
            ["xdotool", "getwindowgeometry", "--shell", window],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        values = dict(line.split("=") for line in geometry.split())
        assert (int(values["X"]), int(values["Y"])) == (1280 - 84, 800 - 84)
        assert (int(values["WIDTH"]), int(values["HEIGHT"])) == (60, 60)

        def click() -> None:
            subprocess.run(
                ["xdotool", "mousemove", "1226", "746", "click", "1"], env=env, check=True
            )

        click()
        assert wait_until(lambda: windows(env, "^Kreafinity$")), "the panel did not open"
        click()
        assert wait_until(lambda: not windows(env, "^Kreafinity$")), "the panel did not close"
    finally:
        program.terminate()
        program.wait(timeout=10)
        daemon.terminate()
        daemon.wait(timeout=10)


def _healthy(url: str) -> bool:
    try:
        return httpx.get(f"{url}/api/health", timeout=1).json().get("name") == "kreafinity"
    except (httpx.HTTPError, json.JSONDecodeError, ValueError):
        return False
