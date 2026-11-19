"""Tool interface, risk levels and the filesystem sandbox shared by tools."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from kreafinity.providers.base import ToolSpec

# Folders holding secrets: never exposed to the model, even inside an allowed root.
DEFAULT_DENIED_PATHS = (
    "~/.ssh",
    "~/.gnupg",
    "~/.pki",
    "~/.password-store",
    "~/.local/share/keyrings",
    "~/.config/kreafinity",
    "~/.local/share/kreafinity",
)


class RiskLevel(StrEnum):
    """How dangerous a tool is, which decides whether the user must approve it."""

    SAFE = "safe"
    """Read-only, run directly (system information, listing a folder…)."""
    CONFIRM = "confirm"
    """Changes something: the user must approve before it runs."""
    DANGER = "danger"
    """Destructive or arbitrary: explicit approval showing the exact action."""


class ToolError(Exception):
    """A tool failed in an expected way; the message is shown to the model."""


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Output of a tool, sent back to the model as text."""

    content: str
    is_error: bool = False

    @classmethod
    def json(cls, data: Any) -> ToolResult:
        """Build a result holding `data` serialized as JSON."""
        return cls(json.dumps(data, ensure_ascii=False, default=str))


@dataclass(frozen=True)
class ToolContext:
    """What a tool may access on this machine.

    Attributes:
        home: The user's home directory; relative paths and ``~`` start here.
        allowed_roots: Folders the tools may access (and their contents).
        denied_paths: Folders that stay off-limits even inside an allowed root.
    """

    home: Path
    allowed_roots: tuple[Path, ...]
    denied_paths: tuple[Path, ...] = field(default=())

    @classmethod
    def create(
        cls,
        home: Path | None = None,
        allowed_paths: Iterable[str] = ("~",),
        denied_paths: Iterable[str] = DEFAULT_DENIED_PATHS,
    ) -> ToolContext:
        """Build a context, expanding ``~`` and resolving symlinks in the paths."""
        home = (home or Path.home()).resolve()
        return cls(
            home=home,
            allowed_roots=tuple(_expand(p, home) for p in allowed_paths),
            denied_paths=tuple(_expand(p, home) for p in denied_paths),
        )

    def resolve(self, raw: str | None) -> Path:
        """Turn a path given by the model into an absolute path the tools may use.

        ``~`` and relative paths start from the home directory. Symlinks are
        resolved before checking, so a link cannot lead outside the sandbox.

        Raises:
            ToolError: The path is outside the allowed folders or denied.
        """
        path = _expand(raw or "~", self.home)
        if not self.is_allowed(path):
            raise ToolError(f"Access denied: {path} is outside the folders you may access")
        return path

    def is_allowed(self, path: Path) -> bool:
        """Whether an absolute, resolved `path` is inside the sandbox."""
        if any(path.is_relative_to(denied) for denied in self.denied_paths):
            return False
        return any(path.is_relative_to(root) for root in self.allowed_roots)


def _expand(raw: str, home: Path) -> Path:
    raw = raw.strip()
    if raw == "~" or raw.startswith("~/"):
        path = home / raw[2:]
    elif raw.startswith("~"):
        raise ToolError(f"Unsupported path: {raw}")
    else:
        path = Path(raw)
        if not path.is_absolute():
            path = home / path
    return path.resolve()


class Tool(ABC):
    """An action the assistant can take on the machine.

    Subclasses set the class attributes, implement `run` and register
    themselves with `kreafinity.tools.register_tool`. Arguments are validated
    against `parameters` before `run` is called.
    """

    #: Unique name, as the model sees it (``snake_case``).
    name: ClassVar[str]
    #: What the tool does and when to use it, written for the model (English).
    description: ClassVar[str]
    #: JSON Schema of the arguments (an ``object`` schema).
    parameters: ClassVar[dict[str, Any]]
    #: Risk level, deciding whether the user must approve each call.
    risk: ClassVar[RiskLevel]

    @classmethod
    def spec(cls) -> ToolSpec:
        """Description of the tool given to the LLM provider."""
        return ToolSpec(name=cls.name, description=cls.description, parameters=cls.parameters)

    def describe(self, arguments: dict[str, Any]) -> str:
        """One-line summary of a call, shown to the user and in the action log."""
        args = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
        return f"{self.name}({args})"

    @abstractmethod
    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute the tool.

        Args:
            arguments: Arguments chosen by the model, already validated
                against `parameters` (but still untrusted: check paths with
                `ToolContext.resolve`).
            context: What the tool may access.

        Raises:
            ToolError: Expected failure, reported to the model.
        """
