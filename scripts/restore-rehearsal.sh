#!/usr/bin/env bash
# =====================================================================
# Phase 9F: ISOLATED disaster-recovery / restore rehearsal.
# =====================================================================
# Restores the newest (or given) pg_dump into a TEMPORARY Compose project and
# runs the full DR acceptance suite: schema head, migrate no-op, preflight,
# production-check, release-check, audit chain verify, restore-boundary
# break-glass, signing-key rotation + current/previous/missing/wrong-key
# recovery, pseudonym-key separation, auth/CSRF/metrics/readiness, archive
# check against generated fixtures, media HTTP Range, duplicate/orphan checks,
# and a machine-readable acceptance report.
#
# INTERACTION WITH THE REAL STACK (project "youtube_archiver") IS READ-ONLY:
#   * optional `pg_dump` / `SELECT version_num` via `docker compose exec -T`
#   * post-teardown existence checks (docker volume inspect / docker ps)
#   * on success: touches RESTORE_REHEARSAL_MARKER_HOST_FILE (one marker file,
#     same pattern as scripts/backup.sh's freshness marker)
# It NEVER runs up/down/build/rm against the real project.
#
# GUARDS:
#   * unique project name ya-rehearsal-<ts>-<rand>, regex-validated before
#     EVERY compose invocation (single COMPOSE array) and again in teardown;
#   * rehearsal volumes are named rehearsal_* (no name collision with
#     youtube_archiver_pgdata / redisdata even if -p were lost);
#   * all binds live under a mktemp REHEARSAL_ROOT outside the repo;
#   * `down -v` exists ONLY in teardown() and only for the temp project;
#   * no downloads: no scheduler service, no enqueue calls, and the job count
#     is asserted unchanged at the end.
#
# usage: ./scripts/restore-rehearsal.sh [path/to/db-<ts>.sql.gz]
#   env: REHEARSAL_REUSE_IMAGE=<image>  reuse an existing app image (skip build)
#        REHEARSAL_KEEP_IMAGE=1         keep the built image after teardown
#        REPORT_DIR=./backups/rehearsals
#        FIXTURES=3                     number of dummy media fixture files
set -euo pipefail

REAL_PROJECT="youtube_archiver"
TEMPLATE="docker-compose.restore-rehearsal.yml"
HEAD_REV="e5f6a7b8c9d0"
TS="$(date +%Y%m%d-%H%M%S)"
PROJ="ya-rehearsal-$(date +%s)-$RANDOM"
REPORT_DIR="${REPORT_DIR:-./backups/rehearsals}"
PY3="${PY3:-python3}"
FIXTURES="${FIXTURES:-3}"
MARKER_HOST_FILE="${RESTORE_REHEARSAL_MARKER_HOST_FILE:-./data/config/last_restore_rehearsal}"

# ---------------- guards ----------------
[[ "$PROJ" =~ ^ya-rehearsal-[0-9]+-[0-9]+$ ]] || { echo "guard: bad project name '$PROJ'"; exit 9; }
[ "$PROJ" != "$REAL_PROJECT" ] || { echo "guard: project collision"; exit 9; }
[ -f "$TEMPLATE" ] && [ -f "docker-compose.yml" ] || { echo "run from the repo root"; exit 2; }
command -v "$PY3" >/dev/null || { echo "python3 required"; exit 2; }

