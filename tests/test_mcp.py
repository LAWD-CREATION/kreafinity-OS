"""MCP: configuration, the stdio and HTTP clients, tools, the manager, the agent, the API."""

import json
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import keyring
import pytest

from fakes import ScriptedProvider, call, reply
from kreafinity.agent import Agent, Conversation
from kreafinity.config import ConfigError, load_settings, save_settings
from kreafinity.mcp import McpManager, config
from kreafinity.mcp.client import HttpConnection, McpError, initialize, list_tools
from kreafinity.mcp.tools import convert, risk_of, tool_name
from kreafinity.tools import ToolContext, ToolRegistry
from memory_keyring import MemoryKeyring

SERVER = str(Path(__file__).parent / "fake_mcp_server.py")


@pytest.fixture
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


def fake(**extra: Any) -> dict[str, Any]:
    return {"command": sys.executable, "args": [SERVER], **extra}


# --- Configuration -----------------------------------------------------------------------------


def test_config_rules(tmp_path: Path) -> None:
    servers = config.parse_servers(
        {
            "heure": {
                "command": "uvx",
                "args": ["mcp-server-time"],
                "auto_approve": ["get_current_time"],
            },
            "maison": {"url": "https://ha.local/mcp", "token": True},
        }
    )
    assert servers["heure"].transport == "stdio" and servers["maison"].transport == "http"
    assert servers["heure"].describe() == "uvx mcp-server-time"
    assert config.to_mapping(servers["heure"]) == {
        "command": "uvx",
        "args": ["mcp-server-time"],
        "auto_approve": ["get_current_time"],
    }
    for entry, message in (
        ({"command": "x", "env": {"GITHUB_TOKEN": "ghp_123"}}, "must not be stored"),
        ({"command": "x", "env": {"OPENAI_API_KEY": "sk"}}, "secret_env"),
        ({"command": "x", "url": "https://a"}, "either 'command'"),
        ({}, "either 'command'"),
        ({"url": "ftp://a"}, "http"),
        ({"url": "https://user:pw@a.b/mcp"}, "credentials"),
        ({"command": "x", "args": "-y"}, "list of strings"),
        ({"command": "x", "secret_env": ["BAD NAME"]}, "invalid variable"),
        ({"command": "x", "what": 1}, "unknown setting"),
    ):
        with pytest.raises(config.McpConfigError, match=message):
            config.parse_server("ok", entry)
    with pytest.raises(config.McpConfigError, match="lower case"):
        config.parse_server("Mon Serveur", {"command": "x"})
    # Checked with the rest of the settings: a secret never reaches the file.
    with pytest.raises(ConfigError, match="must not be stored"):
        save_settings({"mcp_servers": {"gh": {"command": "x", "env": {"GITHUB_TOKEN": "t"}}}})
    save_settings({"mcp_servers": {"heure": {"command": "uvx"}}})
    assert load_settings().mcp_servers == {"heure": {"command": "uvx"}}


def test_secrets(memory_keyring: MemoryKeyring) -> None:
    config.set_secret("gh", "GITHUB_TOKEN", " ghp_abc ")
    assert config.get_secret("gh", "GITHUB_TOKEN") == "ghp_abc"
    assert memory_keyring.passwords == {("kreafinity", "mcp:gh:GITHUB_TOKEN"): "ghp_abc"}
    with pytest.raises(ValueError):
        config.set_secret("gh", "GITHUB_TOKEN", "a\nb")
    config.delete_secret("gh", "GITHUB_TOKEN")
    config.delete_secret("gh", "GITHUB_TOKEN")  # twice: no error
    assert config.get_secret("gh", "GITHUB_TOKEN") is None


# --- Tools ---------------------------------------------------------------------------------------


