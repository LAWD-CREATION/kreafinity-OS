"""Standard locations (freedesktop XDG Base Directory specification)."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

APP_NAME = "kreafinity"


def _xdg_base(variable: str, default: str) -> Path:
    value = os.environ.get(variable, "")
    # The specification says relative paths must be ignored.
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / default


def xdg_data_home() -> Path:
    """Base directory for user data (``~/.local/share``)."""
    return _xdg_base("XDG_DATA_HOME", ".local/share")


def config_dir() -> Path:
    """Directory holding the configuration (``~/.config/kreafinity``)."""
    return _xdg_base("XDG_CONFIG_HOME", ".config") / APP_NAME


def data_dir() -> Path:
    """Directory holding local data: history, action log (``~/.local/share/kreafinity``)."""
    return _xdg_base("XDG_DATA_HOME", ".local/share") / APP_NAME


def user_dirs(home: Path | None = None) -> dict[str, Path]:
    """Return the user's well-known folders (Downloads, Documents…), localized.

    Reads ``user-dirs.dirs`` written by ``xdg-user-dirs``, so on a French
    system ``DOWNLOAD`` maps to ``~/Téléchargements``. Keys are the XDG names
    without prefix and suffix (``DESKTOP``, ``DOWNLOAD``, ``DOCUMENTS``…);
    only existing folders other than the home directory itself are returned.
    """
    home = home or Path.home()
    config_file = _xdg_base("XDG_CONFIG_HOME", ".config") / "user-dirs.dirs"
    try:
        lines = config_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    result: dict[str, Path] = {}
    for line in lines:
        line = line.strip()
        if not line.startswith("XDG_") or "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        if not key.endswith("_DIR"):
            continue
        try:
            words = shlex.split(raw_value)
        except ValueError:
            continue
        if len(words) != 1:
            continue
        value = words[0]
        if value.startswith("$HOME"):
            path = Path(str(home) + value[len("$HOME") :])
        elif value.startswith("/"):
            path = Path(value)
        else:
            continue
        if path != home and path.is_dir():
            result[key[len("XDG_") : -len("_DIR")]] = path
    return result
