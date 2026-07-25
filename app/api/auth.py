"""Phase 9C: authentication endpoints (login / logout / session).

``/api/auth/*`` is public (the SPA must reach it before authenticating). Login is
local-mode only (scrypt hash + signed session cookie); trusted_proxy mode has no
login form (the reverse proxy authenticates). Failures return a single generic
message and never reveal whether auth is misconfigured vs the password was wrong.
Secret values (password, hash, session secret, tokens) are never logged/returned.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from app.config import get_settings
from app.logging_setup import get_logger
from app.schemas import AuthSessionOut, LoginRequest
from app.services import auth

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = get_logger(__name__)

_GENERIC_LOGIN_ERROR = "invalid credentials"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _audit(request: Request, settings, event_type: str, outcome: str, severity: str = "info",
           *, actor: str | None = None, reason: str | None = None) -> None:
    """Record an auth audit event (pseudonymised; best-effort)."""
    try:
        from app.services import audit

        rid = (request.scope.get("state") or {}).get("request_id")
        audit.record_event_autocommit(
            settings=settings, event_type=event_type, category="auth", severity=severity, outcome=outcome,
            actor_kind="admin" if actor else "anonymous",
            actor_id_hash=audit.pseudonymize(settings, actor, kind="admin") if actor else None,
            client_id_hash=audit.pseudonymize(settings, _client_ip(request), kind="ip"),
            request_id=rid, correlation_id=rid, reason_code=reason,
        )
    except Exception:  # noqa: BLE001
        pass


def _set_session_cookies(response: Response, settings, token: str, csrf: str) -> None:
    secure = settings.effective_session_cookie_secure
    samesite = (settings.session_cookie_samesite or "strict").lower()
    max_age = settings.session_max_age_seconds
    # Signed session: HttpOnly so JS can't read it.
    response.set_cookie(settings.session_cookie_name, token, max_age=max_age,
                        httponly=True, secure=secure, samesite=samesite, path="/")
    # Double-submit CSRF token: readable by JS to echo in the X-CSRF-Token header.
    response.set_cookie(settings.csrf_cookie_name, csrf, max_age=max_age,
                        httponly=False, secure=secure, samesite=samesite, path="/")


def _session_out(settings, principal) -> AuthSessionOut:
    mode = (settings.auth_mode or "disabled").lower()
    if principal is not None:
        ident = None if principal.via == "anonymous" else principal.sub
        return AuthSessionOut(authenticated=True, auth_mode=mode, app_env=settings.app_env,
                              identity=ident, login_required=False)
    return AuthSessionOut(authenticated=False, auth_mode=mode, app_env=settings.app_env,
                          identity=None, login_required=(mode == "local"))


@router.get("/session", response_model=AuthSessionOut)
def session_status(request: Request) -> AuthSessionOut:
    """Current auth status for the SPA (public). No secrets returned."""
    settings = get_settings()
    principal = auth.resolve_principal(
        settings, cookies=request.cookies, headers=request.headers, client_ip=_client_ip(request)
    )
    return _session_out(settings, principal)


@router.post("/login", response_model=AuthSessionOut)
def login(payload: LoginRequest, request: Request, response: Response) -> AuthSessionOut:
    settings = get_settings()
    mode = (settings.auth_mode or "disabled").lower()
    if mode != "local":
        # login only applies to local mode
        raise HTTPException(status_code=400, detail="local login is not enabled")

    ip = _client_ip(request)
    allowed, retry_after = auth.rate_limit_login(settings, ip)
    if not allowed:
        _audit(request, settings, "login_rate_limited", "denied", "warning", reason="rate_limited")
        raise HTTPException(status_code=429, detail="too many attempts; try again later",
                            headers={"Retry-After": str(retry_after)})

    secret = settings.session_secret()
    pw_hash = settings.admin_password_hash()
    if not secret or not pw_hash:
        # Misconfiguration: log server-side, return the SAME generic error to the client.
        logger.warning("auth login: local mode not fully configured (session secret / password hash missing)")
        _audit(request, settings, "login_failure", "failure", "warning", reason="not_configured")
        raise HTTPException(status_code=401, detail=_GENERIC_LOGIN_ERROR)

    if not auth.verify_password(payload.password or "", pw_hash):
        _audit(request, settings, "login_failure", "failure", "warning", reason="bad_password")
        raise HTTPException(status_code=401, detail=_GENERIC_LOGIN_ERROR)

    token, csrf = auth.issue_session(secret, sub="admin", max_age=settings.session_max_age_seconds)
    _set_session_cookies(response, settings, token, csrf)
    _audit(request, settings, "login_success", "success", "info", actor="admin")
    return AuthSessionOut(authenticated=True, auth_mode="local", app_env=settings.app_env,
                          identity="admin", login_required=False)


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    """Invalidate the session by clearing the cookies (public; safe to call)."""
    settings = get_settings()
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")
    _audit(request, settings, "logout", "success", "info")
    return {"ok": True}
