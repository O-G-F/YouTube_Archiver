"""Logging configuration.

Important (requirement 12): never log cookies or tokens. We only ever log
yt-dlp *arguments* through :func:`app.services.ytdlp.redact_args`, which masks
the values following ``--cookies`` and similar sensitive flags.
"""

from __future__ import annotations

import logging

from app.config import get_settings

_CONFIGURED = False


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    # yt-dlp and rq are noisy at DEBUG; keep them at INFO unless we are debugging.
    if level > logging.DEBUG:
        logging.getLogger("rq.worker").setLevel(logging.INFO)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
