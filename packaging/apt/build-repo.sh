#!/usr/bin/env bash
# Build the Kreafinity APT repository (signed) from Debian packages.
#
#   KREAFINITY_APT_KEY=<fingerprint> packaging/apt/build-repo.sh <packages folder> <site folder>
#
# The signing key must be in GnuPG's keyring ($GNUPGHOME). The site folder is
# then published as is (GitHub Pages): APT reads dists/ and pool/, people read
# index.html. Variables: KREAFINITY_APT_URL (the repository's address).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
debs="$(cd "${1:?packages folder}" && pwd)"
site="${2:?site folder}"
key="${KREAFINITY_APT_KEY:?the fingerprint of the signing key (KREAFINITY_APT_KEY)}"
url="${KREAFINITY_APT_URL:-https://lawd-creation.github.io/kreafinity-OS}"
suite=stable
component=main

shopt -s nullglob
packages=("$debs"/*.deb)
[ ${#packages[@]} -gt 0 ] || { echo "error: no .deb in $debs" >&2; exit 1; }

rm -rf "$site"
mkdir -p "$site/pool/$component/k/kreafinity"
cp "${packages[@]}" "$site/pool/$component/k/kreafinity/"
site="$(cd "$site" && pwd)"
cd "$site"

# Package indexes, per architecture.
architectures=()
for package in "${packages[@]}"; do
  arch="$(dpkg-deb -f "$package" Architecture)"
  [[ " ${architectures[*]} " == *" $arch "* ]] || architectures+=("$arch")
done
for arch in "${architectures[@]}"; do
  index="dists/$suite/$component/binary-$arch"
  mkdir -p "$index"
  apt-ftparchive --arch "$arch" packages "pool/$component" >"$index/Packages"
  gzip -9nk "$index/Packages"
done

# The Release file lists every index with its checksums; signing it signs everything.
apt-ftparchive \
  -o APT::FTPArchive::Release::Origin=Kreafinity \
  -o APT::FTPArchive::Release::Label=Kreafinity \
  -o APT::FTPArchive::Release::Suite="$suite" \
  -o APT::FTPArchive::Release::Codename="$suite" \
  -o APT::FTPArchive::Release::Architectures="${architectures[*]}" \
  -o APT::FTPArchive::Release::Components="$component" \
  -o APT::FTPArchive::Release::Description="Kreafinity OS" \
  release "dists/$suite" >"$site/Release.tmp"
mv "$site/Release.tmp" "dists/$suite/Release"
gpg --batch --yes --local-user "$key" --armor --detach-sign --output "dists/$suite/Release.gpg" "dists/$suite/Release"
gpg --batch --yes --local-user "$key" --clearsign --output "dists/$suite/InRelease" "dists/$suite/Release"

# What people need: the public key, the source to add, and the latest package.
gpg --armor --export "$key" >kreafinity-archive-keyring.asc
gpg --export "$key" >kreafinity-archive-keyring.gpg
printf 'Types: deb\nURIs: %s\nSuites: %s\nComponents: %s\nSigned-By: /usr/share/keyrings/kreafinity-archive-keyring.gpg\n' \
  "$url" "$suite" "$component" >kreafinity.sources
latest="$(printf '%s\n' "${packages[@]}" | sort -V | tail -n 1)"
version="$(dpkg-deb -f "$latest" Version)"
arch="$(dpkg-deb -f "$latest" Architecture)"
cp "$latest" "kreafinity_latest_$arch.deb"
sed -e "s|@URL@|$url|g" -e "s|@VERSION@|$version|g" -e "s|@ARCH@|$arch|g" "$here/index.html" >index.html
touch .nojekyll  # GitHub Pages: publish the files as they are

echo "APT repository: $site ($version, ${architectures[*]})"
