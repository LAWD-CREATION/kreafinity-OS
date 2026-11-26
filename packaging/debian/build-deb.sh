#!/usr/bin/env bash
# Build the Kreafinity Debian package (.deb).
#
# The package is self-contained: Kreafinity and its own Python live in
# /opt/kreafinity, so the same package works on Debian 12 and 13 and on Ubuntu
# 24.04, whatever their Python. It adds, for every user of the computer: the
# daemon (systemd user service), the applications menu entry, the bubble
# (desktops other than KDE) and the KDE Plasma integration (start menu search,
# taskbar widget, file manager menu, theme). Updates then come with the
# system's (APT).
#
#   packaging/debian/build-deb.sh [output folder]      (default: dist/)
#
# Needs: uv (downloads the Python), dpkg-deb, and Internet access (PyPI).
# Variables: KREAFINITY_PYTHON (Python version, 3.13 by default); KREAFINITY_VERSION
# (package version, computed by default); KREAFINITY_APT_URL and KREAFINITY_APT_KEYRING
# (the APT repository the package configures, see packaging/apt/).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
out="${1:-$repo/dist}"
python_version="${KREAFINITY_PYTHON:-3.13}"
arch="$(dpkg --print-architecture)"
prefix=/opt/kreafinity
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
root="$work/root"

# Only Kreafinity's own packages: never those of the machine building it (user site).
export PYTHONNOUSERSITE=1
unset PYTHONPATH PYTHONHOME

say() { printf '\033[1;35m==>\033[0m %s\n' "$*"; }

# --- Version: 0.1.0.dev0, commit abc1234 of 2026-11-21 14:30:12 UTC → 0.1.0~dev0+git20261121.143012.abc1234
# (date and time first: versions always increase, even for two commits the same day) ---------
upstream="$(python3 -c 'import tomllib, sys; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' "$repo/pyproject.toml")"
version="$(printf '%s' "$upstream" | sed -E 's/\.?(dev|a|b|rc)([0-9]*)$/~\1\2/')"
if git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
  commit="$(git -C "$repo" rev-parse --short=7 HEAD)"
  date="$(TZ=UTC git -C "$repo" log -1 --format=%cd --date=format-local:%Y%m%d.%H%M%S)"
  version="$version+git$date.$commit"
fi
version="${KREAFINITY_VERSION:-$version}"
apt_url="${KREAFINITY_APT_URL:-https://lawd-creation.github.io/kreafinity-OS}"
repository="${KREAFINITY_REPOSITORY:-https://github.com/LAWD-CREATION/kreafinity-OS}"
apt_keyring="${KREAFINITY_APT_KEYRING:-$repo/packaging/apt/kreafinity-archive-keyring.asc}"
say "Kreafinity $version ($arch), Python $python_version"

# --- Its own Python ----------------------------------------------------------------------------
say "Python $python_version"
export UV_PYTHON_INSTALL_DIR="$work/pythons"
uv python install --quiet "$python_version"
python_bin="$(uv python find --managed-python "$python_version")"
python_root="$(realpath "$(dirname "$(dirname "$python_bin")")")"  # uv links versions
mkdir -p "$root$prefix"
cp -a "$python_root/." "$root$prefix/python/"
py="$root$prefix/python/bin/python3"
if [ -L "$root$prefix/python" ] || [ ! -x "$py" ]; then
  echo "error: the bundled Python was not copied ($python_root)" >&2
  exit 1
fi
# Kreafinity's Python is its own: pip may install into it while the package is built.
find "$root$prefix/python/lib" -maxdepth 2 -name EXTERNALLY-MANAGED -delete
"$py" -m ensurepip --upgrade >/dev/null 2>&1 || true

# --- Kreafinity --------------------------------------------------------------------------------
say "Kreafinity and its dependencies"
"$py" -m pip install --quiet --no-cache-dir --disable-pip-version-check --no-warn-script-location "$repo"
# Kreafinity updates itself from the packages published on this repository's GitHub Releases.
"$py" - "$root$prefix/python/kreafinity-package.json" "$version" "$repository" <<'EOF'
import json, sys
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump({"format": "deb", "version": sys.argv[2], "repository": sys.argv[3]}, handle)
EOF
# Kreafinity must start with nothing but what the package holds.
env -i HOME=/nonexistent "$py" -s -c 'import kreafinity.cli, kreafinity.api.app, kreafinity.plasma.krunner' || {
  echo "error: Kreafinity does not start from the package alone" >&2
  exit 1
}
# Compiled in advance (the folder is read-only once installed), with the final paths.
"$py" -m compileall -q -j 0 -s "$root" -p / "$root$prefix/python/lib" >/dev/null || true

mkdir -p "$root$prefix/bin" "$root/usr/bin"
for name in kreafinity:kreafinity.cli kreafinity-core:kreafinity.api.server; do
  command="${name%%:*}" module="${name#*:}"
  # -s: the user's own Python packages (~/.local) never mix with Kreafinity's.
  printf '#!/bin/sh\nexec %s/python/bin/python3 -s -m %s "$@"\n' "$prefix" "$module" >"$root$prefix/bin/$command"
  chmod 755 "$root$prefix/bin/$command"
  ln -s "$prefix/bin/$command" "$root/usr/bin/$command"
done

