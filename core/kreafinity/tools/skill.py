"""Tool: skills, the procedures the assistant learned and keeps improving."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from kreafinity.memory import MemoryRefusedError, MemoryStore, slugify
from kreafinity.tools.base import RiskLevel, Tool, ToolContext, ToolError, ToolResult
from kreafinity.tools.registry import register_tool


@register_tool
class Skill(Tool):
    name = "skill"
    description = (
        "Your skills: procedures you learned for multi-step tasks, listed in your memory. "
        "'read' a skill before a task similar to its description, and follow it (actions still "
        "need the user's approval). After succeeding at a new multi-step task, 'save' how you "
        "did it (steps, tools, pitfalls) so you do better next time; when you find a better "
        "way or a skill failed, 'save' it again with the same name to improve it. 'delete' a "
        "skill that is wrong."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "save", "delete"]},
            "name": {
                "type": "string",
                "maxLength": 80,
                "description": "Skill name, e.g. 'liberer-espace-disque'.",
            },
            "title": {"type": "string", "maxLength": 100, "description": "For 'save'."},
            "description": {
                "type": "string",
                "maxLength": 200,
                "description": "For 'save': when to use it, in one sentence.",
            },
            "steps": {
                "type": "string",
                "maxLength": 4000,
                "description": "For 'save': the procedure, in Markdown.",
            },
        },
        "required": ["action", "name"],
        "additionalProperties": False,
    }
    risk = RiskLevel.SAFE  # the assistant's own notes; visible and erasable in the settings

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        store = MemoryStore()
        action = arguments["action"]
        name = slugify(arguments["name"])
        try:
            if action == "read":
                skill = await asyncio.to_thread(store.use_skill, name)
                return ToolResult.json(
                    {"name": skill.name, "title": skill.title, "steps": skill.steps}
                )
            if action == "save":
                missing = [
                    key for key in ("title", "description", "steps") if not arguments.get(key)
                ]
                if missing:
                    raise ToolError(f"To save a skill, give: {', '.join(missing)}")
                skill = await asyncio.to_thread(
                    store.save_skill,
                    name,
                    arguments["title"],
                    arguments["description"],
                    arguments["steps"],
                )
                return ToolResult.json({"saved": skill.name})
            await asyncio.to_thread(store.delete_skill, name)
            return ToolResult.json({"deleted": name})
        except MemoryRefusedError as exc:
            raise ToolError(str(exc)) from exc
