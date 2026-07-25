#!/usr/bin/env bash
# Phase 9D: roll the APP (web/worker/scheduler) back to a previous image tag.
# Volumes and archive data are never touched. DB schema is NOT auto-rolled-back —
# restore from the pre-deploy pg_dump first if the new release ran a migration.
# Never uses `docker compose down -v`. Secret values are never echoed.
set -euo pipefail

TAG="${1:-${IMAGE_TAG:-}}"
if [ -z "$TAG" ]; then
  echo "usage: IMAGE_TAG=<prev-tag> ./scripts/rollback.sh   (or: ./scripts/rollback.sh <prev-tag>)"
  exit 2
fi
DRY_RUN="${DRY_RUN:-0}"
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.production.yml)
run() { echo "+ $*"; [ "$DRY_RUN" = "1" ] || "$@"; }

echo "== rolling web/worker/scheduler back to image tag: $TAG =="
echo "   (set the image tag via your compose 'image:' + IMAGE_TAG env; app code only)"
IMAGE_TAG="$TAG" run "${COMPOSE[@]}" up -d --no-deps web worker scheduler

cat <<'NOTE'
Post-rollback notes:
  * DB migrations are NOT auto-reverted. If the newer release migrated the schema,
    restore the pre-deploy pg_dump BEFORE running older code against it:
      gunzip -c backups/db-<ts>.sql.gz | docker compose exec -T postgres psql -U archiver archiver
  * archive files / pgdata / redisdata volumes are preserved. NEVER 'docker compose down -v'.
  * Re-run: docker compose exec -T web archiver system release-check
NOTE
