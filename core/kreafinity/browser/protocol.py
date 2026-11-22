"""Talking to the browser: WebDriver BiDi (Firefox) and Chrome DevTools (Chromium).

Both are JSON messages over a WebSocket on 127.0.0.1. Each backend exposes
the same few operations, enough for the assistant: go to an address, run one
of our page scripts, take a screenshot, click, type and press keys.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

logger = logging.getLogger(__name__)

COMMAND_TIMEOUT = 30.0
NAVIGATION_TIMEOUT = 45.0


class BrowserError(Exception):
    """The assistant's browser is missing, closed or refused an operation."""


class BrowserClosedError(BrowserError):
    """The connection to the browser is lost (the user closed it…)."""


class BrowserBusyError(BrowserError):
    """Firefox still holds the session of a lost connection: it must be restarted."""


# Keys the assistant may press: WebDriver code, DevTools key, code, key code, text.
KEYS: dict[str, tuple[str, str, str, int, str]] = {
    "Enter": ("", "Enter", "Enter", 13, "\r"),
    "Tab": ("", "Tab", "Tab", 9, ""),
    "Escape": ("", "Escape", "Escape", 27, ""),
    "Backspace": ("", "Backspace", "Backspace", 8, ""),
    "ArrowUp": ("", "ArrowUp", "ArrowUp", 38, ""),
    "ArrowDown": ("", "ArrowDown", "ArrowDown", 40, ""),
    "ArrowLeft": ("", "ArrowLeft", "ArrowLeft", 37, ""),
    "ArrowRight": ("", "ArrowRight", "ArrowRight", 39, ""),
    "PageUp": ("", "PageUp", "PageUp", 33, ""),
    "PageDown": ("", "PageDown", "PageDown", 34, ""),
    "Space": (" ", " ", "Space", 32, " "),
}


