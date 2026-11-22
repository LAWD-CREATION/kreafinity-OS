"""Pilot mode: keys, input backends, the session's safeguards, the tools and the agent."""

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from fakes import ScriptedProvider, call, reply
from kreafinity import pilot
from kreafinity.agent import Agent, Conversation
from kreafinity.pilot import backends, keys
from kreafinity.pilot import session as session_module
from kreafinity.pilot.backends import InputBackend, InputError, PortalBackend, XdotoolBackend
from kreafinity.pilot.desktop import Probe, WindowInfo
from kreafinity.system import screen
from kreafinity.tools import ToolContext, ToolError, ToolRegistry

# --- Keys ------------------------------------------------------------------------------------


def test_parse_combo() -> None:
    combo = keys.parse_combo("Ctrl+Shift+T")
    assert [k.name for k in combo] == ["ctrl", "shift", "t"]
    assert [k.xdotool for k in combo][:2] == ["ctrl", "shift"]
    assert combo[2].keysym == ord("t")
    assert [k.name for k in keys.parse_combo("alt+F4")] == ["alt", "f4"]
    assert keys.parse_combo("Entrée")[0].xdotool == "Return"
    assert keys.parse_combo("ctrl++")[1].name == "+"
    assert keys.char_keysym("é") == 0xE9 and keys.char_keysym("€") == 0x010020AC
    assert keys.char_keysym("\n") == 0xFF0D
    for forbidden, message in (
        ("Escape", "user's key"),
        ("ctrl+alt+delete", r"Ctrl\+Alt\+Delete"),
        ("ctrl+alt+f2", "text consoles"),
        ("ctrl+foo", "unknown key"),
        ("a+b", "one key at a time"),
        ("", "invalid"),
    ):
        with pytest.raises(keys.ComboError, match=message):
            keys.parse_combo(forbidden)


# --- xdotool (X11) ---------------------------------------------------------------------------


async def test_xdotool_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "calls"
    fake = tmp_path / "xdotool"
    fake.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin{os.pathsep}/bin")
    backend = XdotoolBackend()
    await backend.start((1280, 800))
    await backend.move(10, 20)
    await backend.button("right", True)
    await backend.scroll(-2, 0)
    await backend.type_text("Été")
    await backend.combo(keys.parse_combo("ctrl+s"))
    assert log.read_text().splitlines() == [
        "mousemove 10 20",
        "mousedown 3",
        "click --repeat 2 4",
        "type --clearmodifiers --delay 25 -- Été",
        "key --clearmodifiers ctrl+0x73",
    ]


# --- The RemoteDesktop portal (Wayland), against a fake portal on a private bus -------------


class FakePortal:
    """org.freedesktop.portal.Desktop: RemoteDesktop + ScreenCast, answering by signals."""

    def __init__(self, bus: MessageBus, *, refuse: bool = False) -> None:
        self.bus = bus
        self.refuse = refuse
        self.calls: list[tuple[str, list[Any]]] = []
        bus.add_message_handler(self.handle)

    def handle(self, message: Message) -> Message | None:
        if message.message_type != MessageType.METHOD_CALL or message.destination != (
            "org.freedesktop.portal.Desktop"
        ):
            return None
        self.calls.append((message.member, message.body))
        if message.member.startswith("Notify") or message.member == "Close":
            return Message.new_method_return(message)
        options = message.body[-1]
        token = options["handle_token"].value
        sender = message.sender[1:].replace(".", "_")
        request = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        results: dict[str, Variant] = {}
        code = 0
        if message.member == "CreateSession":
            results = {
                "session_handle": Variant("o", "/org/freedesktop/portal/desktop/session/s/1")
            }
        elif message.member == "Start":
            code = 1 if self.refuse else 0
            results = {
                "devices": Variant("u", 3),
                "restore_token": Variant("s", "token-123"),
                "streams": Variant(
                    "a(ua{sv})",
                    [
                        [
                            41,
                            {
                                "position": Variant("(ii)", [0, 0]),
                                "size": Variant("(ii)", [1280, 800]),
                            },
                        ],
                        [
                            42,
                            {
                                "position": Variant("(ii)", [1280, 0]),
                                "size": Variant("(ii)", [1280, 800]),
                            },
                        ],
                    ],
                ),
            }
        # The answer may come before the method's reply: clients listen first.
        self.bus.send(
            Message.new_signal(
                request, "org.freedesktop.portal.Request", "Response", "ua{sv}", [code, results]
            )
        )
        return Message.new_method_return(message, "o", [request])


