"""Tool: hardware and operating system information."""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import time
from typing import Any, ClassVar

import psutil

from kreafinity.tools.base import RiskLevel, Tool, ToolContext, ToolResult
from kreafinity.tools.registry import register_tool

GIB = 1024**3
# Pseudo and read-only image filesystems that are not real disks.
IGNORED_FILESYSTEMS = frozenset({"squashfs", "tmpfs", "devtmpfs", "overlay", "iso9660"})


@register_tool
class SystemInfo(Tool):
    name = "system_info"
    description = (
        "Get information about the computer: operating system, CPU (model, cores, current "
        "usage), memory, disk space and battery."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    risk = RiskLevel.SAFE

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult.json(await asyncio.to_thread(collect_system_info))


def collect_system_info() -> dict[str, Any]:
    """Gather system information (blocks ~0.3 s to measure CPU usage)."""
    return {
        "os": _os_info(),
        "cpu": _cpu_info(),
        "memory": _memory_info(),
        "disks": _disks_info(),
        "battery": _battery_info(),
    }


def _os_info() -> dict[str, Any]:
    try:
        name = platform.freedesktop_os_release().get("PRETTY_NAME", platform.system())
    except OSError:
        name = platform.system()
    return {
        "name": name,
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "hostname": platform.node(),
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
    }


def _cpu_model() -> str | None:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as cpuinfo:
            for line in cpuinfo:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or None


def _cpu_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "model": _cpu_model(),
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(logical=True),
        "usage_percent": psutil.cpu_percent(interval=0.3),
    }
    with contextlib.suppress(OSError):
        info["load_average"] = [round(v, 2) for v in os.getloadavg()]
    return info


def _memory_info() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "total_gib": round(memory.total / GIB, 1),
        "available_gib": round(memory.available / GIB, 1),
        "used_percent": memory.percent,
        "swap_total_gib": round(swap.total / GIB, 1),
        "swap_used_percent": swap.percent,
    }


def _disks_info() -> list[dict[str, Any]]:
    disks = []
    seen_devices: set[str] = set()
    for partition in psutil.disk_partitions(all=False):
        if partition.fstype in IGNORED_FILESYSTEMS or partition.device in seen_devices:
            continue
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except OSError:
            continue
        seen_devices.add(partition.device)
        disks.append(
            {
                "mountpoint": partition.mountpoint,
                "filesystem": partition.fstype,
                "total_gib": round(usage.total / GIB, 1),
                "free_gib": round(usage.free / GIB, 1),
                "used_percent": usage.percent,
            }
        )
    return disks


def _battery_info() -> dict[str, Any] | None:
    sensors_battery = getattr(psutil, "sensors_battery", None)
    battery = sensors_battery() if sensors_battery else None
    if battery is None:
        return None
    seconds = battery.secsleft
    return {
        "percent": round(battery.percent),
        "plugged_in": battery.power_plugged,
        "time_left_minutes": round(seconds / 60)
        if isinstance(seconds, int) and seconds > 0
        else None,
    }
