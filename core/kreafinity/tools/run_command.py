"""Tool: run a shell command with the user's rights (``danger``: exact command shown)."""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path
from typing import Any, ClassVar

from kreafinity.i18n import t
from kreafinity.providers import available_providers, get_provider_class
from kreafinity.system.process import run_captured
from kreafinity.tools.base import (
    ActionPreview,
    RiskLevel,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
)
from kreafinity.tools.registry import register_tool

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600

#: Programs refused in a command, and why. A safeguard on top of the user's
#: approval, not a sandbox: the command runs with all the user's rights.
FORBIDDEN_PROGRAMS = {
    **dict.fromkeys(("sudo", "su", "doas", "pkexec", "run0", "gksudo", "kdesu"), "root"),
    **dict.fromkeys(("rm", "rmdir", "shred", "unlink", "srm", "wipe"), "delete"),
}
REFUSAL = {
    "root": (
        "Commands are never run with administrator rights ({program}): to install "
        "software, use install_package; otherwise, explain to the user how to do it."
    ),
    "delete": (
        "Deleting with {program} is not allowed: use move_to_trash, so that the files "
        "can be restored from the trash."
    ),
}
# Words after which the next word is a command: separators, keywords, wrappers.
_SEPARATORS = frozenset({";", "&", "&&", "|", "||", "|&", "(", ")", "!", "{", "}"})
_KEYWORDS = frozenset({"then", "do", "else", "elif", "if", "while", "until", "time"})
_WRAPPERS = frozenset(
    {
        "xargs", "env", "nohup", "nice", "ionice", "timeout", "exec", "command", "builtin",
        "stdbuf", "watch", "setsid", "flock", "chrt", "taskset", "eval", "unbuffer",
        "systemd-run", "-exec", "-execdir", "-ok", "-okdir",
    }
)  # fmt: skip
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "fish"})


def command_programs(command: str, depth: int = 0) -> list[str]:
    """Names of the programs a shell command line runs, as far as they can be seen.

    Raises:
        ValueError: The command cannot be parsed (unbalanced quotes).
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    programs: list[str] = []
    expect_command = True
    current = ""
    for token in lexer:
        word = token.strip("`$(){}")
        if token in _SEPARATORS or token == "$" or token.startswith("`"):
            expect_command = True
        if depth < 3 and any(char.isspace() for char in token):  # bash -c "…", '…'
            programs += command_programs(token, depth + 1)
        if not word or token in _SEPARATORS or word in _KEYWORDS:
            continue
        if expect_command:
            if word.startswith("-") or word.isdigit() or _is_assignment(word):
                continue  # option or setting of a wrapper (nice -n 5, VAR=1…)
            current = os.path.basename(word)
            programs.append(current)
            expect_command = current in _WRAPPERS
        elif word in _WRAPPERS or (word == "-c" and current in _SHELLS):
            expect_command = True
    return programs


def _is_assignment(word: str) -> bool:
    name, equal, _ = word.partition("=")
    return bool(equal) and name.replace("_", "a").isalnum()


@register_tool
class RunCommand(Tool):
    name = "run_command"
    description = (
        "Run a shell command (bash) with the user's rights, without a terminal, and return "
        "its output. Use it only when no other tool can do the task. The command gets no "
        "input, must finish on its own and cannot use sudo: use install_package to install "
        "software, move_to_trash to delete, open_application to start a graphical program."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4000,
                "description": "The command line, as typed in a terminal.",
            },
            "folder": {
                "type": "string",
                "description": "Folder to run it in (default: the home folder).",
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_TIMEOUT,
                "description": f"Stop the command after this time (default {DEFAULT_TIMEOUT}).",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }
    risk = RiskLevel.DANGER

    def _plan(self, arguments: dict[str, Any], context: ToolContext) -> tuple[str, Path, int]:
        command = arguments["command"]
        try:
            programs = command_programs(command)
        except ValueError as exc:
            raise ToolError(f"Cannot read this command line: {exc}") from exc
        for program in programs:
            reason = FORBIDDEN_PROGRAMS.get(program)
            if reason is not None:
                raise ToolError(REFUSAL[reason].format(program=program))
        folder = context.resolve(arguments.get("folder"))
        if not folder.is_dir():
            raise ToolError(f"Not a folder: {folder}")
        return command, folder, arguments.get("timeout_seconds", DEFAULT_TIMEOUT)

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        command, folder, timeout = self._plan(arguments, context)
        return ActionPreview(
            summary=t("tool.run_command.summary"),
            details=(
                f"$ {command}",
                t("tool.run_command.folder", path=str(folder)),
                t("tool.run_command.timeout", seconds=timeout),
            ),
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        command, folder, timeout = self._plan(arguments, context)
        shell = shutil.which("bash") or "/bin/sh"
        try:
            result = await run_captured(
                [shell, "-c", command], timeout=timeout, cwd=folder, env=_environment()
            )
        except OSError as exc:
            raise ToolError(f"Cannot run the command: {exc}") from exc
        data: dict[str, Any] = {
            "exit_code": result.exit_code,
            "output": result.output,
            "duration_seconds": result.duration,
        }
        if result.timed_out:
            data["note"] = f"Stopped after {timeout} seconds."
        return ToolResult.json(data, is_error=result.exit_code != 0)


def _environment() -> dict[str, str]:
    """The daemon's environment, without the API keys of the AI providers."""
    secrets = set()
    for name in available_providers():
        variable = get_provider_class(name).api_key_env_var
        if variable:
            secrets.add(variable)
    env = {key: value for key, value in os.environ.items() if key not in secrets}
    env.update(TERM="dumb", PAGER="cat", GIT_PAGER="cat", SYSTEMD_PAGER="")
    return env
