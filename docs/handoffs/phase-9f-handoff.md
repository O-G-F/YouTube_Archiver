# Claude Code Handoff — Phase 9F

## Source of truth

旧Claude Code会話ではなく、以下を正確性の基準とする。

1. 現在のGitリポジトリ
2. commit履歴
3. migration
4. tests
5. README
6. このhandoff

実装前に必ずコードを読み、handoffの記述と実装が異なる場合はコードを優先して報告する。

## Current checkpoint

- Phase 9D commit: `f2e65f7`
- Phase 9E + Phase 9E.1 commit: `d35ea9f`
- Phase 9F: 未着手
- expected working tree: clean
- schema head: `e5f6a7b8c9d0`
- backend tests at checkpoint: 565 passed
- frontend tests at checkpoint: 64 passed
- frontend production build: passed

## Implemented through Phase 9E.1

- body archive staged validation
- comments-light archive profile
- disk capacity guard
- size estimator and batch planning
- Redis AOF persistence
- orphan reconciliation
- duplicate detection
- production-check / release-check
- local and trusted-proxy authentication
- scrypt password hashes
- signed session cookies
- CSRF protection
- CORS / Host / proxy boundary
- Redis-backed login rate limiting
- security headers
- production compose template
- backup/deploy/rollback foundations
- append-only audit events
- actor/client pseudonymization
- request/correlation IDs
- structured logging
- protected metrics
- liveness/readiness endpoints
- audit hash chain
- unsigned-to-signed boundary
- audit signing key rotation
- previous-key verification
- restore boundary
- retention checkpoints
- audit CLI/API/read-only UI

## Audit signing lifecycle

Migration `e5f6a7b8c9d0` adds:

- `chain_version`
- `signature_scheme`
- `signing_key_id`
- lifecycle checkpoint fields

Supported lifecycle:

- legacy unsigned segment
- explicit `signing_enabled` boundary
- HMAC signed segment
- explicit `key_rotated` boundary
- previous verification keys
- explicit `restore_boundary`
- retention checkpoint

Existing audit events must never be deleted, rewritten, or re-signed.

`restore_boundary` is a break-glass operation:
- CLI only
- dry-run by default
- explicit apply
- reason code required
- must not be used to conceal ordinary chain corruption

## Current data baseline

Expected current state:

- body_saved: 1930
- media_files(video): 1930
- comments table: 23,617,536 bytes
- raw_json stored: 0
- duplicate video media files: 0
- orphan jobs: 0
- active jobs: 0
- archive-check: 1930 present / 0 missing

Do not change these values through downloads during Phase 9F.

## Phase 9F objective

Implement and validate production backup integrity and disaster-recovery acceptance without affecting the current environment.

Main deliverables:

1. Restore-boundary break-glass hardening
2. Fresh-install migration rehearsal
3. Upgrade migration rehearsal from the pre-9E.1 revision
4. Backup artifact manifest
5. Manifest integrity verification CLI
6. Archive manifest
7. Isolated temporary restore rehearsal
8. Current/previous/missing audit signing-key recovery tests
9. Pseudonym-key recovery tests
10. Redis AOF/reconcile recovery policy
11. Machine-readable restore acceptance report
12. Non-destructive restore rehearsal and backup verification scripts
13. Production-check/release-check backup integration
14. Read-only backup/recovery readiness UI
15. Incident and recovery runbook updates

## Migration requirements

Test both:

### Fresh install

- empty temporary Postgres
- apply all migrations through `e5f6a7b8c9d0`
- no manual ALTER
- audit tables valid
- `audit_checkpoints.up_to_event_id` nullable
- `audit_checkpoints.reason` nullable
- signing boundary creation succeeds
- signed chain verifies

### Upgrade

- start from the revision immediately before `e5f6a7b8c9d0`
- insert representative legacy audit data
- upgrade to head
- preserve all legacy events
- do not re-sign or delete events
- create explicit lifecycle boundary
- verify chain successfully
- no manual ALTER

Production downgrade of audit migrations must remain prohibited/documented.

## Isolated restore requirements

Use a unique temporary Compose project and temporary volumes.

Never use or delete the current project's:

- Postgres volume
- Redis volume
- archive root
- secrets
- data
- logs

Restore rehearsal must verify:

- DB restore
- schema head
- preflight
- production-check
- release-check
- audit verify
- archive check
- duplicate check
- orphan dry-run
- authentication
- CSRF
- protected metrics
- readiness
- media Range using an existing fixture only
- no new download

## Signing-key recovery scenarios

Verify in a temporary environment:

- current key available → PASS
- current + required previous key → PASS
- required previous key absent → missing verification key / FAIL
- previous key restored → PASS
- incorrect current key → FAIL
- restore boundary requires dry-run and explicit apply
- no event deletion or re-signing

Pseudonymization key must remain separate from audit signing keys.

## Non-negotiable restrictions

Do not:

- download videos
- run full body archive
- run `metadata-run --all --confirm`
- use `--include-permanent`
- delete permanent failures
- delete current Docker volumes
- run `docker compose down -v` against the current project
- rewrite or delete audit events
- commit `.env`
- commit secrets or cookies
- commit archives, logs, data, dumps, or temporary restore files
- expose passwords, hashes, session secrets, cookies, tokens, raw identities, host paths, or raw_json

## Required first response in the new session

Before editing code:

1. Show `git status --short`
2. Show current short HEAD
3. Inspect commits `f2e65f7` and `d35ea9f`
4. Confirm migration head
5. Inspect the existing Phase 9E/9E.1 implementation
6. Inspect existing backup/deploy scripts
7. Compare the code against this handoff
8. Report:
   - confirmed current state
   - discrepancies
   - Phase 9F implementation plan
   - proposed migration-test strategy
   - temporary restore isolation strategy

Do not begin implementation until that audit is complete.