class Backend(ABC):
    """One connection to the browser, driving one tab."""

    kind: str

    def __init__(self, url: str) -> None:
        self.url = url
        self._socket: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._ids = itertools.count(1)

    @property
    def connected(self) -> bool:
        return self._socket is not None and self._reader is not None and not self._reader.done()

    async def connect(self) -> None:
        """Open the WebSocket and pick (or create) the tab to drive."""
        try:
            self._socket = await asyncio.wait_for(
                connect(self.url, max_size=64 * 1024 * 1024, open_timeout=10, ping_interval=None),
                timeout=15,
            )
        except (OSError, WebSocketException, TimeoutError) as exc:
            raise BrowserClosedError(f"cannot connect to the browser: {exc}") from exc
        self._reader = asyncio.create_task(self._read())
        try:
            await self._start()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """Close the connection (the browser window stays open)."""
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.close()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        self._socket = self._reader = None
        self._fail_pending(BrowserClosedError("connection closed"))

    async def _read(self) -> None:
        assert self._socket is not None
        try:
            async for raw in self._socket:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                future = self._pending.pop(message.get("id", -1), None)
                if future is not None and not future.done():
                    future.set_result(message)
        except WebSocketException as exc:
            logger.info("Browser connection lost: %s", exc)
        finally:
            self._fail_pending(BrowserClosedError("the browser was closed"))

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def _send(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        if not self.connected or self._socket is None:
            raise BrowserClosedError("the browser is not connected")
        command_id = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[command_id] = future
        try:
            await self._socket.send(json.dumps({"id": command_id, **payload}))
            return await asyncio.wait_for(future, timeout)
        except WebSocketException as exc:
            raise BrowserClosedError(f"the browser was closed: {exc}") from exc
        except TimeoutError as exc:
            raise BrowserError(f"the browser did not answer ({payload.get('method')})") from exc
        finally:
            self._pending.pop(command_id, None)

    @abstractmethod
    async def _start(self) -> None: ...

    @abstractmethod
    async def use_tab(self) -> None:
        """Drive the tab in use, or the last one if it was closed (a new one if none)."""

    @abstractmethod
    async def navigate(self, url: str) -> None:
        """Go to `url` and wait for the page to load."""

    @abstractmethod
    async def call(self, function: str, argument: dict[str, Any]) -> Any:
        """Run a page script (see `scripts`) and return its decoded JSON result."""

    @abstractmethod
    async def screenshot(self) -> bytes:
        """PNG of what the tab shows."""

    @abstractmethod
    async def click(self, x: int, y: int) -> None:
        """A real mouse click at viewport coordinates (CSS pixels)."""

    @abstractmethod
    async def type_text(self, text: str) -> None:
        """Type text into the focused element, like a keyboard."""

    @abstractmethod
    async def press(self, key: str) -> None:
        """Press one of `KEYS`."""

    @abstractmethod
    async def bring_to_front(self) -> None:
        """Show the tab and its window to the user."""


def _decode(raw: Any) -> Any:
    if not isinstance(raw, str):
        raise BrowserError("unexpected answer from the page")
    return json.loads(raw)


class BidiBackend(Backend):
    """Firefox, through the W3C WebDriver BiDi protocol."""

    kind = "firefox"

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.context = ""

    async def command(
        self, method: str, params: dict[str, Any], timeout: float = COMMAND_TIMEOUT
    ) -> dict[str, Any]:
        message = await self._send({"method": method, "params": params}, timeout)
        if message.get("type") == "error":
            error = message.get("error", "error")
            if error in ("no such frame", "invalid session id"):
                raise BrowserClosedError(f"{error}: {message.get('message', '')}")
            raise BrowserError(f"{error}: {message.get('message', '')}".strip())
        result: dict[str, Any] = message.get("result") or {}
        return result

    async def _start(self) -> None:
        try:
            await self.command(
                "session.new",
                {
                    "capabilities": {
                        "alwaysMatch": {"unhandledPromptBehavior": {"default": "ignore"}}
                    }
                },
            )
        except BrowserError as exc:
            # Firefox keeps the session of a connection lost without
            # session.end, and a session cannot be joined again.
            if "maximum number of active sessions" in str(exc).lower():
                raise BrowserBusyError(str(exc)) from exc
            raise
        await self.use_tab()

    async def close(self) -> None:
        if self.connected:
            with contextlib.suppress(BrowserError):
                await self.command("session.end", {}, timeout=3)
        await super().close()

    async def use_tab(self) -> None:
        """Drive the last tab of the window (the one the user sees most likely)."""
        tree = await self.command("browsingContext.getTree", {"maxDepth": 0})
        contexts = [c["context"] for c in tree.get("contexts", [])]
        if self.context not in contexts:
            if contexts:
                self.context = contexts[-1]
            else:
                created = await self.command("browsingContext.create", {"type": "window"})
                self.context = created["context"]

    async def navigate(self, url: str) -> None:
        await self.use_tab()
        await self.command(
            "browsingContext.navigate",
            {"context": self.context, "url": url, "wait": "complete"},
            timeout=NAVIGATION_TIMEOUT,
        )

    async def call(self, function: str, argument: dict[str, Any]) -> Any:
        result = await self.command(
            "script.callFunction",
            {
                "functionDeclaration": function,
                "target": {"context": self.context},
                "arguments": [{"type": "string", "value": json.dumps(argument)}],
                "awaitPromise": True,
                "resultOwnership": "none",
            },
        )
        if result.get("type") == "exception":
            details = result.get("exceptionDetails", {})
            raise BrowserError(f"page script failed: {details.get('text', 'exception')}")
        return _decode(result.get("result", {}).get("value"))

    async def screenshot(self) -> bytes:
        result = await self.command("browsingContext.captureScreenshot", {"context": self.context})
        return base64.b64decode(result["data"])

    async def click(self, x: int, y: int) -> None:
        await self._actions(
            {
                "type": "pointer",
                "id": "mouse",
                "parameters": {"pointerType": "mouse"},
                "actions": [
                    {"type": "pointerMove", "x": x, "y": y, "origin": "viewport"},
                    {"type": "pointerDown", "button": 0},
                    {"type": "pointerUp", "button": 0},
                ],
            }
        )

    async def type_text(self, text: str) -> None:
        keys: list[dict[str, Any]] = []
        for char in text:
            keys += [{"type": "keyDown", "value": char}, {"type": "keyUp", "value": char}]
        await self._actions({"type": "key", "id": "keyboard", "actions": keys})

    async def press(self, key: str) -> None:
        value = KEYS[key][0]
        await self._actions(
            {
                "type": "key",
                "id": "keyboard",
                "actions": [{"type": "keyDown", "value": value}, {"type": "keyUp", "value": value}],
            }
        )

    async def _actions(self, source: dict[str, Any]) -> None:
        await self.command("input.performActions", {"context": self.context, "actions": [source]})

    async def bring_to_front(self) -> None:
        with contextlib.suppress(BrowserError):  # older Firefox: not supported
            await self.command("browsingContext.activate", {"context": self.context})


class CdpBackend(Backend):
    """Chromium, Chrome and other Chromium browsers, through the Chrome DevTools Protocol."""

    kind = "chromium"

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.target = ""
        self.session = ""

    async def command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        page: bool = True,
        timeout: float = COMMAND_TIMEOUT,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"method": method, "params": params or {}}
        if page:
            payload["sessionId"] = self.session
        message = await self._send(payload, timeout)
        if "error" in message:
            error = message["error"]
            text = error.get("message", "error") if isinstance(error, dict) else str(error)
            if "session" in text.lower() and "not found" in text.lower():
                raise BrowserClosedError(text)
            raise BrowserError(text)
        result: dict[str, Any] = message.get("result") or {}
        return result

    async def _start(self) -> None:
        await self.use_tab()

    async def use_tab(self) -> None:
        targets = await self.command("Target.getTargets", page=False)
        pages = [
            t["targetId"]
            for t in targets.get("targetInfos", [])
            if t.get("type") == "page" and not str(t.get("url", "")).startswith("devtools://")
        ]
        if self.target in pages and self.session:
            return
        if pages:
            self.target = pages[-1]
        else:
            created = await self.command("Target.createTarget", {"url": "about:blank"}, page=False)
            self.target = created["targetId"]
        attached = await self.command(
            "Target.attachToTarget", {"targetId": self.target, "flatten": True}, page=False
        )
        self.session = attached["sessionId"]

    async def navigate(self, url: str) -> None:
        await self.use_tab()
        result = await self.command("Page.navigate", {"url": url}, timeout=NAVIGATION_TIMEOUT)
        if result.get("errorText"):
            raise BrowserError(f"cannot open {url}: {result['errorText']}")

    async def call(self, function: str, argument: dict[str, Any]) -> Any:
        result = await self.command(
            "Runtime.evaluate",
            {
                "expression": f"({function})({json.dumps(json.dumps(argument))})",
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            text = details.get("exception", {}).get("description") or details.get("text")
            raise BrowserError(f"page script failed: {text}")
        return _decode(result.get("result", {}).get("value"))

    async def screenshot(self) -> bytes:
        result = await self.command("Page.captureScreenshot", {"format": "png"})
        return base64.b64decode(result["data"])

    async def click(self, x: int, y: int) -> None:
        for event in ("mouseMoved", "mousePressed", "mouseReleased"):
            await self.command(
                "Input.dispatchMouseEvent",
                {"type": event, "x": x, "y": y, "button": "left", "clickCount": 1},
            )

    async def type_text(self, text: str) -> None:
        await self.command("Input.insertText", {"text": text})

    async def press(self, key: str) -> None:
        _, name, code, number, text = KEYS[key]
        down: dict[str, Any] = {
            "type": "keyDown" if text else "rawKeyDown",
            "key": name,
            "code": code,
            "windowsVirtualKeyCode": number,
        }
        if text:
            down["text"] = text
        await self.command("Input.dispatchKeyEvent", down)
        await self.command(
            "Input.dispatchKeyEvent",
            {"type": "keyUp", "key": name, "code": code, "windowsVirtualKeyCode": number},
        )

    async def bring_to_front(self) -> None:
        await self.command("Page.bringToFront")
