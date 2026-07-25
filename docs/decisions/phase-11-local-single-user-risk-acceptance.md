# ADR: Local single-user residual-risk acceptance (Phase 11)

- **Status:** Accepted
- **Decision date:** 2026-07-25
- **Owner / decision-maker:** O-G-F (project maintainer)
- **Decision source:** operator decision at the start of Phase 11
- **Next review:** 2026-10-22

## Context

After Phase 10B.2/10B.3 the release image still carries **7 CRITICAL
vulnerabilities**, all real Debian 13 (trixie) OS packages with **no trixie fix
available** ("Minor issue" per the Debian Security Tracker):

| CVE | package | class | reachability (Phase 10B.3) |
|-----|---------|-------|-----------------------------|
| CVE-2026-6653 | libxml2 | use-after-free DoS | potentially_reachable (ffmpeg XML parsing) |
| CVE-2026-34873 | libmbedcrypto16 | TLS1.3 resumption impersonation | potentially_reachable (ffmpeg SRT/mbedTLS; unused by YouTube HTTPS) |
| CVE-2026-34875 | libmbedcrypto16 | FFDH key-export overflow | potentially_reachable (same) |
| CVE-2026-58016 | libglib2.0-0t64 | GDBus introspection DoS | not_reachable_with_evidence (no D-Bus) |
| CVE-2026-13221 | perl-base | regex trie overflow | not_reachable_with_evidence (perl not invoked) |
| CVE-2026-42496 | perl-base | Archive::Tar path traversal | not_reachable_with_evidence (perl not invoked) |
| CVE-2026-8376 | perl-base | regex heap overflow (32-bit only) | not_reachable_with_evidence (perl not invoked; host is 64-bit) |

Full evidence: [`docs/vulnerability-decision-dossier.md`](../vulnerability-decision-dossier.md)
and the non-active proposals in [`vulnerability-exception-proposals.yml`](../../vulnerability-exception-proposals.yml).

The operator's context: a **single primary user**, the source will be published
**small-scale on GitHub**, and at this point **product/UI completeness is
prioritised** over chasing the residual CVEs to zero.

## Decision

Accept the 7 CRITICAL findings as **known, tracked residual risk for
`local_single_user` operation only**, and continue development / daily personal
use without waiting for the vulnerabilities to be fully resolved.

**This is a decision to continue development under limited conditions — it does
NOT mean the risk is resolved.**

### Scope this covers

- Local single-user **development** and **personal** use of the archiver.
- Publishing the **source repository** publicly on GitHub (code only).

### Explicit limits (all still hold)

- **NOT production-ready.** This decision does not certify the image for
  production or multi-user/hosted operation.
- **`release-check` continues to FAIL** on `critical_vulnerabilities` and
  `scanner_provenance_verified`. Its code, policy, and results are **not**
  changed to PASS.
- **No active vulnerability exception is created** — `vulnerability-exceptions.yml`
  stays empty. The proposals file is advisory only.
- **Scanner provenance stays `unverified`** and is not hidden (the Trivy image's
  RepoDigest equals its config `.Id`, so it is not a genuine registry digest).
- The running service **must not be exposed anonymously to the public internet**
  (loopback / trusted-proxy + auth only; see the production compose template and
  the Phase 9C/9D auth runbook).
- **No operation権限 for anonymous users or third parties**; no third-party file
  upload or arbitrary-URL submission is opened up.
- The published repository must **never** contain `.env`, secrets, cookies, data,
  videos, DB dumps, backups, release artifacts, host paths, or internal URLs.

### Re-evaluation triggers

- A Debian/upstream fix ships for any of the 7 CVEs → rebuild base + rescan and
  drop it from this list.
- Any change of scope beyond local single-user (hosting, multi-user, public
  exposure) → this acceptance is **void** and must be re-decided.
- At the latest, review by **2026-10-22**.

## Consequences

- Development and personal archiving continue on the current image.
- `release-check` remains a truthful FAIL; anyone reading it sees the real state.
- The residual risk is documented and time-boxed rather than silently ignored.
