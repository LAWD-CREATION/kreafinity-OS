"""The assistant service: conversations, requests, approvals and live events.

Independent of any user interface: the daemon's API exposes it over HTTP and
WebSocket, and the future desktop shell can use it directly. Everything runs
on one asyncio event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal

from kreafinity.agent import (
    ActionLog,
    Agent,
    AgentDone,
    AgentEvent,
    ApprovalRequest,
    Conversation,
    ConversationNotFoundError,
    ConversationStore,
    ProviderFallback,
    ToolCallFinished,
    ToolCallStarted,
    complete_unanswered_calls,
)
from kreafinity.config import ConfigError, Settings, load_settings, save_settings
from kreafinity.i18n import set_language
from kreafinity.providers import (
    LLMProvider,
    Message,
    ProviderAuthError,
    ProviderError,
    ProviderUnavailableError,
    TextDelta,
    available_providers,
    create_provider,
    get_provider_class,
    parse_model_spec,
)
from kreafinity.tools import ToolContext, ToolRegistry, printable

logger = logging.getLogger(__name__)

#: An approval nobody answers is denied after this many seconds.
APPROVAL_TIMEOUT = 600.0
#: Events kept for a slow client before it must resynchronize.
EVENT_QUEUE_SIZE = 5000
#: Length of the tool output excerpt sent with ``tool_finished`` events.
TOOL_RESULT_EXCERPT = 2000

ErrorCode = Literal[
    "not_found",
    "busy",
    "invalid",
    "invalid_config",
    "provider_unavailable",
    "provider_auth",
    "provider_error",
]
Event = dict[str, Any]


class ServiceError(Exception):
    """A request the service cannot fulfil.

    Attributes:
        code: Machine-readable reason, mapped to an HTTP status by the API.
    """

    def __init__(self, message: str, *, code: ErrorCode) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


# --- Events ---------------------------------------------------------------------------


class Subscription:
    """The events received by one client (e.g. one WebSocket connection)."""

    def __init__(self, maxsize: int = EVENT_QUEUE_SIZE) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize)

    async def get(self) -> Event:
        """Wait for the next event."""
        return await self._queue.get()


class EventHub:
    """Broadcasts events to every subscriber, numbered in order (``seq``)."""

    def __init__(self, queue_size: int = EVENT_QUEUE_SIZE) -> None:
        self._subscribers: set[Subscription] = set()
        self._queue_size = queue_size
        self._seq = 0

    def subscribe(self) -> Subscription:
        """Start receiving events."""
        subscription = Subscription(self._queue_size)
        self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Stop receiving events."""
        self._subscribers.discard(subscription)

    def publish(self, event: Event) -> None:
        """Send an event to all subscribers.

        A subscriber too slow to keep up is dropped: its queue is replaced by
        a single ``resync_required`` event, after which it receives nothing.
        """
        self._seq += 1
        event = {"seq": self._seq, **event}
        for subscription in list(self._subscribers):
            try:
                subscription._queue.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers.discard(subscription)
                while not subscription._queue.empty():
                    subscription._queue.get_nowait()
                subscription._queue.put_nowait({"seq": self._seq, "type": "resync_required"})


# --- Runs and approvals --------------------------------------------------------------------


@dataclass
class PendingApproval:
    """A `confirm`/`danger` action waiting for the user's decision."""

    id: str
    conversation_id: str
    run_id: str
    request: ApprovalRequest
    created_at: str
    future: asyncio.Future[bool] = field(repr=False)

    def to_dict(self) -> Event:
        """JSON-serializable form, as shown to the user."""
        return {
            "approval_id": self.id,
            "conversation_id": self.conversation_id,
            "run_id": self.run_id,
            "tool": self.request.tool,
            "risk": self.request.risk.value,
            "summary": self.request.summary,
            "details": list(self.request.details),
            "arguments": self.request.call.arguments,
            "created_at": self.created_at,
        }


@dataclass
class Run:
    """A request being processed for a conversation."""

    id: str
    conversation_id: str
    provider: str
    model: str
    task: asyncio.Task[None] | None = field(default=None, repr=False)


def message_to_dict(message: Message) -> Event:
    """JSON form of a message for clients (internal provider data left out)."""
    result: Event = {"role": message.role, "content": message.content}
    if message.tool_calls:
        result["tool_calls"] = [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in message.tool_calls
        ]
    if message.role == "tool":
        result.update(
            tool_call_id=message.tool_call_id, name=message.name, is_error=message.is_error
        )
    return result


