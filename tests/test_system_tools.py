"""Volume, brightness, shell commands and package installation.

The sound server and APT are replaced by small fake programs on the PATH;
brightness goes through a real (private) D-Bus bus, with a fake logind.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import psutil
import pytest
from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from fakes import ScriptedProvider, call, reply
from kreafinity.agent import Agent, ApprovalRequest, Conversation
from kreafinity.system import brightness
from kreafinity.tools import RiskLevel, ToolContext, ToolError, ToolRegistry, ToolResult
from kreafinity.tools.run_command import command_programs
from kreafinity.tools.system_info import collect_system_info


def tool(name: str) -> Any:
    found = ToolRegistry.load([name]).get(name)
    assert found is not None
    return found


def payload(result: ToolResult) -> Any:
    return json.loads(result.content)


def fake_program(folder: Path, name: str, code: str, **constants: str) -> None:
    """A small Python program called `name` in `folder`, with some constants."""
    path = folder / name
    header = "".join(f"{key} = {value!r}\n" for key, value in constants.items())
    path.write_text(f"#!{sys.executable}\n{header}{code}")
    path.chmod(0o755)


def test_risk_levels() -> None:
    assert tool("set_volume").risk is RiskLevel.CONFIRM
    assert tool("set_brightness").risk is RiskLevel.CONFIRM
    assert tool("run_command").risk is RiskLevel.DANGER
    assert tool("install_package").risk is RiskLevel.DANGER


# --- Volume -----------------------------------------------------------------------------

SOUND_SERVER = """
import json, pathlib, sys
state_file = pathlib.Path(STATE_FILE)
state = json.loads(state_file.read_text())
command = sys.argv[1:]
with open(LOG_FILE, "a") as log:
    log.write(" ".join([pathlib.Path(sys.argv[0]).name, *command]) + "\\n")
"""
WPCTL = (
    SOUND_SERVER
    + """
if command == ["get-volume", "@DEFAULT_AUDIO_SINK@"]:
    print(f"Volume: {state['percent'] / 100:.2f}" + (" [MUTED]" if state["muted"] else ""))
elif command[:2] == ["set-volume", "@DEFAULT_AUDIO_SINK@"]:
    state["percent"] = round(float(command[2]) * 100)
elif command[:2] == ["set-mute", "@DEFAULT_AUDIO_SINK@"]:
    state["muted"] = command[2] == "1"
else:
    sys.exit("unexpected")
state_file.write_text(json.dumps(state))
"""
)
PACTL = (
    SOUND_SERVER
    + """
if command == ["get-sink-volume", "@DEFAULT_SINK@"]:
    level = state["percent"]
    print(f"Volume: front-left: 26214 /  {level}% / -23.88 dB,   "
          f"front-right: 26214 /  {level}% / -23.88 dB\\n        balance 0.00")
elif command == ["get-sink-mute", "@DEFAULT_SINK@"]:
    print("Mute: yes" if state["muted"] else "Mute: no")
elif command[:2] == ["set-sink-volume", "@DEFAULT_SINK@"]:
    state["percent"] = int(command[2].rstrip("%"))
elif command[:2] == ["set-sink-mute", "@DEFAULT_SINK@"]:
    state["muted"] = command[2] == "1"
else:
    sys.exit("unexpected")
