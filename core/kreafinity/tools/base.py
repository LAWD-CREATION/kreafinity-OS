"""Tool interface, risk levels and the filesystem sandbox shared by tools."""

from __future__ import annotations

import json
import os
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from kreafinity.providers.base import Image, ToolSpec

# Folders holding secrets or Kreafinity's own data: never exposed to the
# model, even inside an allowed root.
DEFAULT_DENIED_PATHS = (
    "~/.ssh",
    "~/.gnupg",
    "~/.pki",
    "~/.password-store",
    "~/.local/share/keyrings",
    "~/.local/share/Trash",
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
    """Output of a tool, sent back to the model as text (and images, such as a screenshot)."""

    content: str
    is_error: bool = False
    images: tuple[Image, ...] = ()

    @classmethod
    def json(cls, data: Any, *, is_error: bool = False, images: Sequence[Image] = ()) -> ToolResult:
        """Build a result holding `data` serialized as JSON."""
        return cls(
            json.dumps(data, ensure_ascii=False, default=str),
            is_error=is_error,
            images=tuple(images),
        )


@dataclass(frozen=True, slots=True)
class ActionPreview:
    """What a `confirm`/`danger` call is about to do, shown to the user for approval.

    Attributes:
        summary: One sentence in the user's language (« Mettre 3 éléments à la corbeille »).
        details: The exact targets: paths, URL, processes… one per line.
    """

    summary: str
    details: tuple[str, ...] = ()


def printable(text: str) -> str:
    """Make untrusted text (file names, URLs…) safe to display.

    Control and invisible formatting characters are shown as escapes, so a
    crafted file name cannot rewrite the terminal or disguise itself (ANSI
    sequences, right-to-left override…).
    """
    return "".join(
        char.encode("unicode_escape").decode("ascii")
        if unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp")
        else char
        for char in text
    )


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
            allowed_roots=tuple(_absolute(p, home).resolve() for p in allowed_paths),
            denied_paths=tuple(_absolute(p, home).resolve() for p in denied_paths),
        )

    def resolve(self, raw: str | None) -> Path:
        """Turn a path given by the model into the absolute path of its target.

        ``~`` and relative paths start from the home directory. Symlinks are
        followed before checking, so a link cannot lead outside the sandbox.
        Use this to read or open something.

        Raises:
            ToolError: The path is outside the allowed folders or denied.
        """
        return self._check(_absolute(raw or "~", self.home).resolve())

    def resolve_entry(self, raw: str) -> Path:
        """Like `resolve`, but keep a symlink itself instead of its target.

        Use this to move, rename or trash an entry: acting on a link must
        never affect the file it points to.
        """
        path = Path(os.path.normpath(_absolute(raw, self.home)))
        if path.name:
            path = path.parent.resolve() / path.name
        return self._check(path)

    def is_allowed(self, path: Path) -> bool:
        """Whether an absolute `path` is inside the sandbox."""
        if any(path.is_relative_to(denied) for denied in self.denied_paths):
            return False
        return any(path.is_relative_to(root) for root in self.allowed_roots)

    def is_protected(self, path: Path) -> bool:
        """Whether `path` is the home folder, an allowed root or one of their parents.

        Such folders must never be moved or trashed as a whole.
        """
        return any(root.is_relative_to(path) for root in (self.home, *self.allowed_roots))

    def _check(self, path: Path) -> Path:
        if not self.is_allowed(path):
            raise ToolError(f"Access denied: {path} is outside the folders you may access")
        return path


def _absolute(raw: str, home: Path) -> Path:
    raw = raw.strip()
    if raw == "~" or raw.startswith("~/"):
        return home / raw[2:]
    if raw.startswith("~"):
        raise ToolError(f"Unsupported path: {raw}")
    path = Path(raw)
    return path if path.is_absolute() else home / path


class Tool(ABC):
    """An action the assistant can take on the machine.

    Subclasses set the class attributes, implement `run` and register
    themselves with `kreafinity.tools.register_tool`. Arguments are validated
    against `parameters` before `preview` and `run` are called. Tools that
    are not `SAFE` should override `preview` to show exactly what they will do.
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
        """One-line technical summary of a call, shown while it runs and logged."""
        args = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
        return f"{self.name}({args})"

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        """Describe what the call will do, before the user approves it.

        Also the place to check the arguments (paths exist, are allowed…):
        raising `ToolError` here reports the problem to the model without
        bothering the user.
        """
        return ActionPreview(summary=self.describe(arguments))

    @abstractmethod
    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute the tool.

        Args:
            arguments: Arguments chosen by the model, already validated
                against `parameters` (but still untrusted: check paths with
                `ToolContext.resolve` or `ToolContext.resolve_entry`).
            context: What the tool may access.

        Raises:
            ToolError: Expected failure, reported to the model.
        """
