"""Applications, launching and processes on a freedesktop.org desktop."""

from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import psutil

from kreafinity.paths import xdg_data_home

# Exported applications of Flatpak and Snap, not always listed in XDG_DATA_DIRS.
EXTRA_DATA_DIRS = (
    "~/.local/share/flatpak/exports/share",
    "/var/lib/flatpak/exports/share",
    "/var/lib/snapd/desktop",
)


class LaunchError(Exception):
    """A program or application could not be started."""


@dataclass(frozen=True, slots=True)
class DesktopApp:
    """An installed application (a ``.desktop`` file).

    Attributes:
        id: Desktop file id (``org.gnome.Nautilus``, ``firefox``…).
        name: Name in the user's language.
        comment: Short description, if any.
        keywords: Search keywords, in the user's language.
        path: Location of the ``.desktop`` file.
    """

    id: str
    name: str
    comment: str | None
    keywords: tuple[str, ...]
    path: Path

    def matches(self, query: str) -> bool:
        """Whether `query` appears in the name, id, description or keywords."""
        query = query.casefold()
        haystack = [self.id, self.name, self.comment or "", *self.keywords]
        return any(query in text.casefold() for text in haystack)


def application_dirs() -> list[Path]:
    """Folders holding ``.desktop`` files, by decreasing priority."""
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    bases = [xdg_data_home(), *(Path(p) for p in data_dirs.split(":") if p)]
    bases += [Path(p).expanduser() for p in EXTRA_DATA_DIRS]
    result: list[Path] = []
    for base in bases:
        directory = base / "applications"
        if directory not in result:
            result.append(directory)
    return result


def list_applications(language: str = "fr") -> list[DesktopApp]:
    """Applications the user can launch, sorted by name.

    Hidden entries (``NoDisplay``, ``Hidden``) are left out. When the same id
    exists in several folders, the highest-priority one wins.
    """
    apps: dict[str, DesktopApp] = {}
    for directory in application_dirs():
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.desktop")):
            app_id = str(path.relative_to(directory).with_suffix("")).replace(os.sep, "-")
            if app_id in apps:
                continue
            app = _parse_desktop_file(path, app_id, language)
            # Record hidden ones too, so they still mask lower-priority copies.
            apps[app_id] = app if app is not None else _HIDDEN
    return sorted(
        (app for app in apps.values() if app is not _HIDDEN), key=lambda a: a.name.casefold()
    )


def find_application(query: str, language: str = "fr") -> DesktopApp | None:
    """Find an application by desktop id or exact name (case-insensitive)."""
    wanted = query.casefold().removesuffix(".desktop")
    apps = list_applications(language)
    for app in apps:
        if app.id.casefold() == wanted:
            return app
    for app in apps:
        if app.name.casefold() == wanted:
            return app
    return None


_HIDDEN = DesktopApp(id="", name="", comment=None, keywords=(), path=Path())


def _parse_desktop_file(path: Path, app_id: str, language: str) -> DesktopApp | None:
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # keys are case-sensitive
    try:
        parser.read(path, encoding="utf-8")
        entry = parser["Desktop Entry"]
    except (configparser.Error, KeyError, UnicodeDecodeError, OSError):
        return None
    if entry.get("Type") != "Application":
        return None
    if entry.get("NoDisplay", "").lower() == "true" or entry.get("Hidden", "").lower() == "true":
        return None

    def localized(key: str) -> str | None:
        for candidate in (f"{key}[{language}]", *_regional_keys(entry, key, language), key):
            if entry.get(candidate):
                return entry[candidate]
        return None

    name = localized("Name")
    if not name:
        return None
    keywords = tuple(k for k in (localized("Keywords") or "").split(";") if k)
    return DesktopApp(
        id=app_id, name=name, comment=localized("Comment"), keywords=keywords, path=path
    )


def _regional_keys(entry: configparser.SectionProxy, key: str, language: str) -> list[str]:
    prefix = f"{key}[{language}_"
    return sorted(k for k in entry if k.startswith(prefix))


def find_program(*names: str) -> str | None:
    """Path of the first of `names` found in ``PATH``."""
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def launch(argv: Sequence[str], *, grace_period: float = 2.0) -> None:
    """Start a program detached from Kreafinity.

    Waits `grace_period` seconds: if the program exits with an error by
    then, `LaunchError` is raised with its error message.
    """
    with tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise LaunchError(f"{argv[0]}: {exc.strerror}") from exc
        try:
            code = process.wait(timeout=grace_period)
        except subprocess.TimeoutExpired:
            # Still running: it started. Reap it in the background when it exits.
            threading.Thread(target=process.wait, daemon=True).start()
            return
        if code != 0:
            stderr.seek(0)
            message = stderr.read().decode(errors="replace").strip()
            raise LaunchError(message[:300] or f"{argv[0]} exited with code {code}")


def open_with_default_app(target: str) -> None:
    """Open a file or URL with the user's default application."""
    gio = find_program("gio")
    if gio:
        launch([gio, "open", target])
        return
    xdg_open = find_program("xdg-open")
    if xdg_open:
        launch([xdg_open, target])
        return
    raise LaunchError("No program available to open files (install 'gio' or 'xdg-open')")


def launch_application(app: DesktopApp) -> None:
    """Start an installed application."""
    gio = find_program("gio")
    if gio:
        launch([gio, "launch", str(app.path)])
        return
    gtk_launch = find_program("gtk-launch")
    if gtk_launch:
        launch([gtk_launch, app.id])
        return
    raise LaunchError("No program available to start applications (install 'gio')")


def find_processes(name: str) -> list[psutil.Process]:
    """The current user's processes whose program is called `name` (case-insensitive).

    Kreafinity itself and its parent processes (the terminal running it)
    are never included.
    """
    wanted = name.casefold().removesuffix(".desktop")
    me = psutil.Process()
    excluded = {me.pid, *(p.pid for p in me.parents())}
    uid = os.getuid()
    found = []
    for process in psutil.process_iter(["pid", "name", "exe", "cmdline", "uids"]):
        info = process.info
        if info["pid"] in excluded or not info["uids"] or info["uids"].real != uid:
            continue
        cmdline = info["cmdline"] or []
        names = {
            (info["name"] or "").casefold(),
            os.path.basename(info["exe"] or "").casefold(),
            os.path.basename(cmdline[0]).casefold() if cmdline else "",
        }
        if wanted in names:
            found.append(process)
    return found


def terminate(
    processes: Sequence[psutil.Process], *, timeout: float = 5.0
) -> tuple[list[psutil.Process], list[psutil.Process]]:
    """Ask processes to quit (SIGTERM, never SIGKILL); return (ended, still running)."""
    for process in processes:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied as exc:
            raise LaunchError(f"Not allowed to close process {process.pid}") from exc
    gone, alive = psutil.wait_procs(list(processes), timeout=timeout)
    return list(gone), list(alive)
