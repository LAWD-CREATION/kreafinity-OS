"""Talking to Kreafinity: speech recognition and answers read aloud (engines simulated)."""

import asyncio
import importlib.util
import io
import math
import struct
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kreafinity import cli
from kreafinity.agent import ConversationStore
from kreafinity.api import create_app
from kreafinity.config import ConfigError, load_settings
from kreafinity.service import AssistantService
from kreafinity.tools import ToolContext
from kreafinity.voice import PACKAGES, VoiceError, audio, stt, tts
from kreafinity.voice.manager import VoiceManager

WAV_HEADER = b"RIFF"


def settings(**values: Any) -> SimpleNamespace:
    base = {"voice_input": "local", "voice_model": "small", "voice_reply": "voice"}
    base["language"] = "fr"
    return SimpleNamespace(**{**base, **values})


def engines(
    monkeypatch: pytest.MonkeyPatch, *, installed: bool = True, models: bool = True
) -> None:
    """Pretend the voice engines are (or are not) installed, with their models."""
    monkeypatch.setattr(stt, "local_available", lambda: installed)
    monkeypatch.setattr(tts, "available", lambda: installed)
    monkeypatch.setattr(stt, "model_ready", lambda model: models)
    monkeypatch.setattr(tts, "voice_ready", lambda language: models)


def manager(current: SimpleNamespace, key: str | None = None) -> tuple[VoiceManager, list[Any]]:
    events: list[Any] = []
    return VoiceManager(lambda: current, events.append, lambda provider: key), events


# --- Reading aloud: what is said -------------------------------------------------------------


def test_speakable_text() -> None:
    answer = (
        "## Résultat\n"
        "Voici les **5 plus gros** fichiers :\n"
        "1. `film.mkv` — 4,2 Go\n"
        "2. [archive.zip](file:///home/david/archive.zip) — 1 Go\n"
        "```bash\ndu -sh *\n```\n"
        "Plus d'infos sur https://kreafinity.com !"
    )
    assert tts.speakable(answer) == (
        "Résultat. Voici les 5 plus gros fichiers : film.mkv — 4,2 Go. archive.zip — 1 Go. "
        "(code). Plus d'infos sur !"
    )
    long = "Une phrase assez longue pour remplir. " * 100
    spoken = tts.speakable(long)
    assert len(spoken) <= tts.MAX_CHARACTERS and spoken.endswith(".")
    assert tts.speakable("**  **") == ""


def test_voices_and_models_on_disk(tmp_path: Path) -> None:
    assert tts.voice_name("fr") == "fr_FR-siwis-medium" and tts.voice_name("xx") == tts.VOICES["en"]
    assert not tts.voice_ready("fr") and not stt.model_ready("small")
    tts.piper_dir().mkdir(parents=True)
    for suffix in (".onnx", ".onnx.json"):
        (tts.piper_dir() / f"fr_FR-siwis-medium{suffix}").write_text("…")
    assert tts.voice_ready("fr")
    model = stt.whisper_dir() / "models--Systran--faster-whisper-small" / "snapshots" / "abc"
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"...")
    assert stt.model_ready("small") and not stt.model_ready("medium")


