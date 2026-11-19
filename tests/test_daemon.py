"""The real daemon (uvicorn) started in a subprocess."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.sync.client import connect

SERVE = "from kreafinity.api.server import main; main()"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


class Daemon:
    def __init__(self, process: subprocess.Popen[bytes], port: int, data: Path) -> None:
        self.process = process
        self.port = port
        self.data = data
        self.url = f"http://127.0.0.1:{port}"

    @property
    def token(self) -> str:
        return (self.data / "kreafinity" / "token").read_text().strip()

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return httpx.get(self.url + path, timeout=5, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return httpx.post(self.url + path, timeout=5, **kwargs)


@pytest.fixture
def daemon(tmp_path: Path, home: Path) -> Iterator[Daemon]:
    port = free_port()
    config = tmp_path / "xdg-config" / "kreafinity"
    config.mkdir(parents=True)
    # Ollama "installed" on a port where nothing listens: requests fail cleanly.
    (config / "config.yaml").write_text(
        f"providers:\n  ollama:\n    base_url: http://127.0.0.1:{free_port()}\n"
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from kreafinity.api.server import main; main()",
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    daemon = Daemon(process, port, tmp_path / "xdg-data")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if daemon.get("/api/health").status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.1)
    else:
        process.kill()
        pytest.fail("the daemon did not start")
    yield daemon
    if process.poll() is None:
        process.kill()
        process.wait()


def test_daemon_end_to_end(daemon: Daemon) -> None:
    # Files: private token and daemon info.
    info_file = daemon.data / "kreafinity" / "daemon.json"
    info = json.loads(info_file.read_text())
    assert info == {"url": daemon.url, "port": daemon.port, "pid": daemon.process.pid}
    for path in (info_file, daemon.data / "kreafinity" / "token"):
        assert path.stat().st_mode & 0o777 == 0o600

    # Authentication and host checks.
    assert daemon.get("/api/conversations").status_code == 401
    auth = {"Authorization": f"Bearer {daemon.token}"}
    assert daemon.get("/api/conversations", headers=auth).json() == []
    assert daemon.get("/api/health", headers={"Host": "evil.example"}).status_code == 400

    # Live events over a real WebSocket: the local model is not running,
    # so the request fails cleanly with a clear error code.
    conversation = daemon.post("/api/conversations", headers=auth).json()
    with connect(f"ws://127.0.0.1:{daemon.port}/api/events") as websocket:
        websocket.send(json.dumps({"type": "auth", "token": daemon.token}))
        assert json.loads(websocket.recv(timeout=5))["type"] == "ready"
        response = daemon.post(
            f"/api/conversations/{conversation['id']}/messages",
            headers=auth,
            json={"content": "Bonjour"},
        )
        assert response.status_code == 202
        events = []
        while not events or events[-1]["type"] not in ("run_finished", "run_failed"):
            events.append(json.loads(websocket.recv(timeout=10)))
    assert events[0]["type"] == "run_started"
    assert events[-1] == {**events[-1], "type": "run_failed", "code": "provider_unavailable"}

    # The user message is kept in the history.
    saved = daemon.get(f"/api/conversations/{conversation['id']}", headers=auth).json()
    assert [m["content"] for m in saved["messages"]] == ["Bonjour"]

    # Clean stop on SIGTERM (what systemd sends).
    daemon.process.send_signal(signal.SIGTERM)
    assert daemon.process.wait(timeout=10) == 0
    assert not info_file.exists()


def test_daemon_listens_on_loopback_only(daemon: Daemon) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            address = probe.getsockname()[0]
    except OSError:
        pytest.skip("no network interface besides loopback")
    if address.startswith("127."):
        pytest.skip("no network interface besides loopback")
    with pytest.raises(OSError), socket.create_connection((address, daemon.port), timeout=2):
        pass


def test_second_daemon_leaves_the_first_alone(daemon: Daemon, tmp_path: Path) -> None:
    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
    }
    second = subprocess.run(
        [sys.executable, "-c", SERVE, "--port", str(daemon.port)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert second.returncode == 0  # nothing to do: Kreafinity already runs there
    assert "already running" in second.stderr
    info = json.loads((daemon.data / "kreafinity" / "daemon.json").read_text())
    assert info["pid"] == daemon.process.pid
    assert daemon.get("/api/health").status_code == 200


def test_ui_command_starts_the_daemon(tmp_path: Path, home: Path) -> None:
    """`kreafinity ui` starts a daemon in the background when none is running."""
    port = free_port()
    config = tmp_path / "xdg-config" / "kreafinity"
    config.mkdir(parents=True)
    (config / "config.yaml").write_text(f"port: {port}\n")
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
    }
    result = subprocess.run(
        [sys.executable, "-c", "from kreafinity.cli import app; app()", "ui", "--sans-navigateur"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    info_file = tmp_path / "xdg-data" / "kreafinity" / "daemon.json"
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"http://127.0.0.1:{port}" in result.stdout
        info = json.loads(info_file.read_text())
        assert httpx.get(f"{info['url']}/api/health", timeout=5).json()["name"] == "kreafinity"
        page = httpx.get(info["url"], timeout=5)
        assert page.status_code == 200 and "<title>Kreafinity</title>" in page.text
    finally:
        if info_file.exists():
            os.kill(json.loads(info_file.read_text())["pid"], signal.SIGTERM)
            deadline = time.monotonic() + 10
            while info_file.exists() and time.monotonic() < deadline:
                time.sleep(0.1)


def test_port_taken_by_another_program(tmp_path: Path) -> None:
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        port = other.getsockname()[1]
        env = {
            **os.environ,
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        }
        result = subprocess.run(
            [sys.executable, "-c", SERVE, "--port", str(port)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert result.returncode == 1
    assert "already in use" in result.stderr
