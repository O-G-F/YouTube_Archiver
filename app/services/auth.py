"""Phase 9C: production access control primitives (stdlib-only, no new deps).

* Password hashing: ``hashlib.scrypt`` (memory-hard KDF; PHC-style string).
* Sessions: HMAC-SHA256 signed, stateless token with an embedded CSRF secret.
* Trusted-proxy auth: accept an auth header ONLY when the direct peer IP is a
  configured trusted proxy AND the identity is allow-listed.
* CSRF (double-submit) + Origin/Referer checks for cookie-session mutations.
* Lightweight per-IP login rate limit.

NEVER logs or returns secret VALUES (passwords, hashes, session secret, tokens,
CSRF tokens, cookie contents, proxy header values).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from app.config import Settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

# scrypt parameters (interactive-login strength; memory ~16MB).
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# --------------------------------------------------------------------------- #
# password hashing (scrypt)
# --------------------------------------------------------------------------- #
def hash_password(password: str, *, n: int = _SCRYPT_N, r: int = _SCRYPT_R, p: int = _SCRYPT_P) -> str:
    """scrypt hash as ``scrypt$n$r$p$salt$hash`` (never store plaintext)."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_SCRYPT_DKLEN)
    return f"scrypt${n}${r}${p}${_b64e(salt)}${_b64e(dk)}"


def verify_password(password: str, encoded: str | None) -> bool:
    """Constant-time verify against a stored scrypt hash. False on any malformation."""
    if not encoded:
        return False
    try:
        scheme, n, r, p, salt_b, hash_b = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = _b64d(hash_b)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=_b64d(salt_b),
                            n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except Exception:  # noqa: BLE001 - malformed hash => reject
        return False


def needs_rehash(encoded: str | None, *, n: int = _SCRYPT_N, r: int = _SCRYPT_R, p: int = _SCRYPT_P) -> bool:
    """True if a stored hash uses different cost params than the current defaults
    (so the operator can regenerate it) — the ``scrypt$n$r$p$salt$hash`` format
    carries algorithm + cost + salt + digest, so a future upgrade is detectable.
    A malformed / non-scrypt hash also needs rehashing."""
    if not encoded:
        return False
    try:
        scheme, en, er, ep, _s, _h = encoded.split("$")
        return not (scheme == "scrypt" and int(en) == n and int(er) == r and int(ep) == p)
    except Exception:  # noqa: BLE001
        return True


# --------------------------------------------------------------------------- #
# signed session (HMAC-SHA256, stateless)
# --------------------------------------------------------------------------- #
def _sign(payload_b64: str, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64e(mac)


def issue_session(secret: str, *, sub: str, max_age: int, now: float | None = None) -> tuple[str, str]:
    """Return (session_token, csrf_token). Payload carries iat/exp/csrf plus a
    random ``jti`` nonce. CSRF is signed inside AND returned for the double-submit
    cookie."""
    now = time.time() if now is None else now
    csrf = secrets.token_urlsafe(24)
    payload = {"sub": sub, "iat": int(now), "exp": int(now + max_age),
               "csrf": csrf, "jti": secrets.token_urlsafe(9)}
    payload_b64 = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64, secret)}", csrf


def verify_session(token: str | None, secret: "str | list[str] | None", *,
                   now: float | None = None, max_future_skew: int = 60) -> dict | None:
    """Return the payload if the token is validly signed and time-valid, else None.

    ``secret`` may be a single key or a list (current + previous, for rotation):
    the signature must match at least one (each compared in constant time).
    Rejects expired, future-dated (iat beyond a small skew), and malformed tokens.
    """
    if not token or not secret:
        return None
    keys = [secret] if isinstance(secret, str) else [s for s in secret if s]
    if not keys:
        return None
    try:
        payload_b64, sig = token.split(".", 1)
    except ValueError:
        return None
    if not any(hmac.compare_digest(sig, _sign(payload_b64, k)) for k in keys):
        return None
    try:
        payload = json.loads(_b64d(payload_b64))
    except Exception:  # noqa: BLE001
        return None
    now = time.time() if now is None else now
    if int(payload.get("exp", 0)) < now:
        return None
    if int(payload.get("iat", 0)) > now + max_future_skew:  # future-dated => reject
        return None
    return payload


def gen_session_secret() -> str:
    return secrets.token_urlsafe(48)


# --------------------------------------------------------------------------- #
# trusted-proxy IP check + principal resolution
# --------------------------------------------------------------------------- #
def ip_in_cidrs(ip: str | None, cidrs: list[str]) -> bool:
    if not ip or not cidrs:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for c in cidrs:
        try:
            if addr in ipaddress.ip_network(c, strict=False):
                return True
        except ValueError:
            continue
    return False


@dataclass
class Principal:
    sub: str
    via: str  # "session" | "proxy" | "anonymous"
    csrf: str | None = None


ANONYMOUS = Principal(sub="dev", via="anonymous")


