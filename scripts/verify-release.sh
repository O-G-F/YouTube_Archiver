#!/usr/bin/env bash
# Phase 10A: verify a release candidate from its manifest — re-checks the
# manifest integrity, the source/config lock hashes against the CURRENT tree,
# the schema head, and the per-service build-id agreement. Read-only: touches
# no volumes, recreates no services, downloads nothing. Secret values are never
# echoed.
#
# usage: ./scripts/verify-release.sh [release/<ts>/release-manifest.json]
set -euo pipefail

PY="${PY:-.venv/bin/python}"
command -v "$PY" >/dev/null 2>&1 || PY="python3"
[ -f "Dockerfile" ] || { echo "run from the repo root"; exit 2; }

MANIFEST="${1:-}"
if [ -z "$MANIFEST" ]; then
  MANIFEST="$(ls -1t release/*/release-manifest.json 2>/dev/null | head -1 || true)"
fi
[ -n "$MANIFEST" ] && [ -f "$MANIFEST" ] || { echo "no release manifest found (run scripts/build-release.sh)"; exit 2; }

echo "== verifying $(basename "$(dirname "$MANIFEST")")/release-manifest.json =="
"$PY" -m app.cli release verify-manifest --manifest "$MANIFEST" 2>/dev/null | grep -vE "alembic|^$"
