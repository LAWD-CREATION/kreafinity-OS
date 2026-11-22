"""Looking at the screen: capture, size, consent, and images for every kind of AI API."""

import base64
import io
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image as PILImage

from fakes import ScriptedProvider, call, reply
from kreafinity.agent import Agent, Conversation, ConversationStore
from kreafinity.providers import Image, Message
from kreafinity.providers._chat_format import to_chat_messages
from kreafinity.providers.anthropic import _to_anthropic_messages
from kreafinity.providers.ollama import OllamaProvider, _to_ollama_messages
from kreafinity.system import screen
from kreafinity.tools import ToolContext, ToolError, ToolRegistry

PIXEL = Image("image/jpeg", base64.b64encode(b"jpeg bytes").decode())


def png(width: int, height: int) -> bytes:
    output = io.BytesIO()
    PILImage.new("RGB", (width, height), (40, 20, 90)).save(output, "PNG")
    return output.getvalue()


# --- Capture --------------------------------------------------------------------------------


def test_prepare_scales_down_and_keeps_the_proportions() -> None:
    shot = screen.prepare(png(3000, 2000), tool="spectacle")
    assert (shot.width, shot.height) == (screen.MAX_SIDE, 1045)
    assert shot.original == (3000, 2000) and shot.media_type == "image/jpeg"
    with PILImage.open(io.BytesIO(base64.b64decode(shot.data))) as decoded:
        assert decoded.format == "JPEG" and decoded.size == (shot.width, shot.height)
    small = screen.prepare(png(800, 600))
    assert (small.width, small.height) == (800, 600)  # never enlarged
    with pytest.raises(screen.ScreenError, match="not a readable image"):
        screen.prepare(b"not an image")


