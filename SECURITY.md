# Security Policy

## Intended use

YouTube Local Archiver is designed for **local, single-user** operation on a
trusted host. It is **not** a production-hardened, multi-tenant, or public-facing
service.

- **Do not expose it anonymously to the internet.** Bind the web port to
  `127.0.0.1`, or place it behind authentication **and** a trusted reverse proxy
  that terminates TLS. Enable `AUTH_MODE` before any LAN/internet exposure.
- Do not grant anonymous or third-party operation rights, and do not open up
  arbitrary file upload or arbitrary-URL submission to untrusted users.

## Known vulnerabilities & production status

This project is **not production-ready** and does not claim to be.

- The container image currently ships **7 known CRITICAL OS-package CVEs** that
  have **no upstream fix** yet (Debian "minor issue" DoS/local-class bugs in
  `libxml2`, `libmbedcrypto16`, `libglib2.0`, `perl-base`). They are **not
  hidden** — the System panel surfaces them and each is analysed in
  [`docs/vulnerability-decision-dossier.md`](docs/vulnerability-decision-dossier.md).
- These are **accepted as local single-user residual risk** — see
  [`docs/decisions/phase-11-local-single-user-risk-acceptance.md`](docs/decisions/phase-11-local-single-user-risk-acceptance.md).
- The release-check intentionally **FAILs** on these CVEs and on unverified
  scanner provenance; that FAIL is by design and is not overridden.

## Supported versions

This is an early public beta. Only the **latest `main`** is supported; there are
no long-term-support branches. Update by pulling `main` and rebuilding.

## Reporting a vulnerability

Please report security issues **privately** via **GitHub Security Advisories**
("Report a vulnerability" on the repository's *Security* tab) — not in a public
issue.

- **Do not** paste secrets, cookies, tokens, session values, real URLs, or host
  paths into any report.
- Include a clear description and minimal reproduction; redact any personal data.

We will acknowledge and triage as capacity allows. Given the local single-user
scope, please note the accepted-risk posture above before reporting the known
CVEs already documented here.
