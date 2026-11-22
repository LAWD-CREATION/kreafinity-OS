"""The assistant's browser: protocols, launching, session and tools.

A fake browser speaks WebDriver BiDi (Firefox) or Chrome DevTools over a real
local WebSocket and plays a small login page. The end of the file drives a
real headless Chrome or Firefox when one is installed.
"""

import asyncio
import base64
import functools
import http.server
import io
import json
import shutil
import subprocess
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from PIL import Image as PILImage
from websockets.asyncio.server import ServerConnection, serve

from kreafinity.browser import BrowserError, launcher, scripts
from kreafinity.browser import session as session_module
from kreafinity.browser.protocol import BidiBackend, BrowserBusyError, CdpBackend
from kreafinity.tools import ToolContext, ToolError, ToolRegistry

PAGE = "https://banque.example/connexion"
DONE = "https://banque.example/merci"
ELEMENTS = [
    {
        "ref": 1,
        "tag": "input",
        "role": "",
        "label": "Adresse e-mail",
        "visible": True,
        "type": "email",
        "value": "",
    },
    {
        "ref": 2,
        "tag": "input",
        "role": "",
        "label": "Mot de passe",
        "visible": True,
        "type": "password",
        "secret": True,
    },
    {"ref": 3, "tag": "select", "role": "", "label": "Pays", "visible": True, "value": "France"},
    {"ref": 4, "tag": "button", "role": "", "label": "Se connecter", "visible": True},
    {
        "ref": 5,
        "tag": "a",
        "role": "",
        "label": "Aide",
        "visible": False,
        "href": "https://banque.example/aide",
    },
]


def png() -> bytes:
    output = io.BytesIO()
    PILImage.new("RGB", (800, 600), "white").save(output, "PNG")
    return output.getvalue()


class FakePage:
    """The page scripts' answers, and what the fake browser was asked."""

    def __init__(self) -> None:
        self.url = PAGE
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.typed: list[str] = []

    def script(self, function: str, argument: dict[str, Any]) -> dict[str, Any]:
        if function == scripts.READ_PAGE:
            return {
                "url": self.url,
                "title": "Connexion",
                "text": "Espace client",
                "text_truncated": False,
                "elements": ELEMENTS,
                "scroll": {"y": 0, "height": 2000, "viewport": 700},
            }
        if function == scripts.LOCATE:
            if argument["ref"] == 2 and argument["purpose"] == "type":
                return {"error": "secret"}
            if argument["ref"] == 9:
                return {"error": "gone"}
            return {"x": 100 + argument["ref"], "y": 200, "tag": "input", "disabled": False}
        if function == scripts.SELECT_OPTION:
            if argument["option"] == "Mars":
                return {"error": "no_option", "options": ["France", "La Réunion"]}
            return {"selected": argument["option"]}
        if function == scripts.READY_STATE:
            return {"state": "complete", "url": self.url}
        if function == scripts.CLEAR_POINTER:
            return {}
        if function in (scripts.SCROLL, scripts.POINT):
            return {"y": 500} if function == scripts.SCROLL else {"shown": True}
        if function == scripts.BACK:
            self.url = PAGE
            return {}
        return {"url": self.url, "title": "Connexion" if self.url == PAGE else "Merci"}

    def clicked(self, x: int) -> None:
        if x == 104:  # « Se connecter »
            self.url = DONE


