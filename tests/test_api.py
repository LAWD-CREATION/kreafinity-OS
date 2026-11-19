"""Tests of the daemon API (REST + WebSocket) with FastAPI's test client."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fakes import ScriptedProvider, call, reply
from kreafinity import __version__
from kreafinity.agent import ConversationStore
from kreafinity.api import create_app, load_or_create_token, token_path
from kreafinity.providers import ProviderUnavailableError
from kreafinity.service import AssistantService
from kreafinity.tools import ToolContext

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PORT = 27494


@pytest.fixture
def providers() -> dict[str, ScriptedProvider]:
    return {}


@pytest.fixture
def client(home: Path, providers: dict[str, ScriptedProvider]) -> Iterator[TestClient]:
    def create(name: str, **options: Any) -> ScriptedProvider:
        provider = providers[name]
        provider.name = name
        return provider

    def service() -> AssistantService:
        return AssistantService(
            ConversationStore(), provider_factory=create, context=ToolContext.create(home=home)
        )

    app = create_app(
        service, TOKEN, port=PORT, allowed_hosts=("testserver", "127.0.0.1", "localhost")
    )
    with TestClient(app) as test_client:
        yield test_client


def events_socket(client: TestClient, **kwargs: Any) -> Any:
    socket = client.websocket_connect("/api/events", **kwargs)
    return socket


def receive_until(socket: Any, event_type: str) -> list[dict[str, Any]]:
    events = []
    while True:
        event = socket.receive_json()
        events.append(event)
        if event["type"] == event_type:
            return events


# --- Authentication and host checks --------------------------------------------------


def test_health_needs_no_token(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.json() == {"status": "ok", "name": "kreafinity", "version": __version__}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": TOKEN},
    ],
)
def test_token_is_required(client: TestClient, headers: dict[str, str]) -> None:
    for method, url in [
        ("GET", "/api/conversations"),
        ("POST", "/api/conversations"),
        ("GET", "/api/config"),
        ("PATCH", "/api/config"),
        ("POST", "/api/approvals/x"),
    ]:
        response = client.request(method, url, headers=headers, json={})
        assert response.status_code == 401, (method, url)
        assert response.headers["www-authenticate"] == "Bearer"


def test_body_is_not_read_without_token(client: TestClient) -> None:
    response = client.post(
        "/api/conversations/x/messages",
        content=b"{" * 1_000_000,  # not even valid JSON
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401
    assert client.get("/api/unknown-route").status_code == 401  # no route discovery either


def test_foreign_host_is_refused(client: TestClient) -> None:
    """DNS rebinding: a website resolving its name to 127.0.0.1 is still refused."""
    response = client.get("/api/health", headers={**AUTH, "Host": "evil.example"})
    assert response.status_code == 400


def test_no_documentation_or_cors(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    response = client.options(
        "/api/conversations",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in response.headers


def test_websocket_authentication(client: TestClient) -> None:
    with events_socket(client) as socket:
        socket.send_json({"type": "auth", "token": "wrong"})
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    with events_socket(client) as socket:
        socket.send_text("not json")
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    with (
        pytest.raises(WebSocketDisconnect),
        events_socket(client, headers={"Origin": "https://evil.example"}) as socket,
    ):
        socket.receive_json()
    with events_socket(client, headers={"Origin": f"http://127.0.0.1:{PORT}"}) as socket:
        socket.send_json({"type": "auth", "token": TOKEN})
        ready = socket.receive_json()
        assert ready == {
            "type": "ready",
            "version": __version__,
            "running": [],
            "pending_approvals": [],
        }


def test_token_file(tmp_path: Path) -> None:
    token = load_or_create_token()
    assert len(token) >= 43
    path = token_path()
    assert path == tmp_path / "xdg-data" / "kreafinity" / "token"
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_or_create_token() == token  # stable across restarts
    path.chmod(0o644)
    assert load_or_create_token() == token
    assert path.stat().st_mode & 0o777 == 0o600


# --- Conversations ------------------------------------------------------------------


def test_conversation_crud(client: TestClient) -> None:
    created = client.post("/api/conversations", headers=AUTH, json={"title": "Rangement"})
    assert created.status_code == 201
    conversation = created.json()
    assert conversation["title"] == "Rangement" and conversation["running"] is False
    untitled = client.post("/api/conversations", headers=AUTH)
    assert untitled.status_code == 201

    listed = client.get("/api/conversations", headers=AUTH).json()
    assert {c["id"] for c in listed} == {conversation["id"], untitled.json()["id"]}

    url = f"/api/conversations/{conversation['id']}"
    assert client.patch(url, headers=AUTH, json={"title": "Photos"}).json()["title"] == "Photos"
    assert client.patch(url, headers=AUTH, json={"title": ""}).status_code == 422
    assert client.get(url, headers=AUTH).json()["messages"] == []
    assert client.delete(url, headers=AUTH).status_code == 204
    missing = client.get(url, headers=AUTH)
    assert missing.status_code == 404
    assert missing.json() == {"error": {"code": "not_found", "message": "Conversation not found"}}
    assert client.get("/api/conversations?limit=0", headers=AUTH).status_code == 422


def test_message_streamed_over_websocket(
    client: TestClient, providers: dict[str, ScriptedProvider]
) -> None:
    providers["ollama"] = ScriptedProvider(reply("Bonjour depuis le démon."))
    conversation_id = client.post("/api/conversations", headers=AUTH).json()["id"]

    with events_socket(client) as socket:
        socket.send_json({"type": "auth", "token": TOKEN})
        assert socket.receive_json()["type"] == "ready"
        response = client.post(
            f"/api/conversations/{conversation_id}/messages",
            headers=AUTH,
            json={"content": "Salut"},
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        events = receive_until(socket, "run_finished")

    kinds = [e["type"] for e in events]
    assert kinds[-3:] == ["run_started", "text_delta", "run_finished"]
    assert all(e.get("run_id") == run_id for e in events if e["type"] != "conversation_updated")
    assert events[-2]["text"] == "Bonjour depuis le démon."

    saved = client.get(f"/api/conversations/{conversation_id}", headers=AUTH).json()
    assert saved["title"] == "Salut"
    assert [m["content"] for m in saved["messages"]] == ["Salut", "Bonjour depuis le démon."]


def test_approval_over_the_api(
    client: TestClient, providers: dict[str, ScriptedProvider], home: Path
) -> None:
    providers["ollama"] = ScriptedProvider(
        reply("", [call("move_to_trash", paths=["~/Documents/Facture-2026.pdf"])]),
        reply("C'est fait."),
    )
    conversation_id = client.post("/api/conversations", headers=AUTH).json()["id"]

    with events_socket(client) as socket:
        socket.send_json({"type": "auth", "token": TOKEN})
        socket.receive_json()
        client.post(
            f"/api/conversations/{conversation_id}/messages",
            headers=AUTH,
            json={"content": "Supprime la facture"},
        )
        request = receive_until(socket, "approval_required")[-1]
        assert request["risk"] == "danger"
        assert request["summary"] == "Mettre 1 élément à la corbeille"
        assert request["details"] == [f"{home / 'Documents' / 'Facture-2026.pdf'} (100 B)"]

        # Busy while waiting; the approval is listed; decisions must be real booleans.
        busy = client.post(
            f"/api/conversations/{conversation_id}/messages",
            headers=AUTH,
            json={"content": "Et autre chose ?"},
        )
        assert busy.status_code == 409
        pending = client.get("/api/approvals", headers=AUTH).json()
        assert [a["approval_id"] for a in pending] == [request["approval_id"]]
        url = f"/api/approvals/{request['approval_id']}"
        assert client.post(url, headers=AUTH, json={"approved": "yes"}).status_code == 422
        assert (home / "Documents" / "Facture-2026.pdf").exists()

        assert client.post(url, headers=AUTH, json={"approved": True}).status_code == 200
        events = receive_until(socket, "run_finished")

    finished = next(e for e in events if e["type"] == "tool_finished")
    assert finished["status"] == "ok"
    assert not (home / "Documents" / "Facture-2026.pdf").exists()
    assert client.post(url, headers=AUTH, json={"approved": True}).status_code == 404


def test_cancel_over_the_api(client: TestClient, providers: dict[str, ScriptedProvider]) -> None:
    providers["ollama"] = ScriptedProvider(reply("", [call("create_folder", path="~/Z")]))
    conversation_id = client.post("/api/conversations", headers=AUTH).json()["id"]
    with events_socket(client) as socket:
        socket.send_json({"type": "auth", "token": TOKEN})
        socket.receive_json()
        client.post(
            f"/api/conversations/{conversation_id}/messages", headers=AUTH, json={"content": "x"}
        )
        receive_until(socket, "approval_required")
        response = client.post(f"/api/conversations/{conversation_id}/cancel", headers=AUTH)
        assert response.status_code == 200
        assert receive_until(socket, "run_finished")[-1]["reason"] == "cancelled"
    response = client.post(f"/api/conversations/{conversation_id}/cancel", headers=AUTH)
    assert response.status_code == 404


def test_message_validation(client: TestClient, providers: dict[str, ScriptedProvider]) -> None:
    conversation_id = client.post("/api/conversations", headers=AUTH).json()["id"]
    url = f"/api/conversations/{conversation_id}/messages"
    assert client.post(url, headers=AUTH, json={"content": ""}).status_code == 422
    assert client.post(url, headers=AUTH, json={}).status_code == 422
    unknown = client.post(url, headers=AUTH, json={"content": "x", "provider": "skynet"})
    assert unknown.status_code == 400
    assert unknown.json()["error"]["code"] == "invalid"
    missing = client.post("/api/conversations/nope/messages", headers=AUTH, json={"content": "x"})
    assert missing.status_code == 404


# --- Providers, tools and settings -----------------------------------------------------


def test_providers_and_models(client: TestClient, providers: dict[str, ScriptedProvider]) -> None:
    listed = client.get("/api/providers", headers=AUTH).json()
    assert listed["default"] == "ollama/llama3.1"
    assert {p["name"] for p in listed["providers"]} >= {"anthropic", "mistral", "ollama", "openai"}

    providers["ollama"] = ScriptedProvider(reply("x"))
    models = client.get("/api/providers/ollama/models", headers=AUTH)
    assert models.json() == [{"id": "fake-model", "display_name": "fake-model"}]

    class Offline(ScriptedProvider):
        async def list_models(self) -> Any:
            raise ProviderUnavailableError("down", provider="mistral")

    providers["mistral"] = Offline(reply("x"))
    offline = client.get("/api/providers/mistral/models", headers=AUTH)
    assert offline.status_code == 503
    assert offline.json()["error"]["code"] == "provider_unavailable"

    chosen = client.put("/api/providers/default", headers=AUTH, json={"provider": "mistral"})
    assert chosen.status_code == 200 and chosen.json()["provider"] == "mistral"
    refused = client.put("/api/providers/default", headers=AUTH, json={"provider": "skynet"})
    assert refused.status_code == 400


def test_config_and_tools(client: TestClient) -> None:
    config = client.get("/api/config", headers=AUTH).json()
    assert config["language"] == "fr" and config["port"] == PORT

    updated = client.patch("/api/config", headers=AUTH, json={"fallback": "ollama/llama3.1"})
    assert updated.json()["fallback"] == "ollama/llama3.1"
    for invalid in (
        {"max_iterations": "beaucoup"},
        {"port": 80},
        {"unknown": 1},
        {"providers": {"anthropic": {"api_key": "sk-ant-x"}}},
    ):
        response = client.patch("/api/config", headers=AUTH, json=invalid)
        assert response.status_code == 400, invalid
    assert client.patch("/api/config", headers=AUTH, json=[1, 2]).status_code == 422

    tools = {t["name"]: t for t in client.get("/api/tools", headers=AUTH).json()}
    assert tools["system_info"]["risk"] == "safe"
    assert tools["open_url"]["risk"] == "confirm"
    assert tools["move_to_trash"]["risk"] == "danger"
    assert all(t["enabled"] for t in tools.values())
