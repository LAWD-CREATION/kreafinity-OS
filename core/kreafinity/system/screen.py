"""What is on the screen: a screenshot, made small enough to show to the AI.

The screenshot is taken by the desktop's own tool, the only ones Wayland
allows: Spectacle on KDE Plasma, gnome-screenshot on GNOME, grim on
wlroots desktops (Sway…); on X11, scrot or ImageMagick's import.
"""

from __future__ import annotations

import base64
import io
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: Longest side sent to the AI, in pixels (what vision models read well).
MAX_SIDE = 1568
JPEG_QUALITY = 80
_TIMEOUT = 20

# (program, arguments before the output file), tried in this order.
_TOOLS: tuple[tuple[str, list[str]], ...] = (
    ("spectacle", ["--background", "--nonotify", "--fullscreen", "--output"]),
    ("gnome-screenshot", ["-f"]),
    ("grim", []),
    ("scrot", ["--overwrite"]),
    ("import", ["-window", "root"]),
)


#: The latest screenshot shown to the AI (pilot mode clicks at its coordinates).
LAST: Screenshot | None = None


class ScreenError(Exception):
    """The screen cannot be captured (no graphical session, no tool…)."""


@dataclass(frozen=True, slots=True)
class Screenshot:
    """A screenshot ready for the AI.

    Attributes:
        data: JPEG bytes, base64-encoded.
        width, height: Size sent (the screen's, scaled down if larger).
        original: Size of the screen itself.
        tool: Program that took it.
    """

    data: str
    width: int
    height: int
    original: tuple[int, int]
    tool: str
    media_type: str = "image/jpeg"


def _tools() -> list[tuple[str, list[str]]]:
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
    wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    found = [(name, args) for name, args in _TOOLS if shutil.which(name)]
    if wayland:  # X11 tools only see a black screen there
        found = [(n, a) for n, a in found if n not in ("scrot", "import")]
    if "kde" in desktop:
        found.sort(key=lambda tool: tool[0] != "spectacle")
    elif {"gnome", "ubuntu"} & set(desktop):
        found.sort(key=lambda tool: tool[0] != "gnome-screenshot")
    return found


def capture() -> Screenshot:
    """Take a screenshot of the whole screen (blocking).

    Raises:
        ScreenError: No graphical session, no capture tool, or the capture failed.
    """
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        raise ScreenError("no graphical session (DISPLAY / WAYLAND_DISPLAY)")
    tools = _tools()
    if not tools:
        raise ScreenError("no screenshot tool found (spectacle, gnome-screenshot, grim, scrot)")
    errors = []
    with tempfile.TemporaryDirectory(prefix="kreafinity-screen-") as folder:
        output = Path(folder) / "screen.png"
        for name, arguments in tools:
            try:
                result = subprocess.run(
                    [name, *arguments, str(output)],
                    capture_output=True,
                    text=True,
                    timeout=_TIMEOUT,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                errors.append(f"{name}: {exc}")
                continue
            if result.returncode == 0 and output.is_file() and output.stat().st_size > 0:
                global LAST
                LAST = prepare(output.read_bytes(), tool=name)
                return LAST
            detail = (result.stderr or result.stdout).strip().splitlines()
            errors.append(f"{name}: {detail[-1] if detail else result.returncode}")
    raise ScreenError("; ".join(errors))


def prepare(image: bytes, *, tool: str = "") -> Screenshot:
    """Scale the image down (longest side at most MAX_SIDE) and encode it as JPEG.

    Raises:
        ScreenError: Not an image.
    """
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(image)) as opened:
            original = opened.size
            picture = opened.convert("RGB")
        picture.thumbnail((MAX_SIDE, MAX_SIDE))
        output = io.BytesIO()
        picture.save(output, "JPEG", quality=JPEG_QUALITY, optimize=True)
        size = picture.size
    except (UnidentifiedImageError, OSError) as exc:
        raise ScreenError(f"the screenshot is not a readable image: {exc}") from exc
    return Screenshot(
        data=base64.b64encode(output.getvalue()).decode("ascii"),
        width=size[0],
        height=size[1],
        original=original,
        tool=tool,
    )
