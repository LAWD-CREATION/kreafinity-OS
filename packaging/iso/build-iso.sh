#!/usr/bin/env bash
# Build the Kreafinity OS image (ISO): Debian 13 « trixie », KDE Plasma 6 and
# Kreafinity, to try from a USB key (live) and to install (Calamares).
#
#   sudo packaging/iso/build-iso.sh <kreafinity .deb> [output folder]     (default: dist/)
#
# Run as root on Debian 13, or in a privileged debian:trixie container, with
# live-build installed. About 30 minutes and 15 GB of disk space.
# Variables: KREAFINITY_ISO_MIRROR and KREAFINITY_ISO_SECURITY_MIRROR (Debian
# mirrors to build from; the image itself always uses deb.debian.org).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
deb="$(realpath "${1:?the kreafinity .deb to put in the image}")"
out="$(mkdir -p "${2:-$repo/dist}" && cd "${2:-$repo/dist}" && pwd)"
mirror="${KREAFINITY_ISO_MIRROR:-https://deb.debian.org/debian}"
security="${KREAFINITY_ISO_SECURITY_MIRROR:-https://security.debian.org/debian-security}"
work="$(mktemp -d /var/tmp/kreafinity-iso.XXXXXX)"
trap 'rm -rf "$work"' EXIT

say() { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
[ "$(id -u)" = 0 ] || { echo "error: run as root (live-build needs it)" >&2; exit 1; }
for tool in lb mount mksquashfs xorriso; do
  command -v "$tool" >/dev/null || {
    echo "error: $tool is missing (apt install live-build mount squashfs-tools xorriso)" >&2
    exit 1
  }
done

version="$(dpkg-deb -f "$deb" Version)"
say "Kreafinity OS image, with Kreafinity $version"
cd "$work"
lb config \
  --distribution trixie \
  --archive-areas "main non-free-firmware" \
  --debian-installer none \
  --mirror-bootstrap "$mirror" \
  --mirror-chroot "$mirror" \
  --mirror-chroot-security "$security" \
  --mirror-binary https://deb.debian.org/debian \
  --mirror-binary-security https://security.debian.org/debian-security \
  --chroot-squashfs-compression-type zstd \
  --bootappend-live "boot=live components quiet splash locales=fr_FR.UTF-8 keyboard-layouts=fr timezone=Europe/Paris username=kreafinity user-fullname=Kreafinity hostname=kreafinity" \
  --iso-application "Kreafinity OS" \
  --iso-publisher "Kreafinity - https://kreafinity.com" \
  --iso-volume "KREAFINITY_OS" \
  --image-name kreafinity-os \
  >/dev/null

# Our configuration over live-build's: packages, defaults, hooks, and Kreafinity.
cp -rT "$here/config" "$work/config"
mkdir -p "$work/config/packages.chroot"
cp "$deb" "$work/config/packages.chroot/"

say "Building (lb build: packages, image, ISO)"
if ! lb build >"$work/build.log" 2>&1; then
  tail -n 40 "$work/build.log" >&2
  cp "$work/build.log" "$out/kreafinity-os-build.log"
  echo "error: the build failed (log: $out/kreafinity-os-build.log)" >&2
  exit 1
fi

iso="$out/kreafinity-os-${version}-amd64.iso"
mv "$work"/kreafinity-os-amd64.hybrid.iso "$iso"
(cd "$out" && sha256sum "$(basename "$iso")" >"$(basename "$iso").sha256")
say "Built: $iso ($(du -h "$iso" | cut -f1))"
