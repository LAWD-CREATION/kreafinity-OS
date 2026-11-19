import json
from pathlib import Path
from typing import Any

import pytest

from kreafinity.tools import (
    RiskLevel,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolResult,
    available_tools,
)
from kreafinity.tools.list_directory import ListDirectory
from kreafinity.tools.search_files import SearchFiles
from kreafinity.tools.system_info import SystemInfo


def payload(result: ToolResult) -> Any:
    assert not result.is_error
    return json.loads(result.content)


# --- Sandbox -----------------------------------------------------------------


def test_resolve_paths(context: ToolContext, home: Path) -> None:
    assert context.resolve(None) == home
    assert context.resolve("~") == home
    assert context.resolve("~/Documents") == home / "Documents"
    assert context.resolve("Documents") == home / "Documents"
    assert context.resolve(str(home / "Documents" / "..")) == home


@pytest.mark.parametrize("raw", ["/etc", "~/..", "~root", "~/.ssh", "~/.ssh/id_ed25519"])
def test_resolve_refuses_outside_or_denied(context: ToolContext, raw: str) -> None:
    with pytest.raises(ToolError):
        context.resolve(raw)


def test_symlink_cannot_escape(context: ToolContext, home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "escape").symlink_to(outside)
    with pytest.raises(ToolError, match="Access denied"):
        context.resolve("~/escape")


def test_extra_allowed_root(home: Path, tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    context = ToolContext.create(home=home, allowed_paths=["~", str(media)])
    assert context.resolve(str(media)) == media.resolve()


# --- list_directory ----------------------------------------------------------


async def test_list_directory_largest_files(context: ToolContext, home: Path) -> None:
    result = payload(
        await ListDirectory().run({"path": "~/Téléchargements", "sort_by": "size"}, context)
    )
    assert result["path"] == str(home / "Téléchargements")
    names = [e["name"] for e in result["entries"]]
    assert names == ["big.iso", "video.mp4", "notes.txt", "Photos"]  # hidden file excluded
    assert result["entries"][0]["size_bytes"] == 5000
    assert result["entries"][0]["size"] == "4.9 KiB"
    assert result["entries"][3]["type"] == "directory"


async def test_list_directory_limit_and_default_sort(context: ToolContext) -> None:
    result = payload(await ListDirectory().run({"path": "Téléchargements", "limit": 2}, context))
    assert result["total_entries"] == 4
    assert result["truncated"] is True
    assert [e["name"] for e in result["entries"]] == ["Photos", "big.iso"]  # folders first


async def test_list_directory_hidden_but_never_denied(context: ToolContext) -> None:
    result = payload(await ListDirectory().run({"include_hidden": True}, context))
    names = {e["name"] for e in result["entries"]}
    assert ".config" in names
    assert ".ssh" not in names


async def test_list_directory_errors(context: ToolContext) -> None:
    with pytest.raises(ToolError, match="not found"):
        await ListDirectory().run({"path": "~/Nope"}, context)
    with pytest.raises(ToolError, match="Not a folder"):
        await ListDirectory().run({"path": "~/Documents/Facture-2026.pdf"}, context)
    with pytest.raises(ToolError, match="Access denied"):
        await ListDirectory().run({"path": "/etc"}, context)


async def test_list_directory_shows_symlinks_without_following(
    context: ToolContext, home: Path, tmp_path: Path
) -> None:
    (home / "Documents" / "lien").symlink_to(tmp_path)
    result = payload(await ListDirectory().run({"path": "~/Documents"}, context))
    link = next(e for e in result["entries"] if e["name"] == "lien")
    assert link["type"] == "symlink"


# --- search_files ------------------------------------------------------------


async def test_search_by_wildcard_case_insensitive(context: ToolContext, home: Path) -> None:
    result = payload(await SearchFiles().run({"pattern": "*.jpg"}, context))
    assert [r["path"] for r in result["results"]] == [
        str(home / "Téléchargements" / "Photos" / "IMG_001.JPG")
    ]


async def test_search_substring(context: ToolContext) -> None:
    result = payload(await SearchFiles().run({"pattern": "facture"}, context))
    assert [r["name"] for r in result["results"]] == ["Facture-2026.pdf"]


async def test_search_largest_files(context: ToolContext) -> None:
    result = payload(
        await SearchFiles().run(
            {"pattern": "*", "path": "~/Téléchargements", "sort_by": "size", "limit": 3},
            context,
        )
    )
    assert [r["name"] for r in result["results"]] == ["big.iso", "video.mp4", "IMG_001.JPG"]
    assert result["total_matches"] == 4
    assert result["truncated"] is True
    assert result["search_complete"] is True


async def test_search_directories(context: ToolContext) -> None:
    result = payload(await SearchFiles().run({"pattern": "photo", "kind": "directory"}, context))
    assert [r["name"] for r in result["results"]] == ["Photos"]


async def test_search_skips_hidden_and_denied(context: ToolContext) -> None:
    hidden_excluded = payload(await SearchFiles().run({"pattern": "*"}, context))
    names = {r["name"] for r in hidden_excluded["results"]}
    assert "settings.ini" not in names and ".cache.tmp" not in names

    with_hidden = payload(
        await SearchFiles().run({"pattern": "*", "include_hidden": True, "limit": 200}, context)
    )
    names = {r["name"] for r in with_hidden["results"]}
    assert {"settings.ini", ".cache.tmp"} <= names
    assert "id_ed25519" not in names


async def test_search_does_not_follow_symlinks(
    context: ToolContext, home: Path, tmp_path: Path
) -> None:
    secret_dir = tmp_path / "elsewhere"
    secret_dir.mkdir()
    (secret_dir / "secret.txt").write_text("x")
    (home / "Documents" / "lien").symlink_to(secret_dir)
    result = payload(await SearchFiles().run({"pattern": "secret"}, context))
    assert result["results"] == []


# --- system_info and registry ------------------------------------------------


async def test_system_info(context: ToolContext) -> None:
    info = payload(await SystemInfo().run({}, context))
    assert set(info) == {"os", "cpu", "memory", "disks", "battery"}
    assert info["cpu"]["logical_cores"] >= 1
    assert info["memory"]["total_gib"] > 0


def test_builtin_tools_are_safe_and_registered() -> None:
    assert {"system_info", "list_directory", "search_files"} <= set(available_tools())
    registry = ToolRegistry.load()
    for name in ("system_info", "list_directory", "search_files"):
        tool = registry.get(name)
        assert tool is not None and tool.risk is RiskLevel.SAFE


def test_registry_subset_and_specs() -> None:
    registry = ToolRegistry.load(["search_files", "does_not_exist"])
    assert [tool.name for tool in registry] == ["search_files"]
    spec = registry.specs()[0]
    assert spec.name == "search_files"
    assert spec.parameters["required"] == ["pattern"]
