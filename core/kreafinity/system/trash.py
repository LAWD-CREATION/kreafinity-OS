"""Moving files to the trash, following the freedesktop.org Trash specification.

https://specifications.freedesktop.org/trash-spec/latest/

Files are never deleted: they are moved to ``~/.local/share/Trash/files``
with a ``.trashinfo`` record in ``Trash/info``, so the file manager can
restore them. Files on another filesystem are handed to ``gio trash``.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from kreafinity.paths import xdg_data_home


class TrashError(Exception):
    """A file could not be moved to the trash (it was left untouched)."""


def home_trash_dir() -> Path:
    """The user's trash (``~/.local/share/Trash``)."""
    return xdg_data_home() / "Trash"


def move_to_trash(path: Path, *, trash_dir: Path | None = None) -> Path | None:
    """Move `path` (file, folder or symlink, never followed) to the trash.

    Returns where the entry now is inside the trash, or ``None`` when it was
    handed to ``gio`` (file on another filesystem).

    Raises:
        TrashError: The entry does not exist or cannot be trashed.
    """
    trash_dir = trash_dir or home_trash_dir()
    try:
        entry_device = os.lstat(path).st_dev
    except OSError as exc:
        raise TrashError(f"{path}: {exc.strerror}") from exc

    files_dir, info_dir = trash_dir / "files", trash_dir / "info"
    for directory in (trash_dir, files_dir, info_dir):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if entry_device != os.stat(files_dir).st_dev:
        _trash_with_gio(path)
        return None

    name, info_file = _reserve_name(path, files_dir, info_dir)
    target = files_dir / name
    try:
        os.rename(path, target)
    except OSError as exc:
        info_file.unlink(missing_ok=True)
        if exc.errno == errno.EXDEV:
            _trash_with_gio(path)
            return None
        raise TrashError(f"{path}: {exc.strerror}") from exc
    return target


def _reserve_name(path: Path, files_dir: Path, info_dir: Path) -> tuple[str, Path]:
    """Pick a free name in the trash and atomically create its ``.trashinfo``."""
    stem, suffix = (path.stem, path.suffix) if path.stem else (path.name, "")
    record = (
        "[Trash Info]\n"
        f"Path={quote(str(path))}\n"
        f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n"
    )
    for counter in range(1, 10_000):
        name = path.name if counter == 1 else f"{stem}.{counter}{suffix}"
        info_file = info_dir / f"{name}.trashinfo"
        try:
            fd = os.open(info_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(record)
        if os.path.lexists(files_dir / name):  # orphan file without record
            info_file.unlink()
            continue
        return name, info_file
    raise TrashError(f"{path}: no free name in the trash")


def _trash_with_gio(path: Path) -> None:
    gio = shutil.which("gio")
    if gio is None:
        raise TrashError(
            f"{path} is on another disk and no trash helper (gio) is available; "
            "it was left untouched"
        )
    try:
        completed = subprocess.run(
            [gio, "trash", "--", str(path)], capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TrashError(f"{path}: {exc}") from exc
    if completed.returncode != 0:
        raise TrashError(f"{path}: {completed.stderr.strip() or 'gio trash failed'}")
