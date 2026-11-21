"""Open windows on KDE Plasma (KWin scripts over D-Bus), with a fake KWin."""

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from dbus_fast import Message
from dbus_fast.aio import MessageBus

from kreafinity.system import windows
from kreafinity.tools import ToolContext, ToolError, ToolRegistry
from test_plasma_install import session_bus  # noqa: F401

DOLPHIN = {
    "id": "{d0}",
    "title": "Documents — Dolphin",
    "application": "dolphin",
    "minimized": False,
    "active": True,
}
FIREFOX = {
    "id": "{f1}",
    "title": "Kreafinity — Mozilla Firefox",
    "application": "firefox",
    "minimized": False,
    "active": False,
}
FIREFOX_2 = {**FIREFOX, "id": "{f2}", "title": "Météo Réunion — Mozilla Firefox"}


class FakeKWin:
    """Loads scripts, "runs" them on a list of windows and answers like the real script."""

    def __init__(self, bus: MessageBus, plasma: int, open_windows: list[dict[str, Any]]) -> None:
        self.bus = bus
        self.plasma = plasma
        self.windows = open_windows
        self.scripts: dict[int, str] = {}
        self.unloaded: list[str] = []
        self.actions: list[tuple[str, str]] = []
        self.tasks: set[asyncio.Task[None]] = set()

    def handle(self, message: Message) -> Message | None:
        if message.member == "loadScript":
            script_id = len(self.scripts) + 1
            self.scripts[script_id] = Path(message.body[0]).read_text()
            return Message.new_method_return(message, "i", [script_id])
        if message.member == "unloadScript":
            self.unloaded.append(message.body[0])
            return Message.new_method_return(message, "b", [True])
        if message.member == "run":
            prefix = "/Scripting/Script" if self.plasma == 6 else "/"
            if not message.path.startswith(prefix):
                return Message.new_error(message, "org.freedesktop.DBus.Error.UnknownObject", "")
            script = self.scripts[int(message.path.removeprefix(prefix))]
            task = asyncio.create_task(self.execute(script))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return Message.new_method_return(message)
        return None

    async def execute(self, script: str) -> None:
        request = json.loads(re.search(r"var request = (.*);", script).group(1))  # type: ignore[union-attr]
        reply_to = json.loads(re.search(r"var replyTo = (.*);", script).group(1))  # type: ignore[union-attr]
        if request["action"] == "list":
            answer: dict[str, Any] = {"windows": self.windows}
        else:
            found = [w for w in self.windows if w["id"] == request["id"]]
            if not found:
                answer = {"error": "window not found (closed?)"}
            else:
                found[0]["minimized"] = request["action"] == "minimize"
                self.actions.append((request["action"], request["id"]))
                answer = {"window": found[0]}
        await self.bus.call(
            Message(
                destination=reply_to,
                path=windows.RESULT_PATH,
                interface=windows.RESULT_INTERFACE,
                member="Result",
                signature="s",
                body=[json.dumps(answer)],
            )
        )


@pytest.fixture(params=[6, 5], ids=["plasma6", "plasma5"])
async def kwin(
    request: pytest.FixtureRequest,
    session_bus: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FakeKWin]:
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    bus = await MessageBus(bus_address=session_bus).connect()
    await bus.request_name("org.kde.KWin")
    fake = FakeKWin(bus, request.param, [dict(DOLPHIN), dict(FIREFOX)])
    bus.add_message_handler(fake.handle)
    yield fake
    bus.disconnect()
    await bus.wait_for_disconnect()  # type: ignore[no-untyped-call]


