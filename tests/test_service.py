import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from fakes import ScriptedProvider, call, reply
from kreafinity.agent import ConversationStore
from kreafinity.config import Settings, load_settings
from kreafinity.providers import ProviderAuthError, ProviderUnavailableError
from kreafinity.service import AssistantService, EventHub, ServiceError, Subscription
from kreafinity.tools import ToolContext


class Harness:
    """A service wired to scripted providers, with its event stream."""

    def __init__(self, home: Path, settings: Settings | None = None) -> None:
        self.providers: dict[str, ScriptedProvider] = {}
        self.settings = settings or Settings()
        self.service = AssistantService(
            ConversationStore(),
            settings_loader=lambda: self.settings,
            provider_factory=self.create,
            context=ToolContext.create(home=home),
            approval_timeout=5,
        )
        self.events: Subscription = self.service.events.subscribe()

    def create(self, name: str, **options: Any) -> ScriptedProvider:
        provider = self.providers[name]
        provider.name = name
        provider.display_name = name.title()
        return provider

    async def next(self, *types: str) -> dict[str, Any]:
        """Next event of one of `types` (others are skipped)."""
        while True:
            event = await asyncio.wait_for(self.events.get(), timeout=5)
            if event["type"] in types:
                return event

    async def until(self, *types: str) -> list[dict[str, Any]]:
        """All events up to and including the first one of `types`."""
        seen = []
        while True:
            event = await asyncio.wait_for(self.events.get(), timeout=5)
            seen.append(event)
            if event["type"] in types:
                return seen


@pytest.fixture
async def harness(home: Path) -> AsyncIterator[Harness]:
    h = Harness(home)
    yield h
    await h.service.aclose()


async def test_message_is_streamed_and_saved(harness: Harness) -> None:
    harness.providers["ollama"] = ScriptedProvider(reply("Bonjour !"))
    conversation = harness.service.create_conversation()
    await harness.next("conversation_updated")

    run = await harness.service.send_message(conversation["id"], "Salut")
    assert run["provider"] == "ollama" and run["model"] == "llama3.1"
    events = await harness.until("run_finished")
    updates = [e for e in events if e["type"] == "conversation_updated"]
    events = [e for e in events if e["type"] != "conversation_updated"]

    assert [e["type"] for e in events] == ["run_started", "text_delta", "run_finished"]
    # The title is known as soon as the request is saved, not only at the end.
    assert updates and updates[0]["conversation"]["title"] == "Salut"
    assert updates[0]["conversation"]["running"] is True
    assert events[0]["content"] == "Salut" and events[0]["provider_name"] == "Ollama"
    assert events[1]["text"] == "Bonjour !"
    assert events[2]["reason"] == "completed"
    assert all(e["conversation_id"] == conversation["id"] for e in events)
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)

    updated = await harness.next("conversation_updated")
    assert updated["conversation"]["title"] == "Salut"
    assert updated["conversation"]["running"] is False
    saved = harness.service.get_conversation(conversation["id"])
    assert [(m["role"], m["content"]) for m in saved["messages"]] == [
        ("user", "Salut"),
        ("assistant", "Bonjour !"),
    ]


@pytest.mark.parametrize("approved", [True, False])
async def test_approval_flow(harness: Harness, home: Path, approved: bool) -> None:
    harness.providers["ollama"] = ScriptedProvider(
        reply("", [call("create_folder", path="~/Projets")]), reply("Fini.")
    )
    conversation = harness.service.create_conversation()
    await harness.service.send_message(conversation["id"], "Crée le dossier Projets")

    request = await harness.next("approval_required")
    assert request["risk"] == "confirm"
    assert request["summary"] == "Créer un dossier"
    assert request["details"] == [str(home / "Projets")]
    assert harness.service.pending_approvals()[0]["approval_id"] == request["approval_id"]
    assert not (home / "Projets").exists()  # nothing happens before the decision

    resolved = harness.service.resolve_approval(request["approval_id"], approved)
    assert resolved["approved"] is approved
    finished = await harness.next("tool_finished")
    assert finished["status"] == ("ok" if approved else "denied")
    await harness.next("run_finished")
    assert (home / "Projets").is_dir() is approved
    assert harness.service.pending_approvals() == []
    with pytest.raises(ServiceError) as info:
        harness.service.resolve_approval(request["approval_id"], True)
    assert info.value.code == "not_found"


async def test_unanswered_approval_is_denied(home: Path) -> None:
    harness = Harness(home)
    harness.service.approval_timeout = 0.2
    harness.providers["ollama"] = ScriptedProvider(
        reply("", [call("move_to_trash", paths=["~/Documents"])]), reply("Ok.")
    )
    conversation = harness.service.create_conversation()
    await harness.service.send_message(conversation["id"], "Supprime Documents")
    request = await harness.next("approval_required")
    assert request["risk"] == "danger"
    resolved = await harness.next("approval_resolved")
    assert resolved == {**resolved, "approved": False, "reason": "timeout"}
    assert (await harness.next("tool_finished"))["status"] == "denied"
    assert (home / "Documents").is_dir()
    await harness.service.aclose()