@pytest.fixture
def tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake capture programs on PATH, writing a real PNG and logging their calls."""
    folder = tmp_path / "bin"
    folder.mkdir()
    image = tmp_path / "screen.png"
    image.write_bytes(png(1920, 1080))
    log = tmp_path / "calls.log"
    for name in ("spectacle", "gnome-screenshot", "grim", "scrot", "import"):
        program = folder / name
        program.write_text(
            f'#!/bin/sh\necho "{name}" >> "{log}"\nfor last; do :; done\ncp "{image}" "$last"\n'
        )
        program.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    return log


def test_capture_uses_the_desktop_tool(tools: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    shot = screen.capture()
    assert shot.tool == "spectacle" and shot.original == (1920, 1080)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    assert screen.capture().tool == "gnome-screenshot"
    assert tools.read_text().splitlines() == ["spectacle", "gnome-screenshot"]


def test_capture_on_wayland_never_uses_x11_tools(
    tools: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "sway")
    for name in ("spectacle", "gnome-screenshot", "grim"):
        (tools.parent / "bin" / name).unlink()
    with pytest.raises(screen.ScreenError, match="no screenshot tool"):
        screen.capture()  # scrot and import would only see a black screen


def test_capture_errors(tools: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY")
    with pytest.raises(screen.ScreenError, match="no graphical session"):
        screen.capture()
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    for program in (tools.parent / "bin").iterdir():
        program.write_text("#!/bin/sh\necho 'cannot capture' >&2\nexit 1\n")
    with pytest.raises(screen.ScreenError, match="spectacle: cannot capture"):
        screen.capture()


# --- The tool ---------------------------------------------------------------------------------


async def test_look_at_screen_asks_first(context: ToolContext, tools: Path) -> None:
    look = ToolRegistry.load(["look_at_screen"]).get("look_at_screen")
    assert look is not None and look.risk.value == "confirm"
    preview = await look.preview({"reason": "Voir le message d'erreur du Wi-Fi"}, context)
    assert preview.summary == "Regarder votre écran (une capture d'écran)"
    assert preview.details[0] == "Voir le message d'erreur du Wi-Fi"
    assert "envoyée à l'IA choisie" in preview.details[1]
    result = await look.run({"reason": "Wi-Fi"}, context)
    assert json.loads(result.content)["screen"] == "1920x1080"
    assert len(result.images) == 1 and result.images[0].media_type == "image/jpeg"


async def test_look_at_screen_failure(
    context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    look = ToolRegistry.load(["look_at_screen"]).get("look_at_screen")
    assert look is not None
    with pytest.raises(ToolError, match="Cannot see the screen: no graphical session"):
        await look.run({"reason": "x"}, context)


# --- Images for each kind of API ----------------------------------------------------------------


def conversation_with_a_screenshot() -> list[Message]:
    return [
        Message.user("Pourquoi le Wi-Fi ne marche pas ?"),
        Message.assistant(
            "", [call("look_at_screen", "c1", reason="Wi-Fi"), call("system_info", "c2")]
        ),
        Message.tool_result("c1", "look_at_screen", '{"screen": "1920x1080"}', images=[PIXEL]),
        Message.tool_result("c2", "system_info", "{}"),
        Message.assistant("Je vois une erreur."),
    ]


def test_anthropic_puts_the_image_in_the_tool_result() -> None:
    _, messages = _to_anthropic_messages(conversation_with_a_screenshot())
    results = messages[2]["content"]
    assert results[0]["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": PIXEL.data},
    }
    assert results[1]["content"] == "{}"  # results without images stay plain text


def test_chat_apis_get_the_image_after_the_tool_results() -> None:
    messages = to_chat_messages(conversation_with_a_screenshot())
    roles = [m["role"] for m in messages]
    # Never between the tool results of one turn (OpenAI and Mistral refuse it).
    assert roles == ["user", "assistant", "tool", "tool", "user", "assistant"]
    parts = messages[4]["content"]
    assert "look_at_screen" in parts[0]["text"]
    assert parts[1] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{PIXEL.data}"},
    }
    ending = to_chat_messages(conversation_with_a_screenshot()[:4])
    assert ending[-1]["role"] == "user"  # images at the end of the conversation too


def test_ollama_gets_the_image_after_the_tool_results() -> None:
    messages = _to_ollama_messages(conversation_with_a_screenshot())
    assert [m["role"] for m in messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "user",
        "assistant",
    ]
    assert messages[4]["images"] == [PIXEL.data]


async def test_ollama_models_that_cannot_see(context: ToolContext) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/api/show":
            vision = body["model"] == "qwen2.5vl"
            capabilities = ["completion", "vision"] if vision else ["completion", "tools"]
            return httpx.Response(200, json={"capabilities": capabilities})
        sent.append(body)
        done = {
            "model": body["model"],
            "message": {"role": "assistant", "content": "ok"},
            "done": True,
            "done_reason": "stop",
        }
        return httpx.Response(200, content=json.dumps(done).encode() + b"\n")

    provider = OllamaProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    for model in ("llama3.1", "qwen2.5vl"):
        async for _ in provider.chat(conversation_with_a_screenshot()[:4], model=model):
            pass
    blind, seeing = sent
    assert not any("images" in m for m in blind["messages"])
    assert "does not read images" in blind["messages"][2]["content"]
    assert seeing["messages"][4]["images"] == [PIXEL.data]
    await provider.aclose()


# --- Through the agent --------------------------------------------------------------------------


async def test_the_model_sees_the_screenshot_once_and_history_keeps_none(
    context: ToolContext, tools: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    provider = ScriptedProvider(
        reply("", [call("look_at_screen", "c1", reason="Voir l'erreur")]),
        reply("Je vois une fenêtre d'erreur du Wi-Fi."),
    )

    async def approve(request: Any) -> bool:
        return True

    agent = Agent(provider, ToolRegistry.load(["look_at_screen"]), context, approver=approve)
    conversation = Conversation()
    async for _ in agent.run(conversation, "Regarde mon écran"):
        pass
    seen = provider.requests[1][0]
    assert seen[-1].role == "tool" and len(seen[-1].images) == 1
    store = ConversationStore()
    created = store.create()
    store.append(created.id, conversation.messages)
    assert all(not message.images for message in store.messages(created.id))


async def test_a_model_that_refuses_images_is_asked_again_without_them(
    context: ToolContext,
) -> None:
    from kreafinity.providers import ProviderError, ProviderUnavailableError

    refusal = ProviderError("OpenRouter API error: 400 image input not supported", provider="x")
    provider = ScriptedProvider(refusal, reply("Je ne peux pas voir les images."))
    agent = Agent(provider, ToolRegistry.load([]), context)
    conversation = Conversation(messages=conversation_with_a_screenshot()[:4])
    async for _ in agent.run(conversation, "Alors ?"):
        pass
    first, second = provider.requests[0][0], provider.requests[1][0]
    assert first[3].images
    assert not any(m.images for m in second)
    assert "fake-model does not read images" in second[3].content
    assert conversation.messages[2].images  # the conversation itself is unchanged

    # Other errors are not retried: a request without images, or an unreachable server.
    for error, messages in (
        (refusal, [Message.user("x")]),
        (ProviderUnavailableError("down", provider="x"), conversation_with_a_screenshot()[:4]),
    ):
        provider = ScriptedProvider(error)
        agent = Agent(provider, ToolRegistry.load([]), context)
        with pytest.raises(ProviderError):
            async for _ in agent.run(Conversation(messages=messages), "Alors ?"):
                pass
        assert len(provider.requests) == 1


async def test_only_the_latest_screenshots_are_sent_again(context: ToolContext) -> None:
    from kreafinity.agent.loop import MAX_IMAGE_RESULTS

    messages: list[Message] = []
    for turn in range(MAX_IMAGE_RESULTS + 2):
        messages += [
            Message.user(f"Question {turn}"),
            Message.assistant("", [call("look_at_screen", f"c{turn}", reason="x")]),
            Message.tool_result(f"c{turn}", "look_at_screen", "{}", images=[PIXEL]),
            Message.assistant("Vu."),
        ]
    provider = ScriptedProvider(reply("Encore ?"))
    agent = Agent(provider, ToolRegistry.load([]), context)
    async for _ in agent.run(Conversation(messages=messages), "Et maintenant ?"):
        pass
    sent = [m for m in provider.requests[0][0] if m.role == "tool"]
    assert [bool(m.images) for m in sent] == [False, False, True, True, True]
    assert "earlier screenshot" in sent[0].content
