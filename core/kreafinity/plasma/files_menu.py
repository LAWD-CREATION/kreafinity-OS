"""« Kreafinity » in the file manager's right-click menu (Dolphin service menus).

Each action sends a question about the selected files and folders to the
assistant; the answer shows in the assistant panel (or else in the browser).
The question only names the selection: the assistant looks at it with its
usual tools, within the folders it may access, and asks before changing
anything, as always.

Run by the menu as ``python -m kreafinity.plasma.files_menu ACTION PATH…``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from kreafinity.api.client import DaemonUnavailableError
from kreafinity.config import load_settings
from kreafinity.i18n import set_language, t
from kreafinity.plasma.krunner import send_question
from kreafinity.system import desktop
from kreafinity.system.notifications import send_notification

logger = logging.getLogger(__name__)

#: Menu actions; « ask » first asks the user for their question.
ACTIONS = ("explain", "summarize", "organize", "ask")
#: Files named in one question (the rest are counted).
MAX_FILES = 20
#: Longest question typed in the dialog.
MAX_QUESTION = 2000


def question(action: str, paths: Sequence[Path], custom: str | None = None) -> str:
    """The question sent to the assistant for `action` on the selected `paths`."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action}")
    intro = (custom or "").strip()[:MAX_QUESTION] if action == "ask" else t(f"files.{action}")
    lines = [f"- {_one_line(str(path))}" for path in paths[:MAX_FILES]]
    if len(paths) > MAX_FILES:
        lines.append(t("files.more", count=len(paths) - MAX_FILES))
    return "\n".join([intro, "", t("files.selection"), *lines])


def _one_line(text: str) -> str:
    """A file name on one line (a name may contain line breaks)."""
    return " ".join(text.splitlines())


def ask_user() -> str | None:
    """Ask the user for their question in a small dialog; None if cancelled."""
    title, label = t("files.dialog_title"), t("files.dialog_label")
    if kdialog := shutil.which("kdialog"):
        command = [kdialog, "--title", title, "--inputbox", label]
    elif zenity := shutil.which("zenity"):
        command = [zenity, "--entry", f"--title={title}", f"--text={label}"]
    else:
        raise OSError("no program to show a dialog (kdialog or zenity)")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    answer = result.stdout.strip()
    return answer if result.returncode == 0 and answer else None


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m kreafinity.plasma.files_menu")
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    settings = load_settings()
    set_language(settings.language)
    paths = [path.expanduser().absolute() for path in args.paths]
    try:
        custom = ask_user() if args.action == "ask" else None
        if args.action == "ask" and custom is None:
            return 0  # cancelled
        asyncio.run(send_question(settings.port, question(args.action, paths, custom)))
    except (DaemonUnavailableError, desktop.LaunchError, OSError, ValueError) as exc:
        logger.warning("Cannot ask Kreafinity: %s", exc)
        asyncio.run(send_notification(t("plasma.search.failed"), str(exc)))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