class AssistantService:
    """Conversations with the assistant, shared by all the clients of the daemon."""

    def __init__(
        self,
        store: ConversationStore,
        *,
        settings_loader: Callable[[], Settings] = load_settings,
        provider_factory: Callable[..., LLMProvider] = create_provider,
        context: ToolContext | None = None,
        action_log: ActionLog | None = None,
        approval_timeout: float = APPROVAL_TIMEOUT,
    ) -> None:
        """Create the service.

        Args:
            store: Where conversations are kept.
            settings_loader: Reads the settings (called for each request, so
                changes to the configuration file apply without a restart).
            provider_factory: Creates a provider from its name and options.
            context: Sandbox of the tools; by default built from the settings.
            action_log: Journal of the actions (by default the user's one).
            approval_timeout: Seconds after which an unanswered approval is denied.
        """
        self.store = store
        self.events = EventHub()
        self.approval_timeout = approval_timeout
        self._load_settings = settings_loader
        self._provider_factory = provider_factory
        self._context = context
        self._action_log = action_log or ActionLog()
        self._providers: dict[str, LLMProvider] = {}
        self._runs: dict[str, Run] = {}
        self._approvals: dict[str, PendingApproval] = {}

    # --- Settings and providers --------------------------------------------------------

    def settings(self) -> Settings:
        """Current settings (re-read from the configuration file)."""
        try:
            settings = self._load_settings()
        except ConfigError as exc:
            raise ServiceError(str(exc), code="invalid_config") from exc
        set_language(settings.language)
        return settings

    def _provider(self, settings: Settings, spec: str) -> tuple[LLMProvider, str | None]:
        try:
            name, model = parse_model_spec(spec)
        except ValueError as exc:
            raise ServiceError(str(exc), code="invalid") from exc
        if name not in available_providers():
            known = ", ".join(available_providers())
            raise ServiceError(f"Unknown provider {name!r} (available: {known})", code="invalid")
        options = settings.provider_options(name)
        key = f"{name}:{json.dumps(options, sort_keys=True, default=str)}"
        if key not in self._providers:
            try:
                self._providers[key] = self._provider_factory(name, **options)
            except TypeError as exc:
                raise ServiceError(
                    f"Invalid options for provider {name!r}: {exc}", code="invalid_config"
                ) from exc
        return self._providers[key], model

    def list_providers(self) -> Event:
        """Available providers, with the default and fallback ones."""
        settings = self.settings()
        providers = []
        for name in available_providers():
            provider_class = get_provider_class(name)
            providers.append(
                {
                    "name": name,
                    "display_name": provider_class.display_name,
                    "is_local": provider_class.is_local,
                }
            )
        return {"default": settings.provider, "fallback": settings.fallback, "providers": providers}

    async def list_models(self, name: str) -> list[Event]:
        """Models offered by a provider."""
        provider, _ = self._provider(self.settings(), name)
        try:
            models = await provider.list_models()
        except ProviderError as exc:
            raise _service_error(exc) from exc
        return [{"id": m.id, "display_name": m.display_name or m.id} for m in models]

    def set_default_provider(self, spec: str) -> Event:
        """Choose the provider and model used by default."""
        self._provider(self.settings(), spec)  # validate
        return self.update_config({"provider": spec.strip()})

    def config(self) -> Event:
        """The current settings."""
        return asdict(self.settings())

    def update_config(self, changes: dict[str, Any]) -> Event:
        """Change settings, validated together, and return the new settings."""
        try:
            save_settings(changes)
        except ConfigError as exc:
            raise ServiceError(str(exc), code="invalid") from exc
        result = self.config()
        self.events.publish({"type": "config_updated", "config": result})
        return result

    def list_tools(self) -> list[Event]:
        """Registered tools, with their risk level and whether they are enabled."""
        settings = self.settings()
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "risk": tool.risk.value,
                "enabled": settings.tools is None or tool.name in settings.tools,
            }
            for tool in ToolRegistry.load()
        ]

    # --- Conversations -----------------------------------------------------------------------

    def _info(self, conversation_id: str) -> Event:
        try:
            info = self.store.get(conversation_id).to_dict()
        except ConversationNotFoundError as exc:
            raise ServiceError("Conversation not found", code="not_found") from exc
        run = self._runs.get(conversation_id)
        info["running"] = run is not None
        info["run_id"] = run.id if run else None
        return info

    def list_conversations(self, *, limit: int = 50, offset: int = 0) -> list[Event]:
        """Conversations, most recent first."""
        result = []
        for info in self.store.list_conversations(limit=limit, offset=offset):
            entry = info.to_dict()
            run = self._runs.get(info.id)
            entry["running"] = run is not None
            entry["run_id"] = run.id if run else None
            result.append(entry)
        return result

    def create_conversation(self, title: str = "") -> Event:
        """Start a new, empty conversation."""
        info = self.store.create(title=title)
        event = self._info(info.id)
        self.events.publish({"type": "conversation_updated", "conversation": event})
        return event

    def get_conversation(self, conversation_id: str) -> Event:
        """A conversation with its messages."""
        info = self._info(conversation_id)
        info["messages"] = [message_to_dict(m) for m in self.store.messages(conversation_id)]
        return info

    def rename_conversation(self, conversation_id: str, title: str) -> Event:
        """Change the title of a conversation."""
        try:
            self.store.rename(conversation_id, title)
        except ConversationNotFoundError as exc:
            raise ServiceError("Conversation not found", code="not_found") from exc
        event = self._info(conversation_id)
        self.events.publish({"type": "conversation_updated", "conversation": event})
        return event

    async def delete_conversation(self, conversation_id: str) -> None:
        """Delete a conversation, stopping its request if one is running."""
        if conversation_id in self._runs:
            await self.cancel_run(conversation_id)
        try:
            self.store.delete(conversation_id)
        except ConversationNotFoundError as exc:
            raise ServiceError("Conversation not found", code="not_found") from exc
        self.events.publish({"type": "conversation_deleted", "conversation_id": conversation_id})

    # --- Requests ---------------------------------------------------------------------------

    async def send_message(
        self, conversation_id: str, text: str, provider: str | None = None
    ) -> Event:
        """Start processing a user message; progress is published as events.

        Args:
            conversation_id: The conversation.
            text: The user's message.
            provider: ``provider/model`` to use from now on for this
                conversation (changing provider keeps the conversation).

        Returns:
            The run: ``run_id``, ``provider`` and ``model``.
        """
        if conversation_id in self._runs:
            raise ServiceError("A request is already running in this conversation", code="busy")
        info = self._info(conversation_id)
        settings = self.settings()
        spec = (provider or info["provider"] or settings.provider).strip()
        llm, model = self._provider(settings, spec)
        if provider:
            self.store.set_provider(conversation_id, spec)

        fallback = None
        if settings.fallback and settings.fallback.strip() != spec:
            fallback_provider = self._provider(settings, settings.fallback)
            if fallback_provider[0] is not llm or fallback_provider[1] != model:
                fallback = fallback_provider

        run = Run(
            id=uuid.uuid4().hex,
            conversation_id=conversation_id,
            provider=llm.name,
            model=model or llm.default_model,
        )
        agent = Agent(
            llm,
            ToolRegistry.load(settings.tools),
            self._context or ToolContext.create(allowed_paths=settings.allowed_paths),
            model=model,
            language=settings.language,
            max_iterations=settings.max_iterations,
            approver=partial(self._ask_approval, run),
            action_log=self._action_log,
            fallback=fallback,
        )
        self._runs[conversation_id] = run
        run.task = asyncio.create_task(self._process(run, agent, text))
        return {"run_id": run.id, "provider": run.provider, "model": run.model}

    async def cancel_run(self, conversation_id: str) -> None:
        """Stop the request running in a conversation."""
        run = self._runs.get(conversation_id)
        if run is None or run.task is None:
            raise ServiceError("No request is running in this conversation", code="not_found")
        run.task.cancel()
        await asyncio.wait([run.task])

    async def _process(self, run: Run, agent: Agent, text: str) -> None:
        conversation_id = run.conversation_id
        base = {"conversation_id": conversation_id, "run_id": run.id}
        loaded = self.store.messages(conversation_id)
        messages = complete_unanswered_calls(loaded)
        if messages != loaded:
            self.store.replace_messages(conversation_id, messages)
        conversation = Conversation(id=conversation_id, messages=messages)
        saved = len(messages)

        def persist() -> None:
            nonlocal saved
            new = conversation.messages[saved:]
            if new:
                self.store.append(conversation_id, new)
                saved = len(conversation.messages)

        self.events.publish(
            {
                "type": "run_started",
                **base,
                "provider": run.provider,
                "provider_name": agent.provider.display_name,
                "model": run.model,
                "content": text,
            }
        )
        try:
            async for event in agent.run(conversation, text):
                persist()
                self.events.publish({**base, **_event_to_dict(event)})
        except asyncio.CancelledError:
            persist()
            self.events.publish({"type": "run_finished", **base, "reason": "cancelled"})
            raise
        except ProviderError as exc:
            persist()
            error = _service_error(exc)
            self.events.publish(
                {
                    "type": "run_failed",
                    **base,
                    "code": error.code,
                    "provider": exc.provider,
                    "message": str(exc),
                }
            )
        except Exception as exc:
            logger.exception("Request failed in conversation %s", conversation_id)
            persist()
            self.events.publish(
                {"type": "run_failed", **base, "code": "internal", "message": str(exc)}
            )
        finally:
            if self._runs.get(conversation_id) is run:
                del self._runs[conversation_id]
            for approval in list(self._approvals.values()):
                if approval.run_id == run.id and not approval.future.done():
                    approval.future.cancel()
            with contextlib.suppress(ServiceError):  # unless deleted meanwhile
                self.events.publish(
                    {"type": "conversation_updated", "conversation": self._info(conversation_id)}
                )

    # --- Approvals ------------------------------------------------------------------------

    async def _ask_approval(self, run: Run, request: ApprovalRequest) -> bool:
        approval = PendingApproval(
            id=uuid.uuid4().hex,
            conversation_id=run.conversation_id,
            run_id=run.id,
            request=request,
            created_at=_now(),
            future=asyncio.get_running_loop().create_future(),
        )
        self._approvals[approval.id] = approval
        self.events.publish({"type": "approval_required", **approval.to_dict()})
        try:
            approved = await asyncio.wait_for(approval.future, self.approval_timeout)
        except TimeoutError:
            self.events.publish(
                {
                    "type": "approval_resolved",
                    "approval_id": approval.id,
                    "conversation_id": run.conversation_id,
                    "approved": False,
                    "reason": "timeout",
                }
            )
            return False
        finally:
            self._approvals.pop(approval.id, None)
        return approved is True

    def pending_approvals(self) -> list[Event]:
        """Actions waiting for the user's decision."""
        return [a.to_dict() for a in self._approvals.values() if not a.future.done()]

    def resolve_approval(self, approval_id: str, approved: bool) -> Event:
        """Record the user's decision about a pending action."""
        approval = self._approvals.get(approval_id)
        if approval is None or approval.future.done():
            raise ServiceError("No pending approval with this id", code="not_found")
        approval.future.set_result(approved is True)
        event = {
            "type": "approval_resolved",
            "approval_id": approval_id,
            "conversation_id": approval.conversation_id,
            "approved": approved is True,
            "reason": "user",
        }
        self.events.publish(event)
        return event

    def running(self) -> list[Event]:
        """Requests currently being processed."""
        return [
            {
                "conversation_id": run.conversation_id,
                "run_id": run.id,
                "provider": run.provider,
                "model": run.model,
            }
            for run in self._runs.values()
        ]

    # --- Shutdown ---------------------------------------------------------------------------

    async def aclose(self) -> None:
        """Stop running requests and release the providers."""
        tasks = [run.task for run in self._runs.values() if run.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)
        for provider in self._providers.values():
            try:
                await provider.aclose()
            except Exception:
                logger.exception("Closing provider %s failed", provider.name)
        self._providers.clear()


