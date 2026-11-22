"""First start: is an AI ready, and the welcome that helps choose one.

The daemon runs for real, with a fake Ollama (models, downloads) and a fake
Anthropic API (key check): no network call leaves the machine.
"""

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from kreafinity import onboarding

TESTS = Path(__file__).parent
SERVE = "from kreafinity.api.server import main; main()"


# --- Logic ------------------------------------------------------------------------------------


def test_recommended_model() -> None:
    assert onboarding.recommended(4).name == "qwen2.5:3b"
    assert onboarding.recommended(7.6).name == "qwen2.5:7b"  # « 8 Go » machines report a bit less
    assert onboarding.recommended(64).name == "qwen2.5:7b"  # vision models are proposed, not pushed
    assert onboarding.recommended(2).name == "qwen2.5:3b"


def test_has_model() -> None:
    installed = ["llama3.1:latest", "qwen2.5:7b"]
    assert onboarding.has_model(installed, "llama3.1")
    assert onboarding.has_model(installed, "qwen2.5:7b")
    assert not onboarding.has_model(installed, "qwen2.5:3b")
    assert not onboarding.has_model([], "llama3.1")


def test_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    common = {"is_ollama": False, "ollama_running": False, "model_present": False}
    assert onboarding.problem(requires_key=True, key_present=False, **common) == "no_key"
    assert onboarding.problem(requires_key=True, key_present=True, **common) is None
    local = {"requires_key": False, "key_present": False, "is_ollama": True}
    monkeypatch.setattr(onboarding, "ollama_installed", lambda: False)
    assert (
        onboarding.problem(**local, ollama_running=False, model_present=False) == "ollama_missing"
    )
    monkeypatch.setattr(onboarding, "ollama_installed", lambda: True)
    assert (
        onboarding.problem(**local, ollama_running=False, model_present=False) == "ollama_stopped"
    )
    assert onboarding.problem(**local, ollama_running=True, model_present=False) == "model_missing"
    assert onboarding.problem(**local, ollama_running=True, model_present=True) is None
    assert onboarding.MODEL_NAME.match("qwen2.5:7b") and onboarding.MODEL_NAME.match(
        "library/llama3.1"
    )
    for bad in ("../../etc", "a b", "x;rm", ":tag", ""):
        assert not onboarding.MODEL_NAME.match(bad)


# --- A fake Ollama and a fake Anthropic API -----------------------------------------------


