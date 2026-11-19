"""Translations of user-facing texts.

Catalogs are JSON files in ``kreafinity/locales/`` (``fr.json``, ``en.json``…),
mapping message keys to texts with ``{placeholders}``. French is the
reference language and the fallback for missing keys.
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import Any

DEFAULT_LANGUAGE = "fr"

_language = DEFAULT_LANGUAGE


@cache
def _catalog(language: str) -> dict[str, str]:
    try:
        text = resources.files("kreafinity").joinpath(f"locales/{language}.json").read_text("utf-8")
    except FileNotFoundError:
        return {}
    data: dict[str, str] = json.loads(text)
    return data


def set_language(language: str) -> None:
    """Choose the language of the texts returned by `t`."""
    global _language
    _language = language


def get_language() -> str:
    """The current language."""
    return _language


def _lookup(key: str) -> str | None:
    return _catalog(_language).get(key) or _catalog(DEFAULT_LANGUAGE).get(key)


def t(key: str, **values: Any) -> str:
    """Return the text for `key` in the current language, filled with `values`.

    With a ``count`` value, ``key.one`` or ``key.other`` is used when the
    catalog defines them (in French, 0 and 1 take the singular).
    """
    text = None
    count = values.get("count")
    if isinstance(count, int):
        singular = count in (0, 1) if _language == "fr" else count == 1
        text = _lookup(f"{key}.{'one' if singular else 'other'}")
    text = text or _lookup(key) or key
    return text.format(**values) if values else text
