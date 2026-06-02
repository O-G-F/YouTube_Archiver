"""Redis connection and RQ queue helpers."""

from __future__ import annotations

import functools

from redis import Redis
from rq import Queue

from app.config import get_settings


@functools.lru_cache(maxsize=1)
def get_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url)


def get_queue() -> Queue:
    return Queue(get_settings().rq_queue, connection=get_redis())
