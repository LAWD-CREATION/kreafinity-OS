import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from fakes import ScriptedProvider, call, reply
from kreafinity.agent import (
    ActionLog,
    Agent,
    AgentDone,
    Conversation,
    ToolCallFinished,
    ToolCallStarted,
)
from kreafinity.providers import Message, ProviderUnavailableError, TextDelta, ToolCall
from kreafinity.tools import RiskLevel, Tool, ToolContext, ToolRegistry, ToolResult


class TrashTool(Tool):
    """A `confirm` tool recording its calls (stand-in until step 4)."""

    name = "move_to_trash"
    description = "Move a file to the trash."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    risk = RiskLevel.CONFIRM

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        self.calls.append(arguments)
        return ToolResult("moved")


class CrashingTool(Tool):
    name = "crash"
    description = "Always crashes."
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}
    risk = RiskLevel.SAFE

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        raise RuntimeError("kaboom")


class VerboseTool(Tool):
    name = "verbose"
    description = "Returns a huge output."
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}
    risk = RiskLevel.SAFE

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult("x" * 50_000)


def make_agent(
    provider: ScriptedProvider, context: ToolContext, *extra_tools: Tool, **options: Any
) -> Agent:
    tools = ToolRegistry([*ToolRegistry.load(), *extra_tools])
    return Agent(provider, tools, context, **options)


async def run(agent: Agent, conversation: Conversation, text: str) -> list[Any]:
    return [event async for event in agent.run(conversation, text)]


async def test_plain_answer(context: ToolContext) -> None:
    provider = ScriptedProvider(reply("Bonjour !"))
    agent = make_agent(provider, context)
    conversation = Conversation()

    events = await run(agent, conversation, "Salut")

    assert events == [TextDelta("Bonjour !"), AgentDone(reason="completed", model="fake-model")]
    assert conversation.messages == [Message.user("Salut"), Message.assistant("Bonjour !")]
    messages, tools, model = provider.requests[0]
    assert messages[0].role == "system"
    assert "Kreafinity" in messages[0].content and "français" in messages[0].content
    assert {t.name for t in tools} >= {"system_info", "list_directory", "search_files"}
    assert model is None


