"""A fake Ollama server answering from a script, for end-to-end tests."""

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: Given the conversation sent to the model, returns the answer pieces:
#: {"content": "..."} and/or {"tool_calls": [{"function": {...}}]}.
Script = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]


class FakeOllama:
    def __init__(self, script: Script) -> None:
        self.script = script
        self.requests: list[dict[str, Any]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # /api/tags
                body = json.dumps({"models": [{"name": "llama3.1:latest"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # /api/chat
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(request)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                model = request["model"]
                for piece in fake.script(request["messages"]):
                    message = {"role": "assistant", "content": "", **piece}
                    self._send({"model": model, "message": message, "done": False})
                self._send(
                    {
                        "model": model,
                        "message": {"content": ""},
                        "done": True,
                        "done_reason": "stop",
                    }
                )

            def _send(self, chunk: dict[str, Any]) -> None:
                self.wfile.write((json.dumps(chunk) + "\n").encode())
                self.wfile.flush()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeOllama":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def tool_call(name: str, **arguments: Any) -> dict[str, Any]:
    return {"tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


def words(text: str) -> list[dict[str, Any]]:
    """Stream `text` word by word, like a real model."""
    pieces = text.split(" ")
    return [
        {"content": piece + (" " if i < len(pieces) - 1 else "")} for i, piece in enumerate(pieces)
    ]