@pytest.fixture
def private_bus(tmp_path: Path) -> Iterator[str]:
    if not shutil.which("dbus-daemon"):
        pytest.skip("dbus-daemon is not installed")
    process = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert process.stdout is not None
    address = process.stdout.readline().strip()
    yield address
    process.terminate()
    process.wait()
    process.stdout.close()


@pytest.fixture
async def portal(private_bus: str) -> AsyncIterator[tuple[FakePortal, str]]:
    bus = await MessageBus(bus_address=private_bus).connect()
    await bus.request_name("org.freedesktop.portal.Desktop")
    yield FakePortal(bus), private_bus
    bus.disconnect()


async def test_portal_backend(portal: tuple[FakePortal, str], tmp_path: Path) -> None:
    fake, address = portal
    token = tmp_path / "token"
    token.write_text("old-token")
    backend = PortalBackend(token, bus_address=address)
    await backend.start((5120, 1600))  # two 1280x800 monitors at scale 2
    assert [name for name, _ in fake.calls] == [
        "CreateSession",
        "SelectDevices",
        "SelectSources",
        "Start",
    ]
    devices = fake.calls[1][1][1]
    assert devices["types"].value == 3 and devices["persist_mode"].value == 2
    assert devices["restore_token"].value == "old-token"
    assert token.read_text() == "token-123" and oct(token.stat().st_mode)[-3:] == "600"

    await backend.move(3000, 400)  # on the second monitor, logical (1500, 200)
    motion = fake.calls[-1][1]
    assert fake.calls[-1][0] == "NotifyPointerMotionAbsolute"
    assert motion[2:] == [42, 220.0, 200.0]
    await backend.button("left", True)
    assert fake.calls[-1][1][2:] == [0x110, 1]
    await backend.type_text("Aé")
    sent = [(c[1][2], c[1][3]) for c in fake.calls if c[0] == "NotifyKeyboardKeysym"]
    assert sent == [(0x41, 1), (0x41, 0), (0xE9, 1), (0xE9, 0)]
    await backend.stop()
    assert fake.calls[-1][0] == "Close"


async def test_portal_refused(private_bus: str, tmp_path: Path) -> None:
    bus = await MessageBus(bus_address=private_bus).connect()
    await bus.request_name("org.freedesktop.portal.Desktop")
    FakePortal(bus, refuse=True)
    backend = PortalBackend(tmp_path / "token", bus_address=private_bus)
    with pytest.raises(InputError, match="refused remote control"):
        await backend.start((1280, 800))
    bus.disconnect()


# --- The session's safeguards ----------------------------------------------------------------


class FakeBackend(InputBackend):
    name = "fake"

    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    async def move(self, x: int, y: int) -> None:
        self.events.append(("move", x, y))
        if FakeDesktop.instance is not None:  # the real pointer follows
            FakeDesktop.instance.pointer = (x, y)

    async def button(self, button: str, pressed: bool) -> None:
        self.events.append(("button", button, pressed))

    async def scroll(self, down: int, right: int) -> None:
        self.events.append(("scroll", down, right))

    async def keysym(self, key: keys.Key, pressed: bool) -> None:
        self.events.append(("key", key.name, pressed))

    async def type_text(self, text: str) -> None:
        self.events.append(("type", text))


class FakeDesktop(Probe):
    """KWin stand-in: windows, pointer, and the Escape callback."""

    instance: "FakeDesktop | None" = None

    def __init__(self, desktop_size: tuple[int, int]) -> None:
        self.desktop_size = desktop_size
        self.on_stop: Any = None
        self.on_cursor: Any = None
        self.pointer: tuple[int, int] | None = None
        self.windows: dict[str, WindowInfo] = {}
        self.active = WindowInfo("org.kde.kate", "notes.txt — Kate")
        FakeDesktop.instance = self

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def cursor(self) -> tuple[int, int] | None:
        return self.pointer

    async def window_at(self, x: int, y: int) -> WindowInfo | None:
        return self.windows.get("at", WindowInfo("org.kde.dolphin", "Documents"))

    async def active_window(self) -> WindowInfo | None:
        return self.active


