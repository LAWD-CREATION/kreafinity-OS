"""Updates: checking GitHub, the daemon's manager, installing, the API and the CLI."""

import asyncio
import json
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kreafinity import cli, update
from kreafinity.agent import ConversationStore
from kreafinity.api import create_app
from kreafinity.config import ConfigError, load_settings
from kreafinity.service import AssistantService
from kreafinity.tools import ToolContext

OLD = "a" * 40
NEW = "c" * 40
MIDDLE = "b" * 40
REPOSITORY = "https://github.com/LAWD-CREATION/kreafinity-OS"
API = "https://api.github.com/repos/LAWD-CREATION/kreafinity-OS"
TOKEN = "t" * 43


def commit(sha: str, title: str, body: str = "") -> dict[str, Any]:
    message = f"{title}\n\n{body}" if body else title
    return {
        "sha": sha,
        "commit": {"message": message, "committer": {"date": "2026-11-20T10:00:00Z"}},
    }


def compare_answer(status: str = "ahead") -> dict[str, Any]:
    commits = [
        commit(MIDDLE, "feat: memory that improves", "Details."),
        commit("d" * 40, "Merge pull request #3 from LAWD-CREATION/branch"),
        commit(NEW, "fix: faster search"),
    ]
    return {"status": status, "ahead_by": 3, "commits": commits if status != "identical" else []}