# --- Integration with the system, for every user -----------------------------------------------
say "Desktop integration"
assets="$("$py" -c 'from kreafinity.api.web import plasma_assets; print(plasma_assets())')"
web="$("$py" -c 'from kreafinity.api.web import web_root; print(web_root())')"
"$py" - "$root" "$prefix" <<'EOF'
import sys
from pathlib import Path

from kreafinity.system import integration

root, prefix = Path(sys.argv[1]), sys.argv[2]

def write(path: str, text: str, mode: int = 0o644) -> None:
    target = root / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    target.chmod(mode)

write("/usr/lib/systemd/user/kreafinity.service",
      integration.service_unit([f"{prefix}/bin/kreafinity-core"]))
write("/usr/share/applications/kreafinity.desktop",
      integration.menu_entry(["/usr/bin/kreafinity", "ui"], None))
# The bubble at login, except on KDE Plasma (its taskbar widget does the same).
write("/etc/xdg/autostart/kreafinity-bubble.desktop",
      integration.autostart_entry(["/usr/bin/kreafinity", "bubble"], "Kreafinity, l'assistant IA")
      + "NotShowIn=KDE;\n")
# The welcome (choosing the AI…) at login, until an AI can answer.
write("/etc/xdg/autostart/kreafinity-welcome.desktop",
      integration.autostart_entry(["/usr/bin/kreafinity", "welcome", "--if-needed"],
                                  "Kreafinity : choisir son IA au premier démarrage"))
write("/usr/share/dbus-1/services/org.kreafinity.KRunner.service",
      "[D-BUS Service]\nName=org.kreafinity.KRunner\n"
      f"Exec={prefix}/python/bin/python3 -s -m kreafinity.plasma.krunner\n")
EOF
install -Dm644 "$web/img/logo.svg" "$root/usr/share/icons/hicolor/scalable/apps/kreafinity.svg"
install -Dm644 "$assets/krunner/kreafinity.desktop" "$root/usr/share/krunner/dbusplugins/kreafinity.desktop"
mkdir -p "$root/usr/share/plasma/look-and-feel" "$root/usr/share/plasma/plasmoids" "$root/usr/share/color-schemes" "$root/usr/share/wallpapers" \
  "$root/usr/share/kio/servicemenus" "$root$prefix/share/plasmoids"
cp -r "$assets/look-and-feel/org.kreafinity.desktop" "$root/usr/share/plasma/look-and-feel/"
cp "$assets/color-schemes/Kreafinity.colors" "$root/usr/share/color-schemes/"
cp -r "$assets/wallpapers/Kreafinity" "$root/usr/share/wallpapers/"
# The taskbar widget exists for Plasma 5 and 6: the right one is chosen when installing.
cp -r "$assets/plasmoid-5/org.kreafinity.assistant" "$root$prefix/share/plasmoids/5"
cp -r "$assets/plasmoid-6/org.kreafinity.assistant" "$root$prefix/share/plasmoids/6"
for menu in "$assets"/servicemenus/*.desktop; do
  target="$root/usr/share/kio/servicemenus/$(basename "$menu")"
  sed "s|@COMMAND@|$prefix/python/bin/python3 -s -m kreafinity.plasma.files_menu|" "$menu" >"$target"
  chmod 755 "$target"  # KDE ignores service menus that are not executable
done

# The APT repository, for updates with the system (once its signing key exists).
if [ -f "$apt_keyring" ]; then
  mkdir -p "$root/usr/share/keyrings" "$root/usr/share/kreafinity"
  gpg --dearmor <"$apt_keyring" >"$root/usr/share/keyrings/kreafinity-archive-keyring.gpg"
  chmod 644 "$root/usr/share/keyrings/kreafinity-archive-keyring.gpg"
  printf 'Types: deb\nURIs: %s\nSuites: stable\nComponents: main\nSigned-By: /usr/share/keyrings/kreafinity-archive-keyring.gpg\n' \
    "$apt_url" >"$root/usr/share/kreafinity/kreafinity.sources"
  say "APT repository: $apt_url"
else
  say "No APT repository key yet ($apt_keyring): updates will need a new package"
fi

install -Dm644 "$repo/LICENSE" "$root/usr/share/doc/kreafinity/copyright"
printf 'kreafinity (%s) unstable; urgency=medium\n\n  * Build from %s.\n\n -- Kreafinity <contact@kreafinity.com>  %s\n' \
  "$version" "${commit:-a source tree}" "$(date -R)" | gzip -9n >"$root/usr/share/doc/kreafinity/changelog.Debian.gz"

# --- Package metadata and scripts --------------------------------------------------------------
mkdir -p "$root/DEBIAN"
installed_size="$(du -sk --exclude=DEBIAN "$root" | cut -f1)"
sed -e "s|@VERSION@|$version|" -e "s|@ARCH@|$arch|" -e "s|@SIZE@|$installed_size|" \
  "$here/control.in" >"$root/DEBIAN/control"
printf '%s\n' /etc/xdg/autostart/kreafinity-bubble.desktop /etc/xdg/autostart/kreafinity-welcome.desktop >"$root/DEBIAN/conffiles"
for script in postinst prerm postrm; do
  install -m755 "$here/$script" "$root/DEBIAN/$script"
done

mkdir -p "$out"
package="$out/kreafinity_${version}_${arch}.deb"
dpkg-deb --root-owner-group -Zxz --build "$root" "$package" >/dev/null
say "Built: $package ($(du -h "$package" | cut -f1))"