state_file.write_text(json.dumps(state))
"""
)


class SoundServer:
    def __init__(self, state: Path, log: Path) -> None:
        self.state_file = state
        self.log = log

    @property
    def state(self) -> dict[str, Any]:
        return json.loads(self.state_file.read_text())  # type: ignore[no-any-return]

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


@pytest.fixture(params=["wpctl", "pactl"])
def sound(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SoundServer:
    """A fake PipeWire (wpctl) or PulseAudio (pactl), at 40 %, not muted."""
    folder = tmp_path / "sound-bin"
    folder.mkdir()
    server = SoundServer(tmp_path / "sound.json", tmp_path / "sound.log")
    server.state_file.write_text(json.dumps({"percent": 40, "muted": False}))
    code = {"wpctl": WPCTL, "pactl": PACTL}[request.param]
    fake_program(
        folder, request.param, code, STATE_FILE=str(server.state_file), LOG_FILE=str(server.log)
    )
    if request.param == "pactl":  # PipeWire's wpctl installed but not running: skipped
        fake_program(folder, "wpctl", "import sys\nsys.exit('Could not connect to PipeWire')\n")
    monkeypatch.setenv("PATH", str(folder))  # never the real sound server
    return server


@pytest.mark.parametrize(
    ("arguments", "summary", "expected"),
    [
        ({"level": 55}, "Régler le volume à 55 % (actuellement 40 %)", (55, False)),
        ({"change": -15}, "Régler le volume à 25 % (actuellement 40 %)", (25, False)),
        ({"change": 90}, "Régler le volume à 100 % (actuellement 40 %)", (100, False)),
        ({"mute": True}, "Couper le son", (40, True)),
    ],
)
async def test_set_volume(
    sound: SoundServer,
    context: ToolContext,
    arguments: dict[str, Any],
    summary: str,
    expected: tuple[int, bool],
) -> None:
    preview = await tool("set_volume").preview(arguments, context)
    assert preview.summary == summary
    result = await tool("set_volume").run(arguments, context)
    assert (sound.state["percent"], sound.state["muted"]) == expected
    assert payload(result) == {
        "volume_percent": expected[0],
        "muted": expected[1],
        "before": 40,
    }


async def test_unmute(sound: SoundServer, context: ToolContext) -> None:
    sound.state_file.write_text(json.dumps({"percent": 30, "muted": True}))
    preview = await tool("set_volume").preview({"mute": False, "level": 20}, context)
    assert preview.summary == "Rétablir le son et régler le volume à 20 %"
    await tool("set_volume").run({"mute": False, "level": 20}, context)
    assert sound.state == {"percent": 20, "muted": False}
    # Sound already on: only the level is mentioned.
    preview = await tool("set_volume").preview({"mute": False, "level": 50}, context)
    assert preview.summary == "Régler le volume à 50 % (actuellement 20 %)"


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"level": 40}, "already at 40%"),
        ({"mute": False}, "already on"),
        ({"level": 10, "change": 5}, "not both"),
        ({"mute": True, "level": 10}, "only mute=true"),
    ],
)
async def test_set_volume_errors(
    sound: SoundServer, context: ToolContext, arguments: dict[str, Any], error: str
) -> None:
    with pytest.raises(ToolError, match=error):
        await tool("set_volume").preview(arguments, context)
    assert not any(" set-" in line for line in sound.calls())


async def test_no_sound_server(
    tmp_path: Path, context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ToolError, match="no sound server"):
        await tool("set_volume").preview({"level": 10}, context)
    assert collect_system_info()["sound"] is None


def test_system_info_reports_the_volume(sound: SoundServer) -> None:
    assert collect_system_info()["sound"] == {"volume_percent": 40, "muted": False}


# --- Brightness (logind over a private D-Bus bus) ----------------------------------------

SESSION = "/org/freedesktop/login1/session/_32"


class FakeLogind:
    """The part of systemd-logind used for brightness, writing to a fake sysfs."""

    def __init__(self, backlight_dir: Path) -> None:
        self.backlight_dir = backlight_dir
        self.display = ("2", SESSION)
        self.refuse: str | None = None
        self.calls: list[tuple[str, list[Any]]] = []
        self.bus: MessageBus | None = None

    async def start(self, address: str) -> None:
        self.bus = await MessageBus(bus_address=address).connect()
        await self.bus.request_name("org.freedesktop.login1")
        self.bus.add_message_handler(self.handle)

    async def stop(self) -> None:
        if self.bus is not None:
            self.bus.disconnect()
            await self.bus.wait_for_disconnect()  # type: ignore[no-untyped-call]

    def handle(self, message: Message) -> Message | None:
        if message.message_type is not MessageType.METHOD_CALL:
            return None
        if message.member == "Get" and message.path == "/org/freedesktop/login1/user/self":
            return Message.new_method_return(message, "v", [Variant("(so)", list(self.display))])
        if message.member == "SetBrightness":
            self.calls.append((str(message.path), list(message.body)))
            if self.refuse:
                return Message.new_error(
                    message, "org.freedesktop.DBus.Error.AccessDenied", self.refuse
                )
            _, name, value = message.body
            for file in ("brightness", "actual_brightness"):
                (self.backlight_dir / name / file).write_text(f"{value}\n")
            return Message.new_method_return(message)
        return Message.new_error(message, "org.freedesktop.DBus.Error.UnknownMethod", "unknown")


def backlight(folder: Path, name: str, kind: str, current: int, maximum: int) -> None:
    device = folder / name
    device.mkdir(parents=True)
    for file, value in {
        "type": kind,
        "brightness": current,
        "actual_brightness": current,
        "max_brightness": maximum,
    }.items():
        (device / file).write_text(f"{value}\n")


@pytest.fixture
def backlight_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A laptop screen at 50 %: the firmware device is preferred over the raw one."""
    folder = tmp_path / "sys-backlight"
    backlight(folder, "intel_backlight", "raw", 9600, 19200)
    backlight(folder, "acpi_video0", "firmware", 50, 100)
    monkeypatch.setattr(brightness, "BACKLIGHT_DIR", folder)
    return folder


