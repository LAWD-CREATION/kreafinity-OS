"""Plasma search (KRunner plugin) and the requests to show a conversation in a panel."""

import asyncio
import os
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from dbus_fast import Message, MessageType
from dbus_fast.aio import MessageBus
from fastapi.testclient import TestClient

from kreafinity import service as service_module
from kreafinity.api.client import DaemonUnavailableError
from kreafinity.plasma import krunner
from kreafinity.service import UiRequests
from test_api import AUTH, TOKEN, client, providers  # noqa: F401  (fixtures)
from test_daemon import Daemon, daemon  # noqa: F401  (fixture)

# --- Requests to show a conversation ---------------------------------------------------------


def test_show_reaches_the_panels(client: TestClient) -> None:  # noqa: F811
    conversation = client.post("/api/conversations", headers=AUTH).json()
    body = {"conversation_id": conversation["id"]}
    assert client.post("/api/ui/show", json=body, headers=AUTH).json() == {"delivered": 0}

    with client.websocket_connect("/api/events") as page:  # the full interface: not a panel
        page.send_json({"type": "auth", "token": TOKEN})
        assert page.receive_json()["type"] == "ready"
        assert client.post("/api/ui/show", json=body, headers=AUTH).json() == {"delivered": 0}

    with client.websocket_connect("/api/events?panel=1") as panel:
        panel.send_json({"type": "auth", "token": TOKEN})
        assert panel.receive_json()["type"] == "ready"
        assert client.post("/api/ui/show", json=body, headers=AUTH).json() == {"delivered": 1}
        event = panel.receive_json()
        assert event["type"] == "show_conversation"
        assert event["conversation_id"] == conversation["id"]

    unknown = client.post("/api/ui/show", json={"conversation_id": "nope"}, headers=AUTH)
    assert unknown.status_code == 404
    assert client.post("/api/ui/show", json=body).status_code == 401  # token needed


def test_long_polling_api(client: TestClient) -> None:  # noqa: F811
    start = client.get("/api/ui/requests", headers=AUTH).json()
    assert start == {"requests": [], "last": 0}  # a starting panel gets nothing old
    conversation = client.post("/api/conversations", headers=AUTH).json()
    client.post("/api/ui/show", json={"conversation_id": conversation["id"]}, headers=AUTH)

    result = client.get("/api/ui/requests?after=0&wait=0", headers=AUTH).json()
    assert result["last"] == 1
    assert [(r["type"], r["conversation_id"]) for r in result["requests"]] == [
        ("show_conversation", conversation["id"])
    ]
    assert client.get("/api/ui/requests?after=1&wait=0.1", headers=AUTH).json()["requests"] == []


async def test_waiting_panels_are_counted() -> None:
    requests = UiRequests()
    waiter = asyncio.create_task(requests.wait(0, timeout=5))
    while requests.waiting == 0:
        await asyncio.sleep(0.01)
    await requests.push({"type": "show_conversation", "conversation_id": "c1"})
    found, last = await waiter
    assert last == 1 and found[0]["conversation_id"] == "c1"
    assert requests.waiting == 0


async def test_old_requests_are_not_replayed(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = UiRequests()
    await requests.push({"type": "show_conversation", "conversation_id": "c1"})
    monkeypatch.setattr(service_module, "UI_REQUEST_MAX_AGE", 0.0)
    await asyncio.sleep(0.01)
    assert await requests.wait(0, timeout=0) == ([], 1)


# --- Results shown in the search ------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Comment libérer de l'espace ?", 0.75),
        ("montre mes photos", 0.75),
        ("le volume", 0.4),
        ("firefox", 0.1),
    ],
)
def test_relevance(query: str, expected: float) -> None:
    assert krunner.relevance(query) == expected


def test_matches() -> None:
    assert krunner.matches("ab") == []  # too short
    [match] = krunner.matches("  Quelle heure est-il ?  ")
    match_id, text, icon, kind, relevance, properties = match
    assert match_id == "ask:Quelle heure est-il ?"
    assert text == "« Quelle heure est-il ? »"
    assert properties["subtext"].value == "Demander à Kreafinity, l'assistant IA"
    assert icon == "kreafinity" and kind == krunner.POSSIBLE_MATCH and relevance == 0.75
    assert properties["category"].value == "Kreafinity"
    long_text = krunner.matches("x" * 200)[0][1]
    assert long_text.endswith("… »") and len(long_text) < 120


# --- Over D-Bus ----------------------------------------------------------------------------


