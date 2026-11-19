import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from kreafinity.agent import (
    ConversationNotFoundError,
    ConversationStore,
    complete_unanswered_calls,
    default_history_path,
)
from kreafinity.providers import Message, ToolCall


def test_database_is_private(tmp_path: Path) -> None:
    store = ConversationStore()
    assert store.path == default_history_path()
    assert store.path == tmp_path / "xdg-data" / "kreafinity" / "history.db"
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700


def test_round_trip_keeps_everything() -> None:
    store = ConversationStore()
    conversation = store.create()
    call = ToolCall(id="toolu_1", name="list_directory", arguments={"path": "~/Téléchargements"})
    messages = [
        Message.user("Que contient Téléchargements ?"),
        Message(
            role="assistant",
            content="Je regarde.",
            tool_calls=(call,),
            provider_data={"anthropic": {"content": [{"type": "thinking", "signature": "s"}]}},
        ),
        Message.tool_result("toolu_1", "list_directory", '{"entries": []}', is_error=True),
        Message.assistant("Le dossier est vide."),
    ]
    store.append(conversation.id, messages[:2])
    store.append(conversation.id, messages[2:])

    assert store.messages(conversation.id) == messages
    info = store.get(conversation.id)
    assert info.message_count == 4
    assert info.title == "Que contient Téléchargements ?"


def test_title_is_shortened_and_kept() -> None:
    store = ConversationStore()
    conversation = store.create()
    store.append(conversation.id, [Message.user("Bonjour\n  " + "très " * 40 + "long")])
    title = store.get(conversation.id).title
    assert len(title) == 80 and title.endswith("…") and "\n" not in title
    store.append(conversation.id, [Message.user("Autre chose")])
    assert store.get(conversation.id).title == title

    named = store.create(title="Mes photos")
    store.append(named.id, [Message.user("Range mes photos")])
    assert store.get(named.id).title == "Mes photos"


def test_list_rename_provider_delete() -> None:
    store = ConversationStore()
    first = store.create(title="Premier")
    time.sleep(0.002)
    second = store.create(title="Second")
    time.sleep(0.002)
    store.append(first.id, [Message.user("Encore moi")])  # becomes the most recent

    assert [c.id for c in store.list_conversations()] == [first.id, second.id]
    assert [c.id for c in store.list_conversations(limit=1, offset=1)] == [second.id]

    assert store.rename(second.id, "  Renommé ").title == "Renommé"
    store.set_provider(second.id, "mistral/mistral-small-latest")
    assert store.get(second.id).provider == "mistral/mistral-small-latest"

    store.delete(first.id)
    assert [c.id for c in store.list_conversations()] == [second.id]
    with closing(sqlite3.connect(store.path)) as db:  # messages deleted with their conversation
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_unknown_conversation() -> None:
    store = ConversationStore()
    for action in (
        lambda: store.get("nope"),
        lambda: store.messages("nope"),
        lambda: store.append("nope", [Message.user("x")]),
        lambda: store.rename("nope", "x"),
        lambda: store.delete("nope"),
        lambda: store.set_provider("nope", None),
        lambda: store.replace_messages("nope", []),
    ):
        with pytest.raises(ConversationNotFoundError):
            action()


def test_replace_messages() -> None:
    store = ConversationStore()
    conversation = store.create()
    store.append(conversation.id, [Message.user("a"), Message.assistant("b")])
    store.replace_messages(conversation.id, [Message.user("c")])
    assert store.messages(conversation.id) == [Message.user("c")]


def test_shared_between_two_stores() -> None:
    daemon_side, cli_side = ConversationStore(), ConversationStore()
    conversation = daemon_side.create()
    cli_side.append(conversation.id, [Message.user("depuis la CLI")])
    assert daemon_side.messages(conversation.id) == [Message.user("depuis la CLI")]


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="newer version"):
        ConversationStore(path)


def test_complete_unanswered_calls() -> None:
    a = ToolCall(id="a", name="system_info", arguments={})
    b = ToolCall(id="b", name="list_directory", arguments={})
    interrupted = [
        Message.user("Infos ?"),
        Message.assistant("", [a, b]),
        Message.tool_result("a", "system_info", "ok"),
        # b never got its result: the request was interrupted
        Message.user("Tu es là ?"),
    ]
    repaired = complete_unanswered_calls(interrupted)
    assert [m.role for m in repaired] == ["user", "assistant", "tool", "tool", "user"]
    assert repaired[3].tool_call_id == "b" and repaired[3].is_error
    assert complete_unanswered_calls(repaired) == repaired

    ending_with_calls = interrupted[:2]
    assert [m.tool_call_id for m in complete_unanswered_calls(ending_with_calls)[2:]] == [
        "a",
        "b",
    ]
