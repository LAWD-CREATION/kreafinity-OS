"""Desktop notifications (freedesktop.org Notifications over D-Bus)."""

from __future__ import annotations

import logging

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError

logger = logging.getLogger(__name__)


async def notify(
    bus: MessageBus, summary: str, body: str = "", *, icon: str = "kreafinity"
) -> bool:
    """Show a notification on the desktop; False if no notification server answers."""
    reply = await bus.call(
        Message(
            destination="org.freedesktop.Notifications",
            path="/org/freedesktop/Notifications",
            interface="org.freedesktop.Notifications",
            member="Notify",
            signature="susssasa{sv}i",
            body=["Kreafinity", 0, icon, summary, body, [], {"urgency": Variant("y", 1)}, -1],
        )
    )
    if reply is None or reply.message_type is MessageType.ERROR:
        logger.warning("Notification not shown: %s", reply.error_name if reply else "no reply")
        return False
    return True


async def send_notification(summary: str, body: str = "") -> bool:
    """Show a notification on the user's desktop (session bus); False if impossible."""
    try:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
    except (OSError, DBusError, ValueError):
        return False
    try:
        return await notify(bus, summary, body)
    except DBusError:
        return False
    finally:
        bus.disconnect()
        await bus.wait_for_disconnect()  # type: ignore[no-untyped-call]
