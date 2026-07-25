"""Phase 9C: production access control & security hardening.

Auth primitives + production-check security judgments + live middleware behaviour
via TestClient (401 on unauthenticated API, login/logout, CSRF, authenticated
media Range 206). No real downloads. No secret values leak into responses.
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.config import get_settings
from app.models import MediaFile, Video
from app.services import auth
from app.services import production_check as pc


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _local_settings(monkeypatch, tmp_path, *, app_env="development", secure="false",
                    with_secret=True, with_hash=True, password="correct horse"):
    sec = tmp_path / "session_secret"
    sec.write_text(auth.gen_session_secret(), "utf-8")
    ph = tmp_path / "admin_password_hash"
    ph.write_text(auth.hash_password(password), "utf-8")
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("AUTH_MODE", "local")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", secure)
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "http://testserver")
    if with_secret:
        monkeypatch.setenv("SESSION_SECRET_FILE", str(sec))
    if with_hash:
        monkeypatch.setenv("ADMIN_PASSWORD_HASH_FILE", str(ph))
    get_settings.cache_clear()
    auth.reset_rate_limit()
    return get_settings()


def _client() -> TestClient:
    return TestClient(main_mod.app)


def _login(client, password="correct horse"):
    r = client.post("/api/auth/login", json={"password": password},
                    headers={"origin": "http://testserver"})
    return r


def _csrf(client):
    return client.cookies.get(get_settings().csrf_cookie_name)


# --------------------------------------------------------------------------- #
# auth primitives
# --------------------------------------------------------------------------- #
def test_password_hash_roundtrip():
    h = auth.hash_password("hunter2")
    assert h.startswith("scrypt$")
    assert auth.verify_password("hunter2", h)
    assert not auth.verify_password("nope", h)
    assert not auth.verify_password("hunter2", None)
    assert not auth.verify_password("hunter2", "garbage")


def test_session_sign_verify():
    tok, csrf = auth.issue_session("sekret", sub="admin", max_age=100)
    p = auth.verify_session(tok, "sekret")
    assert p and p["sub"] == "admin" and p["csrf"] == csrf
    assert auth.verify_session(tok, "OTHER") is None      # wrong secret
    assert auth.verify_session(tok + "x", "sekret") is None  # tampered
    assert auth.verify_session(tok, "sekret", now=time.time() + 1000) is None  # expired
    assert auth.verify_session(None, "sekret") is None


def test_resolve_principal_modes(settings, monkeypatch):
    # disabled -> anonymous
    monkeypatch.setenv("AUTH_MODE", "disabled"); get_settings.cache_clear()
    s = get_settings()
    assert auth.resolve_principal(s, cookies={}, headers={}, client_ip="1.2.3.4").via == "anonymous"

    # trusted_proxy: only a trusted-CIDR peer with an allow-listed email is accepted
    monkeypatch.setenv("AUTH_MODE", "trusted_proxy")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    monkeypatch.setenv("ALLOWED_ADMIN_EMAILS", "admin@example.com")
    monkeypatch.setenv("TRUSTED_PROXY_AUTH_HEADER", "X-Auth-Email")
    get_settings.cache_clear(); s = get_settings()
    hdr = {"X-Auth-Email": "admin@example.com"}
    # trusted peer + allowed email -> ok
    p = auth.resolve_principal(s, cookies={}, headers=hdr, client_ip="10.1.2.3")
    assert p is not None and p.via == "proxy" and p.sub == "admin@example.com"
    # DIRECT (untrusted) peer with the same header -> spoof rejected
    assert auth.resolve_principal(s, cookies={}, headers=hdr, client_ip="203.0.113.9") is None
    # trusted peer but email not allow-listed -> rejected
    assert auth.resolve_principal(s, cookies={}, headers={"X-Auth-Email": "evil@x.com"},
                                  client_ip="10.1.2.3") is None


def test_rate_limit_and_helpers():
    auth.reset_rate_limit()
    assert all(auth.rate_limit_ok("9.9.9.9", max_attempts=3, window=60) for _ in range(3))
    assert not auth.rate_limit_ok("9.9.9.9", max_attempts=3, window=60)  # 4th blocked
    assert auth.ip_in_cidrs("172.16.5.5", ["172.16.0.0/12"]) and not auth.ip_in_cidrs("8.8.8.8", ["10.0.0.0/8"])
    assert auth.csrf_valid("t", "t") and not auth.csrf_valid("t", "z")
    assert auth.origin_allowed("http://h/x", None, host="h", cors_origins=["*"])
    assert not auth.origin_allowed(None, None, host="h", cors_origins=["*"])


# --------------------------------------------------------------------------- #
# production-check security judgments
# --------------------------------------------------------------------------- #
def _find(r, name):
    return next(c for c in r["checks"] if c["name"] == name)


def test_prodcheck_production_auth_disabled_fails(settings, session, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production"); monkeypatch.setenv("AUTH_MODE", "disabled")
    get_settings.cache_clear()
    r = pc.production_check(session, get_settings())
    assert _find(r, "auth_mode")["status"] == "fail"
    assert _find(r, "mutating_api_protection")["status"] == "fail"
    assert r["overall"] == "fail"


def test_prodcheck_development_auth_disabled_ok(settings, session, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development"); monkeypatch.setenv("AUTH_MODE", "disabled")
    get_settings.cache_clear()
    r = pc.production_check(session, get_settings())
    assert _find(r, "auth_mode")["status"] == "pass"


def test_prodcheck_production_local_missing_secrets_fail(settings, session, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production"); monkeypatch.setenv("AUTH_MODE", "local")
    get_settings.cache_clear()
    r = pc.production_check(session, get_settings())
    assert _find(r, "session_secret_readable")["status"] == "fail"
    assert _find(r, "local_password_hash_readable")["status"] == "fail"


def test_prodcheck_production_wildcard_cors_fail(settings, session, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production"); monkeypatch.setenv("CORS_ALLOW_ORIGINS", "*")
    get_settings.cache_clear()
    r = pc.production_check(session, get_settings())
    assert _find(r, "cors_policy")["status"] == "fail"


def test_prodcheck_production_insecure_cookie_fail(settings, session, tmp_path, monkeypatch):
    _local_settings(monkeypatch, tmp_path, app_env="production", secure="false")
    r = pc.production_check(session, get_settings())
    assert _find(r, "secure_cookie")["status"] == "fail"


def test_prodcheck_trusted_proxy_without_cidr_fails(settings, session, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production"); monkeypatch.setenv("AUTH_MODE", "trusted_proxy")
    get_settings.cache_clear()
    r = pc.production_check(session, get_settings())
    assert _find(r, "trusted_proxy_config")["status"] == "fail"


def test_prodcheck_no_secret_or_path_leak(settings, session, tmp_path, monkeypatch):
    import json
    _local_settings(monkeypatch, tmp_path, app_env="production", password="topsecretpw")
    r = pc.production_check(session, get_settings())
    blob = json.dumps(r)
    for bad in ("topsecretpw", "scrypt$", "/secrets/", "/Users/", str(tmp_path)):
        assert bad not in blob


# --------------------------------------------------------------------------- #
# middleware / API enforcement (TestClient)
# --------------------------------------------------------------------------- #
def test_disabled_mode_allows_api(settings, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "disabled"); get_settings.cache_clear()
    c = _client()
    assert c.get("/api/liked-videos/progress").status_code == 200
    assert c.get("/health").status_code == 200


def test_local_mode_requires_auth(settings, tmp_path, monkeypatch):
    _local_settings(monkeypatch, tmp_path)
    c = _client()
    # public endpoints stay open
    assert c.get("/health").status_code == 200
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/auth/session").json()["authenticated"] is False
    # protected API is 401 when unauthenticated
    assert c.get("/api/liked-videos/progress").status_code == 401
    assert c.get("/api/system/production-check").status_code == 401


def test_local_login_logout_flow(settings, tmp_path, monkeypatch):
    _local_settings(monkeypatch, tmp_path)
    c = _client()
    # wrong password -> generic 401
    bad = _login(c, "WRONG")
    assert bad.status_code == 401 and bad.json()["detail"] == "invalid credentials"
    # correct password -> 200 + cookies set
    ok = _login(c)
    assert ok.status_code == 200 and ok.json()["authenticated"] is True
    assert get_settings().session_cookie_name in c.cookies
    # authenticated GET works
    assert c.get("/api/liked-videos/progress").status_code == 200
    # logout clears the session -> back to 401
    c.post("/api/auth/logout", headers={"origin": "http://testserver"})
    c.cookies.clear()
    assert c.get("/api/liked-videos/progress").status_code == 401


def test_csrf_required_for_mutations(settings, tmp_path, monkeypatch):
    _local_settings(monkeypatch, tmp_path)
    c = _client()
    _login(c)
    # POST without CSRF header -> 403
    r1 = c.post("/api/liked-videos/archive-plan", json={},
                headers={"origin": "http://testserver"})
    assert r1.status_code == 403
    # POST with the double-submit CSRF token -> allowed (not 403)
    r2 = c.post("/api/liked-videos/archive-plan", json={},
                headers={"origin": "http://testserver", "x-csrf-token": _csrf(c)})
    assert r2.status_code == 200


def test_trusted_proxy_header_spoof_rejected(settings, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "trusted_proxy")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    monkeypatch.setenv("ALLOWED_ADMIN_EMAILS", "admin@example.com")
    monkeypatch.setenv("TRUSTED_PROXY_AUTH_HEADER", "X-Auth-Email")
    get_settings.cache_clear()
    c = _client()
    # direct client (not a trusted proxy IP) cannot spoof the auth header
    r = c.get("/api/liked-videos/progress", headers={"X-Auth-Email": "admin@example.com"})
    assert r.status_code == 401


def test_media_range_requires_auth_and_streams_206(settings, session, tmp_path, monkeypatch):
    # seed a video + on-disk media file
    v = Video(youtube_video_id="vidmedia001", url="https://www.youtube.com/watch?v=vidmedia001",
              title="t", channel_title="C", first_seen_at=datetime(2025, 1, 1))
    session.add(v); session.flush()
    rel = "chan/vidmedia001/v.mp4"
    fp = settings.archive_root / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(b"\x00" * 4096)
    session.add(MediaFile(video_id=v.id, media_type="video", path=rel, profile="video_compressed_1080p_light"))
    session.commit()
    vid, mid = v.id, session.query(MediaFile).filter_by(video_id=v.id).one().id

    _local_settings(monkeypatch, tmp_path)
    c = _client()
    # unauthenticated Range -> 401 (no file bytes / size leaked)
    un = c.get(f"/api/videos/{vid}/media/{mid}", headers={"range": "bytes=0-1023"})
    assert un.status_code == 401
    # authenticated Range -> 206 partial content
    _login(c)
    ok = c.get(f"/api/videos/{vid}/media/{mid}", headers={"range": "bytes=0-1023"})
    assert ok.status_code == 206
    assert ok.headers.get("content-range", "").startswith("bytes 0-1023/")


def test_login_failure_does_not_leak_secrets(settings, tmp_path, monkeypatch):
    _local_settings(monkeypatch, tmp_path, password="s3cr3t-pass")
    c = _client()
    r = c.post("/api/auth/login", json={"password": "s3cr3t-pass-wrong"},
               headers={"origin": "http://testserver"})
    # generic message; never echoes the attempted password or any hash
    body = r.text
    assert "s3cr3t-pass" not in body and "scrypt$" not in body
