"""What the pilot must know about the desktop: the pointer, the windows, the Escape key.

- Where the pointer is, to notice when the user takes back control.
- Which window is under a click, or active when typing: the pilot never
  types in a terminal (commands go through run_command, shown for approval)
  and never acts on Kreafinity's own windows (it could approve itself).
- On KDE Plasma, a KWin script stays loaded during pilot mode: it gives
  Escape to the user (a global shortcut stopping the pilot) and reports
  pointer moves.

Positions are exchanged with KWin as fractions of the desktop, so the
compositor's scaling (HiDPI) does not matter.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dbus_fast import BusType, Message
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError

from kreafinity.pilot.backends import InputError, _run

HELPER_PLUGIN = "kreafinity-pilot"
CALLBACK_PATH = "/org/kreafinity/Pilot"
CALLBACK_INTERFACE = "org.kreafinity.Pilot"
_TIMEOUT = 5.0

#: Window classes of terminal emulators (lower case, substrings).
TERMINALS = (
    "konsole", "yakuake", "terminal", "xterm", "uxterm", "rxvt", "kitty", "alacritty",
    "tilix", "terminator", "wezterm", "foot", "guake", "tilda", "st-256color", "kgx",
    "ptyxis", "cool-retro-term", "blackbox", "qterminal", "lxterminal", "sakura",
    "terminology", "ghostty", "contour", "warp",
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class WindowInfo:
    application: str
    title: str

    @property
    def is_terminal(self) -> bool:
        app = self.application.lower()
        return any(name in app for name in TERMINALS)

    @property
    def is_kreafinity(self) -> bool:
        return "kreafinity" in self.application.lower() or "kreafinity" in self.title.lower()


class Probe:
    """Answers questions about the desktop (positions in desktop pixels)."""

    async def cursor(self) -> tuple[int, int] | None:
        return None

    async def window_at(self, x: int, y: int) -> WindowInfo | None:
        return None

    async def active_window(self) -> WindowInfo | None:
        return None


class XdotoolProbe(Probe):
    """X11: asks the X server through xdotool."""

    async def cursor(self) -> tuple[int, int] | None:
        values = _shell(await _run("xdotool", "getmouselocation", "--shell"))
        return int(values["X"]), int(values["Y"])

    async def window_at(self, x: int, y: int) -> WindowInfo | None:
        # The pilot moves the pointer there first (harmless), then asks.
        values = _shell(await _run("xdotool", "getmouselocation", "--shell"))
        return await self._describe(values.get("WINDOW", ""))

    async def active_window(self) -> WindowInfo | None:
        with contextlib.suppress(InputError):
            return await self._describe((await _run("xdotool", "getactivewindow")).strip())
        return None

    @staticmethod
    async def _describe(window: str) -> WindowInfo | None:
        if not window.isdigit():
            return None
        application = title = ""
        with contextlib.suppress(InputError):
            application = (await _run("xdotool", "getwindowclassname", window)).strip()
        with contextlib.suppress(InputError):
            title = (await _run("xdotool", "getwindowname", window)).strip()
        return WindowInfo(application, title)


def _shell(output: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


# --- KDE Plasma (KWin scripts) -------------------------------------------------------------

_COMMON = """
var replyTo = %(reply_to)s;
var plasma6 = typeof workspace.windowList === "function";
function screen() {
    var s = workspace.virtualScreenSize;
    return { width: Math.max(1, s.width), height: Math.max(1, s.height) };
}
function describe(w) {
    return w ? { application: String(w.resourceClass), title: String(w.caption) } : null;
}
function send(member, data) {
    callDBus(replyTo, "%(path)s", "%(interface)s", member, JSON.stringify(data));
}
"""

QUERY = (
    _COMMON
    + """