@pytest.fixture
async def logind(backlight_dir: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeLogind]:
    if shutil.which("dbus-daemon") is None:
        pytest.skip("dbus-daemon is not installed")
    daemon = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert daemon.stdout is not None
    address = daemon.stdout.readline().strip()
    monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", address)  # our "system bus"
    fake = FakeLogind(backlight_dir)
    await fake.start(address)
    try:
        yield fake
    finally:
        await fake.stop()
        daemon.terminate()
        daemon.wait()
        daemon.stdout.close()


@pytest.mark.parametrize(
    ("arguments", "percent"),
    [({"level": 80}, 80), ({"change": -20}, 30), ({"change": -90}, 5), ({"change": 70}, 100)],
)
async def test_set_brightness(
    logind: FakeLogind,
    backlight_dir: Path,
    context: ToolContext,
    arguments: dict[str, Any],
    percent: int,
) -> None:
    preview = await tool("set_brightness").preview(arguments, context)
    assert preview.summary == f"Régler la luminosité de l'écran à {percent} % (actuellement 50 %)"
    result = await tool("set_brightness").run(arguments, context)
    assert payload(result) == {"brightness_percent": percent, "before": 50}
    # Changed in the user's graphical session, on the preferred device.
    assert logind.calls == [(SESSION, ["backlight", "acpi_video0", percent])]
    assert collect_system_info()["screen_brightness_percent"] == percent


async def test_brightness_without_graphical_session(
    logind: FakeLogind, context: ToolContext
) -> None:
    logind.display = ("", "/")
    await tool("set_brightness").run({"level": 60}, context)
    assert logind.calls[0][0] == "/org/freedesktop/login1/session/auto"


async def test_brightness_refused_by_logind(logind: FakeLogind, context: ToolContext) -> None:
    logind.refuse = "Only owner of session may change brightness."
    with pytest.raises(ToolError, match="Only owner of session"):
        await tool("set_brightness").run({"level": 60}, context)


async def test_brightness_already_set(logind: FakeLogind, context: ToolContext) -> None:
    with pytest.raises(ToolError, match="already at 50%"):
        await tool("set_brightness").preview({"level": 50}, context)
    assert logind.calls == []


