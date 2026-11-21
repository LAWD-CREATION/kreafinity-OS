"""The daemon's voice: state, preparation (install and downloads), listening, speaking."""

from __future__ import annotations

import asyncio
import importlib
import logging
import subprocess
import sys
from collections.abc import Callable, Sequence
from typing import Any

from kreafinity.voice import (
    PACKAGES,
    VoiceError,
    engines_dir,
    environment_writable,
    stt,
    tts,
    use_engines_dir,
)

logger = logging.getLogger(__name__)

Event = dict[str, Any]


def pip_install(packages: Sequence[str]) -> None:
    """Install the voice engines into Kreafinity's own Python (blocking).

    A system package keeps Kreafinity's Python read-only: the engines then go
    to the user's own folder (``~/.local/share/kreafinity/voice/site-packages``).
    """
    target = [] if environment_writable() else ["--target", str(engines_dir())]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            *target,
            *packages,
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise VoiceError(f"installation failed: {detail[-1] if detail else result.returncode}")
    importlib.invalidate_caches()
    use_engines_dir()


class VoiceManager:
    """Speech recognition and synthesis for the daemon.

    Args:
        settings: Returns the current settings (``voice_input``, ``voice_model``,
            ``voice_reply``, ``language``).
        publish: Sends a live event to the interfaces (``voice_updated``).
        api_key: Returns a provider's API key, or None (for OpenAI's API).
        installer: Installs Python packages (tests replace it).
    """

    def __init__(
        self,
        settings: Callable[[], Any],
        publish: Callable[[Event], None],
        api_key: Callable[[str], str | None],
        installer: Callable[[Sequence[str]], None] = pip_install,
    ) -> None:
        self._settings = settings
        self._publish = publish
        self._api_key = api_key
        self._installer = installer
        self.transcriber = stt.LocalTranscriber()
        self.speaker = tts.Speaker()
        self.preparing: str | None = None  # "installing" or "downloading"
        self.error: str | None = None
        self._task: asyncio.Task[None] | None = None

    def info(self) -> Event:
        """What the interfaces need to show the microphone and the settings."""
        settings = self._settings()
        engine = settings.voice_input
        if engine == "local":
            listening = stt.local_available() and stt.model_ready(settings.voice_model)
        elif engine == "openai":
            listening = bool(self._api_key("openai"))
        else:
            listening = False
        speaking = tts.available() and tts.voice_ready(settings.language)
        return {
            "input": engine,
            "model": settings.voice_model,
            "reply": settings.voice_reply,
            "listening": listening,
            "speaking": speaking,
            "installed": stt.local_available() and tts.available(),
            "preparing": self.preparing,
            "error": self.error,
        }

    # --- Preparation -----------------------------------------------------------------------

    def prepare(self) -> Event:
        """Install the engines if needed and download the models, in the background."""
        if self._task is None or self._task.done():
            self.error = None
            self._task = asyncio.create_task(self._prepare())
        return self.info()

    async def _prepare(self) -> None:
        settings = self._settings()
        try:
            if not (stt.local_available() and tts.available()):
                self._step("installing")
                await asyncio.to_thread(self._installer, PACKAGES)
            self._step("downloading")
            if settings.voice_input == "local" and not stt.model_ready(settings.voice_model):
                await asyncio.to_thread(self.transcriber.load, settings.voice_model)
            if not tts.voice_ready(settings.language):
                await asyncio.to_thread(tts.download_voice, settings.language)
        except (VoiceError, OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Voice preparation failed: %s", exc)
            self.error = str(exc)
        except Exception as exc:  # model downloads (network) and other engine errors
            logger.exception("Voice preparation failed")
            self.error = str(exc)
        finally:
            self.preparing = None
            self._publish({"type": "voice_updated", "voice": self.info()})

    def _step(self, step: str) -> None:
        self.preparing = step
        self._publish({"type": "voice_updated", "voice": self.info()})

    # --- Listening and speaking ------------------------------------------------------------

    async def transcribe(self, data: bytes, content_type: str = "") -> str:
        """What was said in the recording `data`.

        Raises:
            VoiceError: Voice off, engine or model missing, unreadable recording.
        """
        settings = self._settings()
        language = settings.language.split("-")[0]
        if settings.voice_input == "off":
            raise VoiceError("speech recognition is turned off")
        if settings.voice_input == "openai":
            key = self._api_key("openai")
            if not key:
                raise VoiceError("no OpenAI API key for speech recognition")
            extension = (
                "ogg" if "ogg" in content_type else "wav" if "wav" in content_type else "webm"
            )
            return await asyncio.to_thread(
                stt.transcribe_openai,
                data,
                api_key=key,
                language=language,
                filename=f"voice.{extension}",
            )
        return await asyncio.to_thread(
            self.transcriber.transcribe, data, model=settings.voice_model, language=language
        )

    async def speak(self, text: str) -> bytes:
        """`text` read aloud (a WAV file).

        Raises:
            VoiceError: Piper or the voice missing, nothing to read.
        """
        return await asyncio.to_thread(self.speaker.speak, text, self._settings().language)
