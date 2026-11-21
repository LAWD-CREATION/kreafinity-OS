"""Open windows on KDE Plasma: list them, bring one forward, minimize, snap… (KWin).

KWin (Plasma's window manager) runs JavaScript scripts loaded over D-Bus
(``org.kde.kwin.Scripting``). Each request writes a small script, loads it,
runs it, and the script sends its answer back to this process with
``callDBus`` (to this connection's unique bus name). Works with Plasma 5
(``workspace.clientList``) and Plasma 6 (``workspace.windowList``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError

ACTIONS = ("focus", "minimize", "restore", "maximize", "left", "right")
RESULT_PATH = "/org/kreafinity/WindowsResult"
RESULT_INTERFACE = "org.kreafinity.WindowsResult"
_TIMEOUT = 5.0

# Runs inside KWin. %(request)s and %(reply_to)s are JSON literals.
SCRIPT = """
var request = %(request)s;
var replyTo = %(reply_to)s;
var plasma6 = typeof workspace.windowList === "function";
function all() { return plasma6 ? workspace.windowList() : workspace.clientList(); }
function active() { return plasma6 ? workspace.activeWindow : workspace.activeClient; }
function activate(w) { if (plasma6) workspace.activeWindow = w; else workspace.activeClient = w; }
function describe(w) {
    return {
        id: String(w.internalId), title: String(w.caption),
        application: String(w.resourceClass), minimized: !!w.minimized,
        active: w === active()
    };
}
function answer(data) {
    callDBus(replyTo, "%(path)s", "%(interface)s", "Result", JSON.stringify(data));
}
var windows = all().filter(function (w) { return w.normalWindow && !w.skipTaskbar; });
if (request.action === "list") {
    answer({ windows: windows.map(describe) });
} else {
    var found = windows.filter(function (w) { return String(w.internalId) === request.id; });
    if (found.length === 0) {
        answer({ error: "window not found (closed?)" });
    } else {
        var w = found[0];
        var area = workspace.clientArea(KWin.MaximizeArea, w);
        if (request.action === "minimize") {
            w.minimized = true;
        } else if (request.action === "maximize") {
            w.minimized = false; w.setMaximize(true, true); activate(w);
        } else {
            w.minimized = false;
            if (request.action === "left" || request.action === "right") {
                w.setMaximize(false, false);
                var half = Math.floor(area.width / 2);
                w.frameGeometry = {
                    x: request.action === "left" ? area.x : area.x + half, y: area.y,
                    width: half, height: area.height
                };
            }
            activate(w);
        }
        answer({ window: describe(w) });
    }
}
"""


class WindowsError(Exception):
    """The windows cannot be listed or changed (no KWin, script failed…)."""


@dataclass(frozen=True, slots=True)
class Window:
    """An open window, as KWin sees it."""

    id: str
    title: str
    application: str
    minimized: bool
    active: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "application": self.application,
            "minimized": self.minimized,
            "active": self.active,
        }


def supported() -> bool:
    """Whether this session is KDE Plasma (its window manager can be scripted)."""
    return "kde" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":")


async def list_windows(bus_address: str | None = None) -> list[Window]:
    """The open windows (those shown in the taskbar)."""
    answer = await _run_script({"action": "list"}, bus_address)
    return [_window(item) for item in answer.get("windows", [])]


async def act(window_id: str, action: str, bus_address: str | None = None) -> Window:
    """Do `action` on the window `window_id`; return it as it is afterwards."""
    if action not in ACTIONS:
        raise WindowsError(f"unknown action {action!r} ({', '.join(ACTIONS)})")
    answer = await _run_script({"action": action, "id": window_id}, bus_address)
    if "error" in answer:
        raise WindowsError(str(answer["error"]))
    return _window(answer["window"])


def _window(item: dict[str, Any]) -> Window:
    return Window(
        id=str(item["id"]),
        title=str(item["title"]),
        application=str(item["application"]),
        minimized=bool(item["minimized"]),
        active=bool(item["active"]),
    )


async def _run_script(request: dict[str, Any], bus_address: str | None) -> dict[str, Any]:
    try:
        if bus_address:
            bus = await MessageBus(bus_address=bus_address).connect()
        else:
            bus = await MessageBus(bus_type=BusType.SESSION).connect()
    except (OSError, DBusError, ValueError) as exc:
        raise WindowsError(f"no session bus: {exc}") from exc
    loop = asyncio.get_running_loop()
    result: asyncio.Future[str] = loop.create_future()

    def receive(message: Message) -> Message | None:
        if message.path == RESULT_PATH and message.member == "Result":
            if not result.done() and message.body:
                result.set_result(str(message.body[0]))
            return Message.new_method_return(message)
        return None

    bus.add_message_handler(receive)
    plugin = f"kreafinity-{uuid.uuid4().hex}"
    script = SCRIPT % {
        "request": json.dumps(request),
        "reply_to": json.dumps(bus.unique_name),
        "path": RESULT_PATH,
        "interface": RESULT_INTERFACE,
    }
    fd, name = tempfile.mkstemp(prefix="kreafinity-", suffix=".js")
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(script)
        script_id = await _call(
            bus, "/Scripting", "org.kde.kwin.Scripting", "loadScript", "ss", [str(path), plugin]
        )
        if not isinstance(script_id, int) or script_id < 0:
            raise WindowsError("KWin refused the script")
        try:
            # Plasma 6 exports scripts under /Scripting, Plasma 5 at the root.
            for script_path in (f"/Scripting/Script{script_id}", f"/{script_id}"):
                with contextlib.suppress(WindowsError):
                    await _call(bus, script_path, "org.kde.kwin.Script", "run", "", [])
                    break
            else:
                raise WindowsError("KWin did not run the script")
            text = await asyncio.wait_for(result, _TIMEOUT)
        except TimeoutError as exc:
            raise WindowsError("KWin did not answer") from exc
        finally:
            with contextlib.suppress(WindowsError):
                await _call(
                    bus, "/Scripting", "org.kde.kwin.Scripting", "unloadScript", "s", [plugin]
                )
    finally:
        path.unlink(missing_ok=True)
        bus.disconnect()
        with contextlib.suppress(Exception):
            await bus.wait_for_disconnect()  # type: ignore[no-untyped-call]
    try:
        answer = json.loads(text)
    except ValueError as exc:
        raise WindowsError("unexpected answer from KWin") from exc
    if not isinstance(answer, dict):
        raise WindowsError("unexpected answer from KWin")
    return answer


async def _call(
    bus: MessageBus, path: str, interface: str, member: str, signature: str, body: list[Any]
) -> Any:
    reply = await bus.call(
        Message(
            destination="org.kde.KWin",
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=body,
        )
    )
    if reply is None or reply.message_type is MessageType.ERROR:
        detail = reply.error_name if reply is not None else "no reply"
        raise WindowsError(f"KWin is not available ({detail})")
    return reply.body[0] if reply.body else None
