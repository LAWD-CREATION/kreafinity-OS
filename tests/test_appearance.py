"""Desktop look: light/dark/Kreafinity theme and wallpaper (Plasma and GNOME)."""

import json
import os
from pathlib import Path

import pytest

from kreafinity.paths import xdg_data_home
from kreafinity.system import appearance
from kreafinity.tools import ToolContext, ToolError, ToolRegistry

COMMANDS = (
    "plasma-apply-colorscheme",
    "plasma-apply-desktoptheme",
    "plasma-apply-wallpaperimage",
    "gsettings",
)


@pytest.fixture
def desktop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake Plasma and GNOME commands, logging their arguments; Breeze schemes installed."""
    folder = tmp_path / "bin"
    folder.mkdir()
    log = tmp_path / "calls.log"
    for name in COMMANDS:
        program = folder / name
        program.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n')
        program.chmod(0o755)
    system = tmp_path / "share"
    (system / "color-schemes").mkdir(parents=True)
    for scheme in ("BreezeDark", "BreezeLight"):
        (system / "color-schemes" / f"{scheme}.colors").write_text("[General]\n")
    monkeypatch.setenv("XDG_DATA_DIRS", str(system))
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    return log


def calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_desktop_detection(desktop: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert appearance.desktop() == "plasma"
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    assert appearance.desktop() == "gnome"
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "XFCE")
    assert appearance.desktop() is None
    with pytest.raises(appearance.AppearanceError, match="KDE Plasma and GNOME only"):
        appearance.set_theme("dark")


def test_plasma_themes(desktop: Path) -> None:
    assert appearance.set_theme("dark") == "plasma"
    assert appearance.set_theme("light") == "plasma"
    assert calls(desktop) == [
        "plasma-apply-colorscheme BreezeDark",
        "plasma-apply-desktoptheme breeze-dark",
        "plasma-apply-colorscheme BreezeLight",
        "plasma-apply-desktoptheme breeze-light",
    ]
    # plasma-apply-colorscheme "succeeds" for unknown schemes: checked beforehand.
    with pytest.raises(appearance.AppearanceError, match="kreafinity desktop install"):
        appearance.set_theme("kreafinity")
    scheme = xdg_data_home() / "color-schemes" / "Kreafinity.colors"
    scheme.parent.mkdir(parents=True)
    scheme.write_text("[General]\n")
    appearance.set_theme("kreafinity")
    assert calls(desktop)[-2] == "plasma-apply-colorscheme Kreafinity"
    with pytest.raises(appearance.AppearanceError, match="unknown theme"):
        appearance.set_theme("pink")


def test_gnome(desktop: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    image = tmp_path / "plage.jpg"
    image.write_bytes(b"\xff\xd8")
    appearance.set_theme("dark")
    appearance.set_wallpaper(image)
    assert calls(desktop) == [
        "gsettings set org.gnome.desktop.interface color-scheme prefer-dark",
        f"gsettings set org.gnome.desktop.background picture-uri {image.as_uri()}",
        f"gsettings set org.gnome.desktop.background picture-uri-dark {image.as_uri()}",
    ]


def test_failures(desktop: Path, tmp_path: Path) -> None:
    (tmp_path / "bin" / "plasma-apply-wallpaperimage").write_text(
        "#!/bin/sh\necho 'Plasma is not running' >&2\nexit 1\n"
    )
    image = tmp_path / "plage.png"
    image.write_bytes(b"\x89PNG")
    with pytest.raises(appearance.AppearanceError, match="failed: Plasma is not running"):
        appearance.set_wallpaper(image)
    with pytest.raises(appearance.AppearanceError, match="not an image"):
        appearance.set_wallpaper(tmp_path / "notes.txt")


async def test_tool(desktop: Path, context: ToolContext, home: Path) -> None:
    set_appearance = ToolRegistry.load(["set_appearance"]).get("set_appearance")
    assert set_appearance is not None and set_appearance.risk.value == "confirm"
    photo = home / "Images" / "vacances.jpg"
    photo.parent.mkdir(exist_ok=True)
    photo.write_bytes(b"\xff\xd8")
    arguments = {"theme": "dark", "wallpaper": "~/Images/vacances.jpg"}

    preview = await set_appearance.preview(arguments, context)
    assert preview.summary == "Passer le bureau en thème sombre et changer le fond d'écran"
    assert preview.details == (str(photo),)
    result = json.loads((await set_appearance.run(arguments, context)).content)
    assert result == {"desktop": "plasma", "theme": "dark", "wallpaper": str(photo)}
    assert f"plasma-apply-wallpaperimage {photo}" in calls(desktop)

    kreafinity = await set_appearance.preview({"wallpaper": "Kreafinity"}, context)
    assert kreafinity.details[0].endswith("1920x1080.jpg")  # the shipped wallpaper


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({}, "Give a theme, a wallpaper, or both"),
        ({"theme": "pink"}, "Unknown theme"),
        ({"wallpaper": "/etc/passwd"}, "outside"),  # only the allowed folders
        ({"wallpaper": "~/absent.jpg"}, "Not found"),
        ({"wallpaper": "~/notes.txt"}, "Not an image"),
    ],
)
async def test_tool_refusals(
    desktop: Path, context: ToolContext, home: Path, arguments: dict[str, str], error: str
) -> None:
    (home / "notes.txt").write_text("…")
    set_appearance = ToolRegistry.load(["set_appearance"]).get("set_appearance")
    assert set_appearance is not None
    with pytest.raises(ToolError, match=error):
        await set_appearance.preview(arguments, context)
    assert calls(desktop) == []


async def test_tool_on_another_desktop(
    desktop: Path, context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "XFCE")
    set_appearance = ToolRegistry.load(["set_appearance"]).get("set_appearance")
    assert set_appearance is not None
    with pytest.raises(ToolError, match="KDE Plasma and GNOME only"):
        await set_appearance.preview({"theme": "dark"}, context)
