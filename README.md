# YouTube Local Archiver

A self-hosted archiver for your own YouTube data — liked videos, watch history,
playlists, channels — built on **yt-dlp + FastAPI + RQ + PostgreSQL**, with a
React admin console. It downloads metadata and (optionally) media bodies on your
own machine and keeps them browsable, searchable, and backed up.

> **Scope:** designed for **local, single-user** use. It is **not** a hosted,
> multi-tenant, or production-hardened service — see
> [Security posture](#security-posture) before exposing it to any network.

---

## What it does

- **Archive your liked videos / watch history** imported from Google Takeout, and
  arbitrary video / playlist / channel URLs.
- **Metadata first**: info.json, description, thumbnails, subtitles, comments and
  live-chat — with a separate step for downloading the actual video/audio body.
- **Browse & search** everything locally (titles, channels, comments, live chat).
- **Play archived media** in the browser with HTTP Range seeking.
- **Jobs & scheduler**: queued downloads/refreshes with retry classification and
  a manual "run once" scheduler (no unattended bulk downloads by default).
- **Operations**: backup / restore rehearsal, an append-only audit trail, and a
  read-only System panel (runtime vs. last-scanned-release status).

### Screens

| Screen | What it shows |
|--------|---------------|
| **Dashboard** | health, counts, jobs-by-status, scheduler, latest activity; a *Getting started* checklist on a fresh install |
| **Videos** / **Video detail** | filter/sort/search list; per-video metadata, comments, live chat, and an in-browser player |
| **Jobs** / **Job detail** | job list with filters + auto-refresh; per-job diagnostics (why a download failed, retryable or not) |
| **Liked videos** | liked-archive progress, failures by reason, and archive operations |
| **Search** | across titles/channels, comments, live chat, collections |
| **Collections** / **Takeout** | tracked playlists/channels; Google Takeout import |
| **System / Settings** | runtime & release status, backup/recovery readiness, audit trail, doctor, profiles, scheduler, configuration (secrets never shown) |

📷 **Screenshots** (from an isolated, all-synthetic demo — no real data):
[`docs/demo/`](docs/demo/README.md). You can reproduce the demo locally with one
command; see that page.

---

## Architecture

Multi-container, one image for all app services:

```
web        FastAPI API + built React SPA   (serves the admin console)
worker     RQ worker                       (runs downloads / refreshes)
scheduler  periodic pass runner            (off by default)
migrate    one-shot alembic upgrade        (runs, then exits 0)
postgres   metadata database
redis      RQ job queue (AOF-persisted)
```

The app image bundles `ffmpeg`/`ffprobe`, `deno` (YouTube JS-challenge runtime),
and `yt-dlp`. Python dependencies are installed from a hash-pinned lock, and
`psycopg2` is built from source (links the system `libpq`).

## Requirements

- Docker + Docker Compose
- Disk for the archive (media bodies are large — plan capacity on a NAS/SSD)
- A modern desktop browser (the console is desktop-oriented)

---

## Quick start (Docker Compose)

```bash
git clone <your-fork-url> youtube-archiver
cd youtube-archiver

cp .env.example .env
# Edit .env: point *_HOST_PATH at real directories on your disk/NAS, and set a
# strong Postgres password. Keep DATABASE_URL/REDIS_URL pointing at the compose
# services unless you know otherwise.

docker compose up -d --build
```

Then open <http://127.0.0.1:8000>. The `migrate` service applies the schema and
seeds built-in profiles before `web`/`worker`/`scheduler` start. On a fresh
install the Dashboard shows a **Getting started** checklist.

### First setup

Work through the *Getting started* checklist (nothing there starts a download
automatically):

1. **Storage** — confirm the archive / config / log directories are writable.
2. **Authentication** — see [Security posture](#security-posture).
3. **YouTube cookies** *(optional)* — a cookies file improves fetch reliability.
4. **Import Google Takeout** *(optional)* — populate your liked/watch history.
5. **Fetch metadata** — info.json / subtitles / thumbnails (no media body).
6. **Download profile** — the default save profile is already set.
7. **Backup** — configure and run backups before relying on the archive.

---

## Security posture

This project is accepted for **local single-user** use with a **documented,
tracked residual risk**. Please read this before exposing it anywhere:

- The container image currently ships **7 known CRITICAL OS-package CVEs** that
  have **no upstream fix** yet (Debian "minor issue" DoS/local-class bugs). They
  are **not hidden** — the System panel surfaces them, and each is analysed in the
  [decision dossier](docs/vulnerability-decision-dossier.md). See the
  [risk-acceptance ADR](docs/decisions/phase-11-local-single-user-risk-acceptance.md).
- **This build is not production-ready.** The release-check intentionally
  **FAILs** on those CVEs and on unverified scanner provenance; that FAIL is by
  design and is not overridden.
- **Do not expose it anonymously to the internet.** Bind to `127.0.0.1` and keep
  it on a trusted host, or put it behind authentication + a trusted reverse proxy.

### localhost vs. LAN / internet

- **Default (safe):** bind the web port to `127.0.0.1` (loopback) on your own
  machine. `AUTH_MODE=disabled` is acceptable here for a single user.
- **LAN or internet:** you **must** enable authentication and avoid a `0.0.0.0`
  bind. Use the tracked `docker-compose.production.example.yml` (loopback web
  port, datastores unpublished) behind a reverse proxy that terminates TLS.

### Enabling authentication

```bash
# generate a session secret and an admin password hash (values are never logged)
docker compose run --rm web archiver auth gen-session-secret
docker compose run --rm web archiver auth hash-password
```

Set `AUTH_MODE=local`, point `SESSION_SECRET_FILE` / `ADMIN_PASSWORD_HASH_FILE`
at the generated files, and set secure cookie options. See
`.env.production.example` for the full auth block.

---

## Ports & volumes

- **Port:** the web console is published on **`127.0.0.1:8000` by default**
  (loopback only — reachable from this machine, not the LAN). To change the port
  set `WEB_PORT`; for LAN access set `WEB_BIND_HOST=0.0.0.0` in `.env` — but
  **enable authentication first** (see [Security posture](#security-posture)).
- **Named volumes:** `pgdata` (Postgres) and `redisdata` (Redis AOF — so queued
  jobs survive restarts). **Never** run `docker compose down -v` unless you intend
  to erase them.
- **Host binds:** the archive / config / log directories are bind-mounted from
  the paths you set in `.env` (`*_HOST_PATH`). Point them at durable storage.

## Backup, restore & update

```bash
# back up Postgres + the archive manifest (writes a verifiable manifest)
./scripts/backup.sh
./scripts/verify-backup.sh

# rehearse a restore into a throwaway isolated stack (never touches your data)
./scripts/restore-rehearsal.sh

# update to a new build (non-destructive; reuses volumes, waits for convergence)
git pull && docker compose up -d --build
```

**Rolling back:** always `./scripts/backup.sh` **before** updating. To roll back,
check out the previous commit and `docker compose up -d --build` again — the
database is **not** assumed to be downgrade-safe, so a rollback is only safe when
the schema is unchanged between the two versions (`archiver system version` shows
the `schema_head`). If a release included an incompatible migration, roll back by
**restoring the pre-update backup** rather than downgrading the image.

## Troubleshooting

- **Worker "not converged" right after a deploy** — the old worker's heartbeat
  lingers for up to ~90s. It clears itself; `deploy.sh` waits for it.
- **`docker compose up` hangs** — usually a Docker daemon restart / heavy host
  load, not a code issue. Check `docker ps -a`, wait for the daemon to settle,
  then retry. Do **not** delete volumes.
- **System panel shows "runtime ≠ scanned release"** — expected on a development
  build that hasn't been through `scripts/build-release.sh`; the vulnerability
  numbers come from the last scanned release, not the running dev build.
- **Diagnostics** — `System / Settings → Doctor` checks storage, ffmpeg, deno,
  database and redis.

## Known limitations

- Desktop-first UI (mobile is usable but not fully optimized).
- Not production-ready (see [Security posture](#security-posture)).
- Full unattended bulk downloading is deliberately gated (small, confirmed batches).

## Development history

The detailed phase-by-phase development log lives in
[docs/development-history.md](docs/development-history.md).

## License

MIT — see `pyproject.toml`.

*(No screenshots are committed: the maintainer's archive contains personal video
titles/channels/comments. Run it locally to see the console.)*
