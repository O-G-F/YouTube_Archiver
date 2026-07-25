"""Phase 9F.1: backup-set manifest (v2) + runtime acceptance.

The v2 manifest must identify the whole backup SET needed for recovery — dump,
archive-manifest linkage, audit chain head, build + operational state — carry a
canonical integrity hash (SHA-256 or HMAC), stay backward compatible with v1,
and never leak paths/secrets/identities.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from app.config import get_settings
from app.services import audit
from app.services import backup_manifest as bm

runner = CliRunner()


def _dump(tmp_path, name="db-20260718-000000.sql.gz", data=b"dump-bytes" * 64):
    art = tmp_path / name
    art.write_bytes(data)
    return art


def _archive_manifest_file(session, settings, tmp_path, name="archive-manifest-x.json"):
    out = tmp_path / name
    bm.write_archive_manifest(session, settings, out_path=out, hash_limit=0)
    return out


def _write_v2(session, settings, tmp_path, **kw):
    art = kw.pop("artifact", None) or _dump(tmp_path)
    am = kw.pop("archive_manifest_path", None)
    if am is None:
        am = _archive_manifest_file(session, settings, tmp_path)
    audit.record_event(session, settings, event_type="pre_backup", category="ops")
    session.commit()
    from app.models import AuditEvent

    head = session.query(AuditEvent).order_by(AuditEvent.id.desc()).first()
    m = bm.write_backup_manifest(
        art, schema_head="e5f6a7b8c9d0",
        build={"app_version": "0.1.0", "build_id": "testbuild"},
        active_jobs=kw.pop("active_jobs", 0),
        audit_head=(head.id, head.event_hash),
        archive_manifest_path=am, **kw)
    return art, art.with_name(art.name + ".manifest.json"), m


# --------------------------------------------------------------------------- #
# 1. v2 schema completeness + valid roundtrip
# --------------------------------------------------------------------------- #
def test_v2_manifest_contains_required_backup_set_fields(settings, session, tmp_path):
    _, mf, m = _write_v2(session, settings, tmp_path)
    for field in ("manifest_version", "backup_id", "created_at", "completed",
                  "app_version", "build_id", "schema_head", "artifact", "size_bytes",
                  "sha256", "active_jobs_at_backup", "audit_head_event_id",
                  "audit_head_event_hash", "archive_manifest", "redis_recovery_mode",
                  "encrypted", "integrity"):
        assert field in m, f"missing {field}"
    assert m["manifest_version"] == 2 and m["completed"] is True
    assert m["redis_recovery_mode"] == "empty_redis_then_reconcile"
    assert m["encrypted"] is False
    assert m["archive_manifest"]["artifact"].startswith("archive-manifest")
    assert m["integrity"]["scheme"] == "sha256" and len(m["integrity"]["hash"]) == 64

    r = bm.verify_backup_manifest(mf, session=session)
    assert r["ok"] and r["reason"] is None and r["manifest_version"] == 2
    assert r["backup_id"] == m["backup_id"]
    assert "legacy_manifest_v1" not in r["warnings"]
    assert "audit_head_not_checked" not in r["warnings"]


def test_v2_dump_checksum_and_size_mismatch(settings, session, tmp_path):
    art, mf, _ = _write_v2(session, settings, tmp_path)
    art.write_bytes(b"tamperbyte" * 64)  # same size
    assert bm.verify_backup_manifest(mf)["reason"] == "sha256_mismatch"
    art.write_bytes(b"short")
    assert bm.verify_backup_manifest(mf)["reason"] == "size_mismatch"
    art.unlink()
    assert bm.verify_backup_manifest(mf)["reason"] == "artifact_missing"


def test_v2_manifest_integrity_mismatch(settings, session, tmp_path):
    _, mf, _ = _write_v2(session, settings, tmp_path)
    data = json.loads(mf.read_text("utf-8"))
    data["schema_head"] = "ffffffffffff"  # field tampered, integrity not recomputed
    mf.write_text(json.dumps(data), "utf-8")
    r = bm.verify_backup_manifest(mf)
    assert not r["ok"] and r["reason"] == "manifest_integrity_mismatch"


def test_v2_archive_manifest_linkage_mismatch_and_missing(settings, session, tmp_path):
    _, mf, m = _write_v2(session, settings, tmp_path)
    am = tmp_path / m["archive_manifest"]["artifact"]
    original = am.read_text("utf-8")
    am.write_text(original + " ", "utf-8")
    assert bm.verify_backup_manifest(mf)["reason"] == "archive_manifest_mismatch"
    am.unlink()
    assert bm.verify_backup_manifest(mf)["reason"] == "archive_manifest_missing"


def test_v2_completed_false_rejected(settings, session, tmp_path):
    _, mf, _ = _write_v2(session, settings, tmp_path, completed=False)
    r = bm.verify_backup_manifest(mf)
    assert not r["ok"] and r["reason"] == "incomplete_backup"


def test_v2_active_jobs_recorded_and_warned(settings, session, tmp_path):
    _, mf, m = _write_v2(session, settings, tmp_path, active_jobs=3)
    assert m["active_jobs_at_backup"] == 3
    r = bm.verify_backup_manifest(mf, session=session)
    assert r["ok"] and any(w.startswith("active_jobs_at_backup=3") for w in r["warnings"])


def test_v2_audit_head_mismatch_detected(settings, session, tmp_path):
    _, mf, m = _write_v2(session, settings, tmp_path)
    session.execute(text("UPDATE audit_events SET event_hash='deadbeef' WHERE id=:i"),
                    {"i": m["audit_head_event_id"]})
    session.commit()
    r = bm.verify_backup_manifest(mf, session=session)
    assert not r["ok"] and r["reason"] == "audit_head_mismatch"


def test_v2_audit_head_offline_warns_not_fails(settings, session, tmp_path):
    _, mf, _ = _write_v2(session, settings, tmp_path)
    r = bm.verify_backup_manifest(mf, session=None)
    assert r["ok"] and "audit_head_not_checked" in r["warnings"]


def test_v2_traversal_rejected_for_archive_link(settings, session, tmp_path):
    _, mf, _ = _write_v2(session, settings, tmp_path)
    data = json.loads(mf.read_text("utf-8"))
    data["archive_manifest"]["artifact"] = "../../etc/passwd"
    data["integrity"] = bm._integrity_for(data, None)  # keep integrity valid -> hit the name check
    mf.write_text(json.dumps(data), "utf-8")
    r = bm.verify_backup_manifest(mf)
    assert not r["ok"] and r["reason"] == "unsafe_artifact_name"


# --------------------------------------------------------------------------- #
# 2. HMAC signing + legacy compatibility
# --------------------------------------------------------------------------- #
def test_v2_hmac_signed_manifest_roundtrip_and_wrong_key(settings, session, tmp_path):
    art = _dump(tmp_path)
    am = _archive_manifest_file(session, settings, tmp_path)
    m = bm.write_backup_manifest(art, schema_head="e5f6a7b8c9d0",
                                 archive_manifest_path=am, hmac_key="backup-key-1")
    assert m["integrity"]["scheme"] == "hmac_sha256"
    mf = art.with_name(art.name + ".manifest.json")
    assert bm.verify_backup_manifest(mf, hmac_key="backup-key-1")["ok"]
    r_wrong = bm.verify_backup_manifest(mf, hmac_key="backup-key-2")
    assert not r_wrong["ok"] and r_wrong["reason"] == "manifest_integrity_mismatch"
    r_missing = bm.verify_backup_manifest(mf, hmac_key=None)
    assert not r_missing["ok"] and r_missing["reason"] == "integrity_key_missing"
    assert "backup-key-1" not in mf.read_text("utf-8")


def test_legacy_v1_manifest_still_verifies_with_warning(settings, tmp_path):
    art = _dump(tmp_path, name="db-legacy.sql.gz")
    digest, size = bm.sha256_file(art)
    legacy = {"manifest_version": 1, "kind": "backup_db_dump", "artifact": art.name,
              "size_bytes": size, "sha256": digest, "schema_head": "e5f6a7b8c9d0",
              "created_at": "2026-07-17T00:00:00"}
    mf = art.with_name(art.name + ".manifest.json")
    mf.write_text(json.dumps(legacy), "utf-8")
    r = bm.verify_backup_manifest(mf)
    assert r["ok"] and "legacy_manifest_v1" in r["warnings"]
    art.write_bytes(b"x" * size)
    assert bm.verify_backup_manifest(mf)["reason"] == "sha256_mismatch"


# --------------------------------------------------------------------------- #
# 3. no-leak + summary + release-check integration
# --------------------------------------------------------------------------- #
def test_v2_manifest_and_summary_no_leak(settings, session, tmp_path, monkeypatch):
    summary = tmp_path / "cfg" / "last_backup_manifest.json"
    art, mf, _ = _write_v2(session, settings, tmp_path, summary_file=summary)
    blob = mf.read_text("utf-8") + summary.read_text("utf-8")
    for bad in (str(tmp_path), str(settings.archive_root), "/Users/", "/home/",
                "@", "password", "cookie"):
        assert bad not in blob, f"leak: {bad}"

    monkeypatch.setenv("BACKUP_MANIFEST_SUMMARY_FILE", str(summary))
    get_settings.cache_clear()
    s = bm.read_backup_manifest_summary(get_settings())
    assert s["manifest_version"] == 2 and s["completed"] is True
    assert s["backup_id"] and s["audit_head_event_id"] and s["redis_recovery_mode"]
    assert s["archive_manifest_artifact"] and s["archive_manifest_sha256"]
    assert s["integrity_scheme"] == "sha256" and s["active_jobs_at_backup"] == 0
    assert str(tmp_path) not in json.dumps(s)


def test_release_check_backup_set_complete_statuses(settings, session, tmp_path, monkeypatch):
    from app.services import production_check as pc

    summary = tmp_path / "cfg" / "last_backup_manifest.json"
    monkeypatch.setenv("BACKUP_MANIFEST_SUMMARY_FILE", str(summary))
    get_settings.cache_clear()

    # no summary yet -> WARN
    r = pc.release_check(session, get_settings())
    by = {c["name"]: c for c in r["checks"]}
    assert by["backup_set_complete"]["status"] == "warn"

    # legacy v1 summary -> WARN
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps({"manifest_version": 1, "artifact": "db-x.sql.gz",
                                   "sha256": "0" * 64, "size_bytes": 1}), "utf-8")
    r = pc.release_check(session, get_settings())
    by = {c["name"]: c for c in r["checks"]}
    assert by["backup_set_complete"]["status"] == "warn" and "v1" in by["backup_set_complete"]["detail"]

    # full v2 set -> PASS
    _write_v2(session, settings, tmp_path, summary_file=summary)
    r = pc.release_check(session, get_settings())
    by = {c["name"]: c for c in r["checks"]}
    assert by["backup_set_complete"]["status"] == "pass"
    assert "idle at backup" in by["backup_set_complete"]["detail"]
    blob = json.dumps(r)
    assert str(tmp_path) not in blob and "/Users/" not in blob

    # v2 with active jobs at backup -> WARN
    _write_v2(session, settings, tmp_path, summary_file=summary, active_jobs=2,
              artifact=_dump(tmp_path, name="db-active.sql.gz"))
    r = pc.release_check(session, get_settings())
    by = {c["name"]: c for c in r["checks"]}
    assert by["backup_set_complete"]["status"] == "warn"
    assert "active_jobs=2" in by["backup_set_complete"]["detail"]


def test_cli_write_and_verify_v2_end_to_end(settings, session, tmp_path, monkeypatch):
    from app.cli import app as cli_app

    audit.record_event(session, get_settings(), event_type="seed", category="ops")
    session.commit()
    art = _dump(tmp_path, name="db-cli.sql.gz")
    am = _archive_manifest_file(session, settings, tmp_path, name="archive-manifest-cli.json")
    summary = tmp_path / "cfg" / "sum.json"
    marker = tmp_path / "cfg" / "verified"
    monkeypatch.setenv("BACKUP_MANIFEST_SUMMARY_FILE", str(summary))
    monkeypatch.setenv("BACKUP_VERIFIED_MARKER_FILE", str(marker))
    get_settings.cache_clear()

    r = runner.invoke(cli_app, ["backup", "write-manifest", "--artifact", str(art),
                                "--archive-manifest", str(am)])
    assert r.exit_code == 0, r.output
    assert "backup_id=bk-" in r.output and "audit_head=#" in r.output
    assert "active_jobs=0" in r.output and "integrity=sha256" in r.output

    mf = art.with_name(art.name + ".manifest.json")
    r2 = runner.invoke(cli_app, ["backup", "verify-manifest", "--manifest", str(mf),
                                 "--write-marker"])
    assert r2.exit_code == 0, r2.output
    assert "OK (v2)" in r2.output and marker.is_file()
    for out in (r.output, r2.output):
        assert str(tmp_path) not in out and "/Users/" not in out

    # combination mismatch: swap in a DIFFERENT archive manifest under the same name
    bm.write_archive_manifest(session, settings, out_path=am, hash_limit=0)
    am.write_text(am.read_text("utf-8") + "\n", "utf-8")
    r3 = runner.invoke(cli_app, ["backup", "verify-manifest", "--manifest", str(mf)])
    assert r3.exit_code == 1 and "archive_manifest_mismatch" in r3.output
