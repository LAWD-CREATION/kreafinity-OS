"""KRunner plugin: « Demander à Kreafinity » in the Plasma search.

Plasma's search (the start menu's search field, and Alt+Space) asks this
program over D-Bus (``org.kde.krunner1`` interface) for results as the user
types; choosing ours sends the question to the assistant and shows the answer
in the assistant panel (taskbar widget or bubble), or else in the browser.

D-Bus starts the program when Plasma first needs it (activation file installed
by ``kreafinity desktop install``); it then stays for the whole session: KRunner
drops the results of a runner it has to start in the middle of a search.

Run with ``python -m kreafinity.plasma.krunner``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import Any

from dbus_fast import BusType, Message, MessageType, NameFlag, RequestNameReply, Variant
from dbus_fast.aio import MessageBus

from kreafinity.api.client import DaemonClient, DaemonUnavailableError, ensure_daemon
from kreafinity.config import load_settings
from kreafinity.i18n import set_language, t
from kreafinity.system import desktop
from kreafinity.system.notifications import notify

logger = logging.getLogger(__name__)

BUS_NAME = "org.kreafinity.KRunner"
OBJECT_PATH = "/runner"
INTERFACE = "org.kde.krunner1"
#: Shorter queries get no result (Plasma also reads this from the plugin file).
MIN_LETTERS = 3
MATCH_PREFIX = "ask:"
ACTION_FULL = "full"
# KRunner match types (QueryMatch::Type).
POSSIBLE_MATCH = 30
#: First words of a question or a request (French, then English).
_QUESTION_WORDS = frozenset(
    {
        "comment",
        "pourquoi",
        "quel",
        "quelle",
        "quels",
        "quelles",
        "combien",
        "où",
        "quand",
        "qui",
        "que",
        "quoi",
        "est-ce",
        "peux",
        "peut",
        "montre",
        "trouve",
        "ouvre",
        "ferme",
        "règle",
        "mets",
        "installe",
        "supprime",
        "cherche",
        "explique",
        "how",
        "why",
        "what",
        "which",
        "where",
        "when",
        "who",
        "can",
        "could",
        "show",
        "find",
        "open",
        "close",
        "set",
        "install",
    }
)


def relevance(query: str) -> float:
    """How much `query` looks like a question for the assistant (0 to 1).

    A sentence or a question ranks high; a single word (an application name,
    usually) stays below the other results.
    """
    words = query.split()
    if not words:
        return 0.0
    first = re.sub(r"\W", "", words[0].casefold())
    if query.rstrip().endswith("?") or len(words) >= 3 or first in _QUESTION_WORDS:
        return 0.75
    return 0.4 if len(words) == 2 else 0.1


def matches(query: str) -> list[list[Any]]:
    """Results for `query`, in the ``a(sssida{sv})`` form of ``Match``."""
    query = query.strip()
    if len(query) < MIN_LETTERS:
        return []
    shown = query if len(query) <= 80 else query[:79] + "…"
    properties = {
        "subtext": Variant("s", t("plasma.search.subtext")),
        "category": Variant("s", "Kreafinity"),
    }
    # The question itself, in full (Plasma shortens long texts from the start).
    return [
        [
            MATCH_PREFIX + query,
            t("plasma.search.ask", query=shown),
            "kreafinity",
            POSSIBLE_MATCH,
            relevance(query),
            properties,
        ]
    ]


class Runner:
    """Answers KRunner's calls on the session bus."""

    def __init__(self, bus: MessageBus, port: int) -> None:
        self.bus = bus
        self.port = port
        self.last_use = time.monotonic()
        self.tasks: set[asyncio.Task[None]] = set()

    def handle(self, message: Message) -> Message | None:
        if message.message_type is not MessageType.METHOD_CALL:
            return None
        if message.path != OBJECT_PATH or message.interface not in (INTERFACE, None):
            return None
        self.last_use = time.monotonic()
        if message.member == "Match" and message.signature == "s":
            return Message.new_method_return(message, "a(sssida{sv})", [matches(message.body[0])])
        if message.member == "Actions":
            actions = [[ACTION_FULL, t("plasma.search.open_full"), "window-new"]]
            return Message.new_method_return(message, "a(sss)", [actions])
        if message.member == "Config":
            return Message.new_method_return(
                message, "a{sv}", [{"MinLetterCount": Variant("i", MIN_LETTERS)}]
            )
        if message.member == "Teardown":
            return Message.new_method_return(message)
        if message.member == "Run" and message.signature == "ss":
            match_id, action_id = message.body
            if match_id.startswith(MATCH_PREFIX):
                # Answer at once: KRunner closes while the request is sent.
                task = asyncio.create_task(self.run(match_id[len(MATCH_PREFIX) :], action_id))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            return Message.new_method_return(message)
        return Message.new_error(
            message, "org.freedesktop.DBus.Error.UnknownMethod", f"Unknown method {message.member}"
        )

    async def run(self, question: str, action: str) -> None:
        """Send `question` to the assistant and show where the answer comes."""
        try:
            url = await asyncio.to_thread(ensure_daemon, self.port)
            client = DaemonClient(url)
            conversation_id = await client.ask(question)
            if action != ACTION_FULL and await client.show(conversation_id) > 0:
                return  # a panel shows it
            await asyncio.to_thread(open_in_browser, url, conversation_id)
        except (DaemonUnavailableError, desktop.LaunchError, OSError, ValueError) as exc:
            logger.warning("Cannot ask Kreafinity: %s", exc)
            await notify(self.bus, t("plasma.search.failed"), str(exc))
        except Exception as exc:  # httpx errors, unexpected answers
            logger.exception("Cannot ask Kreafinity")
            await notify(self.bus, t("plasma.search.failed"), str(exc))
        finally:
            self.last_use = time.monotonic()


def open_in_browser(url: str, conversation_id: str) -> None:
    """Open the full interface on a conversation, in the default browser."""
    opener = desktop.find_program("xdg-open", "gio")
    if opener is None:
        raise OSError("no program to open the browser (xdg-open)")
    address = f"{url}/?conversation={conversation_id}"
    desktop.launch([opener, address] if opener.endswith("xdg-open") else [opener, "open", address])


async def serve(bus_address: str | None = None, *, idle_exit: float | None = None) -> int:
    """Serve KRunner (until idle for `idle_exit` seconds, if given); return an exit code."""
    settings = load_settings()
    set_language(settings.language)
    if bus_address:
        bus = await MessageBus(bus_address=bus_address).connect()
    else:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
    try:
        reply = await bus.request_name(BUS_NAME, NameFlag.DO_NOT_QUEUE)
        if reply not in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
            logger.info("Another Kreafinity search is already running")
            return 0
        runner = Runner(bus, settings.port)
        bus.add_message_handler(runner.handle)
        if idle_exit is None:
            await bus.wait_for_disconnect()  # type: ignore[no-untyped-call]
            return 0
        while time.monotonic() - runner.last_use < idle_exit or runner.tasks:
            await asyncio.sleep(min(5.0, idle_exit))
        return 0
    finally:
        bus.disconnect()
        with contextlib.suppress(Exception):
            await bus.wait_for_disconnect()  # type: ignore[no-untyped-call]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(asyncio.run(serve()))


if __name__ == "__main__":
    main()
