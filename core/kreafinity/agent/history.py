"""Conversation history, stored locally in SQLite.

The database lives in ``~/.local/share/kreafinity/history.db``, readable only
by the user. Messages are kept in the provider-neutral format, including the
provider data needed to resume a conversation with any provider.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kreafinity.paths import data_dir
from kreafinity.providers.base import Message, ToolCall

SCHEMA_VERSION = 1
TITLE_LENGTH = 80

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    provider TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_calls TEXT,
    tool_call_id TEXT,
    name TEXT,
    is_error INTEGER NOT NULL DEFAULT 0,
    provider_data TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (conversation_id, position)
);
CREATE INDEX IF NOT EXISTS conversations_by_update ON conversations(updated_at DESC);
"""


class ConversationNotFoundError(LookupError):
    """No conversation has this id."""


def default_history_path() -> Path:
    """Location of the history database."""
    return data_dir() / "history.db"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class ConversationInfo:
    """A conversation, without its messages.

    Attributes:
        id: Unique id.
        title: Title (by default, the beginning of the first request).
        provider: Last ``provider/model`` chosen for it, if any.
        created_at: Creation time (ISO 8601, UTC).
        updated_at: Time of the last message (ISO 8601, UTC).
        message_count: Number of stored messages.
    """

    id: str
    title: str
    provider: str | None
    created_at: str
    updated_at: str
    message_count: int

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return {
            "id": self.id,
            "title": self.title,
            "provider": self.provider,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
        }


class ConversationStore:
    """Conversations and their messages in a SQLite database.

    Each operation opens its own connection, so the store can be used from
    any thread, and by several processes (daemon and CLI) at the same time.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_history_path()
        self._initialize()

    def _initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.path.exists():
            # Create the file first, so it is private from the start.
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        with self._db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"{self.path} was created by a newer version of Kreafinity "
                    f"(schema {version}, this version knows {SCHEMA_VERSION})"
                )
            db.executescript(_SCHEMA)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        with self._db() as db:
            db.execute("PRAGMA journal_mode = WAL")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:  # commit, or roll back on error
                yield connection
        finally:
            connection.close()

    # --- Conversations -----------------------------------------------------------

    def create(self, title: str = "", provider: str | None = None) -> ConversationInfo:
        """Create an empty conversation."""
        now = _now()
        conversation_id = uuid.uuid4().hex
        with self._db() as db:
            db.execute(
                "INSERT INTO conversations (id, title, provider, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (conversation_id, title.strip(), provider, now, now),
            )
        return self.get(conversation_id)

    def get(self, conversation_id: str) -> ConversationInfo:
        """Information about one conversation.

        Raises:
            ConversationNotFoundError: Unknown id.
        """
        with self._db() as db:
            row = db.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) "
                "AS message_count FROM conversations c WHERE c.id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            raise ConversationNotFoundError(conversation_id)
        return _info(row)

    def list_conversations(self, *, limit: int = 50, offset: int = 0) -> list[ConversationInfo]:
        """Conversations, most recently updated first."""
        with self._db() as db:
            rows = db.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) "
                "AS message_count FROM conversations c "
                "ORDER BY c.updated_at DESC, c.rowid DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [_info(row) for row in rows]

    def rename(self, conversation_id: str, title: str) -> ConversationInfo:
        """Change the title of a conversation."""
        with self._db() as db:
            cursor = db.execute(
                "UPDATE conversations SET title = ? WHERE id = ?", (title.strip(), conversation_id)
            )
        if cursor.rowcount == 0:
            raise ConversationNotFoundError(conversation_id)
        return self.get(conversation_id)

    def set_provider(self, conversation_id: str, provider: str | None) -> None:
        """Remember the ``provider/model`` chosen for a conversation."""
        with self._db() as db:
            cursor = db.execute(
                "UPDATE conversations SET provider = ? WHERE id = ?", (provider, conversation_id)
            )
        if cursor.rowcount == 0:
            raise ConversationNotFoundError(conversation_id)

    def delete(self, conversation_id: str) -> None:
        """Delete a conversation and its messages."""
        with self._db() as db:
            cursor = db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        if cursor.rowcount == 0:
            raise ConversationNotFoundError(conversation_id)

    # --- Messages ------------------------------------------------------------------

    def messages(self, conversation_id: str) -> list[Message]:
        """All messages of a conversation, in order."""
        self.get(conversation_id)
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY position",
                (conversation_id,),
            ).fetchall()
        return [_message(row) for row in rows]

    def append(self, conversation_id: str, messages: Sequence[Message]) -> None:
        """Add messages at the end of a conversation.

        The first user message also becomes the title of an untitled conversation.
        """
        if not messages:
            return
        now = _now()
        with self._db() as db:
            conversation = db.execute(
                "SELECT title FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if conversation is None:
                raise ConversationNotFoundError(conversation_id)
            start = db.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            db.executemany(
                "INSERT INTO messages (conversation_id, position, role, content, tool_calls, "
                "tool_call_id, name, is_error, provider_data, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (conversation_id, start + offset, *_columns(message), now)
                    for offset, message in enumerate(messages)
                ],
            )
            title = conversation["title"]
            if not title:
                first_request = next((m.content for m in messages if m.role == "user"), "")
                title = _title_from(first_request)
            db.execute(
                "UPDATE conversations SET updated_at = ?, title = ? WHERE id = ?",
                (now, title, conversation_id),
            )

    def replace_messages(self, conversation_id: str, messages: Sequence[Message]) -> None:
        """Replace all the messages of a conversation."""
        with self._db() as db:
            if db.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone():
                db.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            else:
                raise ConversationNotFoundError(conversation_id)
        self.append(conversation_id, messages)


def _title_from(text: str) -> str:
    first_line = " ".join(text.split())
    if len(first_line) <= TITLE_LENGTH:
        return first_line
    return first_line[: TITLE_LENGTH - 1].rstrip() + "…"


def _info(row: sqlite3.Row) -> ConversationInfo:
    return ConversationInfo(
        id=row["id"],
        title=row["title"],
        provider=row["provider"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        message_count=row["message_count"],
    )


def _columns(message: Message) -> tuple[Any, ...]:
    tool_calls = (
        json.dumps(
            [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in message.tool_calls],
            ensure_ascii=False,
        )
        if message.tool_calls
        else None
    )
    provider_data = (
        json.dumps(message.provider_data, ensure_ascii=False) if message.provider_data else None
    )
    return (
        message.role,
        message.content,
        tool_calls,
        message.tool_call_id,
        message.name,
        int(message.is_error),
        provider_data,
    )


def _message(row: sqlite3.Row) -> Message:
    tool_calls = tuple(
        ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"])
        for c in json.loads(row["tool_calls"] or "[]")
    )
    return Message(
        role=row["role"],
        content=row["content"],
        tool_calls=tool_calls,
        tool_call_id=row["tool_call_id"],
        name=row["name"],
        is_error=bool(row["is_error"]),
        provider_data=json.loads(row["provider_data"] or "{}"),
    )
