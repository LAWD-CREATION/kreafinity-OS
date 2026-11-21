"""Look of the desktop: light or dark theme, and wallpaper (KDE Plasma and GNOME).

- **KDE Plasma**: the color scheme (``plasma-apply-colorscheme``, the
  applications), the Plasma theme (``plasma-apply-desktoptheme``, panels and
  menus) and ``plasma-apply-wallpaperimage``. The layout is never touched.
- **GNOME** (and Ubuntu's desktop): ``gsettings``, the interface's preferred
  color scheme and the background picture.

Everything is per user: no administrator rights needed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from kreafinity.paths import xdg_data_home

THEMES = ("dark", "light", "kreafinity")
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".svg", ".avif"})
_TIMEOUT = 30

# Plasma: color scheme (applications) and Plasma theme (panels), per theme.
_PLASMA = {
    "dark": ("BreezeDark", "breeze-dark"),
    "light": ("BreezeLight", "breeze-light"),
    "kreafinity": ("Kreafinity", "breeze-dark"),
}
# GNOME: the interface's preferred color scheme.
_GNOME = {"dark": "prefer-dark", "light": "default", "kreafinity": "prefer-dark"}


class AppearanceError(Exception):
    """The look cannot be changed (unsupported desktop, missing theme, command failed…)."""


def desktop() -> str | None:
    """The desktop whose look can be changed: ``plasma``, ``gnome``, or None."""
    current = os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
    if "kde" in current and shutil.which("plasma-apply-colorscheme"):
        return "plasma"
    gnome_like = {"gnome", "ubuntu", "gnome-classic", "unity", "budgie"}
    if gnome_like & set(current) and shutil.which("gsettings"):
        return "gnome"
    return None


def _required_desktop() -> str:
    found = desktop()
    if found is None:
        raise AppearanceError(
            "changing the look is supported on KDE Plasma and GNOME only "
            f"(this desktop: {os.environ.get('XDG_CURRENT_DESKTOP') or 'unknown'})"
        )
    return found


def kreafinity_wallpaper() -> Path | None:
    """The Kreafinity wallpaper image (installed with the desktop integration)."""
    from kreafinity.api.web import plasma_assets

    candidates = [xdg_data_home() / "wallpapers" / "Kreafinity"]
    assets = plasma_assets()
    if assets is not None:
        candidates.append(assets / "wallpapers" / "Kreafinity")
    for folder in candidates:
        image = folder / "contents" / "images" / "1920x1080.jpg"
        if image.is_file():
            return image
    return None


def _color_scheme_exists(name: str) -> bool:
    # plasma-apply-colorscheme succeeds (exit code 0) even for an unknown scheme.
    folders = [xdg_data_home(), *(Path(p) for p in _data_dirs())]
    return any((folder / "color-schemes" / f"{name}.colors").is_file() for folder in folders)


def _data_dirs() -> list[str]:
    return [
        p for p in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":") if p
    ]


def _run(*command: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AppearanceError(f"{command[0]}: {exc}") from exc


def _check(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise AppearanceError(
            f"{result.args[0]} failed: {detail[-1] if detail else result.returncode}"
        )


def set_theme(theme: str) -> str:
    """Apply a light, dark or Kreafinity theme; return the desktop it was applied to.

    Raises:
        AppearanceError: Unknown theme, unsupported desktop, missing theme, failure.
    """
    if theme not in THEMES:
        raise AppearanceError(f"unknown theme {theme!r} ({', '.join(THEMES)})")
    found = _required_desktop()
    if found == "plasma":
        scheme, plasma_theme = _PLASMA[theme]
        if not _color_scheme_exists(scheme):
            hint = " (run: kreafinity desktop install)" if theme == "kreafinity" else ""
            raise AppearanceError(f"the color scheme {scheme} is not installed{hint}")
        _check(_run("plasma-apply-colorscheme", scheme))
        if shutil.which("plasma-apply-desktoptheme"):
            _run("plasma-apply-desktoptheme", plasma_theme)  # panels: best effort
    else:
        _check(
            _run("gsettings", "set", "org.gnome.desktop.interface", "color-scheme", _GNOME[theme])
        )
    return found


def set_wallpaper(image: Path) -> str:
    """Use `image` as the desktop wallpaper; return the desktop it was applied to.

    Raises:
        AppearanceError: Not an image, unsupported desktop, failure.
    """
    if not image.is_file() or image.suffix.lower() not in IMAGE_SUFFIXES:
        raise AppearanceError(f"not an image file: {image}")
    found = _required_desktop()
    if found == "plasma":
        if shutil.which("plasma-apply-wallpaperimage") is None:
            raise AppearanceError("plasma-apply-wallpaperimage is missing (Plasma 5.24 or later)")
        _check(_run("plasma-apply-wallpaperimage", str(image)))
    else:
        uri = image.resolve().as_uri()
        for key in ("picture-uri", "picture-uri-dark"):
            result = _run("gsettings", "set", "org.gnome.desktop.background", key, uri)
            if key == "picture-uri":  # picture-uri-dark only exists since GNOME 42
                _check(result)
    return found
