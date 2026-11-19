"""Local token protecting the daemon.

The daemon only listens on 127.0.0.1, but any program or website running on
the machine can reach that address. Every request must therefore carry a
secret token, stored in a file only the user can read
(``~/.local/share/kreafinity/token``, a folder the tools cannot access).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from kreafinity.paths import data_dir

TOKEN_BYTES = 32


def token_path() -> Path:
    """Location of the token file."""
    return data_dir() / "token"


def load_or_create_token(path: Path | None = None) -> str:
    """Return the daemon token, creating it on first use.

    The file is (re)set to mode 0600 every time, so it stays private.
    """
    path = path or token_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        token = path.read_text(encoding="ascii").strip()
    except (FileNotFoundError, UnicodeDecodeError):
        token = ""
    if len(token) < TOKEN_BYTES:
        token = secrets.token_urlsafe(TOKEN_BYTES)
        temporary = path.with_name(path.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(token + "\n")
        os.replace(temporary, path)
    os.chmod(path, 0o600)
    return token


def token_matches(candidate: str | None, token: str) -> bool:
    """Compare a received token with the real one, in constant time."""
    if not candidate:
        return False
    return secrets.compare_digest(candidate.encode(), token.encode())