def test_tool_names_and_risks() -> None:
    taken: set[str] = set()
    assert tool_name("heure", "get time!", taken) == "mcp_heure_get_time_"
    long = tool_name("serveur", "x" * 80, taken)
    assert len(long) <= 64 and long.startswith("mcp_serveur_x")
    taken.add("mcp_a_b")
    assert tool_name("a", "b", taken) != "mcp_a_b"
    assert risk_of({"name": "t"}, ()).value == "confirm"
    assert risk_of({"name": "t"}, ("t",)).value == "safe"
    assert risk_of({"name": "t", "annotations": {"destructiveHint": True}}, ()).value == "danger"
    assert risk_of({"name": "t", "annotations": {"readOnlyHint": True}}, ()).value == "confirm"


def test_convert() -> None:
    result = convert(
        {
            "content": [
                {"type": "text", "text": "a"},
                {"type": "image", "mimeType": "image/png", "data": "QUJD"},
                {"type": "image", "mimeType": "image/tiff", "data": "QUJD"},
                {"type": "resource", "resource": {"uri": "file:///x.txt", "text": "hello"}},
                {"type": "resource_link", "uri": "https://e.x", "name": "lien"},
                {"type": "audio", "data": "x"},
            ],
            "isError": False,
        }
    )
    assert result.content.splitlines() == [
        "a",
        "[image not shown: image/tiff]",
        "[file:///x.txt]",
        "hello",
        "[link: https://e.x] lien",
        "[audio content not shown]",
    ]
    assert [i.media_type for i in result.images] == ["image/png"]
    assert convert({"structuredContent": {"n": 1}, "isError": True}).content == '{"n": 1}'
    assert convert({"isError": True}).is_error


# --- The stdio client and the manager ------------------------------------------------------------


