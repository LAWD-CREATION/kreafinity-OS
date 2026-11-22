"""Provider-neutral types and the `LLMProvider` interface.

Every LLM backend (Anthropic, OpenAI, Mistral, Ollama…) is an adapter that
converts this neutral format to and from its own API. The agent, the history
store and the user interfaces only ever see the types defined here, which is
what makes switching provider in the middle of a conversation possible.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

Role = Literal["system", "user", "assistant", "tool"]
StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "other"]


def new_tool_call_id() -> str:
    """Return a fresh tool call id, for providers whose API does not supply one."""
    return f"call_{uuid.uuid4().hex}"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A request from the model to run a tool.

    Attributes:
        id: Identifier linking the call to its result message.
        name: Name of the tool to run.
        arguments: Parsed JSON arguments, as produced by the model (untrusted).
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Image:
    """An image shown to the model (a screenshot).

    Attributes:
        media_type: ``image/jpeg`` or ``image/png``.
        data: The image's bytes, base64-encoded.
    """

    media_type: str
    data: str


#: Text introducing images sent in their own message (APIs whose tool results are text only).
IMAGES_INTRO = "Images returned by the tool calls above (from {names}):"

#: Said to a model that cannot read images, instead of the image.
NO_VISION_NOTE = (
    "\n[The image could not be shown: the model {model} does not read images. Tell the user "
    "to choose a model that can, for example Claude, GPT, Mistral Small or Pixtral online, or "
    "qwen2.5vl or llava with Ollama.]"
)


@dataclass(frozen=True, slots=True)
class Message:
    """One message of a conversation, in the neutral format.

    Attributes:
        role: ``system``, ``user``, ``assistant`` or ``tool`` (a tool result).
        content: Text of the message (for ``tool`` messages: the tool output).
        tool_calls: Tools the assistant asked to run (``assistant`` only).
        tool_call_id: Id of the call this message answers (``tool`` only).
        name: Name of the tool that produced this result (``tool`` only).
        is_error: Whether the tool failed (``tool`` only).
        images: Images that come with a tool result (``tool`` only), such as a
            screenshot. They are never saved in the history: the model sees
            them during the request that produced them.
        provider_data: Opaque data an adapter attaches to the messages it
            produces, keyed by provider name, and reads back when the same
            provider sees the conversation again (e.g. Anthropic thinking
            blocks, which must be replayed unchanged). Other providers ignore
            it, so the conversation stays portable.
    """

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    is_error: bool = False
    provider_data: dict[str, Any] = field(default_factory=dict)
    images: tuple[Image, ...] = ()

    @classmethod
    def system(cls, content: str) -> Message:
        """Build a system message."""
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        """Build a user message."""
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str = "", tool_calls: Sequence[ToolCall] = ()) -> Message:
        """Build an assistant message."""
        return cls(role="assistant", content=content, tool_calls=tuple(tool_calls))

    @classmethod
    def tool_result(
        cls,
        tool_call_id: str,
        name: str,
        content: str,
        *,
        is_error: bool = False,
        images: Sequence[Image] = (),
    ) -> Message:
        """Build the message carrying the result of a tool call."""
        return cls(
            role="tool",
            content=content,
            tool_call_id=tool_call_id,
            name=name,
            is_error=is_error,
            images=tuple(images),
        )

    def without_images(self, note: str) -> Message:
        """The same message with its images replaced by `note` (appended to the text)."""
        return Message(
            role=self.role,
            content=self.content + note,
            tool_calls=self.tool_calls,
            tool_call_id=self.tool_call_id,
            name=self.name,
            is_error=self.is_error,
            provider_data=self.provider_data,
        )


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Description of a tool, as advertised to the model.

    Attributes:
        name: Unique tool name (letters, digits, ``_`` and ``-``).
        description: What the tool does, written for the model.
        parameters: JSON Schema of the tool arguments (an ``object`` schema).
    """

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Usage:
    """Token usage reported by the provider, when available."""

    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A chunk of assistant text, emitted while the response streams."""

    text: str
    type: Literal["text_delta"] = "text_delta"


@dataclass(frozen=True, slots=True)
class ResponseDone:
    """Last event of a response stream.

    Attributes:
        message: The complete assistant message (text, tool calls and
            provider data), ready to be appended to the conversation.
        stop_reason: Why the model stopped. Tool calls must only be run when
            it is ``tool_use``: after ``max_tokens`` or ``refusal`` their
            arguments may be truncated.
        model: The model that actually produced the response.
        usage: Token usage, when the provider reports it.
    """

    message: Message
    stop_reason: StopReason
    model: str
    usage: Usage = field(default_factory=Usage)
    type: Literal["done"] = "done"


StreamEvent = TextDelta | ResponseDone


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """A model offered by a provider."""

    id: str
    provider: str
    display_name: str | None = None


class ProviderError(Exception):
    """Base error raised by provider adapters.

    Attributes:
        provider: Name of the provider that failed.
        retryable: Whether retrying later may succeed (rate limit, overload).
    """

    def __init__(self, message: str, *, provider: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


class ProviderUnavailableError(ProviderError):
    """The provider cannot be reached (no network, local server not running…).

    Callers may fall back to another provider (e.g. a local Ollama model).
    """

    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(message, provider=provider, retryable=True)


class ProviderAuthError(ProviderError):
    """The provider rejected the credentials (missing or invalid API key)."""


class LLMProvider(ABC):
    """Common interface of every LLM provider adapter.

    Subclasses set the class attributes, implement `chat` and `list_models`,
    and register themselves with `kreafinity.providers.register_provider`.
    """

    #: Identifier used in configuration and on the CLI (``ollama``, ``anthropic``…).
    name: ClassVar[str]
    #: Human-readable name shown in the interfaces.
    display_name: ClassVar[str]
    #: Whether the models run on this machine (usable without Internet access).
    is_local: ClassVar[bool] = False
    #: Whether an API key is needed (it is then passed as ``api_key``).
    requires_api_key: ClassVar[bool] = False
    #: Environment variable that may hold the API key.
    api_key_env_var: ClassVar[str | None] = None

    def __init__(self, *, default_model: str) -> None:
        self.default_model = default_model

    @property
    def supports_tools(self) -> bool:
        """Whether this provider can be given tools to call."""
        return True

    @abstractmethod
    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        *,
        model: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Send a conversation and stream the assistant's response.

        Args:
            messages: The conversation so far, in the neutral format. System
                messages carry the system prompt.
            tools: Tools the model may call.
            model: Model to use; defaults to `default_model`.

        Yields:
            Zero or more `TextDelta` events, then exactly one `ResponseDone`.

        Raises:
            ProviderUnavailableError: The provider cannot be reached.
            ProviderAuthError: The credentials were rejected.
            ProviderError: Any other provider failure.
        """

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]:
        """Return the models available with this provider."""

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        *,
        model: str | None = None,
    ) -> ResponseDone:
        """Run `chat` to the end and return its final event."""
        async for event in self.chat(messages, tools, model=model):
            if isinstance(event, ResponseDone):
                return event
        raise ProviderError("Response stream ended without a final event", provider=self.name)

    async def aclose(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Release network resources held by the provider."""
