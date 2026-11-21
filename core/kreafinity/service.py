"""The assistant service: conversations, requests, approvals and live events.

Independent of any user interface: the daemon's API exposes it over HTTP and
WebSocket, and the future desktop shell can use it directly. Everything runs
on one asyncio event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import hashlib
import json
import logging
import os
import pwd
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal

import httpx

from kreafinity import __version__, update
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
    review,
)
from kreafinity.agent.review import ReviewState
from kreafinity.config import (
    ConfigError,
    SecretStoreError,
    Settings,
    api_key_source,
    delete_api_key,
    get_api_key,
    keyring_available,
    load_settings,
    resolve_provider_options,
    save_settings,
    set_api_key,
)
from kreafinity.i18n import set_language
from kreafinity.memory import (
    MAX_SECTION,
    MAX_SOUL,
    MEMORY_TOOLS,
    MemoryRefusedError,
    MemoryStore,
)
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
from kreafinity.system.notifications import send_notification
from kreafinity.tools import ToolContext, ToolRegistry, printable
from kreafinity.voice.manager import VoiceManager

logger = logging.getLogger(__name__)

#: An approval nobody answers is denied after this many seconds.
APPROVAL_TIMEOUT = 600.0
#: Events kept for a slow client before it must resynchronize.
EVENT_QUEUE_SIZE = 5000
#: Length of the tool output excerpt sent with ``tool_finished`` events.
TOOL_RESULT_EXCERPT = 2000
#: A request to show a conversation is ignored by panels after this many seconds.
UI_REQUEST_MAX_AGE = 60.0
#: A conversation quiet for this long is reviewed to improve the memory.
REVIEW_DELAY = 120.0

ErrorCode = Literal[
    "not_found",
    "busy",
    "invalid",
    "invalid_config",
    "provider_unavailable",
    "provider_auth",
    "provider_error",
    "keyring_unavailable",
    "update_unavailable",
    "voice_unavailable",
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


def _left_out_tools(settings: Settings) -> frozenset[str]:
    """Memory tools the settings turn off (all with the memory, the soul's alone)."""
    if not settings.memory:
        return MEMORY_TOOLS
    return frozenset() if settings.soul_evolution else frozenset({"soul"})


# --- Events ---------------------------------------------------------------------------


class Subscription:
    """The events received by one client (e.g. one WebSocket connection).

    Attributes:
        panel: The client is a compact assistant panel (desktop bubble, Plasma
            widget), able to show a conversation when asked.
    """

    def __init__(self, maxsize: int = EVENT_QUEUE_SIZE, *, panel: bool = False) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize)
        self.panel = panel

    async def get(self) -> Event:
        """Wait for the next event."""
        return await self._queue.get()


class EventHub:
    """Broadcasts events to every subscriber, numbered in order (``seq``)."""

    def __init__(self, queue_size: int = EVENT_QUEUE_SIZE) -> None:
        self._subscribers: set[Subscription] = set()
        self._queue_size = queue_size
        self._seq = 0

    def subscribe(self, *, panel: bool = False) -> Subscription:
        """Start receiving events (`panel`: the client is an assistant panel)."""
        subscription = Subscription(self._queue_size, panel=panel)
        self._subscribers.add(subscription)
        return subscription

    def panel_count(self) -> int:
        """Number of assistant panels currently listening."""
        return sum(1 for subscription in self._subscribers if subscription.panel)

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


