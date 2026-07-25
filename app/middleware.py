"""Phase 9C/9D/9E: pure-ASGI auth + security-headers + request-id + audit middleware.

Enforces auth for ``/api/*`` (public: health, auth endpoints, static SPA), adds
security response headers + an ``X-Request-ID``, validates the Host header, runs
CSRF/origin checks on cookie-session mutations, and records auth-rejection audit
events (pseudonymised — never raw IP/email).

Pure ASGI (not BaseHTTPMiddleware) so it never buffers the response BODY — media
``Range``/206 streaming is untouched; only response HEADERS are augmented.
Never logs secret values (cookies, tokens, header values, raw IP/email).
"""

from __future__ import annotations

import re
import uuid

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import get_settings
from app.services import auth

_PUBLIC_EXACT = {
    "/health", "/health/live", "/health/ready",
    "/api/health", "/api/auth/login", "/api/auth/logout", "/api/auth/session",
}
_PROTECTED_NONAPI = {"/docs", "/redoc", "/openapi.json"}
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
_NOSTORE_PREFIXES = ("/api/auth", "/api/system", "/api/settings", "/api/audit")
_RID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_CSP = (
    "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; object-src 'none'; "
    "img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; form-action 'self'"
)


def _is_public(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    if path.startswith("/api/"):
        return False
    if path in _PROTECTED_NONAPI:
        return False
    return True  # SPA index + static assets + client-side routes


def direct_peer_ip(scope) -> str | None:
    client = scope.get("client")
    return client[0] if client else None


def _request_id(headers) -> str:
    rid = headers.get("x-request-id")
    if rid and _RID_RE.match(rid):
        return rid
    return uuid.uuid4().hex


def _route_template(path: str) -> str:
    # collapse numeric ids to keep audit/metrics cardinality low
    return re.sub(r"/\d+", "/{id}", path)[:120]


def _security_headers(settings) -> dict[str, str]:
    h = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": _CSP,
        "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    }
    if settings.effective_hsts:
        h["Strict-Transport-Security"] = f"max-age={settings.hsts_max_age_seconds}; includeSubDomains"
    return h


def _audit_reject(settings, event_type, outcome, severity, request, peer, principal, request_id, reason):
    """Record an auth-rejection audit event (best effort; never breaks the request)."""
    try:
        from app.services import audit

        audit.record_event_autocommit(
            settings=settings, event_type=event_type, category="auth",
            severity=severity, outcome=outcome,
            actor_kind=(principal.via if principal else "anonymous"),
            actor_id_hash=(audit.pseudonymize(settings, principal.sub, kind=principal.via)
                           if principal else None),
            client_id_hash=audit.pseudonymize(settings, peer, kind="ip"),
            request_id=request_id, correlation_id=request_id, reason_code=reason,
            metadata={"method": request.method, "route": _route_template(request.url.path)},
        )
    except Exception:  # noqa: BLE001
        pass


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    def _wrap_send(self, send, settings, path, request_id):
        headers = _security_headers(settings) if settings.security_headers_enabled else {}
        nostore = any(path.startswith(p) for p in _NOSTORE_PREFIXES)

        async def wrapped(message):
            if message["type"] == "http.response.start":
                mh = MutableHeaders(raw=message["headers"])
                for k, v in headers.items():
                    mh.setdefault(k, v)
                mh.setdefault("X-Request-ID", request_id)
                if nostore:
                    mh["Cache-Control"] = "no-store"
            await send(message)

        return wrapped

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        settings = get_settings()
        request = Request(scope)
        method = scope.get("method", "GET")
        path = scope.get("path", "")
        request_id = _request_id(request.headers)
        corr = request.headers.get("x-correlation-id")
        correlation_id = corr if (corr and _RID_RE.match(corr)) else request_id
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["correlation_id"] = correlation_id
        send2 = self._wrap_send(send, settings, path, request_id)
        peer = direct_peer_ip(scope)

        allowed_hosts = settings.allowed_hosts_list
        if allowed_hosts:
            host = (request.headers.get("host") or "").split(":")[0].strip().lower()
            if host not in allowed_hosts:
                _audit_reject(settings, "host_rejected", "denied", "warning", request, peer, None,
                              request_id, "host_not_allowed")
                return await JSONResponse({"detail": "invalid host"}, status_code=400)(scope, receive, send2)

        principal = auth.resolve_principal(
            settings, cookies=request.cookies, headers=request.headers, client_ip=peer)
        state["principal"] = principal

        if method == "OPTIONS" or _is_public(path):
            return await self.app(scope, receive, send2)

        if principal is None:
            mode = (settings.auth_mode or "disabled").strip().lower()
            if mode == "trusted_proxy":
                _audit_reject(settings, "trusted_proxy_rejected", "denied", "warning", request, peer,
                              None, request_id, "proxy_untrusted_or_spoof")
            elif method in _MUTATING:
                _audit_reject(settings, "unauthorized", "denied", "warning", request, peer, None,
                              request_id, "no_session")
            return await JSONResponse({"detail": "authentication required"},
                                      status_code=401)(scope, receive, send2)

        if method in _MUTATING and principal.via == "session":
            if not auth.csrf_valid(request.headers.get("x-csrf-token"), principal.csrf):
                _audit_reject(settings, "csrf_rejected", "denied", "warning", request, peer, principal,
                              request_id, "csrf_missing_or_bad")
                return await JSONResponse({"detail": "CSRF validation failed"},
                                          status_code=403)(scope, receive, send2)
            if not auth.origin_allowed(
                request.headers.get("origin"), request.headers.get("referer"),
                host=request.headers.get("host"),
                cors_origins=settings.cors_origins_list,
                trusted_origins=settings.csrf_trusted_origins_list,
            ):
                _audit_reject(settings, "forbidden", "denied", "warning", request, peer, principal,
                              request_id, "origin_not_allowed")
                return await JSONResponse({"detail": "origin not allowed"},
                                          status_code=403)(scope, receive, send2)

        return await self.app(scope, receive, send2)
