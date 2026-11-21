"""Reading answers aloud with a Piper voice, on this computer.

Each language has a voice (``fr_FR-siwis-medium`` for French, about 60 MB),
downloaded once. Answers are cleaned of their formatting first (Markdown,
code, links) so the voice does not read out symbols.
"""

from __future__ import annotations

import io
import re
import threading
import wave
from pathlib import Path
from typing import Any

from kreafinity.voice import VoiceError, installed, voice_dir

VOICES = {
    "fr": "fr_FR-siwis-medium",
    "en": "en_US-lessac-medium",
    "es": "es_ES-davefx-medium",
    "de": "de_DE-thorsten-medium",
    "it": "it_IT-paola-medium",
}
#: Longest text read aloud (an answer is cut at a sentence end before this).
MAX_CHARACTERS = 2000


def piper_dir() -> Path:
    return voice_dir() / "piper"


def available() -> bool:
    """Whether Piper can run here (``piper-tts`` installed)."""
    return installed("piper")


def voice_name(language: str) -> str:
    return VOICES.get(language, VOICES["en"])


def voice_ready(language: str) -> bool:
    """Whether the voice for `language` was downloaded."""
    name = voice_name(language)
    folder = piper_dir()
    return (folder / f"{name}.onnx").is_file() and (folder / f"{name}.onnx.json").is_file()


def download_voice(language: str) -> None:
    """Download the voice for `language` (blocking).

    Raises:
        VoiceError: Piper missing, or the download failed.
    """
    if not available():
        raise VoiceError("speech synthesis is not installed (kreafinity voice install)")
    from piper.download_voices import download_voice as piper_download

    piper_dir().mkdir(parents=True, exist_ok=True)
    try:
        piper_download(voice_name(language), piper_dir())
    except Exception as exc:  # urllib errors, missing voice
        raise VoiceError(f"cannot download the voice: {exc}") from exc


_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_MARKS = re.compile(r"[*_`#>|~]+")
_BULLET = re.compile(r"^\s*(?:[-+•]|\d+[.)])\s+", re.MULTILINE)


def speakable(text: str) -> str:
    """`text` as it should be read: no formatting, no code, bounded length."""
    text = _CODE_BLOCK.sub(" (code) ", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(" ", text)
    text = _BULLET.sub("", text)
    text = _MARKS.sub(" ", text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    # A list item or a title ends a sentence, even without a full stop.
    text = " ".join(
        line if line.endswith((".", "!", "?", ":", ";")) else f"{line}." for line in lines if line
    )
    if len(text) > MAX_CHARACTERS:
        cut = max(text.rfind(end, 0, MAX_CHARACTERS) for end in (". ", "! ", "? "))
        text = text[: cut + 1] if cut > 0 else text[:MAX_CHARACTERS]
    return text.strip()


class Speaker:
    """Piper voices, loaded once and shared."""

    def __init__(self) -> None:
        self._voices: dict[str, Any] = {}
        self._lock = threading.Lock()

    def speak(self, text: str, language: str) -> bytes:
        """`text` read aloud, as a WAV file (blocking: run it in a thread).

        Raises:
            VoiceError: Piper or the voice missing, nothing to read.
        """
        if not available():
            raise VoiceError("speech synthesis is not installed (kreafinity voice install)")
        if not voice_ready(language):
            raise VoiceError("the voice is not downloaded yet")
        spoken = speakable(text)
        if not spoken:
            raise VoiceError("nothing to read")
        name = voice_name(language)
        with self._lock:
            if name not in self._voices:
                from piper import PiperVoice

                self._voices[name] = PiperVoice.load(piper_dir() / f"{name}.onnx")
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                self._voices[name].synthesize_wav(spoken, wav)
        return output.getvalue()
