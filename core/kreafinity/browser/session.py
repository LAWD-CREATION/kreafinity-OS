"""The assistant's browser session: open a page, read it, show or do something in it."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from kreafinity.browser import launcher, scripts
from kreafinity.browser.protocol import (
    KEYS,
    Backend,
    BidiBackend,
    BrowserBusyError,
    BrowserClosedError,
    BrowserError,
    CdpBackend,
)

logger = logging.getLogger(__name__)

MAX_TEXT = 6000
MAX_ELEMENTS = 120
SETTLE_TIMEOUT = 10.0
POINT_SECONDS = 12


@dataclass(slots=True)
class PageState:
    """What the page shows, as read by `BrowserSession.read`."""

    url: str
    title: str
    text: str
    text_truncated: bool
    elements: list[dict[str, Any]]
    scroll: dict[str, int]
    screenshot: bytes | None = None


@dataclass(slots=True)
class BrowserSession:
    """One assistant's browser, driven by this Kreafinity process."""

    backend: Backend | None = None
    #: Elements of the last page read, by reference number (for previews).
    elements: dict[int, dict[str, Any]] = field(default_factory=dict)
    url: str = ""
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def _connect(self, *, start: bool, url: str = "about:blank") -> Backend:
        if self.backend is not None and self.backend.connected:
            return self.backend
        program = await asyncio.to_thread(launcher.find_browser)
        factory = BidiBackend if program.kind == "firefox" else CdpBackend
        address = launcher.endpoint(program)
        if address:
            backend = factory(address)
            try:
                await backend.connect()
                self.backend = backend
                return backend
            except BrowserBusyError:
                if not start:
                    raise BrowserError(
                        "the assistant's browser must be restarted: open the page again "
                        "with browser_open"
                    ) from None
                logger.info("The assistant's Firefox holds a lost session; restarting it")
                await asyncio.to_thread(launcher.stop, program)
            except BrowserClosedError:
                logger.info("The assistant's browser is gone; starting it again")
        if not start:
            raise BrowserError("the assistant's browser is not open: open a page first")
        address = await asyncio.to_thread(launcher.launch, program, url)
        for attempt in range(20):  # the endpoint can appear just before it accepts
            backend = factory(address)
            try:
                await backend.connect()
                self.backend = backend
                return backend
            except BrowserClosedError:
                if attempt == 19:
                    raise
                await asyncio.sleep(0.25)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _reset(self) -> None:
        if self.backend is not None:
            await self.backend.close()
        self.backend = None
        self.elements.clear()

    async def open(self, url: str) -> dict[str, str]:
        """Show `url` in the assistant's browser (starting it if needed)."""
        async with self._lock:
            for attempt in (1, 2):
                backend = await self._connect(start=True, url=url)
                try:
                    await backend.navigate(url)
                    await backend.bring_to_front()
                    break
                except BrowserClosedError:
                    await self._reset()
                    if attempt == 2:
                        raise
            self.elements.clear()
            return await self._where(backend)

    async def read(self, *, screenshot: bool = False) -> PageState:
        """The page's address, title, text and the elements one can use."""
        async with self._lock:
            backend = await self._live()
            await self._settle(backend)
            data = await self._call(
                backend,
                scripts.READ_PAGE,
                {"max_text": MAX_TEXT, "max_elements": MAX_ELEMENTS},
            )
            self.elements = {item["ref"]: item for item in data["elements"]}
            self.url = data["url"]
            image = await backend.screenshot() if screenshot else None
            return PageState(
                url=data["url"],
                title=data["title"],
                text=data["text"],
                text_truncated=data["text_truncated"],
                elements=data["elements"],
                scroll=data["scroll"],
                screenshot=image,
            )

    async def point(self, ref: int, message: str) -> None:
        """Show the user an element: a glowing frame and a bubble with `message`."""
        async with self._lock:
            backend = await self._live()
            await backend.bring_to_front()
            result = await self._call(
                backend,
                scripts.POINT,
                {"ref": ref, "message": message, "seconds": POINT_SECONDS},
            )
            self._check(result, ref)

    async def act(self, steps: list[dict[str, Any]]) -> list[str]:
        """Do the steps in order; return what was done (stops at the first failure)."""
        async with self._lock:
            backend = await self._live()
            await backend.bring_to_front()
            done: list[str] = []
            with contextlib.suppress(BrowserError):  # the highlight would hide the page
                await backend.call(scripts.CLEAR_POINTER, {})
            try:
                for number, step in enumerate(steps, 1):
                    try:
                        await self._step(backend, step)
                    except BrowserError as exc:
                        before = f" (steps done before: {', '.join(done)})" if done else ""
                        raise type(exc)(
                            f"step {number} ({step['action']}) failed: {exc}{before}"
                        ) from exc
                    done.append(step["action"])
                await self._settle(backend)
            finally:
                # Numbers of another page must never be used (or shown) again.
                with contextlib.suppress(BrowserError):
                    url = (await backend.call(scripts.READY_STATE, {}))["url"]
                    if url != self.url:
                        self.elements.clear()
                        self.url = url
            return done

    async def where(self) -> dict[str, str]:
        async with self._lock:
            return await self._where(await self._live())

    async def close(self) -> None:
        """Close the connection; the browser window stays open for the user."""
        async with self._lock:
            await self._reset()

    async def _step(self, backend: Backend, step: dict[str, Any]) -> None:
        action = step["action"]
        if action in ("click", "type"):
            ref = int(step["ref"])
            target = await self._call(backend, scripts.LOCATE, {"ref": ref, "purpose": action})
            self._check(target, ref)
            if target.get("disabled"):
                raise BrowserError(f"element {ref} is disabled")
            if action == "click":
                await backend.click(int(target["x"]), int(target["y"]))
                await self._settle(backend)
            else:
                await backend.type_text(str(step["text"]))
                if step.get("submit"):
                    await backend.press("Enter")
                    await self._settle(backend)
        elif action == "select":
            ref = int(step["ref"])
            result = await self._call(
                backend, scripts.SELECT_OPTION, {"ref": ref, "option": str(step["option"])}
            )
            if result.get("error") == "no_option":
                raise BrowserError(
                    f"no option {step['option']!r} in element {ref}; options: "
                    + ", ".join(result.get("options", []))
                )
            self._check(result, ref)
        elif action == "press":
            await backend.press(str(step["key"]))
            await self._settle(backend)
        elif action == "scroll":
            await self._call(backend, scripts.SCROLL, {"direction": step.get("direction", "down")})
        elif action == "back":
            await self._call(backend, scripts.BACK, {})
            await asyncio.sleep(0.3)
            await self._settle(backend)
        else:  # pragma: no cover - the tool validates the actions
            raise BrowserError(f"unknown action {action}")

    @staticmethod
    def _check(result: dict[str, Any], ref: int) -> None:
        error = result.get("error")
        if error == "gone":
            raise BrowserError(
                f"element {ref} is no longer on the page: read the page again (browser_read)"
            )
        if error == "secret":
            raise BrowserError(
                "this field is for a password or bank card: never type it, ask the user to"
            )
        if error == "not_select":
            raise BrowserError(f"element {ref} is not a list of choices")
        if error:
            raise BrowserError(str(error))

    async def _live(self) -> Backend:
        try:
            backend = await self._connect(start=False)
            await backend.use_tab()  # the user may have closed the tab
            return backend
        except BrowserClosedError:
            await self._reset()
            return await self._connect(start=False)

    async def _call(self, backend: Backend, function: str, argument: dict[str, Any]) -> Any:
        try:
            return await backend.call(function, argument)
        except BrowserClosedError:
            await self._reset()
            raise BrowserError("the assistant's browser was closed: open a page again") from None

    async def _settle(self, backend: Backend) -> None:
        """Wait until the page (maybe a new one after a click) has loaded."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SETTLE_TIMEOUT
        await asyncio.sleep(0.2)
        while loop.time() < deadline:
            try:
                state = await backend.call(scripts.READY_STATE, {})
                if state.get("state") == "complete":
                    return
            except BrowserClosedError:
                raise
            except BrowserError:
                pass  # the page is changing (navigation in progress)
            await asyncio.sleep(0.25)

    async def _where(self, backend: Backend) -> dict[str, str]:
        with contextlib.suppress(BrowserError):
            await self._settle(backend)
            state = await backend.call(
                "(a) => JSON.stringify({ url: location.href, title: document.title })", {}
            )
            self.url = state["url"]
            return {"url": state["url"], "title": state["title"]}
        return {"url": self.url, "title": ""}


_session: BrowserSession | None = None


def get_session() -> BrowserSession:
    """The browser session of this process."""
    global _session
    if _session is None:
        _session = BrowserSession()
    return _session


async def shutdown() -> None:
    """Close the connection to the assistant's browser (its window stays open)."""
    global _session
    if _session is not None:
        await _session.close()
        _session = None


__all__ = ["KEYS", "BrowserSession", "PageState", "get_session", "shutdown"]