class FakeServices:
    def __init__(self) -> None:
        self.models: list[str] = []
        self.pulls: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/api/tags":
                    return self._json(200, {"models": [{"name": m} for m in fake.models]})
                if self.path.startswith("/v1/models"):
                    sent = {self.headers.get("x-api-key"), self.headers.get("authorization")}
                    if not sent & {"sk-ant-good", "Bearer sk-ant-good"}:
                        return self._json(
                            401,
                            {
                                "type": "error",
                                "error": {
                                    "type": "authentication_error",
                                    "message": "invalid x-api-key",
                                },
                            },
                        )
                    return self._json(
                        200,
                        {
                            "data": [
                                {
                                    "id": "claude-test",
                                    "display_name": "Claude Test",
                                    "type": "model",
                                    "created_at": "2025-01-01T00:00:00Z",
                                }
                            ],
                            "has_more": False,
                            "first_id": "claude-test",
                            "last_id": "claude-test",
                        },
                    )
                self._json(404, {"error": "not found"})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path != "/api/pull":
                    return self._json(404, {"error": "not found"})
                model = body["model"]
                fake.pulls.append(model)
                if model == "unknown:1b":
                    return self._json(500, {"error": "pull model manifest: file does not exist"})
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                total = 2_000_000_000
                for line in (
                    {"status": "pulling manifest"},
                    {"status": "pulling abc", "total": total, "completed": total // 4},
                    {"status": "pulling abc", "total": total, "completed": total},
                    {"status": "success"},
                ):
                    self.wfile.write((json.dumps(line) + "\n").encode())
                    self.wfile.flush()
                    time.sleep(0.6)
                fake.models.append(model)

            def _json(self, code: int, data: Any) -> None:
                payload = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def services() -> Iterator[FakeServices]:
    fake = FakeServices()
    yield fake
    fake.close()


@pytest.fixture
def daemon(
    tmp_path: Path, home: Path, services: FakeServices
) -> Iterator[tuple[str, httpx.Client]]:
    from test_ui import free_port

    port = free_port()
    config = tmp_path / "xdg-config" / "kreafinity"
    config.mkdir(parents=True)
    (config / "config.yaml").write_text(
        f"provider: ollama/qwen2.5:3b\nproviders:\n  ollama:\n    base_url: {services.url}\n"
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "ANTHROPIC_BASE_URL": services.url,
        "PYTHONPATH": str(TESTS),
        "PYTHON_KEYRING_BACKEND": "memory_keyring.MemoryKeyring",
    }
    env.pop("ANTHROPIC_API_KEY", None)
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
    token = (tmp_path / "xdg-data" / "kreafinity" / "token").read_text().strip()
    client = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    yield url, client
    client.close()
    process.terminate()
    process.wait(timeout=10)


def wait_for(check: Any, timeout: float = 15) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := check():
            return result
        time.sleep(0.2)
    pytest.fail("timed out")


# --- API ----------------------------------------------------------------------------------------


def test_setup_api(daemon: tuple[str, httpx.Client], services: FakeServices) -> None:
    _, api = daemon
    status = api.get("/api/setup").json()
    assert status["needed"] and status["problem"] == "model_missing"
    assert status["ollama"] == {"installed": True, "running": True, "models": []}
    assert status["recommended"] in {m["name"] for m in status["local_models"]}

    assert api.post("/api/setup/ollama/pull", json={"model": "../x"}).status_code == 400
    assert api.post("/api/setup/ollama/pull", json={"model": "qwen2.5:3b"}).status_code == 202
    assert api.post("/api/setup/ollama/pull", json={"model": "qwen2.5:7b"}).status_code == 409
    status = wait_for(lambda: (s := api.get("/api/setup").json())["ready"] and s)
    assert status["needed"] is False and status["onboarded"] is False
    assert services.pulls == ["qwen2.5:3b"]

    # Claude without a key: the welcome is needed again.
    assert api.put("/api/providers/default", json={"provider": "anthropic"}).status_code == 200
    status = api.get("/api/setup").json()
    assert status["problem"] == "no_key" and status["needed"]

    config = api.post("/api/setup/complete").json()
    assert config["onboarded"] is True
    status = api.get("/api/setup").json()
    assert status["needed"] is False and status["problem"] == "no_key"  # no longer forced open


def test_pull_failure_event(daemon: tuple[str, httpx.Client]) -> None:
    from websockets.sync.client import connect

    url, api = daemon
    token = api.headers["Authorization"].split()[1]
    with connect(url.replace("http", "ws") + "/api/events") as socket:
        socket.send(json.dumps({"type": "auth", "token": token}))
        socket.recv(timeout=5)
        assert api.post("/api/setup/ollama/pull", json={"model": "unknown:1b"}).status_code == 202
        while True:
            event = json.loads(socket.recv(timeout=10))
            if event["type"] == "ollama_pull":
                break
    assert event["model"] == "unknown:1b" and "file does not exist" in event["error"]


# --- The welcome in a real browser -------------------------------------------------------------


playwright_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def page(daemon: tuple[str, httpx.Client]) -> Iterator[Any]:
    import glob

    from test_ui import browser as _unused  # noqa: F401 (same launch rules)

    with playwright_api.sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch()
        except Exception:
            candidates = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
            if not candidates:
                pytest.skip("no Chromium browser for Playwright")
            instance = playwright.chromium.launch(executable_path=candidates[-1])
        context = instance.new_context(viewport={"width": 1200, "height": 860}, locale="fr-FR")
        page = context.new_page()
        page.errors = []
        page.on("pageerror", lambda error: page.errors.append(str(error)))
        page.goto(daemon[0])
        yield page
        assert page.errors == []
        instance.close()


def test_welcome_local_model(page: Any, daemon: tuple[str, httpx.Client]) -> None:
    _, api = daemon
    page.wait_for_selector("#welcome-dialog[open]")
    assert page.inner_text("#welcome-heading") == "Bienvenue dans Kreafinity"
    page.click("#welcome-dialog >> text=Commencer")
    page.click(".choice-card[data-provider=ollama]")
    page.wait_for_selector(".model-option")
    labels = page.locator(".model-option").all_inner_texts()
    assert any("qwen2.5:3b" in label and "Go" in label for label in labels)
    page.check(".model-option input[value='qwen2.5:3b']")
    page.click("#welcome-dialog >> text=Utiliser ce modèle")
    page.wait_for_selector("#welcome-dialog progress:not([hidden])")
    page.wait_for_selector("#welcome-heading:text('Parler à Kreafinity')", timeout=20000)
    page.click("#welcome-dialog >> text=Plus tard")
    page.wait_for_selector("#welcome-heading:text('est prêt')")
    assert "Ollama · qwen2.5:3b" in page.inner_text("#welcome-dialog")
    page.click("#welcome-dialog .onboarding-actions >> text=Commencer")
    page.wait_for_selector("#welcome-dialog:not([open])", state="attached")
    config = api.get("/api/config").json()
    assert config["provider"] == "ollama/qwen2.5:3b" and config["onboarded"] is True
    page.reload()
    page.wait_for_selector("#connection.online")
    page.wait_for_timeout(500)
    assert page.locator("#welcome-dialog[open]").count() == 0  # not again


def test_welcome_api_key(page: Any, daemon: tuple[str, httpx.Client]) -> None:
    _, api = daemon
    page.wait_for_selector("#welcome-dialog[open]")
    page.click("#welcome-dialog >> text=Commencer")
    page.click(".choice-card[data-provider=anthropic]")
    page.fill("#welcome-key", "sk-ant-wrong")
    page.click("#welcome-dialog >> text=Vérifier et continuer")
    error = page.wait_for_selector("#welcome-dialog .form-error:not([hidden])")
    assert "La clé n'a pas fonctionné" in error.inner_text()
    page.fill("#welcome-key", "sk-ant-good")
    page.click("#welcome-dialog >> text=Vérifier et continuer")
    page.wait_for_selector("#welcome-heading:text('Parler à Kreafinity')")
    assert api.get("/api/config").json()["provider"] == "anthropic"
    providers = api.get("/api/providers").json()["providers"]
    assert next(p for p in providers if p["name"] == "anthropic")["api_key"] == "keyring"
