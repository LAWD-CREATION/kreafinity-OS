"""« Toujours autoriser »: actions the user allowed for good."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fakes import ScriptedProvider, call, reply
from kreafinity import permissions
from kreafinity.config import ConfigError, load_settings
from kreafinity.service import ServiceError
from test_service import Harness


def test_rules() -> None:
    assert permissions.options("create_folder", {"path": "~/A"}) == [
        {"rule": "create_folder", "scope": "tool"}
    ]
    assert permissions.options("run_command", {"command": " ls -l ~ "}) == [
        {"rule": "run_command ls -l ~", "scope": "command"},
        {"rule": "run_command", "scope": "tool"},
    ]
    # A command over several lines can only be allowed with the whole tool.
    assert permissions.options("run_command", {"command": "ls\nrm -r ~"}) == [
        {"rule": "run_command", "scope": "tool"}
    ]
    rules = ["create_folder", "run_command ls -l ~"]
    assert permissions.allows(rules, "create_folder", {"path": "~/B"})
    assert permissions.allows(rules, "run_command", {"command": "ls -l ~"})
    assert not permissions.allows(rules, "run_command", {"command": "ls -l ~; rm -r ~"})
    assert not permissions.allows(rules, "move_to_trash", {"paths": ["~/A"]})
    assert permissions.allows(["run_command"], "run_command", {"command": "anything"})
    assert permissions.describe("run_command ls -l ~") == {
        "rule": "run_command ls -l ~",
        "tool": "run_command",
        "command": "ls -l ~",
    }
    assert permissions.is_valid("mcp_github_get-issue")
    for bad in ("", "create_folder ~/A", "run_command a\nb", "rm -rf /", "a" * 3000):
        assert not permissions.is_valid(bad)


def test_setting_is_validated(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("always_allow: [open_application, run_command echo bonjour]\n")
    assert load_settings(config).always_allow == ["open_application", "run_command echo bonjour"]
    config.write_text("always_allow: [create_folder ~/A]\n")
    with pytest.raises(ConfigError, match="always_allow"):
        load_settings(config)
    config.write_text("always_allow: run_command\n")
    with pytest.raises(ConfigError, match="always_allow"):
        load_settings(config)


@pytest.fixture
async def harness(home: Path) -> AsyncIterator[Harness]:
    h = Harness(home)
    h.service._load_settings = load_settings  # settings saved by the service are read back
    yield h
    await h.service.aclose()


async def test_always_allow_a_tool(harness: Harness, home: Path) -> None:
    harness.providers["ollama"] = ScriptedProvider(
        reply("", [call("create_folder", path="~/A")]),
        reply("Fait."),
        reply("", [call("create_folder", path="~/B")]),
        reply("Fait aussi."),
        reply("", [call("create_folder", path="~/C")]),
        reply("Et voilà."),
    )
    conversation = harness.service.create_conversation()["id"]
    await harness.service.send_message(conversation, "Crée A")
    request = await harness.next("approval_required")
    assert request["always_options"] == [{"rule": "create_folder", "scope": "tool"}]
    with pytest.raises(ServiceError, match="not offered"):  # only what the card offers
        harness.service.resolve_approval(request["approval_id"], True, always="run_command")
    with pytest.raises(ServiceError, match="not offered"):  # never with a refusal
        harness.service.resolve_approval(request["approval_id"], False, always="create_folder")
    harness.service.resolve_approval(request["approval_id"], True, always="create_folder")
    await harness.next("run_finished")
    assert load_settings().always_allow == ["create_folder"]
    assert harness.service.always_allowed() == [{"rule": "create_folder", "tool": "create_folder"}]

    # From now on, without asking (still shown and recorded).
    await harness.service.send_message(conversation, "Crée B")
    events = await harness.until("run_finished")
    assert not [e for e in events if e["type"] == "approval_required"]
    assert [e["status"] for e in events if e["type"] == "tool_finished"] == ["ok"]
    assert (home / "B").is_dir()

    # Removed in the settings: asked again.
    assert harness.service.forbid_again("create_folder") == []
    with pytest.raises(ServiceError):
        harness.service.forbid_again("create_folder")
    await harness.service.send_message(conversation, "Crée C")
    request = await harness.next("approval_required")
    harness.service.resolve_approval(request["approval_id"], False)
    await harness.next("run_finished")
    assert not (home / "C").exists()


async def test_always_allow_one_command(harness: Harness, home: Path) -> None:
    harness.providers["ollama"] = ScriptedProvider(
        reply("", [call("run_command", command="echo bonjour")]),
        reply("Fait."),
        reply("", [call("run_command", command="echo bonjour")]),
        reply("Encore."),
        reply("", [call("run_command", command="echo bonjour > ~/x")]),
        reply("Non."),
    )
    conversation = harness.service.create_conversation()["id"]
    await harness.service.send_message(conversation, "Dis bonjour")
    request = await harness.next("approval_required")
    assert request["risk"] == "danger"
    rule = "run_command echo bonjour"
    assert request["always_options"][0] == {"rule": rule, "scope": "command"}
    harness.service.resolve_approval(request["approval_id"], True, always=rule)
    await harness.next("run_finished")

    await harness.service.send_message(conversation, "Encore")
    events = await harness.until("run_finished")
    assert not [e for e in events if e["type"] == "approval_required"]
    assert [e["status"] for e in events if e["type"] == "tool_finished"] == ["ok"]

    # Another command (even starting the same way) is still asked.
    await harness.service.send_message(conversation, "Et dans un fichier")
    request = await harness.next("approval_required")
    harness.service.resolve_approval(request["approval_id"], False)
    await harness.next("run_finished")
    assert not (home / "x").exists()
