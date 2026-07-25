#!/usr/bin/env bash
# Phase 9F: verify the NEWEST (or a given) backup manifest by recomputing the
# artifact's sha256/size in-container. Non-destructive — reads the backup dir,
# touches only the BACKUP_VERIFIED_MARKER_FILE on success. Never removes
# volumes/files, never echoes secret values.
#
# usage: ./scripts/verify-backup.sh [path/to/db-<ts>.sql.gz.manifest.json]
set -euo pipefail

COMPOSE="${COMPOSE:-docker compose}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"

MANIFEST="${1:-}"
if [ -z "$MANIFEST" ]; then
  MANIFEST="$(ls -1t "$BACKUP_DIR"/*.manifest.json 2>/dev/null | head -1 || true)"
fi
if [ -z "$MANIFEST" ] || [ ! -f "$MANIFEST" ]; then
  echo "no backup manifest found (run ./scripts/backup.sh first)"; exit 2
fi
ABS_DIR="$(cd "$(dirname "$MANIFEST")" && pwd)"
BASE="$(basename "$MANIFEST")"

# best-effort audit (never fatal)
audit_op() { $COMPOSE exec -T web archiver audit log-op --event "$1" --outcome "${2:-success}" --severity "${3:-info}" --category backup >/dev/null 2>&1 || true; }
trap 'audit_op backup_verify_failed failure warning' ERR

echo "== verifying $BASE =="
$COMPOSE run --rm --no-deps -T -v "$ABS_DIR:/backups" web \
  archiver backup verify-manifest --manifest "/backups/$BASE" --write-marker

audit_op backup_verified
echo "backup verify complete: $BASE"
