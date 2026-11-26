#!/usr/bin/env bash
# Publish the built package on GitHub Releases, where installed computers
# (Kreafinity OS, the Debian package) find their updates.
#
#   GH_TOKEN=… packaging/release/publish.sh dist
#
# One release per package version (the version holds the commit date and
# hash), marked « latest », with the .deb and its SHA-256. Only the newest
# KEEP releases are kept. Needs the GitHub CLI (gh), as on GitHub's runners.
set -euo pipefail

dist="${1:?usage: publish.sh <folder holding kreafinity_*_amd64.deb>}"
KEEP="${KREAFINITY_KEEP_RELEASES:-10}"
shopt -s nullglob
packages=("$dist"/kreafinity_*_amd64.deb)
[ "${#packages[@]}" -eq 1 ] || { echo "expected one package in $dist, found ${#packages[@]}" >&2; exit 1; }
package="${packages[0]}"
name="$(basename "$package")"
version="${name#kreafinity_}"
version="${version%_amd64.deb}"
tag="v$(printf '%s' "$version" | tr '~+:' '---')"

(cd "$dist" && sha256sum "$name" >"$name.sha256")
if gh release view "$tag" >/dev/null 2>&1; then
  echo "Release $tag already published"
else
  gh release create "$tag" "$package" "$package.sha256" \
    --title "Kreafinity $version" \
    --notes "Mise à jour de Kreafinity, installée depuis Kreafinity (Réglages › Général › Mises à jour, ou le bouton « Mettre à jour »)." \
    --target "${GITHUB_SHA:-main}" \
    --latest
  echo "Published release $tag"
fi

# The oldest releases go (with their tags): installed computers only need the newest.
gh release list --exclude-drafts --limit 200 --json tagName --jq ".[$KEEP:][].tagName" |
  while read -r old; do
    case "$old" in v*) gh release delete "$old" --yes --cleanup-tag ;; esac
  done