class UiRequests:
    """Requests to show a conversation, for panels polling over HTTP (Plasma widget).

    Each request is numbered; a panel asks for the requests after the last
    number it saw, waiting until one arrives.
    """

    def __init__(self, keep: int = 20) -> None:
        self._items: deque[tuple[int, float, Event]] = deque(maxlen=keep)
        self._seq = 0
        self._changed = asyncio.Condition()
        self.waiting = 0

    @property
    def last(self) -> int:
        return self._seq

    async def push(self, request: Event) -> None:
        async with self._changed:
            self._seq += 1
            self._items.append((self._seq, time.monotonic(), {"seq": self._seq, **request}))
            self._changed.notify_all()

    async def wait(self, after: int | None, timeout: float) -> tuple[list[Event], int]:
        """Requests numbered after `after`, waiting up to `timeout` seconds for one.

        Without `after` (a panel starting), nothing old is replayed: only the
        current number is returned, to ask from.
        """
        if after is None or after > self._seq:  # starting, or the daemon restarted
            return [], self._seq
        self.waiting += 1
        try:
            async with self._changed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._changed.wait_for(lambda: self._seq > after), timeout
                    )
                oldest = time.monotonic() - UI_REQUEST_MAX_AGE
                fresh = [event for seq, at, event in self._items if seq > after and at >= oldest]
                return fresh, self._seq
        finally:
            self.waiting -= 1


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
        notifier: Callable[[str, str], Awaitable[bool]] | None = send_notification,
        update_launcher: Callable[[str], None] = update.launch,
        update_client: httpx.AsyncClient | None = None,
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
            notifier: Shows a desktop notification (summary, body).
            update_launcher: Starts installing an update (a commit) in its own process.
            update_client: HTTP client used to ask GitHub for updates (for tests).
        """
        self.store = store
        self.events = EventHub()
        self.ui_requests = UiRequests()
        self.approval_timeout = approval_timeout
        self._load_settings = settings_loader
        self._provider_factory = provider_factory
        self._context = context
        self._action_log = action_log or ActionLog()
        self._providers: dict[str, LLMProvider] = {}
        self._retired: list[LLMProvider] = []
        self._runs: dict[str, Run] = {}
        self._approvals: dict[str, PendingApproval] = {}
        self.updates = update.UpdateManager(
            publish=self.events.publish,
            enabled=lambda: self.settings().check_updates,
            notify=notifier,
            launcher=update_launcher,
            client=update_client,
        )
        self.voice = VoiceManager(self.settings, self.events.publish, get_api_key)
        self._background: list[asyncio.Task[None]] = []
        self._reviews: dict[str, asyncio.Task[None]] = {}
        self.review_delay = REVIEW_DELAY

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
        options = resolve_provider_options(name, settings.provider_options(name))
        fingerprint = json.dumps(options, sort_keys=True, default=str).encode()
        key = f"{name}:{hashlib.sha256(fingerprint).hexdigest()}"
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
                    "requires_api_key": provider_class.requires_api_key,
                    "api_key": api_key_source(name),
                    "api_key_env_var": provider_class.api_key_env_var,
                }
            )
        return {
            "default": settings.provider,
            "fallback": settings.fallback,
            "keyring_available": keyring_available(),
            "providers": providers,
        }

    def _key_provider(self, name: str) -> None:
        if name not in available_providers():
            raise ServiceError(f"Unknown provider {name!r}", code="not_found")
        if not get_provider_class(name).requires_api_key:
            raise ServiceError(f"{name} does not use an API key", code="invalid")

    def set_api_key(self, name: str, key: str) -> Event:
        """Store a provider's API key in the system keyring."""
        self._key_provider(name)
        try:
            set_api_key(name, key)
        except ValueError as exc:
            raise ServiceError("Invalid API key", code="invalid") from exc
        except SecretStoreError as exc:
            raise ServiceError(str(exc), code="keyring_unavailable") from exc
        return self._keys_changed(name)

    def delete_api_key(self, name: str) -> Event:
        """Remove a provider's API key from the system keyring."""
        self._key_provider(name)
        try:
            delete_api_key(name)
        except SecretStoreError as exc:
            raise ServiceError(str(exc), code="keyring_unavailable") from exc
        return self._keys_changed(name)

    def _keys_changed(self, name: str) -> Event:
        # Providers created with the old key are retired (closed at shutdown).
        for key in [k for k in self._providers if k.startswith(f"{name}:")]:
            self._retired.append(self._providers.pop(key))
        result = {"provider": name, "api_key": api_key_source(name)}
        self.events.publish({"type": "providers_updated", **result})
        return result

    def info(self) -> Event:
        """About this installation, for the interfaces."""
        home = self._context.home if self._context else Path.home()
        return {
            "version": __version__,
            "home": str(home),
            "user_name": _first_name(),
            "keyring_available": keyring_available(),
        }

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

    async def show_conversation(self, conversation_id: str) -> Event:
        """Ask the assistant panels to show a conversation (e.g. after a desktop search).

        Returns:
            ``delivered``: how many panels were listening (0: nobody will show it).
        """
        self._info(conversation_id)  # must exist
        request = {"type": "show_conversation", "conversation_id": conversation_id}
        delivered = self.events.panel_count() + self.ui_requests.waiting
        self.events.publish(request)
        await self.ui_requests.push(request)
        return {"delivered": delivered}

    async def wait_ui_requests(self, after: int | None, timeout: float) -> Event:
        """Requests to show a conversation, for a panel polling over HTTP."""
        requests, last = await self.ui_requests.wait(after, timeout)
        return {"requests": requests, "last": last}

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
            ToolRegistry.load(settings.tools, exclude=_left_out_tools(settings)),
            self._context or ToolContext.create(allowed_paths=settings.allowed_paths),
            model=model,
            language=settings.language,
            max_iterations=settings.max_iterations,
            approver=partial(self._ask_approval, run),
            action_log=self._action_log,
            fallback=fallback,
            memory=MemoryStore() if settings.memory else None,
            soul=MemoryStore().soul(settings.language),
        )
        pending = self._reviews.pop(conversation_id, None)
        if pending is not None:  # the conversation goes on: review it later
            pending.cancel()
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
                # Title, message count and date changed: let the interfaces know.
                self.events.publish(
                    {"type": "conversation_updated", "conversation": self._info(conversation_id)}
                )

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
        completed = False
        try:
            async for event in agent.run(conversation, text):
                persist()
                self.events.publish({**base, **_event_to_dict(event)})
            completed = True
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
            if completed:
                self._schedule_review(conversation_id, f"{run.provider}/{run.model}")

    # --- Memory: learning from conversations ---------------------------------------------

    def _schedule_review(self, conversation_id: str, spec: str) -> None:
        settings = self.settings()
        if not (settings.memory and settings.memory_review):
            return
        previous = self._reviews.pop(conversation_id, None)
        if previous is not None:
            previous.cancel()

        async def later() -> None:
            await asyncio.sleep(self.review_delay)
            try:
                await self.review_conversation(conversation_id, spec)
            except Exception:
                logger.exception("Review of conversation %s failed", conversation_id)
            finally:
                if self._reviews.get(conversation_id) is task:
                    del self._reviews[conversation_id]

        task = asyncio.create_task(later())
        self._reviews[conversation_id] = task

    async def review_conversation(self, conversation_id: str, spec: str | None = None) -> int:
        """Reread what is new in a conversation and improve the memory from it.

        Returns:
            How many changes were made to the memory and skills.
        """
        settings = self.settings()
        if not settings.memory:
            return 0
        state = ReviewState()
        try:
            messages = self.store.messages(conversation_id)
        except ConversationNotFoundError:
            return 0
        new = messages[state.reviewed(conversation_id) :]
        if not any(message.role == "user" for message in new):
            return 0
        try:
            llm, model = self._provider(settings, spec or settings.provider)
        except ServiceError as exc:
            logger.info("No review of %s: %s", conversation_id, exc)
            return 0
        memory = MemoryStore()
        agent = Agent(
            llm,
            ToolRegistry.load(sorted(MEMORY_TOOLS), exclude=_left_out_tools(settings)),
            self._context or ToolContext.create(allowed_paths=settings.allowed_paths),
            model=model,
            language=settings.language,
            max_iterations=6,
            action_log=self._action_log,
            memory=memory,
            instructions=review.instructions(settings.language),
            soul=memory.soul(settings.language),
        )
        changes = 0
        soul_changed = False
        try:
            async for event in agent.run(Conversation(), review.transcript(new, settings.language)):
                if isinstance(event, ToolCallFinished) and event.status == "ok":
                    changes += 1
                    soul_changed = soul_changed or event.call.name == "soul"
        except ProviderError as exc:
            logger.info("Review of %s failed: %s", conversation_id, exc)
            return 0  # retried after the next exchange
        state.mark(conversation_id, len(messages))
        if changes:
            self.events.publish(
                {
                    "type": "memory_updated",
                    "conversation_id": conversation_id,
                    "changes": changes,
                    "soul": soul_changed,
                }
            )
        return changes

    def memory_info(self) -> Event:
        """What the assistant remembers (for the settings)."""
        settings = self.settings()
        store = MemoryStore()
        return {
            "enabled": settings.memory,
            "review": settings.memory_review,
            "soul": store.soul(settings.language),
            "soul_is_seed": not store.has_own_soul(),
            "soul_evolution": settings.soul_evolution,
            "soul_limit": MAX_SOUL,
            "user": store.read("user"),
            "notes": store.read("notes"),
            "skills": [
                {
                    "name": skill.name,
                    "title": skill.title,
                    "description": skill.description,
                    "uses": skill.uses,
                    "updated": skill.updated,
                }
                for skill in store.skills()
            ],
            "limit": MAX_SECTION,
        }

    def write_memory(self, section: str, content: str) -> Event:
        """Replace a notebook with the user's text."""
        store = MemoryStore()
        try:
            if section == "soul":
                store.write_soul(content)
            else:
                store.write(section, content)
        except MemoryRefusedError as exc:
            raise ServiceError(str(exc), code="invalid") from exc
        result = self.memory_info()
        self.events.publish({"type": "memory_updated", "changes": 1})
        return result

    def delete_skill(self, name: str) -> Event:
        try:
            MemoryStore().delete_skill(name)
        except MemoryRefusedError as exc:
            raise ServiceError(str(exc), code="not_found") from exc
        return self.memory_info()

    def reset_soul(self) -> Event:
        """Give the assistant back the personality it started with."""
        MemoryStore().reset_soul()
        self.events.publish({"type": "memory_updated", "changes": 1, "soul": True})
        return self.memory_info()

    def forget_memory(self) -> Event:
        """Erase everything the assistant remembers."""
        MemoryStore().forget_all()
        self.events.publish({"type": "memory_updated", "changes": 1})
        return self.memory_info()

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

    # --- Updates ------------------------------------------------------------------------------

    def start_background_tasks(self) -> None:
        """Start the daily update check (called by the daemon once running)."""
        self._background.append(asyncio.create_task(self.updates.watch()))

    def update_info(self) -> Event:
        """Installed version, available update, progress of an installation."""
        return self.updates.info()

    async def check_updates(self) -> Event:
        """Look for an update now."""
        try:
            return await self.updates.check()
        except update.UpdateError as exc:
            raise _update_error(exc) from exc

    def start_update(self) -> Event:
        """Install the available update (the daemon then restarts)."""
        try:
            return self.updates.start()
        except update.UpdateError as exc:
            raise _update_error(exc) from exc

    async def aclose(self) -> None:
        """Stop running requests and release the providers."""
        for review_task in list(self._reviews.values()):
            review_task.cancel()
        self._reviews.clear()
        for background in self._background:
            background.cancel()
        for background in self._background:
            with contextlib.suppress(asyncio.CancelledError):
                await background
        self._background.clear()
        tasks = [run.task for run in self._runs.values() if run.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)
        for provider in [*self._providers.values(), *self._retired]:
            try:
                await provider.aclose()
            except Exception:
                logger.exception("Closing provider %s failed", provider.name)
        self._providers.clear()
        self._retired.clear()


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


def _first_name() -> str:
    """The user's first name (from the account's full name), or the login name."""
    try:
        full_name = pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0].strip()
    except (KeyError, OSError):
        full_name = ""
    if full_name:
        return full_name.split()[0]
    return getpass.getuser()


def _service_error(exc: ProviderError) -> ServiceError:
    if isinstance(exc, ProviderUnavailableError):
        return ServiceError(str(exc), code="provider_unavailable")
    if isinstance(exc, ProviderAuthError):
        return ServiceError(str(exc), code="provider_auth")
    return ServiceError(str(exc), code="provider_error")


def _update_error(exc: update.UpdateError) -> ServiceError:
    codes: dict[str, ErrorCode] = {"busy": "busy", "network": "update_unavailable"}
    return ServiceError(str(exc), code=codes.get(exc.code, "invalid"))