var request = %(request)s;
var size = screen();
var answer = {
    cursor: { x: workspace.cursorPos.x / size.width, y: workspace.cursorPos.y / size.height },
    active: describe(plasma6 ? workspace.activeWindow : workspace.activeClient)
};
if (request.at) {
    var px = request.at.x * size.width, py = request.at.y * size.height;
    var order = workspace.stackingOrder;
    if (!order) {  // Plasma 5: the windows' own stacking numbers, the active one on top
        var activeOne = plasma6 ? workspace.activeWindow : workspace.activeClient;
        order = (plasma6 ? workspace.windowList() : workspace.clientList()).slice();
        order.sort(function (a, b) {
            if (a === activeOne) return 1;
            if (b === activeOne) return -1;
            return (a.stackingOrder || 0) - (b.stackingOrder || 0);
        });
    }
    answer.at = null;
    for (var i = order.length - 1; i >= 0; i--) {
        var w = order[i], g = w.frameGeometry;
        var here = !w.minimized && px >= g.x && px < g.x + g.width && py >= g.y && py < g.y + g.height;
        var shown = true;
        try { shown = plasma6 ? w.isOnCurrentDesktop !== false : w.isOnCurrentDesktop(); } catch (e) {}
        if (here && shown && !w.deleted) { answer.at = describe(w); break; }
    }
}
send("Result", answer);
"""
)

HELPER = (
    _COMMON
    + """
registerShortcut("KreafinityPilotStop", "Kreafinity : arrêter le mode pilote", "Escape",
    function () { send("Stop", { key: "Escape" }); });