async def test_stdio_server(
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    context: ToolContext,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    config.set_secret("faux", "FAKE_SECRET", "s3cret")
    manager = McpManager(log_dir=tmp_path)
    manager.configure(
        {
            "faux": fake(
                env={"FAKE_PLAIN": "bonjour"}, secret_env=["FAKE_SECRET"], auto_approve=["echo"]
            )
        }
    )
    await manager.wait_ready(20)
    (status,) = manager.status()
    assert status["status"] == "ready" and status["server"] == "fake-mcp"
    assert [t["name"] for t in status["tools"]][:2] == ["echo", "picture"]  # both pages
    assert status["secrets"] == [{"name": "FAKE_SECRET", "stored": True}]
    tools = {tool.remote_name: tool for tool in manager.tools()}
    assert set(tools) == {
        "echo",
        "picture",
        "fail",
        "env",
        "delete_everything",
        "pinger",
        "bad schema",
    }
    assert tools["echo"].risk.value == "safe" and tools["fail"].risk.value == "confirm"
    assert tools["delete_everything"].risk.value == "danger"
    assert tools["bad schema"].parameters == {"type": "object", "properties": {}}
    assert tools["echo"].description.startswith("[faux, MCP extension]")

    assert (await tools["echo"].run({"text": "salut"}, context)).content == "écho : salut"
    picture = await tools["picture"].run({}, context)
    assert picture.content == "voici" and picture.images[0].media_type == "image/png"
    assert (await tools["fail"].run({}, context)).is_error
    env = json.loads((await tools["env"].run({}, context)).content)
    assert env == {"secret": "s3cret", "leak": None, "plain": "bonjour"}
    pinged = json.loads((await tools["pinger"].run({}, context)).content)
    assert pinged == {"ping": {}, "sampling": -32601}  # ping answered, sampling refused
    preview = await tools["fail"].preview({"x": 1}, context)
    assert preview.summary == "Utiliser « fail » de l'extension faux"
    assert preview.details[0] == "x : 1" and "Extension MCP « faux »" in preview.details[-1]
    assert "initialized" in (tmp_path / "faux.log").read_text()

    # The server stops (crash): the next call starts it again.
    server = manager.servers["faux"]
    with pytest.raises(McpError):
        await server.call("crash", {})
    assert (await tools["echo"].run({"text": "encore"}, context)).content == "écho : encore"
    await manager.aclose()


async def test_server_problems(memory_keyring: MemoryKeyring) -> None:
    manager = McpManager()
    manager.configure(
        {
            "absent": {"command": "/nonexistent/mcp-server"},
            "secret": fake(secret_env=["FAKE_SECRET"]),
            "off": fake(enabled=False),
        }
    )
    await manager.wait_ready(20)
    status = {s["name"]: s for s in manager.status()}
    assert status["absent"]["status"] == "error" and "cannot start" in status["absent"]["error"]
    assert (
        status["secret"]["status"] == "needs_secret" and status["secret"]["error"] == "FAKE_SECRET"
    )
    assert status["off"]["status"] == "disabled"
    assert manager.tools() == []
    # A changed configuration replaces the server.
    manager.configure({"absent": fake()})
    await manager.wait_ready(20)
    assert manager.status()[0]["status"] == "ready"
    await manager.aclose()


# --- Streamable HTTP --------------------------------------------------------------------------


class HttpMcp:
    def __init__(self) -> None:
        self.seen: list[tuple[str | None, str | None, str]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.seen.append(
                    (
                        self.headers.get("Authorization"),
                        self.headers.get("Mcp-Session-Id"),
                        message.get("method", "reply"),
                    )
                )
                if "id" not in message or "method" not in message:
                    self.send_response(202)
                    self.end_headers()
                    return
                if self.headers.get("Authorization") != "Bearer jeton":
                    self.send_response(401)
                    self.end_headers()
                    return
                if message["method"] == "initialize":
                    body = {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "serverInfo": {"name": "http-fake"},
                        },
                    }
                    payload = json.dumps(body).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Mcp-Session-Id", "session-42")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                # Answer as server-sent events, after a ping request.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                events = [
                    {"jsonrpc": "2.0", "id": 99, "method": "ping"},
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {"tools": [{"name": "hello"}]},
                    },
                ]
                for event in events:
                    self.wfile.write(f"event: message\ndata: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()

            def do_DELETE(self) -> None:
                fake.seen.append((None, self.headers.get("Mcp-Session-Id"), "DELETE"))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


async def test_http_transport() -> None:
    fake = HttpMcp()
    connection = HttpConnection(fake.url, token="jeton")
    info = await initialize(connection)
    assert info["serverInfo"]["name"] == "http-fake" and connection.session_id == "session-42"
    assert await list_tools(connection) == [{"name": "hello"}]
    await connection.close()
    methods = [(auth, session, method) for auth, session, method in fake.seen]
    assert methods[0] == ("Bearer jeton", None, "initialize")
    assert ("Bearer jeton", "session-42", "notifications/initialized") in methods
    assert ("Bearer jeton", "session-42", "reply") in methods  # the ping was answered
    assert methods[-1] == (None, "session-42", "DELETE")
    with pytest.raises(McpError, match="HTTP 401"):
        await initialize(HttpConnection(fake.url, token="faux"))
    fake.server.shutdown()
    fake.server.server_close()


# --- With the agent ----------------------------------------------------------------------------


async def test_agent_asks_for_mcp_tools(
    context: ToolContext, memory_keyring: MemoryKeyring
) -> None:
    manager = McpManager()
    manager.configure({"faux": fake(auto_approve=["echo"])})
    await manager.wait_ready(20)
    tools = {t.remote_name: t for t in manager.tools()}
    provider = ScriptedProvider(
        reply("", [call(tools["echo"].name, "c1", text="un"), call(tools["fail"].name, "c2")]),
        reply("Fini."),
    )
    asked: list[str] = []

    async def approve(request: Any) -> bool:
        asked.append(request.tool)
        return False  # the user refuses

    agent = Agent(provider, ToolRegistry(manager.tools()), context, approver=approve)
    conversation = Conversation()
    async for _ in agent.run(conversation, "Teste"):
        pass
    results = [m for m in conversation.messages if m.role == "tool"]
    assert results[0].content == "écho : un"  # auto-approved
    assert asked == [tools["fail"].name] and "denied" in results[1].content
    await manager.aclose()


# --- API ------------------------------------------------------------------------------------------


@pytest.fixture
def daemon(tmp_path: Path, home: Path) -> Iterator[httpx.Client]:
    import os
    import subprocess
    import time

    from test_ui import free_port

    port = free_port()
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "PYTHONPATH": str(Path(__file__).parent),
        "PYTHON_KEYRING_BACKEND": "memory_keyring.MemoryKeyring",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from kreafinity.api.server import main; main()",
            "--port",
            str(port),
        ],
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
    token = (tmp_path / "xdg-data" / "kreafinity" / "token").read_text().strip()
    client = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    yield client
    client.close()
    process.terminate()
    process.wait(timeout=10)


