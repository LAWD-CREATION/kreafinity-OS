from collections.abc import Iterator
from pathlib import Path

import pytest

from kreafinity.i18n import set_language
from kreafinity.tools import ToolContext


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep tests away from the real user's configuration and data.

    Each test starts in French, the default language (a test changing the
    settings may switch the process to English).
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    set_language("fr")
    yield
    set_language("fr")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A fake home folder with a few files.

    Layout::

        Téléchargements/  big.iso (5000 B), video.mp4 (3000 B), notes.txt (10 B),
                          .cache.tmp (hidden), Photos/IMG_001.JPG (2000 B)
        Documents/        Facture-2026.pdf (100 B)
        .ssh/             id_ed25519 (secret)
        .config/app/      settings.ini (hidden folder)
    """
    root = tmp_path / "home" / "david"
    files = {
        "Téléchargements/big.iso": 5000,
        "Téléchargements/video.mp4": 3000,
        "Téléchargements/notes.txt": 10,
        "Téléchargements/.cache.tmp": 50,
        "Téléchargements/Photos/IMG_001.JPG": 2000,
        "Documents/Facture-2026.pdf": 100,
        ".ssh/id_ed25519": 400,
        ".config/app/settings.ini": 20,
    }
    for relative, size in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    return root.resolve()


@pytest.fixture
def context(home: Path) -> ToolContext:
    return ToolContext.create(home=home)