@pytest.fixture
def bus_address(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A private D-Bus session bus."""
    if shutil.which("dbus-daemon") is None:
        pytest.skip("dbus-daemon is not installed")
    process = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert process.stdout is not None
    address = process.stdout.readline().strip()
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)
    yield address
    process.terminate()
    process.wait()
    process.stdout.close()


class Desktop:
    """What the runner did on the desktop: notifications and browser openings."""

    def __init__(self, bus: MessageBus, opened: Path) -> None:
        self.bus = bus
        self.opened = opened
        self.notifications: list[list[Any]] = []

    def handle(self, message: Message) -> Message | None:
        if message.member == "Notify":
            self.notifications.append(list(message.body))
            return Message.new_method_return(message, "u", [1])
        return None

    async def call(self, member: str, signature: str = "", body: list[Any] | None = None) -> Any:
        reply = await self.bus.call(
            Message(
                destination=krunner.BUS_NAME,
                path=krunner.OBJECT_PATH,
                interface=krunner.INTERFACE,
                member=member,
                signature=signature,
                body=body or [],
            )
        )
        assert reply is not None
        return reply

    def browser_urls(self) -> list[str]:
        return self.opened.read_text().split() if self.opened.exists() else []


@pytest.fixture
async def desktop(
    bus_address: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Desktop]:
    """The runner serving on the private bus, a notification server and a fake browser."""
    folder = tmp_path / "desktop-bin"
    folder.mkdir()
    opened = tmp_path / "opened.txt"
    opener = folder / "xdg-open"
    opener.write_text(f'#!/bin/sh\necho "$1" >> "{opened}"\n')
    opener.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")

    bus = await MessageBus(bus_address=bus_address).connect()
    await bus.request_name("org.freedesktop.Notifications")
    fake = Desktop(bus, opened)
    bus.add_message_handler(fake.handle)
    server = asyncio.create_task(krunner.serve(bus_address))
    for _ in range(200):
        reply = await bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="NameHasOwner",
                signature="s",
                body=[krunner.BUS_NAME],
            )
        )
        if reply is not None and reply.body == [True]:
            break
        await asyncio.sleep(0.02)
    try:
        yield fake
    finally:
        server.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server
        bus.disconnect()
        await bus.wait_for_disconnect()  # type: ignore[no-untyped-call]


async def test_krunner_interface(desktop: Desktop) -> None:
    reply = await desktop.call("Match", "s", ["Comment libérer de l'espace ?"])
    [[match]] = reply.body
    assert match[0] == "ask:Comment libérer de l'espace ?" and match[4] == 0.75
    assert (await desktop.call("Match", "s", ["ab"])).body == [[]]
    actions = (await desktop.call("Actions")).body[0]
    assert [list(action) for action in actions] == [
        ["full", "Ouvrir dans la fenêtre complète", "window-new"]
    ]
    config = (await desktop.call("Config")).body[0]
    assert config["MinLetterCount"].value == 3
    assert (await desktop.call("Teardown")).message_type is MessageType.METHOD_RETURN
    assert (await desktop.call("Explode")).message_type is MessageType.ERROR


async def test_only_one_runner(bus_address: str, desktop: Desktop) -> None:
    assert await asyncio.wait_for(krunner.serve(bus_address), 10) == 0  # name taken: exits


async def test_idle_exit(bus_address: str) -> None:
    assert await asyncio.wait_for(krunner.serve(bus_address, idle_exit=0.2), 10) == 0


async def wait_for(condition: Any, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.05)


async def test_question_shown_in_a_panel(desktop: Desktop, daemon: Daemon) -> None:  # noqa: F811
    auth = {"Authorization": f"Bearer {daemon.token}"}
    async with httpx.AsyncClient(base_url=daemon.url, headers=auth, timeout=30) as http:
        # A Plasma widget waiting for requests (long polling).
        last = (await http.get("/api/ui/requests")).json()["last"]
        panel = asyncio.create_task(
            http.get("/api/ui/requests", params={"after": last, "wait": 20})
        )
        await asyncio.sleep(0.3)

        await desktop.call("Run", "ss", ["ask:Quelle heure est-il ?", ""])

        requests = (await panel).json()["requests"]
        conversation = await http.get(f"/api/conversations/{requests[0]['conversation_id']}")
        assert conversation.json()["messages"][0]["content"] == "Quelle heure est-il ?"
    assert desktop.browser_urls() == []  # the panel shows it


@pytest.mark.parametrize("action", ["", "full"])
async def test_question_opened_in_the_browser(
    desktop: Desktop,
    daemon: Daemon,  # noqa: F811
    action: str,
) -> None:
    """No panel listening (or « open in the full window »): the browser opens."""
    await desktop.call("Run", "ss", ["ask:Range mon bureau", action])
    await wait_for(desktop.browser_urls)
    [url] = desktop.browser_urls()
    assert url.startswith(f"{daemon.url}/?conversation=")
    conversation_id = url.split("=", 1)[1]
    saved = httpx.get(
        f"{daemon.url}/api/conversations/{conversation_id}",
        headers={"Authorization": f"Bearer {daemon.token}"},
        timeout=5,
    ).json()
    assert saved["messages"][0]["content"] == "Range mon bureau"


async def test_failure_is_notified(desktop: Desktop, monkeypatch: pytest.MonkeyPatch) -> None:
    def down(port: int, **_: Any) -> str:
        raise DaemonUnavailableError("Kreafinity did not start")

    monkeypatch.setattr(krunner, "ensure_daemon", down)
    await desktop.call("Run", "ss", ["ask:Bonjour à toi", ""])
    await wait_for(lambda: desktop.notifications)
    app, _, icon, summary, body, *_ = desktop.notifications[0]
    assert (app, icon) == ("Kreafinity", "kreafinity")
    assert summary == "Kreafinity n'a pas pu recevoir votre question"
    assert body == "Kreafinity did not start"


def test_runner_starts_as_a_program(bus_address: str) -> None:
    """``python -m kreafinity.plasma.krunner`` (what D-Bus activation runs)."""
    process = subprocess.Popen(
        [sys.executable, "-m", "kreafinity.plasma.krunner"],
        env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": bus_address},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            owner = subprocess.run(
                [
                    "dbus-send",
                    f"--bus={bus_address}",
                    "--print-reply",
                    "--dest=org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus.NameHasOwner",
                    f"string:{krunner.BUS_NAME}",
                ],
                capture_output=True,
                text=True,
            )
            if "boolean true" in owner.stdout:
                break
            time.sleep(0.1)
        else:
            pytest.fail("the runner did not take its bus name")
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=10)
        assert process.stderr is not None
        process.stderr.close()