class FakeBidi(FakePage):
    def __init__(self, *, busy: bool = False) -> None:
        super().__init__()
        self.busy = busy

    def answer(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "session.new" and self.busy:
            return {
                "type": "error",
                "error": "session not created",
                "message": "Maximum number of active sessions",
            }
        result: dict[str, Any] = {}
        if method == "browsingContext.getTree":
            result = {"contexts": [{"context": "tab-1", "url": self.url}]}
        elif method == "browsingContext.navigate":
            self.url = params["url"]
        elif method == "script.callFunction":
            argument = json.loads(params["arguments"][0]["value"])
            value = json.dumps(self.script(params["functionDeclaration"], argument))
            result = {"type": "success", "result": {"type": "string", "value": value}}
        elif method == "browsingContext.captureScreenshot":
            result = {"data": base64.b64encode(png()).decode()}
        elif method == "input.performActions":
            source = params["actions"][0]
            if source["type"] == "pointer":
                self.clicked(source["actions"][0]["x"])
            else:
                self.typed.append("".join(a["value"] for a in source["actions"][::2]))
        return {"type": "success", "result": result}

    async def serve(self, socket: ServerConnection) -> None:
        async for raw in socket:
            message = json.loads(raw)
            self.commands.append((message["method"], message["params"]))
            answer = self.answer(message["method"], message["params"])
            await socket.send(json.dumps({"id": message["id"], **answer}))


class FakeCdp(FakePage):
    def answer(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if method == "Target.getTargets":
            result = {"targetInfos": [{"targetId": "T1", "type": "page", "url": self.url}]}
        elif method == "Target.attachToTarget":
            result = {"sessionId": "S1"}
        elif method == "Page.navigate":
            self.url = params["url"]
            result = {"frameId": "F"}
        elif method == "Runtime.evaluate":
            expression = params["expression"]
            function = next(
                f
                for f in (
                    scripts.READ_PAGE,
                    scripts.LOCATE,
                    scripts.SELECT_OPTION,
                    scripts.READY_STATE,
                    scripts.CLEAR_POINTER,
                    scripts.SCROLL,
                    scripts.POINT,
                    scripts.BACK,
                    expression[1 : expression.index(")(")],
                )
                if expression.startswith(f"({f})(")
            )
            argument = json.loads(json.loads(expression[len(function) + 3 : -1]))
            result = {
                "result": {"type": "string", "value": json.dumps(self.script(function, argument))}
            }
        elif method == "Page.captureScreenshot":
            result = {"data": base64.b64encode(png()).decode()}
        elif method == "Input.dispatchMouseEvent" and params["type"] == "mousePressed":
            self.clicked(params["x"])
        elif method == "Input.insertText":
            self.typed.append(params["text"])
        return {"result": result}

    async def serve(self, socket: ServerConnection) -> None:
        async for raw in socket:
            message = json.loads(raw)
            self.commands.append((message["method"], message["params"]))
            if message["method"] != "Target.getTargets":
                assert ("sessionId" in message) == (not message["method"].startswith("Target."))
            await socket.send(
                json.dumps(
                    {"id": message["id"], **self.answer(message["method"], message["params"])}
                )
            )


@pytest.fixture
async def bidi() -> AsyncIterator[tuple[FakeBidi, str]]:
    fake = FakeBidi()
    async with serve(fake.serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield fake, f"ws://127.0.0.1:{port}/session"


@pytest.fixture
async def cdp() -> AsyncIterator[tuple[FakeCdp, str]]:
    fake = FakeCdp()
    async with serve(fake.serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield fake, f"ws://127.0.0.1:{port}/devtools/browser/x"


def methods(fake: FakePage) -> list[str]:
    return [m for m, _ in fake.commands]


# --- Protocols ------------------------------------------------------------------------------


async def test_bidi_protocol(bidi: tuple[FakeBidi, str]) -> None:
    fake, url = bidi
    backend = BidiBackend(url)
    await backend.connect()
    assert methods(fake)[:2] == ["session.new", "browsingContext.getTree"]
    assert backend.context == "tab-1"
    await backend.navigate("https://example.org/")
    assert fake.commands[-1][1] == {
        "context": "tab-1",
        "url": "https://example.org/",
        "wait": "complete",
    }
    assert (await backend.call(scripts.READY_STATE, {}))["state"] == "complete"
    await backend.click(12, 34)
    move = fake.commands[-1][1]["actions"][0]["actions"][0]
    assert move == {"type": "pointerMove", "x": 12, "y": 34, "origin": "viewport"}
    await backend.type_text("Été")
    assert fake.typed == ["Été"]
    await backend.press("Enter")
    assert fake.commands[-1][1]["actions"][0]["actions"][0]["value"] == ""
    assert (await backend.screenshot()).startswith(b"\x89PNG")
    await backend.close()
    assert methods(fake)[-1] == "session.end"  # or Firefox keeps the session forever


async def test_bidi_busy_firefox() -> None:
    fake = FakeBidi(busy=True)
    async with serve(fake.serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(BrowserBusyError):
            await BidiBackend(f"ws://127.0.0.1:{port}/session").connect()


async def test_cdp_protocol(cdp: tuple[FakeCdp, str]) -> None:
    fake, url = cdp
    backend = CdpBackend(url)
    await backend.connect()
    assert methods(fake) == ["Target.getTargets", "Target.attachToTarget"]
    assert (backend.target, backend.session) == ("T1", "S1")
    await backend.navigate("https://example.org/")
    assert (await backend.call(scripts.READY_STATE, {}))["url"] == "https://example.org/"
    await backend.click(5, 6)
    assert methods(fake)[-3:] == ["Input.dispatchMouseEvent"] * 3
    await backend.type_text("Bonjour")
    assert fake.typed == ["Bonjour"]
    await backend.press("Enter")
    assert fake.commands[-2][1]["text"] == "\r"
    await backend.close()


async def test_unreachable_browser() -> None:
    with pytest.raises(BrowserError, match="cannot connect"):
        await BidiBackend("ws://127.0.0.1:9/session").connect()


# --- Launching -------------------------------------------------------------------------------


def program(folder: Path, name: str, body: str) -> Path:
    path = folder / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def test_find_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("KREAFINITY_BROWSER", raising=False)
    with pytest.raises(BrowserError, match="no supported browser"):
        launcher.find_browser()
    program(tmp_path, "google-chrome", "")
    program(tmp_path, "firefox", 'exec /snap/bin/firefox "$@"\n')  # Ubuntu's snap wrapper
    found = launcher.find_browser()
    assert found.kind == "firefox"
    assert found.profile == Path.home() / "snap/firefox/common/kreafinity-browser"
    monkeypatch.setenv("KREAFINITY_BROWSER", "google-chrome")
    found = launcher.find_browser()
    assert found.kind == "chromium" and found.profile.name == "chromium"
    assert "xdg-data" in str(found.profile)


def test_launch_and_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    fake = program(
        tmp_path,
        "firefox",
        'while [ "$1" != "--profile" ]; do shift; done\n'
        "printf '%s' '" + json.dumps({"ws_host": "127.0.0.1", "ws_port": 4444}) + "'"
        ' > "$2/WebDriverBiDiServer.json"\n',
    )
    browser = launcher.BrowserProgram("firefox", str(fake), tmp_path / "profile")
    assert launcher.endpoint(browser) is None
    assert launcher.launch(browser) == "ws://127.0.0.1:4444/session"
    assert (
        'user_pref("signon.rememberSignons", false);' in (tmp_path / "profile/user.js").read_text()
    )

    chrome = program(
        tmp_path,
        "chromium",
        "dir=${1#--user-data-dir=}\n"
        'printf "5555\\n/devtools/browser/abc" > "$dir/DevToolsActivePort"\n',
    )
    browser = launcher.BrowserProgram("chromium", str(chrome), tmp_path / "chrome")
    assert launcher.launch(browser) == "ws://127.0.0.1:5555/devtools/browser/abc"

    broken = launcher.BrowserProgram(
        "firefox", str(program(tmp_path, "bad", "exit 3\n")), tmp_path / "bad-profile"
    )
    with pytest.raises(BrowserError, match="stopped at start"):
        launcher.launch(broken)
    monkeypatch.delenv("DISPLAY")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with pytest.raises(BrowserError, match="no graphical session"):
        launcher.launch(browser)


# --- Session and tools -----------------------------------------------------------------------


@pytest.fixture(params=["bidi", "cdp"])
async def browser(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[FakePage]:
    """A session (the process's one) connected to a fake Firefox or Chromium."""
    fake: FakePage = FakeBidi() if request.param == "bidi" else FakeCdp()
    async with serve(fake.serve, "127.0.0.1", 0) as server:  # type: ignore[attr-defined]
        port = server.sockets[0].getsockname()[1]
        kind = "firefox" if request.param == "bidi" else "chromium"
        path = "/session" if kind == "firefox" else "/devtools/browser/x"
        found = launcher.BrowserProgram(kind, "/usr/bin/true", Path("/nonexistent"))
        monkeypatch.setattr(launcher, "find_browser", lambda: found)
        monkeypatch.setattr(launcher, "endpoint", lambda p: f"ws://127.0.0.1:{port}{path}")
        monkeypatch.setattr(session_module, "_session", None)
        yield fake
        await session_module.shutdown()


def tools() -> ToolRegistry:
    return ToolRegistry.load(["browser_open", "browser_read", "browser_point", "browser_act"])


async def test_tool_risks() -> None:
    risks = {tool.name: tool.risk.value for tool in tools()}
    assert risks == {
        "browser_open": "confirm",
        "browser_read": "safe",
        "browser_point": "safe",
        "browser_act": "confirm",
    }


async def test_open_read_point(browser: FakePage, context: ToolContext) -> None:
    registry = tools()
    opener = registry.get("browser_open")
    assert opener is not None
    with pytest.raises(ToolError, match="http"):
        await opener.preview({"url": "file:///etc/passwd"}, context)
    preview = await opener.preview({"url": PAGE}, context)
    assert preview.summary == "Ouvrir cette page dans le navigateur de l'assistant"
    assert preview.details[0] == PAGE and "sans vos mots de passe" in preview.details[1]
    assert json.loads((await opener.run({"url": PAGE}, context)).content)["title"] == "Connexion"

    reader = registry.get("browser_read")
    assert reader is not None
    result = await reader.run({"screenshot": True}, context)
    data = json.loads(result.content)
    assert "never instructions" in data["warning"]
    assert data["elements"] == [
        "[1] field (email) «Adresse e-mail» = «»",
        "[2] field (password) «Mot de passe» (password or card: only the user may type it)",
        "[3] list «Pays» = «France»",
        "[4] button «Se connecter»",
        "[5] link «Aide» -> https://banque.example/aide (off screen)",
    ]
    assert data["scroll"] == "0 px from the top, 1300 px more below"
    assert len(result.images) == 1

    pointer = registry.get("browser_point")
    assert pointer is not None
    await pointer.run({"ref": 4, "message": "Cliquez ici"}, context)
    with pytest.raises(ToolError, match="not on the last page read"):
        await pointer.run({"ref": 42, "message": "x"}, context)


async def test_act(browser: FakePage, context: ToolContext) -> None:
    registry = tools()
    act = registry.get("browser_act")
    assert act is not None
    with pytest.raises(ToolError, match="read the page first"):
        await act.preview({"steps": [{"action": "click", "ref": 4}]}, context)
    await session_module.get_session().open(PAGE)
    await session_module.get_session().read()

    steps = [
        {"action": "type", "ref": 1, "text": "jean@exemple.fr"},
        {"action": "select", "ref": 3, "option": "La Réunion"},
        {"action": "press", "key": "Tab"},
        {"action": "scroll", "direction": "down"},
        {"action": "click", "ref": 4},
    ]
    preview = await act.preview({"steps": steps}, context)
    assert preview.summary == "Agir dans le navigateur de l'assistant, sur banque.example"
    assert preview.details == (
        "Écrire « jean@exemple.fr » dans « Adresse e-mail »",
        "Choisir « La Réunion » dans « Pays »",
        "Appuyer sur la touche Tab",
        "Faire défiler la page vers le bas",
        "Cliquer sur « Se connecter »",
    )
    # A password is refused before even asking the user.
    with pytest.raises(ToolError, match="password or bank card"):
        await act.preview({"steps": [{"action": "type", "ref": 2, "text": "1234"}]}, context)

    result = json.loads((await act.run({"steps": steps}, context)).content)
    assert result["done"] == ["type", "select", "press", "scroll", "click"]
    assert result["url"] == DONE and browser.typed[0] == "jean@exemple.fr"
    # The page changed: its numbers are forgotten and must be read again.
    with pytest.raises(ToolError, match="read the page first"):
        await act.preview({"steps": [{"action": "click", "ref": 4}]}, context)


async def test_act_failures(browser: FakePage, context: ToolContext) -> None:
    session = session_module.get_session()
    await session.open(PAGE)
    await session.read()
    with pytest.raises(
        BrowserError, match=r"step 2 \(select\) failed: no option 'Mars'.*La Réunion"
    ):
        await session.act([{"action": "scroll"}, {"action": "select", "ref": 3, "option": "Mars"}])
    with pytest.raises(BrowserError, match="no longer on the page"):
        await session.act([{"action": "click", "ref": 9}])
    # The page script refuses secret fields too, whatever the cache says.
    with pytest.raises(BrowserError, match="password or bank card"):
        await session.act([{"action": "type", "ref": 2, "text": "x"}])


async def test_not_open(monkeypatch: pytest.MonkeyPatch, context: ToolContext) -> None:
    found = launcher.BrowserProgram("firefox", "/usr/bin/true", Path("/nonexistent"))
    monkeypatch.setattr(launcher, "find_browser", lambda: found)
    monkeypatch.setattr(session_module, "_session", None)
    reader = tools().get("browser_read")
    assert reader is not None
    with pytest.raises(ToolError, match="not open: open a page first"):
        await reader.run({}, context)


# --- A real browser (headless) ---------------------------------------------------------------

SITE = {
    "index.html": """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>Test</title></head><body><h1>Espace client</h1>
<form action="done.html"><label for="e">E-mail</label><input id="e" name="e" type="email">
<label for="p">Mot de passe</label><input id="p" name="p" type="password">
<label for="c">Pays</label><select id="c" name="c"><option>France</option>
<option>La Réunion</option></select><button>Envoyer</button></form></body></html>""",
    "done.html": "<!doctype html><title>Merci</title><p>Reçu</p>",
}


@pytest.fixture
def site(tmp_path: Path) -> Iterator[str]:
    for name, content in SITE.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    handler.log_message = lambda *args: None  # type: ignore[attr-defined]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


REAL = [name for name in ("google-chrome", "chromium", "firefox") if shutil.which(name)]


@pytest.mark.timeout(120)
@pytest.mark.skipif(not REAL, reason="no browser installed")
@pytest.mark.parametrize("name", REAL[:1] + [n for n in REAL[1:] if n == "firefox"])
async def test_real_browser(name: str, site: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KREAFINITY_BROWSER", name)
    monkeypatch.setenv("KREAFINITY_BROWSER_HEADLESS", "1")
    monkeypatch.setattr(session_module, "_session", None)
    session = session_module.get_session()
    try:
        await session.open(f"{site}/index.html")
        page = await session.read(screenshot=True)
        labels = {e["label"]: e for e in page.elements}
        assert labels["Mot de passe"].get("secret") and page.screenshot
        with pytest.raises(BrowserError, match="password"):
            await session.act(
                [{"action": "type", "ref": labels["Mot de passe"]["ref"], "text": "x"}]
            )
        await session.point(labels["Envoyer"]["ref"], "Cliquez ici")
        await session.act(
            [
                {"action": "type", "ref": labels["E-mail"]["ref"], "text": "jean@exemple.fr"},
                {"action": "select", "ref": labels["Pays"]["ref"], "option": "La Réunion"},
                {"action": "click", "ref": labels["Envoyer"]["ref"]},
            ]
        )
        page = await session.read()
        assert page.title == "Merci"
        assert "e=jean%40exemple.fr" in page.url and "c=La+R%C3%A9union" in page.url
    finally:
        await session_module.shutdown()
        used = launcher.find_browser()
        if used.kind == "firefox":
            launcher.stop(used)
        else:
            subprocess.run(["pkill", "-f", f"user-data-dir={used.profile}"], check=False)
        await asyncio.sleep(0.5)
