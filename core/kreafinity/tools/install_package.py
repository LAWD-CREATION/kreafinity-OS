"""Tool: install system packages (``danger``: administrator rights through polkit)."""

from __future__ import annotations

import asyncio
import shlex
from typing import Any, ClassVar

from kreafinity.i18n import t
from kreafinity.system import packages
from kreafinity.tools.base import (
    ActionPreview,
    RiskLevel,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
)
from kreafinity.tools.registry import register_tool

#: Dependencies named in the approval card; the others are counted.
SHOWN_DEPENDENCIES = 8


@register_tool
class InstallPackage(Tool):
    name = "install_package"
    description = (
        "Install software from the system's package repositories (APT: Debian, Ubuntu), "
        "given exact package names (e.g. 'vlc', 'gimp'). The system then asks the user for "
        "their administrator password. Unknown names are reported: then ask the user or try "
        "another name."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "packages": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 100},
                "minItems": 1,
                "maxItems": 20,
                "description": "Package names.",
            }
        },
        "required": ["packages"],
        "additionalProperties": False,
    }
    risk = RiskLevel.DANGER

    async def _plan(self, arguments: dict[str, Any]) -> packages.InstallPlan:
        try:
            return await asyncio.to_thread(packages.plan_install, arguments["packages"])
        except packages.PackageError as exc:
            raise ToolError(f"Cannot install: {exc}") from exc

    async def preview(self, arguments: dict[str, Any], context: ToolContext) -> ActionPreview:
        plan = await self._plan(arguments)
        details = [f"{name} {version}" for name, version in plan.packages]
        if plan.dependencies:
            shown = ", ".join(name for name, _ in plan.dependencies[:SHOWN_DEPENDENCIES])
            if len(plan.dependencies) > SHOWN_DEPENDENCIES:
                shown += ", …"
            details.append(
                t("tool.install_package.dependencies", count=len(plan.dependencies), names=shown)
            )
        details.append(t("tool.install_package.command", command=shlex.join(plan.command)))
        return ActionPreview(
            summary=t("tool.install_package.summary", count=len(plan.packages) or 1),
            details=tuple(details),
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        plan = await self._plan(arguments)
        try:
            result = await packages.install(plan)
        except packages.PackageError as exc:
            raise ToolError(f"Installation failed: {exc}") from exc
        installed = {
            name: await asyncio.to_thread(packages.installed_version, name)
            for name, _ in plan.packages
        }
        return ToolResult.json(
            {
                "installed": installed,
                "dependencies_installed": len(plan.dependencies),
                "already_installed": list(plan.already_installed),
                "duration_seconds": result.duration,
            }
        )
