#!/usr/bin/env bash
# =====================================================================
# Phase 10A: build a RELEASE CANDIDATE — provenance + supply-chain artifacts.
# =====================================================================
# Produces (under ./release/<ts>/): per-service image identity, SBOM,
# vulnerability scan, and a signed release manifest that lets the SAME commit be
# rebuilt and re-verified later. NON-DESTRUCTIVE: it builds images and writes
# artifacts only — it NEVER recreates services, touches volumes, runs
# `down -v`, deletes archives, or downloads videos. Deploy is a separate step
# (scripts/deploy.sh). Secret values are never echoed.
#
# usage: ./scripts/build-release.sh [vX.Y.Z]
#   env: DRY_RUN=1                 print the plan, do nothing
#        RELEASE_SKIP_TESTS=1      skip backend/frontend tests (NOT for a real RC)
#        RELEASE_SKIP_SCAN=1       record scanner status=unavailable, don't run it
#        RELEASE_SCAN_TIMEOUT=600  seconds for the trivy scan
#        BASE_PYTHON_IMAGE / BASE_NODE_IMAGE   pin base images (tag or @sha256)
set -euo pipefail

DRY_RUN="${DRY_RUN:-0}"
PY="${PY:-.venv/bin/python}"
COMPOSE=(docker compose -f docker-compose.yml)
SERVICES=(web worker scheduler migrate)
IMAGE_PREFIX="youtube_archiver"
TS="$(date -u +%Y%m%d-%H%M%S)"
REL_VERSION="${1:-${APP_VERSION:-0.0.0-dev}}"
OUT_DIR="release/${TS}"
RELEASE_SCAN_TIMEOUT="${RELEASE_SCAN_TIMEOUT:-600}"
BASE_PYTHON_IMAGE="${BASE_PYTHON_IMAGE:-python:3.12-slim}"
BASE_NODE_IMAGE="${BASE_NODE_IMAGE:-node:20-slim}"

[ -f "Dockerfile" ] && [ -f "pyproject.toml" ] || { echo "run from the repo root"; exit 2; }
command -v "$PY" >/dev/null 2>&1 || PY="python3"

run() { echo "+ $*"; [ "$DRY_RUN" = "1" ] || "$@"; }

# Portable timeout wrapper (macOS has no coreutils `timeout`). Kills the command
# if it exceeds N seconds and returns 124 — used for best-effort tools (docker
# scout SBOM) that can hang on a network call in an offline environment.
_with_timeout() {
  local secs="$1"; shift
  "$@" & local _pid=$!
  ( sleep "$secs"; kill -9 "$_pid" 2>/dev/null ) & local _watch=$!
  wait "$_pid" 2>/dev/null; local _rc=$?
  kill "$_watch" 2>/dev/null; wait "$_watch" 2>/dev/null
  return "$_rc"
}
SBOM_TIMEOUT="${RELEASE_SBOM_TIMEOUT:-120}"

echo "== Phase 10A release build: version=${REL_VERSION} out=${OUT_DIR} =="
if [ "$DRY_RUN" = "1" ]; then
  echo "  (dry-run) steps: clean-check → guard → backend tests → frontend tests →"
  echo "  frontend build → migration rehearsal summary → docker build → image inspect →"
  echo "  SBOM → vuln scan → release manifest → verify → release-check → marker"
  exit 0
fi
mkdir -p "$OUT_DIR"

# ---- 1. working tree clean ----
echo "== 1/14 working tree clean =="
GIT_COMMIT="$(git rev-parse HEAD 2>/dev/null || echo '')"
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  echo "  working tree is DIRTY — commit or stash before cutting a release candidate"; exit 1
fi
GIT_TREE_CLEAN=1
echo "  clean @ ${GIT_COMMIT:0:12}"

# ---- 2. secret / temp artifact guard ----
echo "== 2/14 secret / artifact guard =="
for bad in .env secrets cookies.txt; do
  if git ls-files --error-unmatch "$bad" >/dev/null 2>&1; then
    echo "  REFUSING: $bad is tracked in git"; exit 1
  fi
done
git check-ignore release/ >/dev/null 2>&1 || { echo "  REFUSING: release/ is not gitignored"; exit 1; }
echo "  ok (no tracked secrets; release/ gitignored)"

