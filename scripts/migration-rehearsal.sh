#!/usr/bin/env bash
# Phase 9F: migration rehearsals against a TEMPORARY throwaway Postgres.
#
#   1. FRESH INSTALL — empty DB -> `alembic upgrade head` -> audit schema checks
#      -> unsigned->signed boundary -> signed chain verify.
#   2. UPGRADE — `alembic upgrade d4e5f6a7b8c9` (pre-9E.1) -> seed representative
#      LEGACY audit chain -> `alembic upgrade head` -> legacy events preserved
#      (no re-sign/delete) -> lifecycle boundary -> verify.
#
# ISOLATION / GUARDS:
#   * a uniquely named `ya-migrehearsal-*` docker container with an anonymous
#     volume, published on a 127.0.0.1 EPHEMERAL port only;
#   * never uses docker compose, never references the real project or its
#     volumes (pgdata/redisdata), never mounts real data;
#   * teardown re-validates the container name prefix before `docker rm -f -v`;
#   * no manual ALTER — schema changes happen ONLY through alembic;
#   * `alembic downgrade` is NEVER executed (production downgrade of the audit
#     migrations is prohibited — it destroys the audit trail).
#
# usage: ./scripts/migration-rehearsal.sh
#   PY=.venv/bin/python REPORT_DIR=./backups/rehearsals PG_IMAGE=postgres:16-alpine
set -euo pipefail

PY="${PY:-.venv/bin/python}"
PG_IMAGE="${PG_IMAGE:-postgres:16-alpine}"
HEAD_REV="e5f6a7b8c9d0"
PRE_REV="d4e5f6a7b8c9"
REPORT_DIR="${REPORT_DIR:-./backups/rehearsals}"
TS="$(date +%Y%m%d-%H%M%S)"
NAME="ya-migrehearsal-$(date +%s)-$RANDOM"

# ---- guards -----------------------------------------------------------------
case "$NAME" in
  ya-migrehearsal-*) : ;;
  *) echo "guard: refusing to run with container name '$NAME'"; exit 9 ;;
esac
[ -f "alembic.ini" ] || { echo "run from the repo root"; exit 2; }
[ -x "$PY" ] || { echo "python not found at $PY (set PY=...)"; exit 2; }

teardown() {
  case "$NAME" in
    ya-migrehearsal-*) docker rm -f -v "$NAME" >/dev/null 2>&1 || true ;;
    *) echo "guard: refusing to remove container '$NAME'" ;;
  esac
  [ -n "${TMP:-}" ] && case "$TMP" in
    /*migrehearsal*) rm -rf "$TMP" ;;
  esac
}
trap teardown EXIT

# ---- temporary postgres (ephemeral loopback port, anonymous volume) ----------
PGPASS="$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')"
docker run -d --name "$NAME" \
  -e POSTGRES_USER=rehearsal -e POSTGRES_PASSWORD="$PGPASS" -e POSTGRES_DB=rehearsal \
  -p 127.0.0.1:0:5432 "$PG_IMAGE" >/dev/null
PORT="$(docker port "$NAME" 5432/tcp | head -1 | awk -F: '{print $NF}')"
[ -n "$PORT" ] || { echo "could not discover rehearsal postgres port"; exit 1; }
echo "== rehearsal postgres: $NAME on 127.0.0.1:$PORT (temporary) =="

ok=0
for _ in $(seq 1 60); do
  if docker exec "$NAME" pg_isready -U rehearsal -d rehearsal >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
[ "$ok" = "1" ] || { echo "rehearsal postgres did not become ready"; exit 1; }

# ---- rehearsal-only environment (never the real .env) ------------------------
TMP="$(mktemp -d -t ya-migrehearsal)"
mkdir -p "$TMP/keys" "$TMP/archive" "$TMP/logs" "$TMP/config" "$REPORT_DIR"
head -c 32 /dev/urandom | base64 > "$TMP/keys/k1"
export DATABASE_URL="postgresql+psycopg2://rehearsal:$PGPASS@127.0.0.1:$PORT/rehearsal"
export ARCHIVE_ROOT="$TMP/archive" LOG_ROOT="$TMP/logs" CONFIG_ROOT="$TMP/config"
export TAKEOUT_IMPORT_ROOT="$TMP/takeout" REDIS_URL="redis://127.0.0.1:6399/0"
export AUDIT_HMAC_KEY_FILE="$TMP/keys/k1" AUDIT_HMAC_KEY_ID="k1"
export AUDIT_HMAC_PREVIOUS_KEY_FILES="" AUDIT_HMAC_PREVIOUS_KEY_IDS=""

# ---- 1. fresh install ---------------------------------------------------------
echo "== [fresh] alembic upgrade head =="
"$PY" -m alembic upgrade head >/dev/null
echo "== [fresh] alembic check (models == migrated schema) =="
"$PY" -m alembic check
echo "== [fresh] verify =="
"$PY" scripts/migration_rehearsal_checks.py fresh-verify \
  --expected-head "$HEAD_REV" --report "$REPORT_DIR/migration-fresh-$TS.json"

# ---- 2. upgrade from pre-9E.1 -------------------------------------------------
echo "== [upgrade] recreate rehearsal database =="
docker exec "$NAME" psql -U rehearsal -d postgres \
  -c "DROP DATABASE rehearsal WITH (FORCE);" -c "CREATE DATABASE rehearsal;" >/dev/null
echo "== [upgrade] alembic upgrade $PRE_REV (pre-9E.1) =="
"$PY" -m alembic upgrade "$PRE_REV" >/dev/null
echo "== [upgrade] seed representative legacy audit chain =="
"$PY" scripts/migration_rehearsal_checks.py seed-legacy --state "$TMP/legacy.json"
echo "== [upgrade] alembic upgrade head =="
"$PY" -m alembic upgrade head >/dev/null
echo "== [upgrade] verify =="
"$PY" scripts/migration_rehearsal_checks.py upgrade-verify \
  --expected-head "$HEAD_REV" --state "$TMP/legacy.json" \
  --report "$REPORT_DIR/migration-upgrade-$TS.json"

echo "migration rehearsal PASSED (reports in $REPORT_DIR/migration-*-$TS.json)"
