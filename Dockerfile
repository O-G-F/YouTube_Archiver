# YouTube Archiver image: Python + ffmpeg/ffprobe + Deno + yt-dlp.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DENO_INSTALL=/usr/local \
    PATH="/usr/local/bin:${PATH}"

# ffmpeg/ffprobe (requirement 3) + tools needed to install Deno.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Deno for the YouTube JS challenge / EJS runtime (requirement 3 / 5.4).
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y \
    && /usr/local/bin/deno --version

WORKDIR /app

# Dependency layer (cached independently of source).
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# Fail the build immediately if the dependency layer is incomplete or corrupt
# (e.g. files written as 0 bytes when the Docker VM disk fills up mid-install).
# This turns a silent runtime ImportError into a loud build failure.
RUN python - <<'PY'
import pydantic, pydantic_core, fastapi, sqlalchemy, rq, yt_dlp, curl_cffi
from pydantic_settings import BaseSettings, SettingsConfigDict
assert pydantic.VERSION.startswith("2."), f"unexpected pydantic {pydantic.VERSION}"
print("dependency verify OK: pydantic", pydantic.VERSION, "| curl_cffi", curl_cffi.__version__)
PY

# Application. requirements.txt is the single source of runtime dependencies,
# so the editable install must NOT re-resolve/override them (--no-deps).
COPY . .
RUN pip install --no-deps -e .

# Final guard: the app's own settings/bootstrap import chain must resolve.
RUN python -c "from app.config import get_settings; from app.bootstrap import seed; print('app import verify OK')"

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["web"]
