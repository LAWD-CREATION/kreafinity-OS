"""The pilot mode banner: a small window on top of the others while the assistant drives."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

from kreafinity.system import notifications

logger = logging.getLogger(__name__)

GTK_CHECK = "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk"


async def gtk_python() -> str | None:
    """A Python interpreter with GTK 3 (the system one, usually)."""
    system = sorted(
        (
            str(p)
            for p in Path("/usr/bin").glob("python3.*")
            if re.fullmatch(r"python3\.\d+", p.name)
        ),
        key=lambda name: [int(part) for part in name.rsplit("python", 1)[1].split(".")],
        reverse=True,
    )
    for candidate in dict.fromkeys([sys.executable, "/usr/bin/python3", *system]):
        if not os.access(candidate, os.X_OK):
            continue
        try:
            check = await asyncio.create_subprocess_exec(
                candidate,
                "-c",
                GTK_CHECK,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await asyncio.wait_for(check.wait(), 20) == 0:
                return candidate
        except (OSError, TimeoutError):
            continue
    return None


class Banner:
    """Shows the banner (or, without GTK, a notification) and reports « Arrêter »."""

    def __init__(self, on_stop: Callable[[str], None]) -> None:
        self.on_stop = on_stop
        self.process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None

    async def show(self, language: str, *, escape: bool, fallback_text: str) -> bool:
        """Show the banner; False if only a notification could be shown."""
        from kreafinity.api.web import pilot_banner_script

        script = pilot_banner_script()
        python = await gtk_python()
        if script is not None and python is not None:
            arguments = [str(script), "--lang", language, *(["--escape"] if escape else [])]
            try:
                self.process = await asyncio.create_subprocess_exec(
                    python,
                    *arguments,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
                assert self.process.stdout is not None
                ready = await asyncio.wait_for(self.process.stdout.readline(), 15)
                if ready.strip() == b"ready":
                    self._reader = asyncio.create_task(self._read())
                    return True
            except (OSError, TimeoutError) as exc:
                logger.warning("Pilot banner did not start: %s", exc)
            await self.close()
        with contextlib.suppress(Exception):
            await notifications.send_notification("Kreafinity", fallback_text)
        return False

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        line = await self.process.stdout.readline()
        if line.strip() == b"stop":
            self.on_stop("banner")
        elif self.process.returncode is None and not line:
            self.on_stop("banner")  # closed by someone else

    def status(self, text: str) -> None:
        if self.process is None or self.process.stdin is None:
            return
        with contextlib.suppress(Exception):
            self.process.stdin.write((json.dumps({"status": text}) + "\n").encode())

    async def close(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
        process, self.process = self.process, None
        if process is None:
            return
        if process.stdin is not None:
            with contextlib.suppress(Exception):
                process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
