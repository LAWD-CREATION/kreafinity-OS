"""The desktop's own « open files » dialog, shown by the daemon.

Used by the assistant panel of the KDE taskbar: inside a Plasma panel, the web
page cannot show a file dialog itself (the panel closes when it loses the
focus), so the daemon asks the desktop: kdialog (KDE), else zenity (GNOME…).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

#: Seconds the dialog may stay open.
PICK_TIMEOUT = 900


class PickerError(Exception):
    """No file dialog can be shown here."""


def pick_files(title: str, *, start: Path | None = None) -> list[Path]:
    """Ask the user to choose files; an empty list when they cancel.

    Raises:
        PickerError: Neither kdialog nor zenity is installed, or the dialog failed.
    """
    folder = str(start or Path.home())
    kdialog, zenity = shutil.which("kdialog"), shutil.which("zenity")
    if kdialog:
        command = [
            kdialog,
            "--title",
            title,
            "--multiple",
            "--separate-output",
            "--getopenfilename",
            folder,
        ]
    elif zenity:
        command = [
            zenity,
            "--file-selection",
            "--multiple",
            "--separator=\n",
            "--title",
            title,
            f"--filename={folder}/",
        ]
    else:
        raise PickerError("no file dialog available (install kdialog or zenity)")
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=PICK_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PickerError(f"the file dialog failed: {exc}") from exc
    if result.returncode == 1:  # cancelled
        return []
    if result.returncode != 0:
        raise PickerError(f"the file dialog failed: {result.stderr.strip()[-300:]}")
    return [Path(line) for line in result.stdout.splitlines() if line.strip()]
