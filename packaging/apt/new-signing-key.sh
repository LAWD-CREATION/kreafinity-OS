#!/usr/bin/env bash
# Create the signing key of the Kreafinity APT repository (once).
#
#   packaging/apt/new-signing-key.sh
#
# It writes:
# - packaging/apt/kreafinity-archive-keyring.asc: the PUBLIC key, to commit
#   (packages then trust the repository);
# - ~/kreafinity-apt-signing-key.asc: the PRIVATE key, to put in the GitHub
#   secret APT_SIGNING_KEY, then to keep somewhere safe offline (a USB key)
#   and to delete from the computer. Never commit it, never send it.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
public="$here/kreafinity-archive-keyring.asc"
private="$HOME/kreafinity-apt-signing-key.asc"
if [ -e "$public" ] || [ -e "$private" ]; then
  echo "A key already exists ($public or $private): delete it first to make a new one." >&2
  exit 1
fi

GNUPGHOME="$(mktemp -d)"
export GNUPGHOME
trap 'rm -rf "$GNUPGHOME"' EXIT
gpg --batch --quiet --passphrase "" --quick-generate-key \
  "Kreafinity APT repository <contact@kreafinity.com>" ed25519 sign 5y
fingerprint="$(gpg --list-secret-keys --with-colons | awk -F: '/^fpr/ { print $10; exit }')"
gpg --armor --export "$fingerprint" >"$public"
(umask 077 && gpg --armor --export-secret-keys "$fingerprint" >"$private")

cat <<EOF
Key created: $fingerprint

1. Commit the public key:
     git add packaging/apt/kreafinity-archive-keyring.asc && git commit -m "chore: APT signing key"
2. On GitHub: Settings > Secrets and variables > Actions > New repository secret
     Name:  APT_SIGNING_KEY
     Value: the whole content of $private
3. On GitHub: Settings > Pages > Source: GitHub Actions
4. Keep $private on a USB key, then delete it from this computer:
     shred -u $private
EOF
