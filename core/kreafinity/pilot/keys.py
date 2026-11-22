"""Keys the pilot can press: names, X keysyms, and what is forbidden.

A combination is written like ``ctrl+s``, ``ctrl+shift+t`` or ``Enter``. The
same names work for both input backends: xdotool (X11) takes the keysym
names, the RemoteDesktop portal (Wayland) the keysym numbers.
"""

from __future__ import annotations

from dataclasses import dataclass


class ComboError(ValueError):
    """A key combination the pilot cannot press (unknown or forbidden)."""


# name -> (xdotool keysym name, keysym number)
_NAMED: dict[str, tuple[str, int]] = {
    "ctrl": ("ctrl", 0xFFE3),
    "shift": ("shift", 0xFFE1),
    "alt": ("alt", 0xFFE9),
    "altgr": ("ISO_Level3_Shift", 0xFE03),
    "super": ("super", 0xFFEB),
    "enter": ("Return", 0xFF0D),
    "tab": ("Tab", 0xFF09),
    "escape": ("Escape", 0xFF1B),
    "backspace": ("BackSpace", 0xFF08),
    "delete": ("Delete", 0xFFFF),
    "insert": ("Insert", 0xFF63),
    "home": ("Home", 0xFF50),
    "end": ("End", 0xFF57),
    "pageup": ("Prior", 0xFF55),
    "pagedown": ("Next", 0xFF56),
    "up": ("Up", 0xFF52),
    "down": ("Down", 0xFF54),
    "left": ("Left", 0xFF51),
    "right": ("Right", 0xFF53),
    "space": ("space", 0x0020),
    "menu": ("Menu", 0xFF67),
    "print": ("Print", 0xFF61),
}
_NAMED.update({f"f{n}": (f"F{n}", 0xFFBE + n - 1) for n in range(1, 13)})

_ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
    "meta": "super",
    "win": "super",
    "windows": "super",
    "cmd": "super",
    "return": "enter",
    "entrée": "enter",
    "entree": "enter",
    "esc": "escape",
    "échap": "escape",
    "echap": "escape",
    "del": "delete",
    "suppr": "delete",
    "pgup": "pageup",
    "page_up": "pageup",
    "prior": "pageup",
    "pgdown": "pagedown",
    "page_down": "pagedown",
    "next": "pagedown",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "espace": "space",
}
MODIFIERS = {"ctrl", "shift", "alt", "altgr", "super"}

# Kept for the user: Escape stops pilot mode; the others leave the session,
# switch to a text console or kill the graphical session.
_FORBIDDEN = {
    frozenset({"escape"}): "Escape is the user's key to stop pilot mode",
    frozenset({"ctrl", "alt", "delete"}): "Ctrl+Alt+Delete is not allowed",
    frozenset({"ctrl", "alt", "backspace"}): "Ctrl+Alt+Backspace is not allowed",
}


@dataclass(frozen=True, slots=True)
class Key:
    """One key of a combination."""

    name: str  # our name: ``ctrl``, ``enter``, ``a``, ``é``
    xdotool: str  # xdotool keysym name
    keysym: int  # X keysym number


def char_keysym(char: str) -> int:
    """The X keysym of a character (Latin-1 directly, else Unicode keysyms)."""
    code = ord(char)
    if char == "\n":
        return 0xFF0D
    if char == "\t":
        return 0xFF09
    if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF:
        return code
    return 0x01000000 + code


def parse_combo(text: str) -> list[Key]:
    """Parse ``ctrl+shift+t`` into keys (modifiers first, then one key).

    Raises:
        ComboError: Empty, unknown, several main keys, or forbidden.
    """
    parts = [part.strip() for part in text.strip().split("+")]
    if text.strip().endswith("++"):  # « ctrl++ » means Ctrl and the + key
        parts = [*parts[:-2], "+"]
    if not parts or any(not part for part in parts):
        raise ComboError(f"invalid key combination: {text!r}")
    keys: list[Key] = []
    for part in parts:
        lowered = _ALIASES.get(part.lower(), part.lower())
        if lowered in _NAMED:
            name, keysym = _NAMED[lowered]
            keys.append(Key(lowered, name, keysym))
        elif len(part) == 1 and part.isprintable():
            char = part.lower() if any(k.name in MODIFIERS for k in keys) else part
            keys.append(Key(char, f"0x{char_keysym(char):x}", char_keysym(char)))
        else:
            raise ComboError(f"unknown key {part!r} in {text!r}")
    main = [k for k in keys if k.name not in MODIFIERS]
    if len(main) > 1:
        raise ComboError(f"one key at a time with its modifiers: {text!r}")
    names = frozenset(k.name for k in keys)
    if names in _FORBIDDEN:
        raise ComboError(_FORBIDDEN[names])
    if {"ctrl", "alt"} <= names and any(k.name.startswith("f") and len(k.name) > 1 for k in main):
        raise ComboError("Ctrl+Alt+F… (text consoles) is not allowed")
    return sorted(keys, key=lambda k: k.name not in MODIFIERS)
