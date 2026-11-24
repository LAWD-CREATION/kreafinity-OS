#!/usr/bin/env bash
# Write the PUBLIC part of the APT signing key (given in $APT_SIGNING_KEY, the
# GitHub secret) to packaging/apt/kreafinity-archive-keyring.asc, where
# build-deb.sh puts it in the package (so installed computers get updates).
#
# If that file is already committed, check it is the same key instead.
# Prints the key's fingerprint. Used by the CI; the private key never leaves
# the temporary GnuPG home.
set -euo pipefail

: "${APT_SIGNING_KEY:?the APT_SIGNING_KEY secret is empty}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
public="$here/kreafinity-archive-keyring.asc"

home="$(mktemp -d)"
trap 'rm -rf "$home"' EXIT
printf '%s\n' "$APT_SIGNING_KEY" | gpg --homedir "$home" --batch --quiet --import 2>/dev/null
fingerprint="$(gpg --homedir "$home" --list-secret-keys --with-colons | awk -F: '/^fpr/ { print $10; exit }')"
[ -n "$fingerprint" ] || { echo "APT_SIGNING_KEY holds no private key" >&2; exit 1; }

if [ -f "$public" ]; then
  committed="$(gpg --homedir "$home" --show-keys --with-colons "$public" | awk -F: '/^fpr/ { print $10; exit }')"
  if [ "$committed" != "$fingerprint" ]; then
    echo "$public is key $committed, but APT_SIGNING_KEY is key $fingerprint" >&2
    exit 1
  fi
else
  gpg --homedir "$home" --armor --export "$fingerprint" >"$public"
fi
echo "$fingerprint"
