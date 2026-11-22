"""Pilot mode: the assistant moves the mouse and types, while the user watches.

The user starts it by approving ``pilot_start`` (and, on Wayland, the
system's own dialog). While it lasts, a banner stays on top of the windows.
It stops when the user presses Escape (KDE Plasma), moves the mouse, clicks
« Arrêter » on the banner, when the request ends, after a few idle minutes,
or when the assistant is done.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from kreafinity.i18n import t
from kreafinity.paths import data_dir
from kreafinity.pilot import backends
from kreafinity.pilot.backends import InputBackend, InputError
from kreafinity.pilot.banner import Banner
from kreafinity.pilot.desktop import KWinDesktop, Probe, WindowInfo, XdotoolProbe, is_kde
from kreafinity.pilot.keys import ComboError, parse_combo
from kreafinity.system import screen

logger = logging.getLogger(__name__)

IDLE_SECONDS = 180  # without any pilot action
MAX_SECONDS = 20 * 60
TAKEOVER_PIXELS = 12  # the pointer moved away from where the pilot left it
SETTLE_SECONDS = 0.7  # before the screenshot that follows the actions
CURSOR_POLL = 0.4  # X11 without KWin: how often the pointer is checked
ITERATIONS = 40  # steps of one request while piloting

STOP_REASONS = {
    "escape": "the user pressed Escape: pilot mode is stopped, they took back control",
    "mouse": "the user moved the mouse: pilot mode is stopped, they took back control",
    "banner": "the user clicked Stop on the banner: pilot mode is stopped",
    "idle": "pilot mode stopped after a few minutes without action",
    "limit": "pilot mode stopped after 20 minutes",
    "request": "pilot mode ended with the request",
    "assistant": "pilot mode was stopped by the assistant",
    "error": "pilot mode stopped after an error",
}


class PilotError(Exception):
    """A step was refused or failed (pilot mode goes on)."""


class PilotStopped(PilotError):
    """Pilot mode is not running (never started, or stopped by the user…)."""


@dataclass
class PilotSession:
    """The pilot mode of this Kreafinity process."""

    active: bool = False
    reason: str = ""
    stopped: str = ""  # why the last pilot mode stopped (a STOP_REASONS key)
    backend: InputBackend | None = None
    probe: Probe | None = None
    kwin: KWinDesktop | None = None
    banner: Banner | None = None
    escape: bool = False
    expected: tuple[int, int] | None = None  # where the pilot left the pointer
    started_at: float = 0.0
    last_action: float = 0.0
    _quiet_until: float = 0.0  # pointer reports to ignore (the pilot is moving it)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def start(self, reason: str, language: str = "fr") -> screen.Screenshot:
        """Start pilot mode (or go on with it) and return a fresh screenshot."""
        async with self._lock:
            if self.active:
                return await self._screenshot()
            shot = await self._capture()
            try:
                await self._open(shot.original, language)
            except BaseException:
                await self._close()
                raise
            self.active, self.reason, self.stopped = True, reason, ""
            self.started_at = self.last_action = time.monotonic()
            self.expected = None
            self._tasks.append(asyncio.create_task(self._watchdog()))
            if isinstance(self.probe, XdotoolProbe):
                self._tasks.append(asyncio.create_task(self._poll_cursor()))
            return shot

    async def _open(self, desktop: tuple[int, int], language: str) -> None:
        wayland = backends.choose(data_dir() / "pilot-portal-token")
        kde = is_kde()
        if isinstance(wayland, backends.PortalBackend) and not kde:
            raise PilotError(
                "pilot mode on Wayland needs KDE Plasma for now (on GNOME, choose a "
                "« GNOME on Xorg » session)"
            )
        self.backend = wayland
        if kde:
            self.kwin = KWinDesktop(desktop)
            self.kwin.on_stop = self._stop_soon
            self.kwin.on_cursor = self._cursor_report
            await self.kwin.start()
            self.probe, self.escape = self.kwin, True
        else:
            self.probe, self.escape = XdotoolProbe(), False
        await self.backend.start(desktop)  # Wayland: the system asks the user
        self.banner = Banner(self._stop_soon)
        await self.banner.show(language, escape=self.escape, fallback_text=t("pilot.banner"))

    async def act(self, steps: Sequence[dict[str, Any]]) -> tuple[list[str], screen.Screenshot]:
        """Do the steps; return what was done and the screenshot that follows."""
        async with self._lock:
            self._check_running()
            done: list[str] = []
            for number, step in enumerate(steps, 1):
                self._check_running()
                await self._check_takeover()
                try:
                    await self._step(step)
                except (InputError, ComboError, PilotError) as exc:
                    if isinstance(exc, PilotStopped):
                        raise
                    before = f" (done before: {', '.join(done)})" if done else ""
                    raise PilotError(f"step {number} ({step['action']}): {exc}{before}") from exc
                done.append(step["action"])
                self.last_action = time.monotonic()
            await asyncio.sleep(SETTLE_SECONDS)
            await self._check_takeover()
            return done, await self._screenshot()

    async def stop(self, why: str = "assistant") -> None:
        """Stop pilot mode (nothing happens if it is not running)."""
        async with self._lock:
            if not self.active:
                return
            self.active, self.stopped = False, why
            logger.info("Pilot mode stopped: %s", why)
            await self._close()

    def _stop_soon(self, why: str) -> None:
        """Called by the banner, the Escape key or a pointer report."""
        if self.active:
            self.active, self.stopped = False, why  # refuse any next step at once
            asyncio.get_running_loop().create_task(self._close())

    def _check_running(self) -> None:
        if not self.active:
            if self.stopped:
                raise PilotStopped(STOP_REASONS.get(self.stopped, self.stopped))
            raise PilotStopped("pilot mode is not running: start it with pilot_start")

    async def _check_takeover(self) -> None:
        if self.expected is None or self.probe is None:
            return
        with contextlib.suppress(InputError):
            position = await self.probe.cursor()
            if position is not None and _far(position, self.expected):
                self._stop_soon("mouse")
        self._check_running()

    def _cursor_report(self, x: int, y: int) -> None:
        quiet = time.monotonic() <= self._quiet_until
        if self.active and self.expected is not None and not quiet and _far((x, y), self.expected):
            self._stop_soon("mouse")

    async def _poll_cursor(self) -> None:
        while self.active:
            await asyncio.sleep(CURSOR_POLL)
            if time.monotonic() > self._quiet_until and not self._lock.locked():
                with contextlib.suppress(Exception):
                    await self._check_takeover()

    async def _watchdog(self) -> None:
        while self.active:
            await asyncio.sleep(5)
            now = time.monotonic()
            if now - self.started_at > MAX_SECONDS:
                self._stop_soon("limit")
            elif now - self.last_action > IDLE_SECONDS and not self._lock.locked():
                self._stop_soon("idle")

    async def _close(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            if task is not asyncio.current_task():
                task.cancel()
        for part in (self.banner, self.backend, self.kwin):
            if part is None:
                continue
            with contextlib.suppress(Exception):
                if isinstance(part, Banner):
                    await part.close()
                else:
                    await part.stop()
        self.banner = self.backend = self.kwin = self.probe = None
        self.expected = None

    # --- Steps ---------------------------------------------------------------------------

    async def _step(self, step: dict[str, Any]) -> None:
        assert self.backend is not None and self.probe is not None
        action = step["action"]
        if action in ("click", "move", "drag", "scroll"):
            x, y = self._point(step["x"], step["y"])
            await self._move_checked(x, y)
            if action == "click":
                self._status(t("pilot.step.click", x=step["x"], y=step["y"]))
                button = step.get("button", "left")
                for _ in range(2 if step.get("double") else 1):
                    await self.backend.button(button, True)
                    await asyncio.sleep(0.04)
                    await self.backend.button(button, False)
                    await asyncio.sleep(0.08)
            elif action == "drag":
                tx, ty = self._point(step["to_x"], step["to_y"])
                await self._refuse_window(await self.probe.window_at(tx, ty), typing=False)
                self._status(t("pilot.step.drag"))
                await self.backend.button("left", True)
                for i in range(1, 11):
                    await self._move(x + (tx - x) * i // 10, y + (ty - y) * i // 10)
                    await asyncio.sleep(0.03)
                await self.backend.button("left", False)
            elif action == "scroll":
                amount = int(step.get("amount", 3))
                direction = step.get("direction", "down")
                self._status(t(f"pilot.step.scroll.{direction}"))
                await self.backend.scroll(
                    amount if direction == "down" else -amount if direction == "up" else 0,
                    amount if direction == "right" else -amount if direction == "left" else 0,
                )
        elif action in ("type", "key"):
            await self._refuse_window(await self.probe.active_window(), typing=True)
            if action == "type":
                text = str(step["text"])
                self._status(t("pilot.step.type", text=text[:60]))
                await self.backend.type_text(text)
            else:
                keys = parse_combo(str(step["keys"]))
                self._status(t("pilot.step.key", keys=str(step["keys"])))
                await self.backend.combo(keys)
        elif action == "wait":
            await asyncio.sleep(min(5.0, max(0.0, float(step.get("seconds", 1)))))
        else:  # pragma: no cover - the tool validates the steps
            raise PilotError(f"unknown action {action}")

    def _point(self, x: Any, y: Any) -> tuple[int, int]:
        """A position on the last screenshot the AI saw -> desktop pixels."""
        last = screen.LAST
        if last is None:
            raise PilotError("look at the screen first (no screenshot yet)")
        px, py = round(float(x)), round(float(y))
        if not (0 <= px < last.width and 0 <= py < last.height):
            raise PilotError(f"({px}, {py}) is outside the screenshot ({last.width}x{last.height})")
        return (
            min(last.original[0] - 1, round(px * last.original[0] / last.width)),
            min(last.original[1] - 1, round(py * last.original[1] / last.height)),
        )

    async def _move(self, x: int, y: int) -> None:
        assert self.backend is not None
        self._quiet_until = time.monotonic() + 0.6
        await self.backend.move(x, y)
        self.expected = (x, y)

    async def _move_checked(self, x: int, y: int) -> None:
        """Move there, refusing Kreafinity's own windows."""
        assert self.probe is not None
        if isinstance(self.probe, XdotoolProbe):
            await self._move(x, y)
            window = await self.probe.window_at(x, y)
        else:
            window = await self.probe.window_at(x, y)
            await self._refuse_window(window, typing=False)
            await self._move(x, y)
        await self._refuse_window(window, typing=False)

    @staticmethod
    async def _refuse_window(window: WindowInfo | None, *, typing: bool) -> None:
        if window is None:
            return
        if window.is_kreafinity:
            raise PilotError(
                "this is a Kreafinity window: the pilot never acts on it (the user does)"
            )
        if typing and window.is_terminal:
            raise PilotError(
                f"the active window is a terminal ({window.application}): never type commands "
                "in pilot mode, use run_command (the user sees and approves the command)"
            )

    def _status(self, text: str) -> None:
        if self.banner is not None:
            self.banner.status(text)

    async def _capture(self) -> screen.Screenshot:
        try:
            return await asyncio.to_thread(screen.capture)
        except screen.ScreenError as exc:
            raise PilotError(f"cannot see the screen: {exc}") from exc

    async def _screenshot(self) -> screen.Screenshot:
        shot = await self._capture()
        if self.backend is not None:
            self.backend.resize(shot.original)
        if self.kwin is not None:
            self.kwin.desktop_size = shot.original
        return shot


def _far(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return abs(a[0] - b[0]) > TAKEOVER_PIXELS or abs(a[1] - b[1]) > TAKEOVER_PIXELS


_session: PilotSession | None = None


def get_session() -> PilotSession:
    """The pilot mode of this process."""
    global _session
    if _session is None:
        _session = PilotSession()
    return _session


async def end_request() -> None:
    """Stop pilot mode when the request ends (the user gets the hand back)."""
    if _session is not None and _session.active:
        await _session.stop("request")