async def test_list_and_act(kwin: FakeKWin) -> None:
    found = await windows.list_windows()
    assert [w.title for w in found] == [DOLPHIN["title"], FIREFOX["title"]]
    assert found[0].active and not found[1].active
    after = await windows.act("{f1}", "minimize")
    assert after.minimized and kwin.actions == [("minimize", "{f1}")]
    with pytest.raises(windows.WindowsError, match="window not found"):
        await windows.act("{zz}", "focus")
    with pytest.raises(windows.WindowsError, match="unknown action"):
        await windows.act("{f1}", "close")
    # Every script is unloaded afterwards (3 ran: list, act, act on a missing window).
    assert len(kwin.unloaded) == 3 and all(n.startswith("kreafinity-") for n in kwin.unloaded)


def test_script_template() -> None:
    script = windows.SCRIPT % {
        "request": json.dumps({"action": "left", "id": '{x}"; evil()'}),
        "reply_to": json.dumps(":1.42"),
        "path": windows.RESULT_PATH,
        "interface": windows.RESULT_INTERFACE,
    }
    assert 'var request = {"action": "left", "id": "{x}\\"; evil()"};' in script  # data only
    assert "workspace.windowList" in script and "workspace.clientList" in script  # 6 and 5


async def test_no_kwin(session_bus: str, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    with pytest.raises(windows.WindowsError, match="KWin is not available"):
        await windows.list_windows()


async def test_kwin_does_not_answer(
    session_bus: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows, "_TIMEOUT", 0.2)
    bus = await MessageBus(bus_address=session_bus).connect()
    await bus.request_name("org.kde.KWin")

    def silent(message: Message) -> Message | None:
        body = [1] if message.member == "loadScript" else []
        return Message.new_method_return(message, "i" if body else "", body)

    bus.add_message_handler(silent)
    try:
        with pytest.raises(windows.WindowsError, match="KWin did not answer"):
            await windows.list_windows()
    finally:
        bus.disconnect()


# --- Tools ---------------------------------------------------------------------------------


def tool(name: str) -> Any:
    found = ToolRegistry.load([name]).get(name)
    assert found is not None
    return found


async def test_list_windows_tool(kwin: FakeKWin, context: ToolContext) -> None:
    assert tool("list_windows").risk.value == "safe"
    result = json.loads((await tool("list_windows").run({}, context)).content)
    assert result["windows"][1]["title"] == FIREFOX["title"]


async def test_arrange_window_tool(kwin: FakeKWin, context: ToolContext) -> None:
    arrange = tool("arrange_window")
    assert arrange.risk.value == "confirm"
    preview = await arrange.preview({"window": "firefox", "action": "left"}, context)
    assert preview.summary == (
        "Placer la fenêtre « Kreafinity — Mozilla Firefox » sur la moitié gauche de l'écran"
    )
    assert preview.details == ("Kreafinity — Mozilla Firefox — firefox",)
    result = json.loads(
        (await arrange.run({"window": "{d0}", "action": "minimize"}, context)).content
    )
    assert result == {
        "done": "minimize",
        "window": {"id": "{d0}", "title": DOLPHIN["title"], "minimized": True},
    }
    assert kwin.actions == [("minimize", "{d0}")]


async def test_arrange_window_needs_one_window(kwin: FakeKWin, context: ToolContext) -> None:
    arrange = tool("arrange_window")
    with pytest.raises(ToolError, match=r"No open window matches 'thunderbird'. Open windows:"):
        await arrange.preview({"window": "thunderbird", "action": "focus"}, context)
    kwin.windows.append(dict(FIREFOX_2))
    with pytest.raises(ToolError, match=r"2 windows match 'firefox'.*Give the id"):
        await arrange.preview({"window": "firefox", "action": "focus"}, context)
    preview = await arrange.preview({"window": "météo", "action": "maximize"}, context)
    assert "Météo Réunion" in preview.summary
    assert kwin.actions == []  # previews change nothing


async def test_window_tools_elsewhere(
    context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    with pytest.raises(ToolError, match="KDE Plasma only"):
        await tool("list_windows").run({}, context)
    with pytest.raises(ToolError, match="KDE Plasma only"):
        await tool("arrange_window").preview({"window": "x", "action": "focus"}, context)