# ---- 3. backend tests ----
echo "== 3/14 backend tests =="
BACKEND_TESTS=0
if [ "${RELEASE_SKIP_TESTS:-0}" = "1" ]; then
  echo "  skipped (RELEASE_SKIP_TESTS=1)"
else
  "$PY" -m pytest tests/ -q -p no:warnings > "$OUT_DIR/backend-tests.log" 2>&1 \
    || { tail -5 "$OUT_DIR/backend-tests.log"; echo "  backend tests FAILED"; exit 1; }
  BACKEND_TESTS="$(grep -oE '[0-9]+ passed' "$OUT_DIR/backend-tests.log" | head -1 | grep -oE '[0-9]+' || echo 0)"
  echo "  ${BACKEND_TESTS} passed"
fi

# ---- 4. frontend tests + 5. frontend build ----
echo "== 4/14 frontend tests =="
FRONTEND_TESTS=0
if [ "${RELEASE_SKIP_TESTS:-0}" = "1" ]; then
  echo "  skipped"
else
  ( cd frontend && npm test -- --run ) > "$OUT_DIR/frontend-tests.log" 2>&1 \
    || { tail -5 "$OUT_DIR/frontend-tests.log"; echo "  frontend tests FAILED"; exit 1; }
  FRONTEND_TESTS="$(grep -oE 'Tests +[0-9]+ passed' "$OUT_DIR/frontend-tests.log" | grep -oE '[0-9]+' | head -1 || echo 0)"
  echo "  ${FRONTEND_TESTS} passed"
fi
echo "== 5/14 frontend production build =="
( cd frontend && npm run build ) > "$OUT_DIR/frontend-build.log" 2>&1 \
  || { tail -5 "$OUT_DIR/frontend-build.log"; echo "  frontend build FAILED"; exit 1; }
FRONTEND_BUILD_ID="$("$PY" -c "from app.services.build_info import frontend_build_id; print(frontend_build_id() or '')" 2>/dev/null || echo '')"
echo "  built ${FRONTEND_BUILD_ID}"

# ---- 6. migration rehearsal summary (Phase 9F reports) ----
echo "== 6/14 migration rehearsal summary =="
REHEARSAL_JSON="$OUT_DIR/migration-rehearsal.json"
"$PY" - "$REHEARSAL_JSON" <<'PY'
import glob, hashlib, json, sys
out = sys.argv[1]
def latest(kind):
    files = sorted(glob.glob(f"backups/rehearsals/migration-{kind}-*.json"))
    if not files:
        return None
    p = files[-1]
    data = json.load(open(p))
    return {"report": p.split("/")[-1], "ok": bool(data.get("ok")),
            "generated_at": data.get("generated_at"),
            "sha256": hashlib.sha256(open(p, "rb").read()).hexdigest()}
fresh, upgrade = latest("fresh"), latest("upgrade")
json.dump({"fresh": fresh, "upgrade": upgrade,
           "present": bool(fresh and upgrade)}, open(out, "w"), indent=2)
print("  fresh:", "ok" if fresh and fresh["ok"] else "missing/failed",
      "| upgrade:", "ok" if upgrade and upgrade["ok"] else "missing/failed")
PY

# ---- 7. resolve base-image digests + docker build (digest-pinned FROM) ----
# Phase 10A.1: a release must NOT build FROM a floating tag. Resolve each base
# image's immutable digest and pass `<tag>@sha256:...` as the FROM build arg.
# If a digest cannot be resolved and RELEASE_REQUIRE_DIGEST!=0, FAIL (a release
# candidate is not reproducible without pinned bases). Digests are resolved from
# what docker actually has/pulls — never guessed/hard-coded in code.
resolve_base_digest() {  # $1=tag(or tag@digest) -> prints "<tag>@sha256:..." or ""
  local tag="${1%%@*}"
  if [ "$1" != "$tag" ]; then echo "$1"; return; fi   # already digest-pinned
  # Prefer the registry digest of the local image; pull first if absent.
  docker image inspect "$tag" >/dev/null 2>&1 || docker pull -q "$tag" >/dev/null 2>&1 || true
  local rd
  rd="$(docker image inspect "$tag" --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' 2>/dev/null || echo '')"
  # RepoDigests is like "python@sha256:..."; recompose as "<tag>@sha256:..."
  local d="${rd##*@}"
  [ -n "$d" ] && [ "$d" != "$rd" ] && echo "${tag}@${d}" || echo ""
}
echo "== 7/14 resolve base digests + docker build =="
PY_PINNED="$(resolve_base_digest "$BASE_PYTHON_IMAGE")"
NODE_PINNED="$(resolve_base_digest "$BASE_NODE_IMAGE")"
if [ "${RELEASE_REQUIRE_DIGEST:-1}" = "1" ]; then
  [ -n "$PY_PINNED" ]   || { echo "  FAIL: could not resolve python base digest (release requires a pinned base)"; exit 1; }
  [ -n "$NODE_PINNED" ] || { echo "  FAIL: could not resolve node base digest (release requires a pinned base)"; exit 1; }
