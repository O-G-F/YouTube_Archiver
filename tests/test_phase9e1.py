"""Phase 9E.1: audit signing lifecycle hardening.

Segment-aware verification distinguishing legitimate (checkpoint-vouched) signing
regime changes — unsigned->signed, key rotation, restore — from tampering. No
real downloads; no secret/key/path leaks.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.models import AuditCheckpoint, _AuditAppendOnly
from app.services import audit
from app.services import production_check as pc

_KEYS = {"a": "key-AAAAAAAA", "b": "key-BBBBBBBB"}


def _settings(monkeypatch, tmp_path, *, key_id=None, prev_ids=(), **extra):
    for k in ("AUDIT_HMAC_KEY_FILE", "AUDIT_HMAC_KEY_ID",
              "AUDIT_HMAC_PREVIOUS_KEY_FILES", "AUDIT_HMAC_PREVIOUS_KEY_IDS"):
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
# chain scenarios
# --------------------------------------------------------------------------- #
def test_legacy_unsigned_only_valid(settings, session, monkeypatch):
    s = _settings(monkeypatch, tmp_path=settings.log_root)  # no key
    for i in range(3):
        audit.record_event(session, s, event_type=f"u{i}", category="ops")
    v = audit.verify_chain(session, s)
    assert v["valid"] and v["unsigned_event_count"] == 3 and v["segment_count"] == 1


def test_signed_only_valid(settings, session, tmp_path, monkeypatch):
    s = _settings(monkeypatch, tmp_path, key_id="a")
    for i in range(3):
        audit.record_event(session, s, event_type=f"s{i}", category="ops")
    v = audit.verify_chain(session, s)
    assert v["valid"] and v["unsigned_event_count"] == 0 and v["signed"] is True


def test_unsigned_to_signed_with_checkpoint_valid(settings, session, tmp_path, monkeypatch):
    dev = _settings(monkeypatch, tmp_path)  # no key
    audit.record_event(session, dev, event_type="u0", category="ops")
    audit.record_event(session, dev, event_type="u1", category="ops")
    s = _settings(monkeypatch, tmp_path, key_id="a")
    audit.establish_signing_boundary(session, s, reason_code="enable", apply=True)
    audit.record_event(session, s, event_type="s0", category="ops")
    v = audit.verify_chain(session, s)
    assert v["valid"] and v["segment_count"] == 2 and v["valid_with_warnings"] is True


def test_unsigned_to_signed_without_checkpoint_invalid(settings, session, tmp_path, monkeypatch):
    dev = _settings(monkeypatch, tmp_path)
    audit.record_event(session, dev, event_type="u0", category="ops")
    s = _settings(monkeypatch, tmp_path, key_id="a")
    audit.record_event(session, s, event_type="s0", category="ops")  # no boundary
    v = audit.verify_chain(session, s)
    assert not v["valid"] and v["failure_reason_code"] == "unexpected_regime_change"


def test_key_rotation_with_checkpoint_valid(settings, session, tmp_path, monkeypatch):
    sa = _settings(monkeypatch, tmp_path, key_id="a")
    audit.record_event(session, sa, event_type="s0", category="ops")
    sb = _settings(monkeypatch, tmp_path, key_id="b", prev_ids=("a",))
    audit.rotate_key(session, sb, reason_code="rot", apply=True)
    audit.record_event(session, sb, event_type="s1", category="ops")
    v = audit.verify_chain(session, sb)
    assert v["valid"] and v["segment_count"] == 2   # a-segment + b-segment (boundary event is in b-seg)


def test_key_rotation_without_checkpoint_invalid(settings, session, tmp_path, monkeypatch):
    sa = _settings(monkeypatch, tmp_path, key_id="a")
    audit.record_event(session, sa, event_type="s0", category="ops")
    sb = _settings(monkeypatch, tmp_path, key_id="b", prev_ids=("a",))
    audit.record_event(session, sb, event_type="s1", category="ops")  # no rotate boundary
    v = audit.verify_chain(session, sb)
    assert not v["valid"] and v["failure_reason_code"] == "unexpected_regime_change"


def test_previous_key_missing_invalid(settings, session, tmp_path, monkeypatch):
    sa = _settings(monkeypatch, tmp_path, key_id="a")
    audit.record_event(session, sa, event_type="s0", category="ops")
    sb = _settings(monkeypatch, tmp_path, key_id="b", prev_ids=("a",))
    audit.rotate_key(session, sb, reason_code="rot", apply=True)
    audit.record_event(session, sb, event_type="s1", category="ops")
    assert audit.verify_chain(session, sb)["valid"]
    sb2 = _settings(monkeypatch, tmp_path, key_id="b")  # drop previous 'a'
    v = audit.verify_chain(session, sb2)
    assert not v["valid"] and v["failure_reason_code"] == "missing_verification_key" and "a" in v["missing_verification_keys"]


def test_tampered_event_invalid(settings, session, tmp_path, monkeypatch):
    s = _settings(monkeypatch, tmp_path, key_id="a")
    audit.record_event(session, s, event_type="s0", category="ops")
    session.commit()
    session.execute(text("UPDATE audit_events SET outcome='x' WHERE id=1"))
    session.commit()
    v = audit.verify_chain(session, s)
    assert not v["valid"] and v["failure_reason_code"] == "event_hash_mismatch"


def test_tampered_checkpoint_invalid(settings, session, tmp_path, monkeypatch):
    dev = _settings(monkeypatch, tmp_path)
    audit.record_event(session, dev, event_type="u0", category="ops")
    s = _settings(monkeypatch, tmp_path, key_id="a")
    audit.establish_signing_boundary(session, s, reason_code="enable", apply=True)
    session.commit()
    session.execute(text("UPDATE audit_checkpoints SET previous_event_hash='deadbeef' WHERE checkpoint_type='signing_enabled'"))
    session.commit()
    v = audit.verify_chain(session, s)
    assert not v["valid"] and v["failure_reason_code"] in ("tampered_checkpoint", "checkpoint_boundary_mismatch")


def test_signed_then_unsigned_event_invalid(settings, session, tmp_path, monkeypatch):
    s = _settings(monkeypatch, tmp_path, key_id="a")
    audit.record_event(session, s, event_type="s0", category="ops")
    dev = _settings(monkeypatch, tmp_path)  # key removed -> next event unsigned
    audit.record_event(session, dev, event_type="u_after", category="ops")
    v = audit.verify_chain(session, s if False else _settings(monkeypatch, tmp_path, key_id="a"))
    assert not v["valid"] and v["failure_reason_code"] == "unexpected_regime_change"


def test_restore_boundary_attests_mixed_prefix(settings, session, tmp_path, monkeypatch):
    dev = _settings(monkeypatch, tmp_path)
    audit.record_event(session, dev, event_type="u0", category="ops")
    s = _settings(monkeypatch, tmp_path, key_id="a")
    audit.record_event(session, s, event_type="s0", category="ops")  # messy (no boundary)
    assert not audit.verify_chain(session, s)["valid"]
    audit.establish_signing_boundary(session, s, reason_code="dev_restore",
                                     checkpoint_type="restore_boundary", apply=True)
    audit.record_event(session, s, event_type="s1", category="ops")
    v = audit.verify_chain(session, s)
    assert v["valid"] and v["valid_with_warnings"] is True


def test_retention_keeps_boundary_and_valid(settings, session, tmp_path, monkeypatch):
    from datetime import timedelta
    from app.models import utcnow
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "30")
    monkeypatch.setenv("AUDIT_SECURITY_RETENTION_DAYS", "30")
    dev = _settings(monkeypatch, tmp_path)
    old = utcnow() - timedelta(days=60)
    audit.record_event(session, dev, event_type="old", category="ops", occurred_at=old)
    s = _settings(monkeypatch, tmp_path, key_id="a", AUDIT_RETENTION_DAYS="30", AUDIT_SECURITY_RETENTION_DAYS="30")
    audit.establish_signing_boundary(session, s, reason_code="enable", apply=True)
    audit.record_event(session, s, event_type="s0", category="ops")
    r = audit.cleanup(session, s)
    # the old event is referenced by the signing boundary -> retention must NOT prune it
    assert r["deleted"] == 0
    assert audit.verify_chain(session, s)["valid"]


# --------------------------------------------------------------------------- #
# append-only (checkpoints) + policy + production-check + leak
# --------------------------------------------------------------------------- #
def test_checkpoint_append_only(settings, session, tmp_path, monkeypatch):
    dev = _settings(monkeypatch, tmp_path)
    audit.record_event(session, dev, event_type="u0", category="ops")
    s = _settings(monkeypatch, tmp_path, key_id="a")
    audit.establish_signing_boundary(session, s, reason_code="enable", apply=True)
    cp = session.query(AuditCheckpoint).first()
    with pytest.raises(_AuditAppendOnly):
        cp.reason_code = "x"
        session.flush()


def test_production_policy_legacy_prefix_rejected(settings, session, tmp_path, monkeypatch):
    dev = _settings(monkeypatch, tmp_path)
    audit.record_event(session, dev, event_type="u0", category="ops")
    s = _settings(monkeypatch, tmp_path, key_id="a", APP_ENV="production", AUTH_MODE="local",
                  AUDIT_ALLOW_LEGACY_UNSIGNED_PREFIX="false")
    audit.establish_signing_boundary(session, s, reason_code="enable", apply=True)
    audit.record_event(session, s, event_type="s0", category="ops")
    r = pc.production_check(session, s)
    assert next(c for c in r["checks"] if c["name"] == "audit_unsigned_event_count")["status"] == "fail"


def test_signing_status_and_no_leak(settings, session, tmp_path, monkeypatch):
    s = _settings(monkeypatch, tmp_path, key_id="a")
    audit.record_event(session, s, event_type="s0", category="ops",
                       actor_id_hash=audit.pseudonymize(s, "admin@x.com", kind="admin"))
    st = audit.signing_status(session, s)
    v = audit.verify_chain(session, s)
    blob = json.dumps(st) + json.dumps(v) + json.dumps(audit.event_to_dict(session.query(__import__("app.models", fromlist=["AuditEvent"]).AuditEvent).first()))
    for bad in (_KEYS["a"], "@x.com", "/Users/", "/secrets/", str(tmp_path)):
        assert bad not in blob
    assert st["current_key_id"] == "a" and st["chain_valid"] is True
