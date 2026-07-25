# YouTube Archiver image (multi-stage):
#   1) build the React/Vite admin UI with Node
#   2) Python runtime: ffmpeg/ffprobe + Deno + yt-dlp, serving API + built UI
#
# Phase 10A (supply chain): base images are ARGs so a production release can pin
# them to an immutable digest (e.g. --build-arg BASE_PYTHON_IMAGE=python:3.12-slim@sha256:...);
# the floating tags below are the reproducible defaults recorded in the release
# manifest. Build identity (APP_* args) is baked into the image so build_info /
# `system version` report it at runtime. Values are non-secret provenance only.
#
# Non-root note (audited, deliberately NOT changed here): the runtime stays root
# because the container writes to HOST bind mounts (ARCHIVE_ROOT/CONFIG_ROOT/
# LOG_ROOT, see docker-compose volumes) whose ownership matches the host user;
# switching to a fixed non-root UID would break writes to those NAS/host paths
# without a coordinated `chown`/`user:` mapping. Revisit with an explicit UID
# mapping when the deployment target's volume ownership is known.

ARG BASE_NODE_IMAGE=node:20-slim
ARG BASE_PYTHON_IMAGE=python:3.12-slim

# ---- Stage 1: frontend build ----
FROM ${BASE_NODE_IMAGE} AS frontend
WORKDIR /ui
# Dependency layer (cached unless package manifests change). `npm ci` is STRICT:
# it fails if package.json and package-lock.json disagree (supply-chain repro).
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
# Build the SPA -> /ui/dist
COPY frontend/ ./
RUN npm run build \
    && test -f dist/index.html

