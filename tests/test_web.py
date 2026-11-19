"""The web interface: recognizing the user's own browser, static files, security headers."""

import json
import os
import re
import socket
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kreafinity.agent import ConversationStore
from kreafinity.api import create_app
from kreafinity.api.peer import _decode, peer_uid
from kreafinity.api.web import web_root
from kreafinity.service import AssistantService
from kreafinity.tools import ToolContext

TOKEN = "w" * 43
PORT = 27494
ORIGIN = f"http://127.0.0.1:{PORT}"
WEB = {"X-Kreafinity-Client": "web"}
UI = Path(__file__).resolve().parents[1] / "ui" / "web"


# --- Which user is behind a connection ---------------------------------------------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc only")
def test_peer_uid_of_a_real_connection() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        with socket.create_connection(server.getsockname()):
            accepted, _ = server.accept()
            with accepted:
                uid = peer_uid(accepted.getpeername(), accepted.getsockname())
    assert uid == os.getuid()


def test_peer_uid_unknown_connections() -> None:
    assert peer_uid(None, ("127.0.0.1", 1)) is None
    assert peer_uid(("testclient", 50000), ("127.0.0.1", 1)) is None
    assert peer_uid(("127.0.0.1", 1), ("127.0.0.1", 2)) is None


def test_decode_proc_net_addresses() -> None:
    if sys.byteorder == "little":
        assert _decode("0100007F:6B66") == ("127.0.0.1", 27494)
        assert _decode("0000000000000000FFFF00000100007F:0050") == ("127.0.0.1", 80)
        assert _decode("00000000000000000000000001000000:0016") == ("::1", 22)
    assert _decode("nonsense") is None


# --- Authentication of the web interface -------------------------------------------


@pytest.fixture
def owner_uid() -> list[int | None]:
    """UID the fake kernel lookup reports for every connection."""
    return [os.getuid()]


@pytest.fixture
def client(home: Path, owner_uid: list[int | None]) -> Iterator[TestClient]:
    def service() -> AssistantService:
        return AssistantService(ConversationStore(), context=ToolContext.create(home=home))

    app = create_app(
        service,
        TOKEN,
        port=PORT,
        allowed_hosts=("testserver", "127.0.0.1", "localhost"),
        web_dir=UI,
        peer_lookup=lambda client, server: owner_uid[0],
    )
    with TestClient(app) as test_client:
        yield test_client


def test_own_browser_needs_no_token(client: TestClient) -> None:
    assert client.get("/api/conversations", headers=WEB).status_code == 200
    assert client.get("/api/conversations", headers={**WEB, "Origin": ORIGIN}).status_code == 200
    same_site = {**WEB, "Sec-Fetch-Site": "same-origin"}
    assert client.post("/api/conversations", headers=same_site).status_code == 201


@pytest.mark.parametrize(
    "headers",
    [
        {},  # no header: plain requests (forms, images…) from any website
        {"X-Kreafinity-Client": "other"},
        {**WEB, "Origin": "https://evil.example"},
        {**WEB, "Origin": "null"},
        {**WEB, "Sec-Fetch-Site": "cross-site"},
        {**WEB, "Sec-Fetch-Site": "same-site"},
    ],
)
def test_other_websites_are_refused(client: TestClient, headers: dict[str, str]) -> None:
    assert client.post("/api/conversations", headers=headers).status_code == 401


def test_other_users_are_refused(client: TestClient, owner_uid: list[int | None]) -> None:
    owner_uid[0] = os.getuid() + 1
    assert client.get("/api/conversations", headers=WEB).status_code == 401
    owner_uid[0] = None  # unknown (not Linux…)
    assert client.get("/api/conversations", headers=WEB).status_code == 401
    # The token still works for them.
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/api/conversations", headers=auth).status_code == 200


def test_websocket_from_own_browser(client: TestClient, owner_uid: list[int | None]) -> None:
    with client.websocket_connect("/api/events", headers={"Origin": ORIGIN}) as socket_:
        socket_.send_json({"type": "auth", "token": None})
        assert socket_.receive_json()["type"] == "ready"
    # Without Origin (not a browser) the token is needed.
    with client.websocket_connect("/api/events") as socket_:
        socket_.send_json({"type": "auth", "token": None})
        with pytest.raises(WebSocketDisconnect):
            socket_.receive_json()
    owner_uid[0] = os.getuid() + 1
    with client.websocket_connect("/api/events", headers={"Origin": ORIGIN}) as socket_:
        socket_.send_json({"type": "auth"})
        with pytest.raises(WebSocketDisconnect):
            socket_.receive_json()


# --- Static files and headers ------------------------------------------------------------


def test_interface_is_served(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert '<script type="module" src="js/main.js"></script>' in page.text
    script = client.get("/js/main.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert client.get("/locales/fr.json").json()["sidebar.new"] == "Nouvelle conversation"
    assert client.get("/css/style.css").status_code == 200
    assert client.get("/img/logo.svg").headers["content-type"].startswith("image/svg")
    assert client.get("/nope.js").status_code == 404
    assert client.get("/api/nope", headers=WEB).status_code == 404  # API routes win


def test_security_headers(client: TestClient) -> None:
    headers = client.get("/").headers
    policy = headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "script-src 'self'" in policy and "unsafe-inline" not in policy
    assert f"connect-src 'self' ws://testserver:{PORT}" in policy
    assert "frame-ancestors 'none'" in policy
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cache-control"] == "no-cache"
    api_headers = client.get("/api/health").headers
    assert api_headers["cache-control"] == "no-store"
    assert "content-security-policy" in api_headers


def test_web_root_finds_the_interface() -> None:
    root = web_root()
    assert root is not None and (root / "index.html").is_file()


# --- Static checks of the interface's code ---------------------------------------------------


def _ui_sources() -> dict[str, str]:
    files = [UI / "index.html", *sorted((UI / "js").glob("*.js"))]
    return {path.name: path.read_text(encoding="utf-8") for path in files}


def test_translations_are_complete() -> None:
    french = json.loads((UI / "locales" / "fr.json").read_text(encoding="utf-8"))
    english = json.loads((UI / "locales" / "en.json").read_text(encoding="utf-8"))
    assert set(french) == set(english)
    used: set[str] = set()
    for source in _ui_sources().values():
        used.update(re.findall(r"\bt\(\"([a-z0-9_.]+)\"", source))
        used.update(re.findall(r'data-i18n(?:-aria|-placeholder)?="([a-z0-9_.]+)"', source))
    assert used, "no translation keys found"
    assert sorted(used - set(french)) == []
    for name in ("system_info", "list_directory", "search_files", "move_to_trash", "open_url"):
        assert f"tool.{name}" in french and f"tool.{name}.help" in french


def test_interface_never_injects_html() -> None:
    """Text from the AI or from file names must never be parsed as HTML."""
    for name, source in _ui_sources().items():
        if name == "index.html":
            assert re.findall(r"<script[^>]*>", source) == [
                '<script type="module" src="js/main.js">'
            ]  # no inline script
            assert not re.search(r"\son[a-z]+\s*=", source)  # no inline event handler
            assert "style=" not in source  # blocked by the Content Security Policy anyway
            continue
        for forbidden in (".innerHTML", ".outerHTML", ".insertAdjacentHTML(", "document.write("):
            assert forbidden not in source, f"{forbidden} in {name}"
        assert not re.search(r"\beval\(|new Function\(", source), name