REHEARSAL_ROOT="$(mktemp -d -t ya-rehearsal)"
case "$REHEARSAL_ROOT" in
  "$PWD"|"$PWD"/*) echo "guard: rehearsal root must be OUTSIDE the repo"; exit 9 ;;
  *ya-rehearsal*) : ;;
  *) echo "guard: unexpected rehearsal root"; exit 9 ;;
esac
umask 077

COMPOSE=(docker compose -p "$PROJ" -f "$TEMPLATE" --env-file "$REHEARSAL_ROOT/.compose-env")
IMG="ya-rehearsal-img:$TS"
ITEMS="$(mktemp -t ya-rehearsal-items)"

item() { # name status(pass|fail|info) expected(yes|no) detail — detail must stay path-free
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" | tr -d '\r' >> "$ITEMS"
  echo "  [$2] $1 - $4"
}

TEARDOWN_DONE=0
teardown() {
  [ "$TEARDOWN_DONE" = "1" ] && return 0
  TEARDOWN_DONE=1
  echo "== teardown (temporary project '$PROJ' ONLY) =="
  if [[ "$PROJ" =~ ^ya-rehearsal-[0-9]+-[0-9]+$ ]] && [ "$PROJ" != "$REAL_PROJECT" ]; then
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  else
    echo "guard: refusing teardown for '$PROJ'"
  fi
  if [ -z "${REHEARSAL_REUSE_IMAGE:-}" ] && [ "${REHEARSAL_KEEP_IMAGE:-0}" != "1" ]; then
    docker rmi "$IMG" >/dev/null 2>&1 || true
  fi
  case "$REHEARSAL_ROOT" in
    *ya-rehearsal*) rm -rf "$REHEARSAL_ROOT" ;;
    *) echo "guard: refusing to remove '$REHEARSAL_ROOT'" ;;
  esac
}
trap teardown EXIT

# ---------------- 0. pick / create the dump (real stack: READ-ONLY) ----------
DUMP="${1:-}"
if [ -z "$DUMP" ]; then
  DUMP="$(ls -1t ./backups/db-*.sql.gz 2>/dev/null | head -1 || true)"
fi
if [ -z "$DUMP" ]; then
  echo "== no existing dump — creating one (read-only pg_dump on the current stack) =="
  mkdir -p ./backups
  DUMP="./backups/db-$TS.sql.gz"
  docker compose -p "$REAL_PROJECT" exec -T postgres pg_dump -U archiver archiver | gzip > "$DUMP"
fi
[ -f "$DUMP" ] || { echo "dump not found: $DUMP"; exit 2; }
DUMP_BASE="$(basename "$DUMP")"
BACKUPS_ABS="$(cd "$(dirname "$DUMP")" && pwd)"
mkdir -p "$REPORT_DIR"
echo "== rehearsal project: $PROJ  dump: $DUMP_BASE =="

# ---------------- 1. rehearsal image ----------------
if [ -n "${REHEARSAL_REUSE_IMAGE:-}" ]; then
  IMG="$REHEARSAL_REUSE_IMAGE"
  echo "== reusing image $IMG =="
else
  echo "== building rehearsal image from repo HEAD (this validates the current code) =="
  docker build -t "$IMG" . > "$REHEARSAL_ROOT/build.log" 2>&1 \
    || { tail -30 "$REHEARSAL_ROOT/build.log"; echo "image build failed"; exit 1; }
fi

# ---------------- 2. backup manifest verify (Phase 9F integrity path) --------
REAL_HEAD="$(docker compose -p "$REAL_PROJECT" exec -T postgres psql -U archiver -At \
  -c 'SELECT version_num FROM alembic_version' 2>/dev/null | tr -d '\r' || true)"
if [ ! -f "$BACKUPS_ABS/$DUMP_BASE.manifest.json" ]; then
  docker run --rm -v "$BACKUPS_ABS:/backups" "$IMG" \
    archiver backup write-manifest --artifact "/backups/$DUMP_BASE" --schema-head "$REAL_HEAD" >/dev/null
fi
if docker run --rm -v "$BACKUPS_ABS:/backups" "$IMG" \
     archiver backup verify-manifest --manifest "/backups/$DUMP_BASE.manifest.json" >/dev/null 2>&1; then
  item backup_manifest_verified pass no "sha256/size of $DUMP_BASE match its manifest"
else
  item backup_manifest_verified fail no "manifest mismatch for $DUMP_BASE"
fi

# ---------------- 3. temp secrets + env ----------------
S="$REHEARSAL_ROOT/secrets"
mkdir -p "$S" "$REHEARSAL_ROOT/archive" "$REHEARSAL_ROOT/config" "$REHEARSAL_ROOT/logs" "$REHEARSAL_ROOT/takeout"
head -c 32 /dev/urandom | base64 > "$S/audit_k1"
head -c 32 /dev/urandom | base64 > "$S/audit_k2_pending"
head -c 32 /dev/urandom | base64 > "$S/audit_pseudonym"
ADMIN_PW="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)"
PGPASS="$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')"
docker run --rm "$IMG" python -c \
  "from app.services.auth import gen_session_secret; print(gen_session_secret())" > "$S/session_secret"
docker run --rm -e ADMIN_PW="$ADMIN_PW" "$IMG" python -c \
  "import os; from app.services.auth import hash_password; print(hash_password(os.environ['ADMIN_PW']))" \
  > "$S/admin_password_hash"
PORT="$($PY3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
BASE="http://127.0.0.1:$PORT"

write_env() { # $1=current key file  $2=current key id  $3=prev files  $4=prev ids
  cat > "$REHEARSAL_ROOT/.env" <<EOF
APP_ENV=production
AUTH_MODE=local
DATABASE_URL=postgresql+psycopg2://archiver:$PGPASS@postgres:5432/archiver
REDIS_URL=redis://redis:6379/0
ARCHIVE_ROOT=/archive
LOG_ROOT=/logs
CONFIG_ROOT=/config
TAKEOUT_IMPORT_ROOT=/takeout_imports
SESSION_SECRET_FILE=/secrets/session_secret
ADMIN_PASSWORD_HASH_FILE=/secrets/admin_password_hash
SESSION_COOKIE_SECURE=true
ALLOWED_HOSTS=localhost,127.0.0.1
PUBLIC_BASE_URL=https://rehearsal.invalid
CORS_ALLOW_ORIGINS=https://rehearsal.invalid
CSRF_TRUSTED_ORIGINS=https://rehearsal.invalid
LOGIN_RATE_LIMIT_MAX_ATTEMPTS=50
AUDIT_ENABLED=true
AUDIT_HMAC_KEY_FILE=$1
AUDIT_HMAC_KEY_ID=$2
AUDIT_HMAC_PREVIOUS_KEY_FILES=$3
AUDIT_HMAC_PREVIOUS_KEY_IDS=$4
AUDIT_PSEUDONYM_KEY_FILE=/secrets/audit_pseudonym
AUDIT_ALLOW_LEGACY_UNSIGNED_PREFIX=true
STRUCTURED_LOGGING=true
ARCHIVE_MIN_FREE_GB=1
SCHEDULER_ENABLED=false
SCHEDULER_COMMENTS_ENABLED=false
BACKUP_MARKER_FILE=/config/last_backup
BACKUP_MANIFEST_SUMMARY_FILE=/config/last_backup_manifest.json
BACKUP_VERIFIED_MARKER_FILE=/config/last_backup_verified
RESTORE_REHEARSAL_MARKER_FILE=/config/last_restore_rehearsal
EOF
}
write_env /secrets/audit_k1 k1 "" ""

cat > "$REHEARSAL_ROOT/.compose-env" <<EOF
REHEARSAL_IMAGE=$IMG
REHEARSAL_ENV_FILE=$REHEARSAL_ROOT/.env
REHEARSAL_ROOT=$REHEARSAL_ROOT
REHEARSAL_WEB_PORT=$PORT
REHEARSAL_PG_PASSWORD=$PGPASS
EOF

# ---------------- 4. restore into the temp stack ----------------
echo "== starting temp postgres/redis + restoring dump =="
"${COMPOSE[@]}" up -d postgres redis >/dev/null 2>&1
ok=0
for _ in $(seq 1 60); do
  "${COMPOSE[@]}" exec -T postgres pg_isready -U archiver >/dev/null 2>&1 && { ok=1; break; }
  sleep 1
done
[ "$ok" = "1" ] || { echo "temp postgres not ready"; exit 1; }

if gunzip -c "$DUMP" | "${COMPOSE[@]}" exec -T postgres psql -q -v ON_ERROR_STOP=1 \
     -U archiver -d archiver >/dev/null; then
  item db_restore pass no "restored $DUMP_BASE into the temporary postgres"
else
  item db_restore fail no "psql restore of $DUMP_BASE failed"
  exit 1
fi

DBHEAD="$("${COMPOSE[@]}" exec -T postgres psql -U archiver -At -c \
  'SELECT version_num FROM alembic_version' | tr -d '\r')"
if [ "$DBHEAD" = "$HEAD_REV" ]; then
  item schema_head pass no "restored schema head $DBHEAD matches code head"
else
  item schema_head fail no "restored head=$DBHEAD expected=$HEAD_REV"
fi

JOBS_BEFORE="$("${COMPOSE[@]}" exec -T postgres psql -U archiver -At -c 'SELECT count(*) FROM jobs' | tr -d '\r')"

if "${COMPOSE[@]}" run --rm -T migrate >/dev/null 2>&1; then
  item migrate_noop pass no "alembic upgrade head is a no-op on the restored DB"
else
  item migrate_noop fail no "migrate service failed on the restored DB"
fi

# ---------------- 5. app up + readiness ----------------
"${COMPOSE[@]}" up -d web worker >/dev/null 2>&1
ok=0
for _ in $(seq 1 40); do
  curl -fsS --max-time 5 "$BASE/health/live" >/dev/null 2>&1 && { ok=1; break; }
  sleep 3
done
if [ "$ok" = "1" ]; then item readiness_live pass no "/health/live 200"; else
  item readiness_live fail no "/health/live did not come up"; exit 1; fi
RC="$(curl -s --max-time 10 -o /dev/null -w '%{http_code}' "$BASE/health/ready")"
if [ "$RC" = "200" ]; then item readiness_ready pass no "/health/ready 200 (db+redis+disk)"
else item readiness_ready fail no "/health/ready status=$RC"; fi

# ---------------- 6. audit verify + break-glass restore boundary -------------
# (MUST run before any login/API call that appends a SIGNED audit event —
#  the restored chain is legacy-unsigned and needs the explicit boundary first)
set +e
"${COMPOSE[@]}" exec -T web archiver audit verify >/dev/null 2>&1; RCA=$?
set -e
[ "$RCA" = "0" ] && item audit_verify_baseline pass no "restored chain verifies (legacy unsigned prefix tolerated)" \
  || item audit_verify_baseline fail no "audit verify exit=$RCA on restored chain"

set +e
"${COMPOSE[@]}" exec -T web archiver audit establish-signing-boundary \
  --type restore_boundary --dry-run >/dev/null 2>&1; R1=$?
"${COMPOSE[@]}" exec -T web archiver audit establish-signing-boundary \
  --type restore_boundary --reason-code restore_rehearsal --apply >/dev/null 2>&1; R2=$?
"${COMPOSE[@]}" exec -T web archiver audit establish-signing-boundary \
  --type restore_boundary --reason-code restore_rehearsal --dry-run >/dev/null 2>&1; R3=$?
"${COMPOSE[@]}" exec -T web archiver audit establish-signing-boundary \
  --type restore_boundary --reason-code restore_rehearsal --apply --confirm-restore >/dev/null 2>&1; R4=$?
"${COMPOSE[@]}" exec -T web archiver audit verify >/dev/null 2>&1; R5=$?
set -e
[ "$R1" = "2" ] && item restore_boundary_requires_reason pass no "restore_boundary without --reason-code refused (exit 2)" \
  || item restore_boundary_requires_reason fail no "missing reason-code exit=$R1 (expected 2)"
[ "$R2" = "2" ] && item restore_boundary_requires_confirm pass no "apply without --confirm-restore refused (exit 2)" \
  || item restore_boundary_requires_confirm fail no "apply without confirm exit=$R2 (expected 2)"
[ "$R3" = "0" ] && item restore_boundary_dry_run pass no "dry-run with reason-code succeeds without applying" \
  || item restore_boundary_dry_run fail no "dry-run exit=$R3"
[ "$R4" = "0" ] && item restore_boundary_applied pass no "break-glass apply with reason+confirm succeeded" \
  || item restore_boundary_applied fail no "apply exit=$R4"
[ "$R5" = "0" ] && item audit_verify_after_restore_boundary pass no "chain valid after restore_boundary + signed suffix" \
  || item audit_verify_after_restore_boundary fail no "verify exit=$R5 after boundary"

# ---------------- 7. auth / CSRF / metrics ----------------
RC="$(curl -s --max-time 10 -o /dev/null -w '%{http_code}' -X POST "$BASE/api/auth/login" \
  -H 'content-type: application/json' -d '{"password":"definitely-wrong-password"}')"
[ "$RC" = "401" ] && item auth_bad_password_rejected pass no "wrong password -> 401" \
  || item auth_bad_password_rejected fail no "wrong password -> $RC (expected 401)"

login() { # perform a login, refresh SESSION/CSRF vars
  printf '{"password":"%s"}' "$ADMIN_PW" > "$REHEARSAL_ROOT/login.json"
  curl -s --max-time 10 -D "$REHEARSAL_ROOT/login_headers.txt" -o /dev/null \
    -X POST "$BASE/api/auth/login" -H 'content-type: application/json' \
    -d @"$REHEARSAL_ROOT/login.json"
  SESSION="$(awk 'tolower($1)=="set-cookie:" && $2 ~ /^ytarch_session=/ {split($2,a,";");print a[1]}' \
    "$REHEARSAL_ROOT/login_headers.txt" | tail -1)"
  CSRF_COOKIE="$(awk 'tolower($1)=="set-cookie:" && $2 ~ /^ytarch_csrf=/ {split($2,a,";");print a[1]}' \
    "$REHEARSAL_ROOT/login_headers.txt" | tail -1)"
  CSRF_VAL="${CSRF_COOKIE#ytarch_csrf=}"
  COOKIES="$SESSION; $CSRF_COOKIE"
}
login
if [ -n "$SESSION" ] && [ -n "$CSRF_VAL" ]; then
  item auth_login_success pass no "local login issued session + csrf cookies"
else
  item auth_login_success fail no "login did not issue cookies"; exit 1
fi

RC="$(curl -s --max-time 10 -o /dev/null -w '%{http_code}' "$BASE/api/system/production-check")"
[ "$RC" = "401" ] && item auth_unauthenticated_rejected pass no "API without session -> 401" \
  || item auth_unauthenticated_rejected fail no "API without session -> $RC (expected 401)"

RC="$(curl -s --max-time 10 -o /dev/null -w '%{http_code}' -X POST "$BASE/api/takeout/preview" \
  -H "Cookie: $COOKIES" -H 'content-type: application/json' -d '{"path":"/takeout_imports/none.zip"}')"
[ "$RC" = "403" ] && item csrf_rejected_without_token pass no "mutation without X-CSRF-Token -> 403" \
  || item csrf_rejected_without_token fail no "mutation without token -> $RC (expected 403)"
RC="$(curl -s --max-time 10 -o /dev/null -w '%{http_code}' -X POST "$BASE/api/takeout/preview" \
  -H "Cookie: $COOKIES" -H "x-csrf-token: $CSRF_VAL" -H "Origin: $BASE" \
  -H 'content-type: application/json' -d '{"path":"/takeout_imports/none.zip"}')"
if [ "$RC" != "403" ] && [ "$RC" != "401" ]; then
  item csrf_accepted_with_token pass no "mutation with token passes CSRF (status=$RC, no side effects)"
else
  item csrf_accepted_with_token fail no "mutation with token -> $RC"
fi

RC="$(curl -s --max-time 10 -o /dev/null -w '%{http_code}' "$BASE/api/system/metrics")"
[ "$RC" = "401" ] && item metrics_protected pass no "metrics without session -> 401" \
  || item metrics_protected fail no "metrics without session -> $RC (expected 401)"
RC="$(curl -s --max-time 10 -o /dev/null -w '%{http_code}' -H "Cookie: $COOKIES" "$BASE/api/system/metrics")"
[ "$RC" = "200" ] && item metrics_authenticated pass no "metrics with session -> 200" \
  || item metrics_authenticated fail no "metrics with session -> $RC"

# ---------------- 8. preflight / production-check / release-check ------------
set +e
"${COMPOSE[@]}" exec -T web archiver system preflight >/dev/null 2>&1
RCP=$?
set -e
[ "$RCP" = "0" ] && item preflight pass no "system preflight exit=0" \
  || item preflight fail no "system preflight exit=$RCP"

PC_FAILS="$(curl -s --max-time 60 -H "Cookie: $COOKIES" "$BASE/api/system/production-check" \
  | $PY3 -c 'import json,sys
try:
    d=json.load(sys.stdin); print(",".join(sorted(c["name"] for c in d["checks"] if c["status"]=="fail")))
except Exception:
    print("PARSE_ERROR")')"
if [ -z "$PC_FAILS" ]; then
  item production_check pass no "production-check: no FAIL checks (warns expected in rehearsal)"
else
  item production_check fail no "production-check unexpected FAILs: $PC_FAILS"
fi

RL_FAILS="$(curl -s --max-time 120 -H "Cookie: $COOKIES" "$BASE/api/system/release-check" \
  | $PY3 -c 'import json,sys
try:
    d=json.load(sys.stdin); print(",".join(sorted(c["name"] for c in d["checks"] if c["status"]=="fail")))
except Exception:
    print("PARSE_ERROR")')"
if [ "$RL_FAILS" = "archive_media_presence" ]; then
  item release_check fail yes "release-check FAIL only from archive_media_presence (archive deliberately not attached)"
elif [ -z "$RL_FAILS" ]; then
  item release_check pass no "release-check: no FAIL checks"
else
  item release_check fail no "release-check unexpected FAILs: $RL_FAILS"
fi

# ---------------- 9. archive fixtures + archive-check + media Range ----------
FIXJSON="$("${COMPOSE[@]}" exec -T -e FIXTURES="$FIXTURES" web python - <<'PYEOF'
import json, os
from sqlalchemy import select
from app.config import get_settings
from app.db import get_session_factory
from app.models import MediaFile, Video
from app.services import storage

s = get_settings()
sess = get_session_factory()()
rows = sess.execute(
    select(MediaFile.id, MediaFile.path, Video.id)
    .join(Video, Video.id == MediaFile.video_id)
    .where(MediaFile.media_type == "video")
    .order_by(MediaFile.id.desc())
    .limit(int(os.environ.get("FIXTURES", "3")))).all()
out = []
for mid, rel, vid in rows:
    p = storage.to_absolute(s, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * 1048576)
    out.append({"media_file_id": mid, "video_db_id": vid})
print(json.dumps(out))
PYEOF
)"
NFIX="$(printf '%s' "$FIXJSON" | $PY3 -c 'import json,sys;print(len(json.load(sys.stdin)))')"
[ "$NFIX" = "$FIXTURES" ] && item media_fixtures pass no "created $NFIX dummy fixture files in the temp archive (no downloads)" \
  || item media_fixtures fail no "fixture creation returned $NFIX/$FIXTURES"

set +e
"${COMPOSE[@]}" exec -T web archiver system archive-check --limit "$FIXTURES" >/dev/null 2>&1; RCF=$?
AC_OUT="$("${COMPOSE[@]}" exec -T web archiver system archive-check 2>&1)"; RCFULL=$?
set -e
[ "$RCF" = "0" ] && item archive_check_fixture pass no "archive-check --limit $FIXTURES: all fixture-backed files present" \
  || item archive_check_fixture fail no "archive-check --limit exit=$RCF"
MISSING="$(printf '%s\n' "$AC_OUT" | awk '/missing files:/ {print $NF}' | tr -d '\r')"
TOTAL="$(printf '%s\n' "$AC_OUT" | awk '/DB video media_files:/ {print $NF}' | tr -d '\r')"
if [ "$RCFULL" != "0" ] && [ -n "$MISSING" ] && [ -n "$TOTAL" ] \
   && [ "$MISSING" = "$((TOTAL - FIXTURES))" ]; then
  item archive_check_full_isolated fail yes "full archive-check: $MISSING/$TOTAL missing — expected (real archive not attached)"
else
  item archive_check_full_isolated fail no "full archive-check unexpected result (exit=$RCFULL missing=$MISSING total=$TOTAL)"
fi

MID="$(printf '%s' "$FIXJSON" | $PY3 -c 'import json,sys;print(json.load(sys.stdin)[0]["media_file_id"])')"
VID="$(printf '%s' "$FIXJSON" | $PY3 -c 'import json,sys;print(json.load(sys.stdin)[0]["video_db_id"])')"
RANGE_HDRS="$(curl -s --max-time 15 -D - -o /dev/null -H "Cookie: $COOKIES" \
  -H "Range: bytes=0-99" "$BASE/api/videos/$VID/media/$MID")"
if printf '%s' "$RANGE_HDRS" | head -1 | grep -q " 206" \
   && printf '%s' "$RANGE_HDRS" | grep -qi "^content-range: bytes 0-99/"; then
  item media_range_206 pass no "media endpoint honours Range (206 + Content-Range) on fixture file"
else
  RC_LINE="$(printf '%s' "$RANGE_HDRS" | head -1 | tr -d '\r')"
  item media_range_206 fail no "Range request did not return 206 ($RC_LINE)"
fi

# ---------------- 10. duplicates / orphans / no new jobs ---------------------
set +e
DUP_OUT="$("${COMPOSE[@]}" exec -T web archiver storage media-duplicates 2>&1)"; RCD=$?
ORP_OUT="$("${COMPOSE[@]}" exec -T web archiver jobs reconcile-orphans 2>&1)"; RCO=$?
set -e
if [ "$RCD" = "0" ]; then
  item duplicate_check pass no "duplicate video media check ran (none expected on restored DB)"
else
  item duplicate_check fail no "media-duplicates exit=$RCD"
fi
if [ "$RCO" = "0" ]; then
  item orphan_dry_run pass no "reconcile-orphans dry-run ran against restored DB + empty Redis (AOF-not-restored policy)"
else
  item orphan_dry_run fail no "reconcile-orphans exit=$RCO"
fi

# ---------------- 11. key rotation + recovery matrix -------------------------
PSEUDO_BEFORE="$(curl -s --max-time 10 -H "Cookie: $COOKIES" \
  "$BASE/api/audit/events?event_type=login_success&limit=1" \
  | $PY3 -c 'import json,sys
try:
    print(json.load(sys.stdin)["events"][0]["actor_id_hash"] or "")
except Exception:
    print("")')"

mv "$S/audit_k2_pending" "$S/audit_k2"
write_env /secrets/audit_k2 k2 /secrets/audit_k1 k1
"${COMPOSE[@]}" up -d --force-recreate web worker >/dev/null 2>&1
ok=0
for _ in $(seq 1 40); do
  curl -fsS --max-time 5 "$BASE/health/live" >/dev/null 2>&1 && { ok=1; break; }
  sleep 3
done
[ "$ok" = "1" ] || { item key_rotation_applied fail no "web did not restart after key swap"; exit 1; }

set +e
"${COMPOSE[@]}" exec -T web archiver audit rotate-key --apply >/dev/null 2>&1; RCR=$?
"${COMPOSE[@]}" exec -T web archiver audit log-op --event rehearsal_post_rotation --category ops >/dev/null 2>&1
"${COMPOSE[@]}" exec -T web archiver audit verify >/dev/null 2>&1; RCV=$?
set -e
[ "$RCR" = "0" ] && item key_rotation_applied pass no "key_rotated boundary applied (k1 -> k2)" \
  || item key_rotation_applied fail no "rotate-key exit=$RCR"
[ "$RCV" = "0" ] && item verify_current_plus_previous pass no "verify PASS with current k2 + previous k1" \
  || item verify_current_plus_previous fail no "verify exit=$RCV after rotation"

mv "$S/audit_k1" "$S/audit_k1.away"
set +e
MISS_OUT="$("${COMPOSE[@]}" exec -T web archiver audit verify 2>&1)"; RCM=$?
set -e
if [ "$RCM" != "0" ] && printf '%s' "$MISS_OUT" | grep -q "missing"; then
  item verify_missing_previous_key fail yes "previous key absent -> verify FAILs with missing verification key (expected)"
else
  item verify_missing_previous_key fail no "missing-key scenario: exit=$RCM (expected non-zero + missing key reason)"
fi
mv "$S/audit_k1.away" "$S/audit_k1"
set +e
"${COMPOSE[@]}" exec -T web archiver audit verify >/dev/null 2>&1; RCK=$?
set -e
[ "$RCK" = "0" ] && item verify_previous_key_restored pass no "previous key restored -> verify PASS" \
  || item verify_previous_key_restored fail no "verify exit=$RCK after restoring previous key"

cp "$S/audit_k2" "$S/audit_k2.bak"
head -c 32 /dev/urandom | base64 > "$S/audit_k2"
set +e
"${COMPOSE[@]}" exec -T web archiver audit verify >/dev/null 2>&1; RCW=$?
set -e
[ "$RCW" != "0" ] && item verify_wrong_current_key fail yes "wrong current key value -> verify FAILs (expected)" \
  || item verify_wrong_current_key fail no "wrong current key unexpectedly verified"
mv "$S/audit_k2.bak" "$S/audit_k2"
set +e
"${COMPOSE[@]}" exec -T web archiver audit verify >/dev/null 2>&1; RCK2=$?
set -e
[ "$RCK2" = "0" ] && item verify_current_key_restored pass no "correct current key restored -> verify PASS" \
  || item verify_current_key_restored fail no "verify exit=$RCK2 after restoring current key"

# ---------------- 12. pseudonym-key separation -------------------------------
login
PSEUDO_AFTER="$(curl -s --max-time 10 -H "Cookie: $COOKIES" \
  "$BASE/api/audit/events?event_type=login_success&limit=1" \
  | $PY3 -c 'import json,sys
try:
    print(json.load(sys.stdin)["events"][0]["actor_id_hash"] or "")
except Exception:
    print("")')"
if [ -n "$PSEUDO_BEFORE" ] && [ "$PSEUDO_BEFORE" = "$PSEUDO_AFTER" ]; then
  item pseudonym_stable_across_rotation pass no "actor pseudonym unchanged across signing-key rotation"
else
  item pseudonym_stable_across_rotation fail no "pseudonym changed across signing rotation (must not)"
fi

cp "$S/audit_pseudonym" "$S/audit_pseudonym.bak"
head -c 32 /dev/urandom | base64 > "$S/audit_pseudonym"
login
PSEUDO_NEWKEY="$(curl -s --max-time 10 -H "Cookie: $COOKIES" \
  "$BASE/api/audit/events?event_type=login_success&limit=1" \
  | $PY3 -c 'import json,sys
try:
    print(json.load(sys.stdin)["events"][0]["actor_id_hash"] or "")
except Exception:
    print("")')"
if [ -n "$PSEUDO_NEWKEY" ] && [ "$PSEUDO_NEWKEY" != "$PSEUDO_BEFORE" ]; then
  item pseudonym_key_change_changes_hash pass no "pseudonym key replacement changes new pseudonyms (correlation break documented)"
else
  item pseudonym_key_change_changes_hash fail no "pseudonym did not change after key replacement"
fi
set +e
"${COMPOSE[@]}" exec -T web archiver audit verify >/dev/null 2>&1; RCPC=$?
set -e
[ "$RCPC" = "0" ] && item chain_valid_after_pseudonym_change pass no "hash chain unaffected by pseudonym key change" \
  || item chain_valid_after_pseudonym_change fail no "verify exit=$RCPC after pseudonym key change"
mv "$S/audit_pseudonym.bak" "$S/audit_pseudonym"

# ---------------- 13. no new download jobs -----------------------------------
JOBS_AFTER="$("${COMPOSE[@]}" exec -T postgres psql -U archiver -At -c 'SELECT count(*) FROM jobs' | tr -d '\r')"
ACTIVE_AFTER="$("${COMPOSE[@]}" exec -T postgres psql -U archiver -At -c \
  "SELECT count(*) FROM jobs WHERE status IN ('queued','running')" | tr -d '\r')"
if [ "$JOBS_AFTER" = "$JOBS_BEFORE" ] && [ "$ACTIVE_AFTER" = "0" ]; then
  item no_new_jobs pass no "job count unchanged ($JOBS_AFTER) and 0 active — no downloads triggered"
else
  item no_new_jobs fail no "jobs before=$JOBS_BEFORE after=$JOBS_AFTER active=$ACTIVE_AFTER"
fi

# ---------------- 14. teardown + real-stack post-conditions ------------------
teardown
trap - EXIT

if docker volume inspect "${REAL_PROJECT}_pgdata" "${REAL_PROJECT}_redisdata" >/dev/null 2>&1; then
  item real_volumes_intact pass no "youtube_archiver pgdata + redisdata volumes still present"
else
  item real_volumes_intact fail no "REAL VOLUMES MISSING — investigate immediately"
fi
RUNNING="$(docker compose -p "$REAL_PROJECT" ps -q 2>/dev/null | wc -l | tr -d ' ')"
if [ "$RUNNING" -ge 5 ]; then
  item real_stack_running pass no "current stack still running ($RUNNING containers)"
else
  item real_stack_running fail no "current stack containers running: $RUNNING (expected >=5)"
fi
LEFT="$(docker ps -a --format '{{.Names}}' | grep -c "^${PROJ}" || true)"
[ "$LEFT" = "0" ] && item rehearsal_cleaned_up pass no "no rehearsal containers left" \
  || item rehearsal_cleaned_up fail no "$LEFT rehearsal containers left"

# ---------------- 15. machine-readable acceptance report ---------------------
REPORT="$REPORT_DIR/restore-acceptance-$TS.json"
if $PY3 scripts/restore_acceptance_report.py --items "$ITEMS" --project "$PROJ" \
     --dump "$DUMP_BASE" --out "$REPORT"; then
  mkdir -p "$(dirname "$MARKER_HOST_FILE")"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$MARKER_HOST_FILE"
  echo "restore rehearsal PASSED — report: $REPORT ; marker touched: $MARKER_HOST_FILE"
  rm -f "$ITEMS"
else
  echo "restore rehearsal FAILED — report: $REPORT (marker NOT touched)"
  rm -f "$ITEMS"
  exit 1
fi
