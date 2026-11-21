"""Recorded audio (WebM/Opus from a browser, WAV, Ogg…) as 16 kHz mono samples."""

from __future__ import annotations

import io
from typing import Any

from kreafinity.voice import VoiceError

SAMPLE_RATE = 16_000
#: Longest recording accepted, in seconds, and largest upload, in bytes.
MAX_SECONDS = 120
MAX_BYTES = 10 * 1024 * 1024


def decode(data: bytes) -> Any:
    """Decode a recording into float32 samples at 16 kHz, mono (a NumPy array).

    Decoded here rather than by faster-whisper: its decoder does not follow
    recent versions of PyAV.

    Raises:
        VoiceError: Not audio, empty, or too long.
    """
    if not data:
        raise VoiceError("empty recording")
    if len(data) > MAX_BYTES:
        raise VoiceError("recording too large")
    try:
        import av  # comes with faster-whisper
        import numpy as np
    except ImportError as exc:
        raise VoiceError("speech recognition is not installed (kreafinity voice install)") from exc
    chunks: list[Any] = []
    try:
        with av.open(io.BytesIO(data), mode="r") as container:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            for frame in container.decode(audio=0):
                chunks.extend(out.to_ndarray().reshape(-1) for out in resampler.resample(frame))
            chunks.extend(out.to_ndarray().reshape(-1) for out in resampler.resample(None))
    except (av.FFmpegError, ValueError, IndexError) as exc:
        raise VoiceError(f"unreadable recording: {exc}") from exc
    if not chunks:
        raise VoiceError("empty recording")
    samples = np.concatenate(chunks).astype(np.float32) / 32768.0
    if len(samples) > MAX_SECONDS * SAMPLE_RATE:
        raise VoiceError(f"recording too long (at most {MAX_SECONDS} seconds)")
    return samples