async def test_busy_cancel_and_repaired_history(harness: Harness, home: Path) -> None:
    harness.providers["ollama"] = ScriptedProvider(
        reply("", [call("create_folder", path="~/A")]), reply("Me revoilà.")
    )
    conversation = harness.service.create_conversation()
    conversation_id = conversation["id"]
    await harness.service.send_message(conversation_id, "Crée A")
    await harness.next("approval_required")

    with pytest.raises(ServiceError) as info:
        await harness.service.send_message(conversation_id, "Et B ?")
    assert info.value.code == "busy"
    assert harness.service.get_conversation(conversation_id)["running"] is True

    await harness.service.cancel_run(conversation_id)
    assert (await harness.next("run_finished"))["reason"] == "cancelled"
    assert harness.service.pending_approvals() == []
    assert not (home / "A").exists()
    with pytest.raises(ServiceError):
        await harness.service.cancel_run(conversation_id)

    # The interrupted tool call gets a result, so the conversation can go on.
    await harness.service.send_message(conversation_id, "Tu es là ?")
    await harness.next("run_finished")
    roles = [m["role"] for m in harness.service.get_conversation(conversation_id)["messages"]]
    assert roles == ["user", "assistant", "tool", "user", "assistant"]


async def test_provider_switch_is_remembered(harness: Harness) -> None:
    harness.providers["ollama"] = ScriptedProvider(reply("local"))
    harness.providers["anthropic"] = ScriptedProvider(reply("claude", model="claude-x"))
    conversation_id = harness.service.create_conversation()["id"]

    run = await harness.service.send_message(conversation_id, "1", provider="anthropic/claude-x")
    assert run == {**run, "provider": "anthropic", "model": "claude-x"}
    await harness.next("run_finished")
    await harness.service.send_message(conversation_id, "2")
    await harness.next("run_finished")

    assert harness.providers["ollama"].requests == []
    assert [r[2] for r in harness.providers["anthropic"].requests] == ["claude-x", "claude-x"]
    assert harness.service.get_conversation(conversation_id)["provider"] == "anthropic/claude-x"

    with pytest.raises(ServiceError) as info:
        await harness.service.send_message(conversation_id, "3", provider="skynet")
    assert info.value.code == "invalid"


async def test_fallback_and_failures(home: Path) -> None:
    harness = Harness(home, Settings(provider="anthropic", fallback="ollama/llama3.1"))
    harness.providers["anthropic"] = ScriptedProvider(
        ProviderUnavailableError("offline", provider="anthropic")
    )
    harness.providers["ollama"] = ScriptedProvider(reply("hors ligne"))
    conversation_id = harness.service.create_conversation()["id"]

    await harness.service.send_message(conversation_id, "Salut")
    fallback = await harness.next("provider_fallback")
    assert (fallback["unavailable"], fallback["fallback"]) == ("Anthropic", "Ollama")
    assert (await harness.next("run_finished", "run_failed"))["type"] == "run_finished"

    harness.settings = Settings(provider="anthropic")  # no fallback any more
    harness.providers["anthropic"].responses = [ProviderAuthError("bad key", provider="anthropic")]
    await harness.service.send_message(conversation_id, "Encore")
    failed = await harness.next("run_failed", "run_finished")
    assert failed == {**failed, "type": "run_failed", "code": "provider_auth"}
    await harness.service.aclose()


async def test_delete_running_conversation(harness: Harness) -> None:
    harness.providers["ollama"] = ScriptedProvider(reply("", [call("create_folder", path="~/X")]))
    conversation_id = harness.service.create_conversation()["id"]
    await harness.service.send_message(conversation_id, "x")
    await harness.next("approval_required")
    await harness.service.delete_conversation(conversation_id)
    assert (await harness.next("conversation_deleted"))["conversation_id"] == conversation_id
    for action in (
        lambda: harness.service.get_conversation(conversation_id),
        lambda: harness.service.rename_conversation(conversation_id, "x"),
    ):
        with pytest.raises(ServiceError) as info:
            action()
        assert info.value.code == "not_found"


async def test_list_models_errors(harness: Harness) -> None:
    class Offline(ScriptedProvider):
        async def list_models(self) -> Any:
            raise ProviderUnavailableError("down", provider="ollama")

    harness.providers["ollama"] = Offline(reply("x"))
    with pytest.raises(ServiceError) as info:
        await harness.service.list_models("ollama")
    assert info.value.code == "provider_unavailable"


async def test_config_providers_and_tools(home: Path) -> None:
    service = AssistantService(ConversationStore(), context=ToolContext.create(home=home))
    assert service.config()["provider"] == "ollama/llama3.1"
    providers = service.list_providers()
    assert {p["name"] for p in providers["providers"]} >= {"anthropic", "ollama"}

    updated = service.update_config({"language": "en", "max_iterations": 4})
    assert (updated["language"], updated["max_iterations"]) == ("en", 4)
    assert load_settings().max_iterations == 4
    with pytest.raises(ServiceError) as info:
        service.update_config({"max_iterations": 0})
    assert info.value.code == "invalid"
    with pytest.raises(ServiceError):
        service.update_config({"providers": {"openai": {"api_key": "sk-x"}}})

    assert service.set_default_provider("mistral/mistral-small-latest")["provider"] == (
        "mistral/mistral-small-latest"
    )
    with pytest.raises(ServiceError):
        service.set_default_provider("skynet")

    service.update_config({"tools": ["system_info", "move_to_trash"]})
    tools = {t["name"]: t for t in service.list_tools()}
    assert tools["move_to_trash"] == {**tools["move_to_trash"], "risk": "danger", "enabled": True}
    assert tools["create_folder"]["enabled"] is False
    await service.aclose()


async def test_slow_subscriber_must_resync() -> None:
    hub = EventHub(queue_size=3)
    slow, fast = hub.subscribe(), hub.subscribe()
    for index in range(5):
        hub.publish({"type": "text_delta", "text": str(index)})
        await fast.get()
    assert (await slow.get())["type"] == "resync_required"
    hub.publish({"type": "text_delta", "text": "after"})
    assert (await fast.get())["text"] == "after"
