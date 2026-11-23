"""A small Model Context Protocol client: JSON-RPC 2.0 over stdio or HTTP.

Only what the assistant needs: the handshake (``initialize``), listing the
tools (``tools/list``) and calling them (``tools/call``). Requests coming from
the server are answered: ``ping``, and an error for anything else (sampling,
elicitation… are not offered).

- **stdio**: Kreafinity starts the server program and exchanges one JSON
  message per line on its standard input and output.
- **Streamable HTTP**: each message is POSTed to the server's address; the
  answer is JSON, or a stream of server-sent events.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from kreafinity import __version__

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
START_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
MAX_LINE = 16 * 1024 * 1024
#: Environment variables passed on to a server program (nothing else of Kreafinity's).
PASSED_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "LC_ALL", "TZ", "TMPDIR",
    "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
    "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_CURRENT_DESKTOP",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
)  # fmt: skip


class McpError(Exception):
    """The server cannot be reached, refused a request or answered nonsense."""


class Connection(ABC):
    """A JSON-RPC connection to one MCP server."""

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.on_notification: Callable[[str, Any], None] | None = None

    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def request(self, method: str, params: dict[str, Any], timeout: float) -> Any: ...

    @abstractmethod
    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None: ...

    def _answer_server(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """The reply to a request or notification sent by the server."""
        method = message.get("method", "")
        if "id" not in message:
            if self.on_notification:
                self.on_notification(method, message.get("params"))
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": -32601, "message": f"{method} is not offered by Kreafinity"},
        }


def _result(message: dict[str, Any], method: str) -> Any:
    if "error" in message:
        error = message["error"] or {}
        raise McpError(f"{method}: {error.get('message', 'error')} ({error.get('code')})")
    return message.get("result")


class StdioConnection(Connection):
    """A server program started by Kreafinity, talking on its standard input/output."""

    def __init__(
        self, command: str, args: list[str], env: dict[str, str], log_file: Path | None = None
    ) -> None:
        super().__init__()
        self.command, self.args, self.env, self.log_file = command, args, env, log_file
        self.process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}

    async def open(self) -> None:
        env = {key: os.environ[key] for key in PASSED_ENV if key in os.environ}
        env.update(self.env)
        log = open(self.log_file, "ab") if self.log_file else None  # noqa: SIM115
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=log or asyncio.subprocess.DEVNULL,
                env=env,
                limit=MAX_LINE,
                start_new_session=True,
            )
        except OSError as exc:
            raise McpError(f"cannot start {self.command}: {exc}") from exc
        finally:
            if log:
                log.close()
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    logger.debug("MCP %s: not JSON: %r", self.command, line[:200])
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" in message:
                    reply = self._answer_server(message)
                    if reply is not None:
                        await self._write(reply)
                    continue
                future = self._pending.pop(message.get("id", -1), None)
                if future is not None and not future.done():
                    future.set_result(message)
        except (ValueError, ConnectionError) as exc:
            logger.warning("MCP %s: connection lost: %s", self.command, exc)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(McpError("the server stopped"))
            self._pending.clear()

    async def _write(self, message: dict[str, Any]) -> None:
        if (
            self.process is None
            or self.process.stdin is None
            or self.process.returncode is not None
        ):
            raise McpError("the server is not running")
        try:
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            await self.process.stdin.drain()
        except (ConnectionError, BrokenPipeError) as exc:
            raise McpError(f"the server stopped: {exc}") from exc

    async def request(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        request_id = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            message = await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            with contextlib.suppress(McpError):
                await self.notify("notifications/cancelled", {"requestId": request_id})
            raise McpError(f"{method}: no answer after {timeout:.0f} s") from exc
        finally:
            self._pending.pop(request_id, None)
        return _result(message, method)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def close(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.stdin is not None:
            with contextlib.suppress(Exception):
                process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader


class HttpConnection(Connection):
    """A server reached at an address (MCP « Streamable HTTP » transport)."""

    def __init__(self, url: str, token: str | None = None) -> None:
        super().__init__()
        self.url = url
        self.session_id = ""
        headers = {"Accept": "application/json, text/event-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.AsyncClient(
            headers=headers, timeout=httpx.Timeout(15.0, read=CALL_TIMEOUT)
        )

    async def open(self) -> None:
        return None

    async def _post(self, message: dict[str, Any], timeout: float) -> dict[str, Any] | None:
        headers = {"MCP-Protocol-Version": PROTOCOL_VERSION}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            async with self.client.stream(
                "POST", self.url, json=message, headers=headers, timeout=timeout
            ) as response:
                if response.headers.get("mcp-session-id"):
                    self.session_id = response.headers["mcp-session-id"]
                if response.status_code == 202 or "id" not in message:
                    return None
                if response.status_code >= 400:
                    body = (await response.aread()).decode(errors="replace")[:300]
                    raise McpError(f"HTTP {response.status_code}: {body}")
                kind = response.headers.get("content-type", "")
                if "text/event-stream" in kind:
                    return await self._from_events(response, message["id"])
                return json.loads(await response.aread())  # type: ignore[no-any-return]
        except httpx.HTTPError as exc:
            raise McpError(f"cannot reach {self.url}: {exc}") from exc
        except ValueError as exc:
            raise McpError(f"unexpected answer from {self.url}") from exc

    async def _from_events(self, response: httpx.Response, wanted: int) -> dict[str, Any]:
        data: list[str] = []
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                message = json.loads("\n".join(data))
                data = []
                if not isinstance(message, dict):
                    continue
                if "method" in message:
                    reply = self._answer_server(message)
                    if reply is not None:
                        await self._post(reply, 15)
                elif message.get("id") == wanted:
                    return message
        raise McpError("the server closed the stream without answering")

    async def request(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        message = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        try:
            answer = await asyncio.wait_for(self._post(message, timeout), timeout + 5)
        except TimeoutError as exc:
            raise McpError(f"{method}: no answer after {timeout:.0f} s") from exc
        if answer is None:
            raise McpError(f"{method}: empty answer")
        return _result(answer, method)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._post(message, 15)

    async def close(self) -> None:
        if self.session_id:
            with contextlib.suppress(Exception):
                await self.client.delete(self.url, headers={"Mcp-Session-Id": self.session_id})
        await self.client.aclose()


async def initialize(connection: Connection, timeout: float = START_TIMEOUT) -> dict[str, Any]:
    """The MCP handshake; returns the server's answer (serverInfo, capabilities…)."""
    result = await connection.request(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "kreafinity", "title": "Kreafinity", "version": __version__},
        },
        timeout,
    )
    if not isinstance(result, dict):
        raise McpError("initialize: unexpected answer")
    await connection.notify("notifications/initialized")
    return result


async def list_tools(
    connection: Connection, timeout: float = START_TIMEOUT
) -> list[dict[str, Any]]:
    """All the server's tools (following the pages)."""
    tools: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(50):
        params = {"cursor": cursor} if cursor else {}
        result = await connection.request("tools/list", params, timeout)
        if not isinstance(result, dict):
            raise McpError("tools/list: unexpected answer")
        tools += [t for t in result.get("tools", []) if isinstance(t, dict) and t.get("name")]
        cursor = result.get("nextCursor")
        if not cursor:
            break
    return tools
