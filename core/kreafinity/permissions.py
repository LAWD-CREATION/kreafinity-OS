"""Actions the user allowed for good (« Toujours autoriser »).

A `confirm`/`danger` tool call normally waits for the user's approval. From an
approval card, the user may allow it for good instead; the rule is kept in the
``always_allow`` setting and listed (and removable) in the settings:

- ``"open_application"``: every call of that tool;
- ``"run_command ls -lh ~/Téléchargements"``: exactly that shell command
  (the shell tool can do anything, so it may also be allowed one command at a
  time).

Allowed actions still go through each tool's own limits (allowed folders, the
trash instead of deletion, pkexec for administrator actions…) and are recorded
in the action log.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

#: The shell tool, which can also be allowed one exact command at a time.
COMMAND_TOOL = "run_command"
MAX_RULE_LENGTH = 2000
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def tool_rule(tool: str) -> str:
    """The rule allowing every call of `tool`."""
    return tool


def command_rule(command: str) -> str | None:
    """The rule allowing exactly `command`, or None if it cannot be one (several lines…)."""
    command = command.strip()
    if not command or "\n" in command or "\r" in command or len(command) > MAX_RULE_LENGTH:
        return None
    return f"{COMMAND_TOOL} {command}"


def options(tool: str, arguments: dict[str, Any]) -> list[dict[str, str]]:
    """The « always allow » choices offered for a call, narrowest first.

    Each is ``{"rule": …, "scope": "command" | "tool"}``.
    """
    choices = []
    if tool == COMMAND_TOOL and isinstance(arguments.get("command"), str):
        rule = command_rule(arguments["command"])
        if rule:
            choices.append({"rule": rule, "scope": "command"})
    choices.append({"rule": tool_rule(tool), "scope": "tool"})
    return choices


def allows(rules: Iterable[str], tool: str, arguments: dict[str, Any]) -> bool:
    """Whether the user allowed this call for good."""
    rules = set(rules)
    if tool_rule(tool) in rules:
        return True
    if tool == COMMAND_TOOL and isinstance(arguments.get("command"), str):
        rule = command_rule(arguments["command"])
        return rule is not None and rule in rules
    return False


def describe(rule: str) -> dict[str, str]:
    """``{"rule", "tool", "command"?}``: a rule as shown in the settings."""
    tool, _, command = rule.partition(" ")
    entry = {"rule": rule, "tool": tool}
    if command:
        entry["command"] = command
    return entry


def is_valid(rule: str) -> bool:
    """Whether `rule` is a tool name, or the shell tool followed by one command."""
    tool, space, command = rule.partition(" ")
    if not _TOOL_NAME.fullmatch(tool) or len(rule) > MAX_RULE_LENGTH:
        return False
    if not space:
        return True
    return tool == COMMAND_TOOL and command_rule(command) == rule
