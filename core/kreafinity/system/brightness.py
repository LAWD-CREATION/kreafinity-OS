"""Screen brightness (laptop backlight), changed through systemd-logind.

The kernel exposes the backlight in ``/sys/class/backlight``, writable by root
only; logind lets the user of the graphical session change it over D-Bus,
without any password. External monitors have no backlight there: they are not
supported.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError

#: Where the kernel lists backlight devices (replaced in tests).
BACKLIGHT_DIR = Path("/sys/class/backlight")
LOGIND = "org.freedesktop.login1"
#: The caller's own session; does not exist for a systemd user service.
AUTO_SESSION = "/org/freedesktop/login1/session/auto"
# Kernel advice: prefer firmware interfaces, then platform ones, then raw.
_TYPE_PREFERENCE = {"firmware": 0, "platform": 1, "raw": 2}


class BrightnessError(Exception):
    """The brightness cannot be read or changed."""


@dataclass(frozen=True, slots=True)
class Backlight:
    """A backlight device and its current level.

    Attributes:
        name: Device name (``intel_backlight``, ``amdgpu_bl0``…).
        brightness: Current raw level.
        max_brightness: Highest raw level.
    """

    name: str
    brightness: int
    max_brightness: int

    @property
    def percent(self) -> int:
        """Current level, from 0 to 100."""
        return round(self.brightness * 100 / self.max_brightness)

    def raw_level(self, percent: int) -> int:
        """Raw level for `percent` (never 0: some screens turn off completely)."""
        return max(1, round(percent * self.max_brightness / 100))


def find_backlight() -> Backlight:
    """The screen's backlight device.

    Raises:
        BrightnessError: This computer has no adjustable backlight.
    """
    candidates = []
    if BACKLIGHT_DIR.is_dir():
        for device in sorted(BACKLIGHT_DIR.iterdir()):
            try:
                kind = (device / "type").read_text(encoding="ascii").strip()
                maximum = int((device / "max_brightness").read_text(encoding="ascii"))
                current_file = device / "actual_brightness"
                if not current_file.exists():
                    current_file = device / "brightness"
                current = int(current_file.read_text(encoding="ascii"))
            except (OSError, ValueError):
                continue
            if maximum > 0:
                candidates.append((_TYPE_PREFERENCE.get(kind, 3), device.name, current, maximum))
    if not candidates:
        raise BrightnessError(
            "this computer has no adjustable screen backlight (external monitors are not supported)"
        )
    _, name, current, maximum = min(candidates)
    return Backlight(name=name, brightness=min(current, maximum), max_brightness=maximum)


async def set_brightness(backlight: Backlight, raw_level: int) -> None:
    """Set the raw level of `backlight`, through systemd-logind.

    Raises:
        BrightnessError: logind cannot be reached or refused the change.
    """
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except (OSError, DBusError) as exc:
        raise BrightnessError(f"cannot reach systemd-logind on the system bus: {exc}") from exc
    try:
        session = await _graphical_session(bus)
        await _call(
            bus,
            session,
            "org.freedesktop.login1.Session",
            "SetBrightness",
            "ssu",
            ["backlight", backlight.name, raw_level],
        )
    finally:
        bus.disconnect()
        await bus.wait_for_disconnect()  # type: ignore[no-untyped-call]  # untyped in dbus-fast


async def _graphical_session(bus: MessageBus) -> str:
    """Object path of the user's graphical session.

    The daemon runs as a systemd user service, outside any session: ask
    logind for the session displayed by this user instead.
    """
    try:
        body = await _call(
            bus,
            "/org/freedesktop/login1/user/self",
            "org.freedesktop.DBus.Properties",
            "Get",
            "ss",
            ["org.freedesktop.login1.User", "Display"],
        )
    except BrightnessError:
        return AUTO_SESSION
    # Display is a (session id, object path) pair; "/" when there is none.
    display = body[0].value if body and isinstance(body[0], Variant) else None
    if not isinstance(display, list | tuple) or len(display) != 2 or display[1] == "/":
        return AUTO_SESSION
    return str(display[1])


async def _call(
    bus: MessageBus, path: str, interface: str, member: str, signature: str, body: list[object]
) -> list[object]:
    reply = await bus.call(
        Message(
            destination=LOGIND,
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=body,
        )
    )
    if reply is None or reply.message_type is MessageType.ERROR:
        detail = reply.body[0] if reply is not None and reply.body else ""
        name = reply.error_name if reply is not None else "no reply"
        raise BrightnessError(f"{name}: {detail}".rstrip(": "))
    return list(reply.body)