# ---- Stage 2: python wheel builder (Phase 10B.2) ----
# Compiles psycopg2 FROM SOURCE and collects every other hash-pinned dependency
# as a wheel into /wheelhouse. gcc + libpq-dev exist ONLY in this stage and never
# reach the runtime image, so production carries no compiler/headers. `pip wheel
# --require-hashes` verifies EVERY lock hash (including the psycopg2 sdist) before
# building, so the hash lock is enforced HERE; the runtime then installs offline
# from the closed wheelhouse. SOURCE_DATE_EPOCH pins build *timestamps*, but the
# wheel is NOT bit-for-bit reproducible: pip compiles psycopg2 in a random temp
# dir, so the `_psycopg.so` GNU build-id (and thus the wheel sha256) varies per
# build. This is metadata-only — the code, DT_NEEDED (libpq.so.5, libc.so.6), and
# the vuln-removal result (system-lib linkage, no vendored pcre2) are invariant.
# The ACTUAL built-wheel sha256 is captured per build into the runtime lock so
# --require-hashes still holds (see Phase 10B.3 wheel-reproducibility evidence).
FROM ${BASE_PYTHON_IMAGE} AS wheelbuild
ENV PIP_NO_CACHE_DIR=1
ARG SOURCE_DATE_EPOCH=1700000000
ENV SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}
# gcc + libc6-dev (C toolchain + libc headers: python:3.12-slim ships Python.h
# but NOT stdlib.h) + libpq-dev (pg_config + libpq headers for the psycopg2 C
# extension). All builder-only; none reach the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev libpq-dev \
    && rm -rf /var/lib/apt/lists/*
# Build tooling for a NON-isolated, offline-deterministic source build (no
# unhashed build deps fetched at wheel time). Builder-stage only.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel
WORKDIR /build
COPY requirements.lock ./
COPY scripts/make-runtime-lock.py ./make-runtime-lock.py
# Full wheelhouse. --require-hashes verifies every lock hash (incl. the psycopg2
# sdist 1dedb1c7…); --no-binary psycopg2 forces source compilation; the runtime
# `--require-hashes` install below fail-closes if any OTHER package unexpectedly
# built from sdist (its hash would not match the committed lock).
RUN pip wheel --require-hashes --no-build-isolation --no-binary psycopg2 \
        -r requirements.lock -w /wheelhouse
# Record the source-built wheel's sha256 (a source build has no pre-known wheel
# hash) and derive a runtime lock pinning psycopg2 to THAT wheel, so
# --require-hashes still holds at the runtime install. Non-secret provenance.
RUN set -eu; \
    whl="$(ls /wheelhouse/psycopg2-*.whl)"; test -n "$whl"; \
    sha="$(sha256sum "$whl" | awk '{print $1}')"; \
    printf '%s\n' "$sha" > /wheelhouse/psycopg2.wheel.sha256; \
    python make-runtime-lock.py requirements.lock "$sha" /wheelhouse/requirements.runtime.lock; \
    echo "built psycopg2 wheel: $(basename "$whl") sha256=$sha"

# ---- Stage 3: python runtime ----
FROM ${BASE_PYTHON_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DENO_INSTALL=/usr/local \
    PATH="/usr/local/bin:${PATH}"

# ffmpeg/ffprobe (requirement 3) + tools needed to install Deno. No apt cache
# is kept (rm -rf lists), and only these packages are added.
#
# Phase 10B OS remediation: after installing ffmpeg (which pulls the mesa/GL
# stack as hard deps), TARGETED-upgrade only the CVE-2026-40393 packages to the
# published Debian security fix (25.0.7-2+deb13u1). This is NOT a blanket
# `apt-get upgrade` — only these named packages are touched, so ffmpeg/deno/
# yt-dlp behaviour is unchanged. The other CRITICALs (libglib2.0/libmbedcrypto16/
# libxml2/perl-base) currently have NO Debian fix; they are tracked for an
# operator exception, not silently patched here.
#
# Phase 10B.2: `libpq5` is the system PostgreSQL client lib that the SOURCE-built
# psycopg2 links against (it pulls the patched system libpcre2-8-0 transitively,
# replacing the -binary wheel's vendored pcre2 10.32). gcc/libpq-dev/headers stay
# in the wheelbuild stage and are NOT installed here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates curl unzip libpq5 \
    && apt-get install -y --only-upgrade \
        libgbm1 libgl1-mesa-dri libglx-mesa0 mesa-libgallium \
    && rm -rf /var/lib/apt/lists/*

# Deno for the YouTube JS challenge / EJS runtime (requirement 3 / 5.4).
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y \
    && /usr/local/bin/deno --version

WORKDIR /app

# Dependency layer (cached independently of source). Phase 10A.1 pinned every
# direct AND transitive package `==` with sha256 hashes in `requirements.lock`
# (generated from the known-good image by scripts/gen-python-lock.py); Phase
# 10B.2 builds those into a closed wheelhouse in the `wheelbuild` stage.
#
# Install OFFLINE from that wheelhouse: `--no-index` = no network / no
# re-resolution (versions come from the lock); `--require-hashes` STILL enforced
# against `requirements.runtime.lock` (identical to requirements.lock except the
# psycopg2 block carries the source-built wheel's sha256 instead of the sdist
# hash — see scripts/make-runtime-lock.py). Every installed package, including
# the source-built psycopg2, is therefore hash-verified. This is NOT a hash-lock
# relaxation: enforcement happened at `pip wheel --require-hashes` in the builder,
# and again here. `requirements.txt` remains the human-edited DIRECT input.
COPY requirements.lock ./
COPY --from=wheelbuild /wheelhouse /wheelhouse
RUN pip install --no-cache-dir --require-hashes --no-index --find-links /wheelhouse \
        -r /wheelhouse/requirements.runtime.lock
# Keep the source-built psycopg2 wheel's sha256 as an in-image provenance marker
# (read by scripts/build-release.sh into the release manifest). Non-secret; then
# drop the wheelhouse so no build artifacts bloat the runtime layer.
RUN mkdir -p /usr/local/share/archiver-provenance \
    && cp /wheelhouse/psycopg2.wheel.sha256 /usr/local/share/archiver-provenance/psycopg2-wheel.sha256 \
    && cp /wheelhouse/requirements.runtime.lock /usr/local/share/archiver-provenance/requirements.runtime.lock \
    && rm -rf /wheelhouse

# Fail the build immediately if the dependency layer is incomplete or corrupt
# (e.g. files written as 0 bytes when the Docker VM disk fills up mid-install).
# This turns a silent runtime ImportError into a loud build failure.
RUN python - <<'PY'
import glob, os, sysconfig
import pydantic, pydantic_core, fastapi, sqlalchemy, rq, yt_dlp, curl_cffi, ijson
import psycopg2  # Phase 10B.2: source-built; loads _psycopg.so -> system libpq5
from pydantic_settings import BaseSettings, SettingsConfigDict
assert pydantic.VERSION.startswith("2."), f"unexpected pydantic {pydantic.VERSION}"
# Fail the build if the -binary manylinux wheel leaked back in: the source build
# must NOT ship the vendored shared-lib bundle (which carries pcre2 10.32).
sp = sysconfig.get_paths()["purelib"]
leaked = glob.glob(os.path.join(sp, "psycopg2_binary.libs")) + \
    glob.glob(os.path.join(sp, "psycopg2_binary-*.dist-info"))
assert not leaked, f"psycopg2-binary leaked into the runtime: {leaked}"
print("dependency verify OK: pydantic", pydantic.VERSION,
      "| psycopg2", psycopg2.__version__, "(source, no vendored libs)",
      "| curl_cffi", curl_cffi.__version__, "| ijson", ijson.__version__)
PY

# Application. requirements.txt is the single source of runtime dependencies,
# so the editable install must NOT re-resolve/override them (--no-deps).
#
# Phase 10B.2 re-audit (editable install in production): deliberately KEPT. The
# full source is already in the image (COPY . .); `-e .` only adds a PEP 660
# finder pointing at /app and pulls in NO build tools or runtime deps (--no-deps).
# Switching to a non-editable `pip install --no-deps .` is orthogonal to this
# phase's supply-chain goal and would risk entry-point/package-discovery
# regressions, so it is not changed here (revisit if the source tree ever leaves
# the runtime image).
COPY . .

# Built admin UI from stage 1 (served by FastAPI at "/" — see app/main.py).
COPY --from=frontend /ui/dist ./frontend/dist

RUN pip install --no-deps -e .

# Final guard: the app import chain AND the built UI must both be present.
RUN python -c "from app.config import get_settings; from app.bootstrap import seed; print('app import verify OK')" \
    && test -f /app/frontend/dist/index.html \
    && echo "frontend dist verify OK"

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Build identity / provenance (Phase 10A). Placed LAST so the volatile values
# (build time, commit) don't bust the cache of the expensive apt/deno/pip/app
# layers above — only this cheap identity layer rebuilds when the args change.
# Passed by scripts/build-release.sh or `docker compose build --build-arg`;
# empty in a plain `docker build`. Non-secret provenance only.
ARG APP_VERSION=""
ARG APP_GIT_COMMIT=""
ARG APP_BUILD_TIME=""
ARG APP_BUILD_ID=""
ARG APP_GIT_TREE_CLEAN=""
ARG APP_FRONTEND_BUILD_ID=""
ARG APP_IMAGE_DIGEST=""
ARG BASE_PYTHON_IMAGE=python:3.12-slim
ENV APP_VERSION=${APP_VERSION} \
    APP_GIT_COMMIT=${APP_GIT_COMMIT} \
    APP_BUILD_TIME=${APP_BUILD_TIME} \
    APP_BUILD_ID=${APP_BUILD_ID} \
    APP_GIT_TREE_CLEAN=${APP_GIT_TREE_CLEAN} \
    APP_FRONTEND_BUILD_ID=${APP_FRONTEND_BUILD_ID} \
    APP_IMAGE_DIGEST=${APP_IMAGE_DIGEST}

# OCI provenance labels (revision/version/created/source/licenses). The source
# URL is intentionally generic; a private repo URL must not be baked in here.
LABEL org.opencontainers.image.title="youtube-archiver" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${APP_GIT_COMMIT}" \
      org.opencontainers.image.created="${APP_BUILD_TIME}" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.base.name="${BASE_PYTHON_IMAGE}"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["web"]
