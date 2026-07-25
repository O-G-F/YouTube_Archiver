#!/usr/bin/env bash
# Phase 9D: pre-deploy backup. Non-destructive — never removes volumes or archive
# files. Dumps Postgres, triggers a Redis AOF rewrite, and touches the backup
# freshness marker that `release-check` reads. Secret values are never echoed.
set -euo pipefail

COMPOSE="${COMPOSE:-docker compose}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
# Host path for the marker. Mount the same dir into the container and set the
# container-side BACKUP_MARKER_FILE (e.g. /config/last_backup) so release-check sees it.
MARKER="${BACKUP_MARKER_HOST_FILE:-./data/config/last_backup}"
POSTGRES_USER="${POSTGRES_USER:-archiver}"
POSTGRES_DB="${POSTGRES_DB:-archiver}"

ts="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR" "$(dirname "$MARKER")"

# best-effort audit (never fatal)
audit_op() { $COMPOSE exec -T web archiver audit log-op --event "$1" --outcome "${2:-success}" --severity "${3:-info}" >/dev/null 2>&1 || true; }
trap 'audit_op backup_failed failure warning' ERR
audit_op backup_started

echo "== Postgres dump -> $BACKUP_DIR/db-$ts.sql.gz =="
$COMPOSE exec -T postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$BACKUP_DIR/db-$ts.sql.gz"

# Phase 9F/9F.1: backup-SET manifest next to the artifact + a small summary
# under /config for release-check (set BACKUP_MANIFEST_SUMMARY_FILE). The set =
# DB dump + archive manifest (sizes snapshot) + audit chain head + build state.
# Transition guard: a pre-9F image has no `archiver backup` — warn, don't abort,
# so the FIRST 9F deploy (backup runs before build) still completes.
echo "== backup manifest (backup set) =="
BACKUP_ABS="$(cd "$BACKUP_DIR" && pwd)"
if $COMPOSE run --rm --no-deps -T web archiver backup --help >/dev/null 2>&1; then
  $COMPOSE run --rm --no-deps -T -v "$BACKUP_ABS:/backups" web \
    archiver backup archive-manifest --out "/backups/archive-manifest-$ts.json"
  $COMPOSE run --rm --no-deps -T -v "$BACKUP_ABS:/backups" web \
    archiver backup write-manifest --artifact "/backups/db-$ts.sql.gz" \
    --archive-manifest "/backups/archive-manifest-$ts.json"
else
  echo "  (warning: image predates Phase 9F — manifest skipped; rebuild then re-run backup)"
fi

echo "== Redis AOF rewrite (BGREWRITEAOF) =="
$COMPOSE exec -T redis redis-cli BGREWRITEAOF >/dev/null || echo "  (warning: redis BGREWRITEAOF skipped)"

echo "== Reminder: also copy the redisdata volume + the archive directory out-of-band."
echo "   (see README Phase 9D backup/restore runbook). Do NOT 'docker compose down -v'."

: > "$MARKER"   # record freshness for release-check
audit_op backup_completed
echo "backup complete: $BACKUP_DIR/db-$ts.sql.gz ; marker touched: $MARKER"
