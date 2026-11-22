"""Moving the mouse and pressing keys: xdotool (X11) or the RemoteDesktop portal (Wayland).

Coordinates are in pixels of the whole desktop, as on a full screenshot.

- **X11**: ``xdotool`` sends the events through the XTEST extension.
- **Wayland**: applications may not send input to others. The XDG desktop
  portal ``org.freedesktop.portal.RemoteDesktop`` lets them, after the user
  allowed it in a dialog shown by the system (KDE Plasma, GNOME). The
  permission can be remembered (restore token) so the dialog is not shown
  every time; the user can revoke it in the system settings.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError

from kreafinity.pilot.keys import Key, char_keysym

logger = logging.getLogger(__name__)

BUTTONS = {"left": (1, 0x110), "middle": (2, 0x112), "right": (3, 0x111)}  # X, evdev
_TIMEOUT = 15.0
DIALOG_TIMEOUT = 120.0  # the user reads and answers the system's dialog


class InputError(Exception):
    """The mouse or keyboard cannot be driven (no tool, refused, closed…)."""


class InputBackend(ABC):
    """Sends mouse and keyboard events to the desktop."""

    name: str

    async def start(self, desktop_size: tuple[int, int]) -> None:  # noqa: B027
        """Get ready (ask the system's permission if needed)."""

    async def stop(self) -> None:  # noqa: B027
        """Release the devices."""

    def resize(self, desktop_size: tuple[int, int]) -> None:  # noqa: B027
        """The latest screenshot's size (the desktop in pixels)."""

    @abstractmethod
    async def move(self, x: int, y: int) -> None: ...

    @abstractmethod
    async def button(self, button: str, pressed: bool) -> None: ...

    @abstractmethod
    async def scroll(self, down: int, right: int) -> None:
        """Wheel steps (negative: up or left)."""

    @abstractmethod
    async def keysym(self, key: Key, pressed: bool) -> None: ...

    @abstractmethod
    async def type_text(self, text: str) -> None: ...

    async def combo(self, keys: Sequence[Key]) -> None:
        """Press the keys in order, release them in reverse order."""
        for key in keys:
            await self.keysym(key, True)
        for key in reversed(keys):
            await self.keysym(key, False)


async def _run(*command: str, timeout: float = _TIMEOUT) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise InputError(f"cannot run {command[0]}: {exc}") from exc
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        raise InputError(f"{command[0]} did not answer") from exc
    if process.returncode != 0:
        detail = err.decode(errors="replace").strip().splitlines()
        raise InputError(f"{command[0]}: {detail[-1] if detail else process.returncode}")
    return out.decode(errors="replace")


class XdotoolBackend(InputBackend):
    """X11, with xdotool (XTEST)."""

    name = "xdotool"

    async def start(self, desktop_size: tuple[int, int]) -> None:
        if not shutil.which("xdotool"):
            raise InputError("xdotool is not installed (sudo apt install xdotool)")

    async def move(self, x: int, y: int) -> None:
        # Not --sync: it waits forever when the pointer is already there.
        await _run("xdotool", "mousemove", str(x), str(y))
        await asyncio.sleep(0.03)

    async def button(self, button: str, pressed: bool) -> None:
        await _run("xdotool", "mousedown" if pressed else "mouseup", str(BUTTONS[button][0]))

    async def scroll(self, down: int, right: int) -> None:
        for steps, positive, negative in ((down, 5, 4), (right, 7, 6)):
            if steps:
                button = positive if steps > 0 else negative
                await _run("xdotool", "click", "--repeat", str(abs(steps)), str(button))

    async def keysym(self, key: Key, pressed: bool) -> None:
        await _run("xdotool", "keydown" if pressed else "keyup", key.xdotool)

    async def combo(self, keys: Sequence[Key]) -> None:
        await _run("xdotool", "key", "--clearmodifiers", "+".join(k.xdotool for k in keys))

    async def type_text(self, text: str) -> None:
        await _run(
            "xdotool",
            "type",
            "--clearmodifiers",
            "--delay",
            "25",
            "--",
            text,
            timeout=_TIMEOUT + len(text) * 0.05,
        )


# --- Wayland: the RemoteDesktop portal ---------------------------------------------------

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REMOTE = "org.freedesktop.portal.RemoteDesktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"


@dataclass(frozen=True, slots=True)
class Stream:
    """One monitor of the portal session, in the compositor's logical coordinates."""

    node: int
    x: int
    y: int
    width: int
    height: int


class PortalBackend(InputBackend):
    """Wayland (KDE Plasma, GNOME…), through the XDG RemoteDesktop portal."""

    name = "portal"

    def __init__(self, token_file: Path, bus_address: str | None = None) -> None:
        self.token_file = token_file
        self.bus_address = bus_address
        self.bus: MessageBus | None = None
        self.session = ""
        self.streams: list[Stream] = []
        self.desktop_size = (0, 0)

    async def start(self, desktop_size: tuple[int, int]) -> None:
        self.desktop_size = desktop_size
        try:
            if self.bus_address:
                self.bus = await MessageBus(bus_address=self.bus_address).connect()
            else:
                self.bus = await MessageBus(bus_type=BusType.SESSION).connect()
        except (OSError, DBusError, ValueError) as exc:
            raise InputError(f"no session bus: {exc}") from exc
        try:
            await self._open()
        except BaseException:
            await self.stop()
            raise

    async def _open(self) -> None:
        created = await self._request(
            REMOTE, "CreateSession", "a{sv}", [], {"session_handle_token": Variant("s", _token())}
        )
        self.session = str(created["session_handle"].value)
        options: dict[str, Variant] = {
            "types": Variant("u", 3),  # keyboard | pointer
            "persist_mode": Variant("u", 2),  # remembered until revoked
        }
        with contextlib.suppress(OSError):
            saved = self.token_file.read_text().strip()
            if saved:
                options["restore_token"] = Variant("s", saved)
        await self._request(REMOTE, "SelectDevices", "oa{sv}", [self.session], options)
        # A screen-cast source gives the monitors' positions, needed to place
        # the pointer at absolute coordinates (the stream is never read).
        await self._request(
            SCREENCAST,
            "SelectSources",
            "oa{sv}",
            [self.session],
            {"types": Variant("u", 1), "multiple": Variant("b", True)},
        )
        started = await self._request(
            REMOTE, "Start", "osa{sv}", [self.session, ""], {}, timeout=DIALOG_TIMEOUT
        )
        devices = int(started.get("devices", Variant("u", 0)).value)
        if devices & 3 != 3:
            raise InputError("the system did not allow both the keyboard and the mouse")
        streams = started.get("streams")
        self.streams = [_stream(item) for item in (streams.value if streams else [])]
        if not self.streams:
            raise InputError("the system gave no screen to place the pointer on")
        if len(self.streams) == 1 and not self.streams[0].width:  # no size given: the desktop
            self.streams = [Stream(self.streams[0].node, 0, 0, *self.desktop_size)]
        token = started.get("restore_token")
        if token is not None and token.value:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            self.token_file.write_text(str(token.value))
            self.token_file.chmod(0o600)

    async def stop(self) -> None:
        if self.bus is None:
            return
        if self.session:
            with contextlib.suppress(Exception):
                await self._call(self.session, "org.freedesktop.portal.Session", "Close", "", [])
        self.session = ""
        self.bus.disconnect()
        with contextlib.suppress(Exception):
            await self.bus.wait_for_disconnect()  # type: ignore[no-untyped-call]
        self.bus = None

    def resize(self, desktop_size: tuple[int, int]) -> None:
        self.desktop_size = desktop_size

    def locate(self, x: int, y: int) -> tuple[Stream, float, float]:
        """Desktop pixels -> (monitor, position on it in logical units)."""
        left = min(s.x for s in self.streams)
        top = min(s.y for s in self.streams)
        width = max(1, max(s.x + s.width for s in self.streams) - left)
        height = max(1, max(s.y + s.height for s in self.streams) - top)
        scale_x = self.desktop_size[0] / width if self.desktop_size[0] else 1.0
        scale_y = self.desktop_size[1] / height if self.desktop_size[1] else 1.0
        lx, ly = left + x / scale_x, top + y / scale_y
        for stream in self.streams:
            if (
                stream.x <= lx < stream.x + stream.width
                and stream.y <= ly < stream.y + stream.height
            ):
                return stream, lx - stream.x, ly - stream.y
        nearest = min(
            self.streams,
            key=lambda s: abs(lx - (s.x + s.width / 2)) + abs(ly - (s.y + s.height / 2)),
        )
        return (
            nearest,
            min(max(lx - nearest.x, 0), nearest.width - 1),
            min(max(ly - nearest.y, 0), nearest.height - 1),
        )

    async def move(self, x: int, y: int) -> None:
        stream, sx, sy = self.locate(x, y)
        await self._notify("NotifyPointerMotionAbsolute", "oa{sv}udd", [stream.node, sx, sy])

    async def button(self, button: str, pressed: bool) -> None:
        await self._notify("NotifyPointerButton", "oa{sv}iu", [BUTTONS[button][1], int(pressed)])

    async def scroll(self, down: int, right: int) -> None:
        for axis, steps in ((0, down), (1, right)):
            if steps:
                await self._notify("NotifyPointerAxisDiscrete", "oa{sv}ui", [axis, steps])

    async def keysym(self, key: Key, pressed: bool) -> None:
        await self._notify("NotifyKeyboardKeysym", "oa{sv}iu", [key.keysym, int(pressed)])

    async def type_text(self, text: str) -> None:
        for char in text:
            symbol = char_keysym(char)
            for state in (1, 0):
                await self._notify("NotifyKeyboardKeysym", "oa{sv}iu", [symbol, state])
            await asyncio.sleep(0.015)

    async def _notify(self, method: str, signature: str, arguments: list[Any]) -> None:
        if not self.session:
            raise InputError("the remote control session is closed")
        await self._call(PORTAL_PATH, REMOTE, method, signature, [self.session, {}, *arguments])

    async def _call(
        self, path: str, interface: str, method: str, signature: str, body: list[Any]
    ) -> Any:
        assert self.bus is not None
        try:
            reply = await asyncio.wait_for(
                self.bus.call(
                    Message(
                        destination=PORTAL,
                        path=path,
                        interface=interface,
                        member=method,
                        signature=signature,
                        body=body,
                    )
                ),
                _TIMEOUT,
            )
        except (TimeoutError, DBusError, OSError) as exc:
            raise InputError(f"portal {method}: {exc}") from exc
        if reply is None or reply.message_type == MessageType.ERROR:
            detail = reply.body[0] if reply is not None and reply.body else "error"
            raise InputError(f"portal {method}: {reply.error_name if reply else ''} {detail}")
        return reply.body[0] if reply.body else None

    async def _request(
        self,
        interface: str,
        method: str,
        signature: str,
        arguments: list[Any],
        options: dict[str, Variant],
        *,
        timeout: float = _TIMEOUT,
    ) -> dict[str, Variant]:
        """Call a portal method answering later with a Request.Response signal."""
        assert self.bus is not None
        token = _token()
        sender = (self.bus.unique_name or "")[1:].replace(".", "_")
        path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        loop = asyncio.get_running_loop()
        answer: asyncio.Future[tuple[int, dict[str, Variant]]] = loop.create_future()

        def receive(message: Message) -> None:
            if (
                message.message_type == MessageType.SIGNAL
                and message.path == path
                and message.member == "Response"
                and not answer.done()
            ):
                answer.set_result((int(message.body[0]), dict(message.body[1])))

        rule = (
            "type='signal',interface='org.freedesktop.portal.Request',"
            f"member='Response',path='{path}'"
        )
        await self.bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[rule],
            )
        )
        self.bus.add_message_handler(receive)
        try:
            await self._call(
                PORTAL_PATH,
                interface,
                method,
                signature,
                [*arguments, {"handle_token": Variant("s", token), **options}],
            )
            try:
                code, results = await asyncio.wait_for(answer, timeout)
            except TimeoutError as exc:
                raise InputError(
                    f"no answer to {method} (was the system's dialog closed?)"
                ) from exc
        finally:
            self.bus.remove_message_handler(receive)
        if code == 1:
            raise InputError("the user refused remote control in the system's dialog")
        if code != 0:
            raise InputError(f"the system refused {method}")
        return results


def _token() -> str:
    return "kreafinity_" + secrets.token_hex(6)


def _stream(item: Any) -> Stream:
    node, properties = item[0], item[1]
    x, y = properties.get("position", Variant("(ii)", [0, 0])).value
    width, height = properties.get("size", Variant("(ii)", [0, 0])).value
    return Stream(int(node), int(x), int(y), int(width), int(height))


def choose(token_file: Path) -> InputBackend:
    """The backend for this session (Wayland: portal; X11: xdotool)."""
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
        return PortalBackend(token_file)
    if os.environ.get("DISPLAY"):
        return XdotoolBackend()
    raise InputError("no graphical session (DISPLAY / WAYLAND_DISPLAY)")