fi
PY_PINNED="${PY_PINNED:-$BASE_PYTHON_IMAGE}"     # dev fallback (RELEASE_REQUIRE_DIGEST=0)
NODE_PINNED="${NODE_PINNED:-$BASE_NODE_IMAGE}"
echo "  base python: ${PY_PINNED}"
echo "  base node:   ${NODE_PINNED}"
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
APP_BUILD_ID="$("$PY" -c "from app.services.build_info import build_id; print(build_id())" 2>/dev/null || echo '')"
"${COMPOSE[@]}" build \
  --build-arg "BASE_PYTHON_IMAGE=${PY_PINNED}" \
  --build-arg "BASE_NODE_IMAGE=${NODE_PINNED}" \
  --build-arg "APP_VERSION=${REL_VERSION}" \
  --build-arg "APP_GIT_COMMIT=${GIT_COMMIT}" \
  --build-arg "APP_BUILD_TIME=${BUILD_TIME}" \
  --build-arg "APP_BUILD_ID=${APP_BUILD_ID}" \
  --build-arg "APP_GIT_TREE_CLEAN=1" \
  --build-arg "APP_FRONTEND_BUILD_ID=${FRONTEND_BUILD_ID}" \
  > "$OUT_DIR/docker-build.log" 2>&1 \
  || { tail -15 "$OUT_DIR/docker-build.log"; echo "  docker build FAILED"; exit 1; }
echo "  built ${#SERVICES[@]} service images"

# ---- 8. image inspect (id / digest / build id per service) ----
echo "== 8/14 image inspect =="
IMAGES_JSON="$OUT_DIR/images.json"
BASE_IMAGES_JSON="$OUT_DIR/base-images.json"
{
  echo "{"
  first=1
  for svc in "${SERVICES[@]}"; do
    img="${IMAGE_PREFIX}-${svc}:latest"
    iid="$(docker image inspect "$img" --format '{{.Id}}' 2>/dev/null || echo '')"
    digest="$(docker image inspect "$img" --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' 2>/dev/null || echo '')"
    bid="$(docker run --rm "$img" python -c 'from app.services.build_info import build_id; print(build_id())' 2>/dev/null || echo '')"
    [ "$first" = "1" ] || echo ","
    first=0
    printf '  "%s": {"name": "%s", "image_id": "%s", "image_digest": %s, "build_id": "%s"}' \
      "$svc" "$img" "$iid" "$( [ -n "$digest" ] && printf '"%s"' "$digest" || printf 'null' )" "$bid"
  done
  echo ""
  echo "}"
} > "$IMAGES_JSON"
# requested tag, the resolved digest-pinned ref (used at build), and the actual
# base image id — so verify can confirm the release built FROM the pinned digest.
PY_ACTUAL="$(docker image inspect "${PY_PINNED}" --format '{{.Id}}' 2>/dev/null || echo '')"
NODE_ACTUAL="$(docker image inspect "${NODE_PINNED}" --format '{{.Id}}' 2>/dev/null || echo '')"
printf '{"python": {"requested": "%s", "ref": "%s", "digest": "%s", "image_id": "%s"}, "node": {"requested": "%s", "ref": "%s", "digest": "%s", "image_id": "%s"}}\n' \
  "$BASE_PYTHON_IMAGE" "$PY_PINNED" "$PY_PINNED" "$PY_ACTUAL" \
  "$BASE_NODE_IMAGE" "$NODE_PINNED" "$NODE_PINNED" "$NODE_ACTUAL" > "$BASE_IMAGES_JSON"
