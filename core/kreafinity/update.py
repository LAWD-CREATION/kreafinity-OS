"""Updates of Kreafinity: finding a newer version on GitHub, and installing it.

The installer records what it installed in ``~/.local/share/kreafinity/install.json``
(repository, branch, exact commit). Checking asks GitHub's API what changed on
that branch since; installing downloads the new commit into the same Python
environment, refreshes the integration with the session and restarts
Kreafinity. A development checkout has no record: it is updated with git.

``python -m kreafinity.update apply <commit>`` installs an update; the daemon
starts it in its own process, so that it survives the daemon's restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from kreafinity.paths import data_dir

logger = logging.getLogger(__name__)

#: How often the daemon looks for an update.
CHECK_INTERVAL = timedelta(hours=24)
#: Changes listed at most in a check.
MAX_CHANGES = 20
_COMMIT = re.compile(r"[0-9a-f]{7,40}")
_GITHUB = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


class UpdateError(Exception):
    """An update cannot be checked or installed; the message says why.

    Attributes:
        code: ``unsupported`` (development copy), ``up_to_date``, ``busy``
            (already installing) or ``network`` (GitHub cannot be asked).
    """

    def __init__(self, message: str, *, code: str = "network") -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --- What is installed ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Installation:
    """What the installer installed.

    Attributes:
        repository: Web address of the repository (``https://github.com/owner/name``).
        ref: Branch (or tag) followed by updates.
        commit: Exact commit installed, if known.
        installed_at: When it was installed (ISO 8601), if known.
        api: Address of the repository in GitHub's API (other forges: given explicitly).
    """

    repository: str
    ref: str
    commit: str | None = None
    installed_at: str | None = None
    api: str | None = None

    def api_url(self) -> str | None:
        if self.api:
            return self.api.rstrip("/")
        match = _GITHUB.match(self.repository)
        if match is None:
            return None
        return f"https://api.github.com/repos/{match.group(1)}/{match.group(2)}"

    def archive_url(self, commit: str) -> str:
        return f"{self.repository.rstrip('/')}/archive/{commit}.tar.gz"


def install_record_path() -> Path:
    return data_dir() / "install.json"


def installation() -> Installation | None:
    """What the installer installed, or None for a development checkout."""
    data = _read_json(install_record_path())
    if data is None:
        return None
    repository, ref = data.get("repository"), data.get("ref")
    if not isinstance(repository, str) or not isinstance(ref, str) or not repository or not ref:
        return None
    commit = data.get("commit")
    installed_at = data.get("installed_at")
    api = data.get("api")
    return Installation(
        repository=repository,
        ref=ref,
        commit=commit if isinstance(commit, str) and _COMMIT.fullmatch(commit) else None,
        installed_at=installed_at if isinstance(installed_at, str) else None,
        api=api if isinstance(api, str) and api else None,
    )


def record_installation(record: Installation) -> None:
    data = {key: value for key, value in asdict(record).items() if value is not None}
    _write_json(install_record_path(), data)


# --- Checking -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Change:
    """A change of the new version (a commit)."""

    commit: str
    title: str
    date: str | None = None


@dataclass(slots=True)
class UpdateCheck:
    """The result of the last check (kept in ``update.json``).

    Attributes:
        available: A newer version exists.
        latest: Its commit.
        changes: What it brings, oldest first.
        checked_at: When GitHub was asked.
        error: Why the last check failed, if it did.
        notified: Commit the user was last notified about (one notification per version).
    """

    available: bool = False
    latest: str | None = None
    changes: list[Change] = field(default_factory=list)
    checked_at: str | None = None
    error: str | None = None
    notified: str | None = None

    def is_stale(self, interval: timedelta = CHECK_INTERVAL) -> bool:
        if self.checked_at is None:
            return True
        try:
            checked = datetime.fromisoformat(self.checked_at)
        except ValueError:
            return True
        return datetime.now(UTC) - checked >= interval


def check_record_path() -> Path:
    return data_dir() / "update.json"


def load_check() -> UpdateCheck:
    data = _read_json(check_record_path()) or {}
    changes = [
        Change(**{k: v for k, v in item.items() if k in ("commit", "title", "date")})
        for item in data.get("changes", [])
        if isinstance(item, dict) and isinstance(item.get("commit"), str)
    ]
    return UpdateCheck(
        available=bool(data.get("available")),
        latest=data.get("latest") if isinstance(data.get("latest"), str) else None,
        changes=changes,
        checked_at=data.get("checked_at") if isinstance(data.get("checked_at"), str) else None,
        error=data.get("error") if isinstance(data.get("error"), str) else None,
        notified=data.get("notified") if isinstance(data.get("notified"), str) else None,
    )


def save_check(result: UpdateCheck) -> None:
    _write_json(check_record_path(), asdict(result))


def _change(entry: dict[str, Any]) -> Change:
    commit = entry.get("commit") or {}
    message = str(commit.get("message") or "").strip()
    date = (commit.get("committer") or {}).get("date")
    return Change(
        commit=str(entry.get("sha", "")),
        title=message.splitlines()[0][:200] if message else "",
        date=date if isinstance(date, str) else None,
    )


async def check(record: Installation, client: httpx.AsyncClient | None = None) -> UpdateCheck:
    """Ask GitHub what changed on the followed branch since the installed version.

    Raises:
        UpdateError: The repository cannot be asked (no network, rate limit…).
    """
    api = record.api_url()
    if api is None:
        raise UpdateError(
            f"updates are only checked on GitHub ({record.repository})", code="unsupported"
        )
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Kreafinity"}
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=20, follow_redirects=True)
    try:
        if record.commit:
            data = await _get(client, f"{api}/compare/{record.commit}...{record.ref}", headers)
            entries = data.get("commits") if data.get("status") in ("ahead", "diverged") else []
        else:  # version unknown: everything published since the installation
            params = {"sha": record.ref, "per_page": str(MAX_CHANGES)}
            if record.installed_at:
                params["since"] = record.installed_at
            listed = await _get(client, f"{api}/commits", headers, params)
            entries = list(reversed(listed)) if isinstance(listed, list) else []
    finally:
        if own_client:
            await client.aclose()
    changes = [_change(entry) for entry in entries or [] if isinstance(entry, dict)]
    changes = [change for change in changes if _COMMIT.fullmatch(change.commit)]
    latest = changes[-1].commit if changes else record.commit
    shown = [change for change in changes if not change.title.startswith("Merge ")]
    return UpdateCheck(
        available=bool(changes) and latest != record.commit,
        latest=latest,
        changes=shown[-MAX_CHANGES:],
        checked_at=_now(),
    )


async def _get(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
) -> Any:
    try:
        response = await client.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        raise UpdateError(f"cannot reach GitHub: {exc}") from exc
    if response.status_code != 200:
        try:
            detail = response.json().get("message", "")
        except ValueError:
            detail = response.text[:200]
        raise UpdateError(f"GitHub answered HTTP {response.status_code}: {detail}")
    try:
        return response.json()
    except ValueError as exc:
        raise UpdateError("unexpected answer from GitHub") from exc


# --- Installing -----------------------------------------------------------------------------------


@dataclass(slots=True)
class Status:
    """Progress of an installation (kept in ``update-status.json``)."""

    state: str = "idle"  # idle, running, done or failed
    target: str | None = None
    message: str | None = None
    updated_at: str | None = None


def status_path() -> Path:
    return data_dir() / "update-status.json"


def read_status() -> Status:
    data = _read_json(status_path()) or {}
    state = data.get("state")
    return Status(
        state=state if state in ("idle", "running", "done", "failed") else "idle",
        target=data.get("target") if isinstance(data.get("target"), str) else None,
        message=data.get("message") if isinstance(data.get("message"), str) else None,
        updated_at=data.get("updated_at") if isinstance(data.get("updated_at"), str) else None,
    )


def write_status(state: str, target: str | None, message: str | None = None) -> None:
    _write_json(status_path(), asdict(Status(state, target, message, _now())))


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=900, check=False
    )


def apply(target: str, *, run: Runner = _run, log: Callable[[str], None] = logger.info) -> None:
    """Install the version `target` (a commit) over the current one, then restart.

    Raises:
        UpdateError: Not installed from a repository, or the installation failed.
    """
    record = installation()
    if record is None:
        raise UpdateError(
            "this is a development installation: update it with git", code="unsupported"
        )
    if not _COMMIT.fullmatch(target):
        raise UpdateError(f"invalid version: {target!r}", code="unsupported")
    write_status("running", target)
    try:
        log(f"Downloading and installing {target[:7]}…")
        result = run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--quiet",
                "--disable-pip-version-check",
                "--upgrade",
                record.archive_url(target),
            ]
        )
        if result.returncode != 0:
            raise UpdateError(f"installation failed: {(result.stderr or result.stdout)[-800:]}")
        record_installation(Installation(record.repository, record.ref, target, _now(), record.api))
        log("Refreshing the integration with the session…")
        # The new version writes its own service, menu and desktop files.
        run([sys.executable, "-m", "kreafinity.cli", "setup", "--refresh"])
    except UpdateError as exc:
        write_status("failed", target, str(exc))
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        write_status("failed", target, str(exc))
        raise UpdateError(str(exc)) from exc
    write_status("done", target)
    log("Restarting Kreafinity…")
    restart(run=run)


def restart(*, run: Runner = _run) -> None:
    """Restart what runs the old version: daemon, bubble, desktop search."""
    import psutil

    from kreafinity.system import integration

    venv = Path(sys.prefix)
    if venv == Path(sys.base_prefix):
        return  # not an installed environment: nothing of ours to restart
    service = _service_active(run)
    stopping: list[psutil.Process] = []
    bubble_was_running = False
    for process in integration.processes_running_from(venv):
        try:
            command = " ".join(process.cmdline())
        except psutil.Error:
            continue
        if "kreafinity_bubble.py" in command:
            bubble_was_running = True
        elif "kreafinity.api.server" in command and service:
            continue  # systemd restarts it
        elif "kreafinity.api.server" not in command and "kreafinity.plasma.krunner" not in command:
            continue
        stopping.append(process)
    for process in stopping:
        with contextlib.suppress(psutil.Error):
            process.terminate()
    # The new daemon needs the port: wait until the old one has really stopped.
    _, alive = psutil.wait_procs(stopping, timeout=10)
    for process in alive:
        with contextlib.suppress(psutil.Error):
            process.kill()
    if service:
        run(["systemctl", "--user", "restart", integration.SERVICE_NAME])
    else:
        _spawn([sys.executable, "-m", "kreafinity.api.server"], "daemon.log")
    if bubble_was_running:
        _spawn([sys.executable, "-m", "kreafinity.cli", "bubble"], "bubble.log")
    for tool in ("kquitapp6", "kquitapp5"):  # the search reloads its plugin
        if shutil.which(tool):
            run([tool, "krunner"])
            break


def _service_active(run: Runner) -> bool:
    from kreafinity.system import integration

    if not integration.service_installed() or shutil.which("systemctl") is None:
        return False
    return run(["systemctl", "--user", "is-active", integration.SERVICE_NAME]).returncode == 0


def _spawn(command: list[str], log_name: str) -> None:
    log_path = data_dir() / log_name
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as log:
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True
        )


def launch(target: str) -> None:
    """Start installing `target` in its own process (it outlives the daemon's restart)."""
    from kreafinity.system import integration

    command = [sys.executable, "-m", "kreafinity.update", "apply", target]
    systemd_run = shutil.which("systemd-run")
    if systemd_run and integration.service_installed() and integration.systemd_available():
        # Outside the daemon's service, which systemd stops entirely on restart.
        started = subprocess.run(
            [
                systemd_run,
                "--user",
                "--collect",
                "--quiet",
                f"--unit=kreafinity-update-{target[:7]}",
                *command,
            ],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if started.returncode == 0:
            return
    _spawn(command, "update.log")


#: An installation still « running » after this long was interrupted.
STALE_RUN = timedelta(minutes=30)


class UpdateManager:
    """The daemon's side: daily check, notification, and starting the installation."""

    def __init__(
        self,
        *,
        publish: Callable[[dict[str, Any]], None],
        enabled: Callable[[], bool],
        notify: Callable[[str, str], Awaitable[bool]] | None = None,
        launcher: Callable[[str], None] = launch,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create the manager.

        Args:
            publish: Sends an event to the interfaces.
            enabled: Whether automatic checks are allowed (user setting).
            notify: Shows a desktop notification (summary, body).
            launcher: Starts the installation of a commit in its own process.
            client: HTTP client for GitHub (for tests).
        """
        self._publish = publish
        self._enabled = enabled
        self._notify = notify
        self._launcher = launcher
        self._client = client

    def info(self) -> dict[str, Any]:
        """What is installed, what is available, and the progress of an installation."""
        record = installation()
        result = load_check()
        status = read_status()
        if status.state == "running" and _older_than(status.updated_at, STALE_RUN):
            status = Status("failed", status.target, "interrupted", status.updated_at)
        available = bool(
            record and result.available and result.latest and result.latest != record.commit
        )
        return {
            "supported": record is not None and record.api_url() is not None,
            "current": (
                {"commit": record.commit, "ref": record.ref, "installed_at": record.installed_at}
                if record
                else None
            ),
            "available": available,
            "latest": result.latest if available else None,
            "changes": [asdict(change) for change in result.changes] if available else [],
            "checked_at": result.checked_at,
            "error": result.error,
            "status": asdict(status),
            "check_updates": self._enabled(),
        }

    async def check(self) -> dict[str, Any]:
        """Ask GitHub now; tell the user (once per version) when an update appears.

        Raises:
            UpdateError: Not installed from GitHub, or GitHub cannot be asked.
        """
        record = installation()
        if record is None:
            raise UpdateError(
                "this is a development installation: update it with git", code="unsupported"
            )
        previous = load_check()
        try:
            result = await check(record, self._client)
        except UpdateError as exc:
            previous.error = str(exc)
            save_check(previous)
            raise
        result.notified = previous.notified
        is_new = result.available and result.latest not in (previous.notified, record.commit)
        if is_new and result.latest:
            result.notified = result.latest
        save_check(result)
        info = self.info()
        if is_new:
            self._publish({"type": "update_available", "update": info})
            if self._notify is not None:
                from kreafinity.i18n import t

                count = len(info["changes"])
                await self._notify(
                    t("update.notification.title"), t("update.notification.body", count=count)
                )
        return info

    def start(self) -> dict[str, Any]:
        """Start installing the available version, in its own process.

        Raises:
            UpdateError: Nothing to install, or an installation is already running.
        """
        info = self.info()
        if not info["supported"]:
            raise UpdateError(
                "this is a development installation: update it with git", code="unsupported"
            )
        if info["status"]["state"] == "running":
            raise UpdateError("an update is already being installed", code="busy")
        if not info["available"]:
            raise UpdateError("Kreafinity is up to date", code="up_to_date")
        target = info["latest"]
        write_status("running", target)
        try:
            self._launcher(target)
        except OSError as exc:
            write_status("failed", target, str(exc))
            raise UpdateError(f"cannot start the update: {exc}", code="busy") from exc
        self._publish({"type": "update_started", "target": target})
        return self.info()

    async def watch(self, *, first_delay: float = 60.0, period: float = 3600.0) -> None:
        """Check once a day, while automatic checks are allowed (runs until cancelled)."""
        await asyncio.sleep(first_delay)
        while True:
            try:
                if self._enabled() and installation() is not None and load_check().is_stale():
                    await self.check()
            except UpdateError as exc:
                logger.info("Update check failed: %s", exc)
            except Exception:
                logger.exception("Update check failed")
            await asyncio.sleep(period)


def _older_than(moment: str | None, age: timedelta) -> bool:
    if moment is None:
        return True
    try:
        return datetime.now(UTC) - datetime.fromisoformat(moment) > age
    except ValueError:
        return True


USAGE = """usage:
  python -m kreafinity.update apply <commit>
  python -m kreafinity.update record <repository> <ref> [<commit>]   (used by the installer)"""


def main(argv: list[str] | None = None) -> int:
    """Install an update, or record what the installer installed."""
    arguments = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(arguments) == 2 and arguments[0] == "apply":
        try:
            apply(arguments[1])
        except UpdateError as exc:
            logger.error("Update failed: %s", exc)
            return 1
        return 0
    if len(arguments) in (3, 4) and arguments[0] == "record":
        commit = arguments[3] if len(arguments) == 4 and _COMMIT.fullmatch(arguments[3]) else None
        record_installation(Installation(arguments[1], arguments[2], commit, _now()))
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