def resolve_principal(settings: Settings, *, cookies, headers, client_ip: str | None,
                      now: float | None = None) -> Principal | None:
    """Resolve the authenticated principal, or None if unauthenticated.

    ``headers`` must support case-insensitive ``.get`` (Starlette Headers or a
    lower-cased dict). Never logs header/cookie values.
    """
    mode = (settings.auth_mode or "disabled").strip().lower()
    if mode == "disabled":
        return ANONYMOUS
    if mode == "local":
        payload = verify_session(cookies.get(settings.session_cookie_name),
                                 settings.session_secrets(), now=now)
        if payload:
            return Principal(sub=str(payload.get("sub", "admin")), via="session", csrf=payload.get("csrf"))
        return None
    if mode == "trusted_proxy":
        # Only trust the auth header when the DIRECT peer is a configured proxy.
        if not ip_in_cidrs(client_ip, settings.trusted_proxy_cidr_list):
            return None
        email = (headers.get(settings.trusted_proxy_auth_header) or "").strip().lower()
        if not email:
            return None
        allow = settings.allowed_admin_email_list
        if not allow or email not in allow:  # fail closed if no allow-list
            return None
        return Principal(sub=email, via="proxy")
    return None


# --------------------------------------------------------------------------- #
# CSRF (double-submit) + Origin/Referer checks
# --------------------------------------------------------------------------- #
def csrf_valid(header_token: str | None, session_csrf: str | None) -> bool:
    if not header_token or not session_csrf:
        return False
    return hmac.compare_digest(header_token, session_csrf)


def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return urlparse(url).netloc or None
    except ValueError:
        return None


def origin_allowed(origin: str | None, referer: str | None, *, host: str | None,
                   cors_origins: list[str] | None = None,
                   trusted_origins: list[str] | None = None) -> bool:
    """For cookie-session mutations: Origin/Referer host must match the request
    host, a CSRF-trusted origin, or a (non-wildcard) CORS origin. Missing both
    Origin and Referer => reject (fail closed)."""
    src_host = _host_of(origin) or _host_of(referer)
    if not src_host:
        return False
    for lst in (trusted_origins or [], (cors_origins or []) if (cors_origins or []) != ["*"] else []):
        for o in lst:
            if _host_of(o) == src_host:
                return True
    return bool(host and src_host == host)


# --------------------------------------------------------------------------- #
# login rate limit (in-process, per client IP)
# --------------------------------------------------------------------------- #
_login_attempts: dict[str, list[float]] = {}


def rate_limit_ok(ip: str | None, *, max_attempts: int, window: int, now: float | None = None) -> bool:
    """Record an attempt and return True if under the limit, else False."""
    now = time.time() if now is None else now
    key = ip or "unknown"
    q = [t for t in _login_attempts.get(key, []) if t > now - window]
    if len(q) >= max_attempts:
        _login_attempts[key] = q
        return False
    q.append(now)
    _login_attempts[key] = q
    return True


def reset_rate_limit() -> None:
    _login_attempts.clear()


# --------------------------------------------------------------------------- #
# Phase 9D: Redis-backed login rate limit (shared across workers, TTL)
# --------------------------------------------------------------------------- #
def _rl_key(settings: Settings, ip: str | None) -> str:
    """HMAC-anonymised rate-limit key — the raw client IP is never stored."""
    secret = (settings.session_secret() or settings.rq_queue or "ratelimit").encode("utf-8")
    digest = hmac.new(secret, (ip or "unknown").encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    return f"ratelimit:login:{digest}"


def _redis_rate_limit(settings: Settings, ip: str | None, *, max_attempts: int,
                      window: int) -> tuple[bool, int]:
    from app.worker.queue import get_redis

    r = get_redis()
    key = _rl_key(settings, ip)
    count = int(r.incr(key))
    if count == 1:
        r.expire(key, window)
    ttl = r.ttl(key)
    ttl = window if (ttl is None or ttl < 0) else int(ttl)
    allowed = count <= max_attempts
    return allowed, (0 if allowed else ttl)


def rate_limit_login(settings: Settings, ip: str | None, *, now: float | None = None) -> tuple[bool, int]:
    """Record a login attempt; return (allowed, retry_after_seconds).

    Backend redis/auto uses Redis (shared across workers, TTL). On Redis failure:
    FAIL-CLOSED in production (or when backend=="redis"); in-memory fallback in
    development ("auto"). Never logs the client IP or any secret.
    """
    max_attempts = settings.login_rate_limit_max_attempts
    window = settings.login_rate_limit_window_seconds
    backend = (settings.login_rate_limit_backend or "auto").strip().lower()
    if backend in ("auto", "redis"):
        try:
            return _redis_rate_limit(settings, ip, max_attempts=max_attempts, window=window)
        except Exception as exc:  # noqa: BLE001 - Redis down
            logger.warning("rate limit: redis backend unavailable (%s)", type(exc).__name__)
            if backend == "redis" or settings.is_production:
                return False, window  # fail closed
            # development "auto" -> in-memory fallback
    allowed = rate_limit_ok(ip, max_attempts=max_attempts, window=window, now=now)
    return allowed, (0 if allowed else window)


def rate_limit_backend_status(settings: Settings) -> dict:
    """Effective rate-limit backend for release-check (no secrets / IPs)."""
    backend = (settings.login_rate_limit_backend or "auto").strip().lower()
    reachable = False
    if backend in ("auto", "redis"):
        try:
            from app.worker.queue import get_redis

            get_redis().ping()
            reachable = True
        except Exception:  # noqa: BLE001
            reachable = False
    effective = "redis" if (backend in ("auto", "redis") and reachable) else "memory"
    return {"configured": backend, "redis_reachable": reachable, "effective": effective}