echo "  recorded $(echo "${SERVICES[@]}" | wc -w | tr -d ' ') service images + digest-pinned base refs"

# ---- 9. SBOM (docker scout -> Trivy offline fallback -> syft; never abort) ----
# Best-effort but REAL. Order: docker scout (SPDX) -> Trivy offline CycloneDX
# (from the cached image, --skip-db-update) -> syft (SPDX). Each attempt is
# bounded by SBOM_TIMEOUT so an offline network hang can't stall the build. A
# produced SBOM is LEAK-SCANNED (host paths / secrets) before it is accepted; a
# leaking SBOM is discarded. Nothing here fakes a pass — a missing SBOM is
# recorded `unavailable` and release-check FAILs it in production.
echo "== 9/14 SBOM =="
SBOM_JSON="$OUT_DIR/sbom-descriptor.json"
WEB_IMAGE="${IMAGE_PREFIX}-web:latest"
REPO_ROOT="$(pwd)"
set +e

# Accept a generated SBOM iff non-empty AND leak-free; write the descriptor and
# return 0, else return 1. Args: <file> <tool> <format> <version>
_accept_sbom() {
  local f="$1" tool="$2" fmt="$3" ver="$4"
  [ -s "$f" ] || return 1
  if ! "$PY" -m app.cli release scan-artifact-leaks --path "$f" --repo-root "$REPO_ROOT" >/dev/null 2>&1; then
    echo "  SBOM ($tool) contained a host path/secret — DISCARDED (NOT shipped)"
    rm -f "$f"
    printf '{"tool": "%s", "status": "unavailable", "reason": "sbom_leak_detected"}\n' "$tool" > "$SBOM_JSON"
    return 1
  fi
  local sha; sha="$("$PY" -c "import hashlib;print(hashlib.sha256(open('$f','rb').read()).hexdigest())")"
  printf '{"tool": "%s", "tool_version": "%s", "format": "%s", "artifact": "%s", "sha256": "%s", "image": "%s"}\n' \
    "$tool" "${ver:-unknown}" "$fmt" "$(basename "$f")" "$sha" "$WEB_IMAGE" > "$SBOM_JSON"
  echo "  SBOM ($tool, $fmt) sha256=${sha:0:12}…"
  return 0
}

SBOM_DONE=0
# 1) docker scout (SPDX)
if [ "${RELEASE_SKIP_SBOM:-0}" != "1" ] && docker scout version >/dev/null 2>&1; then
  SCOUT_VER="$(docker scout version 2>/dev/null | awk -F': ' '/version/{print $2; exit}')"
  _with_timeout "$SBOM_TIMEOUT" docker scout sbom --format spdx --output "$OUT_DIR/sbom.spdx.json" "$WEB_IMAGE" >/dev/null 2>&1
  _accept_sbom "$OUT_DIR/sbom.spdx.json" "docker-scout" "spdx-json" "$SCOUT_VER" && SBOM_DONE=1
fi
# 2) Trivy offline CycloneDX fallback (from the cached image; no DB update needed)
if [ "$SBOM_DONE" != "1" ] && [ "${RELEASE_SKIP_SBOM:-0}" != "1" ]; then
  read -r _TREF _TID <<<"$(docker image ls --format '{{.Repository}}:{{.Tag}} {{.ID}}' \
    | awk '$1 ~ /(^|\/)aquasec\/trivy:|(^|\/)trivy:/ {print ($1 ~ /<none>/ ? $2 : $1), $2; exit}')"
  if [ -n "$_TREF" ]; then
    _TVER="$(docker run --rm "$_TREF" --version 2>/dev/null | awk '/Version/{print $2; exit}')"
    _with_timeout "$SBOM_TIMEOUT" docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
      -v "${TRIVY_DB_VOLUME:-trivy_db_cache}:/root/.cache" "$_TREF" image \
      --format cyclonedx --skip-db-update --quiet "$WEB_IMAGE" > "$OUT_DIR/sbom.cdx.json" 2>/dev/null
    _accept_sbom "$OUT_DIR/sbom.cdx.json" "trivy" "cyclonedx-json" "$_TVER" && SBOM_DONE=1
    echo "  (SBOM via Trivy offline fallback: docker scout unavailable/failed)"
  fi
