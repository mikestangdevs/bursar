#!/usr/bin/env bash
#
# Bursar remote bootstrap — the one-liner the website hands out:
#   curl -fsSL https://bursar-hermes.com/install.sh | bash
#
# Downloads the prebuilt plugin bundle and runs its installer. Pass through
# installer flags, e.g.:  curl -fsSL https://bursar-hermes.com/install.sh | bash -s -- --with-gate
#
set -euo pipefail

URL="${BURSAR_TARBALL_URL:-https://bursar-hermes.com/bursar-plugin.tar.gz}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "▸ Fetching Bursar plugin bundle from $URL"
curl -fsSL "$URL" | tar -xz -C "$TMP"
bash "$TMP/bursar-plugin/install.sh" "$@"
