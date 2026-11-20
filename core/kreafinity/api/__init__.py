"""Local daemon API (FastAPI): REST + WebSocket, on 127.0.0.1 only, token protected.

`create_app` is imported on first use: light clients of the daemon (desktop
search, CLI) do not load the web framework.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kreafinity.api.auth import load_or_create_token, token_path

if TYPE_CHECKING:
    from kreafinity.api.app import create_app

__all__ = ["create_app", "load_or_create_token", "token_path"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from kreafinity.api.app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
