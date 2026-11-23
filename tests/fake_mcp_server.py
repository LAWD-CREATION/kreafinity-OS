"""A fake MCP server over stdio, for the tests (one JSON-RPC message per line)."""

import json
import os
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "title": "Répéter",
        "description": "Repeat the text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {"name": "picture", "description": "A tiny picture.", "inputSchema": {"type": "object"}},
    {"name": "fail", "description": "Always fails.", "inputSchema": {"type": "object"}},
    {
        "name": "env",
        "description": "Shows two environment variables.",
        "inputSchema": {"type": "object"},
    },
    {
        "name": "delete_everything",
        "description": "Deletes things.",
        "inputSchema": {"type": "object"},
        "annotations": {"destructiveHint": True},
    },
    {
        "name": "pinger",
        "description": "Pings the client before answering.",
        "inputSchema": {"type": "object"},
    },
    {
        "name": "bad schema",
        "description": "Has an invalid schema.",
        "inputSchema": {"type": "object", "properties": {"x": {"type": "nonsense"}}},
    },
]
PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9a"
    "wAAAABJRU5ErkJggg=="
)


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def result(request_id, value):
    send({"jsonrpc": "2.0", "id": request_id, "result": value})


def call(name, arguments):
    if name == "echo":
        return {"content": [{"type": "text", "text": f"écho : {arguments.get('text', '')}"}]}
    if name == "picture":
        return {
            "content": [
                {"type": "text", "text": "voici"},
                {"type": "image", "mimeType": "image/png", "data": PNG},
            ]
        }
    if name == "fail":
        return {"content": [{"type": "text", "text": "it broke"}], "isError": True}
    if name == "env":
        return {
            "content": [],
            "structuredContent": {
                "secret": os.environ.get("FAKE_SECRET"),
                "leak": os.environ.get("ANTHROPIC_API_KEY"),
                "plain": os.environ.get("FAKE_PLAIN"),
            },
        }
    if name == "pinger":
        send({"jsonrpc": "2.0", "id": "srv-1", "method": "ping"})
        send({"jsonrpc": "2.0", "id": "srv-2", "method": "sampling/createMessage", "params": {}})
        answers = {}
        while len(answers) < 2:
            message = json.loads(sys.stdin.readline())
            answers[message["id"]] = message
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "ping": answers["srv-1"].get("result"),
                            "sampling": answers["srv-2"].get("error", {}).get("code"),
                        }
                    ),
                }
            ]
        }
    if name == "delete_everything":
        return {"content": [{"type": "text", "text": "gone"}]}
    return None


for line in sys.stdin:
    message = json.loads(line)
    method, request_id = message.get("method"), message.get("id")
    if request_id is None:
        if method == "notifications/initialized":
            print("initialized", file=sys.stderr, flush=True)
        continue
    if method == "initialize":
        result(
            request_id,
            {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "1.0"},
            },
        )
    elif method == "tools/list":
        page = 1 if message.get("params", {}).get("cursor") == "page2" else 0
        chunk = TOOLS[:4] if page == 0 else TOOLS[4:]
        result(request_id, {"tools": chunk, **({"nextCursor": "page2"} if page == 0 else {})})
    elif method == "tools/call":
        params = message["params"]
        if params["name"] == "crash":
            sys.exit(3)
        if params["name"] == "slow":
            time.sleep(5)
        value = call(params["name"], params.get("arguments", {}))
        if value is None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": f"Unknown tool {params['name']}"},
                }
            )
        else:
            result(request_id, value)
    else:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "no"}})