def test_settings(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("voice_input: openai\nvoice_model: base\nvoice_reply: always\n")
    loaded = load_settings(config)
    voice = (loaded.voice_input, loaded.voice_model, loaded.voice_reply)
    assert voice == ("openai", "base", "always")
    defaults = load_settings(tmp_path / "absent.yaml")
    voice = (defaults.voice_input, defaults.voice_model, defaults.voice_reply)
    assert voice == ("local", "small", "voice")
    for line in ("voice_input: google", "voice_model: large", "voice_reply: sometimes"):
        config.write_text(line + "\n")
        with pytest.raises(ConfigError, match=line.split(":")[0]):
            load_settings(config)


# --- Recordings ------------------------------------------------------------------------------


def wav_bytes(seconds: float, rate: int = 48_000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = int(seconds * rate)
        wav.writeframes(
            b"".join(
                struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(frames)
            )
        )
    return output.getvalue()


needs_av = pytest.mark.skipif(
    importlib.util.find_spec("av") is None, reason="PyAV is not installed (voice engines)"
)


@needs_av
def test_decode() -> None:
    samples = audio.decode(wav_bytes(1.0))
    assert abs(len(samples) - audio.SAMPLE_RATE) < 400  # resampled to 16 kHz, mono
    assert samples.dtype.name == "float32" and 0.2 < float(abs(samples).max()) <= 1.0
    with pytest.raises(VoiceError, match="unreadable"):
        audio.decode(b"pas du son")
    with pytest.raises(VoiceError, match="empty"):
        audio.decode(b"")


def test_recording_size_limit() -> None:
    with pytest.raises(VoiceError, match="too large"):
        audio.decode(b"x" * (audio.MAX_BYTES + 1))


def test_local_recognition_needs_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    engines(monkeypatch, models=False)
    with pytest.raises(VoiceError, match="not downloaded yet"):
        stt.LocalTranscriber().transcribe(b"...", model="small", language="fr")
    with pytest.raises(VoiceError, match="unknown Whisper model"):
        stt.LocalTranscriber().load("huge")


# --- The daemon's voice ----------------------------------------------------------------------


def test_info(monkeypatch: pytest.MonkeyPatch) -> None:
    engines(monkeypatch)
    voice, _ = manager(settings())
    assert voice.info() == {
        "input": "local",
        "model": "small",
        "reply": "voice",
        "listening": True,
        "speaking": True,
        "installed": True,
        "preparing": None,
        "error": None,
    }
    engines(monkeypatch, installed=False)
    assert not voice.info()["listening"] and not voice.info()["speaking"]
    online, _ = manager(settings(voice_input="openai"), key="sk-test")
    assert online.info()["listening"]  # OpenAI needs only its key
    assert not manager(settings(voice_input="openai"))[0].info()["listening"]
    assert not manager(settings(voice_input="off"))[0].info()["listening"]


async def test_transcribe(monkeypatch: pytest.MonkeyPatch) -> None:
    engines(monkeypatch)
    heard: list[tuple[bytes, str, str]] = []

    def local(self: Any, data: bytes, *, model: str, language: str) -> str:
        heard.append((data, model, language))
        return "Montre-moi mes photos"

    monkeypatch.setattr(stt.LocalTranscriber, "transcribe", local)
    voice, _ = manager(settings(language="fr-FR"))
    assert await voice.transcribe(b"webm", "audio/webm") == "Montre-moi mes photos"
    assert heard == [(b"webm", "small", "fr")]

    with pytest.raises(VoiceError, match="turned off"):
        await manager(settings(voice_input="off"))[0].transcribe(b"webm")
    with pytest.raises(VoiceError, match="no OpenAI API key"):
        await manager(settings(voice_input="openai"))[0].transcribe(b"webm")

    sent: list[dict[str, Any]] = []

    def openai(data: bytes, **options: Any) -> str:
        sent.append(options)
        return "Bonjour"

    monkeypatch.setattr(stt, "transcribe_openai", openai)
    online, _ = manager(settings(voice_input="openai"), key="sk-test")
    assert await online.transcribe(b"ogg", "audio/ogg;codecs=opus") == "Bonjour"
    assert sent == [{"api_key": "sk-test", "language": "fr", "filename": "voice.ogg"}]


async def test_speak(monkeypatch: pytest.MonkeyPatch) -> None:
    engines(monkeypatch, installed=False)
    with pytest.raises(VoiceError, match="not installed"):
        tts.Speaker().speak("Bonjour", "fr")
    engines(monkeypatch, models=False)
    with pytest.raises(VoiceError, match="not downloaded yet"):
        tts.Speaker().speak("Bonjour", "fr")
    said: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tts.Speaker, "speak", lambda self, text, language: said.append((text, language)) or b"RIFF"
    )
    voice, _ = manager(settings(language="en"))
    assert await voice.speak("Hello") == b"RIFF" and said == [("Hello", "en")]


async def test_prepare_installs_and_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"installed": False, "model": False, "voice": False}
    monkeypatch.setattr(stt, "local_available", lambda: state["installed"])
    monkeypatch.setattr(tts, "available", lambda: state["installed"])
    monkeypatch.setattr(stt, "model_ready", lambda model: state["model"])
    monkeypatch.setattr(tts, "voice_ready", lambda language: state["voice"])
    installed: list[tuple[str, ...]] = []

    def installer(packages: tuple[str, ...]) -> None:
        installed.append(tuple(packages))
        state["installed"] = True

    monkeypatch.setattr(stt.LocalTranscriber, "load", lambda self, model: state.update(model=True))
    monkeypatch.setattr(tts, "download_voice", lambda language: state.update(voice=True))
    events: list[Any] = []
    voice = VoiceManager(lambda: settings(), events.append, lambda p: None, installer=installer)

    started = voice.prepare()
    assert voice.prepare() == voice.info() or started  # a second click does not start twice
    await asyncio.wait_for(voice._task, 5)  # type: ignore[arg-type]

    assert installed == [PACKAGES]
    steps = [event["voice"]["preparing"] for event in events]
    assert steps == ["installing", "downloading", None]
    final = events[-1]["voice"]
    assert final["listening"] and final["speaking"] and final["error"] is None