fi
# 3) syft (SPDX) final fallback
if [ "$SBOM_DONE" != "1" ] && command -v syft >/dev/null 2>&1; then
  syft "$WEB_IMAGE" -o spdx-json="$OUT_DIR/sbom.spdx.json" >/dev/null 2>&1
  _accept_sbom "$OUT_DIR/sbom.spdx.json" "syft" "spdx-json" "" && SBOM_DONE=1
fi
# nothing produced a clean SBOM (and no leak descriptor already written)
if [ "$SBOM_DONE" != "1" ] && [ ! -s "$SBOM_JSON" ]; then
  printf '{"tool": null, "status": "unavailable", "reason": "no_sbom_tool_succeeded"}\n' > "$SBOM_JSON"
  echo "  no SBOM produced — recorded unavailable (release-check will flag)"
fi
set -e

# ---- 9b. apt reproducibility (record the exact runtime package set) ----
# Phase 10B.2: capture the runtime dpkg package set + a sha256 over it so a
# rebuild from the SAME base digest can be checked for apt drift. We do NOT
# snapshot-pin apt, so this is RECORDED (never claimed "fully reproducible"):
# deps/base are fixed, but the apt transaction can differ if the Debian repo moved.
APT_JSON="$OUT_DIR/apt-descriptor.json"
DPKG_TXT="$OUT_DIR/dpkg-packages.txt"
docker run --rm --entrypoint dpkg-query "$WEB_IMAGE" -W -f='${Package}\t${Version}\n' 2>/dev/null | sort > "$DPKG_TXT"
if [ -s "$DPKG_TXT" ]; then
  "$PY" - "$DPKG_TXT" "$APT_JSON" <<'PY'
import hashlib, json, sys
txt_path, out = sys.argv[1], sys.argv[2]
raw = open(txt_path, "rb").read()
pkgs = dict(l.split("\t", 1) for l in raw.decode("utf-8", "replace").splitlines() if "\t" in l)
targeted = {k: pkgs[k] for k in ("libgbm1", "libgl1-mesa-dri", "libglx-mesa0",
                                 "mesa-libgallium", "ffmpeg", "libpq5") if k in pkgs}
sha = hashlib.sha256(raw).hexdigest()
json.dump({"source": "dpkg-query", "package_count": len(pkgs), "sha256": sha,
           "targeted_versions": targeted, "pinned": False,
           "reproducibility": "deps_and_base_fixed_apt_not_pinned"},
          open(out, "w"), indent=2, sort_keys=True)
print(f"  apt: {len(pkgs)} pkgs sha256={sha[:12]}… "
      f"(mesa/ffmpeg/libpq5 versions recorded; NOT snapshot-pinned)")
PY
else
  APT_JSON=""; echo "  apt: could not read dpkg from image — apt descriptor omitted"
fi

# ---- 10. vulnerability scan (trivy, offline via a pre-fetched DB cache) ----
# Phase 10A.1: run a REAL scan using the pre-populated ${TRIVY_DB_VOLUME} cache
# (`trivy image --download-db-only`) with --skip-db-update so it works offline
# and deterministically. Record the scanner image identity (id + RepoDigest),
# version, and the DB's UpdatedAt. A DB-less env still records `unavailable`
# (never a fake pass). Never aborts the build.
echo "== 10/14 vulnerability scan =="
SCAN_JSON="$OUT_DIR/scan-descriptor.json"
TRIVY_DB_VOLUME="${TRIVY_DB_VOLUME:-trivy_db_cache}"
set +e
# Resolve a runnable trivy ref: repo:tag, else the image ID when the tag is <none>.
read -r TRIVY_REF TRIVY_ID <<<"$(docker image ls --format '{{.Repository}}:{{.Tag}} {{.ID}}' \
  | awk '$1 ~ /(^|\/)aquasec\/trivy:|(^|\/)trivy:/ {print ($1 ~ /<none>/ ? $2 : $1), $2; exit}')"
