# Changelog

All notable changes to this project are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/); this project has not
yet cut a versioned release (it is an early public beta on `main`).

Detailed, phase-by-phase development history lives in
[`docs/development-history.md`](docs/development-history.md).

## [Unreleased]

## [0.11.0-beta.1] - 2026-07-25

First public beta. Local single-user scope; **not production-ready**
(7 known CRITICAL OS-package CVEs accepted as residual risk, release-check FAILs
by design, 0 active vulnerability exceptions). See the *Security* section below.

### Added
- Public-beta repository hygiene: `LICENSE` (MIT), `SECURITY.md`,
  `CONTRIBUTING.md`, this changelog, GitHub issue / PR templates, and a
  secret-free CI workflow (backend tests, frontend tests + build, static guards).
- **Secure-by-default bind**: a fresh install publishes the admin console to
  `127.0.0.1` only (`WEB_BIND_HOST`/`WEB_PORT` in `.env`; set `0.0.0.0` for LAN
  only *after* enabling auth). The first-run checklist escalates to a **DANGER**
  warning when the port is on all interfaces with auth disabled; CI asserts the
  default compose bind is loopback.
- A bare `/health` liveness route (was only answered by the SPA fallback).
- **Isolated synthetic demo** (`scripts/demo_seed.py` + `docker-compose.demo.yml`
  + reviewed screenshots in `docs/demo/`): a one-command, loopback-only preview
  seeded with invented data — guarded so it can never touch a real archive.
- First-run **Getting started** checklist for fresh installs (safe links, no
  auto-run, auth/exposure guidance).
- **Runtime vs. last-scanned-release** status in the System panel (match /
  mismatch / no-scanned-release), so a stale release manifest is never shown as
  the running build's scan.
- Honest **security posture** surface: the known CRITICAL count is shown, not
  hidden; the build is marked *not production-ready*.

### Changed
- Rewrote `README.md` as a first-time-user product guide; moved the internal
  phase log to `docs/development-history.md`.
- Reorganized information architecture: Backup / release / audit panels moved
  from the Liked-videos page into **System / Settings**.
- Accessibility: visible keyboard focus (`:focus-visible` ring + design tokens),
  skip-to-content link, `role`/`aria-live` on loading & error states, nav landmark
  labelling, modal Escape-to-close + focus-return, reduced-motion support.
- `psycopg2` is built from source (drops the `-binary` wheel's vendored,
  CVE-flagged `pcre2`); SPA `index.html` is served `no-cache` so deploys are
  picked up immediately.

### Fixed
- Desktop-width overflow: compact key/value tables and stat cards no longer force
  a horizontal page scroll at narrower desktop widths (down to 1024px). Wide data
  tables still scroll inside their own container; the page body never does.
- CI now installs the system build dependencies (`libpq-dev`, `gcc`, `libc6-dev`)
  that the hash-pinned, source-built `psycopg2` needs — the backend `pip install`
  and `/health`-dependent tests would otherwise fail on a clean runner.

### Security
- 7 remaining CRITICAL OS-package CVEs (no upstream fix) are documented and
  **accepted as local single-user residual risk** — the release-check
  intentionally FAILs on them and no active exception is added. See
  [`SECURITY.md`](SECURITY.md) and the decision dossier.