async def test_prepare_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    engines(monkeypatch, models=False)

    def offline(self: Any, model: str) -> None:
        raise OSError("no network")

    monkeypatch.setattr(stt.LocalTranscriber, "load", offline)
    events: list[Any] = []
    voice = VoiceManager(lambda: settings(), events.append, lambda p: None)
    voice.prepare()
    await asyncio.wait_for(voice._task, 5)  # type: ignore[arg-type]
    assert events[-1]["voice"]["error"] == "no network" and voice.preparing is None


# --- API -------------------------------------------------------------------------------------


@pytest.fixture
def client(home: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    engines(monkeypatch)
    token = "t" * 43

    def service() -> AssistantService:
        return AssistantService(
            ConversationStore(), context=ToolContext.create(home=home), notifier=None
        )

    with TestClient(create_app(service, token, port=27494, allowed_hosts=("testserver",))) as app:
        app.headers["Authorization"] = f"Bearer {token}"
        yield app


def test_voice_api(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def transcribe(self: Any, data: bytes, content_type: str = "") -> str:
        assert data == b"opus" and content_type == "audio/webm;codecs=opus"
        return "Quelle heure est-il ?"

    async def speak(self: Any, text: str) -> bytes:
        if text == "rien":
            raise VoiceError("nothing to read")
        return WAV_HEADER + b"..."

    monkeypatch.setattr(VoiceManager, "transcribe", transcribe)
    monkeypatch.setattr(VoiceManager, "speak", speak)
    assert client.get("/api/voice").json()["listening"] is True
    heard = client.post(
        "/api/voice/transcribe", content=b"opus", headers={"Content-Type": "audio/webm;codecs=opus"}
    )
    assert heard.json() == {"text": "Quelle heure est-il ?"}
    spoken = client.post("/api/voice/speak", json={"text": "Il est midi."})
    assert spoken.headers["content-type"] == "audio/wav" and spoken.content.startswith(WAV_HEADER)
    refused = client.post("/api/voice/speak", json={"text": "rien"})
    assert refused.status_code == 503 and refused.json()["error"]["code"] == "voice_unavailable"
    too_big = client.post("/api/voice/transcribe", content=b"x" * (audio.MAX_BYTES + 1))
    assert too_big.status_code == 400
    assert client.post("/api/voice/speak", json={"text": ""}).status_code == 422


def test_voice_api_needs_the_token(client: TestClient) -> None:
    del client.headers["Authorization"]
    assert client.post("/api/voice/transcribe", content=b"opus").status_code == 401
    assert client.post("/api/voice/speak", json={"text": "Bonjour"}).status_code == 401


def test_web_pages_may_play_their_own_audio(client: TestClient) -> None:
    policy = client.get("/").headers["content-security-policy"]
    assert "media-src blob:;" in policy  # answers read aloud (and nothing from elsewhere)


# --- Command line ----------------------------------------------------------------------------


def test_voice_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    engines(monkeypatch, installed=False, models=False)
    shown = runner.invoke(cli.app, ["voice"]).output
    assert "pas encore installée" in shown and "kreafinity voice install" in shown

    state = {"installed": False}
    monkeypatch.setattr(stt, "local_available", lambda: state["installed"])
    monkeypatch.setattr(tts, "available", lambda: state["installed"])
    calls: list[str] = []
    monkeypatch.setattr(
        "kreafinity.voice.manager.pip_install",
        lambda packages: calls.append("pip") or state.update(installed=True),
    )
    monkeypatch.setattr(stt.LocalTranscriber, "load", lambda self, model: calls.append(model))
    monkeypatch.setattr(tts, "download_voice", lambda language: calls.append(language))
    result = runner.invoke(cli.app, ["voix", "install"])
    assert result.exit_code == 0, result.output
    assert calls == ["pip", "small", "fr"] and "La voix est prête" in result.output