def _event_to_dict(event: AgentEvent) -> Event:
    if isinstance(event, TextDelta):
        return {"type": "text_delta", "text": event.text}
    if isinstance(event, ToolCallStarted):
        return {
            "type": "tool_started",
            "call_id": event.call.id,
            "tool": event.call.name,
            "arguments": event.call.arguments,
            "description": printable(event.description),
            "risk": event.risk.value if event.risk else None,
        }
    if isinstance(event, ToolCallFinished):
        return {
            "type": "tool_finished",
            "call_id": event.call.id,
            "tool": event.call.name,
            "status": event.status,
            "is_error": event.result.is_error,
            "result": event.result.content[:TOOL_RESULT_EXCERPT],
        }
    if isinstance(event, ProviderFallback):
        return {
            "type": "provider_fallback",
            "unavailable": event.unavailable,
            "fallback": event.fallback,
            "reason": event.reason,
        }
    if isinstance(event, AgentDone):
        return {"type": "run_finished", "reason": event.reason, "model": event.model}
    raise TypeError(f"Unknown agent event: {event!r}")  # pragma: no cover


def _service_error(exc: ProviderError) -> ServiceError:
    if isinstance(exc, ProviderUnavailableError):
        return ServiceError(str(exc), code="provider_unavailable")
    if isinstance(exc, ProviderAuthError):
        return ServiceError(str(exc), code="provider_auth")
    return ServiceError(str(exc), code="provider_error")
