# Contributing

Thanks for your interest! This is a **local single-user** hobby project shared as
an early public beta — contributions are welcome, but please keep the scope and
safety constraints in mind.

## Ground rules

- **Never commit secrets or personal data.** No `.env`, cookies, tokens, DB
  dumps, backups, release artifacts, real URLs/IPs/emails, host paths, or archive
  media. `.gitignore` / `.dockerignore` guard most of this — do not override them.
- **Do not weaken the security posture.** The release-check FAILs on the known
  CRITICAL CVEs by design; don't flip it to PASS, and don't add active
  vulnerability exceptions without the documented operator-approval process.
- **No unattended bulk downloading** in code paths or tests. Tests must not fetch
  from YouTube, download media, or require real cookies/DB.

## Dev setup

```bash
# backend (Python 3.12)
python -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements.lock && pip install -e '.[dev]'

# frontend
cd frontend && npm ci
```

## Before opening a PR

```bash
# backend tests
.venv/bin/python -m pytest -q

# frontend tests + production build
cd frontend && npx vitest run && npm run build
```

- Match the surrounding code style; keep changes focused.
- New user-facing behavior should have a test.
- Update `CHANGELOG.md` under *Unreleased* for notable changes.

## Pull requests

Open a PR against `main` and fill in the template. CI runs the backend + frontend
tests, the frontend build, and static secret/path guards — all without any
secrets, real database, or network downloads.