class FakeBanner:
    def __init__(self, on_stop: Any) -> None:
        self.on_stop = on_stop
        self.texts: list[str] = []

    async def show(self, language: str, *, escape: bool, fallback_text: str) -> bool:
        return True

    def status(self, text: str) -> None:
        self.texts.append(text)

    async def close(self) -> None: ...


@pytest.fixture
def desktop(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    backend = FakeBackend()
    shot = screen.Screenshot(data="aW1n", width=1568, height=980, original=(3136, 1960), tool="t")

    def capture() -> screen.Screenshot:
        screen.LAST = shot
        return shot

    monkeypatch.setattr(screen, "capture", capture)
    monkeypatch.setattr(backends, "choose", lambda token: backend)
    monkeypatch.setattr(session_module, "is_kde", lambda: True)
    monkeypatch.setattr(session_module, "KWinDesktop", FakeDesktop)
    monkeypatch.setattr(session_module, "Banner", FakeBanner)
    monkeypatch.setattr(session_module, "SETTLE_SECONDS", 0)
    monkeypatch.setattr(session_module, "_session", None)
    monkeypatch.setattr(screen, "LAST", None)
    return backend


async def test_session_steps(desktop: FakeBackend) -> None:
    session = pilot.get_session()
    with pytest.raises(pilot.PilotStopped, match="start it with pilot_start"):
        await session.act([{"action": "click", "x": 1, "y": 1}])
    await session.start("Régler le son")
    assert session.active and session.escape
    done, _ = await session.act(
        [
            {"action": "click", "x": 100, "y": 50, "double": True},
            {"action": "type", "text": "Bonjour"},
            {"action": "key", "keys": "ctrl+s"},
            {"action": "scroll", "x": 10, "y": 10, "direction": "up", "amount": 2},
        ]
    )
    assert done == ["click", "type", "key", "scroll"]
    # Positions on the 1568-px screenshot are doubled on the 3136-px desktop.
    assert desktop.events[0] == ("move", 200, 100)
    assert desktop.events.count(("button", "left", True)) == 2
    assert ("type", "Bonjour") in desktop.events
    assert ("key", "ctrl", True) in desktop.events and ("scroll", -2, 0) in desktop.events
    assert session.banner is not None and "Écrit « Bonjour »" in session.banner.texts  # type: ignore[attr-defined]
    with pytest.raises(pilot.PilotError, match="outside the screenshot"):
        await session.act([{"action": "click", "x": 2000, "y": 10}])
    await pilot.end_request()
    assert not session.active and session.stopped == "request"


async def test_session_guards(desktop: FakeBackend) -> None:
    session = pilot.get_session()
    await session.start("x")
    fake = FakeDesktop.instance
    assert fake is not None
    fake.active = WindowInfo("org.kde.konsole", "~ : bash — Konsole")
    with pytest.raises(pilot.PilotError, match=r"terminal.*run_command"):
        await session.act([{"action": "type", "text": "rm -rf ~"}])
    with pytest.raises(pilot.PilotError, match="terminal"):
        await session.act([{"action": "key", "keys": "enter"}])
    assert not any(e[0] in ("type", "key") for e in desktop.events)
    fake.windows["at"] = WindowInfo("firefox", "Kreafinity — Mozilla Firefox")
    with pytest.raises(pilot.PilotError, match="Kreafinity window"):
        await session.act([{"action": "click", "x": 10, "y": 10}])
    assert not any(e[0] == "button" for e in desktop.events)
    assert session.active  # a refused step does not end pilot mode
    await session.stop()


async def test_taking_back_control(desktop: FakeBackend) -> None:
    session = pilot.get_session()
    await session.start("x")
    fake = FakeDesktop.instance
    assert fake is not None
    await session.act([{"action": "move", "x": 100, "y": 100}])
    await session.act([{"action": "move", "x": 110, "y": 100}])
    assert fake.pointer == (220, 200)
    fake.pointer = (900, 40)  # the user moved the mouse
    with pytest.raises(pilot.PilotStopped, match="moved the mouse"):
        await session.act([{"action": "click", "x": 10, "y": 10}])
    assert not session.active

    await session.start("again")
    fake = FakeDesktop.instance
    assert fake is not None
    fake.on_stop("escape")  # the KWin shortcut
    await asyncio.sleep(0)
    with pytest.raises(pilot.PilotStopped, match="pressed Escape"):
        await session.act([{"action": "wait", "seconds": 0}])

    await session.start("banner")
    assert session.banner is not None
    session.banner.on_stop("banner")
    with pytest.raises(pilot.PilotStopped, match="Stop on the banner"):
        await session.act([{"action": "wait", "seconds": 0}])


async def test_gnome_wayland_refused(desktop: FakeBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    portal_backend = PortalBackend(Path("/nonexistent"))
    monkeypatch.setattr(backends, "choose", lambda token: portal_backend)
    monkeypatch.setattr(session_module, "is_kde", lambda: False)
    with pytest.raises(pilot.PilotError, match="KDE Plasma"):
        await pilot.get_session().start("x")
    assert not pilot.get_session().active


# --- Tools and agent -------------------------------------------------------------------------


def registry() -> ToolRegistry:
    return ToolRegistry.load(["pilot_start", "pilot_act", "pilot_stop", "look_at_screen"])


async def test_tools(
    desktop: FakeBackend, context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    tools = registry()
    start, act = tools.get("pilot_start"), tools.get("pilot_act")
    look = tools.get("look_at_screen")
    assert start is not None and act is not None and look is not None
    assert start.risk.value == "danger" and act.risk.value == "confirm"
    preview = await start.preview({"reason": "Activer le Wi-Fi"}, context)
    assert preview.summary == "Laisser l'assistant piloter la souris et le clavier"
    assert preview.details[0] == "Activer le Wi-Fi"
    assert any("Échap" in line for line in preview.details)
    assert not act.pre_approved({}) and not look.pre_approved({})
    with pytest.raises(ToolError, match="not running"):
        await act.preview({"steps": []}, context)

    result = await start.run({"reason": "x"}, context)
    assert json.loads(result.content)["pilot"] == "on" and len(result.images) == 1
    assert act.pre_approved({}) and look.pre_approved({})
    assert act.iteration_limit() == pilot.ITERATIONS
    with pytest.raises(ToolError, match="Escape is the user's key"):
        await act.run({"steps": [{"action": "key", "keys": "esc"}]}, context)
    with pytest.raises(ToolError, match="needs: x, y"):
        await act.run({"steps": [{"action": "click"}]}, context)
    result = await act.run({"steps": [{"action": "click", "x": 5, "y": 5}]}, context)
    assert json.loads(result.content)["done"] == ["click"] and result.images

    stop = tools.get("pilot_stop")
    assert stop is not None
    assert json.loads((await stop.run({}, context)).content) == {"pilot": "off", "was_on": True}
    with pytest.raises(ToolError, match="stopped by the assistant"):
        await act.run({"steps": [{"action": "wait"}]}, context)


async def test_agent_pilot_flow(desktop: FakeBackend, context: ToolContext) -> None:
    provider = ScriptedProvider(
        reply("", [call("pilot_start", "c1", reason="Ouvrir les réglages")]),
        reply("", [call("pilot_act", "c2", steps=[{"action": "click", "x": 10, "y": 10}])]),
        reply("", [call("look_at_screen", "c3", reason="Voir le résultat")]),
        reply("C'est fait."),
    )
    asked: list[str] = []

    async def approve(request: Any) -> bool:
        asked.append(request.tool)
        return True

    agent = Agent(provider, registry(), context, approver=approve, max_iterations=3)
    events = [event async for event in agent.run(Conversation(), "Ouvre les réglages")]
    # Only starting pilot mode is asked; its steps and screenshots are covered by it.
    assert asked == ["pilot_start"]
    assert type(events[-1]).__name__ == "AgentDone" and events[-1].reason == "completed"
    # 4 model turns with max_iterations=3: pilot mode raised the limit.
    assert len(provider.requests) == 4
    assert not pilot.get_session().active  # ended with the request
