"""Integration with the user's desktop session (freedesktop.org and systemd).

- a systemd user service starting the daemon with the graphical session;
- an entry « Kreafinity » in the applications menu, with its icon;
- the desktop bubble shown at login (XDG autostart).

Everything is per user, in ``~/.config`` and ``~/.local/share``: no root
access is needed, and `uninstall` removes exactly what `install` added.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from kreafinity.paths import xdg_config_home, xdg_data_home

SERVICE_NAME = "kreafinity.service"
SERVICE_DOCUMENTATION = "https://github.com/LAWD-CREATION/kreafinity-OS"
#: Variables of the graphical session the daemon needs to open applications.
SESSION_VARIABLES = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_CURRENT_DESKTOP",
    "XDG_SESSION_TYPE",
    "DBUS_SESSION_BUS_ADDRESS",
)


def service_path() -> Path:
    return xdg_config_home() / "systemd" / "user" / SERVICE_NAME


def menu_entry_path() -> Path:
    return xdg_data_home() / "applications" / "kreafinity.desktop"


def icon_path() -> Path:
    return xdg_data_home() / "icons" / "hicolor" / "scalable" / "apps" / "kreafinity.svg"


def autostart_path() -> Path:
    return xdg_config_home() / "autostart" / "kreafinity-bubble.desktop"


def python_command(*arguments: str) -> list[str]:
    """Run Kreafinity with the interpreter it is installed in (``python -m ...``)."""
    return [sys.executable, "-m", *arguments]


# --- File contents ---------------------------------------------------------------------------


def desktop_quote(argument: str) -> str:
    """Quote an argument for the Exec key of a desktop entry."""
    if argument and not any(char in argument for char in " \t\n\"'\\<>~|&;$*?#()`"):
        return argument
    escaped = "".join("\\" + char if char in '"`$\\' else char for char in argument)
    return f'"{escaped}"'


def service_unit(command: Sequence[str]) -> str:
    """The systemd user unit running the daemon with `command`.

    It starts with the graphical session (GNOME, KDE…) so that the daemon
    can open applications, and stops with it.
    """
    return (
        "[Unit]\n"
        "Description=Kreafinity, l'assistant système (démon local)\n"
        f"Documentation={SERVICE_DOCUMENTATION}\n"
        "PartOf=graphical-session.target\n"
        "After=graphical-session.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={' '.join(_systemd_quote(part) for part in command)}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "\n"
        "[Install]\n"
        "WantedBy=graphical-session.target\n"
    )


def _systemd_quote(argument: str) -> str:
    if argument and not any(char in argument for char in " \t\"'\\"):
        return argument
    return '"' + argument.replace("\\", "\\\\").replace('"', '\\"') + '"'


def menu_entry(command: Sequence[str], icon: Path | None) -> str:
    """The applications menu entry opening the web interface."""
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Kreafinity\n"
        "GenericName=Assistant IA\n"
        "GenericName[en]=AI assistant\n"
        "Comment=Parlez à votre ordinateur\n"
        "Comment[en]=Talk to your computer\n"
        f"Exec={' '.join(desktop_quote(part) for part in command)}\n"
        f"Icon={icon if icon else 'kreafinity'}\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
        "Keywords=IA;assistant;AI;chat;Kreafinity;\n"
        "StartupNotify=false\n"
    )


def autostart_entry(command: Sequence[str], comment: str) -> str:
    """The XDG autostart entry showing the bubble at login."""
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Kreafinity\n"
        f"Comment={comment}\n"
        f"Exec={' '.join(desktop_quote(part) for part in command)}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


# --- systemd ----------------------------------------------------------------------------------


def systemctl(*arguments: str) -> bool:
    """Run ``systemctl --user`` with `arguments`; False if it fails or is unavailable."""
    executable = shutil.which("systemctl")
    if executable is None:
        return False
    try:
        result = subprocess.run(
            [executable, "--user", *arguments],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def systemd_available() -> bool:
    """Whether a systemd user manager runs for this user."""
    return systemctl("is-system-running") or systemctl("show-environment")


# --- Install / uninstall ------------------------------------------------------------------------


@dataclass
class Report:
    """What `install` or `uninstall` did, as translation keys and values."""

    done: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def add(self, key: str, **values: str) -> None:
        self.done.append((key, values))


def install(
    *, bubble: bool, icon_source: Path | None, autostart_comment: str, restart: bool = True
) -> Report:
    """Add the service, the menu entry and (if `bubble`) the bubble at login.

    With `restart`, the daemon is (re)started in the current graphical session.
    """
    report = Report()

    unit = service_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(service_unit(python_command("kreafinity.api.server")), encoding="utf-8")
    report.add("setup.service_file", path=str(unit))
    if systemd_available():
        systemctl("daemon-reload")
        if systemctl("enable", SERVICE_NAME):
            report.add("setup.service_enabled")
        in_session = systemctl("is-active", "graphical-session.target")
        if restart and in_session:
            import_session_environment()
            if systemctl("restart", SERVICE_NAME):
                report.add("setup.service_started")
        elif not in_session:
            report.add("setup.service_next_login")
    else:
        report.add("setup.no_systemd")

    icon = None
    if icon_source is not None and icon_source.is_file():
        icon = icon_path()
        icon.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(icon_source, icon)
    entry = menu_entry_path()
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(menu_entry(python_command("kreafinity.cli", "ui"), icon), encoding="utf-8")
    report.add("setup.menu_entry", path=str(entry))

    if bubble:
        autostart = autostart_path()
        autostart.parent.mkdir(parents=True, exist_ok=True)
        command = python_command("kreafinity.cli", "bubble")
        autostart.write_text(autostart_entry(command, autostart_comment), encoding="utf-8")
        report.add("setup.bubble_autostart", path=str(autostart))
    return report


def uninstall() -> Report:
    """Remove what `install` added (the configuration and history are kept)."""
    report = Report()
    if service_path().exists() and systemd_available():
        systemctl("disable", "--now", SERVICE_NAME)
    for path in (service_path(), menu_entry_path(), icon_path(), autostart_path()):
        if path.exists():
            path.unlink()
            report.add("setup.removed", path=str(path))
    if systemd_available():
        systemctl("daemon-reload")
    return report


def import_session_environment() -> None:
    """Give the systemd user manager this session's display variables.

    Desktops like GNOME and KDE do it themselves; others do not, and a
    daemon started by systemd would then be unable to open applications.
    """
    names = [name for name in SESSION_VARIABLES if os.environ.get(name)]
    if names:
        systemctl("import-environment", *names)


def service_installed() -> bool:
    return service_path().is_file()


def processes_running_from(root: Path) -> list[psutil.Process]:
    """This user's processes started from files under `root` (not the current one).

    With `root` the installation's environment, these are its daemon and
    bubble — and never those of another copy, such as a development checkout.
    """
    me = os.getpid()
    uid = os.getuid()
    prefix = str(root).rstrip("/") + "/"
    found = []
    for process in psutil.process_iter(["pid", "uids", "cmdline"]):
        info = process.info
        if info["pid"] == me or not info["uids"] or info["uids"].real != uid:
            continue
        if any(argument.startswith(prefix) for argument in info["cmdline"] or []):
            found.append(process)
    return found