def test_mcp_api(daemon: httpx.Client, tmp_path: Path) -> None:
    api = daemon
    assert api.get("/api/mcp").json()["servers"] == []
    bad = api.post(
        "/api/mcp/servers", json={"name": "gh", "command": "x", "env": {"GH_TOKEN": "t"}}
    )
    assert bad.status_code == 400 and "must not be stored" in bad.json()["error"]["message"]
    created = api.post(
        "/api/mcp/servers",
        json={
            "name": "faux",
            "command": sys.executable,
            "args": [SERVER],
            "secret_env": ["FAKE_SECRET"],
        },
    )
    assert created.status_code == 201
    (server,) = api.get("/api/mcp").json()["servers"]
    assert server["status"] == "needs_secret" and server["error"] == "FAKE_SECRET"
    assert api.put("/api/mcp/servers/faux/secrets/OTHER", json={"value": "x"}).status_code == 400
    assert (
        api.put("/api/mcp/servers/faux/secrets/FAKE_SECRET", json={"value": "s3cret"}).status_code
        == 200
    )
    import time

    deadline = time.monotonic() + 20
    while (server := api.get("/api/mcp").json()["servers"][0])["status"] != "ready":
        assert time.monotonic() < deadline, server
        time.sleep(0.3)
    assert server["secrets"] == [{"name": "FAKE_SECRET", "stored": True}]
    assert api.patch("/api/mcp/servers/faux", json={"auto_approve": ["echo"]}).status_code == 200
    tools = [t for t in api.get("/api/tools").json() if t.get("server") == "faux"]
    assert {t["name"]: t["risk"] for t in tools}["mcp_faux_echo"] == "safe"
    config_file = (tmp_path / "xdg-config" / "kreafinity" / "config.yaml").read_text()
    assert "s3cret" not in config_file and "auto_approve" in config_file
    assert api.delete("/api/mcp/servers/faux").status_code == 200
    assert api.get("/api/mcp").json()["servers"] == []
    assert api.delete("/api/mcp/servers/faux").status_code == 404


def test_settings_section(daemon: httpx.Client) -> None:
    """Settings > Extensions in a real browser: add a server, see its tools, exempt one."""
    playwright_api = pytest.importorskip("playwright.sync_api")
    import glob

    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception:
            candidates = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
            if not candidates:
                pytest.skip("no Chromium browser for Playwright")
            browser = playwright.chromium.launch(executable_path=candidates[-1])
        page = browser.new_page(viewport={"width": 1200, "height": 900}, locale="fr-FR")
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(str(daemon.base_url))
        page.wait_for_selector("#connection.online")
        if page.locator("#welcome-dialog[open]").count():
            page.click("#welcome-dialog >> text=Plus tard")
        page.click("#open-settings")
        section = page.wait_for_selector("[data-section=mcp]")
        page.wait_for_selector("[data-section=mcp] >> text=Aucune extension")
        page.click(".mcp-add summary")
        page.fill("#mcp-name", "faux")
        page.fill("#mcp-command", f'"{sys.executable}" "{SERVER}"')
        page.click(".mcp-add button.primary")
        page.wait_for_selector(".mcp-card >> text=Prête", timeout=20000)
        labels = page.locator(".mcp-tool").all_inner_texts()
        assert "Répéter" in labels and any("modifie ou supprime" in label for label in labels)
        page.locator(".mcp-tool", has_text="Répéter").locator("input").check()
        page.wait_for_function("() => document.querySelector('.mcp-tool input')?.checked === true")
        assert section.is_visible()
        browser.close()
    assert errors == []
    servers = daemon.get("/api/mcp").json()["servers"]
    assert servers[0]["tools"][0] == {
        **servers[0]["tools"][0],
        "name": "echo",
        "auto_approve": True,
    }
