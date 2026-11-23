"""Live voice conversation in a real browser, with a fake microphone.

Chromium plays a generated sound as the microphone: silence, a speech-like
sound, silence. The speech recognizer and the voice are stood in for by the
test (network routes), the AI by the fake Ollama.
"""

import glob
import io
import json
import math
import os
import subprocess
import sys
import time
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

ANSWER = "Il est **midi**. Bonne journée à vous !\n\n```\ncode non lu\n```\nÀ bientôt"


def speech_like(path: Path, *, rate: int = 48000) -> None:
    """1.5 s of silence, 1.6 s of a voiced, syllable-like sound, then 5 s of silence."""
    frames = bytearray()
    for i in range(int(rate * 8.1)):
        t = i / rate
        value = 0.0
        if 1.5 <= t < 3.1:
            envelope = 0.5 + 0.5 * math.sin(2 * math.pi * 4 * t)  # ~4 syllables a second
            value = (
                envelope
                * sum(
                    math.sin(2 * math.pi * f * t) / k
                    for k, f in enumerate((180, 360, 540, 720, 1100), 1)
                )
                * 0.35
            )
        frames += int(max(-1, min(1, value)) * 32000).to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(bytes(frames))


def silence_wav(seconds: float = 0.2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x00\x00" * int(16000 * seconds))
    return buffer.getvalue()


@pytest.fixture
def daemon(tmp_path: Path, home: Path) -> Iterator[str]:
    from fake_ollama import FakeOllama, words
    from test_ui import free_port

    with FakeOllama(lambda messages: words(ANSWER)) as ollama:
        port = free_port()
        config = tmp_path / "xdg-config" / "kreafinity"
        config.mkdir(parents=True)
        (config / "config.yaml").write_text(
            f"onboarded: true\nproviders:\n  ollama:\n    base_url: {ollama.url}\n"
        )
        env = {
            **os.environ,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        }
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from kreafinity.api.server import main; main()",
                "--port",
                str(port),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{url}/api/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.1)
        yield url
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def page(daemon: str, tmp_path: Path) -> Iterator[Any]:
    sound = tmp_path / "micro.wav"
    speech_like(sound)
    arguments = [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={sound}",
        "--autoplay-policy=no-user-gesture-required",
    ]
    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(args=arguments)
        except Exception:
            candidates = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
            if not candidates:
                pytest.skip("no Chromium browser for Playwright")
            browser = playwright.chromium.launch(executable_path=candidates[-1], args=arguments)
        context = browser.new_context(viewport={"width": 1100, "height": 820}, locale="fr-FR")
        context.grant_permissions(["microphone"])
        page = context.new_page()
        page.errors = []
        page.on("pageerror", lambda error: page.errors.append(str(error)))
        yield page
        browser.close()
        assert page.errors == []


def test_sentence_splitter(page: Any, daemon: str) -> None:
    page.goto(daemon)
    page.wait_for_selector("#connection.online")
    result = page.evaluate("""async () => {
        const { SentenceSplitter, speakable } = await import("/js/live.js");
        const splitter = new SentenceSplitter();
        const out = [];
        const pieces = [
            "Bon", "jour. Voici ", "le **plan** : ", "\\n```py\\nprint(1)\\n", "```\\nFin",
        ];
        for (const piece of pieces) {
            out.push(...splitter.push(piece));
        }
        out.push(...splitter.flush());
        return { out, link: speakable("Voir [le site](https://x.y) et `ls -l`") };
    }""")
    assert result["out"] == ["Bonjour.", "Voici le plan :", "Fin"]
    assert result["link"] == "Voir le site et ls -l"


def test_live_conversation(page: Any, daemon: str) -> None:
    calls: dict[str, list[Any]] = {"transcribe": [], "speak": []}

    def voice_info(route: Any) -> None:
        route.fulfill(
            json={
                "input": "local",
                "reply": "voice",
                "listening": True,
                "speaking": True,
                "preparing": False,
            }
        )

    def transcribe(route: Any) -> None:
        request = route.request
        size = len(request.post_data_buffer or b"")
        if size < 16000:  # the warm-up at the start: a short silence
            return route.fulfill(json={"text": ""})
        calls["transcribe"].append((request.headers.get("content-type"), size))
        route.fulfill(json={"text": "Quelle heure est-il ?"})

    def speak(route: Any) -> None:
        calls["speak"].append(json.loads(route.request.post_data or "{}")["text"])
        route.fulfill(body=silence_wav(), headers={"Content-Type": "audio/wav"})

    page.route("**/api/voice", voice_info)
    page.route("**/api/voice/transcribe", transcribe)
    page.route("**/api/voice/speak", speak)
    page.goto(daemon)
    page.wait_for_selector("#connection.online")
    page.wait_for_selector("#live-start:not([hidden])")
    page.click("#live-start")
    panel = page.wait_for_selector("#live:not([hidden])")
    assert panel.get_attribute("data-state") == "listening"
    page.wait_for_selector(".message.user >> text=Quelle heure est-il ?", timeout=20000)
    page.wait_for_selector(".message.assistant >> text=Bonne journée", timeout=20000)
    page.wait_for_function(
        "() => document.querySelector('#live').dataset.state === 'listening'", timeout=20000
    )
    kind, size = calls["transcribe"][0]
    assert kind == "audio/wav" and size > 16000 * 2  # more than a second of 16 kHz audio
    assert calls["speak"][:3] == [
        "Il est midi.",
        "Bonne journée à vous !",
        "À bientôt",
    ]  # no markdown, no code
    page.click("#live >> text=Terminer")
    assert page.locator("#live").is_hidden()


def test_interrupting_the_answer(page: Any, daemon: str) -> None:
    """Speaking while Kreafinity talks stops its voice and starts a new sentence."""
    transcribed: list[int] = []
    page.route(
        "**/api/voice",
        lambda route: route.fulfill(
            json={
                "input": "local",
                "reply": "voice",
                "listening": True,
                "speaking": True,
                "preparing": False,
            }
        ),
    )

    def transcribe(route: Any) -> None:
        if len(route.request.post_data_buffer or b"") < 16000:  # the warm-up
            return route.fulfill(json={"text": ""})
        transcribed.append(1)
        route.fulfill(
            json={"text": "Quelle heure est-il ?" if len(transcribed) == 1 else "Merci, stop."}
        )

    page.route("**/api/voice/transcribe", transcribe)
    # A long answer read aloud: the next sentence of the microphone comes while it plays.
    page.route(
        "**/api/voice/speak",
        lambda route: route.fulfill(body=silence_wav(12), headers={"Content-Type": "audio/wav"}),
    )
    page.goto(daemon)
    page.wait_for_selector("#connection.online")
    page.click("#live-start")
    page.wait_for_selector("#live:not([hidden])")
    page.evaluate("""() => {
        window.__states = [];
        const panel = document.querySelector("#live");
        new MutationObserver(() => window.__states.push(panel.dataset.state))
            .observe(panel, { attributes: true, attributeFilter: ["data-state"] });
    }""")
    page.wait_for_selector(".message.user >> text=Merci, stop.", timeout=30000)
    states = page.evaluate("() => window.__states")
    spoken = states.index("speaking")
    assert "hearing" in states[spoken:], states  # interrupted while speaking