var last = 0;
function moved() {
    var now = Date.now();
    if (now - last < 120) return;
    last = now;
    var size = screen();
    send("Cursor", { x: workspace.cursorPos.x / size.width, y: workspace.cursorPos.y / size.height });
}
try { workspace.cursorPosChanged.connect(moved); } catch (e) {}
send("Ready", {});
"""
)


async def _connect(bus_address: str | None) -> MessageBus:
    try:
        if bus_address:
            return await MessageBus(bus_address=bus_address).connect()
        return await MessageBus(bus_type=BusType.SESSION).connect()
    except (OSError, DBusError, ValueError) as exc:
        raise InputError(f"no session bus: {exc}") from exc


async def _call(bus: MessageBus, path: str, member: str, signature: str, body: list[Any]) -> Any:
    reply = await asyncio.wait_for(
        bus.call(
            Message(
                destination="org.kde.KWin",
                path=path,
                interface="org.kde.kwin.Script" if member == "run" else "org.kde.kwin.Scripting",
                member=member,
                signature=signature,
                body=body,
            )
        ),
        _TIMEOUT,
    )
    if reply is None or reply.error_name:
        raise InputError(f"KWin {member}: {reply.body if reply else 'no answer'}")
    return reply.body[0] if reply.body else None


async def _load(bus: MessageBus, source: str, plugin: str) -> Path:
    """Load and run a KWin script; return its file (delete it after unloading)."""
    fd, name = tempfile.mkstemp(prefix="kreafinity-pilot-", suffix=".js")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(source)
    path = Path(name)
    try:
        script_id = await _call(bus, "/Scripting", "loadScript", "ss", [str(path), plugin])
        if not isinstance(script_id, int) or script_id < 0:
            raise InputError("KWin refused the script")
        for script_path in (f"/Scripting/Script{script_id}", f"/{script_id}"):
            with contextlib.suppress(InputError, DBusError):
                await _call(bus, script_path, "run", "", [])
                return path
        raise InputError("KWin did not run the script")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def _unload(bus: MessageBus, plugin: str) -> None:
    with contextlib.suppress(Exception):
        await _call(bus, "/Scripting", "unloadScript", "s", [plugin])


def _script(template: str, reply_to: str, **values: Any) -> str:
    return template % {
        "reply_to": json.dumps(reply_to),
        "path": CALLBACK_PATH,
        "interface": CALLBACK_INTERFACE,
        **{key: json.dumps(value) for key, value in values.items()},
    }


class KWinDesktop(Probe):
    """KDE Plasma (X11 or Wayland): KWin answers, and gives Escape to the user."""

    def __init__(self, desktop_size: tuple[int, int], bus_address: str | None = None) -> None:
        self.desktop_size = desktop_size
        self.bus_address = bus_address
        self.bus: MessageBus | None = None
        self._results: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._ready: asyncio.Event = asyncio.Event()
        self._script_file: Path | None = None
        self.on_stop: Callable[[str], None] | None = None
        self.on_cursor: Callable[[int, int], None] | None = None

    async def start(self) -> None:
        """Load the helper script (Escape shortcut, pointer moves)."""
        self.bus = await _connect(self.bus_address)
        self.bus.add_message_handler(self._receive)
        await _unload(self.bus, HELPER_PLUGIN)  # left by a crashed Kreafinity
        self._script_file = await _load(
            self.bus, _script(HELPER, self.bus.unique_name or ""), HELPER_PLUGIN
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), _TIMEOUT)

    async def stop(self) -> None:
        if self.bus is None:
            return
        await _unload(self.bus, HELPER_PLUGIN)  # releases Escape
        if self._script_file:
            self._script_file.unlink(missing_ok=True)
        self.bus.disconnect()
        with contextlib.suppress(Exception):
            await self.bus.wait_for_disconnect()  # type: ignore[no-untyped-call]
        self.bus = None

    def _receive(self, message: Message) -> Message | None:
        if message.path != CALLBACK_PATH or not message.body:
            return None
        try:
            data = json.loads(str(message.body[0]))
        except ValueError:
            data = {}
        if message.member == "Result":
            self._results.put_nowait(data)
        elif message.member == "Ready":
            self._ready.set()
        elif message.member == "Stop" and self.on_stop:
            self.on_stop("escape")
        elif message.member == "Cursor" and self.on_cursor and "x" in data:
            self.on_cursor(*self._pixels(data))
        return Message.new_method_return(message)

    def _pixels(self, point: dict[str, Any]) -> tuple[int, int]:
        return (
            round(float(point["x"]) * self.desktop_size[0]),
            round(float(point["y"]) * self.desktop_size[1]),
        )

    async def _query(self, at: tuple[int, int] | None = None) -> dict[str, Any]:
        if self.bus is None:
            raise InputError("KWin helper not started")
        request: dict[str, Any] = {}
        if at is not None:
            request["at"] = {
                "x": at[0] / max(1, self.desktop_size[0]),
                "y": at[1] / max(1, self.desktop_size[1]),
            }
        plugin = f"kreafinity-query-{uuid.uuid4().hex}"
        while not self._results.empty():
            self._results.get_nowait()
        path = await _load(
            self.bus, _script(QUERY, self.bus.unique_name or "", request=request), plugin
        )
        try:
            return await asyncio.wait_for(self._results.get(), _TIMEOUT)
        except TimeoutError as exc:
            raise InputError("KWin did not answer") from exc
        finally:
            await _unload(self.bus, plugin)
            path.unlink(missing_ok=True)

    async def cursor(self) -> tuple[int, int] | None:
        return self._pixels((await self._query())["cursor"])

    async def window_at(self, x: int, y: int) -> WindowInfo | None:
        return _window((await self._query((x, y))).get("at"))

    async def active_window(self) -> WindowInfo | None:
        return _window((await self._query()).get("active"))


def _window(data: Any) -> WindowInfo | None:
    if not isinstance(data, dict):
        return None
    return WindowInfo(str(data.get("application", "")), str(data.get("title", "")))


def is_kde() -> bool:
    return "kde" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":")


async def release_leftovers(bus_address: str | None = None) -> None:
    """Unload a helper left by a Kreafinity that stopped during pilot mode (frees Escape)."""
    if not is_kde():
        return
    try:
        bus = await _connect(bus_address)
    except InputError:
        return
    try:
        await _unload(bus, HELPER_PLUGIN)
    finally:
        bus.disconnect()