def github(routes: dict[str, Any], seen: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    """A fake GitHub API: path → JSON answer (or a status code)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        answer = routes.get(request.url.path)
        if answer is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if isinstance(answer, int):
            return httpx.Response(answer, json={"message": "API rate limit exceeded"})
        return httpx.Response(200, json=answer)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def installed(commit_sha: str | None = OLD, **extra: Any) -> update.Installation:
    record = update.Installation(
        REPOSITORY, "main", commit_sha, "2026-11-01T00:00:00+00:00", **extra
    )
    update.record_installation(record)
    return record


COMPARE = "/repos/LAWD-CREATION/kreafinity-OS/compare/" + OLD + "...main"


# --- What is installed ------------------------------------------------------------------------


def test_installation_record() -> None:
    assert update.installation() is None  # development copy
    record = installed()
    assert update.installation() == record
    assert record.api_url() == API
    assert record.archive_url(NEW) == f"{REPOSITORY}/archive/{NEW}.tar.gz"
    path = update.install_record_path()
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text(json.dumps({"repository": REPOSITORY, "ref": "main", "commit": "not-a-commit"}))
    assert update.installation() == update.Installation(REPOSITORY, "main")
    path.write_text("{broken")
    assert update.installation() is None
    other = update.Installation(
        "https://git.example.org/k/os", "main", api="https://git.example.org/api/"
    )
    assert other.api_url() == "https://git.example.org/api"
    assert update.Installation("https://git.example.org/k/os", "main").api_url() is None


def test_record_command() -> None:
    assert update.main(["record", REPOSITORY, "main", NEW]) == 0
    record = update.installation()
    assert record is not None and record.commit == NEW and record.installed_at
    assert update.main(["record", REPOSITORY, "main", ""]) == 0  # commit unknown
    assert update.installation().commit is None  # type: ignore[union-attr]
    assert update.main(["nonsense"]) == 2


# --- Asking GitHub ----------------------------------------------------------------------------


async def test_check_finds_the_changes() -> None:
    seen: list[httpx.Request] = []
    async with github({COMPARE: compare_answer()}, seen) as client:
        result = await update.check(installed(), client)
    assert result.available and result.latest == NEW
    assert [change.title for change in result.changes] == [
        "feat: memory that improves",
        "fix: faster search",  # merge commits are left out
    ]
    assert seen[0].headers["user-agent"] == "Kreafinity"


@pytest.mark.parametrize("status", ["identical", "behind"])
async def test_check_up_to_date(status: str) -> None:
    async with github({COMPARE: compare_answer(status)}) as client:
        result = await update.check(installed(), client)
    assert not result.available and result.latest == OLD and result.changes == []


async def test_check_with_an_unknown_version() -> None:
    """Installed before versions were recorded: what was published since."""
    seen: list[httpx.Request] = []
    newest_first = [commit(NEW, "fix: faster search"), commit(MIDDLE, "feat: memory")]
    routes = {"/repos/LAWD-CREATION/kreafinity-OS/commits": newest_first}
    async with github(routes, seen) as client:
        result = await update.check(installed(None), client)
    assert result.available and result.latest == NEW
    assert [change.title for change in result.changes] == ["feat: memory", "fix: faster search"]
    assert seen[0].url.params["since"] == "2026-11-01T00:00:00+00:00"
    assert seen[0].url.params["sha"] == "main"


async def test_check_errors() -> None:
    async with github({COMPARE: 403}) as client:
        with pytest.raises(update.UpdateError, match="HTTP 403: API rate limit") as error:
            await update.check(installed(), client)
    assert error.value.code == "network"
    with pytest.raises(update.UpdateError) as error:
        await update.check(update.Installation("https://git.example.org/k/os", "main"))
    assert error.value.code == "unsupported"


# --- The daemon's manager ---------------------------------------------------------------------


class Manager:
    def __init__(self, routes: dict[str, Any], *, enabled: bool = True) -> None:
        self.events: list[dict[str, Any]] = []
        self.notifications: list[tuple[str, str]] = []
        self.launched: list[str] = []
        self.client = github(routes)

        async def notify(summary: str, body: str) -> bool:
            self.notifications.append((summary, body))
            return True

        self.manager = update.UpdateManager(
            publish=self.events.append,
            enabled=lambda: enabled,
            notify=notify,
            launcher=self.launched.append,
            client=self.client,
        )


async def test_manager_check_notifies_once() -> None:
    installed()
    fake = Manager({COMPARE: compare_answer()})
    info = await fake.manager.check()
    assert info["available"] and info["latest"] == NEW and len(info["changes"]) == 2
    assert info["current"]["commit"] == OLD and info["supported"]
    assert [event["type"] for event in fake.events] == ["update_available"]
    assert fake.notifications == [
        (
            "Mise à jour de Kreafinity disponible",
            "2 nouveautés. Ouvrez Kreafinity pour l'installer.",
        )
    ]
    await fake.manager.check()  # same version: no new message
    assert len(fake.events) == 1 and len(fake.notifications) == 1
    await fake.client.aclose()


async def test_manager_records_check_errors() -> None:
    installed()
    fake = Manager({COMPARE: 403})
    with pytest.raises(update.UpdateError):
        await fake.manager.check()
    assert "rate limit" in (fake.manager.info()["error"] or "")
    await fake.client.aclose()


async def test_manager_start() -> None:
    fake = Manager({COMPARE: compare_answer()})
    with pytest.raises(update.UpdateError) as error:
        fake.manager.start()
    assert error.value.code == "unsupported"
    installed()
    with pytest.raises(update.UpdateError) as error:
        fake.manager.start()  # nothing found yet
    assert error.value.code == "up_to_date"

    await fake.manager.check()
    info = fake.manager.start()
    assert fake.launched == [NEW]
    assert info["status"]["state"] == "running" and info["status"]["target"] == NEW
    assert fake.events[-1] == {"type": "update_started", "target": NEW}
    with pytest.raises(update.UpdateError) as error:
        fake.manager.start()
    assert error.value.code == "busy"

    # An installation that never finished does not block forever.
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    update._write_json(update.status_path(), {"state": "running", "target": NEW, "updated_at": old})
    assert fake.manager.info()["status"]["state"] == "failed"
    fake.manager.start()
    assert fake.launched == [NEW, NEW]
    await fake.client.aclose()


async def test_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    installed()
    calls: list[str] = []
    fake = Manager({COMPARE: compare_answer()})

    async def counting_check() -> dict[str, Any]:
        calls.append("check")
        update.save_check(update.UpdateCheck(checked_at=update._now()))  # now fresh
        return {}

    monkeypatch.setattr(fake.manager, "check", counting_check)
    task = asyncio.create_task(fake.manager.watch(first_delay=0, period=0.01))
    await asyncio.sleep(0.2)
    task.cancel()
    assert calls == ["check"]  # once a day, not at every wake-up

    disabled = Manager({COMPARE: compare_answer()}, enabled=False)
    monkeypatch.setattr(disabled.manager, "check", counting_check)
    update.save_check(update.UpdateCheck())  # stale again
    task = asyncio.create_task(disabled.manager.watch(first_delay=0, period=0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    assert calls == ["check"]  # the user turned automatic checks off
    await fake.client.aclose()
    await disabled.client.aclose()


# --- Installing -------------------------------------------------------------------------------


def fake_runner(failing: str | None = None) -> tuple[list[list[str]], Callable[..., Any]]:
    commands: list[list[str]] = []

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        failed = failing is not None and failing in " ".join(command)
        return subprocess.CompletedProcess(
            command, 1 if failed else 0, "", "boom" if failed else ""
        )

    return commands, run


def test_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    installed()
    restarted: list[bool] = []
    monkeypatch.setattr(update, "restart", lambda **_: restarted.append(True))
    commands, run = fake_runner()
    lines: list[str] = []

    update.apply(NEW, run=run, log=lines.append)

    assert commands[0][:3] == [sys.executable, "-m", "pip"]
    assert commands[0][-3:] == [
        "--force-reinstall",
        "--no-deps",
        f"{REPOSITORY}/archive/{NEW}.tar.gz",
    ]
    assert commands[1][:2] == [sys.executable, "-c"]  # lists the new version's dependencies
    assert commands[2] == [sys.executable, "-m", "kreafinity.cli", "setup", "--refresh"]
    assert update.installation().commit == NEW  # type: ignore[union-attr]
    assert update.read_status().state == "done" and restarted == [True]
    assert len(lines) == 3


def test_apply_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    installed()
    monkeypatch.setattr(update, "restart", lambda **_: pytest.fail("no restart"))
    _, run = fake_runner(failing="pip")
    with pytest.raises(update.UpdateError, match="installation failed: boom"):
        update.apply(NEW, run=run)
    status = update.read_status()
    assert status.state == "failed" and "boom" in (status.message or "")
    assert update.installation().commit == OLD  # type: ignore[union-attr]
    with pytest.raises(update.UpdateError, match="invalid version"):
        update.apply("$(reboot)", run=run)


def test_apply_needs_an_installation() -> None:
    with pytest.raises(update.UpdateError) as error:
        update.apply(NEW, run=fake_runner()[1])
    assert error.value.code == "unsupported"


def test_restart_does_nothing_outside_an_installation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "prefix", sys.base_prefix)
    commands, run = fake_runner()
    update.restart(run=run)
    assert commands == []


# --- API, settings, command line --------------------------------------------------------------


def make_client(home: Path, routes: dict[str, Any], launched: list[str]) -> TestClient:
    def service() -> AssistantService:
        return AssistantService(
            ConversationStore(),
            context=ToolContext.create(home=home),
            notifier=None,
            update_launcher=launched.append,
            update_client=github(routes),
        )

    app = create_app(service, TOKEN, port=27494, allowed_hosts=("testserver",))
    return TestClient(app)


def test_api(home: Path) -> None:
    auth = {"Authorization": f"Bearer {TOKEN}"}
    launched: list[str] = []
    with make_client(home, {COMPARE: compare_answer()}, launched) as client:
        info = client.get("/api/update", headers=auth).json()
        assert info["supported"] is False and info["available"] is False
        assert client.post("/api/update/check", headers=auth).status_code == 400  # dev copy

        installed()
        info = client.post("/api/update/check", headers=auth).json()
        assert info["available"] and info["latest"] == NEW
        assert client.post("/api/update", headers=auth).status_code == 202
        assert launched == [NEW]
        assert client.post("/api/update", headers=auth).status_code == 409  # running
        assert client.get("/api/update").status_code == 401  # token needed

    with make_client(home, {COMPARE: 403}, []) as client:
        assert client.post("/api/update/check", headers=auth).status_code == 503


def test_check_updates_setting(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("check_updates: false\n")
    assert load_settings(config).check_updates is False
    config.write_text("check_updates: maybe\n")
    with pytest.raises(ConfigError, match="check_updates"):
        load_settings(config)


def test_update_command(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["update", "--check"])
    assert result.exit_code == 1 and "Copie de développement" in result.output

    installed()

    async def found(record: update.Installation, client: Any = None) -> update.UpdateCheck:
        return update.UpdateCheck(
            available=True,
            latest=NEW,
            changes=[update.Change(NEW, "fix: faster search")],
            checked_at=update._now(),
        )

    monkeypatch.setattr(update, "check", found)
    result = runner.invoke(cli.app, ["update", "--check"])
    assert result.exit_code == 0
    assert "Une mise à jour est disponible (1 nouveauté)" in result.output
    assert "fix: faster search" in result.output and "kreafinity update" in result.output

    applied: list[str] = []
    monkeypatch.setattr(update, "apply", lambda target, log: applied.append(target))
    result = runner.invoke(cli.app, ["update", "--yes"])
    assert result.exit_code == 0 and applied == [NEW]
    assert "Kreafinity est à jour" in result.output

    result = runner.invoke(cli.app, ["version"])
    assert "(aaaaaaa, main)" in result.output


# --- The Debian package: updates from GitHub Releases -----------------------------------------

PKG_OLD = "0.1.0~dev0+git20261123.104000.f7c0e44"
PKG_NEW = "0.1.0~dev0+git20261126.071424.6bb26d9"
DEB = f"kreafinity_{PKG_NEW}_amd64.deb"
DOWNLOAD = "https://github.com/LAWD-CREATION/kreafinity-OS/releases/download/v1"
LATEST = "/repos/LAWD-CREATION/kreafinity-OS/releases/latest"


def release_answer(assets: list[str] | None = None) -> dict[str, Any]:
    names = [DEB, f"{DEB}.sha256"] if assets is None else assets
    return {
        "tag_name": "v1",
        "assets": [{"name": n, "browser_download_url": f"{DOWNLOAD}/{n}"} for n in names],
    }


@pytest.fixture
def packaged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "package_info", lambda: {"format": "deb", "version": PKG_OLD})


def test_package_installation(packaged: None) -> None:
    record = update.installation()
    assert record == update.Installation(REPOSITORY, "main", "f7c0e44", package=True)
    assert record is not None and record.api_url() == API


async def test_package_check(packaged: None) -> None:
    record = update.installation()
    assert record is not None
    compare = "/repos/LAWD-CREATION/kreafinity-OS/compare/f7c0e44...6bb26d9"
    async with github({LATEST: release_answer(), compare: compare_answer()}) as client:
        result = await update.check(record, client)
    assert result.available and result.latest == "6bb26d9"
    assert [c.title for c in result.changes] == ["feat: memory that improves", "fix: faster search"]
    # Without the list of changes (compare refused), still available.
    async with github({LATEST: release_answer()}) as client:
        result = await update.check(record, client)
    assert result.available and result.changes == []
    # A release without the checksum is not used.
    async with github({LATEST: release_answer([DEB])}) as client:
        with pytest.raises(update.UpdateError, match="no Kreafinity package"):
            await update.check(record, client)


async def test_package_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "package_info", lambda: {"format": "deb", "version": PKG_NEW})
    record = update.installation()
    assert record is not None
    async with github({LATEST: release_answer()}) as client:
        result = await update.check(record, client)
    assert not result.available


def downloads(content: bytes, checksum: str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == LATEST:
            return httpx.Response(200, json=release_answer())
        if request.url.path.endswith(".deb"):
            return httpx.Response(200, content=content)
        if request.url.path.endswith(".sha256"):
            return httpx.Response(200, text=f"{checksum}  {DEB}\n")
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_apply_package(packaged: None, monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib

    content = b"!<arch>\nfake package"
    restarted: list[bool] = []
    monkeypatch.setattr(update, "restart", lambda **_: restarted.append(True))
    commands, run = fake_runner()
    record = update.installation()
    assert record is not None
    client = downloads(content, hashlib.sha256(content).hexdigest())
    update.apply_package(record, "6bb26d9", run=run, client=client)
    (command,) = commands
    assert command[:3] == ["pkexec", "apt-get", "install"]
    assert command[-1].endswith(DEB) and "--allow-downgrades" in command
    assert update.read_status().state == "done" and restarted == [True]
    assert not list((update.data_dir() / "updates").glob("*.deb"))  # cleaned after install

    # A package that does not match its checksum is never installed.
    commands.clear()
    with pytest.raises(update.UpdateError, match="checksum"):
        update.apply_package(record, "6bb26d9", run=run, client=downloads(content, "0" * 64))
    assert commands == [] and update.read_status().state == "failed"

    # The password dialog cancelled.
    def refused(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 126, "", "")

    client = downloads(content, hashlib.sha256(content).hexdigest())
    with pytest.raises(update.UpdateError, match="not granted"):
        update.apply_package(record, "6bb26d9", run=refused, client=client)


def test_restart_of_the_package(packaged: None) -> None:
    commands, run = fake_runner()
    update.restart(run=run)
    assert commands[0] == ["systemctl", "--user", "try-restart", "kreafinity.service"]
