#!/usr/bin/env bash
# Phase 9D: production deploy helper. Non-destructive — never removes volumes,
# never runs `down -v`. Set DRY_RUN=1 to print the steps without executing.
# Secret values are never echoed.
set -euo pipefail

DRY_RUN="${DRY_RUN:-0}"
WEB_HOST_PORT="${WEB_HOST_PORT:-8000}"
# Phase 9F.2: after recreate, a just-stopped worker's heartbeat lingers in Redis
# until its TTL expires, so the final preflight's worker_build_match would flap.
# Poll `system worker-convergence` until the fleet converges (configurable) — NOT
# a fixed sleep — before running preflight/release-check once.
WORKER_CONVERGENCE_TIMEOUT_SECONDS="${WORKER_CONVERGENCE_TIMEOUT_SECONDS:-150}"
WORKER_CONVERGENCE_POLL_SECONDS="${WORKER_CONVERGENCE_POLL_SECONDS:-5}"
# Compose file selection (Phase 9F.1): production uses the hardening overlay;
# when it does not exist (development stack) fall back to the base file so the
# same non-destructive deploy path works in both environments.
if [ -f "docker-compose.production.yml" ]; then
  COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.production.yml)
else
  echo "note: docker-compose.production.yml not found — deploying the base compose file (development)"
  COMPOSE=(docker compose -f docker-compose.yml)
fi

run() { echo "+ $*"; [ "$DRY_RUN" = "1" ] || "$@"; }
# best-effort audit (never fatal; skipped in dry-run)
audit_op() { [ "$DRY_RUN" = "1" ] || "${COMPOSE[@]}" exec -T web archiver audit log-op --event "$1" --outcome "${2:-success}" --severity "${3:-info}" >/dev/null 2>&1 || true; }
trap 'audit_op deploy_failed failure warning' ERR

audit_op deploy_started
echo "== 1/7 pre-deploy backup =="
run ./scripts/backup.sh

echo "== 2/7 build images =="
run "${COMPOSE[@]}" build

echo "== 3/7 apply migrations (one-shot) =="
run "${COMPOSE[@]}" run --rm migrate

echo "== 4/7 recreate services (volumes preserved) =="
run "${COMPOSE[@]}" up -d --remove-orphans

echo "== 5/7 wait for /health (loopback) =="
if [ "$DRY_RUN" != "1" ]; then
  ok=0
  for _ in $(seq 1 40); do
    if curl -fsS "http://127.0.0.1:${WEB_HOST_PORT}/health" >/dev/null 2>&1; then ok=1; break; fi
    sleep 3
  done
  [ "$ok" = "1" ] || { echo "web did not become healthy in time"; exit 1; }
  echo "  web healthy"
fi

echo "== 6/7 wait for worker convergence (new build; timeout ${WORKER_CONVERGENCE_TIMEOUT_SECONDS}s, poll ${WORKER_CONVERGENCE_POLL_SECONDS}s) =="
if [ "$DRY_RUN" != "1" ]; then
  # Poll the machine-readable convergence check: exit 0 = converged. A lingering
  # stale registration keeps it at exit 1 → we WAIT (it expires via TTL). A real
  # persistent build mismatch or missing worker stays not-ready until timeout.
  converged=0
  deadline=$(( SECONDS + WORKER_CONVERGENCE_TIMEOUT_SECONDS ))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if "${COMPOSE[@]}" exec -T web archiver system worker-convergence >/dev/null 2>&1; then
      converged=1; break
    fi
    sleep "$WORKER_CONVERGENCE_POLL_SECONDS"
  done
  if [ "$converged" != "1" ]; then
    echo "  worker fleet did NOT converge within ${WORKER_CONVERGENCE_TIMEOUT_SECONDS}s:"
    "${COMPOSE[@]}" exec -T web archiver system worker-convergence --json || true
    audit_op deploy_failed failure warning     # deploy_failed ONLY on real timeout
    exit 1
  fi
  "${COMPOSE[@]}" exec -T web archiver system worker-convergence || true
  echo "  worker fleet converged on the new build"
fi

echo "== 7/7 preflight + release-check =="
run "${COMPOSE[@]}" exec -T web archiver system preflight
run "${COMPOSE[@]}" exec -T web archiver system release-check

audit_op deploy_succeeded
echo "deploy complete."
