"""Optional Redis wake-up queue for the durable V6.2 pipeline.

Task state always lives in MySQL. Redis only carries task_no hints so workers can
wake immediately; every worker falls back to claiming eligible rows from MySQL.
This makes queue loss, duplication, or Redis downtime safe.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from .config import get_settings


class DatabaseNotifier:
    """No-op notifier used when workers poll MySQL directly."""

    def notify(self, task_no: str) -> None:
        return None

    def dequeue(self, timeout: float) -> Optional[str]:
        return None


class RedisNotifier:
    def __init__(self, url: str, key: str):
        self.url = url
        self.key = key
        self._client = None

    def _redis(self):
        if self._client is None:
            try:
                import redis
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError(
                    "PIPELINE_QUEUE_BACKEND=redis requires: pip install -e '.[redis]'"
                ) from exc
            self._client = redis.Redis.from_url(self.url, decode_responses=True)
        return self._client

    def notify(self, task_no: str) -> None:
        self._redis().rpush(self.key, task_no)

    def dequeue(self, timeout: float) -> Optional[str]:
        # redis-py BLPOP takes an integer timeout; at least one second keeps this
        # genuinely blocking without creating a busy loop.
        item = self._redis().blpop(self.key, timeout=max(1, int(timeout)))
        if not item:
            return None
        value = item[1]
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)


@lru_cache(maxsize=4)
def _notifier(backend: str, url: str, key: str):
    if backend == "redis":
        return RedisNotifier(url, key)
    if backend == "database":
        return DatabaseNotifier()
    raise ValueError("PIPELINE_QUEUE_BACKEND must be database or redis")


def get_notifier():
    settings = get_settings()
    return _notifier(
        settings.pipeline_queue_backend.lower(),
        settings.pipeline_redis_url,
        settings.pipeline_queue_key,
    )


def notify_task(task_no: str) -> Optional[str]:
    """Best-effort wake-up; return a warning instead of losing the DB task."""

    try:
        get_notifier().notify(task_no)
        return None
    except Exception as exc:  # noqa: BLE001 - DB polling is the fallback
        return f"queue notification failed; task remains durable in DB: {exc}"


def dequeue_hint(timeout: float) -> Optional[str]:
    """Best-effort task hint. A caller must still claim/validate it in MySQL."""

    try:
        return get_notifier().dequeue(timeout)
    except Exception:  # noqa: BLE001 - fall back to DB polling
        return None
