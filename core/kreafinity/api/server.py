"""Running the daemon (``kreafinity-core``, or ``kreafinity daemon``)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import urllib.request
from pathlib import Path
from types import FrameType
from typing import Any

import uvicorn

from kreafinity.agent import ConversationStore
from kreafinity.api.app import create_app
from kreafinity.api.auth import load_or_create_token
from kreafinity.api.web import web_root
from kreafinity.config import load_settings
from kreafinity.paths import data_dir
from kreafinity.service import AssistantService

#: The daemon only ever listens on the loopback interface.
HOST = "127.0.0.1"

logger = logging.getLogger(__name__)


class DaemonError(Exception):
    """The daemon cannot start (e.g. its port is already in use)."""


def daemon_info_path() -> Path:
    """File telling clients where the running daemon listens."""
    return data_dir() / "daemon.json"


def read_daemon_info() -> dict[str, Any] | None:
    """Where the running daemon listens (``url``, ``port``, ``pid``), if it runs."""
    try:
        info = json.loads(daemon_info_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def serve(port: int | None = None) -> None:
    """Run the daemon in the foreground until it is stopped (Ctrl-C or SIGTERM).

    Raises:
        DaemonError: The port is already in use.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore"):  # one line per LLM request otherwise
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = load_settings()
    port = port or settings.port
    # Reserve the port first: if another daemon runs, leave its files alone.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((HOST, port))
    except OSError as exc:
        listener.close()
        raise DaemonError(
            f"Port {port} is already in use (is Kreafinity already running?): {exc.strerror}"
        ) from exc
    token = load_or_create_token()
    store = ConversationStore()
    app = create_app(lambda: AssistantService(store), token, port=port, web_dir=web_root())
    config = uvicorn.Config(
        app,
        host=HOST,
        port=port,
        log_level="info",
        access_log=False,
        server_header=False,
        proxy_headers=False,
    )
    server = uvicorn.Server(config)

    info_path = daemon_info_path()
    url = f"http://{HOST}:{port}"
    info_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(info_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"url": url, "port": port, "pid": os.getpid()}, handle)
    logger.info("Kreafinity listens on %s", url)
    # Uvicorn shuts down gracefully on SIGTERM/SIGINT, then raises the signal
    # again with the previous handlers: turn it into a normal exit, so the
    # cleanup below runs and systemd sees a clean stop.
    previous_handler = signal.signal(signal.SIGTERM, _exit_normally)
    try:
        server.run(sockets=[listener])
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        listener.close()
        info = read_daemon_info()
        if info and info.get("pid") == os.getpid():
            info_path.unlink(missing_ok=True)


def _exit_normally(signum: int, frame: FrameType | None) -> None:
    raise SystemExit(0)


def main() -> None:
    """Entry point of ``kreafinity-core``."""
    parser = argparse.ArgumentParser(
        prog="kreafinity-core", description="Démon de l'assistant Kreafinity (127.0.0.1)."
    )
    parser.add_argument("--port", type=int, help="port d'écoute (par défaut : réglage « port »)")
    port = parser.parse_args().port
    try:
        serve(port)
    except DaemonError as exc:
        url = f"http://{HOST}:{port or load_settings().port}"
        if _is_kreafinity(url):
            # Already running (e.g. started on demand before systemd): nothing to do.
            parser.exit(0, f"kreafinity-core: Kreafinity is already running on {url}\n")
        parser.exit(1, f"kreafinity-core: {exc}\n")


def _is_kreafinity(url: str) -> bool:
    """Whether a Kreafinity daemon answers at `url`."""
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=2) as response:
            return bool(json.load(response).get("name") == "kreafinity")
    except (OSError, ValueError):
        return False


if __name__ == "__main__":
    main()
