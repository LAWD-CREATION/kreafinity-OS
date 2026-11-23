"""The MCP servers of this Kreafinity process: started, watched, restarted, stopped."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from kreafinity.mcp import config as mcp_config
from kreafinity.mcp.client import (
    CALL_TIMEOUT,
    START_TIMEOUT,
    Connection,
    HttpConnection,
    McpError,
    StdioConnection,
    initialize,
    list_tools,
)
from kreafinity.mcp.config import ServerConfig
from kreafinity.mcp.tools import McpTool, make_tool

logger = logging.getLogger(__name__)


class McpServer:
    """One configured server and its connection."""

    def __init__(self, config: ServerConfig, log_dir: Path | None = None) -> None:
        self.config = config
        self.log_dir = log_dir
        self.status = "stopped"  # stopped, starting, ready, error, needs_secret
        self.error = ""
        self.tools: list[dict[str, Any]] = []
        self.server_info: dict[str, Any] = {}
        self.connection: Connection | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        return self.config.name

    def missing_secrets(self) -> list[str]:
        names = list(self.config.secret_env) + (["token"] if self.config.token else [])
        return [n for n in names if not mcp_config.get_secret(self.name, n)]

    def start_soon(self) -> asyncio.Task[None]:
        """Start in the background (once at a time)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.start())
        return self._task

    async def start(self) -> None:
        async with self._lock:
            if self.status == "ready" and self._alive():
                return
            await self._close()
            missing = self.missing_secrets()
            if missing:  # the user still has to enter it: not an error
                self.status, self.error = "needs_secret", ", ".join(missing)
                return
            self.status, self.error = "starting", ""
            try:
                self.connection = self._connect()
                self.connection.on_notification = self._notified
                await asyncio.wait_for(self.connection.open(), START_TIMEOUT)
                result = await initialize(self.connection)
                self.server_info = result.get("serverInfo") or {}
                self.tools = await list_tools(self.connection)
                self.status = "ready"
                logger.info("MCP server %s ready: %d tools", self.name, len(self.tools))
            except (McpError, OSError, TimeoutError) as exc:
                self.status, self.error = "error", str(exc) or type(exc).__name__
                logger.warning("MCP server %s failed: %s", self.name, self.error)
                await self._close()

    def _connect(self) -> Connection:
        if self.config.url:
            token = mcp_config.get_secret(self.name, "token") if self.config.token else None
            return HttpConnection(self.config.url, token)
        env = dict(self.config.env)
        for variable in self.config.secret_env:
            env[variable] = mcp_config.get_secret(self.name, variable) or ""
        log_file = None
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_file = self.log_dir / f"{self.name}.log"
        return StdioConnection(self.config.command, list(self.config.args), env, log_file)

    def _alive(self) -> bool:
        connection = self.connection
        return not isinstance(connection, StdioConnection) or connection.running

    def _notified(self, method: str, params: Any) -> None:
        if method == "notifications/tools/list_changed" and self.connection is not None:
            asyncio.get_running_loop().create_task(self._refresh())

    async def _refresh(self) -> None:
        with contextlib.suppress(McpError):
            if self.connection is not None:
                self.tools = await list_tools(self.connection)

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Call a tool (starting the server again if it stopped)."""
        if self.status != "ready" or not self._alive():
            await self.start()
        if self.status == "needs_secret":
            raise McpError(f"secret not stored in the keyring: {self.error}")
        if self.status != "ready" or self.connection is None:
            raise McpError(self.error or "the server is not running")
        return await self.connection.request(
            "tools/call", {"name": tool, "arguments": arguments}, CALL_TIMEOUT
        )

    async def stop(self) -> None:
        async with self._lock:
            await self._close()
            self.status = "stopped"

    async def _close(self) -> None:
        connection, self.connection = self.connection, None
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()

    def snapshot(self) -> dict[str, Any]:
        config = self.config
        secrets = list(config.secret_env) + (["token"] if config.token else [])
        return {
            "name": self.name,
            "transport": config.transport,
            "target": config.describe(),
            "enabled": config.enabled,
            "status": self.status if config.enabled else "disabled",
            "error": self.error,
            "server": self.server_info.get("name", ""),
            "version": self.server_info.get("version", ""),
            "env": dict(config.env),
            "secrets": [
                {"name": n, "stored": bool(mcp_config.get_secret(self.name, n))} for n in secrets
            ],
            "tools": [
                {
                    "name": t["name"],
                    "title": t.get("title") or t["name"],
                    "description": str(t.get("description") or "")[:300],
                    "auto_approve": t["name"] in config.auto_approve,
                    "destructive": bool(
                        (t.get("annotations") or {}).get("destructiveHint") is True
                    ),
                }
                for t in self.tools
            ],
        }


class McpManager:
    """All the configured servers."""

    def __init__(self, log_dir: Path | None = None) -> None:
        self.log_dir = log_dir
        self.servers: dict[str, McpServer] = {}
        self._retired: list[McpServer] = []

    def configure(self, raw: Any) -> None:
        """Follow the ``mcp_servers`` setting: new, changed and removed servers."""
        try:
            configs = mcp_config.parse_servers(raw)
        except mcp_config.McpConfigError as exc:
            logger.warning("Invalid MCP configuration: %s", exc)
            return
        for name in list(self.servers):
            if name not in configs or configs[name] != self.servers[name].config:
                self._retired.append(self.servers.pop(name))
        for name, server_config in configs.items():
            if name not in self.servers:
                self.servers[name] = McpServer(server_config, self.log_dir)
        retired, self._retired = self._retired, []
        for server in retired:
            asyncio.get_running_loop().create_task(server.stop())

    def start_all(self) -> list[asyncio.Task[None]]:
        """Start the enabled servers that are not running (in the background)."""
        return [
            server.start_soon()
            for server in self.servers.values()
            if server.config.enabled and server.status in ("stopped",)
        ]

    async def wait_ready(self, timeout: float = 10.0) -> None:
        """Give starting servers a little time (the first request after a start)."""
        tasks = self.start_all() + [
            s._task for s in self.servers.values() if s._task is not None and not s._task.done()
        ]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    def tools(self) -> list[McpTool]:
        """The tools of the servers that are ready."""
        taken: set[str] = set()
        result = []
        for server in self.servers.values():
            if server.config.enabled and server.status == "ready":
                result += [make_tool(server, remote, taken) for remote in server.tools]
        return result

    def status(self) -> list[dict[str, Any]]:
        return [server.snapshot() for server in self.servers.values()]

    async def aclose(self) -> None:
        for server in [*self.servers.values(), *self._retired]:
            await server.stop()
