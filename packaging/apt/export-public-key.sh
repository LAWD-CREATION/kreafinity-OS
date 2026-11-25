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
# What kind of key it is, read without importing it: "sec" (private) or "pub" only.
kinds="$(key | gpg --homedir "$home" --batch --show-keys --with-colons 2>/dev/null \
  | awk -F: '$1 == "sec" || $1 == "pub" { print $1 }' | sort -u | paste -sd ' ' || true)"
echo "APT_SIGNING_KEY: ${#APT_SIGNING_KEY} characters, $(key | wc -l) lines, key records: ${kinds:-none}" >&2
if [[ " $kinds " != *" sec "* ]]; then
  if [ -n "$kinds" ]; then
    what="only a public key"
  else
    what="no OpenPGP key"
  fi
  echo "::error::The APT_SIGNING_KEY secret holds $what (found: ${headers:-no armor line}). It must hold $expected." >&2
  exit 1
fi
# Import it (GnuPG's summary says what was imported; it never prints key material).
if ! key | gpg --homedir "$home" --batch --import; then
  echo "::error::GnuPG could not import the private key of APT_SIGNING_KEY (see its messages above)." >&2
  exit 1
fi
fingerprint="$(gpg --homedir "$home" --list-secret-keys --with-colons | awk -F: '/^fpr/ { print $10; exit }')"
if [ -z "$fingerprint" ]; then
  echo "::error::GnuPG read a private key in APT_SIGNING_KEY but did not keep it (see its messages above)." >&2
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
