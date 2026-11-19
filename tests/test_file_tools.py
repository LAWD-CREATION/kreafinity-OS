import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

from kreafinity.system import trash as trash_module
from kreafinity.system.trash import TrashError, home_trash_dir, move_to_trash
from kreafinity.tools import RiskLevel, ToolContext, ToolError, ToolRegistry, ToolResult


def tool(name: str) -> Any:
    found = ToolRegistry.load([name]).get(name)
    assert found is not None
    return found


def payload(result: ToolResult) -> Any:
    return json.loads(result.content)


def test_risk_levels() -> None:
    expected = {
        "read_file": RiskLevel.SAFE,
        "create_folder": RiskLevel.CONFIRM,
        "create_file": RiskLevel.CONFIRM,
        "move_file": RiskLevel.CONFIRM,
        "move_to_trash": RiskLevel.DANGER,
    }
    for name, risk in expected.items():
        assert tool(name).risk is risk


# --- Trash (freedesktop specification) ----------------------------------------


def test_trash_file_with_info_record(home: Path) -> None:
    source = home / "Téléchargements" / "big.iso"
    location = move_to_trash(source)

    trash = home_trash_dir()
    assert location == trash / "files" / "big.iso"
    assert not source.exists() and location is not None and location.exists()
    info = (trash / "info" / "big.iso.trashinfo").read_text(encoding="utf-8").splitlines()
    assert info[0] == "[Trash Info]"
    assert info[1].startswith("Path=") and "%C3%A9" in info[1]  # é percent-encoded
    assert unquote(info[1].removeprefix("Path=")) == str(source)
    assert info[2].startswith("DeletionDate=") and len(info[2]) == len("DeletionDate=") + 19
    assert (trash / "files").stat().st_mode & 0o777 == 0o700


def test_trash_name_collisions(home: Path) -> None:
    first = home / "a" / "notes.txt"
    second = home / "b" / "notes.txt"
    for path in (first, second):
        path.parent.mkdir()
        path.write_text(str(path))
    assert move_to_trash(first) == home_trash_dir() / "files" / "notes.txt"
    assert move_to_trash(second) == home_trash_dir() / "files" / "notes.2.txt"
    assert (home_trash_dir() / "info" / "notes.2.txt.trashinfo").exists()


def test_trash_folder_and_symlink_itself(home: Path) -> None:
    link = home / "lien"
    link.symlink_to(home / "Documents")
    move_to_trash(link)
    assert (home / "Documents").is_dir()  # the target is untouched
    assert (home_trash_dir() / "files" / "lien").is_symlink()

    move_to_trash(home / "Téléchargements")
    assert (home_trash_dir() / "files" / "Téléchargements" / "Photos" / "IMG_001.JPG").exists()


def test_trash_missing_file(home: Path) -> None:
    with pytest.raises(TrashError):
        move_to_trash(home / "nope")
    assert not (home_trash_dir() / "info").exists() or not any(
        (home_trash_dir() / "info").iterdir()
    )


