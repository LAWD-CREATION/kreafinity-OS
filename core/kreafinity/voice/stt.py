"""Speech recognition: what the user said, as text.

- ``local``: Whisper on this computer (``faster-whisper``). Private, works
  offline; the model (``small`` by default, about 460 MB) is downloaded once.
- ``openai``: OpenAI's transcription API, with the user's OpenAI key.
"""

from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import Any

from kreafinity.voice import VoiceError, installed, voice_dir
from kreafinity.voice.audio import MAX_BYTES, decode

ENGINES = ("local", "openai", "off")
#: Whisper sizes, from fastest to most accurate.
MODELS = ("tiny", "base", "small", "medium")
OPENAI_MODEL = "gpt-4o-mini-transcribe"


def whisper_dir() -> Path:
    return voice_dir() / "whisper"


def local_available() -> bool:
    """Whether Whisper can run here (``faster-whisper`` installed)."""
    return installed("faster_whisper")


def model_ready(model: str) -> bool:
    """Whether the Whisper `model` was downloaded."""
    snapshots = whisper_dir() / f"models--Systran--faster-whisper-{model}" / "snapshots"
    return any(snapshots.glob("*/model.bin")) if snapshots.is_dir() else False


class LocalTranscriber:
    """Whisper models, loaded once (loading takes seconds) and shared."""

    def __init__(self) -> None:
        self._models: dict[str, Any] = {}
        self._lock = threading.Lock()

    def load(self, model: str) -> Any:
        """The Whisper `model`, downloaded first if needed (slow the first time)."""
        if model not in MODELS:
            raise VoiceError(f"unknown Whisper model {model!r} ({', '.join(MODELS)})")
        if not local_available():
            raise VoiceError("speech recognition is not installed (kreafinity voice install)")
        with self._lock:
            if model not in self._models:
                from faster_whisper import WhisperModel

                self._models[model] = WhisperModel(
                    model, device="cpu", compute_type="int8", download_root=str(whisper_dir())
                )
            return self._models[model]

    def transcribe(self, data: bytes, *, model: str, language: str) -> str:
        """The text of the recording `data` (blocking: run it in a thread).

        Raises:
            VoiceError: Engine or model missing, unreadable recording.
        """
        if not model_ready(model):
            raise VoiceError("the speech recognition model is not downloaded yet")
        samples = decode(data)
        whisper = self.load(model)
        with self._lock:  # one recognition at a time: they use every core
            segments, _ = whisper.transcribe(
                samples, language=language or None, vad_filter=True, beam_size=1
            )
            return " ".join(segment.text.strip() for segment in segments).strip()


def transcribe_openai(data: bytes, *, api_key: str, language: str, filename: str) -> str:
    """The text of the recording, by OpenAI's transcription API (blocking).

    Raises:
        VoiceError: The API refused or could not be reached.
    """
    import openai

    if len(data) > MAX_BYTES:
        raise VoiceError("recording too large")
    client = openai.OpenAI(api_key=api_key, max_retries=1, timeout=60)
    try:
        result = client.audio.transcriptions.create(
            model=OPENAI_MODEL,
            file=(filename, io.BytesIO(data)),
            language=language or openai.omit,
        )
    except openai.OpenAIError as exc:
        raise VoiceError(f"OpenAI transcription failed: {exc}") from exc
    finally:
        client.close()
    return str(result.text).strip()
