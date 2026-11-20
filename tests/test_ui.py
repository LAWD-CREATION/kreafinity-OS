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
SLOW_ANSWER = "Voici une réponse donnée lentement, mot après mot, jusqu'à la fin de la phrase."


class SlowWords(list[dict[str, Any]]):
    """Answer pieces streamed with a pause between them (a slow model)."""

    def __iter__(self) -> Iterator[dict[str, Any]]:
        for piece in super().__iter__():
            time.sleep(0.15)
            yield piece


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
    if "lentement" in request:
        return SlowWords(words(SLOW_ANSWER))
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


def test_panel_shows_a_question_from_the_desktop_search(
    browser: Any, daemon: str, tmp_path: Path
) -> None:
    """A desktop search asks while the answer streams: the panel shows all of it."""
    context = browser.new_context(viewport={"width": 400, "height": 620}, locale="fr-FR")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(
        "window.__messages = [];"
        "window.webkit = {messageHandlers: {kreafinity: {"
        "postMessage: (message) => window.__messages.push(JSON.parse(message))}}};"
    )
    page.goto(f"{daemon}/?compact=1")
    page.wait_for_selector("#connection.online")

    token = (tmp_path / "xdg-data" / "kreafinity" / "token").read_text().strip()
    auth = {"Authorization": f"Bearer {token}"}
    conversation = httpx.post(f"{daemon}/api/conversations", headers=auth, timeout=5).json()
    httpx.post(
        f"{daemon}/api/conversations/{conversation['id']}/messages",
        json={"content": "Réponds lentement"},
        headers=auth,
        timeout=5,
    )
    time.sleep(0.6)  # the answer has started streaming
    shown = httpx.post(
        f"{daemon}/api/ui/show",
        json={"conversation_id": conversation["id"]},
        headers=auth,
        timeout=5,
    )
    assert shown.json() == {"delivered": 1}  # this panel listens

    page.wait_for_selector(f"text={SLOW_ANSWER}", timeout=15_000)
    assistant = page.locator(".message.assistant .body")
    assert assistant.count() == 1 and assistant.inner_text().strip() == SLOW_ANSWER
    assert page.locator(".message.user").inner_text().strip() == "Réponds lentement"
    assert {"type": "show"} in page.evaluate("window.__messages")  # the bubble pops up

    # Opened directly on the conversation (what the Plasma widget does).
    other = context.new_page()
    other.goto(f"{daemon}/?compact=1&conversation={conversation['id']}")
    other.wait_for_selector(f"text={SLOW_ANSWER}")
    assert errors == []
    context.close()


class FakeGitHub:
    """A tiny GitHub: the compare API finds two changes; downloads are missing."""

    def __init__(self, installed: str, latest: str) -> None:
        import http.server
        import threading

        compare = {
            "status": "ahead",
            "commits": [
                {"sha": "b" * 40, "commit": {"message": "feat: memory that improves"}},
                {"sha": latest, "commit": {"message": "fix: faster search"}},
            ],
        }

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == f"/api/repos/k/os/compare/{installed}...main":
                    body = json.dumps(compare).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_update_banner(browser: Any, daemon: str, tmp_path: Path) -> None:
    installed, latest = "a" * 40, "c" * 40
    github = FakeGitHub(installed, latest)
    record = {
        "repository": f"{github.url}/k/os",
        "api": f"{github.url}/api/repos/k/os",
        "ref": "main",
        "commit": installed,
    }
    (tmp_path / "xdg-data" / "kreafinity" / "install.json").write_text(json.dumps(record))
    token = (tmp_path / "xdg-data" / "kreafinity" / "token").read_text().strip()
    auth = {"Authorization": f"Bearer {token}"}
    try:
        found = httpx.post(f"{daemon}/api/update/check", headers=auth, timeout=10).json()
        assert found["available"] and found["latest"] == latest

        context = browser.new_context(viewport={"width": 1280, "height": 860}, locale="fr-FR")
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(daemon)
        banner = page.locator("#update-banner")
        banner.wait_for()
        assert "Une mise à jour de Kreafinity est disponible" in banner.inner_text()
        assert "2 nouveautés." in banner.inner_text()
        page.click("#update-changes summary")
        assert page.locator("#update-list li").all_inner_texts() == [
            "fix: faster search",
            "feat: memory that improves",
        ]

        # The button starts the installation; here the download is missing.
        page.click("#update-install")
        page.wait_for_selector("#update-banner.working")
        assert "Mise à jour en cours" in banner.inner_text()
        page.wait_for_selector("#update-banner.failed", timeout=60_000)
        assert "La mise à jour a échoué" in banner.inner_text()
        assert page.inner_text("#update-install") == "Réessayer"
        status = httpx.get(f"{daemon}/api/update", headers=auth, timeout=5).json()["status"]
        assert status["state"] == "failed" and status["target"] == latest

        # Settings: installed version and « Rechercher maintenant ».
        page.click("#open-settings")
        page.wait_for_selector("text=Version installée : aaaaaaa (branche main)")
        page.click("#settings-update-check")
        page.wait_for_selector("text=Une mise à jour est disponible.")
        page.keyboard.press("Escape")

        # « Plus tard »: hidden, and still hidden after a reload.
        page.click("#update-later")
        assert banner.is_hidden()
        page.reload()
        page.wait_for_selector("#connection.online")
        page.wait_for_timeout(500)
        assert banner.is_hidden()
        assert errors == []
        context.close()
    finally:
        github.close()


def test_soul_in_the_settings(page: Any, tmp_path: Path) -> None:
    soul_file = tmp_path / "xdg-data" / "kreafinity" / "memory" / "soul.md"
    page.click("#open-settings")
    page.wait_for_selector("#settings[open]")
    page.wait_for_selector("#settings-soul")
    assert page.input_value("#settings-soul").startswith("Je suis Kreafinity")
    assert page.is_visible("#settings-soul-seed") and not page.is_visible("#settings-soul-reset")
    page.locator("#settings-soul-evolution").scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / "soul.png"))

    page.fill("#settings-soul", "Je suis Kreafinity, taquin et très bref.")
    page.uncheck("#settings-soul-evolution")
    page.click("#settings button[type=submit]")
    page.wait_for_selector("#settings", state="hidden")
    assert soul_file.read_text() == "Je suis Kreafinity, taquin et très bref.\n"
    config = (tmp_path / "xdg-config" / "kreafinity" / "config.yaml").read_text()
    assert "soul_evolution: false" in config

    page.click("#open-settings")
    page.wait_for_selector("#settings-soul-reset:visible")
    assert not page.is_visible("#settings-soul-seed")
    page.once("dialog", lambda dialog: dialog.accept())
    page.click("#settings-soul-reset")
    page.wait_for_selector("#settings-soul-seed:visible")
    assert page.input_value("#settings-soul").startswith("Je suis Kreafinity, l'assistant")
    assert not soul_file.exists()
