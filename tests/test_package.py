"""Kreafinity installed from the Debian package: updates come from the system (APT)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kreafinity import cli, update
from kreafinity.voice import manager as voice_manager

ROOT = Path(__file__).resolve().parent.parent
DEBIAN = ROOT / "packaging" / "debian"


@pytest.fixture
def packaged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pretend Kreafinity's Python is the Debian package's (its marker next to it)."""
    monkeypatch.setattr(update.sys, "base_prefix", str(tmp_path))
    marker = tmp_path / update.PACKAGE_MARKER
    marker.write_text(json.dumps({"format": "deb", "version": "0.2.0+git20261121.abc1234"}))
    return marker


def test_not_packaged_by_default() -> None:
    assert update.package_info() is None
    assert (
        update.UpdateManager(publish=lambda event: None, enabled=lambda: True).info()["managed"]
        is None
    )


async def test_updates_come_from_the_system(packaged: Path) -> None:
    manager = update.UpdateManager(publish=lambda event: None, enabled=lambda: True)
    info = manager.info()
    assert info["managed"] == "deb" and info["supported"] is False
    with pytest.raises(update.UpdateError, match="system's package manager") as error:
        await manager.check()
    assert error.value.code == "unsupported"
    with pytest.raises(update.UpdateError, match="system's package manager"):
        manager.start()


def test_commands(packaged: Path) -> None:
    runner = CliRunner()
    assert "paquet 0.2.0+git20261121.abc1234" in runner.invoke(cli.app, ["version"]).output
    result = runner.invoke(cli.app, ["update"])
    assert result.exit_code == 1 and "sudo apt update && sudo apt upgrade" in result.output
    # « uninstall » leaves the program to APT, and --purge still erases the user's data.
    data = Path(cli.data_dir())
    data.mkdir(parents=True, exist_ok=True)
    (data / "history.db").write_text("…")
    result = runner.invoke(cli.app, ["uninstall", "--purge", "--yes"])
    assert result.exit_code == 0, result.output
    assert "sudo apt remove kreafinity" in result.output and not data.exists()


def test_voice_engines_go_to_the_user_folder_when_python_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(voice_manager.subprocess, "run", run)
    monkeypatch.setattr(voice_manager, "environment_writable", lambda: False)
    voice_manager.pip_install(["piper-tts"])
    target = commands[0][commands[0].index("--target") + 1]
    assert target.endswith("kreafinity/voice/site-packages") and commands[0][-1] == "piper-tts"
    monkeypatch.setattr(voice_manager, "environment_writable", lambda: True)
    voice_manager.pip_install(["piper-tts"])
    assert "--target" not in commands[1]  # a script installation: its own environment


# --- The package's files -----------------------------------------------------------------------


def test_package_scripts_are_valid_shell() -> None:
    for name in ("build-deb.sh", "postinst", "prerm", "postrm"):
        script = DEBIAN / name
        assert script.stat().st_mode & 0o111, f"{name} is not executable"
        assert subprocess.run(["sh", "-n", str(script)], check=False).returncode == 0
    if shutil.which("shellcheck"):
        scripts = [str(DEBIAN / name) for name in ("build-deb.sh", "postinst", "prerm", "postrm")]
        assert subprocess.run(["shellcheck", *scripts], check=False).returncode == 0


def test_control_template() -> None:
    control = (DEBIAN / "control.in").read_text()
    for field in ("Package: kreafinity", "Version: @VERSION@", "Architecture: @ARCH@", "Depends:"):
        assert field in control
    # Description: a short line, then indented paragraphs (empty lines as " .").
    lines = control.split("Description:", 1)[1].splitlines()[1:]
    assert lines and all(line.startswith(" ") for line in lines if line)


def test_maintainer_scripts_handle_the_widget_and_the_service() -> None:
    postinst = (DEBIAN / "postinst").read_text()
    assert "systemctl --global enable kreafinity.service" in postinst
    assert "/opt/kreafinity/share/plasmoids/$version" in postinst
    prerm = (DEBIAN / "prerm").read_text()
    assert "systemctl --global disable kreafinity.service" in prerm
    assert "rm -rf /usr/share/plasma/plasmoids/org.kreafinity.assistant" in prerm
    build = (DEBIAN / "build-deb.sh").read_text()
    assert "PYTHONNOUSERSITE=1" in build  # never the build machine's own packages
    assert "python3 -s -m" in build  # never the user's own packages either


