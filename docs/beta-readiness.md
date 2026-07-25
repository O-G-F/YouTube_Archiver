# Local single-user public-beta readiness

This is a **read-only acceptance report** for the `v0.11.0-beta.1` public-beta
candidate. It is deliberately **separate** from the production release-check —
it does **not** change any gate, and it does **not** claim production readiness.

| Field | Value |
| --- | --- |
| `local_single_user_beta_ready` | **true** |
| `production_ready` | **false** (unchanged, by design) |
| release-check | **FAIL** (unchanged — see below) |
| active vulnerability exceptions | **0** |
| known CRITICAL OS CVEs | **7** (surfaced, not hidden) |
| scanner provenance | **unverified** (not hidden) |

## What "beta ready" means here

The project is suitable to share as a **small, local, single-user public beta**
— not as a hosted or production service. The two verdicts are intentionally
different: a build can be acceptable for a documented local beta while remaining
*not production-ready*.

## Beta acceptance criteria (all met)

- [x] **Clean install** from a tracked-files-only checkout (no `.env`, secrets,
      or data) succeeds: `docker compose up -d --build`, fresh migration, all
      services healthy, worker converges.
- [x] **Quick Start** in the README runs as written (loopback `127.0.0.1`).
- [x] **CI-equivalent** run is green in a clean container: hash-pinned backend
      install + tests, `npm ci` + vitest + build, and the static guards.
- [x] **Secure by default**: the shipped compose binds the web console to
      loopback; LAN exposure is an explicit, documented opt-in that the README
      couples with enabling authentication.
- [x] **Synthetic demo** works from a clean install and touches no real data.
- [x] **No secrets / personal data / host paths** are tracked (scanned).
- [x] **Not archive/data dependent**: the app starts and is usable with an empty
      database.
- [x] **Update & rollback** rehearsed in isolation — data survives an
      image up/down/up cycle (this release adds no schema migration).
- [x] **Documented limitations** are present (README "Known limitations").

## Honestly-disclosed residual risk (unchanged)

- **7 known CRITICAL OS-package CVEs** with no upstream fix are **accepted as
  local single-user residual risk** and surfaced in the UI + the
  [decision dossier](vulnerability-decision-dossier.md) /
  [risk-acceptance ADR](decisions/phase-11-local-single-user-risk-acceptance.md).
- The build is **not production-ready**; the release-check **FAILs** on those
  CVEs and on **unverified scanner provenance** — that FAIL is by design and is
  not overridden, and **no active vulnerability exception** is added.
- **Do not expose it anonymously to the internet.** See [`SECURITY.md`](../SECURITY.md).