async def test_brightness_without_logind(
    backlight_dir: Path, tmp_path: Path, context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", f"unix:path={tmp_path}/no-bus")
    with pytest.raises(ToolError, match="cannot reach systemd-logind"):
        await tool("set_brightness").run({"level": 60}, context)


async def test_no_backlight(
    tmp_path: Path, context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(brightness, "BACKLIGHT_DIR", tmp_path / "none")
    with pytest.raises(ToolError, match="external monitors are not supported"):
        await tool("set_brightness").preview({"level": 60}, context)
    assert collect_system_info()["screen_brightness_percent"] is None


# --- Shell commands -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "program"),
    [
        ("sudo apt install vlc", "sudo"),
        ("/usr/bin/sudo -i", "sudo"),
        ("ls; rm -rf ~/Documents", "rm"),
        ("ls && echo ok || shred notes.txt", "shred"),
        ("echo $(rm notes.txt)", "rm"),
        ("echo `rm notes.txt`", "rm"),
        ("bash -c 'rm -rf ~/Documents'", "rm"),
        ("sh -c rm", "rm"),
        ("find . -name '*.tmp' -exec rm {} \\;", "rm"),
        ("ls | xargs -0 rm", "rm"),
        ("nice -n 5 rm notes.txt", "rm"),
        ("timeout 5 unlink notes.txt", "unlink"),
        ("env LANG=C pkexec apt install vlc", "pkexec"),
        ("FOO=1 su -c id", "su"),
        ("if true; then rmdir x; fi", "rmdir"),
    ],
)
def test_forbidden_programs_are_seen(command: str, program: str) -> None:
    assert program in command_programs(command)


@pytest.mark.parametrize(
    "command",
    ["grep rm notes.txt", "echo sudo", "du -sh ~/Téléchargements | sort -h | head -5", "ls -la"],
)
async def test_harmless_commands_are_allowed(command: str, context: ToolContext) -> None:
    preview = await tool("run_command").preview({"command": command}, context)
    assert preview.details[0] == f"$ {command}"


@pytest.mark.parametrize(
    ("command", "error"),
    [
        ("sudo apt install vlc", "never run with administrator rights"),
        ("ls ; rm -rf ~/Documents", "use move_to_trash"),
        ("echo 'unbalanced", "Cannot read this command line"),
    ],
)
async def test_refused_commands(command: str, error: str, context: ToolContext) -> None:
    with pytest.raises(ToolError, match=error):
        await tool("run_command").preview({"command": command}, context)


async def test_command_preview(context: ToolContext, home: Path) -> None:
    preview = await tool("run_command").preview(
        {"command": "du -sh *", "folder": "~/Documents", "timeout_seconds": 30}, context
    )
    assert preview.summary == "Exécuter cette commande avec vos droits"
    assert preview.details == (
        "$ du -sh *",
        f"Dans le dossier : {home / 'Documents'}",
        "Arrêtée si elle dure plus de 30 s",
    )


async def test_command_folder_stays_in_the_sandbox(context: ToolContext) -> None:
    for folder in ("/etc", "~/.ssh"):
        with pytest.raises(ToolError, match="Access denied"):
            await tool("run_command").preview({"command": "ls", "folder": folder}, context)


async def test_run_command(
    context: ToolContext, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    command = 'pwd; echo "key=${ANTHROPIC_API_KEY:-none}"; cat; echo oops >&2; exit 3'
    result = await tool("run_command").run({"command": command, "folder": "~/Documents"}, context)
    data = payload(result)
    assert result.is_error and data["exit_code"] == 3
    # Runs in the folder, gets no input (cat returns at once), API keys hidden.
    assert data["output"].splitlines() == [str(home / "Documents"), "key=none", "oops"]


async def test_long_output_is_shortened(context: ToolContext) -> None:
    data = payload(await tool("run_command").run({"command": "seq 1 100000"}, context))
    assert data["exit_code"] == 0
    assert data["output"].startswith("1\n2\n") and data["output"].endswith("99999\n100000\n")
    assert "bytes omitted" in data["output"] and len(data["output"]) < 20_000


async def test_command_timeout_stops_the_whole_group(context: ToolContext, tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    started = time.monotonic()
    result = await tool("run_command").run(
        {"command": f"sleep 60 & echo $! > {pid_file}; wait", "timeout_seconds": 1}, context
    )
    data = payload(result)
    assert data["exit_code"] is None and "Stopped after 1 seconds" in data["note"]
    assert time.monotonic() - started < 10
    child = int(pid_file.read_text())
    assert not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE


async def test_background_program_does_not_block(context: ToolContext) -> None:
    started = time.monotonic()
    data = payload(await tool("run_command").run({"command": "(sleep 5 &); echo done"}, context))
    assert data["exit_code"] == 0 and data["output"] == "done\n"
    assert time.monotonic() - started < 4.5


async def test_cancelled_command_is_stopped(context: ToolContext, tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    task = asyncio.create_task(
        tool("run_command").run({"command": f"echo $$ > {pid_file}; exec sleep 60"}, context)
    )
    while not pid_file.exists() or not pid_file.read_text().strip():
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not psutil.pid_exists(int(pid_file.read_text()))


# --- Package installation ------------------------------------------------------------------

APT = """
import json, os, pathlib, sys
state_file = pathlib.Path(STATE_FILE)
state = json.loads(state_file.read_text())
name = pathlib.Path(sys.argv[0]).name
with open(LOG_FILE, "a") as log:
    log.write(json.dumps([name, *sys.argv[1:], os.environ.get("FAKE_ROOT", "")]) + "\\n")
"""
APT_GET = (
    APT
    + """
arguments = sys.argv[1:]
names = arguments[arguments.index("--") + 1:]
for package in names:
    if package not in state["available"]:
        sys.exit(print(f"E: Unable to locate package {package}", file=sys.stderr) or 100)
wanted = []
for package in names:
    for item in [package, *state["depends"].get(package, [])]:
        if item not in state["installed"] and item not in wanted:
            wanted.append(item)
if "-s" in arguments:
    for item in wanted:
        print(f"Inst {item} ({state['available'][item]} Ubuntu:24.04/noble [amd64])")
    sys.exit(0)
if os.environ.get("FAKE_ROOT") != "1":
    sys.exit(print("E: Could not open lock file - are you root?", file=sys.stderr) or 100)
if state.get("broken"):
    sys.exit(print("E: dpkg was interrupted", file=sys.stderr) or 100)
for item in wanted:
    print(f"Setting up {item} ...")
    state["installed"][item] = state["available"][item]
state_file.write_text(json.dumps(state))
"""
)
DPKG_QUERY = (
    APT
    + """
package = sys.argv[-1]
if package not in state["installed"]:
    sys.exit(print(f"dpkg-query: no packages found matching {package}", file=sys.stderr) or 1)
print(f"install ok installed\\t{state['installed'][package]}", end="")
"""
)
PKEXEC = (
    APT
    + """
answer = os.environ.get("FAKE_PKEXEC", "allow")
if answer == "deny":
    print("Error executing command as another user: Not authorized", file=sys.stderr)
    sys.exit(127)
if answer == "dismiss":
    sys.exit(126)
os.execve(sys.argv[1], sys.argv[1:], {**os.environ, "FAKE_ROOT": "1"})
"""
)


class Apt:
    def __init__(self, folder: Path, state: Path, log: Path) -> None:
        self.folder = folder
        self.state_file = state
        self.log = log

    @property
    def installed(self) -> dict[str, str]:
        return json.loads(self.state_file.read_text())["installed"]  # type: ignore[no-any-return]

    def calls(self) -> list[list[str]]:
        return (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )


@pytest.fixture
def apt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Apt]:
    """A fake APT system with polkit: bash installed, vlc available."""
    folder = tmp_path / "apt-bin"
    folder.mkdir()
    fake = Apt(folder, tmp_path / "apt.json", tmp_path / "apt.log")
    fake.state_file.write_text(
        json.dumps(
            {
                "installed": {"bash": "5.2.21"},
                "available": {
                    "bash": "5.2.21",
                    "vlc": "3.0.20-3",
                    "libvlc5": "3.0.20-3",
                    "vlc-data": "3.0.20-3",
                    "gimp": "2.10.36",
                },
                "depends": {"vlc": ["libvlc5", "vlc-data"]},
            }
        )
    )
    for name, code in {"apt-get": APT_GET, "dpkg-query": DPKG_QUERY, "pkexec": PKEXEC}.items():
        fake_program(folder, name, code, STATE_FILE=str(fake.state_file), LOG_FILE=str(fake.log))
    monkeypatch.setenv("PATH", str(folder))
    yield fake


async def test_install_preview(apt: Apt, context: ToolContext) -> None:
    preview = await tool("install_package").preview({"packages": ["vlc"]}, context)
    assert preview.summary == (
        "Installer 1 logiciel (le système demandera votre mot de passe administrateur)"
    )
    assert preview.details == (
        "vlc 3.0.20-3",
        "+ 2 paquets nécessaires : libvlc5, vlc-data",
        f"Commande : {apt.folder}/pkexec {apt.folder}/apt-get install -y -q "
        "-o DPkg::Lock::Timeout=120 -- vlc",
    )
    assert all("-s" in call[1:] for call in apt.calls() if call[0] == "apt-get")  # simulated


async def test_install(apt: Apt, context: ToolContext) -> None:
    result = await tool("install_package").run({"packages": ["bash", "vlc"]}, context)
    assert payload(result) == {
        "installed": {"vlc": "3.0.20-3"},
        "dependencies_installed": 2,
        "already_installed": ["bash"],
        "duration_seconds": payload(result)["duration_seconds"],
    }
    assert apt.installed == {
        "bash": "5.2.21",
        "vlc": "3.0.20-3",
        "libvlc5": "3.0.20-3",
        "vlc-data": "3.0.20-3",
    }
    # Root only through pkexec, for apt-get, and only for what is missing.
    root_calls = [call for call in apt.calls() if call[-1] == "1"]
    assert root_calls == [
        [
            "apt-get",
            "install",
            "-y",
            "-q",
            "-o",
            "DPkg::Lock::Timeout=120",
            "--",
            "vlc",
            "1",
        ]
    ]


@pytest.mark.parametrize(("answer", "code"), [("deny", "Not authorized"), ("dismiss", "")])
async def test_install_refused_password(
    apt: Apt, context: ToolContext, monkeypatch: pytest.MonkeyPatch, answer: str, code: str
) -> None:
    monkeypatch.setenv("FAKE_PKEXEC", answer)
    with pytest.raises(ToolError, match="administrator rights were not granted") as error:
        await tool("install_package").run({"packages": ["gimp"]}, context)
    assert code in str(error.value)
    assert "gimp" not in apt.installed


@pytest.mark.parametrize(
    ("packages", "error"),
    [
        (["vlc-pro"], "Unable to locate package vlc-pro"),
        (["bash"], "already installed: bash"),
        (["-o", "vlc"], "invalid package name: '-o'"),
        (["vlc; reboot"], "invalid package name"),
        (["VLC"], "invalid package name"),
    ],
)
async def test_install_errors(
    apt: Apt, context: ToolContext, packages: list[str], error: str
) -> None:
    with pytest.raises(ToolError, match=error):
        await tool("install_package").preview({"packages": packages}, context)
    if "invalid" in error:
        assert apt.calls() == []  # never handed to apt-get


async def test_install_failure_is_reported(apt: Apt, context: ToolContext) -> None:
    state = json.loads(apt.state_file.read_text())
    apt.state_file.write_text(json.dumps({**state, "broken": True}))
    with pytest.raises(ToolError, match="dpkg was interrupted"):
        await tool("install_package").run({"packages": ["gimp"]}, context)


async def test_install_needs_apt_and_polkit(
    apt: Apt, context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    (apt.folder / "pkexec").unlink()
    with pytest.raises(ToolError, match="pkexec"):
        await tool("install_package").preview({"packages": ["gimp"]}, context)
    monkeypatch.setenv("PATH", str(apt.folder / "nothing"))
    with pytest.raises(ToolError, match="only APT systems"):
        await tool("install_package").preview({"packages": ["gimp"]}, context)


async def test_installation_needs_explicit_approval(apt: Apt, context: ToolContext) -> None:
    """Through the agent: the exact command is shown, nothing runs if refused."""
    asked: list[ApprovalRequest] = []

    async def approver(request: ApprovalRequest) -> bool:
        asked.append(request)
        return False

    provider = ScriptedProvider(
        reply("", [call("install_package", packages=["gimp"])]), reply("Ok.")
    )
    agent = Agent(provider, ToolRegistry.load(), context, approver=approver)
    _ = [event async for event in agent.run(Conversation(), "Installe GIMP")]

    assert [request.risk for request in asked] == [RiskLevel.DANGER]
    assert any("pkexec" in line and "apt-get install" in line for line in asked[0].details)
    assert "gimp" not in apt.installed
    assert not any(call[-1] == "1" for call in apt.calls())


def test_real_dpkg_query_reports_versions() -> None:
    if shutil.which("dpkg-query") is None:
        pytest.skip("not a Debian system")
    from kreafinity.system.packages import installed_version

    assert installed_version("dpkg")
    assert installed_version("surely-not-a-package-name") is None


def test_environment_is_not_leaked() -> None:
    # The fixtures above only change PATH for the test that uses them.
    assert "apt-bin" not in os.environ["PATH"] and "sound-bin" not in os.environ["PATH"]
