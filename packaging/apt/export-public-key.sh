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

# The key as pasted in GitHub, without Windows line ends or indentation.
key() { printf '%s\n' "$APT_SIGNING_KEY" | tr -d '\r' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'; }
# What the secret holds, to explain a mistake: its armor header lines only, never the key.
headers="$(key | grep -o -- '-----BEGIN [A-Z ]*-----' | sort -u | paste -sd ' ' || true)"
expected="the whole content of ~/kreafinity-apt-signing-key.asc, from -----BEGIN PGP PRIVATE KEY BLOCK----- to -----END PGP PRIVATE KEY BLOCK-----"

home="$(mktemp -d)"
trap 'rm -rf "$home"' EXIT
if ! key | gpg --homedir "$home" --batch --quiet --import; then
  echo "::error::The APT_SIGNING_KEY secret is not a readable OpenPGP key (found: ${headers:-no -----BEGIN PGP …----- line}). It must hold $expected." >&2
  exit 1
fi
fingerprint="$(gpg --homedir "$home" --list-secret-keys --with-colons | awk -F: '/^fpr/ { print $10; exit }')"
if [ -z "$fingerprint" ]; then
  echo "::error::The APT_SIGNING_KEY secret holds no private key (found: ${headers:-nothing}). It must hold $expected." >&2
  exit 1
fi

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
