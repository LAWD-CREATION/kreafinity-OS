"""Desktop notifications (freedesktop.org Notifications over D-Bus)."""

from __future__ import annotations

import logging

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

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
