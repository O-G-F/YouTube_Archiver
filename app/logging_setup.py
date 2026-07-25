"""Logging configuration + shared redaction (Phase 9E).

Never log secrets. All log output passes through :func:`redact_text`, which masks
passwords/tokens/cookies/session-secrets, scrypt hashes, host paths, emails and
raw IPs. ``STRUCTURED_LOGGING=true`` emits JSON lines (also redacted). yt-dlp args
are additionally masked at the source via :func:`app.services.ytdlp.redact_args`.
"""

from __future__ import annotations

import json
import logging
import re

from app.config import get_settings

_CONFIGURED = False

# order matters: value-bearing assignments first, then standalone patterns.
_REDACT: list[tuple[re.Pattern, str]] = [
    (re.compile(r'(?i)\b(password|passwd|token|secret|cookie|authorization|csrf|'
                r'po_token|visitor_data|session_secret|api[_-]?key)\b\s*[=:]\s*\S+'), r'\1=[redacted]'),
    (re.compile(r'scrypt\$[A-Za-z0-9$_\-+/=]+'), '[hash]'),
    (re.compile(r'/(?:Users|home|secrets|archive|config|takeout_imports)/[^\s"\']*'), '[path]'),
    (re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.-]+\b'), '[email]'),
    (re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}\b'), '[ip]'),
]


def redact_text(s: str) -> str:
    for pat, repl in _REDACT:
        s = pat.sub(repl, s)
    return s


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "service": record.name,
            "event": redact_text(record.getMessage()),
        }
        for k in ("request_id", "correlation_id", "job_id", "duration_ms", "outcome", "reason_code"):
            v = getattr(record, k, None)
            if v is not None:
                base[k] = v
        return json.dumps(base, default=str)


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level)
    fmt: logging.Formatter = (
        JsonFormatter() if settings.structured_logging
        else RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    )
    for h in logging.getLogger().handlers:
        h.setFormatter(fmt)
    if level > logging.DEBUG:
        logging.getLogger("rq.worker").setLevel(logging.INFO)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
