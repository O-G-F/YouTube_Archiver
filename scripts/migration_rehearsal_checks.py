"""Phase 9F: migration-rehearsal assertions (run by scripts/migration-rehearsal.sh).

Runs against the TEMPORARY rehearsal Postgres selected by ``DATABASE_URL`` — it
never touches the real stack. Subcommands:

  fresh-verify    after `alembic upgrade head` on an EMPTY database: schema head,
                  audit tables + nullability, unsigned->signed boundary, verify.
  seed-legacy     at the pre-9E.1 revision: insert a representative LEGACY (v1,
                  unsigned) audit chain via raw SQL (the ORM would not match the
                  old schema). Writes the seeded ids/hashes to --state.
  upgrade-verify  after `alembic upgrade head`: legacy events preserved byte-for-
                  byte (no re-sign/delete), lifecycle boundary + signed suffix
                  verifies, nullability relaxed.

Reports are machine-readable JSON (no secrets, no DATABASE_URL, no host paths).
A refusal guard aborts when DATABASE_URL does not look like a rehearsal DB.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

REHEARSAL_DB_TOKEN = "rehearsal"
HEAD_DEFAULT = "e5f6a7b8c9d0"


def _engine():
    url = os.environ.get("DATABASE_URL", "")
    if REHEARSAL_DB_TOKEN not in url:
        print(json.dumps({"ok": False, "error": "guard: DATABASE_URL is not a rehearsal database"}))
        sys.exit(9)
    return create_engine(url, future=True)


def _check(checks: list, name: str, ok: bool, detail: str) -> bool:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})
    return ok


def _db_head(conn) -> str | None:
    try:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # noqa: BLE001
        return None


def _nullable(conn, table: str, column: str) -> str | None:
    return conn.execute(text(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name=:t AND column_name=:c"), {"t": table, "c": column}).scalar()


def _table_exists(conn, table: str) -> bool:
    return bool(conn.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_name=:t"), {"t": table}).scalar())


def _write_report(path: str | None, report: dict) -> None:
    report["ok"] = all(c["ok"] for c in report["checks"])
    report["generated_at"] = datetime.utcnow().isoformat()
    out = json.dumps(report, indent=2, sort_keys=True)
    if path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(out + "\n", encoding="utf-8")
    print(out)
    sys.exit(0 if report["ok"] else 1)


# --------------------------------------------------------------------------- #
# fresh install
# --------------------------------------------------------------------------- #
def fresh_verify(args) -> None:
    from app.config import get_settings
    from app.services import audit

    engine = _engine()
    checks: list[dict] = []
    with engine.connect() as conn:
        head = _db_head(conn)
        _check(checks, "schema_head", head == args.expected_head,
               f"db={head} expected={args.expected_head}")
        _check(checks, "audit_tables_exist",
               _table_exists(conn, "audit_events") and _table_exists(conn, "audit_checkpoints"),
               "audit_events + audit_checkpoints present")
        _check(checks, "up_to_event_id_nullable",
               _nullable(conn, "audit_checkpoints", "up_to_event_id") == "YES",
               f"is_nullable={_nullable(conn, 'audit_checkpoints', 'up_to_event_id')}")
        _check(checks, "reason_nullable",
               _nullable(conn, "audit_checkpoints", "reason") == "YES",
               f"is_nullable={_nullable(conn, 'audit_checkpoints', 'reason')}")

    signed = get_settings()  # AUDIT_HMAC_KEY_FILE/ID come from the rehearsal env
    kid, key = signed.audit_current_signing()
    _check(checks, "signing_key_configured", bool(key), f"current key id={kid}")
    dev = signed.model_copy(update={"audit_hmac_key_file": "", "audit_hmac_key_id": ""})

    with Session(engine) as s:
        # legacy-style unsigned prefix, then the production enablement path
        audit.record_event(s, dev, event_type="rehearsal_unsigned_0", category="ops")
        audit.record_event(s, dev, event_type="rehearsal_unsigned_1", category="ops")
        s.commit()
        b = audit.establish_signing_boundary(s, signed, reason_code="fresh_rehearsal",
                                             checkpoint_type="signing_enabled", apply=True)
        _check(checks, "signing_boundary_created", bool(b.get("applied")),
               f"boundary at event #{b.get('previous_event_id')}")
        audit.record_event(s, signed, event_type="rehearsal_signed_0", category="ops")
        s.commit()
        v = audit.verify_chain(s, signed)
        _check(checks, "signed_chain_verifies",
               v["valid"] and v["segment_count"] == 2 and v["unsigned_event_count"] == 2,
               f"valid={v['valid']} warnings={v['valid_with_warnings']} "
               f"segments={v['segment_count']} unsigned={v['unsigned_event_count']} "
               f"reason={v['failure_reason_code']}")

    _write_report(args.report, {"rehearsal": "fresh_install", "manual_alter_used": False,
                                "checks": checks})


# --------------------------------------------------------------------------- #
# upgrade from pre-9E.1
# --------------------------------------------------------------------------- #
_LEGACY_EVENTS = [
    # representative categories incl. a security-retention one
    ("login_success", "auth", "info", "success"),
    ("archive_enqueue_created", "archive", "info", "success"),
    ("production_check_run", "ops", "info", "success"),
    ("login_failure", "auth", "warning", "failure"),
]


def seed_legacy(args) -> None:
    """Insert a v1 (unsigned, pre-9E.1) audit chain via raw SQL at rev d4e5f6a7b8c9."""
    from app.services.audit import UNSIGNED, _V1_FIELDS, _canon, _hash_with

    engine = _engine()
    base = datetime(2026, 7, 1, 0, 0, 0)  # fixed, µs-free -> canonical is stable
    seeded = []
    prev = None
    with engine.begin() as conn:
        if not _table_exists(conn, "audit_events"):
            print(json.dumps({"ok": False, "error": "audit_events missing (wrong revision?)"}))
            sys.exit(1)
        has_v2 = conn.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_name='audit_events' "
            "AND column_name='chain_version'")).scalar()
        if has_v2:
            print(json.dumps({"ok": False, "error": "chain_version already present — not at pre-9E.1"}))
            sys.exit(1)
        for i, (etype, cat, sev, outcome) in enumerate(_LEGACY_EVENTS):
            occurred = base + timedelta(minutes=i)
            payload = {"occurred_at": occurred.isoformat(), "event_type": etype, "category": cat,
                       "severity": sev, "outcome": outcome, "actor_kind": "system",
                       "actor_id_hash": None, "client_id_hash": None, "request_id": None,
                       "correlation_id": None, "resource_type": None, "resource_id": None,
                       "action": None, "reason_code": None, "metadata_json": None}
            ev_hash = _hash_with(UNSIGNED, None, _canon(_V1_FIELDS, payload), prev)
            row = conn.execute(text(
                "INSERT INTO audit_events (occurred_at, event_type, category, severity, outcome, "
                "actor_kind, previous_hash, event_hash, created_at) VALUES "
                "(:o, :t, :c, :s, :u, 'system', :p, :h, :o) RETURNING id"),
                {"o": occurred, "t": etype, "c": cat, "s": sev, "u": outcome,
                 "p": prev, "h": ev_hash}).scalar()
            seeded.append({"id": row, "event_type": etype, "event_hash": ev_hash})
            prev = ev_hash
    Path(args.state).write_text(json.dumps({"seeded": seeded}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "seeded_count": len(seeded)}))


def upgrade_verify(args) -> None:
    from app.config import get_settings
    from app.services import audit

    engine = _engine()
    state = json.loads(Path(args.state).read_text(encoding="utf-8"))["seeded"]
    checks: list[dict] = []
    with engine.connect() as conn:
        head = _db_head(conn)
        _check(checks, "schema_head", head == args.expected_head,
               f"db={head} expected={args.expected_head}")
        _check(checks, "up_to_event_id_nullable",
               _nullable(conn, "audit_checkpoints", "up_to_event_id") == "YES",
               f"is_nullable={_nullable(conn, 'audit_checkpoints', 'up_to_event_id')}")
        _check(checks, "reason_nullable",
               _nullable(conn, "audit_checkpoints", "reason") == "YES",
               f"is_nullable={_nullable(conn, 'audit_checkpoints', 'reason')}")

        rows = conn.execute(text(
            "SELECT id, event_hash, chain_version, signature_scheme, signing_key_id "
            "FROM audit_events ORDER BY id ASC")).all()
        by_id = {r[0]: r for r in rows}
        preserved = (len(rows) == len(state)
                     and all(by_id.get(e["id"]) and by_id[e["id"]][1] == e["event_hash"]
                             for e in state))
        _check(checks, "legacy_events_preserved", preserved,
               f"{len(state)} legacy events kept, hashes unchanged (no delete/re-sign)")
        legacy_defaults = all(r[2] == 1 and r[3] == "sha256_unsigned" and r[4] == "legacy"
                              for r in rows)
        _check(checks, "legacy_defaults_applied", legacy_defaults,
               "chain_version=1 / sha256_unsigned / key id 'legacy' on all pre-existing rows")

    signed = get_settings()
    kid, key = signed.audit_current_signing()
    _check(checks, "signing_key_configured", bool(key), f"current key id={kid}")

    with Session(engine) as s:
        v0 = audit.verify_chain(s, signed)
        _check(checks, "legacy_chain_verifies_before_boundary", v0["valid"],
               f"valid={v0['valid']} unsigned={v0['unsigned_event_count']}")
        b = audit.establish_signing_boundary(s, signed, reason_code="upgrade_rehearsal",
                                             checkpoint_type="signing_enabled", apply=True)
        _check(checks, "lifecycle_boundary_created", bool(b.get("applied")),
               f"signing_enabled at event #{b.get('previous_event_id')}")
        audit.record_event(s, signed, event_type="rehearsal_signed_after_upgrade", category="ops")
        s.commit()
        v = audit.verify_chain(s, signed)
        _check(checks, "chain_verifies_after_upgrade",
               v["valid"] and v["unsigned_event_count"] == len(state) and v["segment_count"] == 2,
               f"valid={v['valid']} warnings={v['valid_with_warnings']} "
               f"segments={v['segment_count']} unsigned={v['unsigned_event_count']} "
               f"reason={v['failure_reason_code']}")

    _write_report(args.report, {"rehearsal": "upgrade_from_pre_9e1",
                                "from_revision": "d4e5f6a7b8c9",
                                "manual_alter_used": False,
                                "downgrade_executed": False,
                                "checks": checks})


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fresh-verify")
    f.add_argument("--expected-head", default=HEAD_DEFAULT)
    f.add_argument("--report", default=None)
    s = sub.add_parser("seed-legacy")
    s.add_argument("--state", required=True)
    u = sub.add_parser("upgrade-verify")
    u.add_argument("--expected-head", default=HEAD_DEFAULT)
    u.add_argument("--state", required=True)
    u.add_argument("--report", default=None)
    args = ap.parse_args()
    {"fresh-verify": fresh_verify, "seed-legacy": seed_legacy,
     "upgrade-verify": upgrade_verify}[args.cmd](args)


if __name__ == "__main__":
    main()
