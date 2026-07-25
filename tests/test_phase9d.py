"""Phase 9D: ingress + release hardening.

Redis rate-limit fallback/fail-closed + key anonymisation, session rotation /
future-timestamp, security headers (+ HSTS prod-only), Host allow-list, CSRF
trusted origins, release-check, compose static check. No real downloads; Redis is
absent in tests so the fallback/fail-closed paths are exercised directly.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

import app.main as main_mod
from app.config import get_settings
from app.services import auth
from app.services import production_check as pc

REPO = Path(__file__).resolve().parents[1]


def _client() -> TestClient:
    return TestClient(main_mod.app)


def _local(monkeypatch, tmp_path, *, app_env="development", extra=None):
    (tmp_path / "ss").write_text(auth.gen_session_secret(), "utf-8")
    (tmp_path / "ph").write_text(auth.hash_password("correct horse"), "utf-8")
    env = {
        "APP_ENV": app_env, "AUTH_MODE": "local", "SESSION_COOKIE_SECURE": "false",
        "CORS_ALLOW_ORIGINS": "http://testserver",
        "SESSION_SECRET_FILE": str(tmp_path / "ss"),
        "ADMIN_PASSWORD_HASH_FILE": str(tmp_path / "ph"),
    }
    env.update(extra or {})
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    auth.reset_rate_limit()
    return get_settings()


def _login(c):
    return c.post("/api/auth/login", json={"password": "correct horse"},
                  headers={"origin": "http://testserver"})


# --------------------------------------------------------------------------- #
# Redis-backed login rate limit (fallback / fail-closed / anonymisation)
# --------------------------------------------------------------------------- #
def test_rate_limit_memory_fallback_in_dev(settings, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("LOGIN_RATE_LIMIT_BACKEND", "auto")
    monkeypatch.setenv("LOGIN_RATE_LIMIT_MAX_ATTEMPTS", "3")
    get_settings.cache_clear()
    s = get_settings()
    auth.reset_rate_limit()
    # Redis is dead in tests -> "auto" dev falls back to in-memory.
    outcomes = [auth.rate_limit_login(s, "1.2.3.4")[0] for _ in range(4)]
    assert outcomes[:3] == [True, True, True]
    assert outcomes[3] is False


def test_rate_limit_fail_closed_in_production(settings, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LOGIN_RATE_LIMIT_BACKEND", "auto")
    get_settings.cache_clear()
    allowed, retry = auth.rate_limit_login(get_settings(), "9.9.9.9")  # redis down + prod
    assert allowed is False and retry > 0


def test_rate_limit_key_anonymises_ip(settings):
    key = auth._rl_key(get_settings(), "203.0.113.7")
    assert "203.0.113.7" not in key and key.startswith("ratelimit:login:")


def test_rate_limit_backend_status(settings):
    st = auth.rate_limit_backend_status(get_settings())
    assert st["effective"] in ("redis", "memory") and st["redis_reachable"] is False


# --------------------------------------------------------------------------- #
# session hardening
# --------------------------------------------------------------------------- #
def test_session_rotation_accepts_previous_secret():
    tok, _ = auth.issue_session("OLD", sub="admin", max_age=100)
    assert auth.verify_session(tok, ["NEW", "OLD"]) is not None   # rotation window
    assert auth.verify_session(tok, ["NEW"]) is None              # after rotation completes


def test_session_future_timestamp_rejected():
    tok, _ = auth.issue_session("s", sub="a", max_age=1000, now=time.time() + 10_000)
    assert auth.verify_session(tok, "s") is None


def test_session_malformed_and_sig_mismatch():
    tok, _ = auth.issue_session("s", sub="a", max_age=100)
    assert auth.verify_session("not-a-token", "s") is None
    assert auth.verify_session(tok + "x", "s") is None
    assert auth.verify_session(tok, "other") is None


# --------------------------------------------------------------------------- #
# security headers + HSTS + no-store
# --------------------------------------------------------------------------- #
def test_security_headers_present_no_hsts_in_dev(settings, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    r = _client().get("/health")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert "content-security-policy" in r.headers
    assert "referrer-policy" in r.headers
    assert "permissions-policy" in r.headers
    assert "strict-transport-security" not in r.headers  # dev/HTTP => no HSTS


def test_hsts_present_in_production(settings, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    r = _client().get("/health")
    assert "strict-transport-security" in r.headers


def test_no_store_on_sensitive_endpoints(settings, monkeypatch):
    get_settings.cache_clear()
    assert _client().get("/api/auth/session").headers.get("cache-control") == "no-store"


# --------------------------------------------------------------------------- #
# Host allow-list + CSRF trusted origins
# --------------------------------------------------------------------------- #
def test_allowed_hosts_rejects_bad_host(settings, monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "good.example.com")
    get_settings.cache_clear()
    c = _client()
    assert c.get("/health", headers={"host": "evil.com"}).status_code == 400
    assert c.get("/health", headers={"host": "good.example.com"}).status_code == 200


def test_csrf_trusted_origin_allows_cross_origin_mutation(settings, tmp_path, monkeypatch):
    _local(monkeypatch, tmp_path, extra={"CSRF_TRUSTED_ORIGINS": "https://admin.example.com"})
    c = _client()
    _login(c)
    csrf = c.cookies.get(get_settings().csrf_cookie_name)
    r = c.post("/api/liked-videos/archive-plan", json={},
               headers={"origin": "https://admin.example.com", "x-csrf-token": csrf})
    assert r.status_code == 200
    # an UN-trusted origin is still rejected
    r2 = c.post("/api/liked-videos/archive-plan", json={},
                headers={"origin": "https://evil.example.com", "x-csrf-token": csrf})
    assert r2.status_code == 403


# --------------------------------------------------------------------------- #
# production-check ingress judgments
# --------------------------------------------------------------------------- #
def _find(r, name):
    return next(c for c in r["checks"] if c["name"] == name)


def test_prodcheck_production_requires_allowed_hosts_and_csrf(settings, session, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_MODE", "local")
    get_settings.cache_clear()
    r = pc.production_check(session, get_settings())
    assert _find(r, "allowed_hosts")["status"] == "fail"
    assert _find(r, "csrf_trusted_origins")["status"] == "fail"


def test_prodcheck_wildcard_allowed_hosts_fail(settings, session, monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "*")
    get_settings.cache_clear()
    assert _find(pc.production_check(session, get_settings()), "allowed_hosts")["status"] == "fail"


# --------------------------------------------------------------------------- #
# release-check + compose static check
# --------------------------------------------------------------------------- #
def test_release_check_shape_and_extra_checks(settings, session):
    r = pc.release_check(session, get_settings())
    names = {c["name"] for c in r["checks"]}
    assert {"archive_media_presence", "migration_status", "backup_freshness", "build_version"} <= names
    assert r["overall"] in ("pass", "warn", "fail")
    assert set(r["counts"]) == {"pass", "warn", "fail"}


def test_compose_static_check_flags_public_datastore():
    cfg = {"services": {"web": {"ports": ["8000:8000"]},
                        "postgres": {"ports": ["5432:5432"]},
                        "redis": {"ports": ["6379:6379"]}}}
    r = pc.compose_static_check(cfg, production=True)
    by = {c["name"]: c["status"] for c in r["checks"]}
    assert by["compose_web_bind"] == "fail"
    assert by["compose_postgres_publish"] == "fail"
    assert by["compose_redis_publish"] == "fail"
    assert r["overall"] == "fail"


def test_compose_static_check_production_template_is_safe():
    cfg = yaml.safe_load((REPO / "docker-compose.production.example.yml").read_text("utf-8"))
    r = pc.compose_static_check(cfg, production=True)
    by = {c["name"]: c["status"] for c in r["checks"]}
    assert by["compose_web_bind"] == "pass"          # 127.0.0.1 loopback
    assert by["compose_postgres_publish"] == "pass"  # not host-published
    assert by["compose_redis_publish"] == "pass"


def test_production_template_has_no_secrets():
    text = (REPO / "docker-compose.production.example.yml").read_text("utf-8")
    for bad in ("PASSWORD=", "session_secret", "scrypt$", "/Users/", "0.0.0.0:"):
        assert bad not in text


def test_deploy_scripts_are_non_destructive():
    import re

    # matches lines that actually INVOKE compose (not prose/echo/heredoc docs)
    invoke = re.compile(r'^(run\s+|DRY_RUN=\S+\s+|IMAGE_TAG=\S+\s+)*'
                        r'("?\$\{?COMPOSE|\$COMPOSE|docker[- ]compose)\b')
    for name in ("backup.sh", "deploy.sh", "rollback.sh"):
        text = (REPO / "scripts" / name).read_text("utf-8")
        assert "set -euo pipefail" in text
        assert "rm -rf" not in text
        assert "--volumes" not in text
        for ln in text.splitlines():
            s = ln.strip()
            if invoke.match(s):  # a real compose invocation must never be `down`
                assert "down" not in s, f"{name}: {s}"


# --------------------------------------------------------------------------- #
# no leak (release-check has no IP/email/secret/path)
# --------------------------------------------------------------------------- #
def test_release_check_no_secret_or_path_leak(settings, session, tmp_path, monkeypatch):
    _local(monkeypatch, tmp_path, app_env="production",
           extra={"ALLOWED_ADMIN_EMAILS": "admin@example.com",
                  "TRUSTED_PROXY_CIDRS": "10.0.0.0/8"})
    r = pc.release_check(session, get_settings())
    blob = json.dumps(r)
    for bad in ("/Users/", "/secrets/", "scrypt$", "admin@example.com", str(tmp_path)):
        assert bad not in blob
