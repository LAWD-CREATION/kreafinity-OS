"""Volume of the default sound output, through the sound server's own command.

PipeWire (``wpctl``, Debian 12, Ubuntu 22.10 and later) is tried first, then
PulseAudio (``pactl``, which PipeWire also answers through ``pipewire-pulse``).
There is no standard D-Bus interface for the volume, hence these commands.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

_TIMEOUT = 10


class AudioError(Exception):
    """The volume cannot be read or changed (no sound server, command failed…)."""


@dataclass(frozen=True, slots=True)
class VolumeState:
    """Volume of the default output.

    Attributes:
        percent: Volume, from 0 to 100 (or more if amplified beyond 100 %).
        muted: Whether the sound is cut.
        backend: Command used: ``wpctl`` or ``pactl``.
    """

    percent: int
    muted: bool
    backend: str


def get_volume() -> VolumeState:
    """Current volume of the default output.

    Raises:
        AudioError: No sound server answers.
    """
    errors = []
    for backend in _BACKENDS:
        if shutil.which(backend) is None:
            continue
        try:
            return _READERS[backend]()
        except AudioError as exc:
            errors.append(f"{backend}: {exc}")
    if not errors:
        raise AudioError("no sound server command found (wpctl or pactl)")
    raise AudioError("; ".join(errors))


def set_volume(*, percent: int | None = None, muted: bool | None = None) -> VolumeState:
    """Change the volume and/or mute state of the default output; return the new state.

    Raises:
        AudioError: No sound server answers, or it refused the change.
    """
    backend = get_volume().backend
    if backend == "wpctl":
        if percent is not None:
            _run("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{percent / 100:.2f}")
        if muted is not None:
            _run("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if muted else "0")
    else:
        if percent is not None:
            _run("pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%")
        if muted is not None:
            _run("pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0")
    return get_volume()


def _run(*command: str) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            env={**os.environ, "LC_ALL": "C"},  # English output, to parse it
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioError(str(exc)) from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        raise AudioError(message[-1] if message else f"exit code {result.returncode}")
    return result.stdout


def _read_wpctl() -> VolumeState:
    # "Volume: 0.40" or "Volume: 0.40 [MUTED]"
    output = _run("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@")
    match = re.search(r"Volume:\s*([0-9.]+)", output)
    if match is None:
        raise AudioError(f"unexpected answer: {output.strip()[:200]}")
    return VolumeState(
        percent=round(float(match.group(1)) * 100), muted="[MUTED]" in output, backend="wpctl"
    )


def _read_pactl() -> VolumeState:
    # "Volume: front-left: 26214 /  40% / -23.88 dB,   front-right: 26214 /  40% / …"
    volume = _run("pactl", "get-sink-volume", "@DEFAULT_SINK@")
    levels = [int(level) for level in re.findall(r"(\d+)%", volume)]
    if not levels:
        raise AudioError(f"unexpected answer: {volume.strip()[:200]}")
    mute = _run("pactl", "get-sink-mute", "@DEFAULT_SINK@")
    return VolumeState(percent=max(levels), muted="yes" in mute.lower(), backend="pactl")


_BACKENDS = ("wpctl", "pactl")
_READERS = {"wpctl": _read_wpctl, "pactl": _read_pactl}