TRIVY_IMG="$TRIVY_REF"
# Phase 10B.2: record the FULL content id (.Id = sha256:...), NOT the 12-hex short
# id from `docker image ls`, and the REAL registry RepoDigest (.RepoDigests[0],
# empty if the scanner image was loaded locally). Never synthesize a digest from
# the id — the CLI classifier rejects that and degrades to `unverified`.
TRIVY_FULL_ID="$( [ -n "$TRIVY_ID" ] && docker image inspect "$TRIVY_ID" --format '{{.Id}}' 2>/dev/null || echo '')"
TRIVY_DIGEST="$( [ -n "$TRIVY_ID" ] && docker image inspect "$TRIVY_ID" --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' 2>/dev/null || echo '')"
DB_PRESENT="$(docker run --rm -v "${TRIVY_DB_VOLUME}:/root/.cache" alpine sh -c 'test -f /root/.cache/trivy/db/trivy.db && echo yes || echo no' 2>/dev/null)"
# --skip-db-update omits DB metadata from the scan JSON, so read UpdatedAt from
# the cache volume's metadata.json (authoritative DB freshness for the report).
DB_UPDATED="$(docker run --rm -v "${TRIVY_DB_VOLUME}:/root/.cache" alpine cat /root/.cache/trivy/db/metadata.json 2>/dev/null \
  | "$PY" -c 'import json,sys
try: print((json.load(sys.stdin) or {}).get("UpdatedAt","") or "")
except Exception: print("")' 2>/dev/null)"
if [ "${RELEASE_SKIP_SCAN:-0}" = "1" ] || [ -z "$TRIVY_IMG" ]; then
  reason="$( [ -n "$TRIVY_IMG" ] && echo 'skipped_by_env' || echo 'scanner_not_installed' )"
  printf '{"tool": "trivy", "status": "unavailable", "reason": "%s"}\n' "$reason" > "$SCAN_JSON"
  echo "  scanner unavailable (${reason}) — recorded (NOT a pass)"
elif [ "$DB_PRESENT" != "yes" ]; then
  printf '{"tool": "trivy", "tool_id": "%s", "status": "unavailable", "reason": "vuln_db_not_cached"}\n' \
    "${TRIVY_ID}" > "$SCAN_JSON"
  echo "  trivy DB not cached in volume '${TRIVY_DB_VOLUME}' — recorded unavailable"
  echo "  operator action: docker run --rm -v ${TRIVY_DB_VOLUME}:/root/.cache/ ${TRIVY_ID} image --download-db-only"
else
  TRIVY_VER="$(docker run --rm "$TRIVY_IMG" --version 2>/dev/null | awk '/Version/{print $2; exit}')"
  if docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
       -v "${TRIVY_DB_VOLUME}:/root/.cache" "$TRIVY_IMG" image \
       --scanners vuln --skip-db-update --format json --quiet --timeout "${RELEASE_SCAN_TIMEOUT}s" \
       "$WEB_IMAGE" > "$OUT_DIR/trivy.json" 2>"$OUT_DIR/trivy.err"; then
    "$PY" - "$OUT_DIR/trivy.json" "$TRIVY_VER" "$TRIVY_ID" "$TRIVY_DIGEST" "$DB_UPDATED" "$SCAN_JSON" <<'PY'
import json, sys
raw, ver, tid, tdig, db_updated, out = sys.argv[1:7]
data = json.load(open(raw))
sev = {}
for r in (data.get("Results") or []):
    for v in (r.get("Vulnerabilities") or []):
        s = (v.get("Severity") or "UNKNOWN").upper()
        sev[s] = sev.get(s, 0) + 1
status = "fail" if sev.get("CRITICAL", 0) else ("warn" if sev.get("HIGH", 0) else "pass")
md = data.get("Metadata") if isinstance(data.get("Metadata"), dict) else {}
db = md.get("DB") if isinstance(md.get("DB"), dict) else {}
json.dump({"tool": "trivy", "tool_version": ver, "tool_id": tid, "tool_digest": tdig or None,
           "status": status, "completed": True, "severities": sev,
           "db_updated_at": db.get("UpdatedAt") or md.get("Timestamp") or (db_updated or None),
           "db_version": db.get("Version"), "ignored": [], "artifact": "trivy.json"},
          open(out, "w"), indent=2)
