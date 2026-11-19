"""Running a program to completion, with a time limit and a bounded output."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Output kept from the start and from the end of a long output.
HEAD_BYTES = 12_000
TAIL_BYTES = 4_000
#: How long background processes may keep the output open after the program exits.
_GRACE_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class Completed:
    """How a program ended.

    Attributes:
        exit_code: Its exit code, or ``None`` if it was stopped for taking too long.
        output: What it printed (standard output then errors), possibly shortened.
        total_bytes: Size of the whole output.
        duration: Seconds it ran.
    """

    exit_code: int | None
    output: str
    total_bytes: int
    duration: float

    @property
    def timed_out(self) -> bool:
        return self.exit_code is None


class _Capture:
    """Keeps the beginning and the end of a stream, counting the rest."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    async def drain(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(65536):
            self.total += len(chunk)
            room = HEAD_BYTES - len(self.head)
            if room > 0:
                self.head += chunk[:room]
                chunk = chunk[room:]
            if chunk:
                self.tail = (self.tail + chunk)[-TAIL_BYTES:]

    def text(self) -> str:
        head = self.head.decode("utf-8", errors="replace")
        if not self.tail:
            return head
        skipped = self.total - len(self.head) - len(self.tail)
        tail = self.tail.decode("utf-8", errors="replace")
        return f"{head}\n[… {skipped} bytes omitted …]\n{tail}" if skipped else head + tail


async def run_captured(
    command: Sequence[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Completed:
    """Run `command` without a terminal and wait for it, at most `timeout` seconds.

    The program gets no input (so it cannot wait for a password) and runs in
    its own process group: when it takes too long, or when the request is
    cancelled, the whole group is stopped.

    Raises:
        OSError: The program cannot be started.
    """
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        start_new_session=True,
    )
    assert process.stdout is not None
    capture = _Capture()
    reader = asyncio.create_task(capture.drain(process.stdout))
    exit_code: int | None
    try:
        exit_code = await asyncio.wait_for(process.wait(), timeout)
    except TimeoutError:
        exit_code = None
        await _stop_group(process)
    except BaseException:  # cancelled: do not leave the program running
        await _stop_group(process)
        reader.cancel()
        raise
    # Programs it started in the background may hold the output open.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.shield(reader), _GRACE_SECONDS)
    if not reader.done():
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader
        # Stop listening to them (asyncio offers no public way to close the pipe):
        # the program itself has exited, so this does not stop anything.
        transport = getattr(process, "_transport", None)
        if transport is not None:
            transport.close()
    return Completed(
        exit_code=exit_code,
        output=capture.text(),
        total_bytes=capture.total,
        duration=round(time.monotonic() - started, 2),
    )


async def _stop_group(process: asyncio.subprocess.Process) -> None:
    """Ask the process group to stop, then force it after a few seconds."""
    for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 3.0)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, sig)
        try:
            await asyncio.wait_for(process.wait(), wait)
            return
        except TimeoutError:
            continue
