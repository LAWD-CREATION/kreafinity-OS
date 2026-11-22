"""Talking to the daemon from another program of the user (desktop search, scripts).

Requests carry the local token, read from the user's private token file.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

import httpx

from kreafinity.api.auth import token_path
from kreafinity.api.daemon_info import HOST, read_daemon_info
from kreafinity.paths import data_dir
from kreafinity.system import integration


class DaemonUnavailableError(Exception):
    """The daemon is not running and could not be started."""


def daemon_url(port: int) -> str:
    """Where the daemon listens (from its info file, else the configured port)."""
    info = read_daemon_info()
    url = info.get("url") if info else None
    return url if isinstance(url, str) else f"http://{HOST}:{port}"


def is_running(url: str) -> bool:
    """Whether Kreafinity answers at `url`."""
    try:
        health = httpx.get(f"{url}/api/health", timeout=2).json()
    except (httpx.HTTPError, ValueError):
        return False
    return isinstance(health, dict) and health.get("name") == "kreafinity"


def launch_daemon() -> None:
    """Start the daemon in the background: through its systemd user service when
    it is installed (so systemd keeps managing it), otherwise as a detached process."""
    if integration.service_installed() and integration.systemd_available():
        integration.import_session_environment()
        if integration.systemctl("start", integration.SERVICE_NAME):
            return
    log_path = data_dir() / "daemon.log"
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "kreafinity.api.server"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )


def ensure_daemon(port: int, *, timeout: float = 20) -> str:
    """The daemon's address, starting it first if needed.

    Raises:
        DaemonUnavailableError: It did not answer within `timeout` seconds.
    """
    url = daemon_url(port)
    if is_running(url):
        return url
    launch_daemon()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = daemon_url(port)
        if is_running(url):
            return url
        time.sleep(0.3)
    raise DaemonUnavailableError(f"Kreafinity did not start (see {data_dir() / 'daemon.log'})")


class DaemonClient:
    """REST calls to the daemon, authenticated with the local token."""

    def __init__(self, url: str, token: str | None = None) -> None:
        self.url = url.rstrip("/")
        token = token or token_path().read_text(encoding="ascii").strip()
        self._headers = {"Authorization": f"Bearer {token}"}

    async def get(self, path: str) -> Any:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{self.url}{path}", headers=self._headers)
        response.raise_for_status()
        return response.json()

    async def _post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(f"{self.url}{path}", json=body, headers=self._headers)
        response.raise_for_status()
        return response.json()

    async def ask(self, question: str) -> str:
        """Start a new conversation with `question`; return its id (the answer streams on)."""
        conversation = await self._post("/api/conversations")
        await self._post(f"/api/conversations/{conversation['id']}/messages", {"content": question})
        return str(conversation["id"])

    async def show(self, conversation_id: str) -> int:
        """Ask the assistant panels to show a conversation; how many listened."""
        result = await self._post("/api/ui/show", {"conversation_id": conversation_id})
        return int(result.get("delivered", 0))