print("  trivy:", status, sev, "db=", db.get("UpdatedAt") or db_updated)
PY
    # Phase 10B: enrich the descriptor with triage — classify, evaluate
    # time-bound exceptions against the CRITICALs, and attach scanner provenance.
    # Rewrites SCAN_JSON; never aborts (the RC is still produced, gating is
    # release-check's job). Exits 1 when unapproved CRITICAL remain (informational).
    # Pass the FULL image id and the REAL RepoDigest (or none). Set
    # RELEASE_SCANNER_OPERATOR_VERIFIED=1 to attest a local scanner out-of-band
    # (=> local_image_id_verified); otherwise an image with no real RepoDigest is
    # honestly recorded as `unverified`.
    OPV="$([ "${RELEASE_SCANNER_OPERATOR_VERIFIED:-0}" = "1" ] && echo --operator-verified)"
    "$PY" -m app.cli release triage-scan --trivy "$OUT_DIR/trivy.json" --out "$SCAN_JSON" \
      --scanner-version "${TRIVY_VER}" --scanner-image-id "${TRIVY_FULL_ID}" \
      --scanner-source "aquasec/trivy" ${OPV} \
      ${TRIVY_DIGEST:+--scanner-repo-digest "$TRIVY_DIGEST"} 2>/dev/null | grep -v alembic || true
  else
    printf '{"tool": "trivy", "tool_version": "%s", "tool_id": "%s", "status": "unavailable", "reason": "scan_error_or_timeout"}\n' \
      "${TRIVY_VER:-unknown}" "${TRIVY_ID}" > "$SCAN_JSON"
    echo "  trivy scan errored/timed out — recorded unavailable"; tail -3 "$OUT_DIR/trivy.err" 2>/dev/null
  fi
fi
set -e

# ---- 11. release check (best-effort against the running stack; read-only) ----
echo "== 11/14 release-check summary (best-effort, read-only) =="
RC_JSON="$OUT_DIR/release-check.json"
if curl -fsS --max-time 60 "http://127.0.0.1:8000/api/system/release-check" 2>/dev/null \
     | "$PY" -c "import json,sys; d=json.load(sys.stdin); json.dump({'overall': d['overall'], 'counts': d['counts']}, open('$RC_JSON','w'))" 2>/dev/null; then
  echo "  captured: $(cat "$RC_JSON")"
else
  RC_JSON=""; echo "  running stack not reachable — release-check summary omitted"
fi

# ---- 12. release manifest ----
echo "== 12/14 release manifest =="
export APP_VERSION="$REL_VERSION" APP_GIT_COMMIT="$GIT_COMMIT" APP_GIT_TREE_CLEAN=1 \
       APP_BUILD_TIME="$BUILD_TIME" APP_FRONTEND_BUILD_ID="$FRONTEND_BUILD_ID"
MANIFEST="$OUT_DIR/release-manifest.json"
"$PY" -m app.cli release create-manifest --out "$MANIFEST" \
  --backend-tests "${BACKEND_TESTS:-0}" --frontend-tests "${FRONTEND_TESTS:-0}" \
  --images-json "$IMAGES_JSON" --base-images-json "$BASE_IMAGES_JSON" \
  ${SBOM_JSON:+--sbom-json "$SBOM_JSON"} \
  --scan-json "$SCAN_JSON" --rehearsal-json "$REHEARSAL_JSON" \
  ${RC_JSON:+--release-check-json "$RC_JSON"} \
  --base-remediation-status "${RELEASE_BASE_REMEDIATION:-no_action_needed}" \
  --dependency-remediation-status "${RELEASE_DEP_REMEDIATION:-no_action_needed}" \
  ${APT_JSON:+--apt-json "$APT_JSON"} \
  2>/dev/null | grep -v alembic || true

# ---- 13. verify manifest ----
echo "== 13/14 verify manifest =="
"$PY" -m app.cli release verify-manifest --manifest "$MANIFEST" 2>/dev/null | grep -vE "alembic|^$" || \
  { echo "  manifest verify FAILED"; exit 1; }

# ---- 14. completion marker ----
echo "== 14/14 completion marker =="
: > "$OUT_DIR/RELEASE_BUILD_COMPLETE"
echo "release build complete: $OUT_DIR/release-manifest.json"
echo "  (artifacts are gitignored; deploy is a SEPARATE step — scripts/deploy.sh)"
