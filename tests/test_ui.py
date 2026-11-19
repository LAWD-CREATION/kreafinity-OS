"""End-to-end tests of the web interface in a real browser (Playwright + Chromium).

The real daemon runs in a subprocess and talks to a scripted fake Ollama.
Skipped when Playwright or a Chromium browser is not available
(``pip install playwright && playwright install chromium``).
"""

import glob
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from fake_ollama import FakeOllama, tool_call, words

playwright_api = pytest.importorskip("playwright.sync_api")

SERVE = "from kreafinity.api.server import main; main()"
XSS_TEXT = (
    'Voici <img src=x onerror="window.__xss=1"> et '
    "[un lien](javascript:window.__xss=2) puis [Kreafinity](https://kreafinity.com)."
)


def scenario(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the fake model answers, depending on the conversation."""
    last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
    request = messages[last_user]["content"]
    results = [m for m in messages[last_user:] if m["role"] == "tool"]
    if "XSS" in request:
        return words(XSS_TEXT)
    if "plus gros fichiers" not in request:
        return words("Bonjour ! Je suis **Kreafinity**, votre assistant.")
    if not results:
        return [
            tool_call(
                "search_files", pattern="*", path="~/Téléchargements", sort_by="size", limit=5
            )
        ]
    if len(results) == 1:
        found = json.loads(results[0]["content"])["results"]
        names = ", ".join(f"{r['name']} ({r['size']})" for r in found)
        return [
            *words(f"Voici les 5 plus gros fichiers : {names}."),
            tool_call("move_to_trash", paths=[r["path"] for r in found]),
        ]
    if "denied" in results[-1]["content"]:
        return words("D'accord, je n'ai rien mis à la corbeille.")
    return words("C'est fait : les 5 fichiers sont dans la corbeille.")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with playwright_api.sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch()
        except Exception:
            candidates = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
            if not candidates:
                pytest.skip("no Chromium browser for Playwright")
            instance = playwright.chromium.launch(executable_path=candidates[-1])
        yield instance
        instance.close()


@pytest.fixture
def downloads(home: Path) -> Path:
    folder = home / "Téléchargements"
    for index in range(1, 8):
        (folder / f"fichier{index}.bin").write_bytes(b"x" * index * 100_000)
    return folder


@pytest.fixture
def daemon(tmp_path: Path, home: Path) -> Iterator[str]:
    with FakeOllama(scenario) as ollama:
        port = free_port()
        config = tmp_path / "xdg-config" / "kreafinity"
        config.mkdir(parents=True)
        (config / "config.yaml").write_text(f"providers:\n  ollama:\n    base_url: {ollama.url}\n")
        env = {
            **os.environ,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        }
        process = subprocess.Popen(
            [sys.executable, "-c", SERVE, "--port", str(port)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{url}/api/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.1)
        else:
            process.kill()
            pytest.fail("the daemon did not start")
        yield url
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def page(browser: Any, daemon: str) -> Iterator[Any]:
    context = browser.new_context(viewport={"width": 1280, "height": 860}, locale="fr-FR")
    page = context.new_page()
    page.errors = []
    page.on("pageerror", lambda error: page.errors.append(str(error)))
    page.goto(daemon)
    page.wait_for_selector("#app:not([hidden])")
    page.wait_for_selector("#connection.online")
    yield page
    assert page.errors == []
    context.close()


def test_end_of_step_1_in_the_browser(
    page: Any, downloads: Path, home: Path, tmp_path: Path
) -> None:
    """The step 1 goal, from the web interface: list, approve, trash."""
    assert "que puis-je faire pour vous" in page.inner_text("#welcome-title")
    page.click(".suggestion >> nth=0")

    card = page.wait_for_selector(".approval.danger")
    assert "Mettre 5 éléments à la corbeille" in card.inner_text()
    details = page.locator(".approval-details li").all_inner_texts()
    assert len(details) == 5 and all(d.startswith("~/Téléchargements/fichier") for d in details)
    confirm = page.locator(".approval.danger .button.danger")
    assert confirm.is_disabled()  # explicit confirmation first
    assert (downloads / "fichier7.bin").exists()

    page.check(".approval-confirm input")
    confirm.click()
    page.wait_for_selector("text=C'est fait : les 5 fichiers sont dans la corbeille.")
    assert page.locator(".approval.approved").count() == 1
    assert page.locator(".tool.ok").count() == 2

    trash = tmp_path / "xdg-data" / "Trash" / "files"
    assert sorted(p.name for p in trash.iterdir()) == [f"fichier{i}.bin" for i in range(3, 8)]
    assert (downloads / "fichier1.bin").exists()
    page.wait_for_selector(".conversation-item.active >> text=Montre-moi les 5 plus gros")


def test_denial_in_the_browser(page: Any, downloads: Path) -> None:
    page.click(".suggestion >> nth=0")
    page.wait_for_selector(".approval.danger")
    page.click(".approval.danger >> text=Refuser")
    page.wait_for_selector("text=Action refusée.")
    page.wait_for_selector("text=D'accord, je n'ai rien mis à la corbeille.")
    assert all((downloads / f"fichier{i}.bin").exists() for i in range(1, 8))


def test_ai_text_is_never_html(page: Any) -> None:
    page.fill("#prompt", "Test XSS")
    page.press("#prompt", "Enter")
    page.wait_for_selector(".markdown >> text=Kreafinity")
    markdown = page.locator(".message.assistant .markdown").last
    assert "<img src=x" in markdown.inner_text()  # shown as text
    assert markdown.locator("img").count() == 0
    hrefs = markdown.locator("a").evaluate_all("links => links.map(a => a.getAttribute('href'))")
    assert hrefs == ["https://kreafinity.com"]
    assert page.evaluate("window.__xss") is None


def test_history_and_settings(page: Any, daemon: str, tmp_path: Path) -> None:
    page.fill("#prompt", "Bonjour")
    page.press("#prompt", "Enter")
    page.wait_for_selector(".markdown strong >> text=Kreafinity")

    page.reload()
    page.wait_for_selector("#connection.online")
    page.click(".conversation-item >> text=Bonjour")
    page.wait_for_selector(".message.user >> text=Bonjour")
    assert "votre assistant" in page.inner_text(".message.assistant .markdown")

    page.click("#open-settings")
    page.wait_for_selector("#settings[open]")
    page.select_option("#settings-language", "en")
    page.click("#settings button[type=submit]")
    page.wait_for_selector("#new-conversation >> text=New conversation")
    assert "language: en" in (tmp_path / "xdg-config" / "kreafinity" / "config.yaml").read_text()


def test_compact_panel_of_the_desktop_bubble(browser: Any, daemon: str, downloads: Path) -> None:
    """The bubble's panel: compact layout, and messages to the bubble (badge, close, expand)."""
    context = browser.new_context(viewport={"width": 400, "height": 620}, locale="fr-FR")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    # Stand-in for the bubble's bridge (WebKitGTK's script message handler).
    page.add_init_script(
        "window.__messages = [];"
        "window.webkit = {messageHandlers: {kreafinity: {"
        "postMessage: (message) => window.__messages.push(JSON.parse(message))}}};"
    )
    page.goto(f"{daemon}/?compact=1")
    page.wait_for_selector("#connection.online")
    messages = lambda: page.evaluate("window.__messages")  # noqa: E731

    assert page.evaluate("document.documentElement.classList.contains('compact')")
    assert not page.is_visible(".model-picker")
    assert page.is_visible("#close-panel") and page.is_visible("#expand")
    assert page.locator(".suggestion:visible").count() == 3

    page.fill("#prompt", "Montre-moi les 5 plus gros fichiers de mon dossier Téléchargements")
    page.press("#prompt", "Enter")
    page.wait_for_selector(".approval.danger")
    assert {"type": "attention", "count": 1} in messages()  # badge on the bubble
    page.click(".approval.danger >> text=Refuser")
    page.wait_for_selector("text=D'accord, je n'ai rien mis à la corbeille.")
    assert messages()[-1] == {"type": "attention", "count": 0}

    page.click("#expand")
    assert messages()[-1] == {"type": "expand"}
    page.keyboard.press("Escape")
    assert messages()[-1] == {"type": "close"}
    assert errors == []
    context.close()
