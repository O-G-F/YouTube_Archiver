"""Phase 9F: production backup integrity + disaster-recovery acceptance.

Covers: restore-boundary break-glass hardening, backup/archive manifests +
verification, release-check backup integration, signing-key / pseudonym-key
recovery matrices, rehearsal script/compose static guards, and no-leak checks.
No real downloads; no secret/key/path leaks; never touches real volumes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from app.config import get_settings
from app.services import audit

REPO = Path(__file__).resolve().parents[1]
runner = CliRunner()

_KEYS = {"a": "key-AAAAAAAA", "b": "key-BBBBBBBB"}


def _settings(monkeypatch, tmp_path, *, key_id=None, prev_ids=(), **extra):
    for k in ("AUDIT_HMAC_KEY_FILE", "AUDIT_HMAC_KEY_ID",
              "AUDIT_HMAC_PREVIOUS_KEY_FILES", "AUDIT_HMAC_PREVIOUS_KEY_IDS",
              "AUDIT_PSEUDONYM_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    if key_id:
        f = tmp_path / f"k_{key_id}"
        f.write_text(_KEYS[key_id], "utf-8")
        monkeypatch.setenv("AUDIT_HMAC_KEY_FILE", str(f))
        monkeypatch.setenv("AUDIT_HMAC_KEY_ID", key_id)
    if prev_ids:
        files, ids = [], []
        for pid in prev_ids:
            pf = tmp_path / f"p_{pid}"
            pf.write_text(_KEYS[pid], "utf-8")
            files.append(str(pf))
            ids.append(pid)
        monkeypatch.setenv("AUDIT_HMAC_PREVIOUS_KEY_FILES", ",".join(files))
        monkeypatch.setenv("AUDIT_HMAC_PREVIOUS_KEY_IDS", ",".join(ids))
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    return get_settings()


# --------------------------------------------------------------------------- #
# 1. restore-boundary break-glass hardening
# --------------------------------------------------------------------------- #
def test_restore_boundary_requires_reason_code(settings, session, tmp_path, monkeypatch):
    s = _settings(monkeypatch, tmp_path, key_id="a")
    audit.record_event(session, s, event_type="e0", category="ops")
    r = audit.establish_signing_boundary(session, s, reason_code="",
                                         checkpoint_type="restore_boundary", apply=False)
    assert r["ok"] is False and "reason code" in r["reason"]
    r2 = audit.establish_signing_boundary(session, s, reason_code=None,
                                          checkpoint_type="restore_boundary", apply=True)
    assert r2["ok"] is False


def test_restore_boundary_plan_embeds_pre_verify(settings, session, tmp_path, monkeypatch):
    dev = _settings(monkeypatch, tmp_path)
    audit.record_event(session, dev, event_type="u0", category="ops")
    s = _settings(monkeypatch, tmp_path, key_id="a")
    audit.record_event(session, s, event_type="s0", category="ops")  # mixed, no boundary
    plan = audit.establish_signing_boundary(session, s, reason_code="db_restore",
                                            checkpoint_type="restore_boundary", apply=False)
    assert plan["ok"] and plan["dry_run"] is True
    assert plan["pre_boundary_chain_valid"] is False
    assert plan["pre_boundary_failure_reason_code"] == "unexpected_regime_change"


def test_restore_boundary_apply_records_warning_event(settings, session, tmp_path, monkeypatch):
    s = _settings(monkeypatch, tmp_path, key_id="a")
    audit.record_event(session, s, event_type="e0", category="ops")
    r = audit.establish_signing_boundary(session, s, reason_code="db_restore",
                                         checkpoint_type="restore_boundary", apply=True)
    assert r["ok"] and r["applied"] is True
    from app.models import AuditEvent

    ev = (session.query(AuditEvent).filter(AuditEvent.event_type == "audit_signing_boundary")
          .order_by(AuditEvent.id.desc()).first())
    assert ev is not None and ev.severity == "warning" and ev.action == "restore_boundary"
    assert ev.reason_code == "db_restore"
    assert ev.metadata_json and ev.metadata_json.get("pre_boundary_chain_valid") is True
    assert audit.verify_chain(session, s)["valid"]


def test_signing_enabled_boundary_still_defaults_and_info(settings, session, tmp_path, monkeypatch):
    dev = _settings(monkeypatch, tmp_path)
    audit.record_event(session, dev, event_type="u0", category="ops")
    s = _settings(monkeypatch, tmp_path, key_id="a")
    r = audit.establish_signing_boundary(session, s, reason_code="enable", apply=True)
    assert r["ok"] and r["applied"]
    from app.models import AuditEvent

    ev = (session.query(AuditEvent).filter(AuditEvent.event_type == "audit_signing_boundary")
          .order_by(AuditEvent.id.desc()).first())
    assert ev.severity == "info"


def test_cli_restore_boundary_gates(settings, session, tmp_path, monkeypatch):
    from app.cli import app as cli_app

    s = _settings(monkeypatch, tmp_path, key_id="a")
    audit.record_event(session, s, event_type="e0", category="ops")
    session.commit()

    # missing reason-code -> exit 2
    r = runner.invoke(cli_app, ["audit", "establish-signing-boundary",
                                "--type", "restore_boundary", "--dry-run"])
    assert r.exit_code == 2 and "reason-code" in r.output

    # dry-run with reason works, warns, applies nothing
    r = runner.invoke(cli_app, ["audit", "establish-signing-boundary",
                                "--type", "restore_boundary", "--reason-code", "db_restore"])
    assert r.exit_code == 0 and "break-glass" in r.output and "dry-run" in r.output

    # apply without --confirm-restore -> exit 2
    r = runner.invoke(cli_app, ["audit", "establish-signing-boundary",
                                "--type", "restore_boundary", "--reason-code", "db_restore", "--apply"])
    assert r.exit_code == 2 and "confirm-restore" in r.output

    # apply with --confirm-restore -> applied
    r = runner.invoke(cli_app, ["audit", "establish-signing-boundary",
                                "--type", "restore_boundary", "--reason-code", "db_restore",
                                "--apply", "--confirm-restore"])
    assert r.exit_code == 0 and "APPLIED" in r.output


def test_no_audit_write_api_routes():
    """restore_boundary (and any audit mutation) must stay CLI-only."""
    from app.main import app

    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if path.startswith("/api/audit"):
            assert methods <= {"GET", "HEAD", "OPTIONS"}, f"mutating audit route: {path} {methods}"


# --------------------------------------------------------------------------- #
# 2. signing-key recovery matrix (Phase 9F scenarios)
# --------------------------------------------------------------------------- #
def _rotated_chain(session, tmp_path, monkeypatch):
    sa = _settings(monkeypatch, tmp_path, key_id="a")
    audit.record_event(session, sa, event_type="s0", category="ops")
    sb = _settings(monkeypatch, tmp_path, key_id="b", prev_ids=("a",))
    audit.rotate_key(session, sb, reason_code="rot", apply=True)
    audit.record_event(session, sb, event_type="s1", category="ops")
    return sb


def test_key_recovery_current_only_pass(settings, session, tmp_path, monkeypatch):
    s = _settings(monkeypatch, tmp_path, key_id="a")
    audit.record_event(session, s, event_type="s0", category="ops")
    assert audit.verify_chain(session, s)["valid"]


def test_key_recovery_current_plus_previous_pass(settings, session, tmp_path, monkeypatch):
    sb = _rotated_chain(session, tmp_path, monkeypatch)
    v = audit.verify_chain(session, sb)
    assert v["valid"] and v["segment_count"] == 2


def test_key_recovery_previous_absent_fail_then_restored_pass(settings, session, tmp_path, monkeypatch):
    _rotated_chain(session, tmp_path, monkeypatch)
    # previous key absent -> FAIL with missing_verification_key
    sb_missing = _settings(monkeypatch, tmp_path, key_id="b")
    v = audit.verify_chain(session, sb_missing)
    assert not v["valid"] and v["failure_reason_code"] == "missing_verification_key"
    assert "a" in v["missing_verification_keys"]
    # previous key restored -> PASS
    sb_restored = _settings(monkeypatch, tmp_path, key_id="b", prev_ids=("a",))
    assert audit.verify_chain(session, sb_restored)["valid"]


def test_key_recovery_incorrect_current_key_fail(settings, session, tmp_path, monkeypatch):
    s = _settings(monkeypatch, tmp_path, key_id="a")
    audit.record_event(session, s, event_type="s0", category="ops")
    # same key id, wrong key VALUE -> event_hash_mismatch
    (tmp_path / "k_a").write_text("key-WRONGWRONG", "utf-8")
    get_settings.cache_clear()
    v = audit.verify_chain(session, get_settings())
    assert not v["valid"] and v["failure_reason_code"] == "event_hash_mismatch"


# --------------------------------------------------------------------------- #
# 3. pseudonym-key separation (recovery semantics)
# --------------------------------------------------------------------------- #
def test_pseudonyms_stable_across_signing_rotation(settings, session, tmp_path, monkeypatch):
    pfile = tmp_path / "pseudo"
    pfile.write_text("pseudo-key-1", "utf-8")
    sa = _settings(monkeypatch, tmp_path, key_id="a", AUDIT_PSEUDONYM_KEY_FILE=str(pfile))
    h1 = audit.pseudonymize(sa, "admin@example.com", kind="admin")
    sb = _settings(monkeypatch, tmp_path, key_id="b", prev_ids=("a",),
                   AUDIT_PSEUDONYM_KEY_FILE=str(pfile))
    h2 = audit.pseudonymize(sb, "admin@example.com", kind="admin")
    assert h1 == h2  # signing rotation must NOT change pseudonyms


def test_pseudonym_key_change_changes_pseudonyms_but_chain_still_verifies(
        settings, session, tmp_path, monkeypatch):
    pfile = tmp_path / "pseudo"
    pfile.write_text("pseudo-key-1", "utf-8")
    s = _settings(monkeypatch, tmp_path, key_id="a", AUDIT_PSEUDONYM_KEY_FILE=str(pfile))
    h1 = audit.pseudonymize(s, "admin@example.com", kind="admin")
    audit.record_event(session, s, event_type="login_success", category="auth", actor_id_hash=h1)
    # pseudonym key replaced (e.g. lost + regenerated)
    pfile.write_text("pseudo-key-2", "utf-8")
    get_settings.cache_clear()
    s2 = get_settings()
    h2 = audit.pseudonymize(s2, "admin@example.com", kind="admin")
    assert h1 != h2  # correlation to old events breaks (documented consequence)
    audit.record_event(session, s2, event_type="login_success", category="auth", actor_id_hash=h2)
    assert audit.verify_chain(session, s2)["valid"]  # chain integrity is unaffected


def test_pseudonym_key_file_is_separate_from_signing_keys(settings, tmp_path, monkeypatch):
    pfile = tmp_path / "pseudo"
    pfile.write_text("pseudo-key-1", "utf-8")
    s = _settings(monkeypatch, tmp_path, key_id="a", AUDIT_PSEUDONYM_KEY_FILE=str(pfile))
    assert s.audit_pseudonymize_key() == "pseudo-key-1"
    assert s.audit_pseudonymize_key() != s.audit_current_signing()[1]
    assert s.audit_pseudonym_key_configured is True


# --------------------------------------------------------------------------- #
# 4. backup manifest + verification
# --------------------------------------------------------------------------- #
def test_backup_manifest_roundtrip_and_tamper(settings, tmp_path):
    from app.services import backup_manifest as bm

    art = tmp_path / "db-20260717-000000.sql.gz"
    art.write_bytes(b"fake-dump-bytes" * 100)
    summary = tmp_path / "cfg" / "last_backup_manifest.json"
    m = bm.write_backup_manifest(art, schema_head="e5f6a7b8c9d0", summary_file=summary)
    assert m["artifact"] == art.name and m["sha256"] and m["size_bytes"] == art.stat().st_size
    assert summary.is_file()

    mf = art.with_name(art.name + ".manifest.json")
    r = bm.verify_backup_manifest(mf)
    assert r["ok"] and r["reason"] is None and r["schema_head"] == "e5f6a7b8c9d0"

    art.write_bytes(b"tampered-bytes!" * 100)  # same length -> sha mismatch
    r2 = bm.verify_backup_manifest(mf)
    assert not r2["ok"] and r2["reason"] == "sha256_mismatch"

    art.write_bytes(b"short")  # size mismatch
    r3 = bm.verify_backup_manifest(mf)
    assert not r3["ok"] and r3["reason"] == "size_mismatch"

    art.unlink()
    r4 = bm.verify_backup_manifest(mf)
    assert not r4["ok"] and r4["reason"] == "artifact_missing"


def test_backup_manifest_rejects_traversal_artifact(settings, tmp_path):
    from app.services import backup_manifest as bm

    mf = tmp_path / "evil.manifest.json"
    mf.write_text(json.dumps({"artifact": "../../etc/passwd", "sha256": "0" * 64,
                              "size_bytes": 1}), "utf-8")
    r = bm.verify_backup_manifest(mf)
    assert not r["ok"] and r["reason"] == "unsafe_artifact_name"
    mf.write_text("not-json", "utf-8")
    assert bm.verify_backup_manifest(mf)["reason"] == "bad_manifest"


def _media_fixture(session, settings, vid: str, rel: str, data: bytes):
    from app.models import MediaFile, Video

    v = Video(youtube_video_id=vid, url=f"https://www.youtube.com/watch?v={vid}", title="T")
    session.add(v)
    session.flush()
    mf = MediaFile(video_id=v.id, media_type="video", path=rel)
    session.add(mf)
    session.flush()
    p = settings.archive_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return v, mf, p


def test_archive_manifest_roundtrip_detects_missing_and_mismatch(settings, session, tmp_path):
    from app.services import backup_manifest as bm

    _, _, p1 = _media_fixture(session, settings, "vidfix00001", "youtube/videos/c/v1/a.mp4", b"A" * 2048)
    _, _, p2 = _media_fixture(session, settings, "vidfix00002", "youtube/videos/c/v2/b.mp4", b"B" * 4096)
    session.commit()

    out = tmp_path / "archive-manifest.json"
    summary = bm.write_archive_manifest(session, settings, out_path=out, hash_limit=10)
    assert summary["db_video_media_files"] == 2 and summary["present"] == 2
    assert summary["hashed_count"] == 2 and summary["total_bytes"] == 2048 + 4096
    assert "entries" not in summary  # summary stays small

    r = bm.verify_archive_manifest(settings, manifest_path=out)
    assert r["ok"] and r["checked"] == 2 and r["missing"] == 0 and r["hash_mismatch"] == 0

    p1.write_bytes(b"X" * 2048)  # content change, same size
    r2 = bm.verify_archive_manifest(settings, manifest_path=out)
    assert not r2["ok"] and r2["hash_mismatch"] == 1 and "vidfix00001" in r2["mismatch_youtube_ids"]

    p2.write_bytes(b"B")  # size change
    r3 = bm.verify_archive_manifest(settings, manifest_path=out)
    assert r3["size_mismatch"] == 1

    p1.unlink()
    r4 = bm.verify_archive_manifest(settings, manifest_path=out)
    assert r4["missing"] == 1

    # --no-hashes path: only size/existence judged
    p1.write_bytes(b"Y" * 2048)
    p2.write_bytes(b"B" * 4096)
    r5 = bm.verify_archive_manifest(settings, manifest_path=out, check_hashes=False)
    assert r5["ok"] and r5["hash_checked"] == 0


def test_manifests_contain_no_absolute_paths(settings, session, tmp_path):
    from app.services import backup_manifest as bm

    _media_fixture(session, settings, "vidfix00003", "youtube/videos/c/v3/c.mp4", b"C" * 128)
    session.commit()
    art = tmp_path / "db-x.sql.gz"
    art.write_bytes(b"dump")
    bm.write_backup_manifest(art, schema_head="e5f6a7b8c9d0")
    out = tmp_path / "am.json"
    bm.write_archive_manifest(session, settings, out_path=out, hash_limit=0)
    for f in (art.with_name(art.name + ".manifest.json"), out):
        blob = f.read_text("utf-8")
        for bad in (str(tmp_path), str(settings.archive_root), "/Users/"):
            assert bad not in blob


def test_marker_helpers_and_summary_reader(settings, tmp_path, monkeypatch):
    from app.services import backup_manifest as bm

    marker = tmp_path / "cfg" / "last_backup_verified"
    assert bm.marker_age_hours(str(marker)) is None
    assert bm.marker_age_hours("") is None
    assert bm.touch_marker(str(marker)) is True
    age = bm.marker_age_hours(str(marker))
    assert age is not None and age < 1.0
    assert bm.touch_marker("") is False

    summary = tmp_path / "cfg" / "last_backup_manifest.json"
    monkeypatch.setenv("BACKUP_MANIFEST_SUMMARY_FILE", str(summary))
    get_settings.cache_clear()
    assert bm.read_backup_manifest_summary(get_settings()) is None  # missing file
    art = tmp_path / "db-y.sql.gz"
    art.write_bytes(b"dump2")
    bm.write_backup_manifest(art, schema_head="e5f6a7b8c9d0", summary_file=summary)
    s = bm.read_backup_manifest_summary(get_settings())
    assert s and s["artifact"] == "db-y.sql.gz" and s["schema_head"] == "e5f6a7b8c9d0"
    blob = json.dumps(s)
    assert str(tmp_path) not in blob


def test_backup_cli_status_and_verify_no_leak(settings, session, tmp_path, monkeypatch):
    from app.cli import app as cli_app

    art = tmp_path / "db-z.sql.gz"
    art.write_bytes(b"dump3" * 50)
    summary = tmp_path / "cfg" / "last_backup_manifest.json"
    marker = tmp_path / "cfg" / "last_backup_verified"
    monkeypatch.setenv("BACKUP_MANIFEST_SUMMARY_FILE", str(summary))
    monkeypatch.setenv("BACKUP_VERIFIED_MARKER_FILE", str(marker))
    get_settings.cache_clear()

    r = runner.invoke(cli_app, ["backup", "write-manifest", "--artifact", str(art)])
    assert r.exit_code == 0 and "db-z.sql.gz" in r.output

    mf = art.with_name(art.name + ".manifest.json")
    r2 = runner.invoke(cli_app, ["backup", "verify-manifest", "--manifest", str(mf), "--write-marker"])
    assert r2.exit_code == 0 and "OK" in r2.output and marker.is_file()

    r3 = runner.invoke(cli_app, ["backup", "status"])
    assert r3.exit_code == 0 and "db-z.sql.gz" in r3.output
    for out in (r.output, r2.output, r3.output):
        assert str(tmp_path) not in out and "/Users/" not in out

    art.write_bytes(b"dumpX" * 50)  # same size, different content
    r4 = runner.invoke(cli_app, ["backup", "verify-manifest", "--manifest", str(mf)])
    assert r4.exit_code == 1 and "sha256_mismatch" in r4.output


# --------------------------------------------------------------------------- #
# 5. release-check integration + backup-readiness API
# --------------------------------------------------------------------------- #
def test_release_check_has_backup_integrity_checks_warn_when_unset(settings, session):
    from app.services import production_check as pc

    r = pc.release_check(session, get_settings())
    by = {c["name"]: c for c in r["checks"]}
    assert {"backup_manifest", "backup_verified", "restore_rehearsal"} <= set(by)
    for name in ("backup_manifest", "backup_verified", "restore_rehearsal"):
        assert by[name]["status"] == "warn"


def test_release_check_backup_integrity_pass_when_fresh(settings, session, tmp_path, monkeypatch):
    from app.services import backup_manifest as bm
    from app.services import production_check as pc

    summary = tmp_path / "cfg" / "last_backup_manifest.json"
    verified = tmp_path / "cfg" / "last_backup_verified"
    rehearsal = tmp_path / "cfg" / "last_restore_rehearsal"
    art = tmp_path / "db-fresh.sql.gz"
    art.write_bytes(b"dump")
    bm.write_backup_manifest(art, schema_head="e5f6a7b8c9d0", summary_file=summary)
    bm.touch_marker(str(verified))
    bm.touch_marker(str(rehearsal))
    monkeypatch.setenv("BACKUP_MANIFEST_SUMMARY_FILE", str(summary))
    monkeypatch.setenv("BACKUP_VERIFIED_MARKER_FILE", str(verified))
    monkeypatch.setenv("RESTORE_REHEARSAL_MARKER_FILE", str(rehearsal))
    get_settings.cache_clear()

    r = pc.release_check(session, get_settings())
    by = {c["name"]: c for c in r["checks"]}
    assert by["backup_manifest"]["status"] == "pass" and "db-fresh.sql.gz" in by["backup_manifest"]["detail"]
    assert by["backup_verified"]["status"] == "pass"
    assert by["restore_rehearsal"]["status"] == "pass"
    blob = json.dumps(r)
    assert str(tmp_path) not in blob and "/Users/" not in blob


# --------------------------------------------------------------------------- #
# 6. rehearsal scripts / compose template static guards (never touch real stack)
# --------------------------------------------------------------------------- #
def test_new_scripts_are_guarded_and_non_destructive():
    restore = (REPO / "scripts" / "restore-rehearsal.sh").read_text("utf-8")
    migration = (REPO / "scripts" / "migration-rehearsal.sh").read_text("utf-8")
    verify = (REPO / "scripts" / "verify-backup.sh").read_text("utf-8")

    for text in (restore, migration, verify):
        assert "set -euo pipefail" in text
        assert "--include-permanent" not in text
        assert "metadata-run" not in text
        assert "yt-dlp" not in text  # rehearsals never download

    # restore rehearsal: unique-project regex guard + guarded teardown only
    assert 'ya-rehearsal-[0-9]+-[0-9]+$' in restore
    down_lines = [ln for ln in restore.splitlines()
                  if "down -v" in ln and not ln.lstrip().startswith("#")]
    assert len(down_lines) == 1  # ONLY inside teardown()
    teardown_block = restore.split("teardown() {", 1)[1].split("\n}", 1)[0]
    assert 'down -v' in teardown_block
    # every real-project compose interaction is read-only (exec/ps)
    for ln in restore.splitlines():
        if "docker compose" in ln and '"$REAL_PROJECT"' in ln:
            assert (" exec -T " in ln or " ps -q" in ln), f"non-read-only real-project call: {ln}"
    # real volume names may appear in a docker command ONLY as read-only inspect
    for ln in restore.splitlines():
        if ("pgdata" in ln or "redisdata" in ln) and "docker" in ln:
            assert "volume inspect" in ln, f"volume ref in docker command: {ln}"

    # migration rehearsal: docker-run only (no compose at all), guarded rm, no downgrade
    mig_code = [ln for ln in migration.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("docker compose" in ln for ln in mig_code)
    assert "ya-migrehearsal-" in migration
    assert not any("alembic downgrade" in ln for ln in mig_code)
    for ln in mig_code:
        if "docker rm" in ln:
            assert '"$NAME"' in ln

    # verify-backup: no teardown/removal at all
    for text in (verify,):
        assert "down" not in text.replace("shutdown", "")
        assert "rm -rf" not in text


def test_rehearsal_compose_template_is_isolated():
    text = (REPO / "docker-compose.restore-rehearsal.yml").read_text("utf-8")
    # separate volume namespace — never the real pgdata/redisdata names
    assert "rehearsal_pgdata" in text and "rehearsal_redisdata" in text
    import re

    for m in re.finditer(r"^\s+-\s+(pgdata|redisdata):", text, re.M):
        raise AssertionError(f"real volume name referenced: {m.group(0)}")
    # binds only under ${REHEARSAL_ROOT}; never the real data/secrets/host paths
    for bad in ("./data", "./secrets", "ARCHIVE_HOST_PATH", "CONFIG_HOST_PATH",
                "LOG_HOST_PATH", "TAKEOUT_HOST_PATH", "env_file: .env"):
        assert bad not in text, f"template references real path: {bad}"
    # loopback-only publishing; no scheduler service (nothing can enqueue work)
    assert '127.0.0.1:${REHEARSAL_WEB_PORT' in text
    assert "0.0.0.0" not in text
    assert "scheduler" not in text.split("volumes:")[0].replace("# ", "").split("services:")[1] \
        or "scheduler:" not in text
    # no secrets / no real host paths committed
    for bad in ("PASSWORD=", "scrypt$", "/Users/", "/Volume"):
        assert bad not in text


def test_acceptance_report_builder_logic(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "restore_acceptance_report", REPO / "scripts" / "restore_acceptance_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    items = tmp_path / "items.tsv"
    items.write_text(
        "db_restore\tpass\tno\trestored db-x.sql.gz\n"
        "archive_check_full_isolated\tfail\tyes\t1927/1930 missing - expected in isolation\n"
        "auth_login\tpass\tno\tok\n",
        encoding="utf-8")
    r = mod.build(items, project="ya-rehearsal-1-2", dump="db-x.sql.gz")
    assert r["ok"] is True and r["summary"]["fail_expected"] == 1 \
        and r["summary"]["fail_unexpected"] == 0

    items.write_text("boom\tfail\tno\tunexpected failure\n", encoding="utf-8")
    r2 = mod.build(items, project="p", dump="d")
    assert r2["ok"] is False and r2["summary"]["fail_unexpected"] == 1

    # host-path markers in details are withheld and fail the build
    items.write_text("leak\tpass\tno\tsaw /Users/someone/secret\n", encoding="utf-8")
    r3 = mod.build(items, project="p", dump="d")
    assert r3["ok"] is False and r3["items"][0]["detail"].startswith("[detail withheld")


def test_migration_rehearsal_checks_guard(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "migration_rehearsal_checks", REPO / "scripts" / "migration_rehearsal_checks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://archiver:x@postgres:5432/archiver")
    with pytest.raises(SystemExit) as exc:
        mod._engine()
    assert exc.value.code == 9  # refuses a non-rehearsal database


def test_backup_readiness_service_and_api(settings, session, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as main_mod
    from app.services import backup_manifest as bm

    summary = tmp_path / "cfg" / "last_backup_manifest.json"
    art = tmp_path / "db-ready.sql.gz"
    art.write_bytes(b"dump")
    bm.write_backup_manifest(art, schema_head="e5f6a7b8c9d0", summary_file=summary)
    monkeypatch.setenv("BACKUP_MANIFEST_SUMMARY_FILE", str(summary))
    get_settings.cache_clear()

    from app.services import production_check as pc

    d = pc.backup_readiness(get_settings())
    assert d["manifest"]["artifact"] == "db-ready.sql.gz"
    assert {"backup_freshness", "backup_manifest", "backup_verified", "restore_rehearsal"} \
        <= {c["name"] for c in d["checks"]}

    client = TestClient(main_mod.app)
    resp = client.get("/api/system/backup-readiness")
    assert resp.status_code == 200
    body = resp.json()
    assert body["manifest"]["artifact"] == "db-ready.sql.gz"
    assert body["manifest"]["schema_head"] == "e5f6a7b8c9d0"
    blob = json.dumps(body)
    assert str(tmp_path) not in blob and "/Users/" not in blob
