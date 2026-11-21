"""Installing the Plasma integration for the user (no root needed).

- the search plugin: « Demander à Kreafinity » in the start menu and KRunner;
- the taskbar widget opening the assistant panel, with a keyboard shortcut;
- « Kreafinity » in the file manager's right-click menu (Dolphin);
- the Kreafinity global theme: colors, wallpaper and a Windows-like layout.

Files go to ``~/.local/share`` (KDE's standard places for user add-ons);
`remove` takes them all away again.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError

from kreafinity.api.web import plasma_assets, web_root
from kreafinity.paths import xdg_data_home
from kreafinity.plasma.krunner import BUS_NAME
from kreafinity.system import integration
from kreafinity.system.integration import Report

WIDGET_ID = "org.kreafinity.assistant"
THEME_ID = "org.kreafinity.desktop"
COLOR_SCHEME = "Kreafinity"
WALLPAPER = "Kreafinity"
SUPPORTED_VERSIONS = (5, 6)
#: Opens the assistant, like Copilot's Windows+C (changeable in the widget's settings).
SHORTCUT = "Meta+C"
SERVICE_MENUS = ("kreafinity-files", "kreafinity-text", "kreafinity-folder")

# Plasma scripts (run inside plasmashell through D-Bus).
ADD_WIDGET_SCRIPT = """
var found = [];
panels().forEach(function (panel) { found = found.concat(panel.widgets("%(id)s")); });
var mayAdd = %(add)s;
var state = found.length > 0 ? "present" : "absent";
if (found.length == 0 && mayAdd) {
    var bottom = panels().filter(function (panel) { return panel.location == "bottom"; });
    var panel = bottom.length ? bottom[0] : panels()[0];
    if (panel) {
        found = [panel.addWidget("%(id)s")];
        state = "added";
    } else {
        state = "no-panel";
    }
}
// The keyboard shortcut, unless the user already chose one.
var shortcut = found.length > 0 && !found[0].globalShortcut;
if (shortcut) {
    found[0].globalShortcut = "%(shortcut)s";
}
print(state + (shortcut ? " shortcut" : ""));
"""
REMOVE_WIDGET_SCRIPT = """
panels().forEach(function (panel) {
    panel.widgets("%(id)s").forEach(function (widget) { widget.remove(); });
});
"""


class PlasmaError(Exception):
    """The Plasma integration cannot be installed (no Plasma, files missing…)."""


# --- Where things go ---------------------------------------------------------------------------


def krunner_plugin_path() -> Path:
    return xdg_data_home() / "krunner" / "dbusplugins" / "kreafinity.desktop"


def dbus_service_path() -> Path:
    return xdg_data_home() / "dbus-1" / "services" / f"{BUS_NAME}.service"


def widget_path() -> Path:
    return xdg_data_home() / "plasma" / "plasmoids" / WIDGET_ID


def theme_path() -> Path:
    return xdg_data_home() / "plasma" / "look-and-feel" / THEME_ID


def color_scheme_path() -> Path:
    return xdg_data_home() / "color-schemes" / f"{COLOR_SCHEME}.colors"


def wallpaper_path() -> Path:
    return xdg_data_home() / "wallpapers" / WALLPAPER


def service_menu_paths() -> list[Path]:
    folder = xdg_data_home() / "kio" / "servicemenus"
    return [folder / f"{name}.desktop" for name in SERVICE_MENUS]


def installed_paths() -> list[Path]:
    return [
        *service_menu_paths(),
        krunner_plugin_path(),
        dbus_service_path(),
        widget_path(),
        theme_path(),
        color_scheme_path(),
        wallpaper_path(),
    ]


def is_installed() -> bool:
    return krunner_plugin_path().exists() or widget_path().exists()


# --- Plasma itself ----------------------------------------------------------------------------


def plasma_version() -> int | None:
    """Major version of the user's Plasma (5 or 6), or None without Plasma."""
    session = os.environ.get("KDE_SESSION_VERSION", "")
    if session.isdigit():
        return int(session)
    plasmashell = shutil.which("plasmashell")
    if plasmashell is None:
        return None
    try:
        result = subprocess.run(
            [plasmashell, "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"plasmashell (\d+)\.", result.stdout + result.stderr)
    return int(match.group(1)) if match else None


def _run(*command: str) -> bool:
    """Run a Plasma tool; False if it is missing or fails."""
    if shutil.which(command[0]) is None:
        return False
    try:
        result = subprocess.run(command, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


async def _evaluate_script(script: str) -> str | None:
    """Run a script in plasmashell (its scripting console); None if it is not running."""
    try:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
    except (OSError, DBusError, ValueError):
        return None
    try:
        reply = await bus.call(
            Message(
                destination="org.kde.plasmashell",
                path="/PlasmaShell",
                interface="org.kde.PlasmaShell",
                member="evaluateScript",
                signature="s",
                body=[script],
            )
        )
    finally:
        bus.disconnect()
        await bus.wait_for_disconnect()  # type: ignore[no-untyped-call]
    if reply is None or reply.message_type is MessageType.ERROR:
        return None
    return str(reply.body[0]) if reply.body else ""


def evaluate_script(script: str) -> str | None:
    """Synchronous `_evaluate_script` (for the command line)."""
    return asyncio.run(_evaluate_script(script))


# --- Install / remove ---------------------------------------------------------------------------


def install(version: int, *, port: int, add_widget: bool = True) -> Report:
    """Install the search plugin, the taskbar widget and the theme files for `version`.

    With `add_widget`, the widget is also put on the taskbar (an update only
    refreshes the files: a widget the user removed stays removed).

    Raises:
        PlasmaError: Unsupported version, or the files are missing.
    """
    if version not in SUPPORTED_VERSIONS:
        raise PlasmaError(f"Plasma {version} is not supported (5 or 6 needed)")
    assets = plasma_assets()
    if assets is None:
        raise PlasmaError("the Plasma files of Kreafinity are missing from this installation")
    report = Report()

    # Search: plugin description, and D-Bus starting the runner when needed.
    plugin = krunner_plugin_path()
    plugin.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(assets / "krunner" / "kreafinity.desktop", plugin)
    service = dbus_service_path()
    service.parent.mkdir(parents=True, exist_ok=True)
    command = shlex.join([sys.executable, "-m", "kreafinity.plasma.krunner"])
    service.write_text(f"[D-BUS Service]\nName={BUS_NAME}\nExec={command}\n", encoding="utf-8")
    report.add("plasma.search", path=str(plugin))

    # Taskbar widget, pointed at this daemon.
    widget = widget_path()
    _replace_tree(assets / f"plasmoid-{version}" / WIDGET_ID, widget)
    config = widget / "contents" / "config" / "main.xml"
    url = escape(f"http://127.0.0.1:{port}")
    config.write_text(
        re.sub(r"<default>http://[^<]*</default>", f"<default>{url}</default>", config.read_text()),
        encoding="utf-8",
    )
    report.add("plasma.widget", path=str(widget))

    # File manager: « Kreafinity » submenu (the files must be executable for KDE to use them).
    menu_command = shlex.join([sys.executable, "-m", "kreafinity.plasma.files_menu"])
    for name, target in zip(SERVICE_MENUS, service_menu_paths(), strict=True):
        text = (assets / "servicemenus" / f"{name}.desktop").read_text(encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text.replace("@COMMAND@", menu_command), encoding="utf-8")
        target.chmod(0o755)
    report.add("plasma.files_menu")

    # Theme: global theme, colors and wallpaper (applied by `apply_theme`).
    _replace_tree(assets / "look-and-feel" / THEME_ID, theme_path())
    color_scheme_path().parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(assets / "color-schemes" / f"{COLOR_SCHEME}.colors", color_scheme_path())
    _replace_tree(assets / "wallpapers" / WALLPAPER, wallpaper_path())
    report.add("plasma.theme_files")

    # The search and the menu use the Kreafinity icon.
    icon = integration.icon_path()
    web = web_root()
    logo = web / "img" / "logo.svg" if web else None
    if not icon.exists() and logo is not None and logo.is_file():
        icon.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(logo, icon)

    _run(f"kquitapp{version}", "krunner")  # restarted on next use, with the new plugin
    # An update only gives the shortcut to a widget that has none: a widget
    # the user removed stays removed.
    script = ADD_WIDGET_SCRIPT % {
        "id": WIDGET_ID,
        "shortcut": SHORTCUT,
        "add": "true" if add_widget else "false",
    }
    answer = (evaluate_script(script) or "").split()
    if not add_widget:
        if "shortcut" in answer:
            report.add("plasma.shortcut", shortcut=SHORTCUT)
        return report
    if answer and answer[0] in ("added", "present"):
        report.add("plasma.widget_added")
        if "shortcut" in answer:
            report.add("plasma.shortcut", shortcut=SHORTCUT)
    else:
        report.add("plasma.widget_manual")
    return report


def apply_theme(version: int, *, layout: bool) -> Report:
    """Apply the Kreafinity look: colors and wallpaper, and with `layout` the
    Windows-like taskbar (it replaces the current panels)."""
    report = Report()
    command = ["plasma-apply-lookandfeel", "--apply", THEME_ID]
    if layout:
        command.append("--resetLayout")
    if _run(*command):
        report.add("plasma.layout_applied" if layout else "plasma.theme_applied")
    if _run("plasma-apply-colorscheme", COLOR_SCHEME):
        report.add("plasma.colors_applied")
    image = wallpaper_path() / "contents" / "images" / "1920x1080.jpg"
    if _run("plasma-apply-wallpaperimage", str(image)):
        report.add("plasma.wallpaper_applied")
    return report


def remove(version: int | None) -> Report:
    """Remove everything `install` added; go back to Breeze if our theme is in use."""
    report = Report()
    evaluate_script(REMOVE_WIDGET_SCRIPT % {"id": WIDGET_ID})
    if _current_color_scheme(version) == COLOR_SCHEME:
        _run("plasma-apply-colorscheme", "BreezeDark")
    for path in installed_paths():
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        else:
            continue
        report.add("setup.removed", path=str(path))
    if version is not None:
        _run(f"kquitapp{version}", "krunner")
    return report


def _current_color_scheme(version: int | None) -> str | None:
    tool = shutil.which(f"kreadconfig{version or 6}") or shutil.which("kreadconfig5")
    if tool is None:
        return None
    try:
        result = subprocess.run(
            [tool, "--file", "kdeglobals", "--group", "General", "--key", "ColorScheme"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def _replace_tree(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
