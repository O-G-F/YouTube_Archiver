#!/usr/bin/env bash
# Container entrypoint. First argument selects the role.
set -euo pipefail

cmd="${1:-web}"

case "$cmd" in
  web)
    echo "[entrypoint] starting web (uvicorn) ..."
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000
    ;;
  worker)
    echo "[entrypoint] starting RQ worker ..."
    exec archiver worker
    ;;
  migrate)
    echo "[entrypoint] applying migrations ..."
    alembic upgrade head
    echo "[entrypoint] seeding built-in profiles ..."
    python -c "from app.bootstrap import seed; print('[entrypoint] profiles seeded/updated:', seed())"
    echo "[entrypoint] migrate complete."
    ;;
  *)
    # Fall through: run an arbitrary command (e.g. `archiver jobs list`).
    exec "$@"
    ;;
esac
