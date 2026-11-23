"""Finding and starting the assistant's browser, with its own profile.

The assistant never touches the user's usual browser profile: its window has
no saved passwords, history or open sessions. It runs with a debugging
endpoint on 127.0.0.1 so Kreafinity can drive it, and keeps running when
Kreafinity restarts (Kreafinity reconnects to it).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from kreafinity.browser.protocol import BrowserError
from kreafinity.paths import data_dir

#: Browsers tried in this order (the user's default kind first), by protocol.
FIREFOX = ("firefox-esr", "firefox", "librewolf")
CHROMIUM = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "brave-browser",
    "microsoft-edge",
)
START_TIMEOUT = 60.0  # a first start (new profile) on a slow or busy computer

# Firefox settings for the assistant's profile: no first-run pages, never
# remember passwords. The remote agent's "recommended" automation settings are
# off, so safe browsing and other protections stay as in a normal Firefox.
FIREFOX_PREFS = {
    "remote.prefs.recommended": False,
    "remote.active-protocols": 1,
    "browser.shell.checkDefaultBrowser": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "browser.aboutwelcome.enabled": False,
    "startup.homepage_welcome_url": "",
    "startup.homepage_welcome_url.additional": "",
    "datareporting.policy.dataSubmissionPolicyBypassNotification": True,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    "browser.tabs.warnOnClose": False,
    "signon.rememberSignons": False,
    "browser.formfill.enable": False,
    "extensions.formautofill.creditCards.enabled": False,
    "intl.accept_languages": "fr-FR, fr, en-US, en",
}


@dataclass(frozen=True, slots=True)
class BrowserProgram:
    """A browser installed on the machine.

    Attributes:
        kind: ``firefox`` (WebDriver BiDi) or ``chromium`` (Chrome DevTools).
        path: The program.
        profile: The assistant's own profile folder for it.
    """

    kind: str
    path: str
    profile: Path


def _kind_of(name: str) -> str:
    return "firefox" if any(word in name for word in ("firefox", "librewolf")) else "chromium"


def _is_snap(path: str) -> bool:
    """Ubuntu's firefox and chromium are snaps (sometimes behind a small script)."""
    real = os.path.realpath(path)
    if real.startswith("/snap/"):
        return True
    try:
        with open(real, "rb") as program:
            head = program.read(4096)
    except OSError:
        return False
    return head.startswith(b"#!") and b"/snap/bin/" in head


def profile_dir(path: str, kind: str) -> Path:
    """The assistant's profile for this browser.

    A snap browser can only write in its own folder (``~/snap/<name>/common``).
    """
    if _is_snap(path):
        name = "chromium" if kind == "chromium" else "firefox"
        return Path.home() / "snap" / name / "common" / "kreafinity-browser"
    return data_dir() / "browser" / kind


def _default_kind() -> str | None:
    """The kind of the user's default browser (xdg-settings), if known."""
    if not shutil.which("xdg-settings"):
        return None
    try:
        result = subprocess.run(
            ["xdg-settings", "get", "default-web-browser"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    name = result.stdout.strip().lower()
    if not name:
        return None
    return _kind_of(name)


def find_browser() -> BrowserProgram:
    """The browser the assistant uses.

    ``KREAFINITY_BROWSER`` can name a program (path or name); otherwise the
    first installed browser, of the same kind as the user's default one.

    Raises:
        BrowserError: No supported browser is installed.
    """
    wanted = os.environ.get("KREAFINITY_BROWSER", "").strip()
    if wanted:
        path = shutil.which(wanted) or (wanted if os.access(wanted, os.X_OK) else None)
        if not path:
            raise BrowserError(f"KREAFINITY_BROWSER: program not found: {wanted}")
        kind = _kind_of(Path(path).name.lower())
        return BrowserProgram(kind, path, profile_dir(path, kind))
    order = (*FIREFOX, *CHROMIUM) if _default_kind() != "chromium" else (*CHROMIUM, *FIREFOX)
    for name in order:
        path = shutil.which(name)
        if path:
            kind = _kind_of(name)
            return BrowserProgram(kind, path, profile_dir(path, kind))
    raise BrowserError("no supported browser found (Firefox, Chromium or Google Chrome)")


def endpoint(program: BrowserProgram) -> str | None:
    """WebSocket address of the running assistant's browser, from its profile."""
    try:
        if program.kind == "firefox":
            info = json.loads((program.profile / "WebDriverBiDiServer.json").read_text())
            return f"ws://{info['ws_host']}:{int(info['ws_port'])}/session"
        port, path = (program.profile / "DevToolsActivePort").read_text().split("\n")[:2]
        return f"ws://127.0.0.1:{int(port)}{path.strip()}"
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_firefox_prefs(profile: Path) -> None:
    lines = [f"user_pref({json.dumps(k)}, {json.dumps(v)});" for k, v in FIREFOX_PREFS.items()]
    (profile / "user.js").write_text("\n".join(lines) + "\n", encoding="utf-8")


def stop(program: BrowserProgram) -> None:
    """Close the assistant's Firefox (found by its profile lock), politely then by force."""
    try:
        target = os.readlink(program.profile / "lock")  # "127.0.0.1:+1234"
        pid = int(target.rsplit("+", 1)[1])
    except (OSError, ValueError, IndexError):
        return
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return
    if str(program.profile).encode() not in cmdline:
        return  # not the assistant's browser (an old lock)
    for sig, wait in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 3.0)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not Path(f"/proc/{pid}").exists():
                return
            time.sleep(0.2)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def launch(program: BrowserProgram, url: str = "about:blank") -> str:
    """Start the browser with the assistant's profile and return its endpoint.

    Raises:
        BrowserError: No graphical session, or the browser did not start.
    """
    # Without a window, for tests and development (KREAFINITY_BROWSER_HEADLESS=1).
    headless = os.environ.get("KREAFINITY_BROWSER_HEADLESS") == "1"
    if not headless and not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        raise BrowserError("no graphical session (DISPLAY / WAYLAND_DISPLAY)")
    program.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    if program.kind == "firefox":
        stale = program.profile / "WebDriverBiDiServer.json"
        _write_firefox_prefs(program.profile)
        arguments = [
            "--profile",
            str(program.profile),
            "--new-instance",
            "--remote-debugging-port",
            str(_free_port()),  # Firefox does not accept 0 (a port of its choice)
            url,
        ]
    else:
        stale = program.profile / "DevToolsActivePort"
        arguments = [
            f"--user-data-dir={program.profile}",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=AutofillServerCommunication",
            url,
        ]
    if headless:
        arguments.insert(0, "--headless=new" if program.kind == "chromium" else "--headless")
    stale.unlink(missing_ok=True)
    log = open(program.profile / "kreafinity-browser.log", "ab")  # noqa: SIM115 (kept by the child)
    try:
        process = subprocess.Popen(
            [program.path, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,  # stays open when Kreafinity restarts
        )
    except OSError as exc:
        raise BrowserError(f"cannot start {program.path}: {exc}") from exc
    finally:
        log.close()
    # Collect its exit status whenever the user closes it (no zombie process).
    threading.Thread(target=process.wait, name="browser-reaper", daemon=True).start()
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        address = endpoint(program)
        if address:
            return address
        if process.poll() is not None:
            raise BrowserError(
                f"{Path(program.path).name} stopped at start (code {process.returncode}); "
                f"see {program.profile / 'kreafinity-browser.log'}"
            )
        time.sleep(0.2)
    raise BrowserError(f"{Path(program.path).name} did not start in {START_TIMEOUT:.0f} s")