def test_trash_other_filesystem_uses_gio(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(trash_module, "_trash_with_gio", calls.append)
    real_stat = trash_module.os.stat

    def fake_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        result = real_stat(path, *args, **kwargs)
        if str(path).endswith("files"):
            fields = list(result)
            fields[2] += 1  # st_dev: pretend the trash is on another device
            return type(result)(fields)
        return result

    monkeypatch.setattr(trash_module.os, "stat", fake_stat)
    source = home / "Documents" / "Facture-2026.pdf"
    assert move_to_trash(source) is None
    assert calls == [source]
    assert source.exists()  # left to gio (mocked here)


# --- read_file ------------------------------------------------------------------


async def test_read_file(context: ToolContext, home: Path) -> None:
    (home / "Documents" / "note.md").write_text("# Courses\n- café", encoding="utf-8")
    result = payload(await tool("read_file").run({"path": "~/Documents/note.md"}, context))
    assert result["content"] == "# Courses\n- café"
    assert result["truncated"] is False


async def test_read_file_refuses_binary_and_secrets(context: ToolContext, home: Path) -> None:
    (home / "image.png").write_bytes(b"\x89PNG\x00\x00")
    with pytest.raises(ToolError, match="binary"):
        await tool("read_file").run({"path": "~/image.png"}, context)
    with pytest.raises(ToolError, match="Access denied"):
        await tool("read_file").run({"path": "~/.ssh/id_ed25519"}, context)
    with pytest.raises(ToolError, match="Not a file"):
        await tool("read_file").run({"path": "~/Documents"}, context)


async def test_read_file_truncates(context: ToolContext, home: Path) -> None:
    (home / "big.log").write_text("a" * 150_000)
    result = payload(await tool("read_file").run({"path": "~/big.log"}, context))
    assert result["truncated"] is True and len(result["content"]) == 100_000


# --- create_folder / create_file ---------------------------------------------


async def test_create_folder(context: ToolContext, home: Path) -> None:
    create = tool("create_folder")
    preview = await create.preview({"path": "~/Projets/Kreafinity"}, context)
    assert preview.summary == "Créer un dossier"
    assert preview.details == (str(home / "Projets" / "Kreafinity"),)
    await create.run({"path": "~/Projets/Kreafinity"}, context)
    assert (home / "Projets" / "Kreafinity").is_dir()
    with pytest.raises(ToolError, match="Already exists"):
        await create.preview({"path": "~/Projets"}, context)


async def test_create_file(context: ToolContext, home: Path) -> None:
    create = tool("create_file")
    content = "ligne 1\nligne 2\nligne 3\nligne 4\nligne 5\nligne 6"
    arguments = {"path": "~/Documents/todo.txt", "content": content}
    preview = await create.preview(arguments, context)
    assert preview.summary.startswith("Créer un fichier texte")
    assert preview.details[0] == str(home / "Documents" / "todo.txt")
    assert preview.details[1:3] == ("│ ligne 1", "│ ligne 2")
    assert preview.details[-1] == "│ …"

    await create.run(arguments, context)
    assert (home / "Documents" / "todo.txt").read_text(encoding="utf-8") == content
    with pytest.raises(ToolError, match="Already exists"):
        await create.run(arguments, context)  # never overwrites
    with pytest.raises(ToolError, match="Folder not found"):
        await create.preview({"path": "~/Nope/x.txt", "content": ""}, context)
    with pytest.raises(ToolError, match="Access denied"):
        await create.preview({"path": "/tmp/x.txt", "content": ""}, context)


# --- move_file -------------------------------------------------------------------


async def test_rename(context: ToolContext, home: Path) -> None:
    move = tool("move_file")
    arguments = {"source": "~/Documents/Facture-2026.pdf", "destination": "~/Documents/f.pdf"}
    preview = await move.preview(arguments, context)
    assert preview.summary == "Renommer"
    await move.run(arguments, context)
    assert (home / "Documents" / "f.pdf").exists()
    assert not (home / "Documents" / "Facture-2026.pdf").exists()


async def test_move_into_folder(context: ToolContext, home: Path) -> None:
    move = tool("move_file")
    arguments = {"source": "~/Téléchargements/notes.txt", "destination": "~/Documents"}
    preview = await move.preview(arguments, context)
    assert preview.summary == "Déplacer"
    assert preview.details == (
        f"{home / 'Téléchargements' / 'notes.txt'} → {home / 'Documents' / 'notes.txt'}",
    )
    await move.run(arguments, context)
    assert (home / "Documents" / "notes.txt").exists()


@pytest.mark.parametrize(
    ("source", "destination", "message"),
    [
        ("~/nope", "~/Documents", "Not found"),
        ("~/Documents/Facture-2026.pdf", "~/Téléchargements/notes.txt", "Already exists"),
        ("~/Téléchargements", "~/Téléchargements/Photos", "into itself"),
        ("~", "~/Documents", "protected"),
        ("~/Documents/Facture-2026.pdf", "/tmp", "Access denied"),
        ("~/Documents/Facture-2026.pdf", "~/.ssh", "Access denied"),
        ("~/Documents/Facture-2026.pdf", "~/Nope/f.pdf", "Folder not found"),
    ],
)
async def test_move_refusals(
    context: ToolContext, source: str, destination: str, message: str
) -> None:
    with pytest.raises(ToolError, match=message):
        await tool("move_file").preview({"source": source, "destination": destination}, context)


# --- move_to_trash ----------------------------------------------------------------


async def test_move_to_trash_preview_and_run(context: ToolContext, home: Path) -> None:
    trash_tool = tool("move_to_trash")
    arguments = {
        "paths": [
            "~/Téléchargements/big.iso",
            "~/Téléchargements/Photos",
            "~/Téléchargements/Photos/IMG_001.JPG",  # inside a trashed folder: dropped
            "~/Téléchargements/big.iso",  # duplicate: dropped
        ]
    }
    preview = await trash_tool.preview(arguments, context)
    assert preview.summary == "Mettre 2 éléments à la corbeille"
    assert preview.details == (
        f"{home / 'Téléchargements' / 'Photos'} (dossier, 1 fichier, 2.0 KiB)",
        f"{home / 'Téléchargements' / 'big.iso'} (4.9 KiB)",
    )

    result = await trash_tool.run(arguments, context)
    assert not result.is_error
    assert sorted(payload(result)["trashed"]) == sorted(
        [str(home / "Téléchargements" / "big.iso"), str(home / "Téléchargements" / "Photos")]
    )
    assert (home_trash_dir() / "files" / "big.iso").exists()
    assert not (home / "Téléchargements" / "big.iso").exists()


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("~", "protected"),
        ("~/..", "Access denied"),
        ("~/.ssh/id_ed25519", "Access denied"),
        ("~/nope.txt", "Not found"),
        ("/etc/passwd", "Access denied"),
    ],
)
async def test_move_to_trash_refusals(context: ToolContext, path: str, message: str) -> None:
    with pytest.raises(ToolError, match=message):
        await tool("move_to_trash").preview({"paths": [path]}, context)


async def test_move_to_trash_reports_failures(
    context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kreafinity.tools.move_to_trash as module

    def failing(path: Path) -> None:
        raise TrashError(f"{path}: Permission denied")

    monkeypatch.setattr(module, "move_to_trash", failing)
    result = await tool("move_to_trash").run({"paths": ["~/Documents/Facture-2026.pdf"]}, context)
    assert result.is_error
    assert payload(result)["failed"][0]["error"].endswith("Permission denied")