async def test_tool_round_trip(context: ToolContext, home: Path) -> None:
    list_call = call("list_directory", path="~/Téléchargements", sort_by="size", limit=2)
    provider = ScriptedProvider(
        reply("Je regarde.", [list_call]),
        reply("Les plus gros : big.iso et video.mp4."),
    )
    log = ActionLog(home.parent / "actions.log")
    agent = make_agent(provider, context, action_log=log)
    conversation = Conversation()

    events = await run(agent, conversation, "Plus gros fichiers de Téléchargements ?")

    started = next(e for e in events if isinstance(e, ToolCallStarted))
    assert started.risk is RiskLevel.SAFE
    assert "list_directory(" in started.description
    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert finished.status == "ok"
    assert events[-1] == AgentDone(reason="completed", model="fake-model")

    roles = [m.role for m in conversation.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    tool_message = conversation.messages[2]
    assert tool_message.tool_call_id == "call_1"
    assert tool_message.name == "list_directory"
    entries = json.loads(tool_message.content)["entries"]
    assert [e["name"] for e in entries] == ["big.iso", "video.mp4"]
    # The second LLM call saw the tool result.
    assert provider.requests[1][0][-1] == tool_message

    entry = json.loads(log.path.read_text().splitlines()[0])
    assert entry["tool"] == "list_directory"
    assert entry["risk"] == "safe"
    assert entry["status"] == "ok"
    assert entry["model"] == "fake-model"
    assert log.path.stat().st_mode & 0o777 == 0o600


async def test_parallel_calls_are_all_answered(context: ToolContext) -> None:
    calls = [call("system_info", "call_a"), call("search_files", "call_b", pattern="*.pdf")]
    provider = ScriptedProvider(reply("", calls), reply("Fait."))
    conversation = Conversation()
    await run(make_agent(provider, context), conversation, "Infos et PDF ?")
    tool_messages = [m for m in conversation.messages if m.role == "tool"]
    assert [m.tool_call_id for m in tool_messages] == ["call_a", "call_b"]
    assert not any(m.is_error for m in tool_messages)


@pytest.mark.parametrize(
    ("bad_call", "message"),
    [
        (call("format_disk"), "Unknown tool: format_disk"),
        (call("search_files", path="~"), "Invalid arguments: 'pattern' is a required property"),
        (call("list_directory", limit="ten"), "Invalid arguments: limit: 'ten' is not of type"),
        (call("crash"), "Internal error in crash: kaboom"),
    ],
)
async def test_failing_calls_are_reported_to_the_model(
    context: ToolContext, bad_call: ToolCall, message: str
) -> None:
    provider = ScriptedProvider(reply("", [bad_call]), reply("Désolé."))
    conversation = Conversation()
    events = await run(make_agent(provider, context, CrashingTool()), conversation, "Vas-y")

    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert finished.status == "error"
    result = conversation.messages[2]
    assert result.is_error
    assert result.content.startswith(message)


async def test_confirm_tool_is_denied_without_approver(context: ToolContext) -> None:
    trash = TrashTool()
    provider = ScriptedProvider(reply("", [call("move_to_trash", path="~/x")]), reply("Refusé."))
    conversation = Conversation()
    events = await run(make_agent(provider, context, trash), conversation, "Supprime x")

    assert trash.calls == []
    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert finished.status == "denied"
    assert conversation.messages[2].content == "The user denied this action."


@pytest.mark.parametrize(("answer", "executed"), [(True, True), (False, False)])
async def test_confirm_tool_follows_approver(
    context: ToolContext, answer: bool, executed: bool
) -> None:
    trash = TrashTool()
    asked: list[tuple[str, ToolCall]] = []

    async def approver(tool: Tool, tool_call: ToolCall) -> bool:
        asked.append((tool.name, tool_call))
        return answer

    trash_call = call("move_to_trash", path="~/x")
    provider = ScriptedProvider(reply("", [trash_call]), reply("Ok."))
    agent = make_agent(provider, context, trash, approver=approver)
    await run(agent, Conversation(), "Supprime x")

    assert asked == [("move_to_trash", trash_call)]
    assert bool(trash.calls) is executed


async def test_failing_approver_means_denied(context: ToolContext) -> None:
    trash = TrashTool()

    async def approver(tool: Tool, tool_call: ToolCall) -> bool:
        raise RuntimeError("UI disconnected")

    provider = ScriptedProvider(reply("", [call("move_to_trash", path="~/x")]), reply("Ok."))
    await run(make_agent(provider, context, trash, approver=approver), Conversation(), "Vas-y")
    assert trash.calls == []


async def test_safe_tools_do_not_ask(context: ToolContext) -> None:
    async def approver(tool: Tool, tool_call: ToolCall) -> bool:
        raise AssertionError("should not be asked")

    provider = ScriptedProvider(reply("", [call("system_info")]), reply("Ok."))
    events = await run(make_agent(provider, context, approver=approver), Conversation(), "Infos")
    assert any(isinstance(e, ToolCallFinished) and e.status == "ok" for e in events)


async def test_iteration_limit(context: ToolContext) -> None:
    provider = ScriptedProvider(reply("", [call("system_info")]))  # always asks for a tool
    conversation = Conversation()
    events = await run(make_agent(provider, context, max_iterations=3), conversation, "Boucle")

    assert len(provider.requests) == 3
    assert events[-1] == AgentDone(reason="iteration_limit", model="fake-model")
    last = conversation.messages[-1]
    assert last.role == "tool" and last.is_error and "step limit" in last.content
    statuses = [e.status for e in events if isinstance(e, ToolCallFinished)]
    assert statuses == ["ok", "ok", "skipped"]


async def test_truncated_tool_calls_are_not_run(context: ToolContext) -> None:
    trash = TrashTool()

    async def approver(tool: Tool, tool_call: ToolCall) -> bool:
        return True

    provider = ScriptedProvider(reply("", [call("move_to_trash", path="~/Doc")], stop="max_tokens"))
    conversation = Conversation()
    events = await run(make_agent(provider, context, trash, approver=approver), conversation, "x")

    assert trash.calls == []
    assert events[-1] == AgentDone(reason="max_tokens", model="fake-model")
    assert conversation.messages[-1].role == "tool"  # history stays valid


async def test_refusal_and_truncation_without_tools(context: ToolContext) -> None:
    provider = ScriptedProvider(reply("Je ne peux pas.", stop="refusal"))
    events = await run(make_agent(provider, context), Conversation(), "x")
    assert events[-1] == AgentDone(reason="refusal", model="fake-model")


async def test_huge_tool_output_is_truncated(context: ToolContext) -> None:
    provider = ScriptedProvider(reply("", [call("verbose")]), reply("Ok."))
    conversation = Conversation()
    await run(make_agent(provider, context, VerboseTool()), conversation, "x")
    content = conversation.messages[2].content
    assert len(content) < 21_000
    assert content.endswith("[output truncated]")


async def test_switch_provider_mid_conversation(context: ToolContext) -> None:
    first = ScriptedProvider(reply("", [call("system_info")]), reply("4 cœurs."))
    second = ScriptedProvider(reply("Oui, 4 cœurs.", model="other-model"))
    agent = make_agent(first, context)
    conversation = Conversation()

    await run(agent, conversation, "Combien de cœurs ?")
    agent.use_provider(second, "other-model")
    events = await run(agent, conversation, "Tu es sûr ?")

    messages, _, model = second.requests[0]
    assert model == "other-model"
    assert [m.role for m in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert events[-1] == AgentDone(reason="completed", model="other-model")


async def test_provider_error_propagates_and_history_stays_usable(context: ToolContext) -> None:
    provider = ScriptedProvider(
        ProviderUnavailableError("offline", provider="scripted"), reply("Rebonjour")
    )
    agent = make_agent(provider, context)
    conversation = Conversation()
    with pytest.raises(ProviderUnavailableError):
        await run(agent, conversation, "Salut")
    events = await run(agent, conversation, "Tu es là ?")
    assert events[-1].reason == "completed"


def test_system_prompt_language(context: ToolContext, home: Path) -> None:
    provider = ScriptedProvider(reply("ok"))
    french = make_agent(provider, context).system_prompt()
    assert "Réponds en français" in french and str(home) in french
    english = make_agent(provider, context, language="en").system_prompt()
    assert "Answer in English" in english
    spanish = make_agent(provider, context, language="es").system_prompt()
    assert "Answer in Spanish" in spanish


def test_invalid_iteration_limit(context: ToolContext) -> None:
    with pytest.raises(ValueError):
        make_agent(ScriptedProvider(reply("ok")), context, max_iterations=0)
