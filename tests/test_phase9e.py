"""Phase 9E: audit trail + observability.

Append-only hash chain, pseudonymised/redacted audit rows, auth+op audit events,
request/correlation id, metrics protection + cardinality, liveness/readiness,
structured-log redaction, and production-check integration. No real downloads.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.main as main_mod
from app.config import get_settings
from app.db import get_session_factory, session_scope
from app.logging_setup import redact_text
from app.models import AuditEvent, _AuditAppendOnly, utcnow
from app.services import audit
from app.services import production_check as pc


def _client() -> TestClient:
    return TestClient(main_mod.app)


def _local(monkeypatch, tmp_path, **extra):
    (tmp_path / "ss").write_text(audit.gen_session_secret() if hasattr(audit, "gen_session_secret")
                                 else __import__("secrets").token_urlsafe(32), "utf-8")
    from app.services import auth
    (tmp_path / "ss").write_text(auth.gen_session_secret(), "utf-8")
    (tmp_path / "ph").write_text(auth.hash_password("pw"), "utf-8")
    env = {"APP_ENV": "development", "AUTH_MODE": "local", "SESSION_COOKIE_SECURE": "false",
           "CORS_ALLOW_ORIGINS": "http://testserver",
           "SESSION_SECRET_FILE": str(tmp_path / "ss"),
           "ADMIN_PASSWORD_HASH_FILE": str(tmp_path / "ph")}
    env.update(extra)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    auth.reset_rate_limit()
    return get_settings()


def _fresh_events(**kw):
    with session_scope() as s:
        return [audit.event_to_dict(e) for e in audit.list_events(s, **kw)]


# --------------------------------------------------------------------------- #
# append-only + hash chain + redaction
# --------------------------------------------------------------------------- #
def test_append_only_orm_update_delete_blocked(settings, session):
    audit.record_event(session, get_settings(), event_type="t", category="ops")
    session.commit()
    ev = session.query(AuditEvent).first()
    with pytest.raises(_AuditAppendOnly):
        ev.outcome = "x"
        session.flush()
    session.rollback()
    with pytest.raises(_AuditAppendOnly):
        session.delete(session.query(AuditEvent).first())
        session.flush()


def test_hash_chain_valid_then_invalid(settings, session):
    s = get_settings()
    for i in range(3):
        audit.record_event(session, s, event_type=f"e{i}", category="auth")
    session.commit()
    assert audit.verify_chain(session, s)["valid"] is True
    session.execute(text("UPDATE audit_events SET outcome='failure' WHERE id=2"))
    session.commit()
    v = audit.verify_chain(session, s)
    assert v["valid"] is False and v["first_invalid_event_id"] == 2


def test_metadata_and_pseudonym_redaction(settings, session):
    s = get_settings()
    ev = audit.record_event(session, s, event_type="x", category="auth",
                            actor_id_hash=audit.pseudonymize(s, "admin@example.com", kind="admin"),
                            client_id_hash=audit.pseudonymize(s, "203.0.113.9", kind="ip"),
                            metadata={"password": "hunter2", "email": "a@b.com", "note": "ok",
                                      "path": "/Users/x/secret", "count": 5})
    session.commit()
    assert ev.metadata_json == {"note": "ok", "count": 5}       # password/email/path dropped
    assert "@" not in (ev.actor_id_hash or "") and "203.0.113.9" not in (ev.client_id_hash or "")


def test_signed_chain_with_hmac_key(settings, session, tmp_path, monkeypatch):
    key = tmp_path / "audit_key"
    key.write_text("super-audit-key", "utf-8")
    monkeypatch.setenv("AUDIT_HMAC_KEY_FILE", str(key))
    get_settings.cache_clear()
    s = get_settings()
    audit.record_event(session, s, event_type="e", category="auth")
    session.commit()
    v = audit.verify_chain(session, s)
    assert v["valid"] is True and v["signed"] is True


def test_retention_cleanup_keeps_chain_valid(settings, session, monkeypatch):
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "30")
    monkeypatch.setenv("AUDIT_SECURITY_RETENTION_DAYS", "30")
    get_settings.cache_clear()
    s = get_settings()
    old = utcnow() - timedelta(days=60)
    for i in range(3):
        audit.record_event(session, s, event_type=f"old{i}", category="ops", occurred_at=old)
    for i in range(2):
        audit.record_event(session, s, event_type=f"new{i}", category="ops")
    session.commit()
    r = audit.cleanup(session, s)
    assert r["deleted"] == 3
    assert audit.verify_chain(session, s)["valid"] is True  # checkpoint keeps chain valid


def test_export_cap(settings, session, monkeypatch):
    monkeypatch.setenv("AUDIT_MAX_EXPORT_EVENTS", "2")
    get_settings.cache_clear()
    s = get_settings()
    for i in range(5):
        audit.record_event(session, s, event_type=f"e{i}", category="ops")
    session.commit()
    assert len(list(audit.export_events(session, s))) == 2


def test_log_redaction():
    red = redact_text("password=hunter2 token: abc user a@b.com ip 10.1.2.3 path /Users/x/f scrypt$16$s$h")
    assert "hunter2" not in red and "a@b.com" not in red and "10.1.2.3" not in red
    assert "/Users/x" not in red and "scrypt$16" not in red


# --------------------------------------------------------------------------- #
# middleware / API: request id, auth audit, metrics, readiness
# --------------------------------------------------------------------------- #
def test_request_id_generated_validated_returned(settings, monkeypatch):
    get_settings.cache_clear()
    c = _client()
    r = c.get("/health")
    assert r.headers.get("x-request-id")                       # generated
    ok = c.get("/health", headers={"x-request-id": "my.valid_id-1"})
    assert ok.headers.get("x-request-id") == "my.valid_id-1"   # valid echoed
    bad = c.get("/health", headers={"x-request-id": "bad id !!"})
    assert bad.headers.get("x-request-id") != "bad id !!"      # invalid regenerated


def test_login_events_audited(settings, tmp_path, monkeypatch):
    _local(monkeypatch, tmp_path)
    c = _client()
    c.post("/api/auth/login", json={"password": "WRONG"}, headers={"origin": "http://testserver"})
    c.post("/api/auth/login", json={"password": "pw"}, headers={"origin": "http://testserver"})
    types = {e["event_type"] for e in _fresh_events(category="auth", limit=20)}
    assert "login_failure" in types and "login_success" in types


def test_csrf_rejection_audited(settings, tmp_path, monkeypatch):
    _local(monkeypatch, tmp_path)
    c = _client()
    c.post("/api/auth/login", json={"password": "pw"}, headers={"origin": "http://testserver"})
    c.post("/api/liked-videos/archive-plan", json={}, headers={"origin": "http://testserver"})  # no CSRF
    assert any(e["event_type"] == "csrf_rejected" for e in _fresh_events(category="auth", limit=20))


def test_host_rejection_audited(settings, tmp_path, monkeypatch):
    _local(monkeypatch, tmp_path, ALLOWED_HOSTS="good.example.com")
    c = _client()
    assert c.get("/health", headers={"host": "evil.com"}).status_code == 400
    assert any(e["event_type"] == "host_rejected" for e in _fresh_events(category="auth", limit=20))


def test_correlation_id_propagates_to_audit(settings, tmp_path, monkeypatch):
    _local(monkeypatch, tmp_path)
    c = _client()
    c.post("/api/auth/login", json={"password": "pw"}, headers={"origin": "http://testserver"})
    csrf = c.cookies.get(get_settings().csrf_cookie_name)
    c.post("/api/liked-videos/archive-plan", json={},
           headers={"origin": "http://testserver", "x-csrf-token": csrf, "x-correlation-id": "corr-123"})
    ev = _fresh_events(correlation_id="corr-123", limit=5)
    assert any(e["event_type"] == "archive_plan_requested" for e in ev)


def test_metrics_requires_auth_and_no_identity_labels(settings, tmp_path, monkeypatch):
    _local(monkeypatch, tmp_path)
    c = _client()
    assert c.get("/api/system/metrics").status_code == 401       # unauth
    c.post("/api/auth/login", json={"password": "pw"}, headers={"origin": "http://testserver"})
    r = c.get("/api/system/metrics")
    assert r.status_code == 200 and "ytarch_jobs_active" in r.text
    assert "@" not in r.text and "video_id" not in r.text and "channel" not in r.text


def test_liveness_and_readiness(settings, monkeypatch):
    get_settings.cache_clear()
    c = _client()
    assert c.get("/health/live").json() == {"status": "ok"}
    ready = c.get("/health/ready")
    assert ready.status_code in (200, 503) and "ready" in ready.json()


def test_production_check_includes_audit_checks(settings, session, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_MODE", "local")
    get_settings.cache_clear()
    r = pc.production_check(session, get_settings())
    names = {c["name"] for c in r["checks"]}
    assert {"audit_enabled", "audit_current_key_configured", "audit_chain_valid", "https_readiness"} <= names
    # production + no audit key -> FAIL
    assert next(c for c in r["checks"] if c["name"] == "audit_current_key_configured")["status"] == "fail"
