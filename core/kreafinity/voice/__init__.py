"""Talking to the assistant: speech recognition and reading answers aloud.

- **Listening** (`stt`): on this computer with Whisper (``faster-whisper``,
  private, works offline), or through OpenAI's transcription API.
- **Speaking** (`tts`): on this computer with a Piper voice.

The voice engines are optional (``kreafinity voice install``, or Settings >
Voice): Kreafinity works without them. Models are downloaded once into
``~/.local/share/kreafinity/voice``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import sysconfig
from pathlib import Path

from kreafinity.paths import data_dir

#: Python packages of the voice engines (installed on demand).
PACKAGES = ("faster-whisper>=1.1", "piper-tts>=1.3")


class VoiceError(Exception):
    """Listening or speaking failed (no engine, model missing, unreadable audio…)."""


def voice_dir() -> Path:
    """Where the voice models are kept."""
    return data_dir() / "voice"


def engines_dir() -> Path:
    """Where the engines go when Kreafinity's own Python is read-only (system package)."""
    return voice_dir() / "site-packages"


def environment_writable() -> bool:
    """Whether packages can be installed into Kreafinity's own Python."""
    return os.access(sysconfig.get_paths()["purelib"], os.W_OK)


def use_engines_dir() -> None:
    """Make the engines installed for this user importable (after Kreafinity's own packages)."""
    folder = str(engines_dir())
    if Path(folder).is_dir() and folder not in sys.path:
        sys.path.append(folder)
        importlib.invalidate_caches()


def installed(module: str) -> bool:
    """Whether a voice engine's Python package is importable."""
    use_engines_dir()
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


use_engines_dir()  # engines installed for this user (system package installs)