# --- The APT repository ------------------------------------------------------------------------

APT = ROOT / "packaging" / "apt"
needs_repo_tools = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("apt-ftparchive", "gpg", "gpgv", "dpkg-deb")),
    reason="apt-ftparchive, gpg and dpkg-deb are needed (apt-utils, gnupg)",
)


def tiny_package(folder: Path, version: str) -> Path:
    root = folder / f"root-{version}"
    (root / "DEBIAN").mkdir(parents=True)
    (root / "DEBIAN" / "control").write_text(
        f"Package: kreafinity\nVersion: {version}\nArchitecture: amd64\n"
        "Maintainer: Kreafinity <contact@kreafinity.com>\nDescription: test\n"
    )
    package = folder / f"kreafinity_{version}_amd64.deb"
    subprocess.run(
        ["dpkg-deb", "--root-owner-group", "--build", str(root), str(package)],
        check=True,
        capture_output=True,
    )
    return package


@needs_repo_tools
def test_signed_repository(tmp_path: Path) -> None:
    # gpg-agent's socket path must stay short (Unix sockets: 108 characters).
    import tempfile

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="kgpg") as gnupg:
        env = {"PATH": "/usr/bin:/bin", "GNUPGHOME": gnupg, "HOME": str(tmp_path)}
        subprocess.run(
            [
                "gpg",
                "--batch",
                "--quiet",
                "--passphrase",
                "",
                "--quick-generate-key",
                "Kreafinity test <test@example.invalid>",
                "ed25519",
                "sign",
                "1d",
            ],
            check=True,
            env=env,
            capture_output=True,
        )
        listing = subprocess.run(
            ["gpg", "--list-secret-keys", "--with-colons"],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        fingerprint = next(
            line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr")
        )
        debs = tmp_path / "debs"
        debs.mkdir()
        tiny_package(debs, "0.1.0~dev0+git20261121.100000.aaaaaaa")
        tiny_package(debs, "0.1.0~dev0+git20261121.153000.bbbbbbb")
        site = tmp_path / "site"
        subprocess.run(
            [str(APT / "build-repo.sh"), str(debs), str(site)],
            check=True,
            capture_output=True,
            env={
                **env,
                "KREAFINITY_APT_KEY": fingerprint,
                "KREAFINITY_APT_URL": "https://apt.test",
            },
        )
        keyring = site / "kreafinity-archive-keyring.gpg"
        release = site / "dists" / "stable"
        # Both signatures check against the published key (what APT does).
        for command in (
            ["gpgv", "--keyring", str(keyring), str(release / "InRelease")],
            [
                "gpgv",
                "--keyring",
                str(keyring),
                str(release / "Release.gpg"),
                str(release / "Release"),
            ],
        ):
            assert subprocess.run(command, capture_output=True).returncode == 0

    index = (release / "main" / "binary-amd64" / "Packages").read_text()
    assert index.count("Package: kreafinity") == 2
    assert "Filename: pool/main/k/kreafinity/" in index
    text = (release / "Release").read_text()
    assert "Origin: Kreafinity" in text and "Architectures: amd64" in text
    assert "main/binary-amd64/Packages.gz" in text  # checksums of every index
    # The page and the files people need: the latest package, the source, the key.
    assert (site / "kreafinity_latest_amd64.deb").read_bytes() == (
        debs / "kreafinity_0.1.0~dev0+git20261121.153000.bbbbbbb_amd64.deb"
    ).read_bytes()
    assert "URIs: https://apt.test\n" in (site / "kreafinity.sources").read_text()
    page = (site / "index.html").read_text()
    assert "@" not in page.replace("@kreafinity", "")  # every placeholder replaced
    assert "https://apt.test/kreafinity_latest_amd64.deb" in page
    assert (site / ".nojekyll").exists()


def test_package_configures_the_repository() -> None:
    build = (DEBIAN / "build-deb.sh").read_text()
    assert "/usr/share/keyrings/kreafinity-archive-keyring.gpg" in build
    postinst = (DEBIAN / "postinst").read_text()
    # Never over a repository already set up by hand.
    assert "[ ! -e /etc/apt/sources.list.d/kreafinity.sources ]" in postinst
    assert "rm -f /etc/apt/sources.list.d/kreafinity.sources" in (DEBIAN / "postrm").read_text()
    for script in ("build-repo.sh", "new-signing-key.sh", "export-public-key.sh"):
        assert (APT / script).stat().st_mode & 0o111
    # The private key never belongs in the repository.
    assert not list(ROOT.glob("packaging/apt/*secret*")) and not list(
        ROOT.glob("**/*signing-key.asc")
    )


# --- The Kreafinity OS image (ISO) ---------------------------------------------------------------

ISO = ROOT / "packaging" / "iso"


def test_iso_packages() -> None:
    listed = {
        line.strip()
        for line in (ISO / "config" / "package-lists" / "kreafinity.list.chroot")
        .read_text()
        .splitlines()
        if line.strip() and not line.startswith("#")
    }
    # The desktop, the widget's web view, the installer, French.
    for package in (
        "kde-plasma-desktop",
        "qml6-module-qtwebengine",
        "calamares",
        "locales",
        "pkexec",
    ):
        assert package in listed


def test_iso_uses_the_kreafinity_look() -> None:
    from kreafinity.plasma.install import COLOR_SCHEME, THEME_ID

    kdeglobals = (ISO / "config" / "includes.chroot" / "etc" / "xdg" / "kdeglobals").read_text()
    assert f"LookAndFeelPackage={THEME_ID}" in kdeglobals
    assert f"ColorScheme={COLOR_SCHEME}" in kdeglobals
    layout = (
        ROOT
        / "ui"
        / "plasma"
        / "look-and-feel"
        / THEME_ID
        / "contents"
        / "layouts"
        / "org.kde.plasma.desktop-layout.js"
    ).read_text()
    assert 'addWidget("org.kreafinity.assistant")' in layout  # the assistant in the taskbar


def test_iso_scripts() -> None:
    scripts = [
        ISO / "build-iso.sh",
        *sorted((ISO / "config" / "hooks" / "live").glob("*.hook.chroot")),
    ]
    for script in scripts:
        assert script.stat().st_mode & 0o111, f"{script.name} is not executable"
        assert subprocess.run(["sh", "-n", str(script)], check=False).returncode == 0
    if shutil.which("shellcheck"):
        assert subprocess.run(["shellcheck", *map(str, scripts)], check=False).returncode == 0
    build = (ISO / "build-iso.sh").read_text()
    assert (
        "--distribution trixie" in build
        and "--mirror-binary https://deb.debian.org/debian" in build
    )
    assert "locales=fr_FR.UTF-8" in build and "keyboard-layouts=fr" in build


def test_installer_and_boot_menu_branding() -> None:
    import yaml

    branding = ISO / "config" / "includes.chroot" / "etc" / "calamares" / "branding" / "kreafinity"
    desc = yaml.safe_load((branding / "branding.desc").read_text())
    assert desc["componentName"] == "kreafinity" == branding.name
    assert desc["strings"]["productName"] == "Kreafinity OS"
    for image in desc["images"].values():
        assert (branding / image).is_file()
    assert (branding / desc["slideshow"]).is_file()
    assert "Bienvenue dans Kreafinity OS" in (branding / "show.qml").read_text()
    hook = (ISO / "config" / "hooks" / "live" / "0100-kreafinity.hook.chroot").read_text()
    assert "branding: kreafinity" in hook and "Installer Kreafinity OS" in hook
    # The boot menu: Kreafinity's title and background, and it starts by itself after 5 s.
    loaders = ISO / "config" / "bootloaders"
    assert "timeout 50" in (loaders / "isolinux" / "isolinux.cfg").read_text()
    assert "set timeout=5" in (loaders / "grub-pc" / "config.cfg").read_text()
    assert "menu title Kreafinity OS" in (loaders / "syslinux_common" / "menu.cfg").read_text()
    assert "Kreafinity OS" in (loaders / "splash.svg").read_text()
