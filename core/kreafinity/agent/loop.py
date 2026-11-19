"""The agent loop: user message → LLM → tool calls → results → LLM → answer."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import jsonschema

from kreafinity.agent.action_log import ActionLog, ActionStatus
from kreafinity.agent.prompts import build_system_prompt
from kreafinity.paths import user_dirs
from kreafinity.providers.base import (
    LLMProvider,
    Message,
    ProviderUnavailableError,
    ResponseDone,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
)
from kreafinity.tools.base import (
    RiskLevel,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
    printable,
)
from kreafinity.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS = 10
# Longer tool outputs are cut to keep the conversation within context limits.
MAX_TOOL_RESULT_CHARS = 20_000


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """A `confirm`/`danger` tool call waiting for the user's decision.

    Attributes:
        call: The call, with the exact arguments that will be used.
        tool: Name of the tool.
        risk: ``confirm`` (simple approval) or ``danger`` (explicit approval
            after showing exactly what will happen).
        summary: What will happen, in the user's language.
        details: The exact targets (paths, URL…), safe to display.
    """

    call: ToolCall
    tool: str
    risk: RiskLevel
    summary: str
    details: tuple[str, ...] = ()


#: Asked before running a `confirm` or `danger` tool; returns True to allow it.
#: Anything else than True (False, an exception…) denies the action.
Approver = Callable[[ApprovalRequest], Awaitable[bool]]


INTERRUPTED_RESULT = "Not executed: the request was interrupted."


def complete_unanswered_calls(messages: Sequence[Message]) -> list[Message]:
    """Give every tool call a result, adding an error result where one is missing.

    A request interrupted between a tool call and its result (cancelled,
    crashed, Ctrl-C) would otherwise leave a conversation that providers
    reject on the next request.
    """
    result: list[Message] = []
    pending: dict[str, ToolCall] = {}

    def answer_pending() -> None:
        for call in pending.values():
            result.append(
                Message.tool_result(call.id, call.name, INTERRUPTED_RESULT, is_error=True)
            )
        pending.clear()

    for message in messages:
        if message.role == "tool":
            pending.pop(message.tool_call_id or "", None)
        else:
            answer_pending()
            if message.role == "assistant":
                pending.update((call.id, call) for call in message.tool_calls)
        result.append(message)
    answer_pending()
    return result


@dataclass
class Conversation:
    """A conversation in the neutral message format (system prompt excluded)."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    messages: list[Message] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """The agent is about to run (or ask approval for) a tool."""

    call: ToolCall
    description: str
    risk: RiskLevel | None
    type: Literal["tool_started"] = "tool_started"


@dataclass(frozen=True, slots=True)
class ToolCallFinished:
    """A tool call ended (successfully or not)."""

    call: ToolCall
    status: ActionStatus
    result: ToolResult
    type: Literal["tool_finished"] = "tool_finished"


FinishReason = Literal["completed", "iteration_limit", "max_tokens", "refusal", "other"]


@dataclass(frozen=True, slots=True)
class AgentDone:
    """The request is over.

    Attributes:
        reason: ``completed`` when the model answered; otherwise why it stopped.
        model: Model that produced the last response.
    """

    reason: FinishReason
    model: str | None
    type: Literal["agent_done"] = "agent_done"


@dataclass(frozen=True, slots=True)
class ProviderFallback:
    """The provider could not be reached; the agent switched to the fallback one.

    Attributes:
        unavailable: Display name of the provider that failed.
        fallback: Display name of the provider now in use.
        reason: Why the first provider failed.
    """

    unavailable: str
    fallback: str
    reason: str
    type: Literal["provider_fallback"] = "provider_fallback"


AgentEvent = TextDelta | ToolCallStarted | ToolCallFinished | ProviderFallback | AgentDone


class Agent:
    """Runs requests against an LLM provider with a set of tools.

    The provider and model can be changed at any time between requests
    (`use_provider`); the conversation is kept in the neutral format.
    """

    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry,
        context: ToolContext,
        *,
        model: str | None = None,
        language: str = "fr",
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        approver: Approver | None = None,
        action_log: ActionLog | None = None,
        fallback: tuple[LLMProvider, str | None] | None = None,
    ) -> None:
        """Create an agent.

        Args:
            provider: LLM provider to use.
            tools: Tools the model may call.
            context: What the tools may access.
            model: Model to use; defaults to the provider's default model.
            language: Language of the system prompt and of the answers.
            max_iterations: Maximum LLM calls per request.
            approver: Asked before every `confirm`/`danger` tool call. Without
                one, those calls are always refused.
            action_log: Where tool calls are recorded (none: not recorded).
            fallback: Provider and model to switch to when the current
                provider cannot be reached (e.g. a local Ollama model when
                there is no Internet connection).
        """
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        self.provider = provider
        self.model = model
        self.tools = tools
        self.context = context
        self.language = language
        self.max_iterations = max_iterations
        self.approver = approver
        self.action_log = action_log
        self.fallback = fallback

    def use_provider(self, provider: LLMProvider, model: str | None = None) -> None:
        """Switch provider and model; takes effect from the next LLM call."""
        self.provider = provider
        self.model = model

    def system_prompt(self) -> str:
        """The system prompt sent with every request."""
        return build_system_prompt(
            language=self.language,
            home=self.context.home,
            allowed_roots=self.context.allowed_roots,
            user_dirs=user_dirs(self.context.home),
        )

    async def run(self, conversation: Conversation, user_text: str) -> AsyncIterator[AgentEvent]:
        """Handle one user request, appending every message to `conversation`.

        Yields text as it streams, tool call notifications, and finally one
        `AgentDone`. Provider errors propagate (`ProviderError`); the
        conversation keeps what was done so far and stays usable.
        """
        conversation.messages[:] = complete_unanswered_calls(conversation.messages)
        conversation.messages.append(Message.user(user_text))
        system = Message.system(self.system_prompt())
        specs = self.tools.specs() if self.provider.supports_tools else []
        model: str | None = None

        for iteration in range(1, self.max_iterations + 1):
            done: ResponseDone | None = None
            async for event in self._stream_response([system, *conversation.messages], specs):
                if isinstance(event, ResponseDone):
                    done = event
                else:
                    yield event
            if done is None:  # pragma: no cover - providers always end with ResponseDone
                raise RuntimeError("Provider stream ended without a final event")
            model = done.model
            conversation.messages.append(done.message)
            calls = done.message.tool_calls

            if not calls:
                reason: FinishReason = "completed" if done.stop_reason == "end_turn" else "other"
                if done.stop_reason in ("max_tokens", "refusal"):
                    reason = done.stop_reason
                yield AgentDone(reason=reason, model=model)
                return

            if done.stop_reason != "tool_use":
                # Truncated or refused: the arguments may be incomplete. Answer
                # every call so the conversation stays valid, but run nothing.
                async for tool_event in self._skip_calls(
                    conversation, calls, "Not executed: the response was cut off.", model
                ):
                    yield tool_event
                yield AgentDone(
                    reason="max_tokens" if done.stop_reason == "max_tokens" else "refusal",
                    model=model,
                )
                return

            if iteration == self.max_iterations:
                async for tool_event in self._skip_calls(
                    conversation,
                    calls,
                    "Not executed: the step limit for this request was reached.",
                    model,
                ):
                    yield tool_event
                yield AgentDone(reason="iteration_limit", model=model)
                return

            for call in calls:
                async for tool_event in self._run_call(conversation, call, model):
                    yield tool_event

        raise AssertionError("unreachable")  # pragma: no cover

    async def _stream_response(
        self, messages: list[Message], specs: list[ToolSpec]
    ) -> AsyncIterator[StreamEvent | ProviderFallback]:
        """Call the provider, switching to the fallback one if it cannot be reached."""
        started = False
        try:
            async for event in self.provider.chat(messages, specs, model=self.model):
                started = True
                yield event
            return
        except ProviderUnavailableError as exc:
            # Only before anything was received, and never from the fallback itself.
            if started or self.fallback is None or self.provider is self.fallback[0]:
                raise
            unavailable = self.provider.display_name
            self.use_provider(*self.fallback)
            yield ProviderFallback(
                unavailable=unavailable, fallback=self.provider.display_name, reason=str(exc)
            )
        async for event in self.provider.chat(messages, specs, model=self.model):
            yield event

    async def _run_call(
        self, conversation: Conversation, call: ToolCall, model: str | None
    ) -> AsyncIterator[AgentEvent]:
        tool = self.tools.get(call.name)
        description = tool.describe(call.arguments) if tool else call.name
        yield ToolCallStarted(call=call, description=description, risk=tool.risk if tool else None)

        status: ActionStatus
        if tool is None:
            status, result = "error", ToolResult(f"Unknown tool: {call.name}", is_error=True)
        elif (problem := _validation_error(tool, call.arguments)) is not None:
            status, result = "error", ToolResult(f"Invalid arguments: {problem}", is_error=True)
        elif tool.risk is RiskLevel.SAFE:
            status, result = await self._execute(tool, call)
        else:
            status, result = await self._run_with_approval(tool, call)

        if len(result.content) > MAX_TOOL_RESULT_CHARS:
            result = ToolResult(
                result.content[:MAX_TOOL_RESULT_CHARS] + "\n[output truncated]", result.is_error
            )
        self._log(call, tool, status, result, model)
        conversation.messages.append(
            Message.tool_result(call.id, call.name, result.content, is_error=result.is_error)
        )
        yield ToolCallFinished(call=call, status=status, result=result)

    async def _run_with_approval(
        self, tool: Tool, call: ToolCall
    ) -> tuple[ActionStatus, ToolResult]:
        """Run a `confirm`/`danger` tool only if the user approves it."""
        if self.approver is None:
            return "denied", ToolResult(
                "This action needs the user's approval, which is not available here.",
                is_error=True,
            )
        try:
            preview = await tool.preview(call.arguments, self.context)
        except ToolError as exc:
            return "error", ToolResult(str(exc), is_error=True)
        except Exception as exc:
            logger.exception("Preview of %s crashed", call.name)
            return "error", ToolResult(f"Internal error in {call.name}: {exc}", is_error=True)

        request = ApprovalRequest(
            call=call,
            tool=tool.name,
            risk=tool.risk,
            summary=printable(preview.summary),
            details=tuple(printable(line) for line in preview.details),
        )
        try:
            approved = await self.approver(request)
        except Exception:
            logger.exception("Approval of %s failed; treated as denied", call.name)
            approved = False
        if approved is not True:
            return "denied", ToolResult("The user denied this action.", is_error=True)
        return await self._execute(tool, call)

    async def _execute(self, tool: Tool, call: ToolCall) -> tuple[ActionStatus, ToolResult]:
        try:
            result = await tool.run(call.arguments, self.context)
        except ToolError as exc:
            return "error", ToolResult(str(exc), is_error=True)
        except Exception as exc:
            logger.exception("Tool %s crashed", call.name)
            return "error", ToolResult(f"Internal error in {call.name}: {exc}", is_error=True)
        return ("error" if result.is_error else "ok"), result

    async def _skip_calls(
        self, conversation: Conversation, calls: tuple[ToolCall, ...], why: str, model: str | None
    ) -> AsyncIterator[AgentEvent]:
        for call in calls:
            result = ToolResult(why, is_error=True)
            self._log(call, self.tools.get(call.name), "skipped", result, model)
            conversation.messages.append(
                Message.tool_result(call.id, call.name, why, is_error=True)
            )
            yield ToolCallFinished(call=call, status="skipped", result=result)

    def _log(
        self,
        call: ToolCall,
        tool: Tool | None,
        status: ActionStatus,
        result: ToolResult,
        model: str | None,
    ) -> None:
        if self.action_log is None:
            return
        try:
            self.action_log.record(
                tool=call.name,
                risk=tool.risk.value if tool else None,
                arguments=call.arguments,
                status=status,
                detail=result.content[:300] if status != "ok" else None,
                model=model,
            )
        except OSError:
            logger.exception("Cannot write the action log")


def _validation_error(tool: Tool, arguments: dict[str, Any]) -> str | None:
    validator = jsonschema.Draft202012Validator(tool.parameters)
    error = jsonschema.exceptions.best_match(validator.iter_errors(arguments))
    if error is None:
        return None
    location = "/".join(str(p) for p in error.absolute_path)
    return f"{location}: {error.message}" if location else error.message
