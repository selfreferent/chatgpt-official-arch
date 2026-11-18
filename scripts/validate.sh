#!/usr/bin/env bash
# SPDX-License-Identifier: 0BSD
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
bash -n PKGBUILD scripts/*.sh tests/*.sh
tests/test-check-upstream.sh
python3 upstream.py local-sources
srcinfo="$(mktemp)"
trap 'rm -f "$srcinfo"' EXIT
makepkg --printsrcinfo > "$srcinfo"
diff -u .SRCINFO "$srcinfo"
# Verifies helpers as well as upstream when --full is requested.
if [[ "${1:-}" == '--full' ]]; then
    python3 scripts/full-validation.py
fi
