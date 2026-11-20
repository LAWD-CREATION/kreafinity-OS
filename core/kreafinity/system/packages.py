"""System packages (APT: Debian, Ubuntu and derivatives).

Installing needs administrator rights: the command runs through ``pkexec``,
so polkit asks the user for their password in a dialog of the system itself.
Kreafinity never uses ``sudo`` and never sees the password.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

from kreafinity.system.process import Completed, run_captured

#: Valid Debian package name (policy §5.6.1), optionally with an architecture.
PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]+(:[a-z0-9-]+)?")
#: Seconds to wait when another program (updates…) is already installing packages.
LOCK_TIMEOUT = 120
#: Seconds an installation may take.
INSTALL_TIMEOUT = 1800
#: pkexec exit codes when the password dialog was dismissed or refused.
PKEXEC_REFUSED = (126, 127)
# "Inst sl (5.02-1 Ubuntu:24.04/noble [amd64])", "Inst bash [5.2-1] (5.2-2 …)"
_INST_LINE = re.compile(r"^Inst (\S+)(?: \[[^\]]*\])? \((\S+)", re.MULTILINE)


class PackageError(Exception):
    """Packages cannot be installed; the message explains why."""


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """What installing some packages will do.

    Attributes:
        packages: Requested packages that will be installed, with their version.
        dependencies: Other packages installed along with them, with their version.
        already_installed: Requested packages that are already there.
        command: The exact command that will run (``pkexec apt-get install …``).
    """

    packages: tuple[tuple[str, str], ...]
    dependencies: tuple[tuple[str, str], ...]
    already_installed: tuple[str, ...]
    command: tuple[str, ...]


def check_names(names: Sequence[str]) -> list[str]:
    """The package names, validated (nothing that could pass for an option).

    Raises:
        PackageError: A name is not a valid package name.
    """
    for name in names:
        if not PACKAGE_NAME.fullmatch(name):
            raise PackageError(f"invalid package name: {name!r}")
    return list(dict.fromkeys(names))


def plan_install(names: Sequence[str]) -> InstallPlan:
    """Simulate the installation of `names` (no rights needed) to show what it will do.

    Raises:
        PackageError: Not an APT system, pkexec missing, unknown package,
            or nothing to install.
    """
    names = check_names(names)
    apt_get = shutil.which("apt-get")
    if apt_get is None:
        raise PackageError("only APT systems (Debian, Ubuntu and derivatives) are supported")
    pkexec = shutil.which("pkexec")
    if pkexec is None:
        raise PackageError(
            "pkexec (polkit) is not installed, so administrator rights cannot be requested"
        )
    try:
        simulation = subprocess.run(
            [apt_get, "-s", "-q", "install", "--", *names],
            capture_output=True,
            text=True,
            env=_c_locale(),
            stdin=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PackageError(f"apt-get cannot simulate the installation: {exc}") from exc
    if simulation.returncode != 0:
        errors = [line for line in simulation.stderr.splitlines() if line.startswith("E:")]
        raise PackageError("; ".join(errors) or simulation.stderr.strip()[-500:])

    planned = dict(_INST_LINE.findall(simulation.stdout))
    already = tuple(name for name in names if name not in planned and installed_version(name))
    if not planned:
        raise PackageError(f"already installed: {', '.join(already) or ', '.join(names)}")
    wanted = [name for name in names if name not in already]
    return InstallPlan(
        packages=tuple((name, planned[name]) for name in wanted if name in planned),
        dependencies=tuple(item for item in planned.items() if item[0] not in names),
        already_installed=already,
        command=(
            pkexec,
            apt_get,
            "install",
            "-y",
            "-q",
            "-o",
            f"DPkg::Lock::Timeout={LOCK_TIMEOUT}",
            "--",
            *wanted,
        ),
    )


async def install(plan: InstallPlan) -> Completed:
    """Run the installation of `plan`; polkit asks the user for the password.

    Raises:
        PackageError: The password was refused or the installation failed.
    """
    try:
        result = await run_captured(plan.command, timeout=INSTALL_TIMEOUT, env=_c_locale())
    except OSError as exc:
        raise PackageError(f"cannot start the installation: {exc}") from exc
    if result.timed_out:
        raise PackageError(f"the installation took more than {INSTALL_TIMEOUT // 60} minutes")
    if result.exit_code in PKEXEC_REFUSED:
        raise PackageError(
            "administrator rights were not granted (password dialog cancelled or refused, "
            f"or no polkit agent in this session): {result.output.strip()[-300:]}"
        )
    if result.exit_code != 0:
        raise PackageError(f"apt-get failed: {result.output.strip()[-1500:]}")
    return result


def installed_version(name: str) -> str | None:
    """Installed version of package `name`, if it is installed."""
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Status}\t${Version}", name],
            capture_output=True,
            text=True,
            env=_c_locale(),
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    status, _, version = result.stdout.partition("\t")
    if status != "install ok installed":
        return None
    return version.strip() or None


def _c_locale() -> dict[str, str]:
    """The current environment, with messages in English so they can be parsed."""
    return {**os.environ, "LC_ALL": "C"}
